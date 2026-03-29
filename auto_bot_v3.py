#!/usr/bin/env python3
"""
Polymarket 全自动交易机器人 v3.3
核心策略：预热观察 → 趋势确认下注 → 实时盯盘平仓

时间线（5分钟=300秒市场，关键秒数可由 .env 覆盖）：
  0s    市场开始
  2s    PTB获取开始（尽早拿到基准价）
  预热开始 / 早期窗口 / 晚期窗口 由 WARMUP_START_SECONDS、
  EARLY_BET_START / EARLY_BET_END、LATE_BET_START / LATE_BET_END 控制
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
import subprocess
import threading
import requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from ai_trader.polymarket_api import normalize_orderbook
from ai_trader.fees import estimate_sell_fill

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

from ai_trader.polymarket_api import get_current_markets, get_price_to_beat_api
from ai_trader import clob_client
from ai_analyze_v2 import analyze_and_decide
from trading_state import should_trade, decrease_cooldown, get_state_summary, record_bet_result, check_daily_loss_limit, record_bet_cost


_ptb_failures = {}  # PTB 接口连续失败计数（按币种独立: {coin: count}）

# ── 网络熔断器 ──
_api_failures = 0          # 连续 API 失败计数
_circuit_open_until = 0    # 熔断恢复时间戳（Unix）
CIRCUIT_BREAK_THRESHOLD = 5   # 连续失败N次触发熔断
CIRCUIT_BREAK_DURATION = 300  # 熔断持续时间（秒）
_runtime_max_reanalyze = None
PTB_FETCH_START_SECONDS = 2
PTB_API_RETRY_ATTEMPTS = 8
PTB_API_RETRY_INTERVAL = 1.0
PTB_API_REQUEST_TIMEOUT = 1.0

# 持仓数检查锁 + 预占计数：防止并行线程同时通过 MAX_OPEN_POSITIONS 检查
_position_lock = threading.Lock()
_pending_bets = 0  # 正在下注中（已通过检查但尚未写入positions.jsonl）
POSITIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "positions.jsonl")


def _slug_end_timestamp(slug, market_seconds=300):
    try:
        return int(str(slug).split("-")[-1]) + market_seconds
    except Exception:
        return None


def _coerce_timestamp(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value) if value > 0 else None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.isdigit():
            return int(raw)
        try:
            return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
        except Exception:
            return None
    return None


def _load_recent_position_records(now_ts=None, grace_seconds=None):
    now_ts = int(now_ts or time.time())
    grace_seconds = grace_seconds or int(os.environ.get("RESTORE_MARKET_GRACE_SECONDS", "180"))
    records = []
    if not os.path.exists(POSITIONS_FILE):
        return records
    try:
        with open(POSITIONS_FILE) as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except Exception:
                    continue
                slug = record.get("slug")
                if not slug:
                    continue
                end_ts = _slug_end_timestamp(slug)
                if end_ts is None:
                    continue
                if end_ts + grace_seconds < now_ts:
                    continue
                if record.get("closed", False):
                    exit_ts = (
                        _coerce_timestamp(record.get("exit_time"))
                        or _coerce_timestamp(record.get("updated_time"))
                        or _coerce_timestamp(record.get("entry_time"))
                    )
                    if exit_ts is not None and exit_ts + grace_seconds < now_ts and end_ts < now_ts:
                        continue
                records.append(record)
    except Exception as e:
        logger.warning(f"♻️ 恢复positions失败: {e}")
    return records


def get_correlated_exposure(direction, coin):
    """P3: 相关性暴露控制 — BTC/ETH 相关性~0.85，同方向持仓需减仓

    读 logs/positions.jsonl 中未关闭持仓，如果已有同方向的 BTC 或 ETH 持仓，
    返回 0.5（Kelly 减半），否则返回 1.0。
    """
    try:
        if not os.path.exists(POSITIONS_FILE):
            return 1.0
        with open(POSITIONS_FILE) as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    p = json.loads(line)
                    if p.get("closed", False):
                        continue
                    # 同方向的任何 crypto 持仓（BTC/ETH 高度相关）
                    if p.get("direction") == direction:
                        return 0.5
                except Exception:
                    continue
    except Exception:
        pass
    return 1.0


def circuit_breaker_check():
    """检查熔断器状态，返回 True 表示可以继续"""
    global _circuit_open_until
    if _circuit_open_until > 0:
        remaining = _circuit_open_until - time.time()
        if remaining > 0:
            logger.warning(f"  ⚡ 熔断中，剩余 {remaining:.0f}s")
            return False
        else:
            _circuit_open_until = 0
            logger.info("  ✅ 熔断恢复")
    return True


def circuit_breaker_record(success):
    """记录 API 调用结果，触发/重置熔断器"""
    global _api_failures, _circuit_open_until
    if success:
        _api_failures = 0
    else:
        _api_failures += 1
        if _api_failures >= CIRCUIT_BREAK_THRESHOLD:
            _circuit_open_until = time.time() + CIRCUIT_BREAK_DURATION
            logger.error(f"  ⚡ 触发熔断！连续{_api_failures}次API失败，暂停{CIRCUIT_BREAK_DURATION}s")
            _api_failures = 0


def _env_int(name, default, minimum=None):
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = int(default)
    return max(minimum, value) if minimum is not None else value


def _env_float(name, default, minimum=None):
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = float(default)
    return max(minimum, value) if minimum is not None else value


def _env_int_alias(primary, fallback, default, minimum=None):
    raw = os.environ.get(primary)
    if raw is None:
        raw = os.environ.get(fallback, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = int(default)
    return max(minimum, value) if minimum is not None else value


def get_strategy_config():
    """读取下注时序和分析阈值配置。"""
    early_bet_start = _env_int("EARLY_BET_START", 90, minimum=0)
    early_bet_end = _env_int("EARLY_BET_END", 95, minimum=early_bet_start)
    late_bet_start = _env_int("LATE_BET_START", 100, minimum=0)
    late_bet_end = _env_int("LATE_BET_END", 160, minimum=late_bet_start)
    entry_reanalyze_interval = _env_int_alias(
        "ENTRY_REANALYZE_INTERVAL", "LATE_REANALYZE_INTERVAL", 20, minimum=1
    )

    return {
        "warmup_start_seconds": _env_int("WARMUP_START_SECONDS", 20, minimum=0),
        "warmup_sample_interval_early": _env_float("WARMUP_SAMPLE_INTERVAL_EARLY", 5, minimum=0.5),
        "warmup_sample_interval_late": _env_float("WARMUP_SAMPLE_INTERVAL_LATE", 3, minimum=0.5),
        "early_min_samples": _env_int("EARLY_MIN_SAMPLES", 4, minimum=1),
        "early_bet_start": early_bet_start,
        "early_bet_end": early_bet_end,
        "late_bet_start": late_bet_start,
        "late_bet_end": late_bet_end,
        "late_min_updates": _env_int("LATE_MIN_UPDATES", 3, minimum=1),
        "late_low_conf_threshold": _env_float("LATE_LOW_CONF_THRESHOLD", 0.15, minimum=0.0),
        "late_gap_cross_allow_conf": _env_float("LATE_GAP_CROSS_ALLOW_CONF", 0.60, minimum=0.0),
        "late_mature_sample_count": _env_int("LATE_MATURE_SAMPLE_COUNT", 8, minimum=1),
        "entry_reanalyze_interval": entry_reanalyze_interval,
        "late_reanalyze_interval": entry_reanalyze_interval,
        "max_trade_retries": _env_int("MAX_TRADE_RETRIES", 3, minimum=1),
    }


def _get_bayesian_summary(updater, remaining_seconds=None):
    """兼容新旧 updater：优先读取富摘要，旧 mock 则回退原始摘要。"""
    if not updater:
        return None

    getter = getattr(updater, "get_summary", None)
    if not getter:
        return None

    try:
        summary = getter(remaining_seconds=remaining_seconds)
    except TypeError:
        summary = getter()

    if not isinstance(summary, dict):
        return None

    return summary


def _get_bayesian_signal(updater, remaining_seconds=None):
    summary = _get_bayesian_summary(updater, remaining_seconds=remaining_seconds)
    if summary:
        return summary

    if not updater:
        return None

    try:
        direction, p_hat, confidence = updater.get_direction_and_confidence()
    except Exception:
        return None

    return {
        "direction": direction,
        "p_hat": p_hat,
        "confidence": confidence,
    }


def _fetch_ptb_api(slug, timeout=None, retry_attempts=None, retry_interval=None):
    """单个 PTB HTTP 获取，主路径走 crypto-price 接口，首轮无值时重复轮询。"""
    timeout = PTB_API_REQUEST_TIMEOUT if timeout is None else timeout
    retry_attempts = PTB_API_RETRY_ATTEMPTS if retry_attempts is None else retry_attempts
    retry_interval = PTB_API_RETRY_INTERVAL if retry_interval is None else retry_interval

    t0 = time.time()
    for attempt in range(retry_attempts):
        ptb = get_price_to_beat_api(slug, timeout=timeout)
        if ptb:
            return ptb, time.time() - t0, "crypto-price"
        if attempt < retry_attempts - 1 and retry_interval > 0:
            time.sleep(retry_interval)
    return None, time.time() - t0, "crypto-price"


def _fetch_ptb_subprocess(slug):
    """单个 PTB Playwright 回退（仅在 API 失败时使用）。"""
    ptb_script = os.path.join(os.path.dirname(__file__), "ai_trader", "playwright_ptb.py")
    t0 = time.time()
    result = subprocess.run(
        [sys.executable, ptb_script, slug],
        capture_output=True, text=True, timeout=20
    )
    elapsed = time.time() - t0
    if result.returncode == 0 and result.stdout:
        import re
        m = re.search(r'PTB=([\d.]+)', result.stdout)
        if m:
            ptb = float(m.group(1))
            if 0.01 < ptb < 10_000_000:
                return ptb, elapsed, "playwright"
    return None, elapsed, "playwright"


def get_ptb_multi_strategy(slug):
    """单个 PTB 获取：优先 crypto-price API，失败时回退 Playwright。"""
    from ai_trader.coins import coin_from_slug
    coin = coin_from_slug(slug)
    failures = _ptb_failures.get(coin, 0)

    if failures >= 3:
        logger.info(f"   跳过 PTB 获取（{coin}连续失败{failures}次）")
        return None

    try:
        ptb, elapsed, source = _fetch_ptb_api(slug)
        if not ptb:
            logger.info(f"   PTB API 暂无值，{coin} 回退 Playwright | api_wait={elapsed:.2f}s")
            ptb, elapsed, source = _fetch_ptb_subprocess(slug)
        if ptb:
            _ptb_failures[coin] = 0
            print(f"✅ PTB={ptb:.2f} ({source}, {elapsed:.2f}s)")
            return ptb
        _ptb_failures[coin] = failures + 1
        if _ptb_failures[coin] >= 3:
            logger.warning(f"   PTB {coin}连续{_ptb_failures[coin]}次失败，后续跳过")
        else:
            logger.warning(f"   PTB 无结果({_ptb_failures[coin]}/3) | {elapsed:.2f}s")
    except (requests.Timeout, subprocess.TimeoutExpired):
        _ptb_failures[coin] = failures + 1
        logger.warning(f"   PTB 请求超时({_ptb_failures[coin]}/3) [{coin}]")
    except Exception as e:
        _ptb_failures[coin] = failures + 1
        logger.warning(f"   PTB 获取失败({_ptb_failures[coin]}/3): {str(e)[:80]}")

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
                    token_ids = json.loads(markets[0].get('clobTokenIds', '[]'))
                    if len(token_ids) >= 2:
                        return str(token_ids[0]), str(token_ids[1])  # UP, DOWN
    except:
        pass
    return None, None


def get_realtime_odds(up_token, down_token):
    """从 WS 或 CLOB 订单簿获取实时数据
    优先用 WS 实时推送（0ms内存读取），无数据时回退 REST。
    """
    from ai_trader.polymarket_ws import poly_ws
    result = {
        "up_mid": None, "down_mid": None,
        "up_bid": None, "down_bid": None,
        "up_ask": None, "down_ask": None,
    }
    for token_id, label in [(up_token, "UP"), (down_token, "DOWN")]:
        prefix = "up" if label == "UP" else "down"

        # 优先用 WS 实时数据（0ms）
        ws_bid, ws_ask = poly_ws.get_best_bid_ask(token_id)
        if ws_bid is not None and ws_ask is not None:
            if ws_bid >= ws_ask:
                logger.warning(f"  ⚠️ {label} WS订单簿倒挂: bid=${ws_bid:.2f} >= ask=${ws_ask:.2f}，丢弃")
            elif ws_bid > 0 and ws_ask > 0:
                result[f"{prefix}_bid"] = round(ws_bid, 4)
                result[f"{prefix}_ask"] = round(ws_ask, 4)
                result[f"{prefix}_mid"] = round((ws_bid + ws_ask) / 2, 4)
            continue

        # WS 无数据，回退 REST（initial_dump修复后应很少触发）
        # v12.8 诊断：打印查询key vs _bba实际key，排查不匹配
        _bba_keys = list(poly_ws._bba.keys())
        _query_key = token_id
        _match = token_id in poly_ws._bba
        logger.info(
            f"  ⚠️ get_realtime_odds: {label} WS无BBA，降级REST | "
            f"query={_query_key[:16]}.. match={_match} | "
            f"bba_keys={[k[:16]+'..' for k in _bba_keys[:4]]}"
        )
        try:
            book = clob_client.get_orderbook(token_id)
            if book:
                raw_bids = [{"price": b.price, "size": b.size} for b in (book.bids or [])]
                raw_asks = [{"price": a.price, "size": a.size} for a in (book.asks or [])]
                bids, asks = normalize_orderbook(raw_bids, raw_asks)
                if bids:
                    result[f"{prefix}_bid"] = round(float(bids[0]["price"]), 4)
                if asks:
                    result[f"{prefix}_ask"] = round(float(asks[0]["price"]), 4)
                if bids and asks:
                    best_bid = float(bids[0]["price"])
                    best_ask = float(asks[0]["price"])
                    if best_bid >= best_ask:
                        logger.warning(f"  ⚠️ {label} 订单簿倒挂: bid=${best_bid:.2f} >= ask=${best_ask:.2f}，丢弃")
                        result[f"{prefix}_bid"] = None
                        result[f"{prefix}_ask"] = None
                        result[f"{prefix}_mid"] = None
                    elif best_bid > 0 and best_ask > 0:
                        result[f"{prefix}_mid"] = round((best_bid + best_ask) / 2, 4)
                elif asks:
                    result[f"{prefix}_mid"] = result[f"{prefix}_ask"]
        except Exception:
            pass
    return result


def send_notification(coin, direction, confidence, ev, price, size, sniper=False, sniper_thread=False, ambush=False):
    """发送下注通知（直接发送Telegram）"""
    if ambush:
        title = "Polymarket 伏击挂单成交"
        icon = "🎯"
        sniper_tag = "\n模式: 限价伏击"
    elif sniper_thread:
        title = "Polymarket 狙击线程成交"
        icon = "🔫"
        sniper_tag = "\n模式: 狙击线程(独立)"
    elif sniper:
        title = "Polymarket 动量狙击成交"
        icon = "⚡"
        sniper_tag = "\n模式: 动量狙击(主线程)"
    else:
        title = "Polymarket 下注成功"
        icon = "🎯"
        sniper_tag = ""
    notify_text = (
        f"{icon} <b>{title}</b>\n\n"
        f"币种: {coin}\n"
        f"方向: {direction}\n"
        f"置信度: {confidence*100:.0f}%\n"
        f"EV: {ev:+.3f}\n"
        f"价格: ${price:.2f} × {size}份 = ${price*size:.2f}"
        f"{sniper_tag}"
    )
    try:
        TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
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
        TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": notify_text, "parse_mode": "HTML"}, timeout=10)
    except:
        pass


def close_position(token_id, size=5, time_remaining=None):
    """
    FOK平仓 — SDK直连，逐级降价
    """
    from py_clob_client.order_builder.constants import SELL

    # 根据剩余时间决定重试次数
    if time_remaining is not None and time_remaining < 20:
        mode = "emergency"
        max_retries = 4
    elif time_remaining is not None and time_remaining < 45:
        mode = "urgent"
        max_retries = 3
    else:
        mode = "normal"
        max_retries = 2

    print(f"  🔔 FOK平仓: {mode} | 剩余: {time_remaining}s | 重试: {max_retries}次")

    for _attempt in range(max_retries):
        # SDK获取最新best_bid
        best_bid = None
        try:
            book = clob_client.get_orderbook(token_id)
            if book and book.bids:
                raw_bids = [{"price": b.price, "size": b.size} for b in book.bids]
                bids, _ = normalize_orderbook(raw_bids, [])
                best_bid = float(bids[0]['price']) if bids else None
        except:
            pass

        # FOK卖价：best_bid减滑点，确保吃到单
        sell_price = round(max(best_bid * 0.95, 0.01), 2) if best_bid else 0.01

        info = clob_client.place_fok_order(token_id, SELL, sell_price, size)
        if info["matched"]:
            gross_price = round(info["taking"] / size, 4) if size > 0 and info["taking"] > 0 else sell_price
            fill_summary = estimate_sell_fill(
                price=gross_price,
                size=size,
                fee_rate_bps=clob_client.get_fee_rate_bps(token_id),
                gross_proceeds=info.get("taking", 0),
            )
            actual = round(fill_summary["net_price"], 4)
            print(f"  ✅ FOK成交！${actual:.3f} | {info.get('elapsed_ms', 0):.0f}ms")
            return True, actual, info["raw"]

        # 未成交 → 降到地板价重试
        if sell_price > 0.02:
            info2 = clob_client.place_fok_order(token_id, SELL, 0.01, size)
            if info2["matched"]:
                gross_price = round(info2["taking"] / size, 4) if size > 0 and info2["taking"] > 0 else 0.01
                fill_summary = estimate_sell_fill(
                    price=gross_price,
                    size=size,
                    fee_rate_bps=clob_client.get_fee_rate_bps(token_id),
                    gross_proceeds=info2.get("taking", 0),
                )
                actual = round(fill_summary["net_price"], 4)
                print(f"  ✅ 地板价成交！${actual:.3f} | {info2.get('elapsed_ms', 0):.0f}ms")
                return True, actual, info2["raw"]

    return False, None, f"FOK failed after {max_retries} attempts"


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
        self.early_analyzed = set()  # 早期窗口分析过但失败的市场，不阻塞晚期窗口
        self.positions = {}  # slug -> Position
        self.warmup_data = {}  # slug -> [{price, direction, timestamp}, ...]
        self.warmup_started = set()  # 已开始预热的市场
        self.bayesian_updaters = {}  # slug -> BayesianUpdater
        self.token_cache = {}  # slug -> (up_token, down_token)  预热阶段缓存token_ids
        self.skipped_markets = {}  # slug -> {skip_time, price_at_skip, reason, reanalyze_count}
        self.trade_attempts = {}  # slug -> 晚期窗口交易尝试次数（FOK失败后允许重试）
        self.last_analysis_time = {}  # slug -> 上次入场分析时间戳（早/晚期窗口冷却用）
        self._ptb_pending = {}  # slug -> coin, 待并行获取PTB的市场
        self.restored_slugs = set()
        # ═══ 独立狙击监听线程状态 ═══
        self._sniper_running = False
        self._sniper_thread = None
        self._sniper_lock = threading.Lock()       # 防止同一slug被主线程和狙击线程同时执行
        self._sniper_processing = set()            # 狙击线程正在处理的slug
        self._sniper_last_reason = {}              # slug -> last_reason, 去重日志用
        self._sniper_updaters = {}                 # slug -> 独立BayesianUpdater（100ms自更新）
        self._sniper_epoch_used = set()            # v12.7: 已狙击的epoch(如"1774490400")，同epoch只狙1次
        self._sniper_rest_cache = {}               # v12.7.2: token_id -> (ask, ts) REST校正缓存，500ms内不重复请求
        self._cached_balance = None               # v12.9: 预缓存余额，狙击线程Kelly用（每周期刷新）
        self._ambush_orders = {}                   # v12.9: slug -> {token, direction, order_id, price, size, bal_before, ...}
        self._ambush_cancelled_at = {}             # v12.9.1: slug -> timestamp, 撤单冷却防循环
        self._restore_recent_market_state()

    def _restore_recent_market_state(self):
        restored_open = 0
        restored_recent = 0
        seen_open = set()
        seen_recent = set()

        for record in _load_recent_position_records():
            slug = record.get("slug")
            if not slug:
                continue
            self.analyzed.add(slug)
            self.restored_slugs.add(slug)

            if record.get("closed", False):
                if slug not in seen_recent:
                    restored_recent += 1
                    seen_recent.add(slug)
                continue

            if slug in seen_open:
                continue
            seen_open.add(slug)
            restored_open += 1

            token_id = record.get("token_id")
            direction = record.get("direction", "UP")
            entry_price = float(record.get("entry_price", 0) or 0)
            size = float(record.get("token_balance") or record.get("size") or 0)
            entry_time = record.get("entry_time") or datetime.now(timezone.utc).isoformat()

            if token_id:
                self.positions[slug] = Position(
                    slug, token_id, direction, entry_price, size, entry_time
                )

        if restored_open or restored_recent:
            logger.info(f"♻️ 启动恢复: open={restored_open} | recent_closed={restored_recent}")

    # ═══════════════════════════════════════════════════════════
    # 独立狙击监听线程 — 100ms高频轮询，独立贝叶斯，PTB就绪200ms内可狙击
    # ═══════════════════════════════════════════════════════════

    def start_sniper_thread(self):
        """启动独立狙击监听线程"""
        if self._sniper_running:
            return
        self._sniper_running = True
        self._sniper_thread = threading.Thread(
            target=self._sniper_loop, daemon=True, name="sniper-thread"
        )
        self._sniper_thread.start()
        logger.info("[狙] 独立狙击监听线程已启动")

    def stop_sniper_thread(self):
        """停止狙击线程"""
        self._sniper_running = False

    def _sniper_loop(self):
        """狙击线程主循环 — 事件驱动 + 兜底轮询

        v12.8: 优先等待 WS 推送事件(~0ms age)，超时后兜底轮询(50ms)。
        WS 活跃时 age < 1ms，比固定 sleep(50ms) 快 50 倍。
        """
        from ai_trader.polymarket_ws import poly_ws
        POLL_MS = int(os.environ.get("SNIPER_POLL_MS", "50"))  # 兜底轮询间隔
        poll_interval = POLL_MS / 1000.0

        # v12.9: 预取余额（线程启动时，在等待市场数据前完成，零热路径延迟）
        try:
            self._cached_balance = clob_client.get_balance()
            logger.info(f"[狙] 余额预缓存: ${self._cached_balance:.2f}")
        except Exception:
            pass

        while self._sniper_running:
            try:
                # 事件驱动：等 WS 推送，最多等 poll_interval 毫秒
                # 收到推送 → 立即扫描(age ~0ms)  超时 → 兜底扫描
                poly_ws.wait_for_update(timeout=poll_interval)
                self._sniper_scan()
            except Exception as e:
                logger.debug(f"  🔇 狙击线程异常: {e}")
                time.sleep(poll_interval)

    def _manage_ambush(self, slug, coin, tokens, ptb, atr_val, remaining, end_buffer):
        """v12.9.1: 方向确认型伏击 — 管理已有伏击单的成交检测和清理

        放置逻辑在 _place_directional_ambush()，由 _sniper_scan 的 ask>MAX_PRICE 路径触发。
        """
        AMBUSH_WINDOW_END = float(os.environ.get("SNIPER_AMBUSH_END", "30"))
        AMBUSH_BAL_INTERVAL = float(os.environ.get("SNIPER_AMBUSH_BAL_INTERVAL", "0.5"))
        AMBUSH_REPRICE_INTERVAL = float(os.environ.get("SNIPER_AMBUSH_REPRICE_INTERVAL", "0.2"))

        ambush = self._ambush_orders.get(slug)

        if ambush:
            _now = time.time()
            _token = ambush["token"]
            _should_cleanup = (remaining <= AMBUSH_WINDOW_END or slug in self.positions or slug in self.analyzed)

            # ── 层3: WS 快速路径（~0ms） ──
            _ws_fill = False
            try:
                from ai_trader.user_ws import user_trade_stream
                for _oid_check in ambush.get("all_order_ids", []):
                    if _oid_check and user_trade_stream.get_latest_trade(_oid_check):
                        _ws_fill = True
                        break
            except Exception:
                pass

            # ── 层1: 余额检查（500ms 周期） ──
            _should_check_bal = _should_cleanup or _ws_fill or (_now - ambush.get("last_bal_check", 0) >= AMBUSH_BAL_INTERVAL)
            _filled = 0
            if _should_check_bal:
                ambush["last_bal_check"] = _now
                _bal = clob_client.get_token_balance(_token) or 0
                _filled = _bal - ambush["bal_before"]
                _check_age = round(_now - ambush.get("placed_at", _now), 1)
                logger.info(
                f"  [伏] 检查: {coin} {ambush['direction']} @${ambush['price']:.2f} "
                f"| bal={_bal:.2f} before={ambush['bal_before']:.2f} "
                f"{'WS触发' if _ws_fill else '轮询'} "
                f"| 已挂{_check_age}s 追价{ambush.get('reprice_count',0)}次")

            if _filled > 0.5:
                # v12.9.6: 全量入仓 — 不管是哪个追价单成交的，余额增多少就入多少
                filled_size = round(_filled, 4)
                # 立即清掉该token所有残留挂单（防止后续继续成交）
                clob_client.cancel_all(_token)
                clob_client.cancel_orders_batch(ambush.get("all_order_ids", []))
                entry_price = ambush["price"]  # 用最新挂单价（近似）
                cost = round(entry_price * filled_size, 4)
                _dir = ambush["direction"]
                _opp = ambush["opposite_token"]
                if filled_size > ambush.get("size", 5) * 1.5:
                    logger.warning(
                        f"  [伏] 多单成交: 预期{ambush.get('size',0):.1f}份 实际{filled_size:.1f}份 "
                        f"(追价残留成交，全量入仓)")

                # ── v12.9.4: 成交后方向校验 — 方向翻转或信号消失则立即卖出 ──
                AMBUSH_FILL_MIN_CONF = float(os.environ.get("SNIPER_AMBUSH_FILL_MIN_CONF", "0.15"))
                _updater = self._sniper_updaters.get(slug)
                _dir_ok = True
                _fill_conf = 0
                _fill_dir = _dir
                if _updater and _updater.n_updates >= 3:
                    _fill_sig = _get_bayesian_signal(_updater, remaining_seconds=remaining)
                    if _fill_sig:
                        _fill_dir = _fill_sig.get("direction")
                        _fill_conf = _fill_sig.get("confidence", 0)
                        if _fill_dir != _dir:
                            _dir_ok = False  # 方向翻转
                            logger.info(
                                f"  [伏] 方向校验: {coin} 挂单={_dir} 当前={_fill_dir} "
                                f"conf={_fill_conf:.0%} → ❌ 方向翻转")
                        elif _fill_conf < AMBUSH_FILL_MIN_CONF:
                            _dir_ok = False  # 信号消失
                            logger.info(
                                f"  [伏] 方向校验: {coin} 方向={_dir}✅ "
                                f"conf={_fill_conf:.0%}<{AMBUSH_FILL_MIN_CONF:.0%} → ❌ 信号消失")
                        else:
                            logger.info(
                                f"  [伏] 方向校验: {coin} 方向={_dir}✅ "
                                f"conf={_fill_conf:.0%}≥{AMBUSH_FILL_MIN_CONF:.0%} → ✅ 建仓")
                    else:
                        logger.info(f"  [伏] 方向校验: {coin} 无贝叶斯信号 → ✅ 默认建仓")
                else:
                    logger.info(
                        f"  [伏] 方向校验: {coin} updater不足("
                        f"{'无' if not _updater else f'n={_updater.n_updates}'}) → ✅ 默认建仓")

                if not _dir_ok:
                    # 方向翻转/信号消失 → 清场 + 查实际余额 + 全部卖出
                    clob_client.cancel_all(_token)  # 先撤所有残留挂单
                    clob_client.cancel_orders_batch(ambush.get("all_order_ids", []))
                    # 等200ms让撤单生效，再查真实余额
                    time.sleep(0.05)
                    _real_bal = clob_client.get_token_balance(_token) or 0
                    _sell_size = _real_bal if _real_bal > 0.5 else filled_size
                    logger.info(
                        f"\n⚠️ 伏击成交但方向失效! {coin} {_dir} "
                        f"conf={_fill_conf:.0%} → 立即卖出 {_sell_size:.1f}份 (实际余额{_real_bal:.1f})")
                    try:
                        clob_client.update_token_allowance(_token)
                        from py_clob_client.order_builder.constants import SELL
                        _sell_result = clob_client.place_fok_order(
                            _token, SELL, 0.01, _sell_size)
                        # making=maker成交额(USDC), taking=taker成交额(USDC)
                        _sell_usdc = (_sell_result.get("making", 0) or 0) + (_sell_result.get("taking", 0) or 0)
                        _sell_price_avg = round(_sell_usdc / _sell_size, 4) if _sell_size > 0 else 0
                        if _sell_result.get("matched"):
                            _loss = round(cost - _sell_usdc, 2)
                            logger.info(f"  ⚡ 即时卖出成功: ${_sell_price_avg:.3f}×{_sell_size:.1f}=${_sell_usdc:.2f} 亏${_loss:.2f}")
                            send_notification(coin, _dir, _fill_conf, 0,
                                              _sell_price_avg, _sell_size, ambush=True)
                        else:
                            logger.warning(f"  ⚡ 即时卖出未成交: {_sell_result.get('status')}")
                    except Exception as _e:
                        logger.warning(f"  ⚡ 即时卖出异常: {_e}")
                    self.analyzed.add(slug)
                    self._ambush_orders.pop(slug, None)
                    return

                # 方向确认OK → 清场 + 查实际余额 → 全量建仓
                clob_client.cancel_all(_token)
                clob_client.cancel_orders_batch(ambush.get("all_order_ids", []))
                time.sleep(0.05)
                _real_bal_ok = clob_client.get_token_balance(_token) or 0
                if _real_bal_ok > filled_size + 0.5:
                    logger.info(f"  [伏] 实际余额{_real_bal_ok:.1f} > 检测{filled_size:.1f}，用实际余额建仓")
                    filled_size = round(_real_bal_ok, 4)
                    cost = round(entry_price * filled_size, 4)
                _entry_ts = datetime.now(timezone.utc).isoformat()
                self.positions[slug] = Position(slug, _token, _dir, entry_price, filled_size, _entry_ts)
                self.positions[slug].details = {
                    "sniper": True, "ambush": True,
                    "opposite_token_id": _opp,
                    "atr_val": atr_val, "ptb": ptb,
                }
                self.analyzed.add(slug)
                record_bet_cost(slug, cost)

                # 写 positions.jsonl
                import fcntl
                _pos_record = {
                    "token_id": _token, "slug": slug,
                    "direction": _dir, "entry_price": entry_price,
                    "size": filled_size, "entry_cost": cost,
                    "entry_time": _entry_ts,
                    "price_to_beat": ptb, "atr": atr_val,
                    "opposite_token_id": _opp,
                    "sniper_thread": True, "ambush": True,
                }
                try:
                    _lock_fd = open(POSITIONS_FILE + ".lock", "w")
                    fcntl.flock(_lock_fd, fcntl.LOCK_EX)
                    with open(POSITIONS_FILE, "a") as _pf:
                        _pf.write(json.dumps(_pos_record) + "\n")
                    fcntl.flock(_lock_fd, fcntl.LOCK_UN)
                    _lock_fd.close()
                except Exception as _e:
                    logger.warning(f"  [伏] 写入positions.jsonl失败: {_e}")
                try:
                    clob_client.update_token_allowance(_token)
                except Exception:
                    pass

                logger.info(
                    f"\n🎯 伏击成交! {coin} {_dir} ${entry_price:.2f}×{filled_size:.1f}份 "
                    f"cost=${cost:.2f} (方向✅ conf={_fill_conf:.0%})")
                send_notification(coin, _dir, max(_fill_conf, ambush.get("confidence", 0.5)),
                                  0, entry_price, filled_size, ambush=True)
                self._ambush_orders.pop(slug, None)
            elif _should_cleanup:
                # 批量撤全部历史挂单（1次API）+ cancel_all兜底
                clob_client.cancel_orders_batch(ambush.get("all_order_ids", []))
                clob_client.cancel_all(_token)  # 兜底：撤该token所有残留
                self._ambush_orders.pop(slug, None)
                self._ambush_cancelled_at[slug] = time.time()
                logger.info(f"  [伏] 撤销伏击单: {coin} {ambush['direction']} (剩余{remaining:.0f}s)")
            # ── 层2: 追价（200ms 周期，独立于余额检查） ──
            if not _should_cleanup and _filled <= 0.5:
                _should_reprice = (_now - ambush.get("last_reprice", 0) >= AMBUSH_REPRICE_INTERVAL)
                if not _should_reprice:
                    return
                ambush["last_reprice"] = _now
                AMBUSH_REPRICE_MAX = int(os.environ.get("SNIPER_AMBUSH_REPRICE_MAX", "5"))
                _reprice_count = ambush.get("reprice_count", 0)
                if _reprice_count < AMBUSH_REPRICE_MAX:
                    _updater_rp = self._sniper_updaters.get(slug)
                    if not (_updater_rp and _updater_rp.n_updates >= 3 and atr_val and atr_val > 0):
                        logger.info(
                            f"  [伏] 追价跳过: updater={'有' if _updater_rp else '无'} "
                            f"n={_updater_rp.n_updates if _updater_rp else 0} "
                            f"atr_val={atr_val}")
                    if _updater_rp and _updater_rp.n_updates >= 3 and atr_val and atr_val > 0:
                        from ai_analyze_v2 import _random_walk_p_win
                        _cur_price_rp = _updater_rp.current_price
                        if _cur_price_rp and ptb:
                            _gap_rp = abs(_cur_price_rp - ptb)
                            _diff_atr_rp = _gap_rp / atr_val
                            _rw_rp = _random_walk_p_win(_gap_rp, atr_val, remaining)
                            _sig_rp = _get_bayesian_signal(_updater_rp, remaining_seconds=remaining)
                            _conf_rp = _sig_rp.get("confidence", 0) if _sig_rp else 0
                            _p_win_rp = 0.5 + (max(_rw_rp, _conf_rp * 0.5 + 0.5) - 0.5) * 0.60
                            AMBUSH_EDGE = float(os.environ.get("SNIPER_AMBUSH_EDGE", "0.045"))
                            AMBUSH_MIN_P = float(os.environ.get("SNIPER_AMBUSH_MIN_PRICE", "0.45"))
                            AMBUSH_MAX_P = float(os.environ.get("SNIPER_AMBUSH_MAX_PRICE", "0.65"))
                            _new_price = round((_p_win_rp - AMBUSH_EDGE) / 0.01) * 0.01
                            _new_price = max(AMBUSH_MIN_P, min(AMBUSH_MAX_P, _new_price))
                            _old_price = ambush["price"]
                            _old_oid = ambush.get("order_id")

                            AMBUSH_REPRICE_MIN_DELTA = float(
                                os.environ.get("SNIPER_AMBUSH_REPRICE_MIN_DELTA", "0.02")
                            )
                            _price_delta = abs(_new_price - _old_price)
                            if _price_delta < AMBUSH_REPRICE_MIN_DELTA:
                                logger.info(
                                    f"  [伏] 价格稳定: {coin} ${_old_price:.2f}→${_new_price:.2f} "
                                    f"(差${_price_delta:.2f}<${AMBUSH_REPRICE_MIN_DELTA:.2f} 不追价)")

                            if _price_delta >= AMBUSH_REPRICE_MIN_DELTA:
                                # GTD追价：旧单会自动过期（由 SNIPER_AMBUSH_GTD_REPRICE_SEC 控制），不需要手动撤
                                # 仍然尝试批量撤加速（但不依赖它成功）
                                _old_oids = [oid for oid in dict.fromkeys(ambush.get("all_order_ids", [])) if oid]
                                if _old_oid and _old_oid not in _old_oids:
                                    _old_oids.append(_old_oid)
                                ambush["all_order_ids"] = _old_oids[:]
                                clob_client.cancel_orders_batch(_old_oids)

                                from py_clob_client.order_builder.constants import BUY
                                from py_clob_client.clob_types import OrderType as _OT
                                GTD_REPRICE_SEC = int(os.environ.get("SNIPER_AMBUSH_GTD_REPRICE_SEC", "4"))
                                _gtd_exp_rp = int(time.time()) + 60 + GTD_REPRICE_SEC  # +60 Polymarket安全阈值
                                _new_result = clob_client.place_order(
                                    _token, BUY, _new_price, ambush["size"],
                                    order_type=_OT.GTD, expiration=_gtd_exp_rp)
                                _new_oid = _new_result.get("order_id")
                                if _new_oid or _new_result.get("matched"):
                                    _tracked_oids = [oid for oid in dict.fromkeys(_old_oids) if oid]
                                    if _new_oid and _new_oid not in _tracked_oids:
                                        _tracked_oids.append(_new_oid)
                                    ambush["order_id"] = _new_oid
                                    ambush["all_order_ids"] = _tracked_oids
                                    ambush["price"] = _new_price
                                    ambush["reprice_count"] = _reprice_count + 1
                                    if _new_result.get("matched"):
                                        # 追价即时成交 → 立即清场，触发下一轮的成交检测
                                        clob_client.cancel_all(_token)  # 撤所有残留
                                        logger.info(f"  [伏] 追价即时成交: ${_new_price} (撤全部残留+旧单)")
                                        return  # 下一轮 _manage_ambush 会通过余额检查发现成交
                                    else:
                                        logger.info(
                                            f"  [伏] 追价: {coin} {ambush['direction']} "
                                            f"${_old_price:.2f}→${_new_price:.2f} "
                                            f"(p_win={_p_win_rp:.3f} ATR={_diff_atr_rp:.2f} "
                                            f"撤{len(_old_oids)}旧单 TTL={GTD_REPRICE_SEC}s "
                                            f"跟踪{len(_tracked_oids)}单 第{_reprice_count+1}/{AMBUSH_REPRICE_MAX}次)")
            return

        # 注意：放置逻辑已移到 _sniper_scan 的 "ask太贵" 路径，不在这里盲挂

    def _place_directional_ambush(self, slug, coin, tokens, gap_dir, confidence,
                                  diff_atr, ptb, atr_val, remaining):
        """在确认方向上放置伏击限价单（由 _sniper_scan 的 ask>MAX_PRICE 路径调用）"""
        if slug in self._ambush_orders or slug in self.positions or slug in self.analyzed:
            return
        # ★ 防止在即将撤单的时间窗口内下单（否则下→撤→下循环）
        AMBUSH_WINDOW_END = float(os.environ.get("SNIPER_AMBUSH_END", "30"))
        if remaining <= AMBUSH_WINDOW_END + 5:  # +5s安全余量
            return
        # ★ 冷却：刚撤过的slug短期不再下单
        _last_cancel = self._ambush_cancelled_at.get(slug, 0)
        if time.time() - _last_cancel < 30:
            return

        AMBUSH_MIN_ATR = float(os.environ.get("SNIPER_AMBUSH_MIN_ATR", "0.8"))
        AMBUSH_MIN_ATR_ABS = float(os.environ.get("SNIPER_AMBUSH_MIN_ATR_ABS", "30"))
        AMBUSH_MIN_CONF = float(os.environ.get("SNIPER_AMBUSH_MIN_CONF", "0.35"))
        AMBUSH_MIN_PRICE = float(os.environ.get("SNIPER_AMBUSH_MIN_PRICE", "0.45"))
        AMBUSH_MAX_PRICE = float(os.environ.get("SNIPER_AMBUSH_MAX_PRICE", "0.65"))
        AMBUSH_EDGE = float(os.environ.get("SNIPER_AMBUSH_EDGE", "0.045"))  # min edge + fees

        # 方向信号够强 + 市场够活跃才埋单
        if diff_atr < AMBUSH_MIN_ATR or confidence < AMBUSH_MIN_CONF or atr_val < AMBUSH_MIN_ATR_ABS:
            logger.info(
                f"  [伏] 信号不足跳过: {coin} {gap_dir} "
                f"diff_atr={diff_atr:.2f}{'<'+str(AMBUSH_MIN_ATR) if diff_atr<AMBUSH_MIN_ATR else '✓'} "
                f"atr_val=${atr_val:.0f}{'<$'+str(int(AMBUSH_MIN_ATR_ABS)) if atr_val<AMBUSH_MIN_ATR_ABS else '✓'} "
                f"conf={confidence:.0%}{'<'+str(AMBUSH_MIN_CONF) if confidence<AMBUSH_MIN_CONF else '✓'}")
            return

        token = tokens[0] if gap_dir == "UP" else tokens[1]
        opposite = tokens[1] if gap_dir == "UP" else tokens[0]

        # v12.9.4: 动态定价 — fair_value 减去利润空间
        from ai_analyze_v2 import _random_walk_p_win
        from ai_trader.base_rate import get_base_rate
        _rw = _random_walk_p_win(diff_atr * atr_val, atr_val, remaining)
        _p_win = 0.5 + (max(_rw, confidence * 0.5 + 0.5) - 0.5) * 0.60  # 保守 shrinkage
        AMBUSH_PRICE = round((_p_win - AMBUSH_EDGE) / 0.01) * 0.01  # tick 对齐
        AMBUSH_PRICE = max(AMBUSH_MIN_PRICE, min(AMBUSH_MAX_PRICE, AMBUSH_PRICE))

        # v12.9.11: CLOB感知定价 — 当市场价远高于模型价时，参考ask下方挂单
        AMBUSH_ASK_OFFSET = float(os.environ.get("SNIPER_AMBUSH_ASK_OFFSET", "0.05"))
        AMBUSH_MAX_GAP = float(os.environ.get("SNIPER_AMBUSH_MAX_GAP", "0.15"))
        from ai_trader.polymarket_ws import poly_ws as _ambush_ws
        _, _real_ask = _ambush_ws.get_best_bid_ask(token)
        if _real_ask and 0.01 < _real_ask < 0.99:
            _gap_to_market = _real_ask - AMBUSH_PRICE
            if _gap_to_market > AMBUSH_MAX_GAP:
                # 市场比模型贵太多，放弃挂单（几乎不可能成交）
                logger.info(
                    f"  [伏] 市场价过远跳过: {coin} {gap_dir} "
                    f"model=${AMBUSH_PRICE:.2f} ask=${_real_ask:.2f} "
                    f"gap=${_gap_to_market:.2f}>${AMBUSH_MAX_GAP} → 不挂单")
                return
            elif _gap_to_market > AMBUSH_ASK_OFFSET:
                # 市场略贵，提价到 ask 下方 offset 处
                _market_price = round(_real_ask - AMBUSH_ASK_OFFSET, 2)
                _market_price = max(AMBUSH_MIN_PRICE, min(AMBUSH_MAX_PRICE, _market_price))
                logger.info(
                    f"  [伏] CLOB感知提价: {coin} {gap_dir} "
                    f"model=${AMBUSH_PRICE:.2f} ask=${_real_ask:.2f} "
                    f"→ ${_market_price:.2f} (ask-{AMBUSH_ASK_OFFSET})")
                AMBUSH_PRICE = _market_price

        # Kelly 仓位计算
        _br = get_base_rate(diff_atr)
        _kr = 0.5 if _br < 0.55 else 1.0
        _bal = self._cached_balance if self._cached_balance and self._cached_balance > 0 else 20.0
        _f_star = (_p_win - AMBUSH_PRICE) / (1.0 - AMBUSH_PRICE) if _p_win > AMBUSH_PRICE else 0
        _f_quarter = _f_star / 4.0 * _kr
        _dollar_amount = _bal * _f_quarter
        MIN_BET = float(os.environ.get("MIN_BET_SIZE", "2"))
        MAX_BET = float(os.environ.get("MAX_BET_SIZE", "3"))
        _net_size = round(_dollar_amount / AMBUSH_PRICE, 1) if AMBUSH_PRICE > 0 else 0
        _net_size = max(MIN_BET, min(MAX_BET, _net_size))
        ambush_size = round(_net_size / 0.975, 2)  # gross = net / (1-fee)

        if _f_star <= 0:
            return  # Kelly 说不值得

        bal_before = clob_client.get_token_balance(token) or 0

        # GTD: 市场结束前30s自动过期（不需要手动撤单）
        from py_clob_client.order_builder.constants import BUY
        from py_clob_client.clob_types import OrderType as _OT
        AMBUSH_END_SEC = float(os.environ.get("SNIPER_AMBUSH_END", "30"))
        _market = self.tracked.get(slug, {})
        _end_str = _market.get("end_time", "")
        _gtd_exp = 0
        if _end_str:
            _end_dt = datetime.fromisoformat(_end_str.replace("Z", "+00:00"))
            _gtd_exp = int(_end_dt.timestamp()) - int(AMBUSH_END_SEC) + 60  # +60 = Polymarket安全阈值
        result = clob_client.place_order(token, BUY, AMBUSH_PRICE, ambush_size,
                                          order_type=_OT.GTD, expiration=_gtd_exp)
        oid = result.get("order_id")

        if oid:
            self._ambush_orders[slug] = {
                "token": token, "opposite_token": opposite,
                "up_token": tokens[0], "down_token": tokens[1],
                "direction": gap_dir, "confidence": confidence,
                "order_id": oid, "all_order_ids": [oid],
                "price": AMBUSH_PRICE, "size": ambush_size,
                "bal_before": bal_before,
                "placed_at": time.time(), "last_check": time.time(),
            }
            logger.info(
                f"  🎯 伏击挂单: {coin} {gap_dir} @${AMBUSH_PRICE} ×{ambush_size}份 "
                f"| ATR={diff_atr:.2f} conf={confidence:.0%} p_win={_p_win:.3f} "
                f"f*={_f_star:.3f} kelly={_kr} bal=${_bal:.0f} remaining={remaining:.0f}s"
            )
            # Subscribe User WS for instant fill detection
            try:
                from ai_trader.user_ws import user_trade_stream
                _market_info = self.tracked.get(slug, {})
                # condition_id from slug or market info
                user_trade_stream.start()
            except Exception:
                pass

            # 立即成交检查
            if result.get("matched"):
                filled_size = result.get("taking", ambush_size)
                logger.info(f"  🎯 伏击立即成交! {coin} {gap_dir} ${AMBUSH_PRICE}×{filled_size}")
                # 触发下一轮 _manage_ambush 的成交检测流程
        elif result.get("error"):
            logger.debug(f"  [伏] 挂单失败: {coin} {gap_dir} {str(result.get('error',''))[:50]}")

    def _sniper_scan(self):
        """单次狙击扫描 — 遍历所有活跃市场检测狙击机会

        读取 Chainlink/Binance WS 内存价格（0ms），
        计算 gap/ATR → 条件通过则获取CLOB价格 → EV正则直接下单。
        """
        from ai_trader.polymarket_rtds import chainlink_stream
        from ai_trader.binance_api import price_stream as binance_stream

        now = datetime.now(timezone.utc)
        strategy_cfg = get_strategy_config()
        SNIPER_MIN_ATR = float(os.environ.get("SNIPER_MIN_ATR", "0.5"))
        SNIPER_MAX_PRICE = float(os.environ.get("SNIPER_MAX_PRICE", "0.65"))
        SNIPER_MIN_CONF = float(os.environ.get("SNIPER_MIN_CONF", "0.25"))
        SNIPER_MIN_EV = float(os.environ.get("SNIPER_MIN_EV", "0.05"))          # v12.7: 0.01→0.05 配合费用扣除
        SNIPER_P_WIN_SHRINKAGE = float(os.environ.get("SNIPER_P_WIN_SHRINKAGE", "0.60"))  # v12.7: 独立shrinkage(实盘p_win高估27pp)
        SNIPER_MIN_UPDATES = int(os.environ.get("SNIPER_MIN_UPDATES", "4"))     # v12.7: 2→4 多观察200ms减噪
        SNIPER_MAX_PER_EPOCH = int(os.environ.get("SNIPER_MAX_PER_EPOCH", "1")) # v12.7: 同epoch最多狙击N个市场
        _sniper_end_buffer = int(os.environ.get("SNIPER_END_BUFFER", "5"))   # v12.8: 结束前N秒停止狙击

        for slug, market in list(self.tracked.items()):
            # ── 快速跳过：已分析/已持仓/正在狙击中 ──
            if slug in self.analyzed or slug in self.positions:
                self._sniper_last_reason.pop(slug, None)
                continue
            with self._sniper_lock:
                if slug in self._sniper_processing:
                    continue

            coin = market["coin"]

            # 去重日志辅助：同一slug同一原因只打印一次
            def _log_skip(reason_key, msg):
                prev = self._sniper_last_reason.get(slug)
                if prev != reason_key:
                    self._sniper_last_reason[slug] = reason_key
                    logger.info(msg)

            # ── 必要数据就绪检查 ──
            ptb = self.ptb_cache.get(slug)
            shared_updater = self.bayesian_updaters.get(slug)
            atr_val = shared_updater.atr_val if shared_updater else None
            # v12.9.8: 实时ATR — 用sniper updater样本估算，取max(静态, 实时)
            _sniper_upd = self._sniper_updaters.get(slug)
            if _sniper_upd and len(_sniper_upd.samples) >= 5 and atr_val:
                _recent_gaps = [abs(s.get("gap", 0)) for s in _sniper_upd.samples[-10:]]
                _live_atr = sum(_recent_gaps) / len(_recent_gaps) * 1.5 if _recent_gaps else 0
                if _live_atr > atr_val:
                    atr_val = round(_live_atr, 2)
            if not ptb or not atr_val or atr_val <= 0:
                _log_skip("no_data", f"  [狙] 狙击线程: {coin} 等待数据(ptb={'✓' if ptb else '✗'} atr={'✓' if atr_val else '✗'})")
                continue
            tokens = self.token_cache.get(slug)
            if not tokens:
                # v12.8: 狙击线程主动获取token，不等预热
                try:
                    up_t, down_t = get_token_ids(slug)
                    if up_t and down_t:
                        self.token_cache[slug] = (up_t, down_t)
                        tokens = (up_t, down_t)
                        logger.info(f"  [狙] 狙击线程主动获取token: {coin} {up_t[:12]}../{down_t[:12]}..")
                except Exception:
                    pass
            if not tokens:
                _log_skip("no_token", f"  [狙] 狙击线程: {coin} 无token缓存")
                continue

            # v12.8: 确保两个 token 在 WS _bba 中有数据
            from ai_trader.polymarket_ws import poly_ws
            _has_up = poly_ws._bba.get(tokens[0]) is not None
            _has_down = poly_ws._bba.get(tokens[1]) is not None
            if not _has_up or not _has_down:
                poly_ws.subscribe(list(tokens))
                poly_ws.seed_from_rest(list(tokens), clob_client)

            # ── 时间窗口检查 ──
            # v12.8: PTB就绪即可开始狙击，到结束前_sniper_end_buffer秒停止
            end_dt = datetime.fromisoformat(market["end_time"].replace("Z", "+00:00"))
            remaining = (end_dt - now).total_seconds()
            if remaining <= _sniper_end_buffer:
                _log_skip("time_late", f"  [狙] 狙击线程: {coin} 即将结束(remaining={remaining:.0f}s<{_sniper_end_buffer}s)")
                continue
            if remaining > 300:
                # 还没进入5分钟窗口
                continue

            # ── v12.9: 限价伏击单管理 ──
            AMBUSH_ENABLED = os.environ.get("SNIPER_AMBUSH", "0") == "1"
            if AMBUSH_ENABLED:
                self._manage_ambush(slug, coin, tokens, ptb, atr_val, remaining, _sniper_end_buffer)

            # ── v12.7: 同epoch去重 — BTC/ETH同一5分钟高度相关，只狙1个 ──
            epoch = slug.rsplit("-", 1)[-1] if "-" in slug else slug  # e.g. "1774490400"
            epoch_count = sum(1 for e in self._sniper_epoch_used if e == epoch)
            if epoch_count >= SNIPER_MAX_PER_EPOCH:
                _log_skip("epoch_limit",
                    f"  [狙] 狙击线程: {coin} epoch={epoch} 已有{epoch_count}笔狙击，跳过(limit={SNIPER_MAX_PER_EPOCH})")
                continue

            # ── 获取实时价格：Chainlink优先（和结算价一致）→ Binance fallback ──
            # v12.7.2: Binance vs Chainlink 价差可达 $26(0.5+ATR)，
            # Binance说涨但Chainlink说跌 → 狙击方向判断错误
            # 结算用Chainlink，狙击也必须用Chainlink判断方向
            price = chainlink_stream.get_price(coin)
            _price_src = "CL"
            if price is None:
                price = binance_stream.get_price(coin)
                _price_src = "Binance"
            if price is None:
                _log_skip("no_price", f"  [狙] 狙击线程: {coin} 无实时价格(Binance=✗ CL=✗)")
                continue

            # ── 独立贝叶斯：狙击线程自己维护updater，100ms自更新 ──
            if slug not in self._sniper_updaters:
                from ai_trader.bayesian_engine import BayesianUpdater
                self._sniper_updaters[slug] = BayesianUpdater(prior_up=0.5, atr_val=atr_val)
                logger.info(f"  [狙] 狙击线程: {coin} 创建独立贝叶斯(ATR={atr_val:.2f})")
            updater = self._sniper_updaters[slug]
            updater.update(price, ptb)

            if updater.n_updates < SNIPER_MIN_UPDATES:
                _log_skip("low_updates", f"  [狙] 狙击线程: {coin} 独立贝叶斯更新中({updater.n_updates}/{SNIPER_MIN_UPDATES})")
                continue

            # ── 核心计算：gap → ATR偏离 → 方向 ──
            gap = price - ptb
            diff_atr = abs(gap) / atr_val
            if diff_atr < SNIPER_MIN_ATR:
                _log_skip("atr_low",
                    f"  [狙] 狙击线程: {coin} ATR={diff_atr:.2f}<{SNIPER_MIN_ATR} "
                    f"| gap={gap:.0f} price={price:.2f}({_price_src})")
                continue

            gap_dir = "UP" if gap > 0 else "DOWN"

            # ── v12.9: Gap动量检查 — gap缩小说明方向正在反转（死猫弹跳/均值回归） ──
            SNIPER_GAP_DECAY_RATIO = float(os.environ.get("SNIPER_GAP_DECAY_RATIO", "0.75"))
            _sniper_samples = updater.samples
            if len(_sniper_samples) >= 4:
                _recent_abs_gaps = [abs(s["gap"]) for s in _sniper_samples[-6:]]
                _peak_gap = max(_recent_abs_gaps[:-1])
                _curr_gap = _recent_abs_gaps[-1]
                if _peak_gap > 0 and _curr_gap < _peak_gap * SNIPER_GAP_DECAY_RATIO:
                    _log_skip("gap_decay",
                        f"  [狙] 狙击线程: {coin} {gap_dir} ATR={diff_atr:.2f} | "
                        f"gap收缩 {_curr_gap:.0f}<{_peak_gap:.0f}×{SNIPER_GAP_DECAY_RATIO} (动量减弱)")
                    continue

            # ── 贝叶斯方向确认（独立updater） ──
            signal = _get_bayesian_signal(updater, remaining_seconds=remaining)
            if not signal:
                _log_skip("no_signal", f"  [狙] 狙击线程: {coin} {gap_dir} ATR={diff_atr:.2f} | 无贝叶斯信号")
                continue
            bayesian_dir = signal.get("direction")
            bayesian_conf = signal.get("confidence", 0)
            if bayesian_dir != gap_dir:
                _log_skip("dir_mismatch",
                    f"  [狙] 狙击线程: {coin} {gap_dir} ATR={diff_atr:.2f} | "
                    f"方向不一致(gap={gap_dir} 贝叶斯={bayesian_dir} conf={bayesian_conf:.3f})")
                continue
            if bayesian_conf < SNIPER_MIN_CONF:
                _log_skip("low_conf",
                    f"  [狙] 狙击线程: {coin} {gap_dir} ATR={diff_atr:.2f} | "
                    f"置信度不足({bayesian_conf:.3f}<{SNIPER_MIN_CONF})")
                continue

            # ── v12.9: WS 健康检查 — WS失效时价格不可信，禁止狙击 ──
            _target_token = tokens[0] if gap_dir == "UP" else tokens[1]
            from ai_trader.polymarket_ws import poly_ws as _poly_ws_check
            _ws_health = _poly_ws_check.get_best_bid_ask_snapshot(_target_token)
            _ws_health_age = _ws_health.get("age_ms", 9999) if _ws_health else 9999
            SNIPER_WS_MAX_AGE = float(os.environ.get("SNIPER_WS_MAX_AGE", "5000"))
            _ws_stale = _ws_health_age >= SNIPER_WS_MAX_AGE
            if _ws_stale:
                # WS失效：禁止FOK（价格不可信），但允许伏击限价单（不依赖实时价）
                AMBUSH_ENABLED_WS = os.environ.get("SNIPER_AMBUSH", "0") == "1"
                if AMBUSH_ENABLED_WS and slug not in self._ambush_orders and bayesian_conf >= 0.30 and diff_atr >= 0.8:
                    self._place_directional_ambush(
                        slug, coin, tokens, gap_dir, bayesian_conf,
                        diff_atr, ptb, atr_val, remaining)
                _log_skip("ws_stale",
                    f"  [狙] 狙击线程: {coin} {gap_dir} ATR={diff_atr:.2f} | "
                    f"WS失效(age={_ws_health_age:.0f}ms>{SNIPER_WS_MAX_AGE:.0f}ms) 价格不可信")
                continue

            # ── CLOB 执行价获取 ──
            _t0 = time.time()
            clob = get_realtime_odds(tokens[0], tokens[1])
            real_ask = clob.get("up_ask") if gap_dir == "UP" else clob.get("down_ask")

            # v12.6→v12.8: WS→REST 价格校正 — 仅在 WS 数据不新鲜时触发
            # WS age < 2s 时直接信任（WS 比 REST 更实时），省 ~300ms HTTP
            if real_ask and 0.01 < real_ask < 0.99:
                _target_token = tokens[0] if gap_dir == "UP" else tokens[1]
                from ai_trader.polymarket_ws import poly_ws
                _ws_snap = poly_ws.get_best_bid_ask_snapshot(_target_token)
                _ws_age_ms = _ws_snap.get("age_ms", 9999) if _ws_snap else 9999
                _WS_FRESH_MS = float(os.environ.get("SNIPER_WS_FRESH_MS", "50"))
                if _ws_age_ms >= _WS_FRESH_MS:
                    # WS 数据超过阈值(默认50ms)不新鲜，降级 REST 校正
                    try:
                        from ai_analyze_v2 import _get_execution_quote
                        _rest_q = _get_execution_quote(_target_token, force_fresh=True)
                        _rest_ask = _rest_q.get("best_ask") if _rest_q else None
                        if _rest_ask and 0.01 < _rest_ask < 0.99:
                            if abs(_rest_ask - real_ask) > 0.03:
                                logger.info(
                                    f"  [狙] WS→REST校正: {coin} {gap_dir} "
                                    f"WS=${real_ask:.2f}→REST=${_rest_ask:.2f} (WS age={_ws_age_ms:.0f}ms)")
                                real_ask = _rest_ask
                    except Exception:
                        pass
                else:
                    logger.debug(
                        f"  [狙] WS新鲜跳过REST校正: {coin} {gap_dir} "
                        f"ask=${real_ask:.2f} age={_ws_age_ms:.0f}ms")
            _t_ws = time.time()

            if not real_ask or real_ask <= 0.01 or real_ask >= 0.99:
                _log_skip("no_ask",
                    f"  [狙] 狙击线程: {coin} {gap_dir} ATR={diff_atr:.2f} | "
                    f"无可执行价(up={clob.get('up_ask')} down={clob.get('down_ask')})")
                continue
            if real_ask > SNIPER_MAX_PRICE:
                # v12.9.1: ask太贵不FOK → 改为方向确认伏击（限价等好价）
                AMBUSH_ENABLED = os.environ.get("SNIPER_AMBUSH", "0") == "1"
                if AMBUSH_ENABLED and bayesian_conf >= 0.25:
                    self._place_directional_ambush(
                        slug, coin, tokens, gap_dir, bayesian_conf,
                        diff_atr, ptb, atr_val, remaining)
                _log_skip("price_high",
                    f"  [狙] 狙击线程: {coin} {gap_dir} ATR={diff_atr:.2f} | "
                    f"价格过高 ${real_ask:.2f}>${SNIPER_MAX_PRICE}")
                continue

            # ── EV 计算（v12.7: 独立shrinkage + 扣除手续费/spread） ──
            from ai_analyze_v2 import _random_walk_p_win
            rw_pw = _random_walk_p_win(abs(gap), updater.atr_val, remaining)
            # v12.9: 剩余时间越长，shrinkage越保守 — 早期gap多为噪音/死猫弹跳
            # 120s内用全量shrinkage，超出按比例衰减（228s→0.32, 60s→0.60）
            SNIPER_SHRINK_FULL_AT = float(os.environ.get("SNIPER_SHRINK_FULL_AT", "120"))
            _time_factor = min(1.0, SNIPER_SHRINK_FULL_AT / max(remaining, 1))
            _effective_shrinkage = SNIPER_P_WIN_SHRINKAGE * _time_factor
            p_win = 0.5 + (max(rw_pw, signal["p_hat"]) - 0.5) * _effective_shrinkage
            # 扣除隐含成本（和正常流程对齐）
            try:
                from ai_trader.fees import effective_fee_rate
                _fee_rate = effective_fee_rate(real_ask, 1000)
                _entry_fee = real_ask / max(1.0 - _fee_rate, 1e-9) - real_ask
                _spread_cost = 0.04 * 0.3  # 估算spread×提前退出概率
                _exit_fee = (real_ask - 0.04) * effective_fee_rate(max(real_ask - 0.04, 0.01), 1000) * 0.3
                _total_cost = _entry_fee + _spread_cost + _exit_fee
            except Exception:
                _total_cost = 0.016  # fallback: 典型值
            ev_est = round(p_win - real_ask - _total_cost, 4)
            _t_calc = time.time()

            if ev_est <= SNIPER_MIN_EV:
                _log_skip("ev_low",
                    f"  [狙] 狙击线程: {coin} {gap_dir} ATR={diff_atr:.2f} | "
                    f"EV不足 {ev_est:+.4f}<={SNIPER_MIN_EV} | CLOB=${real_ask:.2f} p_win={p_win:.3f} cost={_total_cost:.3f}")
                continue

            # 条件全通过，清除跳过记录
            self._sniper_last_reason.pop(slug, None)

            # ── 标记正在狙击，防止主线程同时处理 ──
            with self._sniper_lock:
                if slug in self.analyzed or slug in self._sniper_processing:
                    continue
                self._sniper_processing.add(slug)

            logger.info(
                f"\n⚡ 狙击线程: {coin} {gap_dir} | "
                f"gap={gap:.0f} ({diff_atr:.1f}ATR) | CLOB=${real_ask:.2f} | "
                f"p_win={p_win:.3f} EV={ev_est:+.3f} | "
                f"Bayesian conf={signal['confidence']:.3f} | "
                f"耗时: WS={(_t_ws-_t0)*1000:.0f}ms 计算={(_t_calc-_t_ws)*1000:.0f}ms"
            )

            # ── v12.9: FOK前二次校验 — 从条件通过到下单可能已过1-2秒 ──
            _price2 = chainlink_stream.get_price(coin)
            if _price2 is None:
                _price2 = binance_stream.get_price(coin)
            if _price2 is not None:
                _gap2 = abs(_price2 - ptb)
                _atr2 = _gap2 / atr_val
                _dir2 = "UP" if _price2 > ptb else "DOWN"
                if _atr2 < SNIPER_MIN_ATR:
                    logger.info(
                        f"  ⚡ 二次校验放弃: {coin} ATR {diff_atr:.2f}→{_atr2:.2f}<{SNIPER_MIN_ATR} "
                        f"(价格{price:.2f}→{_price2:.2f})")
                    with self._sniper_lock:
                        self._sniper_processing.discard(slug)
                    continue
                if _dir2 != gap_dir:
                    logger.info(
                        f"  ⚡ 二次校验放弃: {coin} 方向反转 {gap_dir}→{_dir2} "
                        f"(价格{price:.2f}→{_price2:.2f})")
                    with self._sniper_lock:
                        self._sniper_processing.discard(slug)
                    continue

            # ── v12.9.5: FOK入场前清掉该token所有伏击残留挂单 ──
            sniper_token = tokens[0] if gap_dir == "UP" else tokens[1]
            clob_client.cancel_all(sniper_token)
            _did_sniper_inc = False
            global _pending_bets
            try:
                with _position_lock:
                    MAX_OPEN_POSITIONS = int(os.environ.get("MAX_OPEN_POSITIONS", "2"))
                    open_count = 0
                    if os.path.exists(POSITIONS_FILE):
                        try:
                            with open(POSITIONS_FILE) as _f:
                                for _line in _f:
                                    _p = json.loads(_line.strip())
                                    if not _p.get("closed", False):
                                        open_count += 1
                        except Exception:
                            pass
                    if open_count + _pending_bets >= MAX_OPEN_POSITIONS:
                        logger.info(f"  ⚡ 狙击线程放弃: 持仓已满 {open_count}+{_pending_bets}")
                        continue
                    else:
                        _pending_bets += 1
                        _did_sniper_inc = True

                from ai_analyze_v2 import execute_bet
                from ai_trader.base_rate import get_base_rate
                _br = get_base_rate(diff_atr)
                _kr = 0.5 if _br < 0.55 else 1.0
                sniper_details = {
                    "p_win_final": round(p_win, 4),
                    "expected_value": ev_est,
                    "diff_in_atr": round(diff_atr, 2),
                    "atr": round(updater.atr_val, 2),
                    "estimated_value": round(p_win, 3),
                    "base_rate": round(_br, 4),
                    "price_to_beat": ptb,
                    "opposite_token_id": tokens[1] if gap_dir == "UP" else tokens[0],
                    "sniper": True,
                    "sniper_thread": True,
                }
                _bal = self._cached_balance if self._cached_balance and self._cached_balance > 0 else 999.0
                success, entry_price, bet_size, output = execute_bet(
                    slug, gap_dir, sniper_token,
                    confidence=signal["confidence"],
                    ev=ev_est,
                    p_hat=signal["p_hat"],
                    entry_details=sniper_details,
                    kelly_reduction=_kr,
                    pre_balance=_bal,
                )
                _t_done = time.time()
                total_ms = round((_t_done - _t0) * 1000)
                ws_ms = round((_t_ws - _t0) * 1000)
                calc_ms = round((_t_calc - _t_ws) * 1000)
                fok_ms = round((_t_done - _t_calc) * 1000)

                if success:
                    logger.info(
                        f"  ⚡ 狙击线程成交! ${entry_price:.3f}×{bet_size}份 | "
                        f"总耗时={total_ms}ms (WS={ws_ms}ms 计算={calc_ms}ms FOK={fok_ms}ms)"
                    )
                    self.analyzed.add(slug)
                    self._sniper_epoch_used.add(epoch)  # v12.7: 记录epoch防重复
                    cost = round(entry_price * bet_size, 4)
                    record_bet_cost(slug, cost)
                    self.positions[slug] = Position(
                        slug, sniper_token, gap_dir, entry_price, bet_size,
                        datetime.now(timezone.utc).isoformat()
                    )
                    send_notification(coin, gap_dir, signal["confidence"], ev_est,
                                      entry_price, bet_size, sniper=True, sniper_thread=True)
                else:
                    logger.info(
                        f"  ⚡ 狙击线程未成交: {str(output)[:80]} | "
                        f"总耗时={total_ms}ms (WS={ws_ms}ms 计算={calc_ms}ms FOK={fok_ms}ms)"
                    )
            finally:
                if _did_sniper_inc:
                    with _position_lock:
                        _pending_bets = max(0, _pending_bets - 1)
                with self._sniper_lock:
                    self._sniper_processing.discard(slug)

    def update_markets(self):
        """更新市场列表"""
        if not circuit_breaker_check():
            return []
        try:
            markets = get_current_markets()
            # 空列表是正常的（市场间歇期），不算API失败
            circuit_breaker_record(True)
        except Exception:
            circuit_breaker_record(False)
            markets = []
        now = datetime.now(timezone.utc)
        
        for market in markets:
            slug = market["slug"]
            
            if slug not in self.tracked:
                self.tracked[slug] = market
                # 新市场周期，重置该币种的 PTB 获取失败计数
                _ptb_failures.pop(market["coin"], None)
                end_dt = datetime.fromisoformat(market["end_time"].replace("Z", "+00:00"))
                remaining = (end_dt - now).total_seconds()
                if slug in self.restored_slugs:
                    logger.info(f"\n♻️ 恢复市场: {market['coin']} | {slug}")
                    logger.info(f"   已恢复上下文，阻止重复入场 | 剩余: {remaining:.0f}s")
                else:
                    logger.info(f"\n🆕 新市场: {market['coin']} | {slug}")
                    et_dt = end_dt - timedelta(hours=5)  # UTC-5 (EST)
                    logger.info(f"   结束: {et_dt.strftime('%H:%M:%S')} ET | 剩余: {remaining:.0f}s")

                # token 获取移到预热(8s)，0秒时 Gamma API 可能返回旧市场 token
            else:
                self.tracked[slug]["up_odds"] = market["up_odds"]
                self.tracked[slug]["down_odds"] = market["down_odds"]

            # markets API 返回的 price_to_beat 仅作展示，分析时统一走 crypto-price API 实时获取
        
        return markets
    
    def check_analysis_trigger(self):
        """预热观察 + 趋势确认下注（优化版）
        多个市场同时触发下注时并行执行 analyze_and_trade，避免串行等待。
        """
        now = datetime.now(timezone.utc)
        strategy_cfg = get_strategy_config()
        pending_trades = []  # [(slug, market, extra_info), ...]

        for slug, market in list(self.tracked.items()):
            end_dt = datetime.fromisoformat(market["end_time"].replace("Z", "+00:00"))
            start_dt = end_dt - timedelta(minutes=5)
            elapsed = (now - start_dt).total_seconds()
            remaining = (end_dt - now).total_seconds()
            
            # === PTB 获取期：2s-40s（收集待获取列表，循环末尾并行获取） ===
            if PTB_FETCH_START_SECONDS <= elapsed < 40 and slug not in self.ptb_cache:
                if slug not in self._ptb_pending:
                    self._ptb_pending[slug] = market['coin']
            
            # === 预热期：由 WARMUP_START_SECONDS 控制（延续到晚期窗口结束） ===
            LATE_BET_END = strategy_cfg["late_bet_end"]
            if strategy_cfg["warmup_start_seconds"] <= elapsed <= LATE_BET_END and (slug not in self.analyzed or slug in self.skipped_markets):
                if slug not in self.warmup_started:
                    self.warmup_started.add(slug)
                    self.warmup_data[slug] = []
                    # 初始化贝叶斯更新器，先验 = 市场赔率
                    try:
                        from ai_trader.bayesian_engine import BayesianUpdater
                        from ai_trader.binance_api import get_klines
                        from ai_trader.indicators import atr as calc_atr
                        symbol = f"{market['coin']}USDT"
                        klines = get_klines(symbol, "1m", 15)
                        if klines:
                            atr_val = calc_atr(
                                [k["high"] for k in klines],
                                [k["low"] for k in klines],
                                [k["close"] for k in klines],
                                min(14, len(klines) - 1)
                            )
                        else:
                            atr_val = None
                        self.bayesian_updaters[slug] = BayesianUpdater(
                            prior_up=0.50,
                            atr_val=atr_val
                        )
                        logger.info(f"\n🔥 预热开始: {market['coin']} | {slug} | 剩余{remaining:.0f}s | 贝叶斯先验UP=0.500(无偏) ATR={atr_val}")
                    except Exception as e:
                        logger.warning(f"\n🔥 预热开始(无贝叶斯): {market['coin']} | {slug} | {e}")

                    # v12 fix: 预热时强制刷新 token（0秒发现时 Gamma API 可能返回旧 token）
                    # 预热开始时获取 token + 订阅 WS（确保是当前市场的 token）
                    try:
                        up_t, down_t = get_token_ids(slug)
                        if up_t and down_t:
                            self.token_cache[slug] = (up_t, down_t)
                            clob_client.precache_tokens([up_t, down_t])
                            from ai_trader.polymarket_ws import poly_ws
                            poly_ws.subscribe([up_t, down_t])
                            # v12.2: WS 订阅后立即 REST 预读种子，消除初始无快照
                            poly_ws.seed_from_rest([up_t, down_t], clob_client)
                            logger.info(f"   📡 token + WS + REST种子 已就绪")
                    except Exception:
                        pass

                # 按配置间隔采集，有PTB时才做贝叶斯更新
                ptb_now = self.ptb_cache.get(slug)
                samples = self.warmup_data.get(slug, [])
                sample_interval = (
                    strategy_cfg["warmup_sample_interval_late"]
                    if elapsed >= strategy_cfg["early_bet_start"]
                    else strategy_cfg["warmup_sample_interval_early"]
                )
                if len(samples) == 0 or (time.time() - samples[-1]["ts"]) >= sample_interval:
                    try:
                        # v12.7.2: 预热采样改回 Chainlink 优先（和结算同源）
                        # Binance vs Chainlink 价差可达 $26(0.5+ATR)，
                        # Binance 训练的贝叶斯方向可能和 Chainlink 结算方向相反
                        from ai_trader.price_oracle import get_onchain_price
                        coin = market['coin']
                        price, price_src = get_onchain_price(coin, with_source=True)
                        price_src = price_src or "CL"
                        if not price:
                            # Chainlink 无数据时回退 Binance
                            from ai_trader.binance_api import price_stream as _bn_stream
                            price = _bn_stream.get_price(coin)
                            price_src = "BN"
                        if price and ptb_now:
                            gap = round(price - ptb_now, 2)
                            samples.append({"price": price, "gap": gap, "ts": time.time()})
                            self.warmup_data[slug] = samples

                            # 贝叶斯更新
                            updater = self.bayesian_updaters.get(slug)
                            if updater:
                                updater.update(price, ptb_now)
                                signal = _get_bayesian_signal(updater, remaining_seconds=remaining)
                                if signal:
                                    direction = signal["direction"]
                                    p_hat = signal["p_hat"]
                                    conf = signal["confidence"]
                                    source = signal.get("source")
                                    incremental_conf = signal.get("incremental_confidence")
                                    state_conf = signal.get("state_confidence")
                                    extra = ""
                                    if source:
                                        extra = f" src={source}"
                                    if incremental_conf is not None and state_conf is not None:
                                        extra += f" inc={incremental_conf:.3f} state={state_conf:.3f}"
                                    logger.info(
                                        f"  📍 采样#{len(samples)}: price={price:.2f}({price_src}) gap={gap} "
                                        f"| 贝叶斯: {direction} p̂={p_hat:.4f} conf={conf:.3f}{extra}"
                                    )
                                else:
                                    logger.info(f"  📍 采样#{len(samples)}: price={price:.2f}({price_src}) gap={gap}")
                            else:
                                logger.info(f"  📍 采样#{len(samples)}: price={price:.2f}({price_src}) gap={gap}")
                            # ═══ v12: 动量狙击 — 检测到大波动时跳过完整分析直接下单 ═══
                            # v12.9.1: 主线程动量狙击已禁用 — 狙击线程有完整安全防护
                            # （WS健康检查/gap动量/FOK二次校验/时间衰减shrinkage），
                            # 主线程fallback没有这些检查，会被噪音尖峰误导入仓。
                            # 保留代码但不执行，狙击线程异常时由伏击限价单兜底。
                            _INLINE_SNIPER_DISABLED = True  # v12.9.1: 禁用主线程动量狙击
                            SNIPER_MIN_ATR = float(os.environ.get("SNIPER_MIN_ATR", "0.5"))   # 0.5起步（日志验证2笔成交都是0.5ATR）
                            SNIPER_MAX_PRICE = float(os.environ.get("SNIPER_MAX_PRICE", "0.65"))  # 0.58→0.65: 放宽入场上限，EV正就进
                            SNIPER_MIN_SAMPLES = 2     # 2个样本够了（gap方向是主信号，贝叶斯只确认）
                            _sniper_end_buf = int(os.environ.get("SNIPER_END_BUFFER", "5"))
                            if (not _INLINE_SNIPER_DISABLED  # v12.9.1: 主线程动量狙击已禁用
                                    and updater and updater.atr_val and updater.atr_val > 0
                                    and slug not in self.analyzed
                                    and slug not in self.positions
                                    and slug not in self._sniper_processing  # 狙击线程正在处理时跳过
                                    and remaining > _sniper_end_buf  # v12.8: 结束前N秒停止
                                    and ptb_now is not None  # PTB就绪才狙击
                                    and len(samples) >= SNIPER_MIN_SAMPLES):
                                diff_atr = abs(gap) / updater.atr_val
                                # 方向: 以 gap 为准（price > PTB → UP, price < PTB → DOWN）
                                # 贝叶斯作为确认信号：方向必须一致才触发
                                gap_dir = "UP" if gap > 0 else "DOWN"
                                bayesian_dir = signal.get("direction") if signal else None
                                bayesian_aligned = bayesian_dir == gap_dir
                                # 狙击条件检查 + 日志
                                _sniper_reason = None
                                if diff_atr < SNIPER_MIN_ATR:
                                    _sniper_reason = f"ATR={diff_atr:.2f}<{SNIPER_MIN_ATR}"
                                elif not bayesian_aligned:
                                    _sniper_reason = f"方向不一致(gap={gap_dir},贝叶斯={bayesian_dir})"
                                elif signal.get("confidence", 0) < 0.25:
                                    _sniper_reason = f"conf={signal.get('confidence',0):.0%}<25%"

                                if _sniper_reason:
                                    # 只在 ATR ≥ 0.3 时打印（过滤纯噪音）
                                    if diff_atr >= 0.3:
                                        logger.debug(f"  🔇 狙击跳过: {coin} {gap_dir} | {_sniper_reason} | ATR={diff_atr:.2f}")
                                else:
                                    sniper_dir = gap_dir
                                    tokens = self.token_cache.get(slug)
                                    if not tokens:
                                        logger.info(f"  🔇 狙击跳过: {coin} {sniper_dir} | 无token缓存")
                                    else:
                                        sniper_token = tokens[0] if sniper_dir == "UP" else tokens[1]
                                        opposite_token = tokens[1] if sniper_dir == "UP" else tokens[0]
                                        _t0 = time.time()
                                        from ai_trader.polymarket_ws import poly_ws

                                        # ═══ 用 get_realtime_odds 获取双边价格（和正常分析路径一致）═══
                                        up_token_s = tokens[0]
                                        down_token_s = tokens[1]
                                        clob = get_realtime_odds(up_token_s, down_token_s)
                                        # 取本方 ask 作为执行价
                                        if sniper_dir == "UP":
                                            real_ask = clob.get("up_ask")
                                        else:
                                            real_ask = clob.get("down_ask")

                                        # v12.8: WS→REST价格校正 — 仅在 WS 数据不新鲜时触发
                                        if real_ask and 0.01 < real_ask < 0.99:
                                            _ws_snap = poly_ws.get_best_bid_ask_snapshot(sniper_token)
                                            _ws_age_ms = _ws_snap.get("age_ms", 9999) if _ws_snap else 9999
                                            _WS_FRESH = float(os.environ.get("SNIPER_WS_FRESH_MS", "50"))
                                            if _ws_age_ms >= _WS_FRESH:
                                                try:
                                                    from ai_analyze_v2 import _get_execution_quote
                                                    _rest_q = _get_execution_quote(sniper_token, force_fresh=True)
                                                    _rest_ask = _rest_q.get("best_ask") if _rest_q else None
                                                    if _rest_ask and 0.01 < _rest_ask < 0.99:
                                                        if abs(_rest_ask - real_ask) > 0.03:
                                                            logger.info(
                                                                f"  [动量狙击] WS→REST校正: {coin} {sniper_dir} "
                                                                f"WS=${real_ask:.2f}→REST=${_rest_ask:.2f} (age={_ws_age_ms:.0f}ms)")
                                                            real_ask = _rest_ask
                                                except Exception:
                                                    pass

                                        _t_ws = time.time()
                                        if not real_ask or real_ask <= 0.01 or real_ask >= 0.99:
                                            logger.info(
                                                f"  🔇 狙击跳过: {coin} {sniper_dir} ATR={diff_atr:.2f} | "
                                                f"无可执行价(up={clob.get('up_ask')},down={clob.get('down_ask')})"
                                            )
                                        elif real_ask > SNIPER_MAX_PRICE:
                                            logger.info(
                                                f"  🔇 狙击跳过: {coin} {sniper_dir} ATR={diff_atr:.2f} | "
                                                f"${real_ask:.2f}>${SNIPER_MAX_PRICE}"
                                            )
                                        else:
                                            ws_ask = real_ask
                                            t_sniper = time.time()
                                            b_conf = signal["confidence"]
                                            b_phat = signal["p_hat"]
                                            from ai_analyze_v2 import _random_walk_p_win
                                            rw_pw = _random_walk_p_win(abs(gap), updater.atr_val, remaining)
                                            P_WIN_SHRINKAGE = float(os.environ.get("P_WIN_SHRINKAGE", "0.80"))
                                            # v12.9: 剩余时间越长，shrinkage越保守
                                            _shrink_full_at = float(os.environ.get("SNIPER_SHRINK_FULL_AT", "120"))
                                            _tf = min(1.0, _shrink_full_at / max(remaining, 1))
                                            p_win = 0.5 + (max(rw_pw, b_phat) - 0.5) * P_WIN_SHRINKAGE * _tf
                                            ev_est = round(p_win - ws_ask, 4)
                                            _t_calc = time.time()

                                            logger.info(
                                                f"\n⚡ 动量狙击: {coin} {sniper_dir} | "
                                                f"gap={gap} ({diff_atr:.1f}ATR) | CLOB=${ws_ask:.2f} | "
                                                f"p_win={p_win:.3f} EV={ev_est:+.3f} | "
                                                f"Bayesian conf={b_conf:.3f} | "
                                                f"耗时: WS={(_t_ws-_t0)*1000:.0f}ms 计算={(_t_calc-_t_ws)*1000:.0f}ms"
                                            )

                                            if ev_est <= 0.01:
                                                logger.info(f"  ⚡ 狙击放弃: EV={ev_est:+.3f}<=0.01")
                                            else:
                                                # 持仓检查
                                                _did_sniper_inc = False
                                                global _pending_bets
                                                with _position_lock:
                                                    MAX_OPEN_POSITIONS = int(os.environ.get("MAX_OPEN_POSITIONS", "2"))
                                                    open_count = 0
                                                    if os.path.exists(POSITIONS_FILE):
                                                        try:
                                                            with open(POSITIONS_FILE) as _f:
                                                                for _line in _f:
                                                                    _p = json.loads(_line.strip())
                                                                    if not _p.get("closed", False):
                                                                        open_count += 1
                                                        except Exception:
                                                            pass
                                                    if open_count + _pending_bets >= MAX_OPEN_POSITIONS:
                                                        logger.info(f"  ⚡ 狙击放弃: 持仓已满 {open_count}+{_pending_bets}")
                                                    else:
                                                        _pending_bets += 1
                                                        _did_sniper_inc = True

                                                if _did_sniper_inc:
                                                    # v12.9: 加锁防止与狙击线程同市场双重入场
                                                    _main_sniper_ok = False
                                                    with self._sniper_lock:
                                                        if slug not in self._sniper_processing and slug not in self.positions:
                                                            self._sniper_processing.add(slug)
                                                            _main_sniper_ok = True
                                                    if not _main_sniper_ok:
                                                        logger.info(f"  ⚡ 动量狙击放弃: 狙击线程已处理 {slug}")
                                                        with _position_lock:
                                                            _pending_bets = max(0, _pending_bets - 1)
                                                        _did_sniper_inc = False
                                                    if _main_sniper_ok:
                                                        try:
                                                            from ai_analyze_v2 import execute_bet
                                                            from ai_trader.base_rate import get_base_rate
                                                            _br = get_base_rate(diff_atr)
                                                            _kr = 0.5 if _br < 0.55 else 1.0
                                                            sniper_details = {
                                                                "p_win_final": round(p_win, 4),
                                                                "expected_value": ev_est,
                                                                "diff_in_atr": round(diff_atr, 2),
                                                                "atr": round(updater.atr_val, 2),
                                                                "estimated_value": round(p_win, 3),
                                                                "base_rate": round(_br, 4),
                                                                "price_to_beat": ptb_now,
                                                                "opposite_token_id": tokens[1] if sniper_dir == "UP" else tokens[0],
                                                                "sniper": True,
                                                            }
                                                            _bal = self._cached_balance if self._cached_balance and self._cached_balance > 0 else 999.0
                                                            success, entry_price, bet_size, output = execute_bet(
                                                                slug, sniper_dir, sniper_token,
                                                                confidence=b_conf,
                                                                ev=ev_est,
                                                                p_hat=b_phat,
                                                                entry_details=sniper_details,
                                                                kelly_reduction=_kr,
                                                                pre_balance=_bal,
                                                            )
                                                            _t_fok_done = time.time()
                                                            sniper_ms = round((_t_fok_done - t_sniper) * 1000)
                                                            fok_ms = round((_t_fok_done - _t_calc) * 1000)
                                                            ws_ms = round((_t_ws - _t0) * 1000)
                                                            calc_ms = round((_t_calc - _t_ws) * 1000)
                                                            if success:
                                                                logger.info(
                                                                    f"  ⚡ 狙击成交! ${entry_price:.3f}×{bet_size}份 | "
                                                                    f"总耗时={sniper_ms}ms (WS读取={ws_ms}ms 计算={calc_ms}ms FOK={fok_ms}ms)"
                                                                )
                                                                self.analyzed.add(slug)
                                                                cost = round(entry_price * bet_size, 4)
                                                                record_bet_cost(slug, cost)
                                                                self.positions[slug] = Position(
                                                                    slug, sniper_token, sniper_dir, entry_price, bet_size,
                                                                    datetime.now(timezone.utc).isoformat()
                                                                )
                                                                send_notification(coin, sniper_dir, b_conf, ev_est, entry_price, bet_size, sniper=True)
                                                            else:
                                                                logger.info(
                                                                    f"  ⚡ 狙击未成交: {str(output)[:80]} | "
                                                                    f"总耗时={sniper_ms}ms (WS={ws_ms}ms 计算={calc_ms}ms FOK={fok_ms}ms)"
                                                                )
                                                        finally:
                                                            if _did_sniper_inc:
                                                                with _position_lock:
                                                                    _pending_bets = max(0, _pending_bets - 1)
                                                            with self._sniper_lock:
                                                                self._sniper_processing.discard(slug)

                        elif price and not ptb_now:
                            logger.info(f"  ⏳ 等待PTB... price={price:.2f}({price_src})")
                        elif not price:
                            logger.info(f"  ⏳ 等待链上价格... coin={coin}")
                    except Exception as e:
                        logger.warning(f"  ⚠️ 预热采样失败: {e}")

                # === 波动重触发：跳过的市场检测价格大幅偏离 ===
                REANALYZE_ATR_MULT = float(os.environ.get("REANALYZE_ATR_MULT", "1.5"))
                REANALYZE_COOLDOWN = float(os.environ.get("REANALYZE_COOLDOWN", "15"))
                MAX_REANALYZE = _runtime_max_reanalyze if _runtime_max_reanalyze is not None else 1
                if slug in self.skipped_markets and slug not in self.positions:
                    skip_info = self.skipped_markets[slug]
                    updater = self.bayesian_updaters.get(slug)
                    samples = self.warmup_data.get(slug, [])
                    latest_price = samples[-1]["price"] if samples else None

                    if (skip_info["reanalyze_count"] < MAX_REANALYZE
                            and time.time() - skip_info["skip_time"] >= REANALYZE_COOLDOWN
                            and updater and updater.atr_val and updater.atr_val > 0
                            and latest_price):
                        move = abs(latest_price - skip_info["price_at_skip"])
                        atr = updater.atr_val
                        if move >= atr * REANALYZE_ATR_MULT:
                            skip_info["move_atr"] = round(move / atr, 2)
                            logger.info(
                                f"\n🔄 波动重触发: {market['coin']} "
                                f"移动{move:.0f} ({move/atr:.1f}ATR ≥ {REANALYZE_ATR_MULT}ATR) "
                                f"自跳过({skip_info['reason']})后 | 剩余{remaining:.0f}s"
                            )
                            self.analyzed.discard(slug)
                            self.early_analyzed.discard(slug)
                            self.last_analysis_time.pop(slug, None)
                            skip_info["reanalyze_count"] += 1

            # === 早期下注窗口：由 EARLY_BET_START / EARLY_BET_END 控制 ===
            EARLY_BET_START = strategy_cfg["early_bet_start"]
            EARLY_BET_END = strategy_cfg["early_bet_end"]
            ENTRY_REANALYZE_INTERVAL = strategy_cfg["entry_reanalyze_interval"]
            early_ready = (
                slug not in self.early_analyzed
                or time.time() - self.last_analysis_time.get(slug, 0) >= ENTRY_REANALYZE_INTERVAL
            )
            if EARLY_BET_START <= elapsed <= EARLY_BET_END and slug not in self.analyzed and early_ready:
                updater = self.bayesian_updaters.get(slug)
                samples = self.warmup_data.get(slug, [])
                early_min_samples = strategy_cfg["early_min_samples"]

                # 早期门槛: 至少拿到 EARLY_MIN_SAMPLES 个样本
                if updater and updater.n_updates >= early_min_samples and len(samples) >= early_min_samples:
                    bayesian_summary = _get_bayesian_signal(updater, remaining_seconds=remaining)
                    if not bayesian_summary:
                        continue
                    b_dir = bayesian_summary["direction"]
                    b_phat = bayesian_summary["p_hat"]
                    b_conf = bayesian_summary["confidence"]

                    if b_conf >= 0.25:
                        gap_trend, gap_info = self._calc_gap_trend(samples)

                        if gap_trend == "穿越":
                            logger.info(f"  ⏳ 早期窗口: gap穿越，等待晚期窗口")
                        else:
                            skip_info = self.skipped_markets.get(slug, {})
                            is_reanalyze = skip_info.get("reanalyze_count", 0) > 0
                            self.last_analysis_time[slug] = time.time()
                            self.early_analyzed.add(slug)
                            extra_info = {
                                "gap_trend": gap_trend,
                                "gap_info": gap_info,
                                "warmup_samples": len(samples),
                                "early_window": True,
                                "remaining_seconds": remaining,
                            }
                            if is_reanalyze:
                                extra_info["reanalyze"] = True
                                extra_info["volatility_move_atr"] = skip_info.get("move_atr", 0)
                            extra_info["bayesian"] = bayesian_summary
                            reanalyze_tag = " [重分析]" if is_reanalyze else ""
                            logger.info(
                                f"\n⚡ 早期下注窗口{reanalyze_tag}: {market['coin']} "
                                f"elapsed={elapsed:.0f}s | 贝叶斯: {b_dir} p̂={b_phat:.4f} conf={b_conf:.3f}"
                            )
                            pending_trades.append((slug, market, extra_info))

            # === 晚期下注窗口：由 LATE_BET_START / LATE_BET_END 控制 ===
            LATE_BET_START = strategy_cfg["late_bet_start"]
            MAX_TRADE_RETRIES = strategy_cfg["max_trade_retries"]
            if LATE_BET_START <= elapsed <= LATE_BET_END and slug not in self.analyzed:
                # FOK/流动性失败重试上限
                if self.trade_attempts.get(slug, 0) >= MAX_TRADE_RETRIES:
                    self.analyzed.add(slug)
                    logger.warning(f"\n⚠️ {market['coin']} 晚期窗口已重试{MAX_TRADE_RETRIES}次，放弃")
                    continue
                # 冷却期：避免每2秒重复分析，等待市场条件变化
                if time.time() - self.last_analysis_time.get(slug, 0) < ENTRY_REANALYZE_INTERVAL:
                    continue
                self.last_analysis_time[slug] = time.time()
                # 获取跳过时的价格快照（用于波动重触发）
                _skip_price = None
                _samples_snap = self.warmup_data.get(slug, [])
                if _samples_snap:
                    _skip_price = _samples_snap[-1].get("price")

                is_reanalyze = slug in self.skipped_markets
                samples = self.warmup_data.get(slug, [])
                # 保留 gap 趋势作为辅助信号
                gap_trend, gap_info = self._calc_gap_trend(samples)

                extra_info = {"gap_trend": gap_trend, "gap_info": gap_info, "warmup_samples": len(samples), "remaining_seconds": remaining}
                if is_reanalyze:
                    extra_info["reanalyze"] = True
                    skip_info = self.skipped_markets.get(slug, {})
                    extra_info["volatility_move_atr"] = skip_info.get("move_atr", 0)

                # 贝叶斯后验结果
                updater = self.bayesian_updaters.get(slug)
                if updater and updater.n_updates >= strategy_cfg["late_min_updates"]:
                    bayesian_summary = _get_bayesian_signal(updater, remaining_seconds=remaining)
                    if not bayesian_summary:
                        logger.info(f"\n📊 无贝叶斯数据，使用gap趋势: {gap_trend}")
                        continue
                    extra_info["bayesian"] = bayesian_summary
                    b_dir = bayesian_summary["direction"]
                    b_phat = bayesian_summary["p_hat"]
                    b_conf = bayesian_summary["confidence"]
                    reanalyze_tag = " [重分析]" if is_reanalyze else ""
                    logger.info(f"\n🧠 贝叶斯结果{reanalyze_tag}: {b_dir} p̂={b_phat:.4f} conf={b_conf:.3f} (n={updater.n_updates})")

                    # 贝叶斯置信度太低，方向不确定，跳过（等冷却后重试）
                    if b_conf < strategy_cfg["late_low_conf_threshold"]:
                        if not is_reanalyze and _skip_price:
                            self.skipped_markets[slug] = {
                                "skip_time": time.time(),
                                "price_at_skip": _skip_price,
                                "reason": "low_conf",
                                "reanalyze_count": 0,
                                "window": "late",
                            }
                            # 成熟样本下的极低置信度不再每轮重扫，交给波动重触发重新放行。
                            if gap_trend == "穿越" or len(samples) >= strategy_cfg["late_mature_sample_count"]:
                                self.analyzed.add(slug)
                        logger.warning(f"\n⚠️ {market['coin']} 贝叶斯置信度极低({b_conf:.3f})，等待变化")
                        continue
                else:
                    logger.info(f"\n📊 无贝叶斯数据，使用gap趋势: {gap_trend}")

                # gap 趋势仍作为安全阀
                if gap_trend == "穿越":
                    # 但如果贝叶斯置信度很高（>60%），仍允许交易
                    if updater and updater.n_updates >= strategy_cfg["late_min_updates"]:
                        signal = _get_bayesian_signal(updater, remaining_seconds=remaining)
                        b_conf = signal["confidence"] if signal else 0
                        if b_conf >= strategy_cfg["late_gap_cross_allow_conf"]:
                            logger.info(f"  ✅ gap穿越但贝叶斯置信度高({b_conf:.3f})，允许交易")
                        else:
                            logger.warning(f"\n⚠️ {market['coin']} gap穿越+贝叶斯弱({b_conf:.3f})，等待变化")
                            continue
                    else:
                        logger.warning(f"\n⚠️ {market['coin']} gap穿越零轴，等待变化")
                        continue
                elif gap_trend == "缩小":
                    extra_info["min_discount"] = 0.18
                elif gap_trend == "震荡":
                    extra_info["min_discount"] = 0.15

                self.trade_attempts[slug] = self.trade_attempts.get(slug, 0) + 1
                self.skipped_markets.pop(slug, None)
                self.analyzed.add(slug)  # 本轮已入分析队列，避免重复调度
                pending_trades.append((slug, market, extra_info))

        # 并行获取 PTB（优先轻量 HTTP，失败时单币种回退 Playwright 进程）
        if self._ptb_pending:
            pending_slugs = {s: c for s, c in self._ptb_pending.items() if s not in self.ptb_cache}
            self._ptb_pending.clear()
            if pending_slugs:
                # 过滤掉已熔断的币种
                from ai_trader.coins import coin_from_slug
                active = {s: c for s, c in pending_slugs.items() if _ptb_failures.get(c, 0) < 3}
                if active:
                    coins_str = ", ".join(active.values())
                    logger.info(f"\n💰 并行获取 PTB: {coins_str}")
                    from concurrent.futures import ThreadPoolExecutor as _TPE
                    def _fetch_one_ptb(slug_coin):
                        s, c = slug_coin
                        try:
                            ptb, elapsed, source = _fetch_ptb_api(s)
                            if not ptb:
                                ptb, elapsed, source = _fetch_ptb_subprocess(s)
                            if ptb:
                                _ptb_failures[c] = 0
                                return s, ptb, elapsed, source
                            _ptb_failures[c] = _ptb_failures.get(c, 0) + 1
                            logger.warning(f"   PTB 无结果: {c} ({_ptb_failures[c]}/3) | {elapsed:.2f}s")
                        except (requests.Timeout, subprocess.TimeoutExpired):
                            _ptb_failures[c] = _ptb_failures.get(c, 0) + 1
                            logger.warning(f"   PTB 超时: {c} ({_ptb_failures[c]}/3)")
                        except Exception as e:
                            _ptb_failures[c] = _ptb_failures.get(c, 0) + 1
                            logger.warning(f"   PTB 失败: {c} ({_ptb_failures[c]}/3): {str(e)[:60]}")
                        return s, None, 0, None
                    with _TPE(max_workers=len(active)) as pool:
                        results = list(pool.map(_fetch_one_ptb, active.items()))
                    for s, ptb_val, elapsed, source in results:
                        if ptb_val:
                            self.ptb_cache[s] = ptb_val
                            logger.info(f"   ✅ PTB: {coin_from_slug(s)} ${ptb_val:,.2f} ({source}, {elapsed:.2f}s)")

        # 并行执行所有待分析市场（BTC+ETH+BNB同时分析下注，而非串行等待）
        if len(pending_trades) > 1:
            from concurrent.futures import ThreadPoolExecutor as _TPE
            with _TPE(max_workers=len(pending_trades)) as pool:
                futures = {
                    pool.submit(self.analyze_and_trade, s, m, e): s
                    for s, m, e in pending_trades
                }
                for fut in futures:
                    try:
                        fut.result()
                    except Exception as ex:
                        logger.warning(f"  ❌ 并行分析异常({futures[fut]}): {ex}")
        elif pending_trades:
            self.analyze_and_trade(*pending_trades[0])

    def _calc_gap_trend(self, samples):
        """
        基于 (price - PTB) 的gap变化趋势，判断折价空间是扩大还是收缩。
        返回: (trend, info_dict)
          - "扩大"  : gap绝对值持续变大，折价空间还在增加，信号强
          - "缩小"  : gap绝对值持续缩小，折价正被市场消耗，提高阈值
          - "穿越"  : 价格在PTB两侧反复横跳，方向极不确定，跳过
          - "震荡"  : gap无明显趋势，适度提高阈值
          - "数据不足": 样本 < 4，无法判断
        """
        gaps = [s["gap"] for s in samples if s.get("gap") is not None]

        if len(gaps) < 4:
            return "数据不足", {}

        # 检查是否穿越零轴（价格在PTB两侧反复横跳）
        signs = [1 if g >= 0 else -1 for g in gaps]
        sign_changes = sum(1 for i in range(1, len(signs)) if signs[i] != signs[i - 1])
        if sign_changes >= 2:
            return "穿越", {"sign_changes": sign_changes, "gaps": gaps}

        # 计算gap绝对值的扩大/缩小趋势
        abs_gaps = [abs(g) for g in gaps]
        expanding = sum(1 for i in range(1, len(abs_gaps)) if abs_gaps[i] > abs_gaps[i - 1])
        ratio = expanding / (len(abs_gaps) - 1)

        info = {
            "ratio": round(ratio, 2),
            "initial_gap": gaps[0],
            "latest_gap": gaps[-1],
            "samples": len(gaps),
        }

        if ratio >= 0.70:
            return "扩大", info
        elif ratio <= 0.30:
            return "缩小", info
        else:
            return "震荡", info
    
    def check_close_trigger(self):
        """平仓逻辑已移至position_monitor.py统一管理"""
        pass
    
    def analyze_and_trade(self, slug, market, extra_info=None):
        """分析并下注（带趋势确认）"""
        coin = market["coin"]

        # ★ P0: 防止狙击线程已入仓的市场再被正常路径重复下单
        if slug in self.positions:
            logger.info(f"  ⚡ 跳过分析: {coin} 已有持仓(狙击线程先入)")
            return
        with self._sniper_lock:
            if slug in self._sniper_processing:
                logger.info(f"  ⚡ 跳过分析: {coin} 狙击线程正在处理")
                return

        # ★ 日亏损上限检查（在任何分析之前，线程安全）
        allowed, daily_pnl, limit = check_daily_loss_limit()
        if not allowed:
            logger.warning(f"  🚫 今日亏损 ${daily_pnl:+.2f} 已达上限 -${limit}，停止交易 [{coin}]")
            return

        up_odds = market["up_odds"]
        down_odds = market["down_odds"]
        gap_trend = extra_info.get("gap_trend", "未知") if extra_info else "未知"
        gap_info = extra_info.get("gap_info", {}) if extra_info else {}
        warmup_n = extra_info.get("warmup_samples", 0) if extra_info else 0

        logger.info(f"\n{'='*60}")
        logger.info(f"🔔 分析市场: {coin} | {slug}")
        logger.info(f"  📊 gap趋势: {gap_trend} | 采样: {warmup_n}个 | latest_gap={gap_info.get('latest_gap')}")
        
        # 获取 PTB（优先使用缓存）
        ptb = self.ptb_cache.get(slug)
        if not ptb:
            logger.warning("  ⚠️ PTB 未缓存，尝试实时获取...")
            ptb = get_ptb_multi_strategy(slug)
            if ptb:
                self.ptb_cache[slug] = ptb
        if not ptb:
            print("  ❌ 无法获取 PTB")
            self.analyzed.add(slug)
            logger.info(f"{'='*60}\n")
            return
        
        self.ptb_cache[slug] = ptb
        print(f"  💰 PTB: ${ptb:,.2f}")
        print(f"  📊 赔率: UP={up_odds:.3f} DOWN={down_odds:.3f}")
        
        # 提前获取 token_id（优先用预热阶段缓存，省~500ms Gamma API调用）
        cached_tokens = self.token_cache.get(slug)
        if cached_tokens:
            up_token, down_token = cached_tokens
        else:
            up_token, down_token = get_token_ids(slug)
        if extra_info is None:
            extra_info = {}
        if up_token and down_token:
            extra_info["up_token"] = up_token
            extra_info["down_token"] = down_token
            # 先用 up_token 做 LMSR 评估（方向确定后会用正确的）
            extra_info["token_id"] = up_token

            # 并行预缓存 neg_risk/fee_rate/tick_size（预热已缓存则跳过）
            clob_client.precache_tokens([up_token, down_token])

            # 预取余额（与CLOB查询并行，分析完直接用，省~250ms）
            from concurrent.futures import ThreadPoolExecutor as _TPE
            _pre_balance_fut = _TPE(max_workers=1).submit(clob_client.get_balance)

            # CLOB 订单簿数据：mid 供参考，best_ask 在 analyze_and_decide 中用于执行价校准
            # Gamma 赔率用于方向/概率判断，CLOB best_ask 用于 EV/折价的执行价校准(C1)
            clob = get_realtime_odds(up_token, down_token)
            if clob["up_mid"] and clob["down_mid"]:
                logger.info(
                    f"  📡 CLOB: UP bid={clob['up_bid']} ask={clob['up_ask']} mid={clob['up_mid']:.3f}"
                    f" | DOWN bid={clob['down_bid']} ask={clob['down_ask']} mid={clob['down_mid']:.3f}"
                )
            else:
                logger.info(f"  📡 CLOB赔率获取失败")
            logger.info(f"  📊 Gamma赔率: UP={up_odds:.3f} DOWN={down_odds:.3f}")

        # AI 分析
        should_bet, direction, confidence, details = analyze_and_decide(
            coin, ptb, up_odds, down_odds, slug, extra_info=extra_info
        )
        
        logger.info(f"  🤖 AI: {direction} | 置信度: {confidence*100:.0f}%")
        # 折价优先用C1校准后的CLOB执行价，无CLOB时fallback到Gamma
        show_discount = details.get('exec_discount', details.get('discount', 0))
        show_price_label = "CLOB" if 'exec_discount' in details else "Gamma"
        exec_price_str = f" | 执行价=${details.get('exec_price',0):.3f}" if 'exec_price' in details else f" | Gamma={details.get('leading_odds',0):.3f}"
        logger.info(f"  💵 折价({show_price_label}): ${show_discount:.3f} | 估值: ${details.get('estimated_value',0):.2f} | ATR偏离: {details.get('diff_in_atr',0):.2f}{exec_price_str}")
        if 'ev_gross' in details:
            logger.info(
                f"  📐 EV拆解: gross={details.get('ev_gross',0):+.3f}"
                f" - spread={details.get('spread_cost',0):.3f}"
                f" - 入场费={details.get('entry_fee_cost',0):.3f}"
                f" - 预期出场费={details.get('expected_exit_fee_cost',0):.3f}"
                f" = {details.get('expected_value',0):+.3f}"
            )
        if details.get("clob_empty_book"):
            logger.info(f"  📡 空簿校准: 执行价=${details.get('exec_price',0):.3f} (原ask=${details.get('clob_raw_ask',0):.3f})")
        
        if not should_bet:
            logger.warning(f"  ❌ 不满足: {details.get('bet_reason','')}")
            # 记录跳过（供波动重触发使用），已经是重分析的不再记录
            is_reanalyze = (extra_info or {}).get("reanalyze", False)
            if not is_reanalyze and slug not in self.positions:
                current_price = details.get("current_price") or details.get("crypto_price")
                if current_price:
                    self.skipped_markets[slug] = {
                        "skip_time": time.time(), "price_at_skip": current_price,
                        "reason": details.get("bet_reason", "no_edge")[:30],
                        "reanalyze_count": 0,
                        "window": "early" if extra_info.get("early_window") else "late",
                    }
            else:
                self.skipped_markets.pop(slug, None)
            # 分析拒绝不加入 analyzed，允许冷却后重新分析
            self.analyzed.discard(slug)
            decrease_cooldown()
            logger.info(f"{'='*60}\n")
            return
        
        logger.info(f"  ✅ 满足下注条件！")

        # 检查最大同时持仓数（锁+预占计数，防止并行线程同时通过检查）
        MAX_OPEN_POSITIONS = int(os.environ.get("MAX_OPEN_POSITIONS", "2"))
        global _pending_bets
        _did_increment_pending = False
        with _position_lock:
            try:
                open_count = 0
                if os.path.exists(POSITIONS_FILE):
                    with open(POSITIONS_FILE) as _f:
                        for _line in _f:
                            _p = json.loads(_line.strip())
                            if not _p.get("closed", False):
                                open_count += 1
                total = open_count + _pending_bets
                if total >= MAX_OPEN_POSITIONS:
                    logger.warning(f"  ⏸️ 持仓{open_count}+下注中{_pending_bets}={total}，达上限{MAX_OPEN_POSITIONS}，跳过")
                    logger.info(f"{'='*60}\n")
                    return
                _pending_bets += 1
                _did_increment_pending = True
            except Exception as _e:
                logger.warning(f"  ⚠️ 检查持仓数失败: {_e}")

        # 检查冷却期：满足下注条件时跳过冷却（EV模型已确认有edge）
        if not should_trade():
            logger.info(f"  ⚠️ 冷却期中，但满足下注条件，跳过冷却直接交易")
            from trading_state import load_state, save_state
            state = load_state()
            state["cooldown_remaining"] = 0
            save_state(state)
        
        # 使用前面已获取的 token_id
        up_token = extra_info.get("up_token")
        down_token = extra_info.get("down_token")
        if not up_token or not down_token:
            logger.error(f"  ❌ 无法获取 token_id")
            self.analyzed.add(slug)
            logger.info(f"{'='*60}\n")
            if _did_increment_pending:
                with _position_lock:
                    _pending_bets = max(0, _pending_bets - 1)
            return

        token_id = up_token if direction == "UP" else down_token

        # P3: 相关性暴露控制 — BTC/ETH 相关性~0.85，同方向仓位减半
        correlation_factor = get_correlated_exposure(direction, coin)
        kelly_reduction = details.get("kelly_reduction", 1.0) * correlation_factor
        if correlation_factor < 1.0:
            logger.info(f"  ⚠️ 相关性控制: 已有同方向持仓，Kelly×{correlation_factor}")

        # 执行下注（传入贝叶斯后验概率 + entry_details + kelly_reduction）
        logger.info(f"  💸 执行下注: {direction} | Token: {token_id[:16]}...")
        from ai_analyze_v2 import execute_bet
        bayesian_info = extra_info.get("bayesian") if extra_info else None
        p_hat = bayesian_info.get("p_hat") if bayesian_info else None
        # P1: 记录反向token_id，供对冲使用
        details["opposite_token_id"] = down_token if direction == "UP" else up_token

        # 取回预取的余额（分析期间已并行获取，此时应已完成）
        _balance = None
        try:
            _balance = _pre_balance_fut.result(timeout=3)
        except Exception:
            pass  # fallback: execute_bet内部自行获取

        # ★ P0: 二次检查 — AI分析耗时数秒，期间狙击线程可能已入仓
        if slug in self.positions:
            logger.info(f"  ⚡ 放弃下单: {coin} 分析期间已被狙击线程入仓")
            if _did_increment_pending:
                with _position_lock:
                    _pending_bets = max(0, _pending_bets - 1)
            return

        try:
            success, entry_price, bet_size, output = execute_bet(
                slug, direction, token_id,
                confidence=confidence,
                ev=details.get('expected_value', 0),
                p_hat=p_hat,
                entry_details=details,
                kelly_reduction=kelly_reduction,
                pre_balance=_balance,
            )

            if success:
                logger.info(f"  ✅ 下注成功！（{bet_size}份）")
                self.analyzed.add(slug)  # 下注成功，阻止晚期窗口重复分析

                # ★ 立即预扣下注成本到 daily_pnl（防止并行线程超限）
                cost = round(entry_price * bet_size, 4)
                new_pnl = record_bet_cost(slug, cost)
                logger.info(f"  📉 预扣成本 ${cost:.2f} → 今日PnL: ${new_pnl:+.2f}")

                # 记录持仓（使用实际下单价格和动态仓位）
                position = Position(
                    slug, token_id, direction, entry_price, bet_size,
                    datetime.now(timezone.utc).isoformat()
                )
                self.positions[slug] = position
                try:
                    clob_client.update_token_allowance(token_id)
                    logger.info("  🔓 入场后刷新卖出allowance")
                except Exception:
                    pass

                # 真实盈亏在 position_monitor close_position 时记录

                # 发送通知
                is_sniper = (extra_info or {}).get("sniper", False)
                send_notification(coin, direction, confidence, details.get('expected_value', 0), entry_price, bet_size, sniper=is_sniper)
            elif isinstance(output, str) and output.startswith("PENDING_GHOST"):
                logger.warning(f"  ⏳ 下单请求超时，成交状态待确认: {output} | 已写入 pending_orders，交由 monitor 对账")
            elif isinstance(output, str) and output.startswith("PENDING"):
                logger.warning(f"  ⏳ 挂单待成交: {output} | 已记录待成交，交由 monitor 对账入仓")
            elif isinstance(output, str) and output.startswith("SKIP_"):
                # 所有 SKIP 类型（余额不足/价格获取失败/价格过高/流动性不足）都不算失败，不影响统计
                retry_n = self.trade_attempts.get(slug, 0)
                self.analyzed.discard(slug)
                logger.warning(f"  ⚠️ {output}，跳过（不计入统计）| 尝试{retry_n}次，等待重试")
            elif isinstance(output, str) and ("Too Early" in output or "not ready" in output or "425" in output):
                # API时序错误（市场未开始接单），不计为交易失败
                self.analyzed.discard(slug)
                logger.warning(f"  ⚠️ API时序错误，跳过（不计入统计）: {output[:100]}")
            else:
                output_str = str(output) if output else ""
                if "fully filled" in output_str or "FOK" in output_str:
                    retry_n = self.trade_attempts.get(slug, 0)
                    self.analyzed.discard(slug)
                    logger.warning(f"  ⚠️ FOK未成交（流动性不足）| 尝试{retry_n}次，等待重试")
                elif "Request exception" in output_str or "status_code=None" in output_str:
                    self.analyzed.discard(slug)
                    logger.warning(f"  ⚠️ 网络超时，跳过（不计入统计）: {output_str[:80]}")
                else:
                    logger.error(f"  ❌ 下注失败: {output_str[:150]}")
                    self.analyzed.add(slug)
                    record_bet_result(False, slug)

            logger.info(f"  📊 {get_state_summary()}")
            print(f"{'='*60}\n")
        finally:
            if _did_increment_pending:
                with _position_lock:
                    _pending_bets = max(0, _pending_bets - 1)
    
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
            from ai_trader.coins import coin_from_slug
            coin = coin_from_slug(slug)
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
                # 退订 Polymarket WS
                tokens = self.token_cache.get(slug)
                if tokens:
                    from ai_trader.polymarket_ws import poly_ws
                    poly_ws.unsubscribe(list(tokens))
                del self.tracked[slug]
                self.ptb_cache.pop(slug, None)
                self.positions.pop(slug, None)
                self.bayesian_updaters.pop(slug, None)
                self._sniper_updaters.pop(slug, None)
                self._sniper_last_reason.pop(slug, None)
                self.token_cache.pop(slug, None)
                self.skipped_markets.pop(slug, None)
                self.trade_attempts.pop(slug, None)
                self.last_analysis_time.pop(slug, None)
                self.warmup_data.pop(slug, None)
                self._ptb_pending.pop(slug, None)
                self.analyzed.discard(slug)
                self.early_analyzed.discard(slug)
                self.restored_slugs.discard(slug)
                # v12.9: 清理伏击挂单（GTC不会自动过期）
                ambush = self._ambush_orders.pop(slug, None)
                if ambush:
                    try:
                        clob_client.cancel_all(ambush["token"])
                    except Exception:
                        pass
                    logger.info(f"  [伏] 市场结束清理伏击单: {slug}")
                self._ambush_cancelled_at.pop(slug, None)


def main():
    global _runtime_max_reanalyze
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    _runtime_max_reanalyze = int(os.environ.get("MAX_REANALYZE", "1"))
    strategy_cfg = get_strategy_config()
    print("🤖 Polymarket 全自动交易机器人 v3.4 启动")
    print("   策略: PTB预获取 → 贝叶斯预热 → 趋势确认下注")
    print("   新增: SDK直连下单(延迟<50ms) | FOK即时平仓 | LMSR流动性评估")
    print(
        "   时间线: "
        f"{PTB_FETCH_START_SECONDS}-40s获取PTB → {strategy_cfg['warmup_start_seconds']}s起预热"
        f" → early {strategy_cfg['early_bet_start']}-{strategy_cfg['early_bet_end']}s"
        f" → late {strategy_cfg['late_bet_start']}-{strategy_cfg['late_bet_end']}s"
    )
    print()

    # 初始化 CLOB SDK 客户端（全局单例，全程复用）
    clob_client.init_client()

    # 启动 Pyth 链上价格流（持仓监控主数据源）
    from ai_trader.pyth_api import pyth_stream
    pyth_stream.start()

    # 启动 Binance WebSocket 实时价格流（K线/ATR + 价格fallback）
    from ai_trader.binance_api import price_stream
    price_stream.start()

    # 启动 Chainlink RTDS 价格流（预热采样 + 决策时价格，与结算同源）
    from ai_trader.polymarket_rtds import chainlink_stream
    chainlink_stream.start()

    # 启动 Polymarket WebSocket 实时 orderbook 流
    from ai_trader.polymarket_ws import poly_ws
    poly_ws.start()

    tracker = MarketTracker()
    # 启动独立狙击监听线程（200ms轮询，比主循环3-5s采样快15倍）
    if os.environ.get("SNIPER_THREAD_ENABLED", "1") == "1":
        tracker.start_sniper_thread()
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
