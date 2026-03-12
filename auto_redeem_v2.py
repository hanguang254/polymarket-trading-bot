#!/usr/bin/env python3
"""
Polymarket 自动领取已结算收益脚本 v2.1
- 完全基于 Polymarket CLI（polymarket ctf redeem）
- 不再依赖 py_clob_client / py_builder_relayer_client / poly_web3 等 SDK
- 通过 `polymarket data positions` 获取持仓，筛选已结算市场
- 通过 `polymarket ctf redeem --condition <ID>` 执行链上 redeem
"""
import os
import sys
import json
import time
import logging
import subprocess
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

# ==============================================================================
# 配置
# ==============================================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

# 钱包地址（从 .env 读取，或使用默认值）
# EOA_WALLET:   签名用的 EOA 地址（polymarket wallet address 输出）
# PROXY_WALLET: Polymarket 代理钱包（持仓实际存储地址）
# 可通过 `polymarket wallet show` 查看
EOA_WALLET = os.environ.get("EOA_WALLET", "").strip()
PROXY_WALLET = os.environ.get("PROXY_WALLET", "").strip()
if not EOA_WALLET or not PROXY_WALLET:
    raise RuntimeError("EOA_WALLET and PROXY_WALLET must be set in .env")
SIGNATURE_TYPE = os.environ.get("SIGNATURE_TYPE", "eoa").strip()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

REDEEM_INTERVAL = int(os.environ.get("REDEEM_INTERVAL", 600))  # 默认10分钟

# ==============================================================================
# 日志
# ==============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [REDEEM] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("auto_redeem")

# ==============================================================================
# Telegram 通知
# ==============================================================================

def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"Telegram 通知失败: {e}")

# ==============================================================================
# CLI 工具函数
# ==============================================================================

def run_cli(args: list, timeout: int = 30) -> tuple:
    """
    执行 polymarket CLI 命令

    Returns:
        (success, stdout, stderr)
    """
    cmd = ["polymarket"] + args
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.returncode == 0, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return False, "", "timeout"
    except Exception as e:
        return False, "", str(e)


def _parse_positions_output(stdout: str) -> list:
    """解析 CLI JSON 输出为 position 列表"""
    if not stdout.strip():
        return []
    try:
        data = json.loads(stdout)
        # CLI 返回格式: 直接 [...] 数组
        if isinstance(data, dict):
            return data.get("positions", data.get("data", []))
        elif isinstance(data, list):
            return data
        return []
    except json.JSONDecodeError:
        return []


def fetch_positions() -> list:
    """
    通过 CLI 获取钱包持仓

    同时查 proxy 地址和 EOA 地址，合并去重
    """
    all_positions = []

    for addr, label in [(PROXY_WALLET, "proxy"), (EOA_WALLET, "eoa")]:
        success, stdout, stderr = run_cli([
            "data", "positions", addr,
            "--signature-type", SIGNATURE_TYPE,
            "--limit", "100",
            "-o", "json",
        ], timeout=20)

        if success:
            positions = _parse_positions_output(stdout)
            if positions:
                log.info(f"  {label} 地址: {len(positions)} 个持仓")
                all_positions.extend(positions)
        else:
            err = stderr[:100] if stderr else "unknown"
            log.warning(f"  {label} 地址查询失败: {err}")

    # 也尝试 closed-positions（已关闭的可能需要 redeem）
    for addr in [PROXY_WALLET]:
        success, stdout, _ = run_cli([
            "data", "closed-positions", addr,
            "--signature-type", SIGNATURE_TYPE,
            "--limit", "50",
            "-o", "json",
        ], timeout=20)

        if success:
            closed = _parse_positions_output(stdout)
            if closed:
                log.info(f"  已关闭持仓: {len(closed)} 个")
                all_positions.extend(closed)

    return all_positions


def get_resolved_conditions(positions: list) -> list:
    """
    从持仓中筛选已结算的 condition IDs

    已结算判断条件（满足任一）:
    - resolved == true
    - redeemable == true
    - game_status == "resolved"
    - outcome 字段非空（说明已有结果）

    Returns:
        list of {condition_id, slug, value, neg_risk}
    """
    resolved = []
    seen = set()

    for p in positions:
        condition_id = p.get("condition_id") or p.get("conditionId")
        if not condition_id or condition_id in seen:
            continue

        is_resolved = (
            p.get("resolved") is True
            or p.get("redeemable") is True
            or p.get("game_status") == "resolved"
            or p.get("status") == "resolved"
            or p.get("outcome") not in (None, "", "pending")
        )

        if is_resolved:
            seen.add(condition_id)
            resolved.append({
                "condition_id": condition_id,
                "slug": p.get("slug", p.get("title", "unknown"))[:50],
                "value": float(p.get("current_value", p.get("value", 0)) or 0),
                "neg_risk": p.get("neg_risk", p.get("negRisk", False)),
                "size": float(p.get("size", 0) or 0),
            })

    return resolved


def redeem_condition(condition_id: str, neg_risk: bool = False, size: float = 0) -> bool:
    """
    执行单个 condition 的 redeem

    Args:
        condition_id: 0x 开头的 condition ID
        neg_risk: 是否为 neg-risk 市场
        size: 持仓数量（neg-risk 需要）

    Returns:
        是否成功
    """
    # 确保 condition_id 有 0x 前缀
    if not condition_id.startswith("0x"):
        condition_id = "0x" + condition_id

    if neg_risk:
        # neg-risk 市场: 两个 outcome 的 amount
        # 赢的 outcome 有 size，输的是 0
        amt = str(int(size)) if size > 0 else "1"
        success, stdout, stderr = run_cli([
            "ctf", "redeem-neg-risk",
            "--condition", condition_id,
            "--amounts", f"{amt},{amt}",
            "--signature-type", SIGNATURE_TYPE,
        ], timeout=60)
    else:
        # 普通市场: 直接 redeem，CLI 自动处理 index-sets
        success, stdout, stderr = run_cli([
            "ctf", "redeem",
            "--condition", condition_id,
            "--signature-type", SIGNATURE_TYPE,
        ], timeout=60)

    output = stdout + stderr
    if success:
        log.info(f"  ✅ redeem 成功: {condition_id[:18]}...")
        return True

    err = output[:200].lower()
    # 常见非错误情况
    if any(kw in err for kw in ["already", "nothing to redeem", "no payout",
                                 "zero", "no balance", "no positions"]):
        log.info(f"  ⏭️ 无需领取: {condition_id[:18]}...")
        return True

    log.warning(f"  ❌ redeem 失败: {condition_id[:18]}... | {output[:150]}")
    return False

# ==============================================================================
# 核心逻辑
# ==============================================================================

def get_usdc_balance() -> float:
    """查询当前 USDC 余额"""
    success, stdout, _ = run_cli([
        "clob", "balance",
        "--asset-type", "collateral",
        "--signature-type", SIGNATURE_TYPE,
        "-o", "json",
    ], timeout=15)
    if success:
        try:
            data = json.loads(stdout)
            return float(data.get("balance", 0))
        except (json.JSONDecodeError, ValueError):
            pass
    return -1  # -1 表示查询失败


def do_redeem() -> float:
    """执行一轮领取，返回本轮预估领取的 USDC 总额"""
    positions = fetch_positions()

    if not positions:
        log.info("✅ 暂无持仓数据")
        return 0.0

    resolved = get_resolved_conditions(positions)
    log.info(f"📋 持仓总数: {len(positions)}，已结算可领取: {len(resolved)}")

    if not resolved:
        log.info("✅ 暂无已结算头寸可领取")
        return 0.0

    total_value = sum(r["value"] for r in resolved)
    preview = ", ".join(r["slug"] for r in resolved[:5])
    if len(resolved) > 5:
        preview += f" ... 共 {len(resolved)} 个"
    log.info(f"🎯 准备领取 {len(resolved)} 个头寸，约 ${total_value:.2f} USDC: {preview}")

    success_count = 0
    fail_count = 0

    for r in resolved:
        ok = redeem_condition(r["condition_id"], r.get("neg_risk", False), r.get("size", 0))
        if ok:
            success_count += 1
        else:
            fail_count += 1
        # 每次 redeem 间隔 2 秒，避免 rate limit
        time.sleep(2)

    log.info(
        f"{'✅' if success_count > 0 else '⚠️'} 领取完成: "
        f"{success_count} 成功 / {fail_count} 失败 / {len(resolved)} 总计，"
        f"约 ${total_value:.2f} USDC"
    )

    # 查询当前余额
    balance = get_usdc_balance()
    if balance >= 0:
        log.info(f"💰 当前余额: ${balance:.2f} USDC")

    if success_count > 0 and total_value > 0:
        balance_line = f"💰 余额: ${balance:.2f} USDC\n" if balance >= 0 else ""
        send_telegram(
            f"💰 <b>Polymarket 自动结算领取</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"✅ 成功: {success_count}/{len(resolved)} 笔\n"
            f"💵 领取: ${total_value:.2f} USDC\n"
            f"{balance_line}"
            f"📅 时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )

    return total_value if success_count > 0 else 0.0

# ==============================================================================
# 主循环
# ==============================================================================

def main() -> None:
    log.info("=" * 55)
    log.info("🔄 Polymarket 自动领取守护进程启动 (CLI 模式)")
    log.info(f"   代理钱包    : {PROXY_WALLET}")
    log.info(f"   签名类型    : {SIGNATURE_TYPE}")
    log.info(f"   轮询间隔    : {REDEEM_INTERVAL}s ({REDEEM_INTERVAL // 60} 分钟)")
    log.info(f"   CLI命令     : polymarket ctf redeem")
    log.info("=" * 55)

    total_redeemed = 0.0
    cycle = 0

    while True:
        cycle += 1
        now = datetime.now(timezone.utc).strftime("%H:%M:%S")
        log.info(f"─── 第 {cycle} 轮 [{now} UTC] ───")

        try:
            amount = do_redeem()
            total_redeemed += amount
            if amount > 0:
                log.info(f"📊 累计已领取: ${total_redeemed:.2f} USDC")
        except Exception as e:
            log.error(f"❌ 本轮异常: {type(e).__name__}: {e}")

        log.info(f"⏳ 等待 {REDEEM_INTERVAL // 60} 分钟后进行下一轮...\n")
        time.sleep(REDEEM_INTERVAL)


if __name__ == "__main__":
    main()
