#!/usr/bin/env python3
"""
持仓止盈监控 - 5分钟市场专用
在市场关闭前的80-100秒窗口内监控价格，达到+15%即止盈
"""
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
import requests
from ai_trader.polymarket_api import normalize_orderbook

# ═══ 日志：print 同时写入 logs/monitor.log ═══
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
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
            path = os.path.join(LOG_DIR, f"monitor_{today}.log")
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

sys.stdout = _TeeWriter(sys.stdout)
sys.stderr = _TeeWriter(sys.stderr)

POSITIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "positions.jsonl")
PROFIT_THRESHOLD = 0.15  # 15% 止盈
SLIPPAGE = float(os.environ.get("SLIPPAGE", "0.01"))  # 空簿回退滑点（env可配置）

# P0 双曲贴现止盈参数（可通过 .env 覆盖）
P0_BASE_PROFIT = float(os.environ.get("P0_BASE_PROFIT", "0.15"))      # 基础止盈阈值
P0_HYPERBOLIC_K = float(os.environ.get("P0_HYPERBOLIC_K", "0.15"))    # 双曲贴现系数

# Telegram 通知配置
# Normalize thresholds to keep P0 base consistent with PROFIT_THRESHOLD.
PROFIT_THRESHOLD = float(os.environ.get("PROFIT_THRESHOLD", str(PROFIT_THRESHOLD)))
P0_BASE_PROFIT = float(os.environ.get("P0_BASE_PROFIT", str(PROFIT_THRESHOLD)))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

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
    """获取当前市场价格 — 多源融合，防止宽价差导致虚假中间价

    5分钟市场临近结束时，bid/ask 可能极端分散（如 $0.01/$0.99），
    此时 midpoint=$0.50 完全不代表真实价值。
    策略：
      1. 订单簿价差合理（<50%）→ 用 midpoint
      2. 价差过大 → 用 midpoint API（服务端计算，通常更准）
      3. 都失败 → 返回 None
    """
    best_bid = None
    best_ask = None
    # 方案1：从订单簿获取 bid/ask
    try:
        resp = requests.get(
            f"https://clob.polymarket.com/book?token_id={token_id}",
            timeout=5
        )
        if resp.status_code == 200:
            data = resp.json()
            bids, asks = normalize_orderbook(data.get('bids', []), data.get('asks', []))
            best_bid = float(bids[0]['price']) if bids else None
            best_ask = float(asks[0]['price']) if asks else None
            if best_bid and best_ask:
                spread = best_ask - best_bid
                mid = (best_bid + best_ask) / 2
                # 价差合理（<50% of mid）→ 信任 midpoint
                if mid > 0 and spread / mid < 0.50:
                    return round(mid, 4)
                # 价差过大 → 不信任订单簿 midpoint，回退
    except:
        pass
    # 方案2：midpoint API（服务端计算，5分钟市场通常更准）
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
    # 方案3：last-trade-price（空簿时的真实市场价）
    ltp = get_last_trade_price(token_id)
    if ltp:
        return ltp
    # 方案4：只有 bid 或 ask，返回有的那个
    if best_bid and best_ask:
        return round((best_bid + best_ask) / 2, 4)
    if best_bid:
        return best_bid
    if best_ask:
        return best_ask
    return None

def get_best_bid_raw(token_id):
    """获取原始最佳买价（止损专用，不打折扣，追求最快成交）"""
    try:
        resp = requests.get(
            f"https://clob.polymarket.com/book?token_id={token_id}",
            timeout=5
        )
        if resp.status_code == 200:
            data = resp.json()
            bids, _ = normalize_orderbook(data.get('bids', []), data.get('asks', []))
            if bids and len(bids) > 0:
                best_bid = float(bids[0]['price'])
                if best_bid > 0.02:
                    return best_bid
    except:
        pass
    # 回退：last-trade-price（不减SLIPPAGE，止损不留余地）
    ltp = get_last_trade_price(token_id)
    if ltp:
        print(f"    📡 get_best_bid_raw空簿回退: last_trade=${ltp:.3f}")
        return ltp
    return None


def get_best_bid(token_id):
    """获取最佳买价（用于卖出）- 从订单簿获取，空簿回退last-trade-price"""
    try:
        resp = requests.get(
            f"https://clob.polymarket.com/book?token_id={token_id}",
            timeout=5
        )
        if resp.status_code == 200:
            data = resp.json()
            bids, _ = normalize_orderbook(data.get('bids', []), data.get('asks', []))
            if bids and len(bids) > 0:
                best_bid = float(bids[0]['price'])
                if best_bid > 0.02:  # 正常市场
                    return best_bid * 0.99
                # 空簿: bid≤0.02，回退last-trade-price
    except:
        pass
    # 回退：last-trade-price - SLIPPAGE
    ltp = get_last_trade_price(token_id)
    if ltp:
        fallback = max(round(ltp - SLIPPAGE, 2), 0.01)
        print(f"    📡 get_best_bid空簿回退: last_trade=${ltp:.3f} → bid=${fallback:.2f}")
        return fallback
    return None

def get_best_ask(token_id):
    """获取最佳卖价（用于买入对冲）- 从订单簿获取，空簿回退last-trade-price"""
    try:
        resp = requests.get(
            f"https://clob.polymarket.com/book?token_id={token_id}",
            timeout=5
        )
        if resp.status_code == 200:
            data = resp.json()
            _, asks = normalize_orderbook(data.get('bids', []), data.get('asks', []))
            if asks and len(asks) > 0:
                ask = float(asks[0]['price'])
                if ask < 0.95:  # 正常市场
                    return ask
                # 空簿: ask≥0.95，回退last-trade-price
    except:
        pass
    # 回退：last-trade-price + SLIPPAGE
    ltp = get_last_trade_price(token_id)
    if ltp:
        fallback = min(round(ltp + SLIPPAGE, 2), 0.99)
        print(f"    📡 get_best_ask空簿回退: last_trade=${ltp:.3f} → ask=${fallback:.2f}")
        return fallback
    return None

def get_last_trade_price(token_id):
    """获取最近成交价（空簿时的真实市场价参考）"""
    try:
        resp = requests.get(
            f"https://clob.polymarket.com/last-trade-price?token_id={token_id}",
            timeout=5
        )
        if resp.status_code == 200:
            price = float(resp.json().get('price', 0))
            if 0.01 < price < 0.99:
                return price
    except:
        pass
    return None

def buy_opposite_token(token_id, size, price, max_retries=2):
    """买入对冲token（BUY订单）
    返回: (success, output, actual_price)
    """
    price = round(price, 2)
    for attempt in range(max_retries):
        cmd = [
            "polymarket", "clob", "create-order",
            "--signature-type", "eoa",
            "--token", token_id,
            "--side", "buy",
            "--price", str(price),
            "--size", str(size)
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                info = parse_order_output(result.stdout)
                if info["matched"]:
                    actual_price = round(info["making"] / size, 4) if size > 0 and info["making"] > 0 else price
                    print(f"    📊 对冲成交: Status={info['status']} | 实际价=${actual_price:.4f}")
                    return True, result.stdout, actual_price
                else:
                    # 避免多次重试叠加挂单
                    cancel_all_orders(token_id)
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
    return False, "All retries failed", None

def analyze_liquidity(token_id, target_size):
    """分析订单簿流动性"""
    try:
        resp = requests.get(f"https://clob.polymarket.com/book?token_id={token_id}", timeout=3)
        if resp.status_code != 200:
            return None

        book = resp.json()
        bids, _ = normalize_orderbook(book.get('bids', []), book.get('asks', []))
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
    info = {
        "status": None,
        "matched": False,
        "making": 0,
        "taking": 0,
        "order_id": None,
        "success": None,
    }
    for line in output.strip().split("\n"):
        line = line.strip()
        lower = line.lower()
        if lower.startswith("order id:") or lower.startswith("order_id:"):
            info["order_id"] = line.split(":", 1)[1].strip()
        elif lower.startswith("success:"):
            val = line.split(":", 1)[1].strip().lower()
            if val in ("true", "false"):
                info["success"] = val == "true"
        elif line.startswith("Status:"):
            info["status"] = line.split(":", 1)[1].strip()
            info["matched"] = info["status"] == "MATCHED"
        elif line.startswith("Making:"):
            try:
                info["making"] = float(line.split(":", 1)[1].strip())
            except:
                pass
        elif line.startswith("Taking:"):
            try:
                info["taking"] = float(line.split(":", 1)[1].strip())
            except:
                pass
    return info

def _check_and_adjust_size(token_id, size):
    """校验链上真实余额，返回调整后的 size。
    返回: adjusted_size (>0正常, 0=余额为零, None=查询失败用原size)
    """
    real_balance = get_token_balance(token_id)
    if real_balance is not None:
        if real_balance <= 0:
            print(f"    ⚠️ 链上余额为0，跳过卖出")
            return 0
        if real_balance < size:
            adjusted = math.floor(real_balance * 100) / 100.0
            print(f"    ⚠️ 链上余额({real_balance})< 记录size({size})，用真实余额卖出({adjusted})")
            return adjusted if adjusted > 0 else 0
        return size
    return None

def market_sell_immediate(token_id, size, price=None):
    """市价立即卖出（止损专用）
    价格策略（逐级降价，追求成交而非价格）：
      1. 外部传入 price（调用方已有 best_bid，避免重复查询）
      2. 未传入 → last-trade-price - 滑点
      3. 都没有 → $0.01（地板价，CLOB按最高bid撮合）
    返回: (success, actual_price)
      success=True: 成交
      success=False, actual_price=None: 正常失败
      success=False, actual_price="NO_BALANCE": token余额为零，不必再重试
    """
    # 先取消可能存在的旧挂单，释放被锁定的token余额
    cancel_all_orders(token_id)

    # 校验链上真实余额
    adjusted = _check_and_adjust_size(token_id, size)
    if adjusted == 0:
        return False, "NO_BALANCE"
    if adjusted is not None:
        size = adjusted

    # 确定卖价：止损优先成交，用传入价格减滑点
    sell_price = None
    if price and price > 0.01:
        # 传入的是best_bid原始值，减滑点确保能吃到bid
        sell_price = round(max(price - SLIPPAGE, 0.01), 2)
    if not sell_price:
        ltp = get_last_trade_price(token_id)
        if ltp:
            sell_price = round(max(ltp - SLIPPAGE, 0.01), 2)
    if not sell_price:
        sell_price = 0.01  # 地板价：CLOB会按最高bid撮合

    print(f"    ⚡ 市价止损: 卖价=${sell_price:.2f}")

    def _try_sell(p):
        cmd = [
            "polymarket", "clob", "create-order",
            "--signature-type", "eoa",
            "--token", token_id,
            "--side", "sell",
            "--price", str(round(p, 2)),
            "--size", str(size)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return result

    try:
        result = _try_sell(sell_price)
        if result.returncode == 0:
            info = parse_order_output(result.stdout)
            if info["matched"]:
                actual_price = round(info["taking"] / size, 4) if size > 0 and info["taking"] > 0 else sell_price
                print(f"    ⚡ 市价成交: Taking=${info['taking']:.4f} | 实际价=${actual_price:.4f}")
                return True, actual_price
            else:
                print(f"    ❌ 市价卖出无买方(Status={info['status']})，取消挂单")
                cancel_all_orders(token_id)
                return False, None
        else:
            err = result.stderr.strip()
            print(f"    ❌ 止损被拒(卖价${sell_price:.2f}): {err[:200]}")

            # "not enough balance" → 减量重试（size精度不匹配链上余额）
            if "not enough balance" in err.lower():
                for retry_size in [round(size * 0.98, 2), round(size * 0.95, 2), round(size * 0.90, 2)]:
                    if retry_size < 1:
                        break
                    print(f"    🔄 余额不足，减量重试: {retry_size}份")
                    cmd_retry = [
                        "polymarket", "clob", "create-order",
                        "--signature-type", "eoa",
                        "--token", token_id,
                        "--side", "sell",
                        "--price", str(sell_price),
                        "--size", str(retry_size)
                    ]
                    try:
                        r = subprocess.run(cmd_retry, capture_output=True, text=True, timeout=15)
                        if r.returncode == 0:
                            info_r = parse_order_output(r.stdout)
                            if info_r["matched"]:
                                ap = round(info_r["taking"] / retry_size, 4) if retry_size > 0 and info_r["taking"] > 0 else sell_price
                                print(f"    ⚡ 减量成交: {retry_size}份 | 实际价=${ap:.4f}")
                                return True, ap
                            else:
                                cancel_all_orders(token_id)
                                return False, None
                        elif "not enough balance" not in r.stderr.lower():
                            break  # 其他错误不再减量
                    except Exception:
                        break
                print(f"    ⚠️ 减量重试均失败，余额可能为零")
                return False, "NO_BALANCE"

            # 其他400错误：降到$0.01地板价重试（CLOB按最高bid撮合）
            if sell_price > 0.02:
                print(f"    🔄 降价重试: $0.01地板价")
                result2 = _try_sell(0.01)
                if result2.returncode == 0:
                    info2 = parse_order_output(result2.stdout)
                    if info2["matched"]:
                        actual_price = round(info2["taking"] / size, 4) if size > 0 and info2["taking"] > 0 else 0.01
                        print(f"    ⚡ 地板价成交: Taking=${info2['taking']:.4f} | 实际价=${actual_price:.4f}")
                        return True, actual_price
                    else:
                        cancel_all_orders(token_id)
                        print(f"    ❌ 地板价也无买方")
                else:
                    print(f"    ❌ 地板价也被拒: {result2.stderr.strip()[:150]}")

            return False, None
    except Exception as e:
        print(f"    ❌ 市价卖出异常: {str(e)[:80]}")
        return False, None

def sell_position(token_id, size, price, max_retries=3):
    """卖出持仓（带重试+成交确认）
    返回: (success, output, actual_price)
      - success: True仅当Status=MATCHED
      - actual_price: 实际成交价（Taking/Size），None表示未知
    """
    # 校验链上真实余额
    adjusted = _check_and_adjust_size(token_id, size)
    if adjusted == 0:
        return False, "NO_BALANCE", None
    if adjusted is not None:
        size = adjusted

    price = round(price, 2)

    for attempt in range(max_retries):
        cmd = [
            "polymarket", "clob", "create-order",
            "--signature-type", "eoa",
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
                    # 避免多次重试叠加挂单
                    cancel_all_orders(token_id)
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

    # P0: 记录交易结果，供 base_rate 校准
    try:
        from ai_trader.base_rate import record_outcome
        entry = position.get("entry_price", 0)
        won = exit_price > entry if entry > 0 else False
        diff_in_atr = position.get("diff_in_atr", 0)
        record_outcome(
            slug=position.get("slug", "unknown"),
            direction=position.get("direction", "UP"),
            diff_in_atr=diff_in_atr,
            won=won,
            extra={"entry_price": entry, "exit_price": exit_price, "size": position.get("size", 0)}
        )
    except Exception:
        pass

    # Bug 5 fix: 用真实 PnL 更新 trading_state
    try:
        from trading_state import record_bet_result
        entry = position.get("entry_price", 0)
        size = position.get("size", 0)
        pnl = (exit_price - entry) * size if entry > 0 else 0.0
        won = exit_price > entry if entry > 0 else False
        record_bet_result(won, position.get("slug", "unknown"), pnl=pnl)
    except Exception:
        pass

def update_position(position, new_size=None, partial_exit=None):
    """更新持仓（如分批卖出后的剩余仓位）"""
    updated = False
    all_positions = []
    if os.path.exists(POSITIONS_FILE):
        with open(POSITIONS_FILE, "r") as f:
            for line in f:
                if line.strip():
                    try:
                        pos = json.loads(line)
                        if isinstance(pos, dict):
                            if pos.get("token_id") == position["token_id"] and pos.get("entry_time") == position["entry_time"]:
                                if new_size is not None:
                                    pos["size"] = new_size
                                if partial_exit:
                                    history = pos.get("partial_exits", [])
                                    history.append(partial_exit)
                                    pos["partial_exits"] = history
                                    pos["last_partial_exit"] = partial_exit
                                pos["updated_time"] = datetime.now(timezone.utc).isoformat()
                                updated = True
                            all_positions.append(pos)
                    except:
                        pass

    if updated:
        with open(POSITIONS_FILE, "w") as f:
            for pos in all_positions:
                f.write(json.dumps(pos) + "\n")
    return updated

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

def _coerce_timestamp(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value) if value > 0 else None
    if isinstance(value, str):
        v = value.strip()
        if not v:
            return None
        if v.isdigit():
            return int(v)
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return int(dt.timestamp())
        except Exception:
            return None
    return None

def resolve_market_end_time(slug, entry_time=None, position=None, market_seconds=300):
    end_timestamp = get_market_end_time(slug)
    if end_timestamp:
        return end_timestamp
    if isinstance(position, dict):
        for key in ("end_timestamp", "end_time", "market_end", "settle_time"):
            ts = _coerce_timestamp(position.get(key))
            if ts:
                return ts
    ts = _coerce_timestamp(entry_time)
    if ts:
        return ts + market_seconds
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

def get_market_outcome(slug, direction):
    """从Gamma API获取真实结算结果（不依赖延迟后的Binance价格）
    返回: settle_price (1.00=赢, 0.00=输, None=未结算/查询失败)
    """
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
                        if isinstance(market, dict) and market.get("closed"):
                            # 方法1: outcome 字段 ("Up" / "Down")
                            outcome = market.get("outcome", "")
                            if outcome:
                                won = (direction == "UP" and outcome.lower() == "up") or \
                                      (direction == "DOWN" and outcome.lower() == "down")
                                return 1.00 if won else 0.00
                            # 方法2: outcome_prices "[1, 0]" 或 "[0, 1]"
                            op = market.get("outcome_prices", "")
                            if op:
                                try:
                                    prices = json.loads(op) if isinstance(op, str) else op
                                    if isinstance(prices, list) and len(prices) >= 2:
                                        up_price = float(prices[0])
                                        if up_price > 0.5:
                                            return 1.00 if direction == "UP" else 0.00
                                        else:
                                            return 1.00 if direction == "DOWN" else 0.00
                                except (ValueError, TypeError):
                                    pass
    except:
        pass
    return None

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
        ptb_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_trader", "playwright_ptb.py")
        result = subprocess.run(
            ["python3", ptb_script, slug],
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

def compute_p0_profit_threshold(remaining_seconds, base_profit, hyperbolic_k, entry_price=None):
    time_factor = remaining_seconds / 60.0
    threshold = base_profit * (1.0 + hyperbolic_k * time_factor)
    # 适配昂贵token：阈值不能超过最大可能利润的80%
    # 入场价0.82时最大利润21.9%，若阈值26%则永远无法触发止盈
    if entry_price and entry_price > 0:
        max_possible_profit = (1.0 - entry_price) / entry_price
        threshold = min(threshold, max_possible_profit * 0.80)
    return threshold

def should_attempt_stop_loss(direction_correct):
    return direction_correct is False

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

def calc_realtime_ev(direction, crypto_price, ptb_price, atr_val, entry_price):
    """
    计算理论EV（基于 BTC/ETH vs PTB 的偏离度）

    仅用于 ATR 信号判断（diff_in_atr），不再作为退出决策的EV。
    退出决策的EV直接用 token_price - entry_price（市场共识）。

    Returns:
        (realtime_ev, p_hat_now, diff_in_atr) or (None, None, None)
    """
    if not all([crypto_price, ptb_price, atr_val]) or atr_val <= 0:
        return None, None, None

    diff = crypto_price - ptb_price
    diff_in_atr = abs(diff) / atr_val

    try:
        from ai_trader.base_rate import get_base_rate
        raw_p = get_base_rate(diff_in_atr)
    except Exception:
        raw_p = min(0.50 + diff_in_atr * 0.08, 0.97)

    price_above_ptb = (diff > 0)
    direction_correct = (
        (direction == "UP" and price_above_ptb) or
        (direction == "DOWN" and not price_above_ptb)
    )
    p_hat_now = raw_p if direction_correct else (1.0 - raw_p)

    realtime_ev = p_hat_now - entry_price
    return realtime_ev, p_hat_now, diff_in_atr


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

# 预挂单状态追踪（持久化到文件，进程重启不丢失）
PRE_ORDERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "pre_orders.json")

# Pending buy orders (LIVE but not yet matched)
PENDING_ORDERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "pending_orders.jsonl")
PENDING_ORDER_TTL = int(os.environ.get("PENDING_ORDER_TTL", "120"))
PENDING_AUTO_CANCEL = os.environ.get("PENDING_AUTO_CANCEL", "0") == "1"
PENDING_MIN_FILL = float(os.environ.get("PENDING_MIN_FILL", "0.5"))

_pending_positions_cache = {"ts": 0, "data": {}}

def _append_pending_update(entry):
    try:
        os.makedirs(os.path.dirname(PENDING_ORDERS_FILE), exist_ok=True)
        with open(PENDING_ORDERS_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass

def _load_pending_orders():
    orders = {}
    try:
        if os.path.exists(PENDING_ORDERS_FILE):
            with open(PENDING_ORDERS_FILE, "r") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue
                    key = entry.get("order_id") or entry.get("pending_id")
                    if not key:
                        key = f"{entry.get('slug','')}-{entry.get('token_id','')}-{entry.get('created_at','')}"
                        entry["pending_id"] = key
                    orders[key] = entry
    except Exception:
        pass
    return orders

def _get_wallet_address():
    return os.environ.get("PROXY_WALLET") or os.environ.get("EOA_WALLET") or ""

def _coerce_float(value):
    try:
        return float(value)
    except Exception:
        return None

def _extract_positions_snapshot(obj, out):
    if isinstance(obj, dict):
        token_id = obj.get("token_id") or obj.get("tokenId") or obj.get("id")
        size = _coerce_float(
            obj.get("size") or obj.get("amount") or obj.get("shares") or obj.get("position")
            or obj.get("qty") or obj.get("quantity")
        )
        if token_id and size is not None:
            avg_price = _coerce_float(
                obj.get("avg_price") or obj.get("avgPrice") or obj.get("averagePrice")
                or obj.get("average_price") or obj.get("entryPrice") or obj.get("price")
            )
            out[str(token_id)] = {"size": size, "avg_price": avg_price}
        for v in obj.values():
            _extract_positions_snapshot(v, out)
    elif isinstance(obj, list):
        for item in obj:
            _extract_positions_snapshot(item, out)

def _fetch_positions_snapshot():
    wallet = _get_wallet_address()
    if not wallet:
        return {}
    try:
        cmd = ["polymarket", "data", "positions", wallet, "-o", "json"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            return {}
        data = json.loads(result.stdout or "{}")
        out = {}
        _extract_positions_snapshot(data, out)
        return {k: v for k, v in out.items() if v.get("size", 0) > 0}
    except Exception:
        return {}

def _get_positions_snapshot_cached(ttl=3):
    now = time.time()
    if now - _pending_positions_cache["ts"] < ttl:
        return _pending_positions_cache["data"]
    data = _fetch_positions_snapshot()
    _pending_positions_cache["ts"] = now
    _pending_positions_cache["data"] = data
    return data

def _load_pre_orders():
    """从文件加载预挂单状态"""
    try:
        if os.path.exists(PRE_ORDERS_FILE):
            with open(PRE_ORDERS_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def _save_pre_orders(data):
    """保存预挂单状态到文件"""
    try:
        os.makedirs(os.path.dirname(PRE_ORDERS_FILE), exist_ok=True)
        with open(PRE_ORDERS_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass

_pre_orders = _load_pre_orders()

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
        _save_pre_orders(_pre_orders)
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
        cmd = ["polymarket", "clob", "cancel-all", "--token", token_id, "--signature-type", "eoa"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return result.returncode == 0
    except:
        return False

def reconcile_pending_orders():
    """Promote LIVE buy orders to positions when they fill; cancel stale ones."""
    orders = _load_pending_orders()
    if not orders:
        return

    active_status = {"LIVE", "PENDING", "ACCEPTED"}
    active = {k: v for k, v in orders.items() if v.get("status") in active_status}
    if not active:
        return

    print(f"  🔄 reconcile: 检查 {len(active)} 笔挂单...")

    # 先尝试 positions snapshot，失败不影响后续 balance 查询
    positions_snapshot = {}
    try:
        positions_snapshot = _get_positions_snapshot_cached()
    except Exception as e:
        print(f"  ⚠️ reconcile: positions snapshot 失败: {e}")

    open_positions = get_open_positions()
    open_keys = {(p.get("slug"), p.get("token_id")) for p in open_positions}

    now = datetime.now(timezone.utc)

    for key, order in active.items():
        slug = order.get("slug")
        token_id = order.get("token_id")
        if not slug or not token_id:
            continue

        # 已经在 positions.jsonl 中有记录，跳过
        if (slug, token_id) in open_keys:
            print(f"  ✅ reconcile: {slug} 已在持仓中，标记 RESOLVED_ALREADY")
            _append_pending_update({
                "order_id": order.get("order_id"),
                "pending_id": order.get("pending_id"),
                "slug": slug,
                "token_id": token_id,
                "status": "RESOLVED_ALREADY",
                "resolved_at": now.isoformat(),
            })
            continue

        created_at = order.get("created_at") or order.get("entry_time")
        age = None
        if created_at:
            try:
                age = (now - datetime.fromisoformat(created_at.replace("Z", "+00:00"))).total_seconds()
            except Exception:
                age = None

        # 检测成交：先查 positions snapshot，再 fallback 到 token balance
        snapshot = positions_snapshot.get(str(token_id))
        filled_size = snapshot.get("size") if snapshot else None

        if not filled_size:
            try:
                balance = get_token_balance(token_id)
                print(f"  📊 reconcile: {slug} token balance={balance}")
                if balance is not None and balance > 0:
                    filled_size = balance
                    snapshot = {"size": balance, "avg_price": None}
            except Exception as e:
                print(f"  ⚠️ reconcile: get_token_balance 失败: {e}")

        if filled_size and filled_size >= PENDING_MIN_FILL:
            print(f"  ✅ reconcile 成交入仓: {slug} token={str(token_id)[:10]}... size={filled_size}")
            avg_price = snapshot.get("avg_price") if snapshot else None
            entry_price = order.get("limit_price")
            price_source = "limit_price"
            if avg_price and 0.01 < avg_price < 0.99:
                entry_price = avg_price
                price_source = "positions_avg"
            if not entry_price:
                ltp = get_last_trade_price(token_id)
                if ltp:
                    entry_price = ltp
                    price_source = "last_trade"
            if not entry_price:
                entry_price = order.get("limit_price", 0.50)

            size = filled_size if filled_size else order.get("requested_size") or order.get("size") or 0

            coin = "BTC" if "btc" in (slug or "").lower() else "ETH"
            confidence = order.get("confidence") or 0
            ev = order.get("ev") or 0

            position = {
                "token_id": token_id,
                "slug": slug,
                "direction": order.get("direction"),
                "entry_price": entry_price,
                "size": size,
                "confidence": confidence,
                "ev": ev,
                "entry_time": datetime.now(timezone.utc).isoformat(),
                "closed": False,
                "pending_order_id": order.get("order_id") or order.get("pending_id"),
                "entry_price_source": price_source,
            }

            entry_details = order.get("entry_details") or {}
            for key_name in ("price_to_beat", "atr", "estimated_value", "diff_in_atr",
                             "base_rate", "p_win_final"):
                if key_name in entry_details:
                    position[key_name] = entry_details[key_name]
            if "atr" in entry_details:
                position["atr_val"] = entry_details["atr"]
            if "price_to_beat" in entry_details:
                position["ptb"] = entry_details["price_to_beat"]
            if "opposite_token_id" in entry_details:
                position["opposite_token_id"] = entry_details["opposite_token_id"]

            os.makedirs("logs", exist_ok=True)
            with open("logs/positions.jsonl", "a") as f:
                f.write(json.dumps(position) + "\n")

            cancel_all_orders(token_id)

            # 挂单成交通知（区分于直接成交的 🎯 通知）
            try:
                wait_sec = int(age) if age else "?"
                msg = (
                    f"⏰ <b>Polymarket 挂单成交</b>\n\n"
                    f"币种: {coin}\n"
                    f"方向: {order.get('direction')}\n"
                    f"置信度: {confidence*100:.0f}%\n"
                    f"EV: {ev:+.3f}\n"
                    f"价格: ${entry_price:.2f} × {size}份 = ${entry_price*size:.2f}\n"
                    f"等待: {wait_sec}s | 来源: {price_source}"
                )
                send_telegram(msg)
                print(f"  📨 reconcile: TG通知已发送 - {coin} {order.get('direction')}")
            except Exception as e:
                print(f"  ❌ reconcile: TG通知失败: {e}")

            _append_pending_update({
                "order_id": order.get("order_id"),
                "pending_id": order.get("pending_id"),
                "slug": slug,
                "token_id": token_id,
                "status": "FILLED",
                "filled_size": size,
                "entry_price": entry_price,
                "entry_price_source": price_source,
                "resolved_at": now.isoformat(),
            })
            continue

        # 未成交，记录调试信息
        print(f"  ⏳ reconcile: {slug} 未成交 filled_size={filled_size} age={int(age) if age else '?'}s")

        if age is not None and age >= PENDING_ORDER_TTL:
            print(f"  ⚠️ reconcile 过期取消: {slug} token={str(token_id)[:10]}... age={int(age)}s")
            cancel_all_orders(token_id)
            _append_pending_update({
                "order_id": order.get("order_id"),
                "pending_id": order.get("pending_id"),
                "slug": slug,
                "token_id": token_id,
                "status": "EXPIRED",
                "resolved_at": now.isoformat(),
            })
            # 过期取消通知
            try:
                coin = "BTC" if "btc" in (slug or "").lower() else "ETH"
                msg = (
                    f"⌛ <b>挂单过期取消</b>\n\n"
                    f"币种: {coin}\n"
                    f"方向: {order.get('direction')}\n"
                    f"限价: ${order.get('limit_price', 0):.2f} × {order.get('requested_size', 0)}份\n"
                    f"等待: {int(age)}s"
                )
                send_telegram(msg)
            except Exception:
                pass

def _looks_like_token_balance_listing(output):
    if not output:
        return False
    lower = output.lower()
    return "balance" in lower and ("token" in lower or "token_id" in lower or "tokenid" in lower)

def _parse_token_balance_from_output(output, token_id):
    if not output:
        return None

    def _coerce(value):
        try:
            return float(value)
        except Exception:
            return None

    def _extract_from_obj(obj):
        if isinstance(obj, dict):
            for key in ("token_id", "tokenId", "id"):
                if obj.get(key) == token_id:
                    for bal_key in ("balance", "amount", "size", "quantity", "qty"):
                        if bal_key in obj:
                            return _coerce(obj[bal_key])
            if token_id in obj:
                return _coerce(obj[token_id])
            for container_key in ("balances", "tokens", "data", "results"):
                if container_key in obj:
                    found = _extract_from_obj(obj[container_key])
                    if found is not None:
                        return found
        elif isinstance(obj, list):
            for item in obj:
                found = _extract_from_obj(item)
                if found is not None:
                    return found
        return None

    try:
        data = json.loads(output)
        found = _extract_from_obj(data)
        if found is not None:
            return found
    except Exception:
        pass

    for line in output.splitlines():
        if token_id not in line:
            continue
        m = re.search(r"(balance|amount|size|qty)\s*[:=]\s*([0-9]*\.?[0-9]+)", line, re.IGNORECASE)
        if m:
            return _coerce(m.group(2))
        after = line.split(token_id, 1)[1]
        m = re.search(r"([0-9]*\.?[0-9]+)", after)
        if m:
            return _coerce(m.group(1))
    return None

def get_token_balance(token_id):
    """查询钱包中指定 conditional token 余额"""
    try:
        cmd = [
            "polymarket", "clob", "balance",
            "--asset-type", "conditional",
            "--token", str(token_id),
            "--signature-type", "eoa",
            "--output", "json",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            data = json.loads(result.stdout)
            bal = data.get("balance")
            if bal is not None:
                return float(bal)
    except Exception:
        pass
    return None

def check_balance_changed(token_id, expected_size):
    """通过查询token余额判断是否成交（余额减少=卖出成功）"""
    try:
        balance = get_token_balance(token_id)
        if balance is not None:
            threshold = max(0.01, expected_size * 0.01)
            return balance <= threshold
    except:
        pass
    return False

def sell_and_confirm(token_id, size, price, timeout_sec=5):
    """挂单并确认成交，未成交则取消
    返回: (success, msg_or_actual_price)
    """
    # 校验链上真实余额
    adjusted = _check_and_adjust_size(token_id, size)
    if adjusted == 0:
        return False, "NO_BALANCE"
    if adjusted is not None:
        size = adjusted

    price = round(price, 2)
    cmd = [
        "polymarket", "clob", "create-order",
        "--signature-type", "eoa",
        "--token", token_id,
        "--side", "sell",
        "--price", str(price),
        "--size", str(size)
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            print(f"    [SELL] create-order failed: rc={result.returncode} stderr={result.stderr.strip()[:200]} stdout={result.stdout.strip()[:200]}")
            return False, result.stderr.strip() or "下单失败"

        info = parse_order_output(result.stdout)
        print(
            f"    [SELL] status={info['status']} matched={info['matched']} "
            f"making={info['making']:.4f} taking={info['taking']:.4f} order_id={info.get('order_id')}"
        )
        if info["matched"]:
            actual_price = round(info["taking"] / size, 4) if size > 0 and info["taking"] > 0 else price
            return True, actual_price
        else:
            raw = (result.stdout or "").strip()
            if raw:
                print(f"    [SELL] raw_output={raw[:400]}")

        # LIVE挂单：等待一小段时间确认成交
        start = time.time()
        while time.time() - start < timeout_sec:
            if check_balance_changed(token_id, size):
                return True, price
            time.sleep(1)

        cancel_all_orders(token_id)
        print(f"    [SELL] not matched after {timeout_sec}s, order cancelled")
        return False, "未成交已取消"
    except Exception as e:
        print(f"    [SELL] exception: {str(e)[:200]}")
        return False, str(e)

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
    print("🔍 持仓监控 v7 启动（早期容忍 + 信号不足退出 + 双曲止盈 + 3阶段平仓）...")
    print("   Exit Protocol: 早期容忍(ATR梯度) | P0双曲止盈(>30s) | EV+ATR持有 | 全程止损")
    print("   容忍窗口: <30s/1ATR, <60s/0.7ATR, <90s/0.5ATR | 阶段2: 120-60s | 阶段3: 60-30s | 阶段4: 30-0s")
    
    close_attempts = {}  # (slug, entry_time) -> attempts count
    stop_loss_attempts = {}  # (slug, entry_time) -> stop loss attempts
    tp_state = {}  # (slug, entry_time) -> "FIRST_TOUCH" | "PULLED_BACK"
    
    while True:
        try:
            reconcile_pending_orders()
            positions = get_open_positions()
            
            if not positions:
                time.sleep(3)
                continue

            open_keys = {(p.get("slug", "unknown"), p.get("entry_time", "")) for p in positions}
            close_attempts = {k: v for k, v in close_attempts.items() if k in open_keys}
            stop_loss_attempts = {k: v for k, v in stop_loss_attempts.items() if k in open_keys}
            tp_state = {k: v for k, v in tp_state.items() if k in open_keys}
            
            for pos in positions:
                token_id = pos["token_id"]
                entry_price = pos["entry_price"]
                size = pos["size"]
                slug = pos.get("slug", "unknown")
                entry_time = pos.get("entry_time", "")
                attempt_key = (slug, entry_time)
                coin = "BTC" if "btc" in slug else "ETH"
                direction = pos.get("direction", "UP")
                
                # 获取当前token价格
                current_price = get_market_price(token_id)
                if current_price is None:
                    continue
                
                profit_rate = (current_price - entry_price) / entry_price if entry_price > 0 else 0
                
                # 获取市场剩余时间
                end_timestamp = resolve_market_end_time(slug, entry_time, pos)
                if not end_timestamp:
                    continue
                
                now = datetime.now(timezone.utc)
                end_time = datetime.fromtimestamp(end_timestamp, tz=timezone.utc)
                remaining = (end_time - now).total_seconds()
                
                # 市场已关闭（remaining <= 0）→ 只做结算/清理
                if remaining <= 0:
                    if remaining < -30:
                        # 自动清理：优先用API查真实结算结果
                        settle_price = get_market_outcome(slug, direction)
                        if settle_price is None:
                            # API无结果，回退Binance价格（可能不准）
                            ptb = pos.get("ptb") or pos.get("price_to_beat")
                            crypto = get_current_crypto_price(coin)
                            if ptb and crypto:
                                won = (direction == "UP" and crypto > ptb) or (direction == "DOWN" and crypto < ptb)
                                settle_price = 1.00 if won else 0.00
                                print(f"  ⚠️ API无outcome，用Binance回退判断(可能不准)")
                            else:
                                settle_price = current_price if current_price else entry_price
                        close_position(pos, settle_price)
                        result_emoji = "🟢" if settle_price > 0.5 else "🔴"
                        print(f"  {result_emoji} 清理过期持仓: {slug} (过期{-remaining:.0f}s) 结算价=${settle_price:.2f}")
                        close_attempts.pop(attempt_key, None)
                        stop_loss_attempts.pop(attempt_key, None)
                        continue

                    # -30s ~ 0s：等待结算并检查市场关闭
                    if int(-remaining) % 10 == 0:
                        print(f"  ⏳ {slug} 已关闭，等待结算 | 过期{-remaining:.0f}s")

                    if check_market_closed(slug):
                        # 优先用API查真实结算结果
                        settle_price = get_market_outcome(slug, direction)
                        if settle_price is None:
                            ptb = pos.get("ptb") or pos.get("price_to_beat")
                            crypto = get_current_crypto_price(coin)
                            if ptb and crypto:
                                won = (direction == "UP" and crypto > ptb) or (direction == "DOWN" and crypto < ptb)
                                settle_price = 1.00 if won else 0.00
                                print(f"  ⚠️ API无outcome，用Binance回退判断(可能不准)")
                            else:
                                settle_price = current_price if current_price else entry_price
                        result_emoji = "🟢" if settle_price > 0.5 else "🔴"
                        print(f"  {result_emoji} {slug} 已关闭 结算价=${settle_price:.2f}")
                        close_position(pos, settle_price)
                        close_attempts.pop(attempt_key, None)
                        stop_loss_attempts.pop(attempt_key, None)
                    continue
                
                # PTB 在下注时已记录，直接从持仓数据读取
                ptb_price = pos.get("ptb") or pos.get("price_to_beat")
                crypto_price = get_current_crypto_price(coin)
                is_losing = is_losing_direction(direction, crypto_price, ptb_price, remaining) if ptb_price and crypto_price else False
                # 只有在 remaining <= 60 且方向正确时才算"必赢"，避免 is_losing_direction 在 >60s 时返回 False 导致误判
                if ptb_price and crypto_price and remaining <= 60:
                    is_winning = (direction == "UP" and crypto_price > ptb_price) or (direction == "DOWN" and crypto_price < ptb_price)
                else:
                    is_winning = False
                
                # 用加密货币价格判断实际方向（不依赖可能失真的 token 价格）
                if ptb_price and crypto_price:
                    direction_correct = (direction == "UP" and crypto_price > ptb_price) or (direction == "DOWN" and crypto_price < ptb_price)
                else:
                    direction_correct = None

                status = "🟢赢" if is_winning else "🔴输" if is_losing else "⚪"
                # 补充显示：用加密货币方向替代可能失真的 token 利润率
                if direction_correct is not None:
                    dir_icon = "✅" if direction_correct else "❌"
                    print(f"  📈 {coin} {direction} | ${entry_price:.2f}→${current_price:.2f} ({profit_rate*100:+.1f}%) | 剩余{remaining:.0f}s | {status} | 方向{dir_icon}")
                else:
                    print(f"  📈 {coin} {direction} | ${entry_price:.2f}→${current_price:.2f} ({profit_rate*100:+.1f}%) | 剩余{remaining:.0f}s | {status}")

                sold = False
                sold_price = 0
                attempted_close = False
                attempted_stop_loss = False
                stop_loss_attempt_recorded = False

                # ═══ 安全检查：token 价格与方向判断矛盾时，信任市场 ═══
                # 场景：direction_correct=True（Binance价格 vs PTB），但 token 价格暴跌
                # 说明 PTB 数据可能有误，或价格在边界反转，市场已 price-in 亏损
                # 早期波动大、CLOB流动性差，需要更宽容的阈值
                # 注意：最后30秒不降级 — CLOB已关闭无法下单，降级只会白费力气
                #       方向正确就持有到结算拿$1
                # 持仓时间保护：入场<60秒CLOB流动性差，token价格波动不代表真实信号
                hold_seconds = 0
                if entry_time:
                    try:
                        entry_dt = datetime.fromisoformat(entry_time)
                        hold_seconds = (datetime.now(timezone.utc) - entry_dt).total_seconds()
                    except (ValueError, TypeError):
                        hold_seconds = 0

                if hold_seconds < 60:
                    pass  # 入场<60秒，跳过降级检查，CLOB流动性差token价格不可靠
                else:
                    drawdown_limit = -0.30 if remaining > 180 else -0.20 if remaining > 120 else -0.15 if remaining > 60 else -0.10
                    if direction_correct and profit_rate < drawdown_limit and remaining > 30:
                        print(f"  ⚠️ 方向✅但token跌{profit_rate*100:.1f}%（阈值{drawdown_limit*100:.0f}%），市场信号矛盾，不再盲目持有")
                        direction_correct = False  # 降级为方向未知，走正常止损流程

                # ═══ EV 持续监控（Exit Protocol）═══
                # 二元市场: token_price ≈ 市场隐含胜率 → EV = token_price - entry_price
                # 这是最直接最准确的EV，不依赖理论模型
                market_ev = current_price - entry_price if current_price else None
                ev_label_global = f"EV={market_ev:+.3f}" if market_ev is not None else "EV=N/A"

                # ═══ P0: 双曲贴现止盈（首次达标+强信号→等$1结算，回调后再达标→立即卖）═══
                profit_threshold = compute_p0_profit_threshold(remaining, P0_BASE_PROFIT, P0_HYPERBOLIC_K, entry_price)

                # 回调检测：利润跌回阈值以下 + 之前是 FIRST_TOUCH → 标记 PULLED_BACK
                if profit_rate < profit_threshold and tp_state.get(attempt_key) == "FIRST_TOUCH":
                    tp_state[attempt_key] = "PULLED_BACK"
                    print(f"  [P0] 回调检测: 利润{profit_rate*100:.1f}%跌破阈值{profit_threshold*100:.1f}%，标记PULLED_BACK | 剩余{remaining:.0f}s")

                if profit_rate >= profit_threshold and remaining > 30:
                    # 判断是否应该跳过本次止盈（首次达标+强信号→等结算）
                    cur_tp_state = tp_state.get(attempt_key)
                    should_skip_tp = False

                    if cur_tp_state is None:
                        # 首次达标：检查是否满足跳过条件
                        atr_val_tp = pos.get("atr_val") or get_atr_from_binance(coin)
                        _, _, diff_atr_tp = calc_realtime_ev(direction, crypto_price, ptb_price, atr_val_tp, entry_price)
                        if direction_correct and diff_atr_tp and diff_atr_tp >= 2.0 and remaining > 60:
                            tp_state[attempt_key] = "FIRST_TOUCH"
                            atr_str_tp = f"{diff_atr_tp:.1f}ATR" if diff_atr_tp else ""
                            print(
                                f"  [P0] 首次达标跳过: {profit_rate*100:.1f}% >= {profit_threshold*100:.1f}% "
                                f"| 方向✅ {atr_str_tp}≥2.0 | 等$1结算 | 剩余{remaining:.0f}s"
                            )
                            should_skip_tp = True
                    elif cur_tp_state == "FIRST_TOUCH":
                        # 还在首次触发区间（没回调过），继续持有
                        print(f"  [P0] 持续持有: 利润{profit_rate*100:.1f}% | 等回调或结算 | 剩余{remaining:.0f}s")
                        should_skip_tp = True

                    # PULLED_BACK 或 首次达标但不满足跳过条件 → 执行止盈
                    if not should_skip_tp:
                        tp_label = "回调后止盈" if cur_tp_state == "PULLED_BACK" else "P0 take-profit"

                        # Executable-price-first: use orderbook depth, fallback to LTP - slippage.
                        exec_price = None
                        exec_source = None

                        liquidity = analyze_liquidity(token_id, size)
                        if liquidity:
                            best_bid = liquidity.get("best_bid")
                            best_bid_size = liquidity.get("best_bid_size", 0)
                            total_liquidity = liquidity.get("total_liquidity", 0)
                            if best_bid:
                                if best_bid_size >= size:
                                    exec_price = best_bid
                                    exec_source = "best_bid"
                                elif total_liquidity >= size:
                                    optimal_price = find_optimal_price(liquidity, size)
                                    if optimal_price:
                                        exec_price = optimal_price
                                        exec_source = "depth"
                                else:
                                    exec_source = "thin_book"

                        if exec_price is None:
                            ltp = get_last_trade_price(token_id)
                            if ltp:
                                exec_price = max(ltp - SLIPPAGE, 0.01)
                                exec_source = "ltp"

                        if exec_price is not None and entry_price > 0:
                            # Floor to 2 decimals to avoid rounding above executable price.
                            exec_price = max(0.01, math.floor(exec_price * 100) / 100.0)
                            exec_profit_rate = (exec_price - entry_price) / entry_price
                            if exec_profit_rate >= profit_threshold:
                                print(
                                    f"  [P0] TP exec ({tp_label}): {exec_profit_rate*100:.1f}% >= {profit_threshold*100:.1f}% "
                                    f"| {ev_label_global} | price ${exec_price:.2f} ({exec_source}) | remaining {remaining:.0f}s"
                                )
                                attempted_stop_loss = True
                                cancel_all_orders(token_id)
                                success, actual = sell_and_confirm(token_id, size, exec_price, timeout_sec=4)
                                if success:
                                    sold = True
                                    sold_price = actual or exec_price
                                    self_notify(pos, sold_price, coin, direction, size, tp_label)
                                    close_position(pos, sold_price)
                                    close_attempts.pop(attempt_key, None)
                                    stop_loss_attempts.pop(attempt_key, None)
                                    tp_state.pop(attempt_key, None)
                                    continue
                                elif actual == "NO_BALANCE":
                                    print(f"  ⚠️ 余额为零，标记持仓关闭等结算")
                                    close_position(pos, current_price or 0)
                                    close_attempts.pop(attempt_key, None)
                                    stop_loss_attempts.pop(attempt_key, None)
                                    tp_state.pop(attempt_key, None)
                                    continue
                            else:
                                print(
                                    f"  [P0] Exec below threshold: {exec_profit_rate*100:.1f}% < {profit_threshold*100:.1f}% "
                                    f"| price ${exec_price:.2f} ({exec_source}) | remaining {remaining:.0f}s"
                                )
                        else:
                            if exec_source == "thin_book":
                                print(f"  [P0] Triggered but book too thin for size | remaining {remaining:.0f}s")
                            else:
                                print(f"  [P0] Triggered but no executable price | remaining {remaining:.0f}s")
                if direction_correct and remaining > 0:
                    atr_val = pos.get("atr_val") or get_atr_from_binance(coin)
                    _, _, diff_atr = calc_realtime_ev(direction, crypto_price, ptb_price, atr_val, entry_price)
                    atr_str = f"{diff_atr:.1f}ATR" if diff_atr else ""

                    # 时间衰减ATR阈值：越接近结算，需要越弱的信号即可持有
                    # 封顶2.0：早期也不应要求不可能达到的ATR倍数
                    atr_hold_threshold = min(1.0 + max(0, remaining - 60) / 120, 2.0)

                    # 市场EV正（token > entry）→ 持有
                    if market_ev is not None and market_ev > 0.03:
                        print(f"  💎 方向正确，持有等结算 | {ev_label_global} {atr_str} | 剩余{remaining:.0f}s")
                        continue
                    elif diff_atr and diff_atr >= atr_hold_threshold:
                        # token略亏但ATR信号强 → 持有
                        print(f"  💎 方向正确，ATR强信号持有 | {ev_label_global} {atr_str}≥{atr_hold_threshold:.1f} | 剩余{remaining:.0f}s")
                        continue

                    # EV不足 + ATR不够强 → 放行到阶段策略
                    print(f"  ⚠️ 方向正确但信号不足({ev_label_global} {atr_str}<{atr_hold_threshold:.1f})，放行到阶段策略 | 剩余{remaining:.0f}s")

                    # ═══ 信号不足早期处理（120s前无阶段策略接管的盲区）═══
                    # 方向错误时 EV 显著为负 且 ATR信号也弱 → 及时止损
                    # 方向正确时跳过：token价格可能滞后于crypto，方向对就不算弱信号
                    # 仅EV微负（-0.01~0）或ATR强（≥1.0）都不应触发退出
                    if not direction_correct and remaining > 120 and market_ev is not None and market_ev < -0.03 \
                            and (not diff_atr or diff_atr < 1.0):
                        best_bid_early = get_best_bid(token_id)
                        if best_bid_early and best_bid_early >= entry_price * 0.90:
                            print(f"  📉 早期信号弱退出: EV={market_ev:+.3f}<0 | bid=${best_bid_early:.3f} | 剩余{remaining:.0f}s")
                            attempted_close = True
                            success, output, actual_price = sell_position(token_id, size, best_bid_early, max_retries=2)
                            if success:
                                sold = True
                                sold_price = actual_price or best_bid_early
                                self_notify(pos, sold_price, coin, direction, size, "信号不足早期退出")
                                close_position(pos, sold_price)
                                close_attempts.pop(attempt_key, None)
                                stop_loss_attempts.pop(attempt_key, None)
                                continue
                        # bid太低不值得卖，继续观望等下轮

                # ═══ 全程方向错误止损（remaining > 0，不限阶段）═══
                # 方向正确的已被前面 direction_correct + EV/ATR continue 跳过
                # 到这里说明：方向错误 / 方向未知 / 方向正确但信号不足
                if should_attempt_stop_loss(direction_correct) and remaining > 30:
                    elapsed = 300 - remaining  # 已持仓时间（秒）

                    # ═══ 早期容忍窗口：偏离小于阈值时不急于止损 ═══
                    # 5分钟市场前1-2分钟波动大，方向可能短暂反转后回来
                    # 梯度容忍：持仓越久容忍越小
                    #   elapsed < 30s  → 容忍 1.0 ATR
                    #   elapsed 30-60s → 容忍 0.7 ATR
                    #   elapsed 60-90s → 容忍 0.5 ATR
                    #   elapsed > 90s  → 不容忍，立即止损
                    atr_val_sl = pos.get("atr_val") or get_atr_from_binance(coin)
                    if atr_val_sl and crypto_price and ptb_price and elapsed < 90:
                        deviation_atr = abs(crypto_price - ptb_price) / atr_val_sl
                        if elapsed < 30:
                            tolerance = 1.0
                        elif elapsed < 60:
                            tolerance = 0.7
                        else:
                            tolerance = 0.5

                        if deviation_atr < tolerance:
                            if close_attempts.get(attempt_key, 0) % 5 == 0:  # 每5轮打一次日志
                                print(f"  ⏸️ 方向反了但偏离小({deviation_atr:.2f}ATR<{tolerance})，观望 | 持仓{elapsed:.0f}s | 剩余{remaining:.0f}s")
                            continue

                    attempts = stop_loss_attempts.get(attempt_key, 0)
                    # 止损用原始best_bid，不打折扣，追求最快成交
                    best_bid = get_best_bid_raw(token_id)

                    # bid 极低（<$0.05）→ 没有买方，尝试P1对冲
                    if best_bid and best_bid < 0.05:
                        opposite_token = pos.get("opposite_token_id")
                        if opposite_token:
                            opposite_ask = get_best_ask(opposite_token)
                            if opposite_ask and opposite_ask < (1.00 - entry_price - 0.02):
                                print(f"  🔄 P1对冲: bid=${best_bid:.3f}<$0.05 | 买反向token @ ${opposite_ask:.3f} | 净成本=${entry_price+opposite_ask:.3f} | 剩余{remaining:.0f}s")
                                h_success, h_output, h_actual = buy_opposite_token(opposite_token, size, opposite_ask)
                                if h_success:
                                    hedge_price = h_actual or opposite_ask
                                    net_settle = 1.00 - entry_price - hedge_price
                                    print(f"  ✅ 对冲成功！结算$1.00 - 入场${entry_price:.3f} - 对冲${hedge_price:.3f} = 净利${net_settle:.3f}")
                                    self_notify(pos, entry_price + net_settle, coin, direction, size, "P1对冲止损")
                                    close_position(pos, entry_price + net_settle)
                                    close_attempts.pop(attempt_key, None)
                                    stop_loss_attempts.pop(attempt_key, None)
                                    sold = True
                                    continue
                                else:
                                    print(f"  ❌ 对冲下单失败，等下轮重试")
                            else:
                                ask_str = f"${opposite_ask:.3f}" if opposite_ask else "N/A"
                                if attempts == 0:
                                    print(f"  🔴 对冲不划算: ask={ask_str} ≥ ${1.00 - entry_price - 0.02:.3f}，等过期结算 | 剩余{remaining:.0f}s")
                        else:
                            if attempts == 0:
                                print(f"  🔴 方向错误但无买方(bid=${best_bid:.3f})且无对冲token，等过期结算 | 剩余{remaining:.0f}s")
                        attempted_stop_loss = True
                        stop_loss_attempt_recorded = True
                        stop_loss_attempts[attempt_key] = attempts + 1
                        continue

                    # 市价止损：传入已有的best_bid避免重复查询
                    print(f"  🛑 方向错误止损(市价): bid=${best_bid:.3f} | 剩余{remaining:.0f}s")
                    attempted_stop_loss = True
                    ok, actual_price = market_sell_immediate(token_id, size, price=best_bid)
                    if ok:
                        sold = True
                        sold_price = actual_price
                        self_notify(pos, sold_price, coin, direction, size, "方向错误止损")
                    elif actual_price == "NO_BALANCE":
                        # 余额为零=token已不在钱包，标记关闭不再重试
                        print(f"  ⚠️ 余额为零，标记持仓关闭等结算")
                        close_position(pos, current_price or 0)
                        close_attempts.pop(attempt_key, None)
                        stop_loss_attempts.pop(attempt_key, None)
                        continue
                    elif attempts >= 3:
                        # 连续3次市价都无买方，放弃止损等结算
                        print(f"  🔴 市价止损{attempts+1}次无买方，放弃等结算 | 剩余{remaining:.0f}s")
                        stop_loss_attempt_recorded = True
                        stop_loss_attempts[attempt_key] = attempts + 1
                        continue

                    if sold:
                        close_position(pos, sold_price)
                        close_attempts.pop(attempt_key, None)
                        continue
                    else:
                        stop_loss_attempt_recorded = True
                        stop_loss_attempts[attempt_key] = attempts + 1
                
                # ═══ 阶段2：结束前120-60秒（流动性下降期）═══
                # 方向错误已被全程止损处理，这里处理信号不足的情况
                if 60 < remaining <= 120:
                    # 方向正确 → 跳过阶段2，等后续阶段处理（阶段3/4有方向保护）
                    if direction_correct:
                        print(f"  💎 阶段2: 方向正确，跳过平仓等后续 | {ev_label_global} | 剩余{remaining:.0f}s")
                        continue
                    print(f"  ⚠️ 阶段2：{ev_label_global} | {'分批挂单' if size > 5 else '挂单确认'}")

                    best_bid = get_best_bid(token_id)
                    if not best_bid or best_bid < 0.02:
                        best_bid = current_price * 0.95 if current_price else 0.10

                    if size <= 5:
                        price = round(best_bid * 0.97, 2)
                        attempted_close = True
                        success, actual = sell_and_confirm(token_id, size, price, timeout_sec=4)
                        if success:
                            sold = True
                            sold_price = actual
                            self_notify(pos, sold_price, coin, direction, size, "阶段2平仓")
                        elif actual == "NO_BALANCE":
                            print(f"  ⚠️ 余额为零，标记持仓关闭等结算")
                            close_position(pos, current_price or 0)
                            close_attempts.pop(attempt_key, None)
                            stop_loss_attempts.pop(attempt_key, None)
                            continue
                        else:
                            price2 = round(best_bid * 0.90, 2)
                            attempted_close = True
                            success2, actual2 = sell_and_confirm(token_id, size, price2, timeout_sec=4)
                            if success2:
                                sold = True
                                sold_price = actual2
                                self_notify(pos, sold_price, coin, direction, size, "阶段2降价平仓")
                    else:
                        attempted_close = True
                        ok, sold_count, avg_price = sell_in_batches(token_id, size, best_bid)
                        if ok and sold_count > 0:
                            sold_price = avg_price or best_bid * 0.97
                            if sold_count >= size:
                                sold = True
                                self_notify(pos, sold_price, coin, direction, sold_count, f"阶段2分批({sold_count}/{size})")
                            else:
                                # 部分成交：更新剩余仓位，继续监控
                                remaining_size = size - sold_count
                                partial_exit = {
                                    "time": datetime.now(timezone.utc).isoformat(),
                                    "price": sold_price,
                                    "size": sold_count,
                                    "label": "阶段2分批"
                                }
                                update_position(pos, new_size=remaining_size, partial_exit=partial_exit)
                                pos["size"] = remaining_size
                                self_notify(pos, sold_price, coin, direction, sold_count, f"阶段2分批({sold_count}/{size})")
                                close_attempts.pop(attempt_key, None)
                                continue
                
                # ═══ 阶段3：结束前60-30秒（流动性枯竭期）═══
                if not sold and 30 < remaining <= 60:
                    # 方向正确 → 跳过阶段3，等阶段4持有到结算拿$1
                    if direction_correct:
                        print(f"  💎 阶段3: 方向正确，跳过平仓等结算 | {ev_label_global} | 剩余{remaining:.0f}s")
                    else:
                        stage_ev = market_ev
                        ev_label = ev_label_global

                        print(f"  🚨 阶段3：平仓 ({ev_label})")

                        # EV > 0（token > entry）→ 设最低价保护，不用地板价
                        min_price = None
                        if stage_ev is not None and stage_ev > 0:
                            min_price = max(entry_price * 0.85, 0.15)
                            print(f"  🛡️ EV正，最低价保护: ${min_price:.2f}")

                        attempted_close = True
                        result = smart_sell_position(token_id, size, is_losing)
                        if result:
                            success, sell_price, output = result
                            if success and (min_price is None or sell_price >= min_price):
                                sold = True
                                sold_price = sell_price
                                self_notify(pos, sold_price, coin, direction, size, "阶段3智能平仓")
                            elif success and min_price and sell_price < min_price:
                                # 已在交易所成交，必须标记关闭，否则产生幽灵持仓
                                sold = True
                                sold_price = sell_price
                                print(f"  🛡️ 成交价${sell_price:.2f}<最低价${min_price:.2f}，已成交标记关闭")
                                self_notify(pos, sold_price, coin, direction, size, "阶段3低价成交")

                        if not sold:
                            # EV > 0 时保护最低价
                            if min_price:
                                best_bid = get_best_bid(token_id)
                                if best_bid and best_bid < min_price:
                                    print(f"  🛡️ 最佳买价${best_bid:.3f}<最低价${min_price:.2f}，跳过市价卖出")
                                else:
                                    attempted_close = True
                                    ok, actual_price = market_sell_immediate(token_id, size, price=best_bid)
                                    if actual_price == "NO_BALANCE":
                                        print(f"  ⚠️ 余额为零，标记持仓关闭等结算")
                                        close_position(pos, current_price or 0)
                                        close_attempts.pop(attempt_key, None)
                                        sold = True
                                    elif ok:
                                        if actual_price >= min_price:
                                            sold = True
                                            sold_price = actual_price
                                            self_notify(pos, sold_price, coin, direction, size, "阶段3市价平仓")
                                        else:
                                            sold = True
                                            sold_price = actual_price
                                            print(f"  🛡️ 市价成交${actual_price:.2f}<最低价${min_price:.2f}，已成交标记关闭")
                                            self_notify(pos, sold_price, coin, direction, size, "阶段3低价成交")
                            else:
                                # 无最低价保护，直接市价
                                attempted_close = True
                                ok, actual_price = market_sell_immediate(token_id, size)
                                if actual_price == "NO_BALANCE":
                                    print(f"  ⚠️ 余额为零，标记持仓关闭等结算")
                                    close_position(pos, current_price or 0)
                                    close_attempts.pop(attempt_key, None)
                                    sold = True
                                elif ok:
                                    sold = True
                                    sold_price = actual_price
                                    self_notify(pos, sold_price, coin, direction, size, "阶段3市价平仓")
                
                # ═══ 阶段4：结束前30秒（最后机会）═══
                if not sold and 0 < remaining <= 30:
                    stage_ev = market_ev
                    ev_label = ev_label_global

                    if stage_ev is not None and stage_ev >= 0:
                        # EV >= 0（token >= entry）→ 持有到结算
                        print(f"  💎 阶段4: {ev_label}>=0，持有到结算")
                    elif direction_correct:
                        # 方向正确（Binance确认）→ 持有等$1结算
                        # 最后30秒CLOB通常已关闭，强行卖出大概率400错误
                        print(f"  💎 阶段4: 方向正确，持有到结算 ({ev_label})")
                    elif remaining <= 10:
                        # 最后10秒 CLOB 基本已关闭，不浪费时间尝试
                        print(f"  ⏳ 阶段4: 剩余{remaining:.0f}s<10s，CLOB已关闭，等待结算 ({ev_label})")
                    else:
                        # EV < 0 + 方向不对 + 还有10-30秒 → 尝试市价
                        print(f"  💀 阶段4：最后机会 ({ev_label})")
                        attempted_close = True
                        ok, actual_price = market_sell_immediate(token_id, size)
                        if actual_price == "NO_BALANCE":
                            print(f"  ⚠️ 余额为零，标记持仓关闭等结算")
                            close_position(pos, current_price or 0)
                            close_attempts.pop(attempt_key, None)
                            sold = True
                        elif ok:
                            sold = True
                            sold_price = actual_price
                            self_notify(pos, sold_price, coin, direction, size, "阶段4兜底")
                
                if sold:
                    close_position(pos, sold_price)
                    close_attempts.pop(attempt_key, None)
                    continue
                
                if attempted_stop_loss and not sold and not stop_loss_attempt_recorded:
                    stop_loss_attempts[attempt_key] = stop_loss_attempts.get(attempt_key, 0) + 1
                    if stop_loss_attempts[attempt_key] % 5 == 0:
                        print(f"  WARN {slug} stop-loss attempts={stop_loss_attempts[attempt_key]}")
                elif attempted_close and not sold:
                    close_attempts[attempt_key] = close_attempts.get(attempt_key, 0) + 1
                    if close_attempts[attempt_key] % 5 == 0:
                        print(f"  ⚠️ {slug} 已尝试{close_attempts[attempt_key]}次未成功")
        
        except Exception as e:
            print(f"❌ 监控错误: {e}")

        time.sleep(1)


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
