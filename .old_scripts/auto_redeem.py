#!/usr/bin/env python3
"""
Polymarket 自动结算领取脚本
每30分钟检查已结算头寸，自动 redeem 赢得的 token

基于 polymarket CLI，零额外依赖，零额外凭证
"""

import subprocess
import json
import time
import os
import sys
from datetime import datetime, timezone
import requests # 新增导入 requests 库

# ============================================================
# 配置
# ============================================================

WALLET_ADDRESS = "0x602E35d45A59182C8c3231C8Fbd0A3e886f4aDE8"
CHECK_INTERVAL = 1800  # 30分钟
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, "logs", "auto_redeem.log")
REDEEMED_FILE = os.path.join(SCRIPT_DIR, "logs", "redeemed_conditions.json")

# Telegram 通知
TELEGRAM_CHAT_ID = "1609325006"

def load_redeemed():
    """加载已领取记录"""
    try:
        if os.path.exists(REDEEMED_FILE):
            with open(REDEEMED_FILE, "r") as f:
                return json.load(f)
    except:
        pass
    return {}

def save_redeemed(redeemed):
    """保存已领取记录"""
    try:
        os.makedirs(os.path.dirname(REDEEMED_FILE), exist_ok=True)
        with open(REDEEMED_FILE, "w") as f:
            json.dump(redeemed, f, indent=2)
    except:
        pass

def log(msg):
    """打印并写入日志"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except:
        pass

def send_telegram(text):
    """推送 Telegram 通知"""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return
    try:
        import requests
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
        # Data API URL
        api_url = f"https://data-api.polymarket.com/positions?user={WALLET_ADDRESS}"
        
        # 发送HTTP GET请求
        response = requests.get(api_url, timeout=30)
        response.raise_for_status() # 如果状态码不是200，则抛出HTTPError

        positions = response.json()
        return positions
    except requests.exceptions.Timeout:
        log("❌ 查询持仓超时 (Data API)")
        return []
    except requests.exceptions.RequestException as e:
        log(f"❌ 查询持仓请求失败 (Data API): {e}")
        return []
    except json.JSONDecodeError:
        log(f"❌ 持仓数据解析失败 (Data API): {response.text[:200]}")
        return []
    except Exception as e:
        log(f"❌ 查询持仓异常 (Data API): {e}")
        return []

def redeem_position(condition_id):
    """执行 redeem"""
    try:
        result = subprocess.run(
            [
                "polymarket", "ctf", "redeem",
                "--condition", condition_id,
                "--signature-type", "gnosis-safe",
                "--index-sets", "1,2",
                "-o", "json"
            ],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0:
            return True, result.stdout.strip()
        else:
            return False, result.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, "超时"
    except Exception as e:
        return False, str(e)

def check_and_redeem():
    """主检查逻辑：查询持仓 → 过滤可领取 → 执行 redeem"""
    positions = get_positions()
    
    if not positions:
        log("📋 无持仓")
        return 0
    
    # 过滤可领取的持仓
    redeemable = [p for p in positions if p.get("redeemable") == True]
    
    # 统计当前持仓
    active = [p for p in positions if not p.get("redeemable")]
    if active:
        log(f"📊 当前活跃持仓: {len(active)}个")
        for p in active:
            slug = p.get("slug", "?")
            outcome = p.get("outcome", "?")
            size = p.get("size", "0")
            cur_price = p.get("cur_price", "0")
            pnl = p.get("percent_pnl", "0")
            log(f"   {slug} | {outcome} | {size}份 | ${cur_price} | PnL:{pnl}%")
    
    if not redeemable:
        log(f"📋 {len(positions)}个持仓，0个可领取")
        return 0
    
    log(f"🎯 发现 {len(redeemable)} 个可领取持仓！")
    
    # 加载已领取记录，去重
    redeemed_record = load_redeemed()
    new_redeemable = [p for p in redeemable if p.get("condition_id") not in redeemed_record]
    
    if not new_redeemable:
        log(f"📋 {len(redeemable)}个可领取，但全部已领取过，跳过")
        return 0
    
    log(f"🆕 其中 {len(new_redeemable)} 个是新的，开始领取")
    
    total_redeemed = 0.0
    success_count = 0
    success_positions = []
    
    for pos in new_redeemable:
        condition_id = pos.get("condition_id", "")
        slug = pos.get("slug", "unknown")
        outcome = pos.get("outcome", "?")
        size = float(pos.get("size", 0))
        current_value = float(pos.get("current_value", 0))
        
        log(f"💰 领取: {slug} | {outcome} | {size}份 | 价值${current_value:.2f}")
        
        ok, msg = redeem_position(condition_id)
        
        if ok:
            success_count += 1
            total_redeemed += current_value
            success_positions.append(pos)
            # 记录已领取
            redeemed_record[condition_id] = {
                "slug": slug,
                "outcome": outcome,
                "size": size,
                "value": current_value,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            save_redeemed(redeemed_record)
            log(f"   ✅ 领取成功！${current_value:.2f}")
        else:
            log(f"   ❌ 领取失败: {msg[:100]}")
    
    # 推送通知
    if success_count > 0:
        # 查领取后余额
        try:
            bal_result = subprocess.run(
                ["polymarket", "clob", "balance", "--asset-type", "collateral", "--signature-type", "gnosis-safe"],
                capture_output=True, text=True, timeout=15
            )
            balance_line = [l for l in bal_result.stdout.split("\n") if "Balance" in l]
            balance_str = balance_line[0].split("$")[1].strip() if balance_line else "?"
        except:
            balance_str = "?"
        
        notify_text = (
            f"💰 <b>自动结算领取成功</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"✅ 成功: {success_count}/{len(new_redeemable)}笔\n"
            f"💵 领取: ${total_redeemed:.2f} USDC\n"
            f"━━━━━━━━━━━━━━━━\n"
        )
        for pos in success_positions:
            slug = pos.get("slug", "?")
            outcome = pos.get("outcome", "?")
            size = pos.get("size", "0")
            avg_price = pos.get("avg_price", "0")
            pnl = pos.get("percent_pnl", "0")
            val = pos.get("current_value", "0")
            # 从slug提取币种
            coin = "BTC" if "btc" in slug else "ETH" if "eth" in slug else "?"
            notify_text += (
                f"  {coin} {outcome} | {size}份\n"
                f"  买入${avg_price} → 结算$1.00\n"
                f"  盈利: +{pnl}% (${val})\n"
            )
        notify_text += (
            f"━━━━━━━━━━━━━━━━\n"
            f"💰 当前余额: ${balance_str}\n"
        )
        
        send_telegram(notify_text)
    
    return success_count

def main():
    log("=" * 50)
    log("🔄 自动结算守护进程启动")
    log(f"   钱包: {WALLET_ADDRESS}")
    log(f"   间隔: {CHECK_INTERVAL}s ({CHECK_INTERVAL // 60}分钟)")
    log("=" * 50)
    
    cycle = 0
    total_redeemed_count = 0
    
    while True:
        cycle += 1
        now = datetime.now().strftime("%H:%M:%S")
        log(f"--- 第{cycle}轮 ({now}) ---")
        
        try:
            count = check_and_redeem()
            total_redeemed_count += count
            if total_redeemed_count > 0:
                log(f"📊 累计已领取: {total_redeemed_count}笔")
        except Exception as e:
            log(f"❌ 本轮异常: {type(e).__name__}: {e}")
        
        log(f"⏳ 等待 {CHECK_INTERVAL // 60} 分钟...")
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
