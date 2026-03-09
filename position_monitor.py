#!/usr/bin/env python3
"""
持仓止盈监控 - 5分钟市场专用
在市场关闭前的80-100秒窗口内监控价格，达到+15%即止盈
"""
import json
import os
import subprocess
import time
from datetime import datetime, timezone
import requests

POSITIONS_FILE = "/root/.openclaw/workspace/polymarket-arb-bot/logs/positions.jsonl"
PROFIT_THRESHOLD = 0.15  # 15% 止盈

# Telegram 通知配置
TELEGRAM_BOT_TOKEN = "8315083265:AAGM_rUxfOzmnTDYd6v2n6n-kEArK37tKKk"
TELEGRAM_CHAT_ID = "1609325006"

def send_telegram(text):
    """发送 Telegram 通知"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
    except:
        pass

def get_open_positions():
    """获取未关闭的持仓"""
    positions = []
    if os.path.exists(POSITIONS_FILE):
        with open(POSITIONS_FILE, "r") as f:
            for line in f:
                if line.strip():
                    try:
                        pos = json.loads(line)
                        if isinstance(pos, dict) and not pos.get("closed", False):
                            positions.append(pos)
                    except:
                        pass
    return positions

def get_market_price(token_id):
    """获取当前市场中间价"""
    try:
        resp = requests.get(
            f"https://clob.polymarket.com/midpoint?token_id={token_id}",
            timeout=5
        )
        if resp.status_code == 200:
            data = resp.json()
            mid = data.get("mid")
            if mid:
                return float(mid)
    except:
        pass
    return None

def get_best_bid(token_id):
    """获取最佳买价（用于卖出）- 从订单簿获取"""
    try:
        resp = requests.get(
            f"https://clob.polymarket.com/book?token_id={token_id}",
            timeout=5
        )
        if resp.status_code == 200:
            data = resp.json()
            bids = data.get('bids', [])
            if bids and len(bids) > 0:
                # 最佳买价（买方愿意支付的最高价）
                best_bid = float(bids[0]['price'])
                # 使用略低于最佳买价的价格（99%），提高成交率
                return best_bid * 0.99
    except:
        pass
    return None

def analyze_liquidity(token_id, target_size):
    """分析订单簿流动性"""
    try:
        resp = requests.get(f"https://clob.polymarket.com/book?token_id={token_id}", timeout=3)
        if resp.status_code != 200:
            return None
        
        bids = resp.json().get('bids', [])
        if not bids:
            return None
        
        # 计算累计流动性
        cumulative = []
        total = 0
        for bid in bids:
            price = float(bid['price'])
            size = float(bid['size'])
            total += size
            cumulative.append({'price': price, 'size': size, 'cumulative': total})
        
        best_bid = float(bids[0]['price'])
        best_bid_size = float(bids[0]['size'])
        
        # 计算流动性评分
        bid_score = min(best_bid / 0.5, 1.0) * 4
        liquidity_score = min(total / target_size / 10, 1.0) * 3
        depth_score = min(len(bids) / 10, 1.0) * 2
        coverage = 1.0 if total >= target_size else total / target_size
        score = bid_score + liquidity_score + depth_score + coverage
        
        return {
            'best_bid': best_bid,
            'best_bid_size': best_bid_size,
            'total_liquidity': total,
            'depth': len(bids),
            'cumulative': cumulative,
            'score': round(score, 2)
        }
    except:
        return None

def find_optimal_price(liquidity, target_size):
    """找到满足流动性的最优价格"""
    if not liquidity:
        return None
    
    cumulative = liquidity['cumulative']
    best_bid = liquidity['best_bid']
    
    # 找累计流动性>=target_size的最高价
    for level in cumulative:
        if level['cumulative'] >= target_size and level['price'] >= best_bid * 0.95:
            return level['price']
    
    return best_bid * 0.95

def parse_order_output(output):
    """解析CLI订单输出，提取成交状态和实际价格"""
    info = {"status": None, "matched": False, "making": 0, "taking": 0}
    for line in output.strip().split("\n"):
        line = line.strip()
        if line.startswith("Status:"):
            info["status"] = line.split(":", 1)[1].strip()
            info["matched"] = info["status"] == "MATCHED"
        elif line.startswith("Making:"):
            try: info["making"] = float(line.split(":", 1)[1].strip())
            except: pass
        elif line.startswith("Taking:"):
            try: info["taking"] = float(line.split(":", 1)[1].strip())
            except: pass
    return info

def sell_position(token_id, size, price, max_retries=3):
    """卖出持仓（带重试+成交确认）
    返回: (success, output, actual_price)
      - success: True仅当Status=MATCHED
      - actual_price: 实际成交价（Taking/Size），None表示未知
    """
    price = round(price, 2)
    
    for attempt in range(max_retries):
        cmd = [
            "polymarket", "clob", "create-order",
            "--signature-type", "gnosis-safe",
            "--token", token_id,
            "--side", "sell",
            "--price", str(price),
            "--size", str(size)
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                info = parse_order_output(result.stdout)
                if info["matched"]:
                    actual_price = round(info["taking"] / size, 4) if size > 0 and info["taking"] > 0 else price
                    print(f"    📊 成交确认: Status={info['status']} | Taking=${info['taking']:.4f} | 实际价=${actual_price:.4f}")
                    return True, result.stdout, actual_price
                else:
                    # LIVE=挂单未成交，不算成功
                    print(f"    ⏳ 挂单未成交: Status={info['status']} | 尝试{attempt+1}/{max_retries}")
                    if attempt < max_retries - 1:
                        time.sleep(2)
                    continue
            if attempt < max_retries - 1:
                time.sleep(2)
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2)
            else:
                return False, str(e), None
    
    return False, result.stderr if 'result' in locals() else "All retries failed", None

def close_position(position, exit_price):
    """标记持仓为已关闭"""
    position["closed"] = True
    position["exit_price"] = exit_price
    position["exit_time"] = datetime.now(timezone.utc).isoformat()
    
    all_positions = []
    if os.path.exists(POSITIONS_FILE):
        with open(POSITIONS_FILE, "r") as f:
            for line in f:
                if line.strip():
                    try:
                        pos = json.loads(line)
                        if isinstance(pos, dict):
                            if pos.get("token_id") == position["token_id"] and pos.get("entry_time") == position["entry_time"]:
                                all_positions.append(position)
                            else:
                                all_positions.append(pos)
                    except:
                        pass
    
    with open(POSITIONS_FILE, "w") as f:
        for pos in all_positions:
            f.write(json.dumps(pos) + "\n")

def get_market_end_time(slug):
    """从slug提取市场结束时间（Unix时间戳）"""
    try:
        # slug格式: btc-updown-5m-1772945400
        # timestamp是市场开始时间，5分钟市场需要+300秒
        parts = slug.split('-')
        if len(parts) >= 4:
            timestamp = int(parts[-1])
            return timestamp + 300  # 5分钟市场
    except:
        pass
    return None

def check_market_closed(slug):
    """检查市场是否已关闭"""
    try:
        resp = requests.get(
            f"https://gamma-api.polymarket.com/events?slug={slug}",
            timeout=5
        )
        if resp.status_code == 200:
            events = resp.json()
            if isinstance(events, list) and len(events) > 0:
                event = events[0]
                if isinstance(event, dict):
                    markets = event.get("markets")
                    if isinstance(markets, list) and len(markets) > 0:
                        market = markets[0]
                        if isinstance(market, dict):
                            return market.get("closed", False)
    except:
        pass
    return False

def get_current_crypto_price(coin):
    """获取BTC/ETH当前实时价格"""
    try:
        symbol = "BTCUSDT" if coin == "BTC" else "ETHUSDT"
        resp = requests.get(
            f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}",
            timeout=5
        )
        if resp.status_code == 200:
            data = resp.json()
            return float(data["price"])
    except:
        pass
    return None

def get_ptb_from_slug(slug):
    """从slug获取PTB价格"""
    try:
        result = subprocess.run(
            ["python3", "/root/.openclaw/workspace/polymarket-arb-bot/playwright_ptb.py", slug],
            capture_output=True,
            text=True,
            timeout=15
        )
        if result.returncode == 0:
            return float(result.stdout.strip())
    except:
        pass
    return None

def get_atr_from_binance(coin, period=14):
    """获取ATR（平均真实波幅）"""
    try:
        symbol = "BTCUSDT" if coin == "BTC" else "ETHUSDT"
        resp = requests.get(
            f"https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": "1m", "limit": period + 1},
            timeout=5
        )
        if resp.status_code == 200:
            klines = resp.json()
            trs = []
            for i in range(1, len(klines)):
                high = float(klines[i][2])
                low = float(klines[i][3])
                prev_close = float(klines[i-1][4])
                tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
                trs.append(tr)
            if trs:
                return sum(trs) / len(trs)
    except:
        pass
    return None

def update_realtime_confidence(initial_confidence, direction, crypto_price, ptb_price, atr_val):
    """
    简化版贝叶斯更新：根据价格偏离PTB的程度动态调整置信度
    
    依据: 文档的序列贝叶斯更新 P(H|D1...Dt) ∝ P(H)×ΠP(Dk|H)
    简化为: 用价格偏离ATR倍数来衡量似然变化
    """
    if not crypto_price or not ptb_price or not atr_val or atr_val <= 0:
        return initial_confidence
    
    diff = crypto_price - ptb_price
    diff_in_atr = abs(diff) / atr_val
    
    if direction == "UP":
        if diff > 0:
            # 价格在PTB之上，方向正确 → 提升置信度
            boost = min(diff_in_atr * 0.08, 0.15)
            return min(initial_confidence + boost, 0.99)
        else:
            # 价格在PTB之下，方向错误 → 降低置信度
            penalty = min(diff_in_atr * 0.12, 0.5)
            return max(initial_confidence - penalty, 0.1)
    else:  # DOWN
        if diff < 0:
            # 价格在PTB之下，方向正确 → 提升置信度
            boost = min(diff_in_atr * 0.08, 0.15)
            return min(initial_confidence + boost, 0.99)
        else:
            # 价格在PTB之上，方向错误 → 降低置信度
            penalty = min(diff_in_atr * 0.12, 0.5)
            return max(initial_confidence - penalty, 0.1)

def should_stop_loss(direction, current_price, ptb_price, elapsed_seconds, initial_confidence=0.85, atr_val=None):
    """判断是否应该提前止损（升级版：基于实时置信度）"""
    if not current_price or not ptb_price or elapsed_seconds < 30:
        return False
    
    # 如果有ATR，使用实时置信度判断
    if atr_val and atr_val > 0:
        updated_conf = update_realtime_confidence(initial_confidence, direction, current_price, ptb_price, atr_val)
        
        # 置信度降到60%以下 → 止损
        if updated_conf < 0.60:
            return True
        
        # 置信度在60-70%且已过30秒 → 止损
        if updated_conf < 0.70 and elapsed_seconds >= 30:
            return True
        
        return False
    
    # fallback: 原逻辑
    if direction == "UP" and current_price < ptb_price:
        return True
    if direction == "DOWN" and current_price > ptb_price:
        return True
    
    return False

def is_losing_direction(direction, current_price, ptb_price, remaining_seconds):
    """判断当前方向是否必输（用于平仓窗口）"""
    if not current_price or not ptb_price or remaining_seconds > 60:
        return False
    
    # 买UP但价格低于PTB，且距离结束<60秒
    if direction == "UP" and current_price < ptb_price:
        return True
    
    # 买DOWN但价格高于PTB，且距离结束<60秒
    if direction == "DOWN" and current_price > ptb_price:
        return True
    
    return False

def smart_sell_position(token_id, size, is_losing=False):
    """智能平仓：基于流动性分析选择最优策略"""
    
    # 1. 分析流动性
    liquidity = analyze_liquidity(token_id, size)
    
    # 如果分析失败，回退到传统方法
    if not liquidity:
        print("  ⚠️ 流动性分析失败，使用传统策略")
        return None
    
    score = liquidity['score']
    best_bid = liquidity['best_bid']
    print(f"  📊 流动性评分: {score}/10 | 最佳买价: ${best_bid:.3f}")
    
    # 2. 根据评分选择策略
    if score >= 8.0:
        # 优秀：流动性匹配
        optimal_price = find_optimal_price(liquidity, size)
        if optimal_price:
            print(f"  ✅ 策略：流动性匹配 | 价格: ${optimal_price:.3f}")
            success, output, actual_price = sell_position(token_id, size, optimal_price, max_retries=2)
            if success:
                return True, actual_price or optimal_price, output
    
    elif score >= 6.0:
        # 良好：优先尝试最佳买价
        if liquidity['best_bid_size'] >= size:
            price = best_bid * 0.99
            print(f"  ✅ 策略：单价格（流动性充足）| 价格: ${price:.3f}")
            success, output, actual_price = sell_position(token_id, size, price, max_retries=2)
            if success:
                return True, actual_price or price, output
    
    # 3. 评分不高或上述策略失败，使用多价格梯度
    print(f"  ⚠️ 使用多价格梯度策略")
    return None  # 返回None表示需要使用传统多价格策略

def try_sell_with_multiple_prices(token_id, size, best_bid, current_price, entry_price, is_losing):
    """多价格尝试平仓（改进版：更激进的价格策略）"""
    
    # 检测订单簿健康度
    orderbook_healthy = best_bid and best_bid >= 0.10 and best_bid >= current_price * 0.5 if current_price else False
    
    if is_losing:
        # 必输时，使用极端激进策略
        if orderbook_healthy:
            prices = [
                best_bid * 0.98,
                best_bid * 0.95,
                best_bid * 0.90,
                best_bid * 0.85,
                best_bid * 0.80,
                0.10,
                0.05,
                0.01
            ]
        else:
            # 订单簿崩溃，直接市价
            prices = [
                current_price * 0.95 if current_price else None,
                current_price * 0.90 if current_price else None,
                current_price * 0.80 if current_price else None,
                current_price * 0.70 if current_price else None,
                0.10,
                0.05,
                0.01
            ]
    else:
        # 正常情况，使用渐进策略
        if orderbook_healthy:
            prices = [
                best_bid * 0.99,
                best_bid * 0.98,
                best_bid * 0.97,
                best_bid * 0.95,
                best_bid * 0.93,
                best_bid * 0.90,
                0.15,
                0.10
            ]
        else:
            # 订单簿不健康，使用密集梯度
            prices = [
                current_price * 0.99 if current_price else None,
                current_price * 0.98 if current_price else None,
                current_price * 0.97 if current_price else None,
                current_price * 0.95 if current_price else None,
                current_price * 0.93 if current_price else None,
                current_price * 0.90 if current_price else None,
                0.15,
                0.10
            ]
    
    # 过滤有效价格
    valid_prices = [p for p in prices if p and p >= 0.01]
    
    # 尝试每个价格，每次失败后等待1秒
    for i, price in enumerate(valid_prices):
        success, output, actual_price = sell_position(token_id, size, price, max_retries=2)
        if success:
            return True, actual_price or price, output
        # 前几次失败后等待，让市场稳定
        if i < len(valid_prices) - 1:
            time.sleep(1)
    
    return False, None, "所有价格尝试均失败"

# 预挂单状态追踪
_pre_orders = {}  # slug -> {"type": "take_profit"|"close", "time": timestamp}

def place_pre_order(token_id, size, price, slug, order_type):
    """预挂卖单"""
    global _pre_orders
    
    success, output, actual_price = sell_position(token_id, size, price)
    if success:
        _pre_orders[slug] = {
            "type": order_type,
            "price": actual_price or price,
            "time": datetime.now(timezone.utc).isoformat()
        }
        print(f"  📋 预挂单成功: {order_type} @ ${(actual_price or price):.2f}")
    else:
        print(f"  ❌ 预挂单失败: {output[:80]}")
    return success

def has_pre_order(slug):
    """检查是否已有预挂单"""
    return slug in _pre_orders

def cancel_all_orders(token_id):
    """取消所有活跃订单"""
    try:
        cmd = ["polymarket", "clob", "cancel-all", "--token", token_id, "--signature-type", "gnosis-safe"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return result.returncode == 0
    except:
        return False

def check_balance_changed(token_id, expected_size):
    """通过查询token余额判断是否成交（余额减少=卖出成功）"""
    try:
        cmd = ["polymarket", "clob", "balance", "--signature-type", "gnosis-safe"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            # 解析余额输出，检查token是否还在
            return token_id not in result.stdout
    except:
        pass
    return False

def sell_and_confirm(token_id, size, price, timeout_sec=5):
    """挂单并确认成交，未成交则取消
    返回: (success, msg_or_actual_price)
    """
    success, output, actual_price = sell_position(token_id, size, price, max_retries=1)
    if not success:
        # sell_position现在只有MATCHED才返回True，所以失败=未成交
        cancel_all_orders(token_id)
        return False, "未成交已取消"
    
    return True, actual_price or price

def sell_in_batches(token_id, total_size, base_price):
    """分批出货 - 确保全部卖完"""
    if total_size <= 5:
        return sell_and_confirm(token_id, total_size, base_price, timeout_sec=3)
    
    # 分3批
    batches = [
        (int(total_size * 0.5), base_price * 0.99),
        (int(total_size * 0.3), base_price * 0.97),
        (total_size - int(total_size * 0.5) - int(total_size * 0.3), base_price * 0.95),
    ]
    
    sold_total = 0
    sold_prices = []
    
    for batch_size, batch_price in batches:
        if batch_size <= 0:
            continue
        batch_price = round(batch_price, 2)
        success, output, actual_price = sell_position(token_id, batch_size, batch_price, max_retries=2)
        if success:
            real_price = actual_price or batch_price
            sold_total += batch_size
            sold_prices.append(real_price)
            print(f"    ✅ 分批 {batch_size}份 @ ${real_price:.2f}")
            
            msg = f"📦 <b>分批平仓</b>\n\n已卖出 {batch_size}份 @ ${real_price:.2f}\n累计: {sold_total}/{total_size}份"
            send_telegram(msg)
        else:
            print(f"    ❌ 分批失败 {batch_size}份 @ ${batch_price:.2f}，降价重试")
            for retry_price in [batch_price * 0.90, batch_price * 0.80, 0.10, 0.05, 0.01]:
                retry_price = round(retry_price, 2)
                success2, _, actual_price2 = sell_position(token_id, batch_size, retry_price, max_retries=1)
                if success2:
                    real_price2 = actual_price2 or retry_price
                    sold_total += batch_size
                    sold_prices.append(real_price2)
                    print(f"    ✅ 降价成功 {batch_size}份 @ ${real_price2:.2f}")
                    msg = f"📦 <b>分批平仓(降价)</b>\n\n已卖出 {batch_size}份 @ ${real_price2:.2f}\n累计: {sold_total}/{total_size}份"
                    send_telegram(msg)
                    break
    
    if sold_total > 0:
        avg_price = sum(sold_prices) / len(sold_prices) if sold_prices else base_price * 0.97
        return True, sold_total, avg_price
    return False, 0, None

def monitor():
    """主监控循环 - 重构版：提前卖、分批卖、确认成交"""
    print("🔍 持仓监控 v2 启动（4阶段平仓 | 120s→90s→60s→30s）...")
    
    close_attempts = {}  # slug -> attempts count
    
    while True:
        try:
            positions = get_open_positions()
            
            if not positions:
                time.sleep(3)
                continue
            
            for pos in positions:
                token_id = pos["token_id"]
                entry_price = pos["entry_price"]
                size = pos["size"]
                slug = pos.get("slug", "unknown")
                coin = "BTC" if "btc" in slug else "ETH"
                direction = pos.get("direction", "UP")
                
                # 获取当前token价格
                current_price = get_market_price(token_id)
                if current_price is None:
                    continue
                
                profit_rate = (current_price - entry_price) / entry_price if entry_price > 0 else 0
                
                # 获取市场剩余时间
                end_timestamp = get_market_end_time(slug)
                if not end_timestamp:
                    continue
                
                now = datetime.now(timezone.utc)
                end_time = datetime.fromtimestamp(end_timestamp, tz=timezone.utc)
                remaining = (end_time - now).total_seconds()
                
                # 获取PTB和实时价格判断赢输
                ptb_price = get_ptb_from_slug(slug)
                crypto_price = get_current_crypto_price(coin)
                is_losing = is_losing_direction(direction, crypto_price, ptb_price, remaining) if ptb_price and crypto_price else False
                is_winning = not is_losing if ptb_price and crypto_price else False
                
                status = "🟢赢" if is_winning else "🔴输" if is_losing else "⚪"
                print(f"  📈 {coin} {direction} | ${entry_price:.2f}→${current_price:.2f} ({profit_rate*100:+.1f}%) | 剩余{remaining:.0f}s | {status}")
                
                sold = False
                
                # ═══ 阶段1：结束前120-90秒（流动性健康期）═══
                if 90 < remaining <= 120:
                    
                    # 利润≥10%直接止盈
                    if profit_rate >= 0.10:
                        print(f"  🎯 阶段1止盈（{profit_rate*100:.1f}%）")
                        best_bid = get_best_bid(token_id)
                        if best_bid:
                            success, output, actual_price = sell_position(token_id, size, best_bid, max_retries=2)
                            if success:
                                sold = True
                                self_notify(pos, actual_price or best_bid, coin, direction, size, "阶段1止盈")
                    
                    # 必赢 → 高价卖出
                    elif is_winning:
                        print(f"  🟢 阶段1：必赢，高价卖出")
                        best_bid = get_best_bid(token_id)
                        if best_bid and best_bid > entry_price:
                            success, output, actual_price = sell_position(token_id, size, best_bid, max_retries=2)
                            if success:
                                sold = True
                                self_notify(pos, actual_price or best_bid, coin, direction, size, "阶段1必赢平仓")
                    
                    # 必输 → 立即止损
                    elif is_losing:
                        print(f"  🔴 阶段1：必输，立即止损！")
                        best_bid = get_best_bid(token_id)
                        sell_price = best_bid if best_bid and best_bid > 0.05 else round(current_price * 0.95, 2)
                        success, output, actual_price = sell_position(token_id, size, sell_price, max_retries=2)
                        if success:
                            sold = True
                            self_notify(pos, actual_price or sell_price, coin, direction, size, "阶段1必输止损")
                
                # ═══ 阶段2：结束前90-60秒（流动性下降期）═══
                elif 60 < remaining <= 90:
                    print(f"  ⚠️ 阶段2：{'分批挂单' if size > 5 else '挂单确认'}")
                    
                    best_bid = get_best_bid(token_id)
                    if not best_bid or best_bid < 0.02:
                        best_bid = current_price * 0.95 if current_price else 0.10
                    
                    if size <= 5:
                        price = round(best_bid * (0.97 if is_losing else 0.99), 2)
                        success, actual = sell_and_confirm(token_id, size, price, timeout_sec=4)
                        if success:
                            sold = True
                            self_notify(pos, actual, coin, direction, size, "阶段2平仓")
                        else:
                            price2 = round(best_bid * 0.95, 2)
                            success2, actual2 = sell_and_confirm(token_id, size, price2, timeout_sec=4)
                            if success2:
                                sold = True
                                self_notify(pos, actual2, coin, direction, size, "阶段2降价平仓")
                    else:
                        ok, sold_count, avg_price = sell_in_batches(token_id, size, best_bid)
                        if ok and sold_count >= size * 0.5:
                            sold = True
                            self_notify(pos, avg_price or best_bid * 0.97, coin, direction, sold_count, f"阶段2分批({sold_count}/{size})")
                
                # ═══ 阶段3：结束前60-30秒（流动性枯竭期）═══
                elif 30 < remaining <= 60:
                    print(f"  🚨 阶段3：激进平仓")
                    
                    result = smart_sell_position(token_id, size, is_losing)
                    if result:
                        success, sell_price, output = result
                        if success:
                            sold = True
                            self_notify(pos, sell_price, coin, direction, size, "阶段3智能平仓")
                    
                    if not sold:
                        best_bid = get_best_bid(token_id)
                        success, sell_price, output = try_sell_with_multiple_prices(
                            token_id, size, best_bid, current_price, entry_price, True
                        )
                        if success:
                            sold = True
                            self_notify(pos, sell_price, coin, direction, size, "阶段3激进平仓")
                
                # ═══ 阶段4：结束前30秒（最后机会）═══
                elif 0 < remaining <= 30:
                    print(f"  💀 阶段4：最后机会")
                    for price in [0.10, 0.05, 0.02, 0.01]:
                        success, output, actual_price = sell_position(token_id, size, price, max_retries=1)
                        if success:
                            sold = True
                            self_notify(pos, actual_price or price, coin, direction, size, "阶段4兜底")
                            break
                
                # ═══ 市场已关闭 ═══
                elif remaining <= 0:
                    if check_market_closed(slug):
                        print(f"  ⏳ {slug} 已关闭")
                        close_position(pos, entry_price)
                        close_attempts.pop(slug, None)
                        continue
                
                if sold:
                    close_attempts.pop(slug, None)
                    continue
                
                if remaining <= 120:
                    close_attempts[slug] = close_attempts.get(slug, 0) + 1
                    if close_attempts[slug] % 5 == 0:
                        print(f"  ⚠️ {slug} 已尝试{close_attempts[slug]}次未成功")
        
        except Exception as e:
            print(f"❌ 监控错误: {e}")
        
        time.sleep(2)


def self_notify(pos, sell_price, coin, direction, size, label):
    """统一平仓通知"""
    entry_price = pos["entry_price"]
    profit = (sell_price - entry_price) * size
    pct = (sell_price - entry_price) / entry_price * 100 if entry_price > 0 else 0
    emoji = "📈" if profit > 0 else "📉"
    print(f"  ✅ {label}！盈亏: ${profit:+.2f} ({pct:+.1f}%)")
    
    msg = (
        f"{emoji} <b>{label}</b>\n\n"
        f"币种: {coin} | 方向: {direction}\n"
        f"入场: ${entry_price:.2f} × {size}份\n"
        f"出场: ${sell_price:.2f} × {size}份\n"
        f"{'盈利' if profit > 0 else '亏损'}: ${abs(profit):.2f} ({pct:+.1f}%)"
    )
    send_telegram(msg)


if __name__ == "__main__":
    monitor()
