#!/usr/bin/env python3
"""
Polymarket 自动领取已结算收益脚本 v3.2
- CLI 查持仓 + web3 EOA 直接链上结算（不走 Gnosis Safe）
- EOA 直接调用 CTF.redeemPositions / NegRiskAdapter.redeemPositions
- 支持 normal / neg-risk 自动切换重试
- 链上 payoutDenominator 验证已结算 + balanceOf 检查余额
- 持久化已 redeem 记录 + 日志按天写入文件 + Telegram 通知
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
from eth_abi import encode as abi_encode
from web3 import Web3

# ==============================================================================
# 配置
# ==============================================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

EOA_WALLET = os.environ.get("EOA_WALLET", "").strip()
PROXY_WALLET = os.environ.get("PROXY_WALLET", "").strip()
PRIVATE_KEY = os.environ.get("PRIVATE_KEY", "").strip()
if not PROXY_WALLET or not PRIVATE_KEY:
    raise RuntimeError("PROXY_WALLET and PRIVATE_KEY must be set in .env")
SIGNATURE_TYPE = os.environ.get("SIGNATURE_TYPE", "eoa").strip()

RPC_URL = os.environ.get("POLYGON_RPC_URL", "https://polygon-rpc.com").strip()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

REDEEM_INTERVAL = int(os.environ.get("REDEEM_INTERVAL", 600))  # 默认10分钟

# 已 redeem 记录文件
REDEEMED_FILE = os.path.join(SCRIPT_DIR, "logs", "redeemed_conditions.json")

# ==============================================================================
# 合约地址 (Polygon mainnet)
# ==============================================================================

CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"       # Conditional Tokens Framework
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"  # NegRiskAdapter
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"      # USDC (PoS) on Polygon

# ==============================================================================
# 最小 ABI
# ==============================================================================

CTF_ABI = [
    {
        "constant": True,
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "id", "type": "uint256"},
        ],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    }
]

USDC_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    }
]

# ==============================================================================
# 日志：终端 + 按天日志文件
# ==============================================================================

LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)


class _TeeWriter:
    """同时写入终端和日志文件，日志按天自动轮转"""
    def __init__(self, stream):
        self._stream = stream
        self._date = None
        self._file = None

    def _ensure_file(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if self._date != today:
            if self._file:
                self._file.close()
            self._date = today
            path = os.path.join(LOG_DIR, f"redeem_{today}.log")
            self._file = open(path, "a", encoding="utf-8")
        return self._file

    def write(self, msg):
        self._stream.write(msg)
        try:
            f = self._ensure_file()
            f.write(msg)
            f.flush()
        except Exception:
            pass

    def flush(self):
        self._stream.flush()
        if self._file:
            try:
                self._file.flush()
            except Exception:
                pass


sys.stdout = _TeeWriter(sys.__stdout__)
sys.stderr = _TeeWriter(sys.__stderr__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [REDEEM] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("auto_redeem")

# ==============================================================================
# 已 redeem 记录持久化
# ==============================================================================

def load_redeemed() -> dict:
    try:
        with open(REDEEMED_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_redeemed(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(REDEEMED_FILE), exist_ok=True)
        with open(REDEEMED_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.warning(f"保存 redeem 记录失败: {e}")


def mark_redeemed(redeemed: dict, condition_id: str, slug: str, value: float,
                  size: float, tx_hash: str = "") -> None:
    redeemed[condition_id] = {
        "slug": slug,
        "value": value,
        "size": size,
        "tx_hash": tx_hash,
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_redeemed(redeemed)

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
# Web3 初始化
# ==============================================================================

def init_web3():
    """初始化 web3 连接和钱包"""
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    if not w3.is_connected():
        raise ConnectionError(f"无法连接 RPC: {RPC_URL}")

    wallet = w3.eth.account.from_key(PRIVATE_KEY)

    ctf_contract = w3.eth.contract(
        address=Web3.to_checksum_address(CTF_ADDRESS), abi=CTF_ABI
    )
    usdc_contract = w3.eth.contract(
        address=Web3.to_checksum_address(USDC_ADDRESS), abi=USDC_ABI
    )

    return w3, wallet, ctf_contract, usdc_contract

# ==============================================================================
# CLI 持仓获取
# ==============================================================================

def run_cli(args: list, timeout: int = 30) -> tuple:
    """执行 polymarket CLI 命令，返回 (success, stdout, stderr)"""
    cmd = ["polymarket"] + args
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.returncode == 0, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return False, "", "timeout"
    except Exception as e:
        return False, "", str(e)


def _parse_cli_json(stdout: str) -> list:
    """解析 CLI JSON 输出为列表"""
    if not stdout.strip():
        return []
    try:
        data = json.loads(stdout)
        if isinstance(data, dict):
            return data.get("positions", data.get("data", []))
        elif isinstance(data, list):
            return data
        return []
    except json.JSONDecodeError:
        return []


def fetch_positions_cli() -> list:
    """通过 polymarket CLI 获取钱包持仓"""
    all_positions = []

    for addr, label in [(PROXY_WALLET, "proxy"), (EOA_WALLET, "eoa")]:
        if not addr:
            continue
        success, stdout, stderr = run_cli([
            "data", "positions", addr,
            "--signature-type", SIGNATURE_TYPE,
            "--limit", "100",
            "-o", "json",
        ], timeout=20)

        if success:
            positions = _parse_cli_json(stdout)
            if positions:
                log.info(f"  {label} 地址: {len(positions)} 个持仓")
                all_positions.extend(positions)
        else:
            err = stderr[:100] if stderr else "unknown"
            log.warning(f"  {label} 地址查询失败: {err}")

    # 也查 closed-positions
    if PROXY_WALLET:
        success, stdout, _ = run_cli([
            "data", "closed-positions", PROXY_WALLET,
            "--signature-type", SIGNATURE_TYPE,
            "--limit", "50",
            "-o", "json",
        ], timeout=20)

        if success:
            closed = _parse_cli_json(stdout)
            if closed:
                log.info(f"  已关闭持仓: {len(closed)} 个")
                all_positions.extend(closed)

    return all_positions


# ==============================================================================
# 链上验证
# ==============================================================================

def check_resolved_onchain(w3: Web3, cond_id: str) -> bool:
    """链上检查 condition 是否已结算: payoutDenominator > 0"""
    selector = w3.keccak(text="payoutDenominator(bytes32)")[:4]
    call_data = selector + abi_encode(
        ["bytes32"], [bytes.fromhex(cond_id.replace("0x", ""))]
    )
    result = w3.eth.call(
        {"to": Web3.to_checksum_address(CTF_ADDRESS), "data": call_data}
    )
    denominator = int(result.hex(), 16)
    return denominator > 0


def find_redeemable(w3: Web3, wallet, ctf_contract) -> list[dict]:
    """CLI 获取持仓 -> API 字段预筛 -> 链上验证已结算 + 余额 > 0"""
    eoa_cs = Web3.to_checksum_address(wallet.address)
    # 检查余额的地址列表：EOA 优先，proxy 备选
    check_addrs = [eoa_cs]
    if PROXY_WALLET and PROXY_WALLET.lower() != wallet.address.lower():
        check_addrs.append(Web3.to_checksum_address(PROXY_WALLET))

    # 1. CLI 获取持仓
    positions = fetch_positions_cli()
    if not positions:
        return []

    # 2. API 字段预筛已结算的（减少链上调用）
    redeemable = []
    seen_conditions = set()

    for p in positions:
        cond_id = p.get("condition_id") or p.get("conditionId", "")
        token_id = p.get("asset") or p.get("token_id", "")
        title = p.get("title", p.get("slug", p.get("market_slug", cond_id[:16])))
        neg_risk = p.get("neg_risk", p.get("negRisk", False))
        cur_value = float(p.get("current_value", p.get("value", 0)) or 0)
        size = float(p.get("size", 0) or 0)

        if not cond_id or cond_id in seen_conditions:
            continue
        seen_conditions.add(cond_id)

        # API 字段预筛：只对可能已结算的做链上验证
        is_likely_resolved = (
            p.get("resolved") is True
            or p.get("redeemable") is True
            or p.get("game_status") == "resolved"
            or p.get("status") == "resolved"
            or p.get("outcome") not in (None, "", "pending")
        )
        if not is_likely_resolved:
            continue

        # 3. 链上确认已结算
        try:
            if not check_resolved_onchain(w3, cond_id):
                continue
        except Exception as e:
            log.debug(f"  链上检查失败: {cond_id[:18]}... | {e}")
            continue

        # 4. 链上检查 token 余额（先查 EOA，再查 proxy）
        bal = 0
        holder = eoa_cs
        if token_id:
            for addr in check_addrs:
                try:
                    b = ctf_contract.functions.balanceOf(addr, int(token_id)).call()
                    if b > 0:
                        bal = b
                        holder = addr
                        break
                except Exception:
                    continue
            if bal == 0:
                continue
        else:
            bal = int(size * 1e6) if size > 0 else 0

        redeemable.append({
            "condition_id": cond_id,
            "token_id": token_id,
            "balance": bal,
            "holder": holder,
            "slug": (title or "unknown")[:50],
            "value": cur_value,
            "neg_risk": neg_risk,
            "size": size,
        })
        log.info(f"  可领取: {(title or '')[:40]} (balance: {bal}, holder: {holder[:10]}...)")

    return redeemable

# ==============================================================================
# EOA 直接链上 Redeem
# ==============================================================================

def _send_tx(w3: Web3, wallet, target: str, call_data: bytes) -> str | None:
    """
    EOA 直接发送交易到目标合约

    Returns: tx_hash hex string on success, None on failure
    """
    target_cs = Web3.to_checksum_address(target)

    gas_estimate = w3.eth.estimate_gas(
        {"from": wallet.address, "to": target_cs, "data": call_data}
    )

    tx = w3.eth.account.sign_transaction(
        {
            "to": target_cs,
            "data": call_data,
            "gas": gas_estimate + 50_000,
            "gasPrice": w3.eth.gas_price,
            "nonce": w3.eth.get_transaction_count(wallet.address),
            "chainId": 137,
        },
        PRIVATE_KEY,
    )

    tx_hash = w3.eth.send_raw_transaction(tx.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

    if receipt.status == 1:
        return tx_hash.hex()
    return None


def redeem_normal(w3: Web3, wallet, cond_id: str) -> str | None:
    """
    普通市场 redeem: CTF.redeemPositions(collateral, parentCollectionId, conditionId, indexSets)
    EOA 直接调用 CTF 合约
    """
    usdc_cs = Web3.to_checksum_address(USDC_ADDRESS)

    selector = w3.keccak(
        text="redeemPositions(address,bytes32,bytes32,uint256[])"
    )[:4]
    call_data = selector + abi_encode(
        ["address", "bytes32", "bytes32", "uint256[]"],
        [
            usdc_cs,
            b"\x00" * 32,  # parentCollectionId (root)
            bytes.fromhex(cond_id.replace("0x", "")),
            [1, 2],        # indexSets: YES + NO
        ],
    )

    return _send_tx(w3, wallet, CTF_ADDRESS, call_data)


def redeem_neg_risk(w3: Web3, wallet, cond_id: str, amounts: list[int]) -> str | None:
    """
    neg-risk 市场 redeem: NegRiskAdapter.redeemPositions(conditionId, amounts)
    EOA 直接调用 NegRiskAdapter
    """
    selector = w3.keccak(
        text="redeemPositions(bytes32,uint256[])"
    )[:4]
    call_data = selector + abi_encode(
        ["bytes32", "uint256[]"],
        [
            bytes.fromhex(cond_id.replace("0x", "")),
            amounts,
        ],
    )

    return _send_tx(w3, wallet, NEG_RISK_ADAPTER, call_data)


def redeem_position(w3: Web3, wallet, cond_id: str,
                    neg_risk: bool = False, balance: int = 0) -> str | None:
    """
    执行单个 condition 的链上 redeem（EOA 直接调用）
    策略：先按 neg_risk 标志尝试，失败则切换另一种方式重试

    Returns: tx_hash on success, None on failure
    """
    if not cond_id.startswith("0x"):
        cond_id = "0x" + cond_id

    amt = balance if balance > 0 else 1

    # 第一次尝试
    label1 = "neg-risk" if neg_risk else "normal"
    try:
        if neg_risk:
            tx = redeem_neg_risk(w3, wallet, cond_id, [amt, amt])
        else:
            tx = redeem_normal(w3, wallet, cond_id)
        if tx:
            log.info(f"  ✅ redeem 成功({label1}): {cond_id[:18]}... | tx: {tx}")
            return tx
    except Exception as e:
        err_msg = str(e).lower()
        if "execution reverted" in err_msg:
            log.info(f"  🔄 {label1}方式 revert，切换重试: {cond_id[:18]}...")
        else:
            log.warning(f"  ⚠️ {label1}失败: {cond_id[:18]}... | {str(e)[:120]}")

    # 第二次：切换方式重试
    label2 = "normal" if neg_risk else "neg-risk"
    try:
        if neg_risk:
            tx = redeem_normal(w3, wallet, cond_id)
        else:
            tx = redeem_neg_risk(w3, wallet, cond_id, [amt, amt])
        if tx:
            log.info(f"  ✅ redeem 成功({label2}): {cond_id[:18]}... | tx: {tx}")
            return tx
    except Exception as e:
        log.warning(f"  ❌ {label2}也失败: {cond_id[:18]}... | {str(e)[:120]}")

    return None

# ==============================================================================
# 核心逻辑
# ==============================================================================

def get_usdc_balance(wallet, usdc_contract) -> float:
    """查询 EOA 钱包 USDC 余额"""
    try:
        bal = usdc_contract.functions.balanceOf(
            Web3.to_checksum_address(wallet.address)
        ).call()
        return bal / 1e6
    except Exception:
        return -1


def do_redeem(w3: Web3, wallet, ctf_contract, usdc_contract) -> float:
    """执行一轮领取，返回实际 USDC 收益"""

    # 1. 获取可 redeem 的持仓
    redeemable = find_redeemable(w3, wallet, ctf_contract)

    if not redeemable:
        log.info("✅ 暂无可领取的已结算持仓")
        return 0.0

    # 2. 过滤已 redeem 的
    redeemed = load_redeemed()
    pending = [r for r in redeemable if r["condition_id"] not in redeemed]
    skipped = len(redeemable) - len(pending)

    log.info(
        f"📋 链上已结算: {len(redeemable)}，"
        f"待领取: {len(pending)}" + (f"，已跳过: {skipped}" if skipped else "")
    )

    if not pending:
        log.info("✅ 暂无新的已结算头寸可领取")
        return 0.0

    total_value = sum(r["value"] for r in pending)
    preview = ", ".join(r["slug"] for r in pending[:5])
    if len(pending) > 5:
        preview += f" ... 共 {len(pending)} 个"
    log.info(f"🎯 准备领取 {len(pending)} 个头寸，约 ${total_value:.2f} USDC: {preview}")

    # 3. USDC 余额（领取前）
    usdc_before = get_usdc_balance(wallet, usdc_contract)
    if usdc_before >= 0:
        log.info(f"💰 领取前余额: ${usdc_before:.2f} USDC")

    # 4. 逐个 redeem
    success_count = 0
    fail_count = 0

    for r in pending:
        cid = r["condition_id"]
        val = r.get("value", 0)
        bal = r.get("balance", 0)

        tx_hash = redeem_position(
            w3, wallet, cid,
            neg_risk=r.get("neg_risk", False),
            balance=bal,
        )

        if tx_hash:
            success_count += 1
            mark_redeemed(redeemed, cid, r["slug"], val, r.get("size", 0), tx_hash)
        else:
            fail_count += 1

        # 间隔 3 秒，等区块确认 + 避免 nonce 冲突
        time.sleep(3)

    # 5. USDC 余额（领取后）
    usdc_after = get_usdc_balance(wallet, usdc_contract)
    gained = (usdc_after - usdc_before) if usdc_before >= 0 and usdc_after >= 0 else 0

    log.info(
        f"{'✅' if success_count > 0 else '⚠️'} 领取完成: "
        f"{success_count} 成功 / {fail_count} 失败 / {len(pending)} 总计"
    )
    if usdc_after >= 0:
        log.info(f"💵 USDC 变化: +${gained:.2f}  |  余额: ${usdc_after:.2f}")

    # 6. Telegram 通知
    if success_count > 0 and gained > 0:
        balance_line = f"💰 余额: ${usdc_after:.2f} USDC\n" if usdc_after >= 0 else ""
        send_telegram(
            f"💰 <b>Polymarket 链上自动结算</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"✅ 成功: {success_count}/{len(pending)} 笔\n"
            f"💵 收益: +${gained:.2f} USDC\n"
            f"{balance_line}"
            f"📅 时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )

    return gained

# ==============================================================================
# 主循环
# ==============================================================================

def main() -> None:
    log.info("=" * 55)
    log.info("🔄 Polymarket 链上自动结算 v3.2 启动 (CLI查仓+EOA直接结算)")
    log.info(f"   RPC         : {RPC_URL}")
    log.info(f"   轮询间隔    : {REDEEM_INTERVAL}s ({REDEEM_INTERVAL // 60} 分钟)")
    log.info(f"   已领取记录  : {REDEEMED_FILE}")
    log.info("=" * 55)

    w3, wallet, ctf_contract, usdc_contract = init_web3()
    log.info(f"   EOA 钱包    : {wallet.address}")
    if PROXY_WALLET:
        log.info(f"   Proxy 钱包  : {PROXY_WALLET}")
    log.info(f"   RPC 已连接   : chainId={w3.eth.chain_id}")

    total_redeemed = 0.0
    cycle = 0

    while True:
        cycle += 1
        now = datetime.now(timezone.utc).strftime("%H:%M:%S")
        log.info(f"─── 第 {cycle} 轮 [{now} UTC] ───")

        try:
            amount = do_redeem(w3, wallet, ctf_contract, usdc_contract)
            total_redeemed += amount
            if amount > 0:
                log.info(f"📊 累计已领取: ${total_redeemed:.2f} USDC")
        except ConnectionError as e:
            log.error(f"❌ RPC 连接失败: {e}")
            try:
                w3, wallet, ctf_contract, usdc_contract = init_web3()
                log.info("  🔄 RPC 重连成功")
            except Exception:
                pass
        except Exception as e:
            log.error(f"❌ 本轮异常: {type(e).__name__}: {e}")

        log.info(f"⏳ 等待 {REDEEM_INTERVAL // 60} 分钟后进行下一轮...\n")
        time.sleep(REDEEM_INTERVAL)


if __name__ == "__main__":
    main()
