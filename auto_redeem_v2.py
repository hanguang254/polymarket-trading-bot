#!/usr/bin/env python3
"""
Polymarket 自动结算领取脚本 v2
使用官方 Python SDK + Relayer API 实现 gasless redeem
"""

import os
import sys
import time
import json
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv

import requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds
from py_builder_relayer_client.client import RelayClient
from py_builder_signing_sdk.config import BuilderConfig
from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds
from poly_web3.web3_service.proxy_service import ProxyWeb3Service
from poly_web3.web3_service.base import BaseWeb3Service
from poly_web3.schema import WalletType
from poly_web3.const import RELAYER_URL

# 加载 .env 文件
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

# ==============================================================================
# 配置
# ==============================================================================

PROXY_WALLET = "0x602E35d45A59182C8c3231C8Fbd0A3e886f4aDE8"
CLOB_API_KEY = "019cd562-af00-7324-81b0-540510d8be6a"
CLOB_API_SECRET = "_giIPpY0fJpJuvEyMvdnYmJq4N_t4SV9veeGOEBxbGM="
CLOB_API_PASSPHRASE = "20116594757532b440694328e1b9191dc6fdf31645a2d8af0da93facfbe2545f"

PRIVATE_KEY = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
BUILDER_KEY = os.environ.get("BUILDER_KEY", "")
BUILDER_SECRET = os.environ.get("BUILDER_SECRET", "")
BUILDER_PASSPHRASE = os.environ.get("BUILDER_PASSPHRASE", "")

CHECK_INTERVAL = 1800
BATCH_SIZE = 5

LOG_FILE = os.path.join(SCRIPT_DIR, "logs", "auto_redeem_v2.log")
REDEEMED_FILE = os.path.join(SCRIPT_DIR, "logs", "redeemed_conditions_v2.json")
TELEGRAM_CHAT_ID = "1609325006"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [REDEEM] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("auto_redeem_v2")

# ==============================================================================
# 工具函数
# ==============================================================================

def load_redeemed():
    try:
        if os.path.exists(REDEEMED_FILE):
            with open(REDEEMED_FILE, "r") as f:
                return json.load(f)
    except:
        pass
    return {}

def save_redeemed(redeemed):
    try:
        os.makedirs(os.path.dirname(REDEEMED_FILE), exist_ok=True)
        with open(REDEEMED_FILE, "w") as f:
            json.dump(redeemed, f, indent=2)
    except:
        pass

def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML"
        }, timeout=10)
    except:
        pass

def get_positions():
    """通过Data API查询所有持仓"""
    try:
        api_url = f"https://data-api.polymarket.com/positions?user={PROXY_WALLET}"
        response = requests.get(api_url, timeout=30)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        log.error(f"❌ 查询持仓失败: {e}")
        return []

def create_services():
    """创建 SDK 服务"""
    if not PRIVATE_KEY:
        log.error("❌ 缺少 POLYMARKET_PRIVATE_KEY 环境变量")
        sys.exit(1)
    
    clob = ClobClient(
        "clob.polymarket.com",
        key=PRIVATE_KEY,
        chain_id=137,
        signature_type=2,  # POLY_PROXY
        funder=PROXY_WALLET,
        creds=ApiCreds(
            api_key=CLOB_API_KEY,
            api_secret=CLOB_API_SECRET,
            api_passphrase=CLOB_API_PASSPHRASE,
        ),
    )
    
    if BUILDER_KEY and BUILDER_SECRET and BUILDER_PASSPHRASE:
        bc = BuilderConfig(local_builder_creds=BuilderApiKeyCreds(
            key=BUILDER_KEY,
            secret=BUILDER_SECRET,
            passphrase=BUILDER_PASSPHRASE
        ))
        relay = RelayClient(
            relayer_url=RELAYER_URL,
            chain_id=137,
            private_key=PRIVATE_KEY,
            builder_config=bc,
        )
    else:
        log.warning("⚠️  未配置 Builder API，将使用默认 Relayer")
        relay = RelayClient(
            relayer_url=RELAYER_URL,
            chain_id=137,
            private_key=PRIVATE_KEY,
        )
    
    svc = ProxyWeb3Service(clob, relay)
    svc.wallet_type = WalletType.PROXY
    return svc

def do_redeem(svc: ProxyWeb3Service):
    """执行一次领取，返回领取的总金额"""
    positions = get_positions()
    
    if not positions:
        log.info("📋 无持仓")
        return 0.0
    
    # 过滤可领取的持仓
    redeemable = [p for p in positions if p.get("redeemable") == True]
    active = [p for p in positions if not p.get("redeemable")]
    
    if active:
        log.info(f"📊 当前活跃持仓: {len(active)}个")
    
    if not redeemable:
        log.info(f"📋 {len(positions)}个持仓，0个可领取")
        return 0.0
    
    # 去重已领取
    redeemed_record = load_redeemed()
    new_redeemable = [p for p in redeemable if p.get("condition_id") not in redeemed_record]
    
    if not new_redeemable:
        log.info(f"📋 {len(redeemable)}个可领取，但全部已领取过")
        return 0.0
    
    total_size = sum(float(p.get("size", 0)) for p in new_redeemable)
    log.info(f"🎯 发现 {len(new_redeemable)} 个新的可领取头寸，约 ${total_size:.2f} USDC")
    
    try:
        results = svc.redeem_all(batch_size=BATCH_SIZE)
        success = sum(1 for r in results if r and r.get("state") in ("STATE_MINED", "STATE_CONFIRMED"))
        
        # 记录已领取
        for pos in new_redeemable:
            condition_id = pos.get("condition_id", "")
            redeemed_record[condition_id] = {
                "slug": pos.get("slug", "?"),
                "outcome": pos.get("outcome", "?"),
                "size": pos.get("size", 0),
                "value": pos.get("current_value", 0),
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
        save_redeemed(redeemed_record)
        
        log.info(f"✅ 领取完成: {success}/{len(results)} 笔交易成功，约 ${total_size:.2f} USDC")
        
        # 发送通知
        if success > 0:
            notify_text = (
                f"💰 <b>自动结算领取成功</b>\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"✅ 成功: {success}/{len(new_redeemable)}笔\n"
                f"💵 领取: ${total_size:.2f} USDC\n"
            )
            send_telegram(notify_text)
        
        return total_size
    except Exception as e:
        log.error(f"❌ 领取失败: {type(e).__name__}: {e}")
        return 0.0

def main():
    log.info("=" * 50)
    log.info("🔄 自动结算守护进程启动 (SDK版)")
    log.info(f"   钱包: {PROXY_WALLET}")
    log.info(f"   间隔: {CHECK_INTERVAL}s ({CHECK_INTERVAL // 60}分钟)")
    log.info(f"   批量: {BATCH_SIZE} conditions/batch")
    log.info("=" * 50)
    
    svc = create_services()
    total_redeemed = 0.0
    cycle = 0
    
    while True:
        cycle += 1
        now = datetime.now(timezone.utc).strftime("%H:%M:%S")
        log.info(f"--- 第{cycle}轮 ({now} UTC) ---")
        
        try:
            amount = do_redeem(svc)
            total_redeemed += amount
            if amount > 0:
                log.info(f"📊 累计已领取: ${total_redeemed:.2f}")
        except Exception as e:
            log.error(f"❌ 本轮异常: {type(e).__name__}: {e}")
            try:
                svc = create_services()
                log.info("已重建服务连接")
            except Exception as e2:
                log.error(f"重建失败: {e2}")
        
        log.info(f"⏳ 等待 {CHECK_INTERVAL // 60} 分钟...")
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
