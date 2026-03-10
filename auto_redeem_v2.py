#!/usr/bin/env python3
"""
Polymarket 自动领取已结算收益脚本
- 浏览器钱包（OKX / MetaMask 等）+ Gnosis Safe 代理
- 使用 Relayer API gasless 链上 redeem，无需 MATIC
"""
import os
import sys
import time
import logging
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds
from py_builder_relayer_client.client import RelayClient
from py_builder_signing_sdk.config import BuilderConfig
from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds
from poly_web3.web3_service.proxy_service import ProxyWeb3Service
from poly_web3.web3_service.base import BaseWeb3Service
from poly_web3.schema import WalletType
from poly_web3.const import RELAYER_URL

# ==============================================================================
# 加载 .env
# ==============================================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

# ==============================================================================
# 配置（固定值直接写，敏感值从环境变量读）
# ==============================================================================

# ⚠️ 必须填 Gnosis Safe 代理地址（右上角那个），不是 OKX EOA 地址
PROXY_WALLET = "0x33aDb455c8133799Af4202e8c8e1bA6a30575B48"

CLOB_API_KEY = "019cd562-af00-7324-81b0-540510d8be6a"
CLOB_API_SECRET = "_giIPpY0fJpJuvEyMvdnYmJq4N_t4SV9veeGOEBxbGM="
CLOB_API_PASSPHRASE = "20116594757532b440694328e1b9191dc6fdf31645a2d8af0da93facfbe2545f"

# 私钥等敏感信息从 .env 读取
PRIVATE_KEY = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
BUILDER_KEY = os.environ.get("BUILDER_KEY", "").strip()
BUILDER_SECRET = os.environ.get("BUILDER_SECRET", "").strip()
BUILDER_PASSPHRASE = os.environ.get("BUILDER_PASSPHRASE", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "1609325006").strip()

REDEEM_INTERVAL = 1800  # 秒，30 分钟轮询一次
BATCH_SIZE = 5  # 每批 redeem 的 condition 数量

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
# 服务初始化
# ==============================================================================

def create_services() -> ProxyWeb3Service:
    """创建并返回 ProxyWeb3Service（浏览器钱包 / Gnosis Safe 模式）"""
    if not PRIVATE_KEY:
        log.error("❌ 缺少 POLYMARKET_PRIVATE_KEY，请在 .env 中配置")
        sys.exit(1)

    # signature_type=0 → POLY_PROXY，对应 CLI 代理钱包
    clob = ClobClient(
        host="https://clob.polymarket.com",
        key=PRIVATE_KEY,
        chain_id=137,
        signature_type=0,
        funder=PROXY_WALLET,
        creds=ApiCreds(
            api_key=CLOB_API_KEY,
            api_secret=CLOB_API_SECRET,
            api_passphrase=CLOB_API_PASSPHRASE,
        ),
    )

    has_builder = all([BUILDER_KEY, BUILDER_SECRET, BUILDER_PASSPHRASE])
    if has_builder:
        builder_config = BuilderConfig(
            local_builder_creds=BuilderApiKeyCreds(
                key=BUILDER_KEY,
                secret=BUILDER_SECRET,
                passphrase=BUILDER_PASSPHRASE,
            )
        )
        relay = RelayClient(
            relayer_url=RELAYER_URL,
            chain_id=137,
            private_key=PRIVATE_KEY,
            builder_config=builder_config,
        )
    else:
        log.warning("⚠️ 未配置 Builder API，使用默认 Relayer")
        relay = RelayClient(
            relayer_url=RELAYER_URL,
            chain_id=137,
            private_key=PRIVATE_KEY,
        )

    # ProxyWeb3Service 构造后设置 wallet_type
    svc = ProxyWeb3Service(clob, relay)
    svc.wallet_type = WalletType.PROXY
    return svc

# ==============================================================================
# 核心逻辑
# ==============================================================================

def fetch_resolved_positions(wallet_address: str) -> list:
    """拉取已结算（redeemable=True）的持仓，过滤掉进行中的市场"""
    try:
        all_positions = BaseWeb3Service.fetch_positions(wallet_address)
    except Exception as e:
        log.error(f"❌ 拉取持仓失败: {e}")
        return []

    if not all_positions:
        return []

    resolved = [
        p for p in all_positions
        if p.get("redeemable") is True
        or p.get("resolved") is True
        or p.get("game_status") == "resolved"
    ]
    log.info(f"📋 持仓总数: {len(all_positions)}，已结算可领取: {len(resolved)}")
    return resolved


def do_redeem(svc: ProxyWeb3Service) -> float:
    """执行一轮领取，返回本轮领取的 USDC 总额"""
    # 直接使用配置的代理地址，不调用私有方法
    positions = fetch_resolved_positions(PROXY_WALLET)

    if not positions:
        log.info("✅ 暂无已结算头寸可领取")
        return 0.0

    total_value = sum(float(p.get("current_value", 0)) for p in positions)
    n = len(positions)
    preview_slugs = ", ".join(p.get("slug", "?")[:40] for p in positions[:5])
    if n > 5:
        preview_slugs += f" ... 共 {n} 个"
    log.info(f"🎯 准备领取 {n} 个头寸，约 ${total_value:.2f} USDC: {preview_slugs}")

    try:
        results = svc.redeem_all(batch_size=BATCH_SIZE)
    except Exception as e:
        log.error(f"❌ redeem_all 执行异常: {type(e).__name__}: {e}")
        return 0.0

    success_states = {"STATE_MINED", "STATE_CONFIRMED"}
    success = sum(1 for r in results if r and r.get("state") in success_states)
    failed = len(results) - success
    log.info(
        f"{'✅' if success > 0 else '⚠️ '} 领取完成: "
        f"{success} 成功 / {failed} 失败 / {len(results)} 总计，"
        f"约 ${total_value:.2f} USDC"
    )

    if failed > 0:
        for i, r in enumerate(results):
            if r and r.get("state") not in success_states:
                log.warning(f" 第 {i+1} 笔失败: {r}")

    if success > 0 and total_value > 0:
        send_telegram(
            f"💰 <b>Polymarket 自动结算领取</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"✅ 成功: {success}/{len(results)} 笔\n"
            f"💵 领取: ${total_value:.2f} USDC\n"
            f"📅 时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )

    return total_value

# ==============================================================================
# 主循环
# ==============================================================================

def main() -> None:
    log.info("=" * 55)
    log.info("🔄 Polymarket 自动领取守护进程启动")
    log.info(f"   代理钱包 : {PROXY_WALLET}")
    log.info(f"   签名类型 : POLY_GNOSIS_SAFE (浏览器钱包)")
    log.info(f"   轮询间隔 : {REDEEM_INTERVAL}s ({REDEEM_INTERVAL // 60} 分钟)")
    log.info(f"   批次大小 : {BATCH_SIZE} conditions/batch")
    log.info("=" * 55)

    svc = create_services()
    total_redeemed = 0.0
    cycle = 0

    while True:
        cycle += 1
        now = datetime.now(timezone.utc).strftime("%H:%M:%S")
        log.info(f"─── 第 {cycle} 轮 [{now} UTC] ───")

        try:
            amount = do_redeem(svc)
            total_redeemed += amount
            if amount > 0:
                log.info(f"📊 累计已领取: ${total_redeemed:.2f} USDC")
        except Exception as e:
            log.error(f"❌ 本轮出现未捕获异常: {type(e).__name__}: {e}")
            log.info("🔁 尝试重建服务连接...")
            try:
                svc = create_services()
                log.info("✅ 服务连接已重建")
            except Exception as e2:
                log.error(f"重建失败: {e2}")

        log.info(f"⏳ 等待 {REDEEM_INTERVAL // 60} 分钟后进行下一轮...\n")
        time.sleep(REDEEM_INTERVAL)


if __name__ == "__main__":
    main()
