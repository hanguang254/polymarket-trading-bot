#!/usr/bin/env python3
"""
Polymarket 全自动交易机器人 v3.1
核心策略：预热观察 → 趋势确认下注 → 实时盯盘平仓

时间线（5分钟=300秒市场）：
  0s    市场开始
  60s   预热扫描开始（积累趋势数据）
  120s  下注窗口开启（已有60秒观察数据）
  180s  下注窗口关闭
  下注后 立即进入实时监控（止盈/止损/趋势反转）
  120s  时间止盈（盈利中挂单锁利）
  60s   强制平仓开始
  30s   激进清仓
  10s   地板价清仓
  0s    市场结束
"""
import logging
import sys
import time
import json
import os
import requests
from datetime import datetime, timezone, timedelta

# --- 新增日志配置 ---
LOG_FILE = "logs/polymarket-bot.log"
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True) # 确保logs目录存在

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, mode='a'), # 使用追加模式
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)
# --- 日志配置结束 ---

sys.path.insert(0, "/root/.openclaw/workspace/polymarket-arb-bot")

from ai_trader.polymarket_api import get_current_markets
from ai_analyze_v2 import analyze_and_decide
from trading_state import should_trade, decrease_cooldown, get_state_summary, record_bet_result


def get_ptb_multi_strategy(slug, coin="BTC"):
    """多层获取 PTB"""
    ts = int(slug.split("-")[-1])
    
    # 1. API 直接获取
    try:
        resp = requests.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=5)
        if resp.status_code == 200:
            events = resp.json()
            if events:
                meta = events[0].get("eventMetadata", {})
                ptb = meta.get("priceToBeat")
                if ptb:
                    return float(ptb)
    except:
        pass
    
    # 2. Playwright
    try:
        from ai_trader.playwright_ptb import get_price_to_beat_playwright
        ptb = get_price_to_beat_playwright(slug, timeout_ms=12000)
        if ptb:
            return ptb
    except:
        pass
    
    # 3. Binance 近似
    symbol = f"{coin}USDT"
    try:
        resp = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": "1m", "startTime": ts * 1000, "limit": 1},
            timeout=3,
        )
        if resp.status_code == 200:
            kline = resp.json()[0]
            return float(kline[1])
    except:
        pass
    
    return None


def get_token_ids(slug):
    """获取市场的 token_ids"""
    try:
        resp = requests.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=5)
        if resp.status_code == 200:
            events = resp.json()
            if events and events[0].get('markets'):
                markets = events[0]['markets']
                if markets:
                    token_ids = eval(markets[0].get('clobTokenIds', '[]'))
                    if len(token_ids) >= 2:
                        return str(token_ids[0]), str(token_ids[1])  # UP, DOWN
    except:
        pass
    return None, None


def send_notification(coin, direction, confidence, ev, price, size):
    """发送下注通知（直接发送Telegram）"""
    notify_text = (
        f"🎯 <b>Polymarket 下注成功</b>\n\n"
        f"币种: {coin}\n"
        f"方向: {direction}\n"
        f"置信度: {confidence*100:.0f}%\n"
        f"EV: {ev:+.3f}\n"
        f"价格: ${price:.2f} × {size}份 = ${price*size:.2f}"
    )
    try:
        TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        TELEGRAM_CHAT_ID = "1609325006"
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": notify_text, "parse_mode": "HTML"}, timeout=10)
    except:
        pass


def send_close_notification(coin, direction, entry_price, exit_price, size, pnl):
    """发送平仓通知（直接发送Telegram）"""
    pnl_emoji = "📈" if pnl > 0 else "📉"
    pnl_text = "盈利" if pnl > 0 else "亏损"
    notify_text = (
        f"{pnl_emoji} <b>Polymarket 平仓通知</b>\n\n"
        f"币种: {coin}\n"
        f"方向: {direction}\n"
        f"入场: ${entry_price:.3f} × {size}份 = ${entry_price*size:.2f}\n"
        f"出场: ${exit_price:.3f} × {size}份 = ${exit_price*size:.2f}\n"
        f"{pnl_text}: <b>${abs(pnl):.2f}</b>"
    )
    try:
        TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        TELEGRAM_CHAT_ID = "1609325006"
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": notify_text, "parse_mode": "HTML"}, timeout=10)
    except:
        pass


def close_position(token_id, size=5, time_remaining=None):
    """
    改进的平仓函数 v2
    - 分级策略：根据剩余时间自动切换模式
    - 多次重试：每个模式多次尝试不同价格
    - 动态订单簿：每次重试前重新获取最新数据
    """
    import subprocess
    
    # 自动判断模式
    if time_remaining is not None:
        if time_remaining < 20:
            mode = "emergency"
            max_retries = 8
            retry_delay = 1
        elif time_remaining < 45:
            mode = "urgent"
            max_retries = 5
            retry_delay = 2
        else:
            mode = "normal"
            max_retries = 3
            retry_delay = 3
    else:
        mode = "normal"
        max_retries = 3
        retry_delay = 3
    
    print(f"  🔔 平仓模式: {mode} | 剩余: {time_remaining}s | 重试: {max_retries}次")
    
    for attempt in range(max_retries):
        # 获取最新订单簿
        try:
            resp = requests.get(f"https://clob.polymarket.com/book?token_id={token_id}", timeout=3)
            if resp.status_code == 200:
                bids = resp.json().get('bids', [])
                best_bid = float(bids[0]['price']) if bids else None
            else:
                best_bid = None
        except:
            best_bid = None
        
        # 根据模式选择价格策略
        if mode == "emergency":
            prices = [best_bid * 0.95, best_bid * 0.90, best_bid * 0.80, 0.05, 0.01] if best_bid and best_bid >= 0.05 else [0.05, 0.01]
        elif mode == "urgent":
            prices = [best_bid * 0.98, best_bid * 0.95, best_bid * 0.90, 0.10, 0.05] if best_bid and best_bid >= 0.10 else [0.10, 0.05]
        else:
            prices = [best_bid * 0.99, best_bid * 0.97, best_bid * 0.95] if best_bid and best_bid >= 0.20 else [0.20, 0.15]
        
        # 过滤有效价格
        valid_prices = [round(p, 3) for p in prices if p and p >= 0.01]
        
        # 尝试每个价格
        for price in valid_prices:
            cmd = [
                "polymarket", "clob", "create-order",
                "--signature-type", "eoa",
                "--token", token_id,
                "--side", "sell",
                "--price", str(price),
                "--size", str(size),
            ]
            
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
                if result.returncode == 0:
                    print(f"  ✅ 成交！价格: ${price:.3f}")
                    return True, price, result.stdout
            except:
                pass
        
        # 重试前等待
        if attempt < max_retries - 1:
            time.sleep(retry_delay)
    
    return False, None, f"Failed after {max_retries} attempts"


class Position:
    """持仓管理"""
    def __init__(self, slug, token_id, direction, entry_price, size, entry_time):
        self.slug = slug
        self.token_id = token_id
        self.direction = direction
        self.entry_price = entry_price
        self.size = size
        self.entry_time = entry_time
        self.closed = False
        self.exit_price = None
        self.exit_time = None
        self.pnl = None


class MarketTracker:
    def __init__(self):
        self.tracked = {}  # slug -> market info
        self.ptb_cache = {}
        self.analyzed = set()
        self.positions = {}  # slug -> Position
        self.warmup_data = {}  # slug -> [{price, direction, timestamp}, ...]
        self.warmup_started = set()  # 已开始预热的市场
    
    def update_markets(self):
        """更新市场列表"""
        markets = get_current_markets()
        now = datetime.now(timezone.utc)
        
        for market in markets:
            slug = market["slug"]
            
            if slug not in self.tracked:
                self.tracked[slug] = market
                end_dt = datetime.fromisoformat(market["end_time"].replace("Z", "+00:00"))
                remaining = (end_dt - now).total_seconds()
                logger.info(f"\n🆕 新市场: {market['coin']} | {slug}")
                # 转换为美国东部时间显示
                et_dt = end_dt - timedelta(hours=5)  # UTC-5 (EST)
                logger.info(f"   结束: {et_dt.strftime('%H:%M:%S')} ET | 剩余: {remaining:.0f}s")
            else:
                self.tracked[slug]["up_odds"] = market["up_odds"]
                self.tracked[slug]["down_odds"] = market["down_odds"]
        
        return markets
    
    def check_analysis_trigger(self):
        """预热观察 + 趋势确认下注（优化版）"""
        now = datetime.now(timezone.utc)
        
        for slug, market in list(self.tracked.items()):
            end_dt = datetime.fromisoformat(market["end_time"].replace("Z", "+00:00"))
            start_dt = end_dt - timedelta(minutes=5)
            elapsed = (now - start_dt).total_seconds()
            remaining = (end_dt - now).total_seconds()
            
            # === PTB 获取期：30s-60s ===
            if 30 <= elapsed < 60 and slug not in self.ptb_cache:
                logger.info(f"\n💰 获取 PTB: {market['coin']} | {slug}")
                ptb = get_ptb_multi_strategy(slug, market['coin'])
                if ptb:
                    self.ptb_cache[slug] = ptb
                    logger.info(f"   PTB: ${ptb:,.2f}")
                else:
                    logger.warning(f"   ⚠️ PTB 获取失败")
            
            # === 预热期：40s-100s（60秒采样） ===
            if 40 <= elapsed <= 100 and slug not in self.analyzed:
                if slug not in self.warmup_started:
                    self.warmup_started.add(slug)
                    self.warmup_data[slug] = []
                    logger.info(f"\n🔥 预热开始: {market['coin']} | {slug} | 剩余{remaining:.0f}s")
                
                # 每5秒采集一次趋势数据
                samples = self.warmup_data.get(slug, [])
                if len(samples) == 0 or (time.time() - samples[-1]["ts"]) >= 5:
                    try:
                        from ai_trader.binance_api import get_current_price
                        symbol = f"{market['coin']}USDT"
                        price = get_current_price(symbol)
                        if price:
                            direction = "UP" if len(samples) > 0 and price > samples[-1].get("price", price) else "DOWN"
                            samples.append({"price": price, "direction": direction, "ts": time.time()})
                            self.warmup_data[slug] = samples
                    except Exception as e:
                        logger.warning(f"  ⚠️ 预热采样失败: {e}")
            
            # === 下注窗口：100s-160s（已有60秒观察数据） ===
            if 100 <= elapsed <= 160 and slug not in self.analyzed:
                self.analyzed.add(slug)
                
                # 趋势确认（参考，不阻止分析）
                samples = self.warmup_data.get(slug, [])
                trend_strength = self._calc_trend_strength(samples)
                
                # 趋势信息传递给分析引擎
                extra_info = {"trend_strength": trend_strength, "warmup_samples": len(samples)}
                
                if trend_strength == "震荡":
                    logger.warning(f"\n⚠️ {market['coin']} | {slug} | 趋势震荡，提高阈值到15%")
                    extra_info["min_discount"] = 0.15  # 震荡时提高折价阈值
                
                self.analyze_and_trade(slug, market, extra_info)
    
    def _calc_trend_strength(self, samples):
        """计算趋势强度"""
        if len(samples) < 4:
            return "震荡"
        
        # 计算价格方向
        ups = 0
        for i in range(1, len(samples)):
            if samples[i]["price"] > samples[i-1]["price"]:
                ups += 1
        
        total = len(samples) - 1
        if total == 0:
            return "震荡"
        
        ratio = ups / total
        
        if ratio >= 0.75 or ratio <= 0.25:
            return "强趋势"
        elif ratio >= 0.6 or ratio <= 0.4:
            return "中趋势"
        else:
            return "震荡"
    
    def check_close_trigger(self):
        """平仓逻辑已移至position_monitor.py统一管理"""
        pass
    
    def analyze_and_trade(self, slug, market, extra_info=None):
        """分析并下注（带趋势确认）"""
        coin = market["coin"]
        up_odds = market["up_odds"]
        down_odds = market["down_odds"]
        trend = extra_info.get("trend_strength", "未知") if extra_info else "未知"
        warmup_n = extra_info.get("warmup_samples", 0) if extra_info else 0
        
        logger.info(f"\n{'='*60}")
        logger.info(f"🔔 分析市场: {coin} | {slug}")
        logger.info(f"  📊 趋势: {trend} | 预热采样: {warmup_n}个")
        
        # 获取 PTB（优先使用缓存）
        ptb = self.ptb_cache.get(slug)
        if not ptb:
            logger.warning("  ⚠️ PTB 未缓存，尝试实时获取...")
            ptb = get_ptb_multi_strategy(slug, coin)
            if ptb:
                self.ptb_cache[slug] = ptb
        if not ptb:
            print("  ❌ 无法获取 PTB")
            logger.info(f"{'='*60}\n")
            return
        
        self.ptb_cache[slug] = ptb
        print(f"  💰 PTB: ${ptb:,.2f}")
        print(f"  📊 赔率: UP={up_odds:.3f} DOWN={down_odds:.3f}")
        
        # AI 分析
        should_bet, direction, confidence, details = analyze_and_decide(
            coin, ptb, up_odds, down_odds, slug
        )
        
        logger.info(f"  🤖 AI: {direction} | 置信度: {confidence*100:.0f}%")
        logger.info(f"  💵 折价: ${details.get('discount',0):.3f} | 估值: ${details.get('estimated_value',0):.2f} | ATR偏离: {details.get('diff_in_atr',0):.2f}")
        
        if not should_bet:
            logger.warning(f"  ❌ 不满足: {details.get('bet_reason','')}")
            decrease_cooldown()
            logger.info(f"{'='*60}\n")
            return
        
        logger.info(f"  ✅ 满足下注条件！")
        
        # 检查冷却期
        if not should_trade():
            cooldown = decrease_cooldown()
            print(f"  ⏸️ 冷却期中，观望剩余 {cooldown} 期")
            logger.info(f"{'='*60}\n")
            return
        
        # 获取 token_id
        up_token, down_token = get_token_ids(slug)
        if not up_token or not down_token:
            logger.error(f"  ❌ 无法获取 token_id")
            logger.info(f"{'='*60}\n")
            return
        
        token_id = up_token if direction == "UP" else down_token
        
        # 执行下注
        logger.info(f"  💸 执行下注: {direction} | Token: {token_id[:16]}...")
        from ai_analyze_v2 import execute_bet
        success, entry_price, bet_size, output = execute_bet(slug, direction, token_id, confidence=confidence, ev=details.get('expected_value', 0.5))
        
        if success:
            logger.info(f"  ✅ 下注成功！（{bet_size}份）")
            
            # 记录持仓（使用实际下单价格和动态仓位）
            position = Position(
                slug, token_id, direction, entry_price, bet_size,
                datetime.now(timezone.utc).isoformat()
            )
            self.positions[slug] = position
            
            record_bet_result(True, slug)
            
            # 发送通知
            send_notification(coin, direction, confidence, details.get('expected_value', 0), entry_price, bet_size)
        else:
            logger.error(f"  ❌ 下注失败: {output[:150]}")
            record_bet_result(False, slug)
        
        logger.info(f"  📊 {get_state_summary()}")
        print(f"{'='*60}\n")
    
    def close_position(self, slug, position):
        """平仓"""
        print(f"\n{'='*60}")
        print(f"🔔 平仓: {position.direction} | {slug}")
        
        # 计算剩余时间
        try:
            end_ts = int(slug.split("-")[-1])
            time_remaining = end_ts - int(time.time())
        except:
            time_remaining = None
        
        success, exit_price, output = close_position(position.token_id, position.size, time_remaining)
        
        if success:
            position.closed = True
            position.exit_price = exit_price
            position.exit_time = datetime.now(timezone.utc).isoformat()
            
            # 计算盈亏
            entry_cost = position.entry_price * position.size
            exit_value = exit_price * position.size
            pnl = exit_value - entry_cost
            position.pnl = pnl
            
            print(f"  ✅ 平仓成功！")
            print(f"  💰 入场: ${position.entry_price:.3f} × {position.size} = ${entry_cost:.2f}")
            print(f"  💰 出场: ${exit_price:.3f} × {position.size} = ${exit_value:.2f}")
            print(f"  {'📈' if pnl > 0 else '📉'} 盈亏: ${pnl:+.2f}")
            
            # 发送Telegram通知
            coin = "BTC" if "btc" in slug.lower() else "ETH"
            send_close_notification(coin, position.direction, position.entry_price, exit_price, position.size, pnl)
            
            # 记录到日志
            log_entry = {
                "timestamp": position.exit_time,
                "slug": slug,
                "direction": position.direction,
                "entry_price": position.entry_price,
                "exit_price": exit_price,
                "size": position.size,
                "pnl": pnl,
            }
            os.makedirs("logs", exist_ok=True)
            with open("logs/closed_positions.jsonl", "a") as f:
                f.write(json.dumps(log_entry) + "\n")
        else:
            print(f"  ❌ 平仓失败: {output[:150]}")
        
        print(f"{'='*60}\n")
    
    def cleanup(self):
        """清理旧数据"""
        now = datetime.now(timezone.utc)
        
        for slug in list(self.tracked.keys()):
            end_dt = datetime.fromisoformat(self.tracked[slug]["end_time"].replace("Z", "+00:00"))
            if (now - end_dt).total_seconds() > 120:
                del self.tracked[slug]
                self.ptb_cache.pop(slug, None)
                self.positions.pop(slug, None)


def main():
    print("🤖 Polymarket 全自动交易机器人 v3.2 启动")
    print("   策略: PTB预获取 → 预热观察 → 趋势确认下注")
    print("   时间线: 30-60s获取PTB → 40-100s预热 → 100-160s下注")
    print("   趋势确认: 强/中趋势正常阈值，震荡提高到15%")
    print("   容错: 自动捕获异常，避免EPIPE崩溃")
    print()
    
    tracker = MarketTracker()
    error_count = 0
    max_errors = 100
    
    try:
        while True:
            try:
                tracker.update_markets()
                tracker.check_analysis_trigger()
                tracker.check_close_trigger()
                tracker.cleanup()
                error_count = 0  # 成功执行，重置错误计数
            except KeyboardInterrupt:
                raise
            except Exception as e:
                error_count += 1
                error_type = type(e).__name__
                print(f"❌ 循环错误 ({error_count}/{max_errors}): {error_type}: {e}")
                
                # 如果是EPIPE错误，记录但继续运行
                if "EPIPE" in str(e) or "Broken pipe" in str(e):
                    print("⚠️ Playwright EPIPE错误，已自动恢复")
                
                # 连续错误太多，停止运行
                if error_count >= max_errors:
                    print(f"🚨 连续错误{max_errors}次，停止运行")
                    break
                
                time.sleep(5)  # 错误后等待5秒再继续
                continue
            
            time.sleep(2)
    
    except KeyboardInterrupt:
        print("\n⛔ 机器人停止")


if __name__ == "__main__":
    main()
