#!/usr/bin/env python3
"""
持仓止盈监控 - 5分钟市场专用
在市场关闭前的80-100秒窗口内监控价格，达到+15%即止盈
"""
import fcntl
import json
import math
import os
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timezone
from statistics import median
import requests
from ai_trader.polymarket_api import normalize_orderbook, get_price_to_beat_api
from ai_trader import clob_client
from ai_trader.fees import effective_fee_rate, estimate_buy_fill, estimate_sell_fill
from ai_trader.binance_api import price_stream as _price_stream
from ai_trader.pyth_api import pyth_stream as _pyth_stream, get_pyth_price
from ai_trader.polymarket_ws import poly_ws as _poly_ws
from ai_trader.polymarket_rtds import chainlink_stream as _chainlink_stream
from ai_trader.coins import coin_from_slug as _coin_from_slug, get_binance_symbol as _get_binance_symbol

# 价格数据源配置: 1=Chainlink优先(官方结算价), 2=Pyth优先(链上预言机)
PRICE_SOURCE = int(os.environ.get("PRICE_SOURCE", "1"))
from py_clob_client.order_builder.constants import BUY, SELL

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

# .env 必须在读取任何环境变量之前加载
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

POSITIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "positions.jsonl")
POSITIONS_LOCK = POSITIONS_FILE + ".lock"
PROFIT_THRESHOLD = float(os.environ.get("PROFIT_THRESHOLD", "0.15"))
SLIPPAGE = float(os.environ.get("SLIPPAGE", "0.01"))  # 空簿回退滑点（env可配置）

# P0 双曲贴现止盈参数（可通过 .env 覆盖）
P0_BASE_PROFIT = float(os.environ.get("P0_BASE_PROFIT", str(PROFIT_THRESHOLD)))
P0_HYPERBOLIC_K = float(os.environ.get("P0_HYPERBOLIC_K", "0.15"))

# v12.8: Trailing Take-Profit 追踪止盈参数
# 利润达到 P0 阈值后激活追踪，从峰值回撤超过阈值就止盈
TRAILING_TP_ENABLED = os.environ.get("TRAILING_TP_ENABLED", "1") == "1"
TRAILING_TP_DRAWDOWN_HIGH = float(os.environ.get("TRAILING_TP_DRAWDOWN_HIGH", "0.30"))   # 利润>30%时回撤30%止盈
TRAILING_TP_DRAWDOWN_MID = float(os.environ.get("TRAILING_TP_DRAWDOWN_MID", "0.35"))    # 利润>20%时回撤35%止盈
TRAILING_TP_DRAWDOWN_LOW = float(os.environ.get("TRAILING_TP_DRAWDOWN_LOW", "0.40"))    # 利润>15%时回撤40%止盈
TRAILING_TP_TIME_TIGHTEN = float(os.environ.get("TRAILING_TP_TIME_TIGHTEN", "0.5"))     # 剩余时间<120s时回撤容忍度×0.5

# ATR 三层止损/抄底决策参数
PRICE_DROP_TRIGGER = float(os.environ.get("PRICE_DROP_TRIGGER", "0.15"))
PRICE_DROP_HARD_STOP = float(os.environ.get("PRICE_DROP_HARD_STOP", "0.25"))
# v12.8: 无条件绝对硬止损 — 不看方向、不看EV、不看ATR，跌到就走
ABSOLUTE_HARD_STOP = float(os.environ.get("ABSOLUTE_HARD_STOP", "0.30"))
ATR_SAFE_THRESHOLD = float(os.environ.get("ATR_SAFE_THRESHOLD", "2.0"))
ATR_DANGER_THRESHOLD = float(os.environ.get("ATR_DANGER_THRESHOLD", "1.0"))
DIP_BUY_SIZE_RATIO = float(os.environ.get("DIP_BUY_SIZE_RATIO", "0.50"))
DIP_BUY_MIN_REMAINING = float(os.environ.get("DIP_BUY_MIN_REMAINING", "60"))

# 伏击仓位持有到期（跳过中途止损，只保留绝对硬止损兜底）
AMBUSH_HOLD_TO_EXPIRY = os.environ.get("AMBUSH_HOLD_TO_EXPIRY", "1") == "1"
AMBUSH_HARD_STOP = float(os.environ.get("AMBUSH_HARD_STOP", "0.70"))  # 伏击仓位绝对硬止损阈值

# v14: 分阶段时间衰减止盈止损
AMBUSH_TP_PHASE1_RATIO = float(os.environ.get("AMBUSH_TP_PHASE1_RATIO", "0.60"))
AMBUSH_TP_PHASE2_RATIO = float(os.environ.get("AMBUSH_TP_PHASE2_RATIO", "0.55"))
AMBUSH_TP_PHASE3_RATIO = float(os.environ.get("AMBUSH_TP_PHASE3_RATIO", "0.40"))
AMBUSH_TP_PHASE4_FIXED = float(os.environ.get("AMBUSH_TP_PHASE4_FIXED", "0.05"))
AMBUSH_RAPID_SL = float(os.environ.get("AMBUSH_RAPID_SL", "0.25"))
AMBUSH_MID_SL = float(os.environ.get("AMBUSH_MID_SL", "0.35"))
AMBUSH_NOISE_TOLERANCE = float(os.environ.get("AMBUSH_NOISE_TOLERANCE", "0.10"))  # v14.3: 方向✅时额外容忍度
AMBUSH_LATE_SL = float(os.environ.get("AMBUSH_LATE_SL", "0.30"))
AMBUSH_RAPID_SL_WINDOW = int(os.environ.get("AMBUSH_RAPID_SL_WINDOW", "30"))
AMBUSH_BREAKEVEN_TRIGGER = float(os.environ.get("AMBUSH_BREAKEVEN_TRIGGER", "0.15"))
AMBUSH_BREAKEVEN_MARGIN = float(os.environ.get("AMBUSH_BREAKEVEN_MARGIN", "0.02"))
AMBUSH_CONFIRM_WINDOW = int(os.environ.get("AMBUSH_CONFIRM_WINDOW", "15"))
AMBUSH_CONFIRM_THRESHOLD = float(os.environ.get("AMBUSH_CONFIRM_THRESHOLD", "0.0003"))

# PTB Proximity Buffer（临近PTB时冻结方向信号，防止噪音触发止损）
PTB_PROXIMITY_ATR = float(os.environ.get("PTB_PROXIMITY_ATR", "0.7"))
PTB_PROXIMITY_EXTREME_STOP = float(os.environ.get("PTB_PROXIMITY_EXTREME_STOP", "0.50"))

# ATR 衰减止损（crypto实时价格驱动，不依赖token价格）
ATR_DECAY_EXIT_THRESHOLD = float(os.environ.get("ATR_DECAY_EXIT_THRESHOLD", "0.5"))  # ATR降到此值以下触发
ATR_DECAY_RATIO = float(os.environ.get("ATR_DECAY_RATIO", "0.30"))  # ATR降到入场值的此比例以下触发
ATR_DIRECTION_CORRECT_STOP = float(os.environ.get("ATR_DIRECTION_CORRECT_STOP", "0.5"))  # 方向正确但ATR<此值+token跌>20%也止损
ATR_DECAY_CONFIRMATIONS = int(os.environ.get("ATR_DECAY_CONFIRMATIONS", "3"))  # 需连续确认，防止单点插针误杀
ENABLE_DIRECTION_DOWNGRADE = os.environ.get("ENABLE_DIRECTION_DOWNGRADE", "0").lower() in ("1", "true", "yes", "on")
ATR_DOWNGRADE_THRESHOLD = float(os.environ.get("ATR_DOWNGRADE_THRESHOLD", "0.15"))

# 尾盘（<=60s）改用 oracle 确认状态机，避免单个 Chainlink tick 插针误杀
TAIL_ORACLE_WINDOW = 7
TAIL_ORACLE_MIN_SAMPLES = 3
TAIL_ORACLE_ENTER_ATR_FLOOR = 0.25
TAIL_ORACLE_RECOVER_RATIO = 0.5
TAIL_ORACLE_VOL_MULT = 3.0
TAIL_ORACLE_PRICE_BPS_FLOOR = 0.00005

# 快市场领先观测（仅告警，不改变任何交易决策）
ENABLE_ORACLE_STALE_WATCH = os.environ.get("ENABLE_ORACLE_STALE_WATCH", "1").lower() in ("1", "true", "yes", "on")
ORACLE_STALE_WATCH_MAX_REMAINING = float(os.environ.get("ORACLE_STALE_WATCH_MAX_REMAINING", "60"))
ORACLE_STALE_WATCH_ATR = float(os.environ.get("ORACLE_STALE_WATCH_ATR", "0.5"))
ORACLE_STALE_WATCH_CONFIRMATIONS = int(os.environ.get("ORACLE_STALE_WATCH_CONFIRMATIONS", "3"))

# ── EV-Gate 止损（v11）──
ENABLE_EV_GATE = os.environ.get("ENABLE_EV_GATE", "1").lower() in ("1", "true", "yes", "on")
EV_EXIT_CONFIRMATIONS = int(os.environ.get("EV_EXIT_CONFIRMATIONS", "2"))
EV_P_WIN_CAP = float(os.environ.get("EV_P_WIN_CAP", "0.95"))
EV_P_WIN_FLOOR = float(os.environ.get("EV_P_WIN_FLOOR", "0.02"))
EV_ATR_SIGMA_RATIO = float(os.environ.get("EV_ATR_SIGMA_RATIO", "1.5"))
EV_CIRCUIT_BREAKER_LOSS = float(os.environ.get("EV_CIRCUIT_BREAKER_LOSS", "0.70"))
EV_CIRCUIT_BREAKER_BLIND_SECS = float(os.environ.get("EV_CIRCUIT_BREAKER_BLIND_SECS", "120"))

# 狙击入场保护期：狙击线程入场后 N 秒内 EV-Gate 不生效（电路断路器仍生效）
SNIPER_GRACE_SECONDS = float(os.environ.get("SNIPER_GRACE_SECONDS", "8"))

# BN 预警加速：当 Binance 检测到快速不利移动时，折扣 P(win)
BN_EW_VELOCITY_ATR = 0.3   # BN 每秒移动 ≥ 0.3ATR 触发
BN_EW_DISCOUNT = 0.80      # P(win) × 0.80
BN_EW_MIN_SAMPLES = 3      # 至少3个快照
BN_EW_WINDOW_SEC = 3.0     # 3秒窗口

def calc_proximity_threshold(remaining):
    """proximity zone 宽度随时间衰减：越接近到期 buffer 越窄"""
    if remaining > 180:
        return PTB_PROXIMITY_ATR           # 0.7 ATR（前2分钟完整buffer）
    elif remaining > 60:
        return 0.3 + 0.4 * (remaining - 60) / 120  # 0.7→0.3 线性衰减
    else:
        return 0.15                        # 最后1分钟极窄


def should_arm_atr_decay_exit(diff_atr, entry_atr, profit_rate):
    """ATR衰减止损只在确实逼近PTB且token已显著亏损时进入待确认状态。"""
    if diff_atr is None or entry_atr is None or entry_atr <= 0:
        return False
    if profit_rate is None or profit_rate >= -0.05:
        return False
    if diff_atr >= ATR_DECAY_EXIT_THRESHOLD:
        return False
    return (diff_atr / entry_atr) < ATR_DECAY_RATIO


def should_release_proximity_guard(profit_rate, remaining, wrong_streak, wrong_ratio):
    """决定 proximity 保护是否解除。深亏时加快解除，但不再单点立即失效。"""
    if remaining > 120:
        extreme_stop = PTB_PROXIMITY_EXTREME_STOP
    elif remaining > 60:
        extreme_stop = PTB_PROXIMITY_EXTREME_STOP - 0.05
    else:
        extreme_stop = PTB_PROXIMITY_EXTREME_STOP - 0.10

    if profit_rate is not None and profit_rate <= -extreme_stop:
        return True, extreme_stop, 0

    streak_threshold = 2 if profit_rate is not None and profit_rate <= -PRICE_DROP_HARD_STOP else 4
    released = wrong_streak >= streak_threshold or wrong_ratio >= 0.75
    return released, extreme_stop, streak_threshold


def evaluate_proximity_guard(direction_correct, true_direction_correct, diff_atr, remaining,
                             profit_rate, wrong_streak=0, direction_history=None):
    """预判 proximity guard 是否应冻结方向，供伏击止损和通用保护共用。"""
    result = {
        "freeze": False,
        "in_proximity": False,
        "released": False,
        "release_by_extreme_stop": False,
        "prox_threshold": None,
        "projected_streak": wrong_streak,
        "wrong_ratio": 0.0,
        "extreme_stop": None,
        "streak_threshold": 0,
    }
    if direction_correct is not False or true_direction_correct is not False or diff_atr is None:
        return result

    prox_threshold = calc_proximity_threshold(remaining)
    result["prox_threshold"] = prox_threshold
    if diff_atr >= prox_threshold:
        return result

    result["in_proximity"] = True
    projected_streak = wrong_streak + 1
    history = list(direction_history or [])
    history.append(False)
    projected_hist = history[-8:]
    wrong_ratio = (
        sum(1 for d in projected_hist if d is False) / len(projected_hist)
        if len(projected_hist) >= 4 else 0
    )
    released, extreme_stop, streak_threshold = should_release_proximity_guard(
        profit_rate, remaining, projected_streak, wrong_ratio
    )
    result.update({
        "released": released,
        "projected_streak": projected_streak,
        "wrong_ratio": wrong_ratio,
        "extreme_stop": extreme_stop,
        "streak_threshold": streak_threshold,
    })
    if released and profit_rate is not None and profit_rate <= -extreme_stop:
        result["release_by_extreme_stop"] = True
        return result
    if not released:
        result["freeze"] = True
    return result


def should_direction_downgrade(direction_correct, remaining, diff_atr, profit_rate):
    """仅在显式开启时，才允许用 token 风险去降级仍在正确侧的底层方向。"""
    if not ENABLE_DIRECTION_DOWNGRADE:
        return False
    if direction_correct is not True:
        return False
    if remaining < 60:
        return False
    if diff_atr is None or diff_atr >= ATR_DOWNGRADE_THRESHOLD:
        return False
    return profit_rate is not None and profit_rate <= -PRICE_DROP_HARD_STOP


def get_direction_flip_required_confirms(diff_atr, remaining):
    """小ATR且离结算还早时，要求更多连续确认，避免在PTB附近被噪音扫掉。"""
    if diff_atr is not None and diff_atr >= 1.5:
        return 1
    if diff_atr is not None and diff_atr >= 1.0:
        return 2
    if remaining > 120:
        return 4
    if remaining > 60:
        return 3
    return 2


def calc_signed_direction_gap(direction, crypto_price, ptb_price):
    """返回带方向的 gap: >0 表示在正确侧，<0 表示在错误侧。"""
    if crypto_price is None or ptb_price is None:
        return None
    if direction == "UP":
        return crypto_price - ptb_price
    if direction == "DOWN":
        return ptb_price - crypto_price
    return None


def get_tail_oracle_confirmations(remaining):
    """尾盘越接近结算，要求更多 oracle 连续确认。"""
    if remaining <= 30:
        return 3
    if remaining <= 60:
        return 2
    return 1


def classify_tail_oracle_state(direction, ptb_price, atr_val, recent_prices, remaining, wrong_streak=0):
    """
    尾盘 oracle-only 状态机:
    - 最近价格用 median 去单点 spike
    - 用 MAD + ATR floor 做 hysteresis
    - 只有 confirmed wrong 才允许把方向视为错误
    """
    inactive = {
        "active": False,
        "state": "inactive",
        "wrong_signal": False,
        "effective_direction_correct": None,
        "raw_direction_correct": None,
        "wrong_streak": 0,
        "required_confirms": get_tail_oracle_confirmations(remaining),
        "median_gap": None,
        "enter_margin": None,
        "recover_margin": None,
        "mad_gap": None,
    }
    if remaining > 60 or direction not in ("UP", "DOWN") or ptb_price is None:
        return inactive

    gaps = []
    for price in recent_prices or []:
        gap = calc_signed_direction_gap(direction, price, ptb_price)
        if gap is not None:
            gaps.append(gap)
    if not gaps:
        return inactive

    required_confirms = get_tail_oracle_confirmations(remaining)
    latest_gap = gaps[-1]
    if len(gaps) < TAIL_ORACLE_MIN_SAMPLES:
        return {
            "active": True,
            "state": "warming",
            "wrong_signal": False,
            "effective_direction_correct": True,
            "raw_direction_correct": True,
            "wrong_streak": 0,
            "required_confirms": required_confirms,
            "median_gap": latest_gap,
            "enter_margin": None,
            "recover_margin": None,
            "mad_gap": None,
        }

    median_gap = median(gaps)
    deviations = [abs(gap - median_gap) for gap in gaps]
    mad_gap = median(deviations) if deviations else 0.0
    atr_floor = atr_val * TAIL_ORACLE_ENTER_ATR_FLOOR if atr_val and atr_val > 0 else 0.0
    price_floor = abs(ptb_price) * TAIL_ORACLE_PRICE_BPS_FLOOR
    enter_margin = max(mad_gap * TAIL_ORACLE_VOL_MULT, atr_floor, price_floor)
    recover_margin = max(enter_margin * TAIL_ORACLE_RECOVER_RATIO, price_floor * TAIL_ORACLE_RECOVER_RATIO)

    wrong_signal = median_gap <= -enter_margin
    correct_signal = median_gap >= recover_margin
    if wrong_signal:
        next_streak = wrong_streak + 1
    elif correct_signal:
        next_streak = 0
    else:
        next_streak = max(0, wrong_streak - 1)

    if wrong_signal and next_streak >= required_confirms:
        state = "wrong_confirmed"
    elif wrong_signal:
        state = "wrong_pending"
    elif correct_signal:
        state = "correct"
    else:
        state = "noise"

    return {
        "active": True,
        "state": state,
        "wrong_signal": wrong_signal,
        "effective_direction_correct": state != "wrong_confirmed",
        "raw_direction_correct": not wrong_signal,
        "wrong_streak": next_streak,
        "required_confirms": required_confirms,
        "median_gap": median_gap,
        "enter_margin": enter_margin,
        "recover_margin": recover_margin,
        "mad_gap": mad_gap,
    }

# 链上余额预缓存（止损时避免临时查链增加延迟）
# {token_id: (balance, timestamp)}
_balance_cache = {}
BALANCE_CACHE_TTL = 8  # 缓存有效期(秒)，观望区每轮刷新

# v14.3: TP单状态缓存 — zone-based TTL 避免每轮迭代都 HTTP 查询
# {order_id: (info_dict, fetch_time)}
_tp_status_cache = {}
_TP_CACHE_COLD = float(os.environ.get("TP_CACHE_COLD_TTL", "5.0"))   # 冷区: 5s
_TP_CACHE_WARM = float(os.environ.get("TP_CACHE_WARM_TTL", "2.0"))   # 暖区: 2s

# v14.3: TP重挂节流 — 避免高频 cancel+place 导致 TP 空窗
_TP_REBUILD_MIN_INTERVAL = float(os.environ.get("TP_REBUILD_MIN_INTERVAL", "5.0"))
_last_tp_rebuild = {}  # attempt_key -> timestamp


_OUTCOMES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "outcomes.jsonl")
_OUTCOMES_LOCK = _OUTCOMES_PATH + ".lock"
_OUTCOMES_SCAN_INTERVAL = 60  # 定期回填间隔(秒)
_last_outcomes_scan = 0


def _periodic_outcomes_backfill():
    """v14.3: 定期扫描 outcomes.jsonl，为已结算市场回填 directional_won

    修复四个问题:
    1. SL/TP提前退出的仓位在市场结算后也能被回填(不依赖open positions)
    2. 只对官方结算(get_market_outcome返回0/1)标记 calibration_eligible=True
    3. 覆盖所有结算分支(periodic scan独立于settlement代码路径)
    4. 用 fcntl 文件锁防止与 record_outcome append 竞争
    """
    global _last_outcomes_scan
    now = time.time()
    if now - _last_outcomes_scan < _OUTCOMES_SCAN_INTERVAL:
        return
    _last_outcomes_scan = now

    if not os.path.exists(_OUTCOMES_PATH):
        return

    # Step 1: 找出 directional_won=null 的 slug(只看最近24h)
    from datetime import timedelta
    cutoff_str = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    pending_slugs = set()
    try:
        with open(_OUTCOMES_PATH, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("directional_won") is not None:
                    continue
                ts = rec.get("timestamp", "")
                if ts < cutoff_str:
                    continue
                slug = rec.get("slug", "")
                if slug:
                    pending_slugs.add(slug)
    except Exception:
        return

    if not pending_slugs:
        return

    # Step 2: 通过 Gamma API 获取官方结算结果(只有0.00/1.00)
    resolved = {}  # slug -> settle_price (UP视角: 1.00=UP赢, 0.00=DOWN赢)
    for slug in pending_slugs:
        try:
            settle = get_market_outcome(slug, "UP")  # UP视角统一查询
            if settle is not None:
                resolved[slug] = settle
        except Exception:
            continue

    if not resolved:
        return

    # Step 3: 在文件锁下回填
    lock_fd = None
    try:
        lock_fd = open(_OUTCOMES_LOCK, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        lines = []
        updated = 0
        with open(_OUTCOMES_PATH, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    lines.append(line)
                    continue
                slug = rec.get("slug", "")
                if slug in resolved and rec.get("directional_won") is None:
                    up_won = resolved[slug] > 0.5  # UP视角
                    rec_dir = rec.get("direction", "UP")
                    rec["directional_won"] = up_won if rec_dir == "UP" else not up_won
                    rec["calibration_eligible"] = True
                    updated += 1
                lines.append(json.dumps(rec, ensure_ascii=False))

        if updated > 0:
            _tmp = _OUTCOMES_PATH + ".tmp"
            with open(_tmp, "w") as f:
                f.write("\n".join(lines) + "\n")
            os.rename(_tmp, _OUTCOMES_PATH)  # atomic rename: 读侧永远看到完整文件
            print(f"  📊 定期回填 directional_won: {updated}条 ({', '.join(resolved.keys())})")
    except Exception as e:
        print(f"  ⚠️ outcomes 回填失败: {e}")
    finally:
        if lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()


def _get_tp_status(tp_oid, ttl=5.0):
    """带 TTL 缓存的 TP 状态查询"""
    cached = _tp_status_cache.get(tp_oid)
    if cached and (time.time() - cached[1]) < ttl:
        return cached[0]
    try:
        info = clob_client.get_order(tp_oid)
        _tp_status_cache[tp_oid] = (info, time.time())
        return info
    except Exception:
        return cached[0] if cached else None

# Telegram 通知配置

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def _safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _size_step():
    step = _safe_float(os.environ.get("SIZE_STEP")) or 0.1
    return step if step > 0 else 0.1


def _round_size(value, mode="nearest", step=None):
    value = max(_safe_float(value) or 0.0, 0.0)
    step = step or _size_step()
    if step <= 0:
        return round(value, 6)

    units = value / step
    if mode == "ceil":
        rounded_units = math.ceil(units - 1e-9)
    elif mode == "floor":
        rounded_units = math.floor(units + 1e-9)
    else:
        rounded_units = round(units)
    return round(rounded_units * step, 6)


def _buy_fill_ratio(price, fee_rate_bps):
    fee_rate = effective_fee_rate(price, fee_rate_bps)
    return max(1.0 - fee_rate, 1e-9), fee_rate


def _plan_dip_buy_size(token_id, original_size, buy_price):
    original_net_size = max(_safe_float(original_size) or 0.0, 0.0)
    if original_net_size <= 0 or buy_price <= 0:
        return {
            "gross_order_size": 0.0,
            "target_net_size": 0.0,
            "expected_net_size": 0.0,
            "min_gross_size": 0.0,
            "fee_rate_bps": 0,
            "entry_fee_rate": 0.0,
            "forced_to_min": False,
            "skip_reason": "NON_POSITIVE_TARGET",
        }

    min_net = float(os.environ.get("MIN_BET_SIZE", "5"))
    max_net = float(os.environ.get("MAX_BET_SIZE", "10"))
    size_step = _size_step()
    fee_rate_bps = clob_client.get_fee_rate_bps(token_id)
    fill_ratio, entry_fee_rate = _buy_fill_ratio(buy_price, fee_rate_bps)
    raw_net_size = original_net_size * DIP_BUY_SIZE_RATIO
    forced_to_min = 0 < raw_net_size < min_net

    max_net_by_balance = None
    try:
        balance = _safe_float(clob_client.get_balance())
    except Exception:
        balance = None
    if balance is not None and buy_price > 0:
        max_gross_by_balance = _round_size(balance / buy_price, mode="floor", step=size_step)
        max_net_by_balance = max_gross_by_balance * fill_ratio
    else:
        max_gross_by_balance = None

    hard_net_cap = max_net
    if max_net_by_balance is not None:
        hard_net_cap = min(hard_net_cap, max_net_by_balance)
    if hard_net_cap < min_net:
        return {
            "gross_order_size": 0.0,
            "target_net_size": 0.0,
            "expected_net_size": 0.0,
            "min_gross_size": _round_size(min_net / fill_ratio, mode="ceil", step=size_step),
            "fee_rate_bps": fee_rate_bps,
            "entry_fee_rate": round(entry_fee_rate, 6),
            "forced_to_min": forced_to_min,
            "skip_reason": "HARD_CAP_BELOW_MIN",
        }

    target_net_size = min(max_net, max(raw_net_size, min_net))
    if not forced_to_min:
        target_net_size = min(max_net, raw_net_size)
    target_net_size = min(target_net_size, hard_net_cap)

    gross_order_size = _round_size(target_net_size / fill_ratio, mode="ceil", step=size_step)
    min_gross_size = _round_size(min_net / fill_ratio, mode="ceil", step=size_step)
    if max_gross_by_balance is not None and gross_order_size > max_gross_by_balance + 1e-9:
        gross_order_size = max_gross_by_balance
    expected_net_size = gross_order_size * fill_ratio
    if expected_net_size < min_net:
        return {
            "gross_order_size": 0.0,
            "target_net_size": round(target_net_size, 6),
            "expected_net_size": round(expected_net_size, 6),
            "min_gross_size": min_gross_size,
            "fee_rate_bps": fee_rate_bps,
            "entry_fee_rate": round(entry_fee_rate, 6),
            "forced_to_min": forced_to_min,
            "skip_reason": "EXPECTED_NET_BELOW_MIN",
        }

    return {
        "gross_order_size": gross_order_size,
        "target_net_size": round(target_net_size, 6),
        "expected_net_size": round(expected_net_size, 6),
        "min_gross_size": min_gross_size,
        "fee_rate_bps": fee_rate_bps,
        "entry_fee_rate": round(entry_fee_rate, 6),
        "forced_to_min": forced_to_min,
        "skip_reason": None,
    }


def _position_size(position):
    """当前持有份数（监控用）— 信任链上余额，含 0"""
    token_balance = _safe_float(position.get("token_balance")) if isinstance(position, dict) else None
    if token_balance is not None:
        return max(token_balance, 0.0)
    size = _safe_float(position.get("size")) if isinstance(position, dict) else None
    return size or 0.0


def _realized_trade_size(position):
    """原始入场总份数（outcome 记录用）— 优先 entry_cost/entry_price 回推"""
    entry_cost = _safe_float(position.get("entry_cost"))
    entry_price = _safe_float(position.get("entry_price"))
    if entry_cost and entry_price and entry_price > 0:
        return round(entry_cost / entry_price, 6)
    # 回退: size + partial_sizes
    size = _safe_float(position.get("size")) or 0.0
    partial_total = sum(
        _safe_float(p.get("size")) or 0.0
        for p in (position.get("partial_exits", []) or [])
    )
    return round(size + partial_total, 6)


def _sell_fill_summary(token_id, size, gross_proceeds, gross_price):
    fee_rate_bps = clob_client.get_fee_rate_bps(token_id)
    return estimate_sell_fill(
        price=gross_price,
        size=size,
        fee_rate_bps=fee_rate_bps,
        gross_proceeds=gross_proceeds,
    )


def send_telegram(text):
    """发送 Telegram 通知"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
    except Exception:
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

def _get_orderbook_bids_asks(token_id):
    """获取订单簿 — WS book snapshot 优先，REST 回退

    v12.8: WS 有完整 book snapshot 时直接用（0ms），避免 REST ~300ms。
    """
    # WS book snapshot（含完整深度）
    ws_book = _poly_ws.get_book_snapshot(token_id)
    if ws_book and ws_book.get("bids") and ws_book.get("asks"):
        return normalize_orderbook(ws_book["bids"], ws_book["asks"])
    # REST 回退
    try:
        book = clob_client.get_orderbook(token_id)
        if book and book.bids is not None and book.asks is not None:
            raw_bids = [{"price": b.price, "size": b.size} for b in book.bids]
            raw_asks = [{"price": a.price, "size": a.size} for a in book.asks]
            return normalize_orderbook(raw_bids, raw_asks)
    except Exception:
        pass
    return [], []

def get_market_price(token_id):
    """获取当前市场价格 — 多源融合（WS优先 → SDK回退）

    v12.8: WS 数据新鲜(age<2s)时跳过所有 REST 调用，省 ~300ms。
    """
    best_bid = None
    best_ask = None
    # 方案0：Polymarket WebSocket 实时推送（0ms延迟）
    try:
        ws_bid, ws_ask = _poly_ws.get_best_bid_ask(token_id)
        if ws_bid is not None and ws_ask is not None:
            spread = ws_ask - ws_bid
            mid = (ws_bid + ws_ask) / 2
            if mid > 0 and spread / mid < 0.50:
                return round(mid, 4)
        best_bid = ws_bid
        best_ask = ws_ask
        # WS 有部分数据但不完整，用单边
        if best_bid and not best_ask:
            return best_bid
        if best_ask and not best_bid:
            return best_ask
    except Exception:
        pass
    # WS 新鲜但无有效 bid/ask → 跳过慢 REST，直接用 LTP
    ws_snap = _poly_ws.get_best_bid_ask_snapshot(token_id)
    if ws_snap and ws_snap.get("age_ms", 9999) < 50:
        ltp = get_last_trade_price(token_id)
        if ltp:
            return ltp
        return best_bid or best_ask
    # WS 不新鲜，降级 REST
    # 方案1：SDK订单簿
    try:
        bids, asks = _get_orderbook_bids_asks(token_id)
        best_bid = float(bids[0]['price']) if bids else best_bid
        best_ask = float(asks[0]['price']) if asks else best_ask
        if best_bid and best_ask:
            spread = best_ask - best_bid
            mid = (best_bid + best_ask) / 2
            if mid > 0 and spread / mid < 0.50:
                return round(mid, 4)
    except Exception:
        pass
    # 方案2：SDK midpoint
    mid = clob_client.get_midpoint(token_id)
    if mid and mid > 0:
        return mid
    # 方案3：SDK last-trade-price
    ltp = get_last_trade_price(token_id)
    if ltp:
        return ltp
    # 方案4：只有 bid 或 ask
    if best_bid:
        return best_bid
    if best_ask:
        return best_ask
    return None

def get_best_bid_raw(token_id):
    """获取原始最佳买价（止损专用，WS优先 → SDK回退）

    v12.8: WS 新鲜时跳过 REST orderbook 调用。
    """
    # WS 实时推送
    ws_bid = _poly_ws.get_best_bid(token_id)
    if ws_bid is not None and ws_bid > 0.02:
        return ws_bid
    # WS 新鲜但无 bid → 用 LTP，跳过 REST
    ws_snap = _poly_ws.get_best_bid_ask_snapshot(token_id)
    if ws_snap and ws_snap.get("age_ms", 9999) < 50:
        ltp = get_last_trade_price(token_id)
        if ltp:
            return ltp
        return None
    # WS 不新鲜，SDK REST 回退
    try:
        bids, _ = _get_orderbook_bids_asks(token_id)
        if bids:
            best_bid = float(bids[0]['price'])
            if best_bid > 0.02:
                return best_bid
    except Exception:
        pass
    ltp = get_last_trade_price(token_id)
    if ltp:
        print(f"    📡 get_best_bid_raw空簿回退: last_trade=${ltp:.3f}")
        return ltp
    return None


def get_best_bid(token_id):
    """获取最佳买价（用于卖出，WS优先 → SDK回退）

    v12.8: WS 新鲜时跳过 REST orderbook 调用。
    """
    # WS 实时推送
    ws_bid = _poly_ws.get_best_bid(token_id)
    if ws_bid is not None and ws_bid > 0.02:
        return ws_bid * 0.99
    # WS 新鲜但无 bid → 用 LTP，跳过 REST
    ws_snap = _poly_ws.get_best_bid_ask_snapshot(token_id)
    if ws_snap and ws_snap.get("age_ms", 9999) < 50:
        ltp = get_last_trade_price(token_id)
        if ltp:
            fallback = max(round(ltp - SLIPPAGE, 2), 0.01)
            return fallback
        return None
    # WS 不新鲜，SDK REST 回退
    try:
        bids, _ = _get_orderbook_bids_asks(token_id)
        if bids:
            best_bid = float(bids[0]['price'])
            if best_bid > 0.02:
                return best_bid * 0.99
    except Exception:
        pass
    ltp = get_last_trade_price(token_id)
    if ltp:
        fallback = max(round(ltp - SLIPPAGE, 2), 0.01)
        print(f"    📡 get_best_bid空簿回退: last_trade=${ltp:.3f} → bid=${fallback:.2f}")
        return fallback
    return None

def get_best_ask(token_id):
    """获取最佳卖价（用于买入对冲，WS优先 → SDK回退）

    v12.8: WS 新鲜时跳过 REST orderbook 调用。
    """
    # WS 实时推送
    ws_ask = _poly_ws.get_best_ask(token_id)
    if ws_ask is not None and ws_ask < 0.95:
        return ws_ask
    # WS 新鲜但无 ask → 用 LTP，跳过 REST
    ws_snap = _poly_ws.get_best_bid_ask_snapshot(token_id)
    if ws_snap and ws_snap.get("age_ms", 9999) < 50:
        ltp = get_last_trade_price(token_id)
        if ltp:
            fallback = min(round(ltp + SLIPPAGE, 2), 0.99)
            return fallback
        return None
    # WS 不新鲜，SDK REST 回退
    try:
        _, asks = _get_orderbook_bids_asks(token_id)
        if asks:
            ask = float(asks[0]['price'])
            if ask < 0.95:
                return ask
    except Exception:
        pass
    ltp = get_last_trade_price(token_id)
    if ltp:
        fallback = min(round(ltp + SLIPPAGE, 2), 0.99)
        print(f"    📡 get_best_ask空簿回退: last_trade=${ltp:.3f} → ask=${fallback:.2f}")
        return fallback
    return None

def get_last_trade_price(token_id):
    """获取最近成交价（SDK直连）"""
    price = _safe_float(clob_client.get_last_trade_price(token_id))
    if price is not None and 0.01 < price < 0.99:
        return price
    return None

def buy_opposite_token(token_id, size, price, max_retries=2):
    """买入对冲token（FOK即时成交）
    返回: (success, output, actual_price)
    """
    price = round(price, 2)
    for attempt in range(max_retries):
        info = clob_client.place_fok_order(token_id, BUY, price, size)
        if info["matched"]:
            actual_price = round(info["making"] / size, 4) if size > 0 and info["making"] > 0 else price
            print(f"    📊 对冲成交: Status={info['status']} | 实际价=${actual_price:.4f} | {info.get('elapsed_ms', 0):.0f}ms")
            return True, info["raw"], actual_price
        if info.get("status") == "ERROR":
            return False, info.get("error", ""), None
        # FOK未成交，降价重试
        if attempt < max_retries - 1:
            price = round(price + 0.01, 2)  # 提高出价
    return False, "All retries failed", None

def analyze_liquidity(token_id, target_size):
    """分析订单簿流动性（SDK直连）"""
    try:
        bids, _ = _get_orderbook_bids_asks(token_id)
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


def _prefetch_balance(token_id):
    """预取链上余额并缓存，供止损时直接使用（避免临时查链增加延迟）"""
    real_balance = get_token_balance(token_id)
    if real_balance is not None:
        _balance_cache[token_id] = (real_balance, time.time())
    return real_balance


def _check_and_adjust_size(token_id, size, position=None):
    """校验链上真实余额，返回调整后的 size。
    返回: adjusted_size (>0正常, 0=余额为零, None=查询失败用原size)
    如传入 position 且余额不一致，会同步更新 positions.jsonl
    优先使用预缓存余额（TTL内），避免止损时额外网络延迟。
    """
    # 优先用缓存（观望区已预取）
    cached = _balance_cache.get(token_id)
    if cached and (time.time() - cached[1]) < BALANCE_CACHE_TTL:
        real_balance = cached[0]
    else:
        real_balance = get_token_balance(token_id)
        if real_balance is not None:
            _balance_cache[token_id] = (real_balance, time.time())
    if real_balance is not None:
        if real_balance <= 0:
            # Fix: 持仓刚创建时 CLOB balance API 可能有延迟，不要立即判定为0
            if position is not None:
                entry_time = position.get("entry_time")
                if entry_time:
                    try:
                        et = datetime.fromisoformat(entry_time.replace("Z", "+00:00"))
                        age_sec = (datetime.now(timezone.utc) - et).total_seconds()
                        if age_sec < 30:
                            print(f"    ⚠️ 链上余额为0但持仓仅{age_sec:.0f}s，可能API延迟，等下一轮重试")
                            _balance_cache.pop(token_id, None)  # 清缓存，下轮重新查
                            return -1  # -1 = 余额疑似延迟，调用方应跳过本轮不关仓
                    except Exception:
                        pass
            print(f"    ⚠️ 链上余额为0，跳过卖出")
            return 0
        if real_balance < size:
            adjusted = math.floor(real_balance * 100) / 100.0
            print(f"    ⚠️ 链上余额({real_balance})< 记录size({size})，用真实余额卖出({adjusted})")
            # 同步更新持仓记录，防止后续循环反复触发不一致
            if position is not None:
                position["token_balance"] = adjusted
                update_position(position, new_size=adjusted)
            return adjusted if adjusted > 0 else 0
        return size
    return None

def _estimate_exit_price(token_id, current_price, entry_price):
    """NO_BALANCE时估算退出价（避免用0导致虚假全额亏损）
    优先级: current_price → LTP → best_bid → entry_price(保本)
    """
    current_price = _safe_float(current_price)
    entry_price = _safe_float(entry_price) or 0
    if current_price is not None and current_price > 0:
        return current_price
    ltp = get_last_trade_price(token_id)
    if ltp and ltp > 0.01:
        return ltp
    bid = get_best_bid_raw(token_id)
    if bid and bid > 0.01:
        return bid
    # 无法获取任何价格 → 用入场价（PnL=0），避免虚假全额亏损
    return entry_price if entry_price > 0 else 0



def _resolve_or_estimate_exit_price(token_id, current_price, entry_price, slug, direction):
    """NO_BALANCE时确定退出价：优先查结算结果(1.0/0.0)，未结算则回退到CLOB估算。
    当 redeem_resolved.py 已赎回token时，余额为零但市场已结算，应按结算价计PnL。
    """
    try:
        settle = get_market_outcome(slug, direction)
        if settle is not None:
            return settle
    except Exception:
        pass
    return _estimate_exit_price(token_id, current_price, entry_price)


def _estimate_ghost_price(token_id, caller_price):
    """幽灵成交时估算实际成交价（CLOB按best_bid撮合，不是地板价）"""
    ltp = get_last_trade_price(token_id)
    if ltp and ltp > 0.01:
        return ltp
    bid = get_best_bid_raw(token_id)
    if bid and bid > 0.01:
        return bid
    caller_price = _safe_float(caller_price)
    if caller_price is not None and caller_price > 0.01:
        return caller_price
    return None  # 无法估算


def _normalize_book_levels(levels):
    normalized = []
    for level in levels or []:
        if isinstance(level, dict):
            price = _safe_float(level.get("price"))
            size = _safe_float(level.get("size"))
        else:
            price = _safe_float(getattr(level, "price", None))
            size = _safe_float(getattr(level, "size", None))
        if price is None or size is None or price <= 0 or size <= 0:
            continue
        normalized.append((round(price, 4), round(size, 4)))
    return normalized


def _get_stream_snapshot(stream, coin):
    """读取价格流 snapshot；mock/旧对象不支持时安全返回 None。"""
    getter = getattr(stream, "get_snapshot", None)
    if not callable(getter):
        return None
    try:
        snapshot = getter(coin)
    except Exception:
        return None
    return snapshot if isinstance(snapshot, dict) else None


def _format_age_ms(age_ms):
    age = _safe_float(age_ms)
    if age is None:
        return "N/A"
    if age < 1000:
        return f"{age:.0f}ms"
    if age < 10_000:
        return f"{age / 1000:.2f}s"
    return f"{age / 1000:.1f}s"


def _format_stream_age(label, snapshot):
    if not isinstance(snapshot, dict):
        return f"{label}=N/A"
    age_label = _format_age_ms(snapshot.get("age_ms"))
    source_age = _safe_float(snapshot.get("source_age_ms"))
    source_label = f"/src={_format_age_ms(source_age)}" if source_age is not None else ""
    stale = " stale" if snapshot.get("stale") else ""
    return f"{label}={age_label}{source_label}{stale}"


def _format_price_skew(label, left_snapshot, right_snapshot, atr_val):
    if not isinstance(left_snapshot, dict) or not isinstance(right_snapshot, dict):
        return f"{label}=N/A"
    left_price = _safe_float(left_snapshot.get("price"))
    right_price = _safe_float(right_snapshot.get("price"))
    if left_price is None or right_price is None:
        return f"{label}=N/A"
    delta = left_price - right_price
    if atr_val and atr_val > 0:
        return f"{label}={delta:+.2f}({delta / atr_val:+.2f}ATR)"
    return f"{label}={delta:+.2f}"


def _get_market_observability(token_id):
    """返回盘口快照 age/spread，便于对比盘口和 oracle 哪边先动。"""
    try:
        bba_snapshot = _poly_ws.get_best_bid_ask_snapshot(token_id)
    except Exception:
        bba_snapshot = None
    if isinstance(bba_snapshot, dict):
        best_bid = _safe_float(bba_snapshot.get("best_bid"))
        best_ask = _safe_float(bba_snapshot.get("best_ask"))
        mid = ((best_bid + best_ask) / 2.0) if best_bid is not None and best_ask is not None else None
        spread_pct = ((best_ask - best_bid) / mid * 100.0) if mid and best_ask >= best_bid else None
        return {
            "source": "ws_bba",
            "age_ms": bba_snapshot.get("age_ms"),
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread_pct": spread_pct,
        }

    try:
        book_snapshot = _poly_ws.get_book_snapshot(token_id)
    except Exception:
        book_snapshot = None
    if isinstance(book_snapshot, dict):
        bids = _normalize_book_levels(book_snapshot.get("bids"))
        asks = _normalize_book_levels(book_snapshot.get("asks"))
        best_bid = bids[0][0] if bids else None
        best_ask = asks[0][0] if asks else None
        mid = ((best_bid + best_ask) / 2.0) if best_bid is not None and best_ask is not None else None
        spread_pct = ((best_ask - best_bid) / mid * 100.0) if mid and best_ask >= best_bid else None
        return {
            "source": "ws_book",
            "age_ms": book_snapshot.get("age_ms"),
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread_pct": spread_pct,
        }
    return None


def _format_price_observability(crypto_debug, market_obs, atr_val, wake_context=None):
    if not isinstance(crypto_debug, dict):
        return None
    selected_label = crypto_debug.get("source_path") or crypto_debug.get("source") or "N/A"
    parts = [
        f"sel={selected_label}@{_format_age_ms(crypto_debug.get('selected_age_ms'))}",
        _format_stream_age("CL", crypto_debug.get("chainlink")),
        _format_stream_age("Pyth", crypto_debug.get("pyth")),
        _format_stream_age("BN", crypto_debug.get("binance")),
        _format_price_skew("CL-Py", crypto_debug.get("chainlink"), crypto_debug.get("pyth"), atr_val),
        _format_price_skew("CL-BN", crypto_debug.get("chainlink"), crypto_debug.get("binance"), atr_val),
    ]
    if isinstance(market_obs, dict):
        spread_pct = _safe_float(market_obs.get("spread_pct"))
        market_part = f"OB={_format_age_ms(market_obs.get('age_ms'))}/{market_obs.get('source') or 'N/A'}"
        if spread_pct is not None:
            market_part += f" spr={spread_pct:.1f}%"
        parts.append(market_part)
    if isinstance(wake_context, dict):
        wake_label = wake_context.get("label")
        wake_detail = wake_context.get("detail")
        if wake_label:
            parts.append(f"wake={wake_label}({wake_detail})" if wake_detail else f"wake={wake_label}")
    return " | ".join(parts)


def evaluate_oracle_stale_watch(crypto_debug, atr_val, remaining, streak=0, was_active=False):
    """观测官方价(CL)是否明显落后于快市场(Binance)，仅返回告警状态。"""
    state = {
        "eligible": False,
        "condition": False,
        "active": False,
        "triggered": False,
        "recovered": bool(was_active),
        "next_streak": 0,
        "required_confirms": ORACLE_STALE_WATCH_CONFIRMATIONS,
        "skew": None,
        "skew_atr": None,
        "cl_age_ms": None,
        "cl_source_age_ms": None,
        "bn_age_ms": None,
        "bn_source_age_ms": None,
    }
    if not ENABLE_ORACLE_STALE_WATCH:
        state["recovered"] = False
        return state
    if remaining is None or remaining > ORACLE_STALE_WATCH_MAX_REMAINING:
        return state
    if atr_val is None or atr_val <= 0:
        return state

    cl_snapshot = crypto_debug.get("chainlink") if isinstance(crypto_debug, dict) else None
    bn_snapshot = crypto_debug.get("binance") if isinstance(crypto_debug, dict) else None
    if not isinstance(cl_snapshot, dict) or not isinstance(bn_snapshot, dict):
        return state

    cl_price = _safe_float(cl_snapshot.get("price"))
    bn_price = _safe_float(bn_snapshot.get("price"))
    if cl_price is None or bn_price is None:
        return state

    skew = cl_price - bn_price
    skew_atr = abs(skew) / atr_val
    condition = skew_atr >= ORACLE_STALE_WATCH_ATR
    next_streak = streak + 1 if condition else 0
    active = condition and next_streak >= ORACLE_STALE_WATCH_CONFIRMATIONS

    state.update({
        "eligible": True,
        "condition": condition,
        "active": active,
        "triggered": (not was_active) and active,
        "recovered": was_active and not active,
        "next_streak": next_streak,
        "skew": skew,
        "skew_atr": skew_atr,
        "cl_age_ms": cl_snapshot.get("age_ms"),
        "cl_source_age_ms": cl_snapshot.get("source_age_ms"),
        "bn_age_ms": bn_snapshot.get("age_ms"),
        "bn_source_age_ms": bn_snapshot.get("source_age_ms"),
    })
    return state


# ═══════════════════════════════════════════════════════════
# EV-Gate 止损纯函数
# ═══════════════════════════════════════════════════════════

def random_walk_p_win(direction, crypto_price, ptb_price, atr_val, remaining_seconds):
    """
    Oracle-based P(win at settlement) using Brownian motion model.

    Handles ATR≈0 gracefully: small gap + time remaining ≈ coin flip (0.50).
    Returns float in [EV_P_WIN_FLOOR, EV_P_WIN_CAP].
    """
    gap = calc_signed_direction_gap(direction, crypto_price, ptb_price)
    if gap is None:
        return 0.50

    if atr_val is None or atr_val <= 0 or remaining_seconds is None or remaining_seconds <= 0:
        # 无波动率数据或已到期：靠 gap 符号判断
        if remaining_seconds is not None and remaining_seconds <= 0:
            return max(EV_P_WIN_FLOOR, min(EV_P_WIN_CAP, 0.95 if gap > 0 else 0.05))
        return 0.50

    sigma_ratio = EV_ATR_SIGMA_RATIO if EV_ATR_SIGMA_RATIO > 0 else 1.5
    sigma_per_min = atr_val / sigma_ratio
    sigma_total = sigma_per_min * math.sqrt(remaining_seconds / 60.0)

    if sigma_total <= 0:
        return max(EV_P_WIN_FLOOR, min(EV_P_WIN_CAP, 0.95 if gap > 0 else 0.05))

    z = gap / sigma_total  # signed: positive = correct side
    p_win = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    return max(EV_P_WIN_FLOOR, min(EV_P_WIN_CAP, p_win))


def calc_net_exit_value(token_id, size):
    """实际可执行卖出净到手价（扣 fee）。无流动性时返回 0.0。"""
    bid = get_best_bid_raw(token_id)
    if not bid or bid < 0.02:
        return 0.0
    fee_rate_bps = clob_client.get_fee_rate_bps(token_id)
    sell_est = estimate_sell_fill(price=bid, size=size, fee_rate_bps=fee_rate_bps)
    net_price = _safe_float(sell_est.get("net_price"))
    return net_price if net_price and net_price > 0 else 0.0


def calc_ev_comparison(direction, crypto_price, ptb_price, atr_val,
                       remaining_seconds, entry_price, current_bid_net):
    """
    比较持有到结算 vs 立即卖出的期望值。

    Returns dict:
        p_win:       oracle random-walk P(win)
        ev_hold:     P(win) × $1.00
        ev_sell:     net bid price (what we'd get selling now)
        ev_edge:     ev_hold - ev_sell (positive = holding is better)
        should_exit: bool (True = selling is rational)
        reason:      human-readable explanation
    """
    p_win = random_walk_p_win(direction, crypto_price, ptb_price, atr_val, remaining_seconds)
    ev_hold = p_win  # P(win) × $1.00
    ev_sell = max(current_bid_net, 0.0)

    ev_edge = ev_hold - ev_sell
    should_exit = ev_sell > ev_hold

    if should_exit:
        reason = f"卖出更优(P_win={p_win:.1%},bid=${ev_sell:.3f})"
    elif ev_edge < 0.05:
        reason = f"边际持有(edge={ev_edge:+.3f})"
    else:
        reason = f"持有(P_win={p_win:.1%}>>bid${ev_sell:.3f})"

    return {
        "p_win": round(p_win, 4),
        "ev_hold": round(ev_hold, 4),
        "ev_sell": round(ev_sell, 4),
        "ev_edge": round(ev_edge, 4),
        "should_exit": should_exit,
        "reason": reason,
    }


def calc_bn_early_warning(direction, ptb_price, atr_val, bn_history):
    """
    Binance 预警加速：检测 BN 价格是否在快速向不利方向移动。

    bn_history: deque of (timestamp, price) 最近几秒的 BN 价格快照
    Returns dict:
        active:    True = BN 检测到快速不利移动
        velocity:  BN 每秒变化量（signed，负=对 UP 方向不利）
        velocity_atr: 每秒变化的 ATR 倍数
        discount:  P(win) 应乘以此因子（active 时 < 1.0）
        reason:    说明
    """
    inactive = {
        "active": False,
        "velocity": 0.0,
        "velocity_atr": 0.0,
        "discount": 1.0,
        "reason": None,
    }
    if not bn_history or len(bn_history) < BN_EW_MIN_SAMPLES:
        return inactive
    if atr_val is None or atr_val <= 0 or ptb_price is None:
        return inactive

    # 取窗口内的样本
    now_ts = bn_history[-1][0]
    window_start = now_ts - BN_EW_WINDOW_SEC
    window = [(ts, p) for ts, p in bn_history if ts >= window_start]
    if len(window) < BN_EW_MIN_SAMPLES:
        return inactive

    oldest_ts, oldest_price = window[0]
    newest_ts, newest_price = window[-1]
    dt = newest_ts - oldest_ts
    if dt < 0.3:
        return inactive

    # velocity = 价格变化速度（signed）
    raw_velocity = (newest_price - oldest_price) / dt  # $/sec

    # 对方向的不利速度：UP方向下跌是不利，DOWN方向上涨是不利
    if direction == "UP":
        adverse_velocity = -raw_velocity  # 下跌 → positive adverse
    elif direction == "DOWN":
        adverse_velocity = raw_velocity   # 上涨 → positive adverse
    else:
        return inactive

    velocity_atr = adverse_velocity / atr_val if atr_val > 0 else 0.0

    if velocity_atr >= BN_EW_VELOCITY_ATR:
        return {
            "active": True,
            "velocity": round(raw_velocity, 2),
            "velocity_atr": round(velocity_atr, 4),
            "discount": BN_EW_DISCOUNT,
            "reason": f"BN快跌{velocity_atr:.2f}ATR/s",
        }
    return inactive


def _get_fresh_exit_quote(token_id, price_hint=None):
    """退出前刷新最新 bid/book，优先 WS 快照，其次 SDK。"""
    try:
        book_snapshot = _poly_ws.get_book_snapshot(token_id)
    except Exception:
        book_snapshot = None
    if book_snapshot:
        bids = _normalize_book_levels(book_snapshot.get("bids"))
        if bids:
            best_bid = bids[0][0]
            return {
                "limit_price": round(best_bid, 2) if best_bid > 0.01 else 0.01,
                "best_bid": best_bid,
                "bid_depth": round(sum(size for _, size in bids), 2),
                "source": "ws_book",
                "snapshot_age_ms": book_snapshot.get("age_ms"),
            }

    try:
        bba_snapshot = _poly_ws.get_best_bid_ask_snapshot(token_id)
    except Exception:
        bba_snapshot = None
    if bba_snapshot:
        best_bid = _safe_float(bba_snapshot.get("best_bid"))
        if best_bid is not None and best_bid > 0.01:
            return {
                "limit_price": round(best_bid, 2),
                "best_bid": best_bid,
                "bid_depth": None,
                "source": "ws_bba",
                "snapshot_age_ms": bba_snapshot.get("age_ms"),
            }

    try:
        bids, _ = _get_orderbook_bids_asks(token_id)
        bids = _normalize_book_levels(bids)
        if bids:
            best_bid = bids[0][0]
            return {
                "limit_price": round(best_bid, 2) if best_bid > 0.01 else 0.01,
                "best_bid": best_bid,
                "bid_depth": round(sum(size for _, size in bids), 2),
                "source": "sdk_book",
                "snapshot_age_ms": None,
            }
    except Exception:
        pass

    ltp = get_last_trade_price(token_id)
    if ltp and ltp > 0.01:
        fallback = max(round(ltp - SLIPPAGE, 2), 0.01)
        return {
            "limit_price": fallback,
            "best_bid": ltp,
            "bid_depth": None,
            "source": "ltp",
            "snapshot_age_ms": None,
        }

    hinted = _safe_float(price_hint)
    if hinted is not None and hinted > 0.01:
        return {
            "limit_price": round(hinted, 2),
            "best_bid": hinted,
            "bid_depth": None,
            "source": "caller",
            "snapshot_age_ms": None,
        }

    return {
        "limit_price": 0.01,
        "best_bid": None,
        "bid_depth": None,
        "source": "floor",
        "snapshot_age_ms": None,
    }


def _bucket_exit_error(error_text):
    msg = str(error_text or "").lower()
    if not msg:
        return "unknown"
    if "not enough balance" in msg or "allowance" in msg:
        return "allowance_or_balance"
    if "fully filled or killed" in msg or "couldn't be fully filled" in msg:
        return "fok_killed"
    if (
        "request exception" in msg
        or "status_code=none" in msg
        or "timeout" in msg
        or "connection" in msg
        or "network" in msg
    ):
        return "request_exception"
    return "unknown"


def _calculate_total_realized_pnl(position, final_exit_price):
    """聚合部分成交和最终平仓，得到整笔交易的真实 realized PnL。

    PnL = 总收入(exit_price × final_size + partial收入) - 实际 entry_cost
    不依赖 _position_size（token_balance 可能是售后状态=0），独立推算最终退出份数。
    """
    final_exit_price = _safe_float(final_exit_price) or 0.0

    # ── 推算最终退出份数（不用 _position_size） ──
    # 注意: size 可能已被 partial 流程更新为 remaining，不能当 original。
    # entry_cost 和 entry_price 永远不被 partial 更新，用它们回推 original_total。
    token_balance = _safe_float(position.get("token_balance")) if isinstance(position, dict) else None
    partial_total = sum(
        _safe_float(p.get("size")) or 0.0
        for p in (position.get("partial_exits", []) or [])
    )
    if token_balance is not None and token_balance > 0:
        # token_balance > 0 → 售前链上余额，直接作为最终退出份数
        final_size = token_balance
    else:
        # 用 entry_cost / entry_price 回推原始总份数（partial-invariant）
        _ec = _safe_float(position.get("entry_cost"))
        _ep = _safe_float(position.get("entry_price"))
        if _ec and _ep and _ep > 0:
            original_total = _ec / _ep
        else:
            original_total = _safe_float(position.get("size")) or 0.0
        final_size = max(original_total - partial_total, 0.0)

    # ── 总收入 = 最终平仓收入 + 部分成交收入 ──
    total_revenue = final_exit_price * final_size
    for partial in position.get("partial_exits", []) or []:
        part_price = _safe_float(partial.get("price"))
        part_size = _safe_float(partial.get("size"))
        if part_price is not None and part_size is not None:
            total_revenue += part_price * part_size

    # ── 总成本 = 实际 entry_cost（含手续费），回退 entry_price × original_size ──
    entry_cost = _safe_float(position.get("entry_cost"))
    if entry_cost is None or entry_cost <= 0:
        entry_price = _safe_float(position.get("entry_price")) or 0.0
        entry_cost = entry_price * (final_size + partial_total)

    total_pnl = total_revenue - entry_cost
    return round(total_pnl, 4)


def market_sell_immediate(token_id, size, price=None, position=None, skip_cancel=False, max_retries=3, remaining=None):
    """市价立即卖出（止损专用）
    价格策略（逐级降价，追求成交而非价格）：
      1. 外部传入 price（调用方已有 best_bid，避免重复查询）
      2. 未传入 → last-trade-price - 滑点
      3. 都没有 → $0.01（地板价，CLOB按最高bid撮合）
    返回: (success, actual_price)
      success=True: 成交
      success=False, actual_price=None: 正常失败
      success=False, actual_price="NO_BALANCE": token余额为零，不必再重试
    skip_cancel: 调用方已执行cancel_all_orders时传True，避免重复网络调用
    remaining: 剩余秒数（None=从position slug自动推断，0=禁用maker退出，>0=可尝试maker）
    """
    # 先取消可能存在的旧挂单，释放被锁定的token余额
    if not skip_cancel:
        cancel_all_orders(token_id)

    # 校验链上真实余额（传入position以同步更新持仓记录）
    adjusted = _check_and_adjust_size(token_id, size, position=position)
    if adjusted == -1:
        # 新持仓余额可能未到账，等3秒后重试一次
        print("    ⏳ 等待3s后重新查询余额...")
        time.sleep(3)
        _balance_cache.pop(token_id, None)
        adjusted = _check_and_adjust_size(token_id, size, position=position)
        if adjusted == -1:
            adjusted = 0  # 二次确认仍为0，视为真正无余额
    if adjusted == 0:
        return False, "NO_BALANCE"
    if adjusted is not None:
        size = adjusted

    # ── v13.1 M1: Maker Exit — GTD SELL先尝试(0手续费+20%返佣) ──
    if remaining is None and position:
        try:
            _slug = position.get("slug", "")
            _start_ts = int(str(_slug).split("-")[-1])
            remaining = max(_start_ts + 300 - time.time(), 0)
        except Exception:
            remaining = 0
    elif remaining is None:
        remaining = 0

    _MAKER_EXIT_ENABLED = os.environ.get("MAKER_EXIT_ENABLED", "1") == "1"
    _MAKER_EXIT_MIN_REMAINING = float(os.environ.get("MAKER_EXIT_MIN_REMAINING", "30"))
    _MAKER_EXIT_WAIT = float(os.environ.get("MAKER_EXIT_WAIT", "8"))

    if _MAKER_EXIT_ENABLED and remaining > _MAKER_EXIT_MIN_REMAINING:
        quote = _get_fresh_exit_quote(token_id, price_hint=price)
        _maker_bid = _safe_float(quote.get("best_bid"))
        if _maker_bid and _maker_bid > 0.05:
            # 在best_bid上方1tick挂卖 → 确保不立即match → maker身份(0手续费+返佣)
            _maker_price = round(_maker_bid + 0.01, 2)
            from py_clob_client.clob_types import OrderType as _MakerOT
            _maker_exp = int(time.time()) + 60 + int(_MAKER_EXIT_WAIT)
            print(
                f"    🏷️ Maker退出: GTD SELL @${_maker_price:.2f} ×{size:.2f} "
                f"| bid=${_maker_bid:.2f} | 等{_MAKER_EXIT_WAIT:.0f}s"
            )
            try:
                _maker_result = clob_client.place_order(
                    token_id, SELL, _maker_price, size,
                    order_type=_MakerOT.GTD, expiration=_maker_exp)
                _maker_oid = _maker_result.get("order_id")
                if _maker_result.get("matched"):
                    _taking = _maker_result.get("taking", 0) or (_maker_price * size)
                    _actual = round(_taking / size, 4) if size > 0 and _taking > 0 else _maker_price
                    print(f"    🏷️ Maker即时成交! ${_actual:.4f} (0手续费)")
                    return True, _actual
                if _maker_oid:
                    _wait_end = time.time() + _MAKER_EXIT_WAIT
                    while time.time() < _wait_end:
                        time.sleep(2)
                        _balance_cache.pop(token_id, None)
                        _remain = _check_and_adjust_size(token_id, size, position=position)
                        if _remain == 0 or _remain == -1:
                            print(f"    🏷️ Maker成交! ${_maker_price:.4f} (0手续费+返佣)")
                            return True, _maker_price
                    # 超时 → 撤maker单，准备FAK fallback
                    try:
                        clob_client.cancel_order(_maker_oid)
                    except Exception:
                        pass
                    _balance_cache.pop(token_id, None)
                    _final_check = _check_and_adjust_size(token_id, size, position=position)
                    if _final_check == 0 or _final_check == -1:
                        print(f"    🏷️ Maker撤单时成交! ${_maker_price:.4f}")
                        return True, _maker_price
                    if _final_check is not None:
                        size = _final_check
                    print(f"    🏷️ Maker未成交({_MAKER_EXIT_WAIT:.0f}s)，切换FAK")
            except Exception as e:
                print(f"    🏷️ Maker挂单失败: {str(e)[:60]}，切换FAK")

    allowance_refreshed = False
    last_bucket = "unknown"
    last_limit_price = round(price, 2) if price and price > 0.01 else 0.01

    for attempt in range(1, max_retries + 1):
        quote = _get_fresh_exit_quote(token_id, price_hint=price)
        sell_price = quote["limit_price"]
        last_limit_price = sell_price
        bid_depth = _safe_float(quote.get("bid_depth"))
        age_ms = _safe_float(quote.get("snapshot_age_ms"))
        best_bid = _safe_float(quote.get("best_bid"))
        source = quote.get("source")
        if not isinstance(source, str):
            source = "unknown"
        depth_label = f"{bid_depth:.2f}" if bid_depth is not None else "N/A"
        age_label = f"{age_ms:.0f}ms" if age_ms is not None else "N/A"
        bid_label = f"${best_bid:.3f}" if best_bid is not None else "N/A"
        # v12.9.7: FAK (Fill-And-Kill) — partial fill OK, then sell remainder
        _use_fak = os.environ.get("STOP_LOSS_USE_FAK", "1") == "1"
        _order_type_label = "FAK" if _use_fak else "FOK"
        print(
            f"    ⚡ {_order_type_label}止损[{attempt}/{max_retries}]: limit=${sell_price:.2f} "
            f"| best_bid={bid_label} | depth={depth_label} | src={source} | age={age_label}"
        )

        try:
            if _use_fak:
                info = clob_client.place_fak_order(token_id, SELL, sell_price, size)
            else:
                info = clob_client.place_fok_order(token_id, SELL, sell_price, size)
        except Exception as e:
            info = {
                "matched": False,
                "status": "ERROR",
                "error": str(e),
                "raw": str(e),
                "elapsed_ms": 0,
                "taking": 0,
            }

        if info["matched"]:
            _taking = info.get("taking", 0) or 0
            _making = _safe_float(info.get("making")) or 0  # 实际成交份数
            _filled = _making if _making > 0 else size
            gross_price = round(_taking / _filled, 4) if _filled > 0 and _taking > 0 else sell_price
            fill_summary = _sell_fill_summary(token_id, _filled, _taking, gross_price)
            actual_price = round(fill_summary["net_price"], 4)
            print(
                f"    ⚡ 市价成交: gross=${gross_price:.4f} | net=${actual_price:.4f} "
                f"| fee=${fill_summary['fee_usdc']:.4f} | {info.get('elapsed_ms', 0):.0f}ms"
            )

            # FAK 部分成交检查
            if _use_fak:
                _balance_cache.pop(token_id, None)
                # fix: 用 API making 字段算成交份数（不受链上余额延迟影响）
                sold_count = round(_making, 6) if _making > 0 else 0
                if sold_count <= 0 and _taking > 0 and gross_price > 0:
                    sold_count = round(_taking / gross_price, 6)
                # fix: 用成交份数计算剩余，不依赖可能延迟的链上余额
                if sold_count > 0:
                    _remain = max(0, round(size - sold_count, 6))
                else:
                    _remain = _check_and_adjust_size(token_id, size, position=position)
                if _remain and _remain > 0.5:
                    # 记录已成交部分的 PnL
                    if sold_count > 0 and position is not None:
                        entry_price = _safe_float(position.get("entry_price")) or 0
                        if entry_price > 0:
                            partial = {"price": actual_price, "size": sold_count,
                                       "time": datetime.now(timezone.utc).isoformat(),
                                       "type": "fak_partial"}
                            partials = position.get("partial_exits") or []
                            partials.append(partial)
                            position["partial_exits"] = partials
                            from trading_state import record_partial_pnl
                            record_partial_pnl(position.get("slug", ""), actual_price, sold_count, entry_price)
                            position["size"] = _remain
                            position["token_balance"] = _remain
                            update_position(position, new_size=_remain, partial_exit=partial)
                            print(f"    ⚡ FAK部分成交: 已售{sold_count:.2f}份@${actual_price:.4f} 剩余{_remain:.2f}份，继续卖出")
                        else:
                            print(f"    ⚡ FAK部分成交: 剩余{_remain:.2f}份，继续卖出")
                    else:
                        print(f"    ⚡ FAK部分成交: 剩余{_remain:.2f}份，继续卖出")
                    size = _remain
                    continue  # 下一轮降价卖剩余
                elif _remain and 0 < _remain <= 0.5:
                    # 零头用地板价清
                    try:
                        clob_client.place_fok_order(token_id, SELL, 0.01, _remain)
                        print(f"    ⚡ 零头清除: {_remain:.2f}份@$0.01")
                    except Exception:
                        pass

            return True, actual_price

        err = info.get("error", "") or info.get("raw", "")
        last_bucket = _bucket_exit_error(err)
        print(
            f"    ❌ {_order_type_label}未成交: Status={info.get('status')} | bucket={last_bucket} "
            f"| {info.get('elapsed_ms', 0):.0f}ms"
        )

        # FOK后余额可能已变，清除预缓存，后续回查必须用实时数据
        _balance_cache.pop(token_id, None)

        if info.get("status") == "ERROR" and last_bucket not in ("fok_killed", "allowance_or_balance"):
            recheck = _check_and_adjust_size(token_id, size, position=position)
            if recheck == 0:
                ghost_price = _estimate_ghost_price(token_id, last_limit_price) or sell_price
                print(f"    ⚡ 链上余额已清零，订单实际已成交（幽灵成交）| 估计价=${ghost_price:.4f}")
                return True, ghost_price
            if recheck is not None:
                size = recheck

        if last_bucket == "allowance_or_balance":
            recheck = _check_and_adjust_size(token_id, size, position=position)
            if recheck == 0:
                return False, "NO_BALANCE"
            if recheck is not None:
                size = recheck
            if not allowance_refreshed and size > 0:
                print("    🔄 检测到余额/授权异常，刷新allowance后继续重试")
                clob_client.update_token_allowance(token_id)
                allowance_refreshed = True

    final_check = _check_and_adjust_size(token_id, size, position=position)
    if final_check == 0:
        return False, "NO_BALANCE"
    if final_check is not None:
        size = final_check

    if last_bucket == "allowance_or_balance" and allowance_refreshed:
        print("    ⚠️ allowance已刷新但仍未成交，保留持仓待下一轮重试")
    elif last_bucket == "fok_killed":
        print("    ⚠️ 退出FOK被市场杀掉，等待下一轮用最新盘口重试")
    elif last_bucket == "request_exception":
        print("    ⚠️ 退出请求异常，等待下一轮重试")

    return False, None

def sell_position(token_id, size, price, max_retries=3):
    """卖出持仓（FOK即时成交 + 降价重试）
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
        info = clob_client.place_fok_order(token_id, SELL, price, size)
        if info["matched"]:
            gross_price = round(info["taking"] / size, 4) if size > 0 and info["taking"] > 0 else price
            fill_summary = _sell_fill_summary(token_id, size, info.get("taking", 0), gross_price)
            actual_price = round(fill_summary["net_price"], 4)
            print(
                f"    📊 成交确认: Status={info['status']} | gross=${gross_price:.4f} "
                f"| net=${actual_price:.4f} | fee=${fill_summary['fee_usdc']:.4f} "
                f"| {info.get('elapsed_ms', 0):.0f}ms"
            )
            return True, info["raw"], actual_price
        # 余额/授权不足 → 立即停止，不降价重试
        err = info.get("error", "") or info.get("raw", "")
        if "not enough balance" in err.lower():
            return False, "NO_BALANCE", None
        # FOK超时/异常 → 查链上余额检测幽灵成交
        if info.get("status") == "ERROR":
            recheck = _check_and_adjust_size(token_id, size)
            if recheck == 0:
                print(f"    ⚡ 链上余额已清零，订单实际已成交（幽灵成交）")
                return True, "GHOST_FILL", price
        print(f"    ⏳ FOK未成交 | 尝试{attempt+1}/{max_retries} | {info.get('elapsed_ms', 0):.0f}ms")
        if attempt < max_retries - 1:
            price = round(max(price - 0.01, 0.01), 2)  # 降价重试

    return False, "All FOK retries failed", None

def close_position(position, exit_price):
    """标记持仓为已关闭"""
    # v14.1: 在清除close_intent前先提取退出原因
    _exit_reason = position.get("close_intent_reason", "unknown")
    for key in ("close_intent_active", "close_intent_reason", "close_intent_at"):
        position.pop(key, None)
    position["closed"] = True
    position["exit_price"] = exit_price
    position["exit_time"] = datetime.now(timezone.utc).isoformat()
    # v14.1: 决策快照 — 退出记录（用真实PnL计算，含partial exits和fee）
    try:
        from ai_trader.trade_logger import log_exit
        _real_pnl = _calculate_total_realized_pnl(position, exit_price)
        log_exit(position.get("slug", ""), position.get("coin", ""),
                 _exit_reason, exit_price=exit_price,
                 entry_price=position.get("entry_price", 0),
                 size=position.get("size", 0),
                 realized_pnl=round(_real_pnl, 4))
    except Exception:
        pass

    # 跨进程文件锁: 防止 bot append 和 monitor rewrite 竞争导致持仓丢失
    lock_fd = None
    try:
        lock_fd = open(POSITIONS_LOCK, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

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
    finally:
        if lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()

    # P0: 记录交易结果，供 base_rate 校准
    try:
        from ai_trader.base_rate import record_outcome
        entry = position.get("entry_price", 0)
        total_pnl = _calculate_total_realized_pnl(position, exit_price)
        realized_won = total_pnl > 0
        exit_price_val = _safe_float(exit_price)
        calibration_eligible = exit_price_val is not None and (exit_price_val <= 0.01 or exit_price_val >= 0.99)
        directional_won = (exit_price_val > 0.5) if calibration_eligible else None
        diff_in_atr = position.get("diff_in_atr", 0)
        record_outcome(
            slug=position.get("slug", "unknown"),
            direction=position.get("direction", "UP"),
            diff_in_atr=diff_in_atr,
            won=realized_won,
            extra={
                "entry_price": entry,
                "exit_price": exit_price,
                "size": _realized_trade_size(position),
                "entry_cost": round(_safe_float(position.get("entry_cost")) or ((_safe_float(entry) or 0.0) * _realized_trade_size(position)), 6),
                "realized_pnl": total_pnl,
                "realized_won": realized_won,
                "directional_won": directional_won,
                "calibration_eligible": calibration_eligible,
            }
        )
    except Exception:
        pass

    # Bug 5 fix: 用真实 PnL 更新 trading_state
    try:
        from trading_state import record_bet_result, settle_bet_cost
        entry = position.get("entry_price", 0)
        size = _position_size(position)
        total_pnl = _calculate_total_realized_pnl(position, exit_price)
        won = total_pnl > 0
        slug = position.get("slug", "unknown")
        # 用实际盈亏替换预扣成本
        settle_bet_cost(slug, total_pnl)
        record_bet_result(won, slug, pnl=0.0)  # pnl已在settle中处理，这里只更新胜负统计
    except Exception:
        pass

_reverse_entry_done = set()  # slug已反向入场过，防重复


def try_reverse_entry(position, remaining, crypto_debug=None, atr_val=None, ptb_price=None):
    """止损后尝试反向入场 — 方向明确反转时买入反方向token"""
    if os.environ.get("STOP_LOSS_REVERSE_ENTRY", "1") != "1":
        return
    slug = position.get("slug", "")
    if slug in _reverse_entry_done:
        return  # 同一市场只反向一次
    if remaining is None or remaining < 60:
        return  # 剩余时间不够
    opposite_token = position.get("opposite_token_id")
    if not opposite_token:
        return  # 没有反向token_id

    # 检查反向信号
    try:
        from ai_trader.polymarket_rtds import chainlink_stream
        from ai_trader.binance_api import price_stream
        from ai_trader.coins import coin_from_slug

        coin = coin_from_slug(slug)
        cl_price = chainlink_stream.get_price(coin)
        bn_price = price_stream.get_price(coin)
        if not cl_price or not ptb_price or not atr_val or atr_val <= 0:
            return

        gap = cl_price - ptb_price
        diff_atr = abs(gap) / atr_val
        old_dir = position.get("direction", "UP")
        new_dir = "UP" if gap > 0 else "DOWN"

        # 必须反转（新方向和旧方向相反）
        if new_dir == old_dir:
            return

        # BN 也要确认反转
        if bn_price:
            bn_gap = bn_price - ptb_price
            bn_dir = "UP" if bn_gap > 0 else "DOWN"
            if bn_dir != new_dir:
                return  # BN不确认

        REVERSE_MIN_CONF = float(os.environ.get("REVERSE_MIN_CONF", "0.35"))
        REVERSE_MIN_ATR = float(os.environ.get("REVERSE_MIN_ATR", "0.5"))

        if diff_atr < REVERSE_MIN_ATR:
            return

        # 简单信心估算（不依赖Bayesian updater，用random walk）
        from ai_analyze_v2 import _random_walk_p_win
        rw_pw = _random_walk_p_win(abs(gap), atr_val, remaining)
        p_win = 0.5 + (rw_pw - 0.5) * 0.60
        if p_win < 0.5 + REVERSE_MIN_CONF * 0.5:
            return  # 信心不够

        # 动态定价
        EDGE = float(os.environ.get("SNIPER_AMBUSH_EDGE", "0.045"))
        MIN_P = float(os.environ.get("SNIPER_AMBUSH_MIN_PRICE", "0.50"))
        MAX_P = float(os.environ.get("SNIPER_AMBUSH_MAX_PRICE", "0.75"))
        buy_price = round((p_win - EDGE) / 0.01) * 0.01
        buy_price = max(MIN_P, min(MAX_P, buy_price))

        # Kelly 仓位
        MIN_BET = float(os.environ.get("MIN_BET_SIZE", "5"))
        MAX_BET = float(os.environ.get("MAX_BET_SIZE", "8"))
        f_star = (p_win - buy_price) / (1.0 - buy_price) if p_win > buy_price else 0
        if f_star <= 0:
            return
        bal = clob_client.get_balance() or 20
        net_size = round(bal * f_star / 4.0 / buy_price, 1)
        net_size = max(MIN_BET, min(MAX_BET, net_size))
        gross_size = round(net_size / 0.975, 2)

        # FOK 买入反方向
        from py_clob_client.order_builder.constants import BUY
        clob_client.update_token_allowance(opposite_token)
        info = clob_client.place_fok_order(opposite_token, BUY, buy_price, gross_size)

        if info.get("matched"):
            actual_price = info.get("taking", 0)
            if actual_price and gross_size > 0:
                actual_price = round(actual_price / gross_size, 4)
            else:
                actual_price = buy_price

            # 写入 positions.jsonl
            _entry_ts = datetime.now(timezone.utc).isoformat()
            _rev_record = {
                "token_id": opposite_token,
                "slug": slug,
                "direction": new_dir,
                "entry_price": actual_price,
                "size": gross_size,
                "entry_cost": round(actual_price * gross_size, 4),
                "entry_time": _entry_ts,
                "price_to_beat": ptb_price,
                "atr": atr_val,
                "opposite_token_id": position.get("token_id"),
                "ambush": True,
                "reverse_entry": True,
            }
            try:
                lock_fd = open(POSITIONS_LOCK, "w")
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                with open(POSITIONS_FILE, "a") as pf:
                    pf.write(json.dumps(_rev_record) + "\n")
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                lock_fd.close()
            except Exception:
                pass

            _reverse_entry_done.add(slug)
            print(
                f"  🔄 反向入场! {new_dir} @${actual_price:.2f}×{gross_size:.1f}份 "
                f"(止损{old_dir}后反转, p_win={p_win:.3f} ATR={diff_atr:.2f})")

            # 通知
            try:
                from auto_bot_v3 import send_notification
                send_notification(coin, new_dir, p_win - 0.5, 0, actual_price, gross_size, ambush=True)
            except Exception:
                pass
        else:
            print(f"  🔄 反向入场未成交: {new_dir} @${buy_price:.2f} | {info.get('status')}")

    except Exception as e:
        print(f"  ⚠️ 反向入场异常: {e}")


def update_position(position, new_size=None, partial_exit=None):
    """更新持仓（如分批卖出后的剩余仓位）"""
    lock_fd = None
    try:
        lock_fd = open(POSITIONS_LOCK, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

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
                                        pos["token_balance"] = new_size
                                    if partial_exit:
                                        history = pos.get("partial_exits", [])
                                        history.append(partial_exit)
                                        pos["partial_exits"] = history
                                        pos["last_partial_exit"] = partial_exit
                                    # 同步调用方修改的字段（如抄底后的entry_price/original_size等）
                                    for sync_key in ("entry_price", "original_entry_price", "original_size",
                                                     "dip_buy_price", "dip_buy_size", "dip_buy_gross_size",
                                                     "dip_buy_fee_shares", "dip_buy_fee_usdc", "dip_buy_cost",
                                                     "entry_cost",
                                                     "close_crypto_price", "close_crypto_time",
                                                     "close_intent_active", "close_intent_reason", "close_intent_at"):
                                        if sync_key in position:
                                            pos[sync_key] = position[sync_key]
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
    finally:
        if lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()


def _merge_fill_into_existing_position(position, new_total_size, fill_price=None):
    """把额外成交并回已有持仓，避免同 token 的幽灵加仓失管。"""
    recorded_size = _position_size(position)
    total_size = round(_safe_float(new_total_size) or 0.0, 6)
    if total_size <= recorded_size + 1e-9:
        return 0.0, 0.0

    added_size = round(total_size - recorded_size, 6)
    fill_price_val = _safe_float(fill_price)

    prev_entry_price = _safe_float(position.get("entry_price")) or 0.0
    prev_entry_cost = _safe_float(position.get("entry_cost"))
    if prev_entry_cost is None:
        prev_entry_cost = round(prev_entry_price * recorded_size, 6)

    added_cost = 0.0
    if fill_price_val and fill_price_val > 0:
        added_cost = round(fill_price_val * added_size, 6)
        total_cost = round(prev_entry_cost + added_cost, 6)
        position["entry_cost"] = total_cost
        if total_size > 0:
            position["entry_price"] = round(total_cost / total_size, 6)

    position["size"] = total_size
    position["token_balance"] = total_size
    update_position(position, new_size=total_size)
    return added_size, added_cost


def _arm_close_intent(position, reason):
    if position is None:
        return
    if position.get("close_intent_active") and position.get("close_intent_reason") == reason:
        return
    position["close_intent_active"] = True
    position["close_intent_reason"] = reason
    position["close_intent_at"] = datetime.now(timezone.utc).isoformat()
    update_position(position)


def _clear_close_intent(position):
    if position is None or not position.get("close_intent_active"):
        return
    position["close_intent_active"] = False
    position["close_intent_reason"] = None
    position["close_intent_at"] = None
    update_position(position)

def get_market_end_time(slug):
    """从slug提取市场结束时间（Unix时间戳）"""
    try:
        # slug格式: btc-updown-5m-1772945400
        # timestamp是市场开始时间，5分钟市场需要+300秒
        parts = slug.split('-')
        if len(parts) >= 4:
            timestamp = int(parts[-1])
            return timestamp + 300  # 5分钟市场
    except Exception:
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
    except Exception:
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
    except Exception:
        pass
    return None


def freeze_settlement_reference_price(position, crypto_price):
    """在收盘前后冻结一份底层价格，供 API 无 outcome 时回退使用。"""
    price = _safe_float(crypto_price)
    if not isinstance(position, dict) or price is None or price <= 0:
        return False

    prev = _safe_float(position.get("close_crypto_price"))
    if prev is not None and abs(prev - price) < 1e-9:
        return False

    position["close_crypto_price"] = round(price, 2)
    position["close_crypto_time"] = datetime.now(timezone.utc).isoformat()
    return update_position(position)


def get_settlement_reference_price(position, coin):
    """结算回退优先使用收盘前冻结价，避免误用收盘后漂移的实时价。"""
    frozen = _safe_float(position.get("close_crypto_price")) if isinstance(position, dict) else None
    if frozen is not None and frozen > 0:
        return frozen, "冻结价"

    debug = get_current_crypto_price_debug(coin)
    live = debug.get("price")
    if live is not None:
        return live, debug.get("source")
    return None, None


def get_current_crypto_price_debug(coin):
    """返回当前 crypto 价格及调试元数据，不改变原有选源优先级。"""
    cl_snapshot = _get_stream_snapshot(_chainlink_stream, coin)
    pyth_snapshot = _get_stream_snapshot(_pyth_stream, coin)
    binance_snapshot = _get_stream_snapshot(_price_stream, coin)

    debug = {
        "coin": coin,
        "price": None,
        "source": None,
        "source_path": None,
        "selected_age_ms": None,
        "chainlink": cl_snapshot,
        "pyth": pyth_snapshot,
        "binance": binance_snapshot,
    }

    def _select(price, source, source_path, age_ms=None):
        debug["price"] = price
        debug["source"] = source
        debug["source_path"] = source_path
        debug["selected_age_ms"] = _safe_float(age_ms)
        return debug

    if PRICE_SOURCE == 2:
        # Pyth优先模式
        if isinstance(pyth_snapshot, dict) and not pyth_snapshot.get("stale"):
            return _select(pyth_snapshot.get("price"), "Pyth", "pyth_stream", pyth_snapshot.get("age_ms"))
        pyth_rest = get_pyth_price(coin)
        if pyth_rest is not None:
            return _select(pyth_rest, "Pyth", "pyth_rest")
        if isinstance(cl_snapshot, dict) and not cl_snapshot.get("stale"):
            return _select(cl_snapshot.get("price"), "CL", "chainlink_stream", cl_snapshot.get("age_ms"))
    else:
        # Chainlink优先模式（默认，官方结算价）
        if isinstance(cl_snapshot, dict) and not cl_snapshot.get("stale"):
            return _select(cl_snapshot.get("price"), "CL", "chainlink_stream", cl_snapshot.get("age_ms"))
        if isinstance(pyth_snapshot, dict) and not pyth_snapshot.get("stale"):
            return _select(pyth_snapshot.get("price"), "Pyth", "pyth_stream", pyth_snapshot.get("age_ms"))
        pyth_rest = get_pyth_price(coin)
        if pyth_rest is not None:
            return _select(pyth_rest, "Pyth", "pyth_rest")

    # 最终 fallback: Binance
    if isinstance(binance_snapshot, dict) and not binance_snapshot.get("stale"):
        return _select(binance_snapshot.get("price"), "WS", "binance_ws", binance_snapshot.get("age_ms"))
    try:
        symbol = _get_binance_symbol(coin)
        resp = requests.get(
            f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}",
            timeout=5
        )
        if resp.status_code == 200:
            data = resp.json()
            return _select(float(data["price"]), "REST", "binance_rest")
    except Exception:
        pass
    return debug


def get_current_crypto_price(coin):
    """获取BTC/ETH当前实时价格 - PRICE_SOURCE=1 Chainlink优先, =2 Pyth优先"""
    return get_current_crypto_price_debug(coin).get("price")

def get_ptb_from_slug(slug):
    """从 slug 获取 PTB，优先 crypto-price API，失败时回退 Playwright。"""
    try:
        ptb = get_price_to_beat_api(slug, timeout=3)
        if ptb is not None:
            return ptb

        ptb_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_trader", "playwright_ptb.py")
        result = subprocess.run(
            ["python3", ptb_script, slug],
            capture_output=True,
            text=True,
            timeout=15
        )
        if result.returncode == 0:
            import re
            match = re.search(r'PTB=([\d.]+)', result.stdout or "")
            if match:
                return float(match.group(1))
    except Exception:
        pass
    return None

def get_atr_from_binance(coin, period=14):
    """获取ATR（平均真实波幅）"""
    try:
        symbol = _get_binance_symbol(coin)
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
    except Exception:
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
    if entry_price and entry_price > 0:
        max_possible_profit = (1.0 - entry_price) / entry_price
        threshold = min(threshold, max_possible_profit * 0.80)
    return threshold


def compute_trailing_drawdown(high_water_mark, remaining_seconds, direction_correct=False, diff_atr=None):
    """根据峰值利润等级和剩余时间，计算允许的最大回撤比例。

    返回值: 0.0~1.0 之间的回撤比例（从峰值利润回撤多少比例就触发止盈）
    例: high_water_mark=0.228, 返回0.35 → 利润跌到 0.228*(1-0.35)=0.148 就止盈

    v12.8: Trailing Take-Profit 核心计算
    """
    # 根据峰值利润等级选择基础回撤容忍度
    if high_water_mark >= 0.30:
        base_drawdown = TRAILING_TP_DRAWDOWN_HIGH  # 30%
    elif high_water_mark >= 0.20:
        base_drawdown = TRAILING_TP_DRAWDOWN_MID   # 35%
    else:
        base_drawdown = TRAILING_TP_DRAWDOWN_LOW   # 40%

    # 时间衰减：剩余时间越少，回撤容忍度越低（收紧止盈）
    if remaining_seconds < 120:
        time_factor = TRAILING_TP_TIME_TIGHTEN + (1.0 - TRAILING_TP_TIME_TIGHTEN) * (remaining_seconds / 120.0)
        base_drawdown *= time_factor

    # 方向正确 + ATR 强信号时给更多空间（最多放宽 20%）
    if direction_correct and diff_atr and diff_atr >= 2.0:
        base_drawdown *= 1.2

    return min(base_drawdown, 0.60)  # 上限 60%，不能太宽松


def execute_dip_buy(token_id, original_size, coin, slug, pos):
    """抄底加仓：用FOK在best_ask买入原仓位的DIP_BUY_SIZE_RATIO倍
    返回: (success, bought_size, buy_price) or (False, 0, None)
    """
    # ★ 抄底前检查日亏损上限
    try:
        from trading_state import check_daily_loss_limit
        allowed, daily_pnl, limit = check_daily_loss_limit()
        if not allowed:
            print(f"    🚫 今日亏损 ${daily_pnl:+.2f} 已达上限 -${limit}，跳过抄底")
            return False, 0, None
    except Exception:
        pass
    best_ask = get_best_ask(token_id)
    if not best_ask or best_ask >= 0.95:
        print(f"    ❌ 抄底失败: best_ask={best_ask}, 无法买入")
        return False, 0, None

    buy_price = round(best_ask, 2)
    dip_plan = _plan_dip_buy_size(token_id, original_size, buy_price)
    dip_size = float(dip_plan.get("gross_order_size") or 0.0)
    if dip_size <= 0:
        print(f"    ⚠️ 抄底跳过: {dip_plan.get('skip_reason') or 'SIZE_TOO_SMALL'}")
        return False, 0, None

    current_net_size = _position_size(pos)
    if dip_plan.get("forced_to_min"):
        print(
            f"    📏 抄底最小净仓位补齐: raw_net={(original_size or 0) * DIP_BUY_SIZE_RATIO:.2f}份 "
            f"→ target_net={dip_plan['target_net_size']:.2f}份 | gross={dip_size:.2f}份"
        )
    print(
        f"    🟢 抄底下单: gross {dip_size:.2f}份 "
        f"(目标net {dip_plan['target_net_size']:.2f} / 预计net {dip_plan['expected_net_size']:.2f}) × ${buy_price:.2f} (FOK)"
    )

    def _finalize_dip_fill(info, fallback_price, planned_gross_size):
        actual_gross_size = planned_gross_size
        if info.get("taking", 0):
            actual_gross_size = round(float(info.get("taking", 0) or planned_gross_size), 6)
        actual_gross_price = round(
            info["making"] / actual_gross_size, 4
        ) if actual_gross_size > 0 and info.get("making", 0) > 0 else fallback_price
        entry_cost = round(float(info.get("making", 0) or (actual_gross_price * actual_gross_size)), 6)

        total_balance = None
        net_added_size = None
        try:
            total_balance = _safe_float(clob_client.get_token_balance(token_id))
        except Exception:
            total_balance = None
        if total_balance is not None and total_balance > 0:
            net_added_size = max(round(total_balance - current_net_size, 6), 0.0)

        # 健全性检查：net_added_size 不应小于 gross_size 的 50%
        # (正常fee约2-5%，如果net<gross的50%说明余额查询异常，回退到公式估算)
        if net_added_size is not None and actual_gross_size > 0:
            if net_added_size < actual_gross_size * 0.50:
                print(
                    f"    ⚠️ 抄底余额异常: net_added={net_added_size:.4f} < gross×50%={actual_gross_size*0.50:.4f} "
                    f"(bal={total_balance} cur={current_net_size})，回退公式估算")
                net_added_size = None  # 回退让 estimate_buy_fill 用费率公式

        fill_summary = estimate_buy_fill(
            price=actual_gross_price,
            gross_size=actual_gross_size,
            fee_rate_bps=dip_plan["fee_rate_bps"],
            gross_cost=entry_cost,
            net_size=net_added_size,
        )
        bought_net_size = fill_summary["net_size"] or actual_gross_size
        effective_buy_price = fill_summary["effective_entry_price"]
        if bought_net_size <= 0:
            return False, 0.0, None

        if total_balance is not None and total_balance > 0:
            pos["token_balance"] = total_balance
        else:
            pos["token_balance"] = round(current_net_size + bought_net_size, 6)
        pos["dip_buy_gross_size"] = actual_gross_size
        pos["dip_buy_fee_shares"] = fill_summary["fee_shares"]
        pos["dip_buy_fee_usdc"] = fill_summary["fee_usdc"]
        pos["dip_buy_cost"] = entry_cost

        print(
            f"    ✅ 抄底成交: gross {actual_gross_size:.4f}份 → net {bought_net_size:.4f}份 "
            f"× ${effective_buy_price:.4f} | fee=${fill_summary['fee_usdc']:.4f} | {info.get('elapsed_ms', 0):.0f}ms"
        )
        try:
            clob_client.update_token_allowance(token_id)
            print("    🔓 抄底后刷新卖出allowance")
        except Exception:
            pass
        try:
            from trading_state import record_bet_cost
            record_bet_cost(slug, round(entry_cost, 4))
            print(f"    📉 抄底预扣成本 ${entry_cost:.2f}")
        except Exception:
            pass
        return True, bought_net_size, effective_buy_price

    try:
        info = clob_client.place_fok_order(token_id, BUY, buy_price, dip_size)
        if info["matched"]:
            return _finalize_dip_fill(info, buy_price, dip_size)
        print(f"    ❌ 抄底FOK未成交: Status={info.get('status')} | {info.get('elapsed_ms', 0):.0f}ms")

        # 提高出价重试一次
        buy_price2 = round(min(buy_price + 0.01, 0.95), 2)
        info2 = clob_client.place_fok_order(token_id, BUY, buy_price2, dip_size)
        if info2["matched"]:
            return _finalize_dip_fill(info2, buy_price2, dip_size)
        return False, 0, None
    except Exception as e:
        print(f"    ❌ 抄底异常: {str(e)[:80]}")
        return False, 0, None

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
        if output == "NO_BALANCE":
            return False, None, "NO_BALANCE"
        # FOK后缓存已自动清除，无需sleep等待

    return False, None, "所有价格尝试均失败"

# 预挂单状态追踪（持久化到文件，进程重启不丢失）
PRE_ORDERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "pre_orders.json")

# Pending buy orders (LIVE but not yet matched)
PENDING_ORDERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "pending_orders.jsonl")
PENDING_ORDER_TTL = int(os.environ.get("PENDING_ORDER_TTL", "30"))
PENDING_AUTO_CANCEL = os.environ.get("PENDING_AUTO_CANCEL", "0") == "1"
PENDING_MIN_FILL = float(os.environ.get("PENDING_MIN_FILL", "0.5"))


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
    """取消所有活跃订单（SDK直连）"""
    return clob_client.cancel_all(token_id)

def reconcile_pending_orders():
    """Promote LIVE buy orders to positions when they fill; cancel stale ones."""
    orders = _load_pending_orders()
    if not orders:
        return

    active_status = {"LIVE", "PENDING", "ACCEPTED"}
    # Normalize status to uppercase so "live"/"pending" are treated as active
    active = {}
    for k, v in orders.items():
        status = (v.get("status") or "").upper()
        if status in active_status:
            v["status"] = status
            active[k] = v
    if not active:
        return

    print(f"  🔄 reconcile: 检查 {len(active)} 笔挂单...")

    open_positions = get_open_positions()
    open_by_key = {(p.get("slug"), p.get("token_id")): p for p in open_positions}

    now = datetime.now(timezone.utc)

    for key, order in active.items():
        slug = order.get("slug")
        token_id = order.get("token_id")
        if not slug or not token_id:
            continue
        existing_position = open_by_key.get((slug, token_id))

        created_at = order.get("created_at") or order.get("entry_time")
        age = None
        if created_at:
            try:
                age = (now - datetime.fromisoformat(created_at.replace("Z", "+00:00"))).total_seconds()
            except Exception:
                age = None

        # 检测成交：1. SDK查询订单状态 → 2. fallback到token余额
        filled_size = None
        avg_price = None  # SDK get_order may populate this
        order_id = order.get("order_id")
        order_status = None
        balance = None

        # 方法1：SDK get_order — 直接查订单状态，最准确
        if order_id:
            try:
                order_info = clob_client.get_order(order_id)
                if order_info:
                    order_status = (order_info.get("status") or "").upper()
                    if order_status == "MATCHED":
                        # 订单已成交
                        size_matched = float(order_info.get("size_matched", 0) or 0)
                        original_size = float(order_info.get("original_size", 0) or order.get("requested_size", 0))
                        filled_size = size_matched if size_matched > 0 else original_size
                        avg_price = float(order_info.get("price", 0) or 0)
                        print(f"  📡 reconcile: SDK订单状态=MATCHED size={filled_size} price={avg_price}")
                    elif order_status in ("CANCELLED", "CANCELED") and not existing_position:
                        print(f"  ❌ reconcile: 订单已取消 {slug}")
                        _append_pending_update({
                            "order_id": order_id, "slug": slug, "token_id": token_id,
                            "status": "CANCELLED", "resolved_at": now.isoformat(),
                        })
                        continue
                    else:
                        print(f"  ⏳ reconcile: SDK订单状态={order_status} {slug}")
            except Exception as e:
                print(f"  ⚠️ reconcile: SDK get_order 失败: {e}")

        # 方法2：fallback — 查 token 余额
        if not filled_size or existing_position:
            try:
                balance = get_token_balance(token_id)
                print(f"  📊 reconcile: {slug} token balance={balance}")
                if not filled_size and balance is not None and balance > 0:
                    filled_size = balance
            except Exception as e:
                print(f"  ⚠️ reconcile: get_token_balance 失败: {e}")

        if existing_position:
            recorded_size = _position_size(existing_position)
            actual_balance = balance if balance is not None else None
            if filled_size and filled_size > 0:
                inferred_total = round(recorded_size + filled_size, 6)
                actual_balance = max(actual_balance or 0.0, inferred_total)

            if actual_balance and actual_balance >= recorded_size + PENDING_MIN_FILL:
                addon_price = None
                addon_source = "unknown"
                if avg_price and 0.01 < avg_price < 0.99:
                    addon_price = avg_price
                    addon_source = "positions_avg"
                elif order.get("limit_price"):
                    addon_price = order.get("limit_price")
                    addon_source = "limit_price"

                added_size, added_cost = _merge_fill_into_existing_position(
                    existing_position, actual_balance, addon_price
                )
                if added_size >= PENDING_MIN_FILL:
                    print(
                        f"  ✅ reconcile 补并持仓: {slug} 原{recorded_size:.4f} → 现{actual_balance:.4f} "
                        f"(补{added_size:.4f} @ {addon_source})"
                    )
                    cost_recorded = False
                    if added_cost > 0 and not order.get("cost_recorded"):
                        try:
                            from trading_state import record_bet_cost
                            record_bet_cost(slug, round(added_cost, 4))
                            cost_recorded = True
                            print(f"  📉 reconcile: 补记幽灵加仓成本 ${added_cost:.2f}")
                        except Exception as e:
                            print(f"  ⚠️ reconcile: 补记幽灵加仓成本失败: {e}")

                    _append_pending_update({
                        "order_id": order.get("order_id"),
                        "pending_id": order.get("pending_id"),
                        "slug": slug,
                        "token_id": token_id,
                        "status": "FILLED_ADDON",
                        "filled_size": added_size,
                        "new_total_size": actual_balance,
                        "entry_price": existing_position.get("entry_price"),
                        "entry_price_source": addon_source,
                        "cost_recorded": cost_recorded,
                        "resolved_at": now.isoformat(),
                    })
                    continue

            resolved_status = "CANCELLED" if order_status in ("CANCELLED", "CANCELED") else "RESOLVED_ALREADY"
            print(f"  ✅ reconcile: {slug} 已在持仓中，无额外余额，标记 {resolved_status}")
            _append_pending_update({
                "order_id": order.get("order_id"),
                "pending_id": order.get("pending_id"),
                "slug": slug,
                "token_id": token_id,
                "status": resolved_status,
                "resolved_at": now.isoformat(),
            })
            continue

        if filled_size and filled_size >= PENDING_MIN_FILL:
            print(f"  ✅ reconcile 成交入仓: {slug} token={str(token_id)[:10]}... size={filled_size}")
            # avg_price may have been set by SDK get_order above; otherwise None
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

            gross_size = filled_size if filled_size else order.get("requested_size") or order.get("size") or 0
            size = gross_size

            coin = _coin_from_slug(slug)
            confidence = order.get("confidence") or 0
            ev = order.get("ev") or 0
            fee_rate_bps = clob_client.get_fee_rate_bps(token_id)
            entry_cost = round((_safe_float(entry_price) or 0.0) * gross_size, 6)
            real_balance = None

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
                "gross_entry_price": entry_price,
                "gross_size": gross_size,
                "entry_cost": entry_cost,
                "fee_rate_bps": fee_rate_bps,
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

            # 挂单成交后查链上真实余额 + 刷新allowance
            try:
                real_balance = get_token_balance(token_id)
                if real_balance and real_balance > 0:
                    fill_summary = estimate_buy_fill(
                        price=entry_price,
                        gross_size=gross_size,
                        fee_rate_bps=fee_rate_bps,
                        gross_cost=entry_cost,
                        net_size=real_balance,
                    )
                    size = fill_summary["net_size"] or gross_size
                    entry_price = fill_summary["effective_entry_price"]
                    position["entry_price"] = entry_price
                    position["size"] = size
                    position["token_balance"] = real_balance
                    position["buy_fee_shares"] = fill_summary["fee_shares"]
                    position["buy_fee_usdc"] = fill_summary["fee_usdc"]
                clob_client.update_token_allowance(token_id)
            except Exception:
                pass

            pending_entry_cost = round((_safe_float(position.get("entry_price")) or 0.0) * (_safe_float(position.get("size")) or 0.0), 4)
            if pending_entry_cost > 0 and not order.get("cost_recorded"):
                try:
                    from trading_state import record_bet_cost
                    record_bet_cost(slug, pending_entry_cost)
                    print(f"  📉 reconcile: 补记入场成本 ${pending_entry_cost:.2f}")
                except Exception as e:
                    print(f"  ⚠️ reconcile: 补记入场成本失败: {e}")

            os.makedirs("logs", exist_ok=True)
            _recon_lock = open(POSITIONS_LOCK, "w")
            try:
                fcntl.flock(_recon_lock, fcntl.LOCK_EX)
                with open("logs/positions.jsonl", "a") as f:
                    f.write(json.dumps(position) + "\n")
            finally:
                fcntl.flock(_recon_lock, fcntl.LOCK_UN)
                _recon_lock.close()

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
                "cost_recorded": pending_entry_cost > 0,
                "resolved_at": now.isoformat(),
            })
            continue

        # 未成交，记录调试信息
        print(f"  ⏳ reconcile: {slug} 未成交 filled_size={filled_size} age={int(age) if age else '?'}s")

        # ═══ 价格守卫：当前市价大幅低于挂单限价时，立即取消避免高位接盘 ═══
        limit_price = order.get("limit_price")
        if limit_price and limit_price > 0:
            current_ltp = get_last_trade_price(token_id)
            if current_ltp and current_ltp < limit_price * 0.95:
                deviation_pct = (limit_price - current_ltp) / limit_price * 100
                print(f"  🛡️ 价格守卫: 市价${current_ltp:.3f} < 挂单价${limit_price:.3f}×0.95 (偏离{deviation_pct:.1f}%)，取消挂单")
                cancel_all_orders(token_id)
                _append_pending_update({
                    "order_id": order.get("order_id"),
                    "pending_id": order.get("pending_id"),
                    "slug": slug,
                    "token_id": token_id,
                    "status": "PRICE_GUARD_CANCELLED",
                    "limit_price": limit_price,
                    "current_price": current_ltp,
                    "deviation_pct": round(deviation_pct, 1),
                    "resolved_at": now.isoformat(),
                })
                try:
                    coin = _coin_from_slug(slug)
                    msg = (
                        f"🛡️ <b>挂单价格保护取消</b>\n\n"
                        f"币种: {coin}\n"
                        f"方向: {order.get('direction')}\n"
                        f"挂单价: ${limit_price:.2f} → 市价: ${current_ltp:.3f}\n"
                        f"偏离: {deviation_pct:.1f}%，避免高位接盘"
                    )
                    send_telegram(msg)
                except Exception:
                    pass
                continue

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
                coin = _coin_from_slug(slug)
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


def get_token_balance(token_id):
    """查询钱包中指定 conditional token 余额（SDK直连）"""
    return clob_client.get_token_balance(token_id)

def check_balance_changed(token_id, expected_size):
    """通过查询token余额判断是否成交（余额减少=卖出成功）"""
    try:
        balance = get_token_balance(token_id)
        if balance is not None:
            threshold = max(0.01, expected_size * 0.01)
            return balance <= threshold
    except Exception:
        pass
    # 兼容旧部署：SDK 不可用时，尝试解析历史 CLI 余额输出。
    try:
        result = subprocess.run(["true"], capture_output=True, text=True, timeout=5)
        output = result.stdout or ""
        if f"token_id={token_id}" in output:
            import re
            match = re.search(rf"token_id={token_id}\s+balance:\s*([0-9.]+)", output)
            if match:
                balance = float(match.group(1))
                threshold = max(0.01, expected_size * 0.01)
                return balance <= threshold
            return True
        if "Token balances:" in output:
            return True
    except Exception:
        pass
    return False

def sell_and_confirm(token_id, size, price, timeout_sec=5, position=None):
    """FOK卖出（即时成交或取消，无需等待确认）
    返回: (success, msg_or_actual_price)
    """
    # 校验链上真实余额（传入position以同步更新持仓记录）
    adjusted = _check_and_adjust_size(token_id, size, position=position)
    if adjusted == -1:
        time.sleep(3)
        _balance_cache.pop(token_id, None)
        adjusted = _check_and_adjust_size(token_id, size, position=position)
        if adjusted == -1:
            adjusted = 0
    if adjusted == 0:
        return False, "NO_BALANCE"
    if adjusted is not None:
        size = adjusted

    price = round(price, 2)
    try:
        info = clob_client.place_fok_order(token_id, SELL, price, size)
        print(
            f"    [SELL] status={info['status']} matched={info['matched']} "
            f"making={info['making']:.4f} taking={info['taking']:.4f} | {info.get('elapsed_ms', 0):.0f}ms"
        )
        if info["matched"]:
            gross_price = round(info["taking"] / size, 4) if size > 0 and info["taking"] > 0 else price
            fill_summary = _sell_fill_summary(token_id, size, info.get("taking", 0), gross_price)
            actual_price = round(fill_summary["net_price"], 4)
            return True, actual_price

        err = info.get("error", "") or info.get("raw", "")

        # 余额/授权不足 → 刷新allowance重试
        if "not enough balance" in err.lower():
            if adjusted and adjusted > 0:
                print(f"    🔄 链上有余额但授权不足，刷新allowance后重试")
                clob_client.update_token_allowance(token_id)
                info2 = clob_client.place_fok_order(token_id, SELL, price, size)
                if info2["matched"]:
                    gross_price = round(info2["taking"] / size, 4) if size > 0 and info2["taking"] > 0 else price
                    fill_summary = _sell_fill_summary(token_id, size, info2.get("taking", 0), gross_price)
                    actual_price = round(fill_summary["net_price"], 4)
                    return True, actual_price
                err2 = info2.get("error", "") or info2.get("raw", "")
                if "not enough balance" in err2.lower():
                    return False, "NO_BALANCE"
                # allowance已修复但原价没人买 → 降价重试
            else:
                return False, "NO_BALANCE"

        # 原价无人买 → $0.01地板价兜底（接受任何买方出价）
        if price > 0.02:
            print(f"    🔄 原价无买方，降至$0.01地板价重试")
            info3 = clob_client.place_fok_order(token_id, SELL, 0.01, size)
            if info3["matched"]:
                gross_price = round(info3["taking"] / size, 4) if size > 0 and info3["taking"] > 0 else 0.01
                fill_summary = _sell_fill_summary(token_id, size, info3.get("taking", 0), gross_price)
                actual_price = round(fill_summary["net_price"], 4)
                print(
                    f"    ⚡ 地板价成交: gross=${gross_price:.4f} | net=${actual_price:.4f} "
                    f"| fee=${fill_summary['fee_usdc']:.4f} | {info3.get('elapsed_ms', 0):.0f}ms"
                )
                return True, actual_price
            err3 = info3.get("error", "") or info3.get("raw", "")
            if "not enough balance" in err3.lower():
                return False, "NO_BALANCE"
            print(f"    ❌ 地板价也无买方")

        return False, "FOK未成交"
    except Exception as e:
        print(f"    [SELL] exception: {str(e)[:200]}")
        if "not enough balance" in str(e).lower():
            if adjusted and adjusted > 0:
                print(f"    🔄 链上有余额但授权不足，刷新allowance后重试")
                clob_client.update_token_allowance(token_id)
                try:
                    info2 = clob_client.place_fok_order(token_id, SELL, price, size)
                    if info2["matched"]:
                        gross_price = round(info2["taking"] / size, 4) if size > 0 and info2["taking"] > 0 else price
                        fill_summary = _sell_fill_summary(token_id, size, info2.get("taking", 0), gross_price)
                        actual_price = round(fill_summary["net_price"], 4)
                        return True, actual_price
                except Exception:
                    pass
            return False, "NO_BALANCE"
        return False, str(e)

def sell_in_batches(token_id, total_size, base_price):
    """分批出货 - 确保全部卖完"""
    if total_size <= 5:
        success, actual = sell_and_confirm(token_id, total_size, base_price, timeout_sec=3)
        return success, total_size if success else 0, actual if success else None
    
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
        elif output == "NO_BALANCE":
            break  # 余额为零，不再继续分批
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
    # 启动 RTDS Chainlink 价格流（主数据源，Polymarket 直接结算价）
    _chainlink_stream.start()
    # 启动 Pyth 链上价格流（fallback）
    _pyth_stream.start()
    # 启动 Binance WebSocket 实时价格流（fallback）
    _price_stream.start()
    # 启动 Polymarket WebSocket 实时 orderbook 流
    _poly_ws.start()
    _ws_subscribed = set()  # 已订阅的 token_ids
    _src_label = "Chainlink优先" if PRICE_SOURCE == 1 else "Pyth优先"
    print(f"🔍 持仓监控 v9.8 启动（ATR三层决策 + {_src_label}止损优化 + Proximity衰减streak + 尾盘Oracle确认）...")
    print(f"   Exit Protocol: P0双曲止盈+TrailingTP | 绝对硬止损-{ABSOLUTE_HARD_STOP*100:.0f}% | ATR衰减止损 | -{PRICE_DROP_HARD_STOP*100:.0f}%硬止损 | 方向翻转 | ATR≥2抄底 | ATR<1止损 | 尾盘Oracle状态机")
    print(f"   参数: 触发线-{PRICE_DROP_TRIGGER*100:.0f}% | 硬止损-{PRICE_DROP_HARD_STOP*100:.0f}% | ATR安全≥{ATR_SAFE_THRESHOLD} | ATR危险<{ATR_DANGER_THRESHOLD}")
    print(f"   Proximity Buffer: {PTB_PROXIMITY_ATR}ATR | 极端安全阀-{PTB_PROXIMITY_EXTREME_STOP*100:.0f}%(时间衰减) | streak衰减+8轮滑动窗口")
    if ENABLE_ORACLE_STALE_WATCH:
        print(
            f"   Oracle Lead Watch: <= {ORACLE_STALE_WATCH_MAX_REMAINING:.0f}s | "
            f"|CL-BN|≥{ORACLE_STALE_WATCH_ATR:.2f}ATR × {ORACLE_STALE_WATCH_CONFIRMATIONS} | 仅告警"
        )

    close_attempts = {}  # (slug, entry_time) -> attempts count
    stop_loss_attempts = {}  # (slug, entry_time) -> stop loss attempts
    tp_state = {}  # (slug, entry_time) -> "FIRST_TOUCH" | "PULLED_BACK"
    dip_bought = {}  # (slug, entry_time) -> True if already dip-bought
    prev_direction_correct = {}  # (slug, entry_time) -> last known direction_correct
    tp_fail_count = {}  # (slug, entry_time) -> 连续止盈失败次数
    direction_wrong_streak = {}  # (slug, entry_time) -> 方向错误衰减计数（❌+1, ✅-1, 不清零）
    direction_history = {}  # (slug, entry_time) -> deque(maxlen=8) 最近8轮方向记录
    atr_history = {}  # (slug, entry_time) -> deque(maxlen=5) 最近5轮ATR值（速度检测用）
    entry_atr = {}  # (slug, entry_time) -> 入场时的ATR值
    atr_decay_confirm_streak = {}  # (slug, entry_time) -> ATR衰减止损连续确认次数
    close_intents = {}  # (slug, entry_time) -> {"reason": str}
    tail_oracle_prices = {}  # (slug, entry_time) -> deque(maxlen=7) 最近oracle价格
    tail_oracle_wrong_streak = {}  # (slug, entry_time) -> 尾盘oracle错误确认计数
    oracle_stale_watch_streak = {}  # (slug, entry_time) -> CL-BN 持续偏离确认轮数（仅告警）
    oracle_stale_watch_active = {}  # (slug, entry_time) -> 是否已进入快市场领先告警态
    ev_exit_confirm = {}  # (slug, entry_time) -> 连续EV退出确认轮数（EV-Gate用）
    bn_price_history = {}  # (slug, entry_time) -> deque of (ts, price) BN价格快照
    high_water_mark = {}  # v12.8: (slug, entry_time) -> 持仓期间最高利润率（trailing TP用）
    rapid_sl_confirms = {}  # v14: 快速止损方向❌连续确认轮数（防单次噪音触发）
    trailing_tp_active = {}  # v12.8: (slug, entry_time) -> True if trailing TP已激活
    last_wake_context = {"label": "startup", "detail": None}
    
    _orphan_last_scan = 0
    _orphan_known_tokens = set()  # 内存去重：已处理过的token永远不再写入

    while True:
        try:
            reconcile_pending_orders()

            # v12.9.7: 孤儿 token 扫描 — 每30秒查 data-api 持仓，发现未记录的 token 自动纳入管理
            _now_orphan = time.time()
            if _now_orphan - _orphan_last_scan >= 30:
                _orphan_last_scan = _now_orphan
                try:
                    _proxy = os.environ.get("PROXY_WALLET", "")
                    if _proxy:
                        _api_positions = requests.get(
                            "https://data-api.polymarket.com/positions",
                            params={"user": _proxy, "limit": 50, "sizeThreshold": 0.5},
                            timeout=10,
                        ).json()
                        if isinstance(_api_positions, list) and _api_positions:
                            # 从文件+内存双重去重
                            _known_tokens = set(_orphan_known_tokens)
                            if os.path.exists(POSITIONS_FILE):
                                with open(POSITIONS_FILE) as _of:
                                    for _ol in _of:
                                        try:
                                            _op = json.loads(_ol.strip())
                                            _known_tokens.add(_op.get("token_id", ""))
                                        except Exception:
                                            pass
                            for _ap in _api_positions:
                                _ap_asset = _ap.get("asset", "")
                                _ap_size = _ap.get("size", 0)
                                if _ap_asset and _ap_size > 0.5 and _ap_asset not in _known_tokens:
                                    # 链上余额交叉验证 — data-api的size是历史值，可能远大于实际持有
                                    _real_bal = clob_client.get_token_balance(_ap_asset)
                                    if _real_bal is None:
                                        continue  # 查询失败，不标记已知，下轮重试
                                    if _real_bal < 0.5:
                                        _orphan_known_tokens.add(_ap_asset)  # 确认是dust，永久跳过
                                        continue
                                    # 孤儿 token — 用链上真实余额写入
                                    _ap_size = _real_bal
                                    _orphan_record = {
                                        "token_id": _ap_asset,
                                        "slug": _ap.get("eventSlug", _ap.get("slug", "orphan")),
                                        "direction": _ap.get("outcome", "UP"),
                                        "entry_price": _ap.get("avgPrice", 0.5),
                                        "size": _ap_size,
                                        "entry_cost": round(_ap.get("avgPrice", 0.5) * _ap_size, 4),
                                        "entry_time": datetime.now(timezone.utc).isoformat(),
                                        "orphan_detected": True,
                                        "ambush": True,
                                    }
                                    try:
                                        _lfd = open(POSITIONS_LOCK, "w")
                                        fcntl.flock(_lfd, fcntl.LOCK_EX)
                                        with open(POSITIONS_FILE, "a") as _pf:
                                            _pf.write(json.dumps(_orphan_record) + "\n")
                                        fcntl.flock(_lfd, fcntl.LOCK_UN)
                                        _lfd.close()
                                        _orphan_known_tokens.add(_ap_asset)  # 内存去重
                                        print(
                                            f"  🔍 孤儿token检测: {_ap.get('outcome','?')} "
                                            f"{_ap_size:.1f}份 @${_ap.get('avgPrice',0):.2f} "
                                            f"| {_ap.get('title','')[:30]} → 纳入管理")
                                    except Exception as _we:
                                        print(f"  ⚠️ 孤儿token写入失败: {_we}")
                                    _orphan_known_tokens.add(_ap_asset)  # 即使写入失败也标记已知
                except Exception as _oe:
                    pass  # 扫描失败不影响主循环

            # v14.3: 定期回填 directional_won（独立于持仓，覆盖SL/TP早退记录）
            _periodic_outcomes_backfill()

            positions = get_open_positions()
            
            if not positions:
                time.sleep(3)
                continue

            open_keys = {(p.get("slug", "unknown"), p.get("entry_time", "")) for p in positions}
            close_attempts = {k: v for k, v in close_attempts.items() if k in open_keys}
            stop_loss_attempts = {k: v for k, v in stop_loss_attempts.items() if k in open_keys}
            tp_state = {k: v for k, v in tp_state.items() if k in open_keys}
            dip_bought = {k: v for k, v in dip_bought.items() if k in open_keys}
            prev_direction_correct = {k: v for k, v in prev_direction_correct.items() if k in open_keys}
            tp_fail_count = {k: v for k, v in tp_fail_count.items() if k in open_keys}
            direction_wrong_streak = {k: v for k, v in direction_wrong_streak.items() if k in open_keys}
            direction_history = {k: v for k, v in direction_history.items() if k in open_keys}
            atr_history = {k: v for k, v in atr_history.items() if k in open_keys}
            entry_atr = {k: v for k, v in entry_atr.items() if k in open_keys}
            atr_decay_confirm_streak = {k: v for k, v in atr_decay_confirm_streak.items() if k in open_keys}
            close_intents = {k: v for k, v in close_intents.items() if k in open_keys}
            tail_oracle_prices = {k: v for k, v in tail_oracle_prices.items() if k in open_keys}
            tail_oracle_wrong_streak = {k: v for k, v in tail_oracle_wrong_streak.items() if k in open_keys}
            oracle_stale_watch_streak = {k: v for k, v in oracle_stale_watch_streak.items() if k in open_keys}
            oracle_stale_watch_active = {k: v for k, v in oracle_stale_watch_active.items() if k in open_keys}
            ev_exit_confirm = {k: v for k, v in ev_exit_confirm.items() if k in open_keys}
            bn_price_history = {k: v for k, v in bn_price_history.items() if k in open_keys}
            high_water_mark = {k: v for k, v in high_water_mark.items() if k in open_keys}
            trailing_tp_active = {k: v for k, v in trailing_tp_active.items() if k in open_keys}
            rapid_sl_confirms = {k: v for k, v in rapid_sl_confirms.items() if k in open_keys}
            # v14.3: 清理 TP 缓存/节流状态中已关闭仓位的条目
            for _stale_key in [k for k in _last_tp_rebuild if k not in open_keys]:
                _last_tp_rebuild.pop(_stale_key, None)
            _active_tp_oids = {p.get("tp_order_id") for p in positions if p.get("tp_order_id")}
            for _stale_oid in [k for k in _tp_status_cache if k not in _active_tp_oids]:
                _tp_status_cache.pop(_stale_oid, None)

            # 退订已关闭持仓的 token_ids
            active_token_ids = set()
            for p in positions:
                active_token_ids.add(p.get("token_id", ""))
                ot = p.get("opposite_token_id")
                if ot:
                    active_token_ids.add(ot)
            stale_ids = _ws_subscribed - active_token_ids
            if stale_ids:
                _poly_ws.unsubscribe(list(stale_ids))
                _ws_subscribed -= stale_ids
            
            for pos in positions:
                token_id = pos["token_id"]
                entry_price = pos["entry_price"]
                size = pos.get("token_balance") or pos["size"]
                slug = pos.get("slug", "unknown")
                entry_time = pos.get("entry_time", "")
                attempt_key = (slug, entry_time)
                coin = _coin_from_slug(slug)
                direction = pos.get("direction", "UP")
                current_price = None

                # 预缓存 neg_risk/fee_rate，避免平仓时额外HTTP查询
                clob_client.precache_token(token_id)
                opposite_token = pos.get("opposite_token_id")
                if opposite_token:
                    clob_client.precache_token(opposite_token)

                # 自动订阅 Polymarket WS（首次见到的 token_id）
                # v12.8: subscribe + seed_from_rest 确保首轮即有WS数据，不会age=9999
                if token_id not in _ws_subscribed:
                    sub_ids = [token_id]
                    if opposite_token:
                        sub_ids.append(opposite_token)
                    _poly_ws.subscribe(sub_ids)
                    _poly_ws.seed_from_rest(sub_ids, clob_client)
                    _ws_subscribed.update(sub_ids)

                # 获取市场剩余时间
                end_timestamp = resolve_market_end_time(slug, entry_time, pos)
                if not end_timestamp:
                    continue
                
                now = datetime.now(timezone.utc)
                end_time = datetime.fromtimestamp(end_timestamp, tz=timezone.utc)
                remaining = (end_time - now).total_seconds()
                
                ptb_price = pos.get("ptb") or pos.get("price_to_beat")
                crypto_debug = get_current_crypto_price_debug(coin)
                crypto_price = crypto_debug.get("price")
                if ptb_price and crypto_price and remaining <= 5:
                    freeze_settlement_reference_price(pos, crypto_price)

                # 市场已关闭（remaining <= 0）→ 只做结算/清理
                if remaining <= 0:
                    if remaining < -30:
                        # 自动清理：优先用API查真实结算结果
                        settle_price = get_market_outcome(slug, direction)
                        if settle_price is None:
                            # API无结果，回退链上价格判断
                            ptb = ptb_price
                            crypto, src = get_settlement_reference_price(pos, coin)
                            if ptb and crypto:
                                won = (direction == "UP" and crypto > ptb) or (direction == "DOWN" and crypto < ptb)
                                settle_price = 1.00 if won else 0.00
                                print(f"  ⚠️ API无outcome，用{src}回退判断")
                            else:
                                # 孤儿无ptb: 尝试从WS/API获取token价（已结算≈0或1）
                                _fb = get_best_bid_raw(token_id)
                                if _fb is not None:
                                    settle_price = 1.00 if _fb > 0.5 else 0.00
                                    print(f"  ⚠️ 孤儿无ptb，用token价${_fb:.2f}→结算={settle_price:.0f}")
                                else:
                                    _raw_ltp = _safe_float(clob_client.get_last_trade_price(token_id))
                                    if _raw_ltp is not None:
                                        settle_price = 1.00 if _raw_ltp > 0.5 else 0.00
                                        print(f"  ⚠️ 孤儿无ptb，用LTP=${_raw_ltp:.2f}→结算={settle_price:.0f}")
                                    else:
                                        settle_price = entry_price
                                        print(f"  ⚠️ 孤儿无ptb且无市场数据，回退entry_price=${entry_price}")
                        close_position(pos, settle_price)
                        # v14.3: directional_won 回填已改为 _periodic_outcomes_backfill() 定期扫描
                        result_emoji = "🟢" if settle_price > 0.5 else "🔴"
                        print(f"  {result_emoji} 清理过期持仓: {slug} (过期{-remaining:.0f}s) 结算价=${settle_price:.2f}")
                        close_attempts.pop(attempt_key, None)
                        stop_loss_attempts.pop(attempt_key, None)
                        dip_bought.pop(attempt_key, None)
                        direction_wrong_streak.pop(attempt_key, None)
                        direction_history.pop(attempt_key, None)
                        continue

                    # -30s ~ 0s：等待结算并检查市场关闭
                    if int(-remaining) % 10 < 3:
                        print(f"  ⏳ {slug} 已关闭，等待结算 | 过期{-remaining:.0f}s")

                    if check_market_closed(slug):
                        # 优先用API查真实结算结果
                        settle_price = get_market_outcome(slug, direction)
                        if settle_price is None:
                            ptb = ptb_price
                            crypto, src = get_settlement_reference_price(pos, coin)
                            if ptb and crypto:
                                won = (direction == "UP" and crypto > ptb) or (direction == "DOWN" and crypto < ptb)
                                settle_price = 1.00 if won else 0.00
                                print(f"  ⚠️ API无outcome，用{src}回退判断")
                            else:
                                _fb = get_best_bid_raw(token_id)
                                if _fb is not None:
                                    settle_price = 1.00 if _fb > 0.5 else 0.00
                                    print(f"  ⚠️ 孤儿无ptb，用token价${_fb:.2f}→结算={settle_price:.0f}")
                                else:
                                    _raw_ltp = _safe_float(clob_client.get_last_trade_price(token_id))
                                    if _raw_ltp is not None:
                                        settle_price = 1.00 if _raw_ltp > 0.5 else 0.00
                                        print(f"  ⚠️ 孤儿无ptb，用LTP=${_raw_ltp:.2f}→结算={settle_price:.0f}")
                                    else:
                                        settle_price = entry_price
                                        print(f"  ⚠️ 孤儿无ptb且无市场数据，回退entry_price=${entry_price}")
                        result_emoji = "🟢" if settle_price > 0.5 else "🔴"
                        print(f"  {result_emoji} {slug} 已关闭 结算价=${settle_price:.2f}")
                        close_position(pos, settle_price)
                        close_attempts.pop(attempt_key, None)
                        stop_loss_attempts.pop(attempt_key, None)
                        dip_bought.pop(attempt_key, None)
                        direction_wrong_streak.pop(attempt_key, None)
                        direction_history.pop(attempt_key, None)
                        close_intents.pop(attempt_key, None)
                    continue

                # 获取当前token价格（仅在市场未过期时需要）
                current_price = get_market_price(token_id)
                if current_price is None:
                    continue
                market_obs = _get_market_observability(token_id)
                
                profit_rate = (current_price - entry_price) / entry_price if entry_price > 0 else 0
                atr_val = pos.get("atr_val") or get_atr_from_binance(coin)
                oracle_watch = evaluate_oracle_stale_watch(
                    crypto_debug,
                    atr_val,
                    remaining,
                    oracle_stale_watch_streak.get(attempt_key, 0),
                    oracle_stale_watch_active.get(attempt_key, False),
                )
                if oracle_watch["next_streak"] > 0:
                    oracle_stale_watch_streak[attempt_key] = oracle_watch["next_streak"]
                else:
                    oracle_stale_watch_streak.pop(attempt_key, None)
                if oracle_watch["active"]:
                    oracle_stale_watch_active[attempt_key] = True
                else:
                    oracle_stale_watch_active.pop(attempt_key, None)

                # 用加密货币价格判断实际方向（不依赖可能失真的 token 价格）
                if ptb_price and crypto_price:
                    single_tick_direction_correct = (
                        (direction == "UP" and crypto_price > ptb_price) or
                        (direction == "DOWN" and crypto_price < ptb_price)
                    )
                else:
                    single_tick_direction_correct = None

                tail_info = None
                direction_signal_correct = single_tick_direction_correct
                direction_correct = single_tick_direction_correct
                if ptb_price and crypto_price and remaining <= 60:
                    history = tail_oracle_prices.setdefault(attempt_key, deque(maxlen=TAIL_ORACLE_WINDOW))
                    history.append(crypto_price)
                    tail_info = classify_tail_oracle_state(
                        direction,
                        ptb_price,
                        atr_val,
                        list(history),
                        remaining,
                        tail_oracle_wrong_streak.get(attempt_key, 0),
                    )
                    if tail_info["active"]:
                        tail_oracle_wrong_streak[attempt_key] = tail_info["wrong_streak"]
                        direction_signal_correct = tail_info["raw_direction_correct"]
                        direction_correct = tail_info["effective_direction_correct"]
                    else:
                        tail_oracle_wrong_streak.pop(attempt_key, None)
                else:
                    tail_oracle_wrong_streak.pop(attempt_key, None)

                true_direction_correct = direction_correct

                # PTB 在下注时已记录，直接从持仓数据读取
                is_losing = bool(tail_info and tail_info["active"] and tail_info["state"] == "wrong_confirmed")
                is_winning = bool(tail_info and tail_info["active"] and direction_correct is True)
                if not (tail_info and tail_info["active"]):
                    is_losing = is_losing_direction(direction, crypto_price, ptb_price, remaining) if ptb_price and crypto_price else False
                    if ptb_price and crypto_price and remaining <= 60:
                        is_winning = (direction == "UP" and crypto_price > ptb_price) or (direction == "DOWN" and crypto_price < ptb_price)
                    else:
                        is_winning = False

                status = "🟢赢" if is_winning else "🔴输" if is_losing else "⚪"
                # 补充显示：用加密货币方向替代可能失真的 token 利润率
                price_source = crypto_debug.get("source") or "N/A"
                crypto_label = f" | {coin}=${crypto_price:,.2f}({price_source})" if crypto_price else ""
                if direction_correct is not None:
                    dir_icon = "✅" if direction_correct else "❌"
                    print(f"  📈 {coin} {direction} | ${entry_price:.2f}→${current_price:.2f} ({profit_rate*100:+.1f}%) | 剩余{remaining:.0f}s | {status} | 方向{dir_icon}{crypto_label}")
                else:
                    print(f"  📈 {coin} {direction} | ${entry_price:.2f}→${current_price:.2f} ({profit_rate*100:+.1f}%) | 剩余{remaining:.0f}s | {status}{crypto_label}")
                observability = _format_price_observability(crypto_debug, market_obs, atr_val, last_wake_context)
                if observability:
                    print(f"  📡 源观测: {observability}")
                if oracle_watch["triggered"]:
                    print(
                        f"  ⚠️ 快市场领先告警(CL-BN): {oracle_watch['skew']:+.2f} "
                        f"({oracle_watch['skew_atr']:+.2f}ATR) | "
                        f"{oracle_watch['next_streak']}/{oracle_watch['required_confirms']}轮确认 | "
                        f"CL={_format_age_ms(oracle_watch.get('cl_age_ms'))}/src={_format_age_ms(oracle_watch.get('cl_source_age_ms'))} "
                        f"BN={_format_age_ms(oracle_watch.get('bn_age_ms'))}/src={_format_age_ms(oracle_watch.get('bn_source_age_ms'))} | "
                        f"剩余{remaining:.0f}s"
                    )
                elif oracle_watch["recovered"]:
                    skew_label = "N/A"
                    if oracle_watch["skew"] is not None and oracle_watch["skew_atr"] is not None:
                        skew_label = f"{oracle_watch['skew']:+.2f} ({oracle_watch['skew_atr']:+.2f}ATR)"
                    print(f"  ✅ 快市场领先恢复(CL-BN): {skew_label} | 剩余{remaining:.0f}s")
                if tail_info and tail_info["active"]:
                    median_gap = tail_info["median_gap"]
                    enter_margin = tail_info["enter_margin"]
                    median_gap_atr = (median_gap / atr_val) if median_gap is not None and atr_val else None
                    enter_margin_atr = (enter_margin / atr_val) if enter_margin is not None and atr_val else None
                    if tail_info["state"] == "wrong_pending":
                        gap_label = f"{median_gap_atr:+.2f}ATR" if median_gap_atr is not None else f"${median_gap:+.2f}"
                        margin_label = f"{enter_margin_atr:.2f}ATR" if enter_margin_atr is not None else f"${enter_margin:.2f}"
                        print(
                            f"  ⏸️ 尾盘Oracle待确认: {tail_info['wrong_streak']}/{tail_info['required_confirms']}轮 | "
                            f"median={gap_label} <= -{margin_label}"
                        )
                    elif tail_info["state"] == "wrong_confirmed":
                        gap_label = f"{median_gap_atr:+.2f}ATR" if median_gap_atr is not None else f"${median_gap:+.2f}"
                        print(f"  🚨 尾盘Oracle确认翻面: median={gap_label} | 连续{tail_info['wrong_streak']}轮")
                    elif single_tick_direction_correct is False and direction_correct is True:
                        gap_label = f"{median_gap_atr:+.2f}ATR" if median_gap_atr is not None else f"${median_gap:+.2f}"
                        print(f"  🔶 尾盘Oracle过滤单tick反向: median={gap_label} | state={tail_info['state']}")

                sold = False
                sold_price = 0
                attempted_close = False
                attempted_stop_loss = False
                stop_loss_attempt_recorded = False
                ev_gate = None

                if pos.get("close_intent_active") and attempt_key not in close_intents:
                    close_intents[attempt_key] = {
                        "reason": pos.get("close_intent_reason") or "锁定平仓",
                    }

                active_close_intent = close_intents.get(attempt_key)
                if active_close_intent:
                    reason = active_close_intent.get("reason") or "锁定平仓"
                    prior_attempts = stop_loss_attempts.get(attempt_key, 0)
                    # v12: 强制升级 — 连续失败 5 次后用地板价 FOK 清仓
                    if prior_attempts >= 5:
                        print(f"  🚨 强制清仓: {reason} | 连续{prior_attempts}次失败 → 地板价$0.01退出")
                        from py_clob_client.order_builder.constants import SELL
                        floor_info = clob_client.place_fok_order(token_id, SELL, 0.01, size)
                        if floor_info.get("matched"):
                            floor_price = round(floor_info.get("taking", 0) / size, 4) if size > 0 else 0.01
                            sold = True
                            sold_price = floor_price
                            self_notify(pos, sold_price, coin, direction, size, f"{reason}(强制清仓)")
                            _clear_close_intent(pos)
                            close_position(pos, sold_price)
                            for d in (close_attempts, stop_loss_attempts, dip_bought, direction_wrong_streak, direction_history, close_intents):
                                d.pop(attempt_key, None)
                            continue
                    print(f"  🔒 平仓锁定: {reason} | 已重试{prior_attempts}次 | 继续退出")
                    attempted_stop_loss = True
                    ok, actual_price = market_sell_immediate(token_id, size, position=pos)
                    if ok:
                        sold = True
                        sold_price = actual_price
                        self_notify(pos, sold_price, coin, direction, size, reason)
                        _clear_close_intent(pos)
                        close_position(pos, sold_price)
                        close_attempts.pop(attempt_key, None)
                        stop_loss_attempts.pop(attempt_key, None)
                        dip_bought.pop(attempt_key, None)
                        direction_wrong_streak.pop(attempt_key, None)
                        direction_history.pop(attempt_key, None)
                        close_intents.pop(attempt_key, None)
                        continue
                    if actual_price == "NO_BALANCE":
                        est_exit = _resolve_or_estimate_exit_price(token_id, current_price, entry_price, slug, direction)
                        print("  ⚠️ 余额为零，标记持仓关闭等结算")
                        self_notify(pos, est_exit, coin, direction, size, f"{reason}(余额已清)")
                        _clear_close_intent(pos)
                        close_position(pos, est_exit)
                        close_attempts.pop(attempt_key, None)
                        stop_loss_attempts.pop(attempt_key, None)
                        dip_bought.pop(attempt_key, None)
                        direction_wrong_streak.pop(attempt_key, None)
                        direction_history.pop(attempt_key, None)
                        close_intents.pop(attempt_key, None)
                        continue
                    stop_loss_attempt_recorded = True
                    stop_loss_attempts[attempt_key] = prior_attempts + 1
                    continue

                # ═══ EV 持续监控（Exit Protocol）═══
                # 二元市场: token_price ≈ 市场隐含胜率 → EV = token_price - entry_price
                # 这是最直接最准确的EV，不依赖理论模型
                market_ev = current_price - entry_price if current_price else None
                ev_label_global = f"EV={market_ev:+.3f}" if market_ev is not None else "EV=N/A"

                # ═══ 伏击仓位标记（提前识别，供后续止损/止盈分流）═══
                _is_ambush_pos = pos.get("ambush", False)

                # ═══ v12.8: 绝对硬止损 — 不看方向/EV/ATR，跌到就走 ═══
                # v12.9: 最后60秒放宽到-70%（噪音洗出去比亏损更贵，让结算自然完成）
                # v13.1: 伏击仓位用更宽的阈值（AMBUSH_HARD_STOP，默认-70%）
                if _is_ambush_pos and AMBUSH_HOLD_TO_EXPIRY:
                    _hard_stop_threshold = AMBUSH_HARD_STOP
                else:
                    _hard_stop_threshold = ABSOLUTE_HARD_STOP if remaining > 60 else 0.70
                if profit_rate <= -_hard_stop_threshold and remaining > 10:
                    _hard_label = f"-{_hard_stop_threshold*100:.0f}%" + ("(宽松)" if remaining <= 60 else "")
                    print(
                        f"  💀 绝对硬止损: {profit_rate*100:+.1f}% 超过{_hard_label} | "
                        f"方向{'✅' if direction_correct else '❌'} | {ev_label_global} | 剩余{remaining:.0f}s"
                    )
                    attempted_stop_loss = True
                    # v14: 伏击仓位TP保护 — 撤单前先确认TP状态
                    _hs_tp_oid = pos.get("tp_order_id") if _is_ambush_pos else None
                    _hs_tp_confirmed_dead = not _hs_tp_oid
                    if _hs_tp_oid:
                        try:
                            _hs_tp_info = clob_client.get_order(_hs_tp_oid)
                            _hs_tp_status = (_hs_tp_info.get("status", "") if _hs_tp_info else "").upper()
                            if _hs_tp_status in ("MATCHED", "FILLED"):
                                _hs_tp_price = pos.get("tp_price", current_price)
                                _hs_ord_price = float(_hs_tp_info.get("price") or 0)
                                if _hs_ord_price > 0:
                                    _hs_tp_price = _hs_ord_price
                                print(f"  💰 硬止损路径: TP已成交@${_hs_tp_price:.2f}，用TP价格关仓")
                                self_notify(pos, _hs_tp_price, coin, direction, size, "伏击GTD止盈(硬止损路径)")
                                close_position(pos, _hs_tp_price)
                                close_attempts.pop(attempt_key, None)
                                continue
                            elif _hs_tp_status in ("CANCELLED", "CANCELED", "EXPIRED"):
                                _hs_tp_confirmed_dead = True
                            else:
                                _hs_tp_confirmed_dead = False
                        except Exception:
                            _hs_tp_confirmed_dead = False  # 查询失败，不能确认TP已死
                    _hs_cancel_ok = cancel_all_orders(token_id)
                    if not _hs_tp_confirmed_dead and not _hs_cancel_ok:
                        print(f"  ⚠️ 硬止损: TP可能仍活跃且撤单失败，跳过卖出")
                        continue
                    ok, actual_price = market_sell_immediate(token_id, size, position=pos, remaining=0)
                    if actual_price == "NO_BALANCE":
                        print(f"  ⚠️ 绝对硬止损: 余额为零，等结算")
                        _exit_price = _resolve_or_estimate_exit_price(token_id, current_price, entry_price, slug, direction)
                        # v14: NO_BALANCE时回查TP是否刚成交
                        if _hs_tp_oid:
                            try:
                                _hs_tp_recheck = clob_client.get_order(_hs_tp_oid)
                                _hs_re_status = (_hs_tp_recheck.get("status", "") if _hs_tp_recheck else "").upper()
                                if _hs_re_status in ("MATCHED", "FILLED"):
                                    _hs_fill_price = pos.get("tp_price", 0)
                                    _hs_ord_price2 = float(_hs_tp_recheck.get("price") or 0)
                                    if _hs_ord_price2 > 0:
                                        _hs_fill_price = _hs_ord_price2
                                    if _hs_fill_price > 0:
                                        print(f"  💰 NO_BALANCE: TP已成交@${_hs_fill_price:.2f}，用TP价格关仓")
                                        _exit_price = _hs_fill_price
                            except Exception:
                                pass
                        self_notify(pos, _exit_price, coin, direction, size, f"绝对硬止损(-{ABSOLUTE_HARD_STOP*100:.0f}%)(余额已清)")
                        close_position(pos, _exit_price)
                        continue
                    if ok:
                        sold = True
                        sold_price = actual_price
                        self_notify(pos, sold_price, coin, direction, size, f"绝对硬止损(-{ABSOLUTE_HARD_STOP*100:.0f}%)")
                        close_position(pos, sold_price)
                        # v12.9.8: 止损后反向入场
                        try_reverse_entry(pos, remaining, atr_val=atr_val, ptb_price=ptb_price)
                        continue

                # ═══ v14: 伏击分阶段时间衰减止盈止损 ═══
                if _is_ambush_pos and AMBUSH_HOLD_TO_EXPIRY:
                    # ── 计算持仓时长 ──
                    _entry_ts_str = pos.get("entry_time", "")
                    _held_seconds = 0
                    if _entry_ts_str:
                        try:
                            _entry_dt = datetime.fromisoformat(_entry_ts_str.replace("Z", "+00:00"))
                            _held_seconds = (now - _entry_dt).total_seconds()
                        except Exception:
                            pass

                    # ── Step 1: TP单状态检查（v14.3: zone-based缓存，冷区5s/暖区2s跳过HTTP）──
                    _tp_oid = pos.get("tp_order_id")
                    _tp_active = False
                    _tp_confirmed_dead = not _tp_oid  # 无oid → 确认无TP
                    if _tp_oid:
                        # zone TTL: 根据 remaining + profit 选择查询频率
                        if remaining < 30 or abs(profit_rate) > 0.40:
                            _tp_ttl = 0.0  # 热区: 即将结算或极端盈亏 → 每次查
                        elif remaining < 60 or profit_rate > (pos.get("tp_price", 1.0) / entry_price - 1) * 0.7:
                            _tp_ttl = _TP_CACHE_WARM  # 暖区: 临近结算或利润接近TP → 2s
                        else:
                            _tp_ttl = _TP_CACHE_COLD  # 冷区: 安全观望 → 5s
                        try:
                            _tp_info = _get_tp_status(_tp_oid, ttl=_tp_ttl)
                            _tp_status = (_tp_info.get("status", "") if _tp_info else "").upper()
                            if _tp_status in ("MATCHED", "FILLED"):
                                _tp_status_cache.pop(_tp_oid, None)  # 已成交，清缓存
                                _tp_price = pos.get("tp_price", current_price)
                                print(f"  💰 伏击止盈成交! @${_tp_price:.2f} ({profit_rate*100:+.1f}%)")
                                self_notify(pos, _tp_price, coin, direction, size, "伏击GTD止盈")
                                close_position(pos, _tp_price)
                                close_attempts.pop(attempt_key, None)
                                continue
                            elif _tp_status in ("CANCELLED", "CANCELED", "EXPIRED"):
                                _tp_status_cache.pop(_tp_oid, None)  # 已终结，清缓存
                                _tp_confirmed_dead = True  # TP已终结，明确证伪
                                pos["tp_order_id"] = ""  # 清oid让_need_tp_rebuild生效
                                pos["tp_price"] = 0
                            else:
                                _tp_active = True  # TP仍活跃
                                _tp_confirmed_dead = False
                        except Exception as _tp_e:
                            print(f"  ⚠️ 止盈单查询异常: {_tp_e}")
                            _tp_confirmed_dead = False  # 查询失败，不能确认TP已死

                    # ── NO_BALANCE时TP竞态回查辅助函数 ──
                    def _resolve_exit_with_tp_check(_tp_oid_local, _fallback_price):
                        """NO_BALANCE时先查TP是否刚成交，用TP价格代替估算价"""
                        if _tp_oid_local:
                            try:
                                _tp_recheck = clob_client.get_order(_tp_oid_local)
                                _tp_re_status = (_tp_recheck.get("status", "") if _tp_recheck else "").upper()
                                if _tp_re_status in ("MATCHED", "FILLED"):
                                    _tp_fill_price = pos.get("tp_price", 0)
                                    _ord_price = float(_tp_recheck.get("price") or 0)
                                    if _ord_price > 0:
                                        _tp_fill_price = _ord_price
                                    if _tp_fill_price > 0:
                                        print(f"  💰 NO_BALANCE: TP已成交@${_tp_fill_price:.2f}，用TP价格关仓")
                                        return _tp_fill_price
                            except Exception:
                                pass
                        return _fallback_price

                    # ── Step 2: 方向确认窗口（成交后AMBUSH_CONFIRM_WINDOW秒内）──
                    if _held_seconds > 0 and _held_seconds <= AMBUSH_CONFIRM_WINDOW and crypto_price and ptb_price:
                        _dir_upper = direction.upper()
                        _btc_move = (crypto_price - ptb_price) / ptb_price
                        _dir_confirmed = (_dir_upper == "UP" and _btc_move > 0) or (_dir_upper == "DOWN" and _btc_move < 0)
                        _btc_reversed = abs(_btc_move) > AMBUSH_CONFIRM_THRESHOLD and not _dir_confirmed
                        if _btc_reversed:
                            print(
                                f"  🚫 方向确认失败: {coin} {direction} | BTC {_btc_move*100:+.4f}% 反向 "
                                f"| 持仓{_held_seconds:.0f}s → 立即止损")
                            _cancel_ok = cancel_all_orders(token_id)
                            if not _tp_confirmed_dead and not _cancel_ok:
                                print(f"  ⚠️ TP可能仍活跃且撤单失败，跳过方向确认止损")
                                continue
                            attempted_stop_loss = True
                            ok, actual_price = market_sell_immediate(token_id, size, position=pos, remaining=0)
                            if actual_price == "NO_BALANCE":
                                _fallback = _resolve_or_estimate_exit_price(token_id, current_price, entry_price, slug, direction)
                                _exit_price = _resolve_exit_with_tp_check(_tp_oid, _fallback)
                                self_notify(pos, _exit_price, coin, direction, size, "伏击方向确认失败(余额已清)")
                                close_position(pos, _exit_price)
                                continue
                            if ok:
                                self_notify(pos, actual_price, coin, direction, size, "伏击方向确认失败")
                                close_position(pos, actual_price)
                                continue

                    _ambush_diff_atr = None
                    if crypto_price and ptb_price and atr_val:
                        _, _, _ambush_diff_atr = calc_realtime_ev(
                            direction, crypto_price, ptb_price, atr_val, entry_price
                        )
                    if not (tail_info and tail_info["active"]):
                        _ambush_proximity = evaluate_proximity_guard(
                            direction_correct=direction_correct,
                            true_direction_correct=true_direction_correct,
                            diff_atr=_ambush_diff_atr,
                            remaining=remaining,
                            profit_rate=profit_rate,
                            wrong_streak=direction_wrong_streak.get(attempt_key, 0),
                            direction_history=direction_history.get(attempt_key),
                        )
                        if _ambush_proximity["freeze"]:
                            print(
                                f"  🔶 伏击PTB死区保护: {_ambush_diff_atr:.2f}ATR<"
                                f"{_ambush_proximity['prox_threshold']:.2f} | 方向冻结✅ "
                                f"| streak={_ambush_proximity['projected_streak']} "
                                f"ratio={_ambush_proximity['wrong_ratio']:.0%} | 剩余{remaining:.0f}s"
                            )
                            direction_correct = True

                    # ── Step 3: 分阶段止损 ──
                    _ambush_sl_triggered = False
                    if _held_seconds <= AMBUSH_RAPID_SL_WINDOW:
                        _RAPID_SL_CONFIRMS_NEEDED = 3  # 需要连续3轮方向❌确认
                        # v14.3: 硬截断 — 确认期内亏损超过MID_SL直接退出，不等3轮确认
                        # 防止从-25%滑到-47%的滑点问题（Trade #5/#10实证）
                        if direction_correct is False and profit_rate <= -AMBUSH_MID_SL:
                            rapid_sl_confirms.pop(attempt_key, None)
                            print(
                                f"  ⚡ 快速止损硬截断: {profit_rate*100:+.1f}% 超过-{AMBUSH_MID_SL*100:.0f}% "
                                f"| 持仓{_held_seconds:.0f}s | 方向❌ → 跳过确认直接退出")
                            _ambush_sl_triggered = True
                        elif direction_correct is False and profit_rate <= -AMBUSH_RAPID_SL:
                            # 方向错误+亏损超阈值: 累积确认
                            _rsl_count = rapid_sl_confirms.get(attempt_key, 0) + 1
                            rapid_sl_confirms[attempt_key] = _rsl_count
                            if _rsl_count >= _RAPID_SL_CONFIRMS_NEEDED:
                                print(
                                    f"  ⚡ 伏击快速止损: {profit_rate*100:+.1f}% 超过-{AMBUSH_RAPID_SL*100:.0f}% "
                                    f"| 持仓{_held_seconds:.0f}s | 方向❌×{_rsl_count} → 确认退出")
                                _ambush_sl_triggered = True
                            else:
                                print(
                                    f"  ⏸️ 快速止损确认中: {profit_rate*100:+.1f}% 方向❌ "
                                    f"| {_rsl_count}/{_RAPID_SL_CONFIRMS_NEEDED}轮 | 持仓{_held_seconds:.0f}s")
                        elif direction_correct is True:
                            # v14.3 Step2: 方向✅时用自适应阈值（base + 噪音容忍）
                            _effective_sl = AMBUSH_MID_SL + AMBUSH_NOISE_TOLERANCE  # -35%+10% = -45%
                            if profit_rate <= -_effective_sl:
                                rapid_sl_confirms.pop(attempt_key, None)
                                print(
                                    f"  ⚡ 伏击快速止损(方向✅深亏): {profit_rate*100:+.1f}% 超过-{_effective_sl*100:.0f}% "
                                    f"| 持仓{_held_seconds:.0f}s → 流动性异常，止损退出")
                                _ambush_sl_triggered = True
                        else:
                            rapid_sl_confirms.pop(attempt_key, None)  # 条件不满足，重置计数
                    elif remaining <= 60:
                        if profit_rate <= -AMBUSH_LATE_SL:
                            print(
                                f"  ⏰ 伏击尾盘止损: {profit_rate*100:+.1f}% 超过-{AMBUSH_LATE_SL*100:.0f}% "
                                f"| 剩余{remaining:.0f}s → 止损优于骑到$0")
                            _ambush_sl_triggered = True
                    else:
                        # v14.3 Step2: 中期止损 — 方向✅时加宽容忍度，随时间衰减
                        _effective_mid_sl = AMBUSH_MID_SL
                        if direction_correct is True and AMBUSH_NOISE_TOLERANCE > 0:
                            if remaining >= 120:
                                _effective_mid_sl = AMBUSH_MID_SL + AMBUSH_NOISE_TOLERANCE  # 全量容忍
                            elif remaining > 60:
                                _decay = (remaining - 60) / 60.0  # 120s→60s 线性衰减
                                _effective_mid_sl = AMBUSH_MID_SL + AMBUSH_NOISE_TOLERANCE * _decay
                            # remaining <= 60 时不加容忍，用原始 MID_SL
                        if profit_rate <= -_effective_mid_sl:
                            print(
                                f"  📉 伏击中期止损: {profit_rate*100:+.1f}% 超过-{_effective_mid_sl*100:.0f}% "
                                f"| 剩余{remaining:.0f}s | 方向{'✅' if direction_correct else '❌'}"
                                f"{' (噪音容忍)' if _effective_mid_sl > AMBUSH_MID_SL else ''}")
                            _ambush_sl_triggered = True

                    if _ambush_sl_triggered:
                        _cancel_ok = cancel_all_orders(token_id)
                        if not _tp_confirmed_dead and not _cancel_ok:
                            print(f"  ⚠️ TP可能仍活跃且撤单失败，跳过分阶段止损")
                            continue
                        attempted_stop_loss = True
                        ok, actual_price = market_sell_immediate(token_id, size, position=pos, remaining=0)
                        if actual_price == "NO_BALANCE":
                            _fallback = _resolve_or_estimate_exit_price(token_id, current_price, entry_price, slug, direction)
                            _exit_price = _resolve_exit_with_tp_check(_tp_oid, _fallback)
                            self_notify(pos, _exit_price, coin, direction, size, "伏击分阶段止损(余额已清)")
                            close_position(pos, _exit_price)
                            continue
                        if ok:
                            self_notify(pos, actual_price, coin, direction, size, "伏击分阶段止损")
                            close_position(pos, actual_price)
                            try_reverse_entry(pos, remaining, atr_val=atr_val, ptb_price=ptb_price)
                            continue

                    # ── Step 4: Breakeven锁定 ──
                    if profit_rate >= AMBUSH_BREAKEVEN_TRIGGER and _held_seconds > AMBUSH_RAPID_SL_WINDOW:
                        _be_price = round(entry_price * (1 + AMBUSH_BREAKEVEN_MARGIN), 4)
                        if current_price and current_price < _be_price:
                            print(
                                f"  🔒 Breakeven止损: 曾盈利{AMBUSH_BREAKEVEN_TRIGGER*100:.0f}%+ "
                                f"但回撤到${current_price:.3f} < 保本${_be_price:.3f} → 锁定退出")
                            _cancel_ok = cancel_all_orders(token_id)
                            if not _tp_confirmed_dead and not _cancel_ok:
                                print(f"  ⚠️ TP可能仍活跃且撤单失败，跳过Breakeven止损")
                                continue
                            attempted_stop_loss = True
                            ok, actual_price = market_sell_immediate(token_id, size, position=pos, remaining=0)
                            if actual_price == "NO_BALANCE":
                                _fallback = _resolve_or_estimate_exit_price(token_id, current_price, entry_price, slug, direction)
                                _exit_price = _resolve_exit_with_tp_check(_tp_oid, _fallback)
                                self_notify(pos, _exit_price, coin, direction, size, "Breakeven锁定(余额已清)")
                                close_position(pos, _exit_price)
                                continue
                            if ok:
                                self_notify(pos, actual_price, coin, direction, size, "Breakeven锁定")
                                close_position(pos, actual_price)
                                continue

                    # ── Step 5: 动态TP阶梯 — 调价 / 补挂丢失的TP ──
                    _need_tp_rebuild = not pos.get("tp_order_id") and _tp_confirmed_dead
                    if (_tp_active or _need_tp_rebuild) and remaining > 10:
                        _cur_tp = pos.get("tp_price", 0)
                        if remaining > 180:
                            _target_tp = round(entry_price + (1 - entry_price) * AMBUSH_TP_PHASE1_RATIO, 2)
                        elif remaining > 90:
                            _target_tp = round(entry_price + (1 - entry_price) * AMBUSH_TP_PHASE2_RATIO, 2)
                        elif remaining > 30:
                            _target_tp = round(entry_price + (1 - entry_price) * AMBUSH_TP_PHASE3_RATIO, 2)
                        else:
                            _target_tp = round(entry_price + AMBUSH_TP_PHASE4_FIXED, 2)
                        _target_tp = max(entry_price + 0.01, min(0.99, _target_tp))

                        _do_place_tp = False
                        # v14.3: TP重挂节流 — 同一仓位5s内最多重挂一次，减少TP空窗期
                        if not _need_tp_rebuild and time.time() - _last_tp_rebuild.get(attempt_key, 0) < _TP_REBUILD_MIN_INTERVAL:
                            pass  # 节流: 上次重挂不到5s，跳过阶梯调整
                        # (A) 补挂: 无活跃TP需重建（上轮重挂失败或TP已过期）
                        elif _need_tp_rebuild:
                            # 先清理可能残留在CLOB上的孤儿卖单（释放token余额）
                            _cleanup_ok = cancel_all_orders(token_id)
                            if _cleanup_ok:
                                _do_place_tp = True
                                print(f"  🔄 TP补挂: 清理完成，目标@${_target_tp:.2f} | 剩余{remaining:.0f}s")
                            else:
                                print(f"  ⚠️ TP补挂: 孤儿订单清理失败，跳过补挂，下轮重试")
                        # (B) 重挂: TP活跃且需下调（上调无意义）
                        elif _cur_tp > 0 and _target_tp < _cur_tp - 0.01:
                            try:
                                _cancel_ok = clob_client.cancel_order(_tp_oid)
                                if not _cancel_ok:
                                    _cancel_ok = clob_client.cancel_all(token_id)
                                if not _cancel_ok:
                                    print(f"  ⚠️ TP阶梯: 旧TP撤单失败，保留旧TP@${_cur_tp:.2f}，跳过重挂")
                                    raise StopIteration
                                _do_place_tp = True
                            except StopIteration:
                                pass  # 撤单失败，已打日志，保留旧TP
                            except Exception as _tp_cancel_e:
                                print(f"  ⚠️ TP撤单异常: {_tp_cancel_e}")

                        if _do_place_tp:
                            try:
                                from py_clob_client.order_builder.constants import SELL as _SELL_TP
                                from py_clob_client.clob_types import OrderType as _OT_TP
                                _new_exp = int(end_timestamp) - 15 + 60
                                if _new_exp > int(time.time()) + 30:
                                    _tp_size = min(size, int(size * 10000) / 10000)
                                    _new_tp_result = clob_client.place_order(
                                        token_id, _SELL_TP, _target_tp, _tp_size,
                                        order_type=_OT_TP.GTD, expiration=_new_exp)
                                    _new_tp_oid = _new_tp_result.get("order_id")
                                    # 即时成交: GTD TP下单瞬间被撮合（matched=True, order_id可能为空）
                                    if _new_tp_result.get("matched"):
                                        print(f"  💰 TP即时成交@${_target_tp:.2f} ({profit_rate*100:+.1f}%)")
                                        self_notify(pos, _target_tp, coin, direction, size, "伏击GTD止盈(即时成交)")
                                        close_position(pos, _target_tp)
                                        close_attempts.pop(attempt_key, None)
                                        continue
                                    elif _new_tp_oid:
                                        _tp_persist_vals = {"tp_order_id": _new_tp_oid, "tp_price": _target_tp}
                                    else:
                                        print(f"  ⚠️ TP{'补挂' if _need_tp_rebuild else '重挂'}失败: {_new_tp_result.get('status')}")
                                        _tp_persist_vals = {"tp_order_id": "", "tp_price": 0}
                                    # 更新内存 + 持久化到 positions.jsonl
                                    pos["tp_order_id"] = _tp_persist_vals["tp_order_id"]
                                    pos["tp_price"] = _tp_persist_vals["tp_price"]
                                    try:
                                        _tp_lk = open(POSITIONS_LOCK, "w")
                                        fcntl.flock(_tp_lk, fcntl.LOCK_EX)
                                        _tp_rows = []
                                        if os.path.exists(POSITIONS_FILE):
                                            with open(POSITIONS_FILE, "r") as _tpf:
                                                for _tpl in _tpf:
                                                    if _tpl.strip():
                                                        try:
                                                            _tpr = json.loads(_tpl)
                                                            if (isinstance(_tpr, dict)
                                                                    and _tpr.get("token_id") == token_id
                                                                    and _tpr.get("entry_time") == entry_time):
                                                                _tpr.update(_tp_persist_vals)
                                                            _tp_rows.append(_tpr)
                                                        except Exception:
                                                            pass
                                        with open(POSITIONS_FILE, "w") as _tpf:
                                            for _tpr in _tp_rows:
                                                _tpf.write(json.dumps(_tpr) + "\n")
                                        fcntl.flock(_tp_lk, fcntl.LOCK_UN)
                                        _tp_lk.close()
                                    except Exception as _tp_persist_e:
                                        print(f"  ⚠️ TP持久化失败: {_tp_persist_e}")
                                    if _new_tp_oid:
                                        _last_tp_rebuild[attempt_key] = time.time()  # v14.3: 记录重挂时间用于节流
                                        _tp_status_cache.pop(_tp_oid, None)  # v14.3: 清旧oid缓存
                                        _tp_act = "补挂成功" if _need_tp_rebuild else f"阶梯下调: ${_cur_tp:.2f}→"
                                        print(
                                            f"  📊 TP{_tp_act}${_target_tp:.2f} "
                                            f"| 剩余{remaining:.0f}s (oid={_new_tp_oid[:16]})")
                            except Exception as _tp_place_e:
                                print(f"  ⚠️ TP下单异常: {_tp_place_e}")

                    # ── Step 6: 尾盘小亏持有到期（<30%亏损 + 最后60s → 省手续费）──
                    if int(remaining) % 30 < 2:
                        _phase = "确认" if _held_seconds <= AMBUSH_CONFIRM_WINDOW else (
                            "快速" if _held_seconds <= AMBUSH_RAPID_SL_WINDOW else (
                                "尾盘" if remaining <= 60 else "中期"))
                        _tp_label = f"止盈@${pos.get('tp_price', 0):.2f}" if _tp_active else "无TP"
                        print(
                            f"  💎 伏击持有({_phase}): {profit_rate*100:+.1f}% | "
                            f"方向{'✅' if direction_correct else '❌'} | "
                            f"{_tp_label} | 剩余{remaining:.0f}s")
                    continue

                profit_threshold = compute_p0_profit_threshold(remaining, P0_BASE_PROFIT, P0_HYPERBOLIC_K, entry_price)

                # v12.8: 持续更新 high_water_mark（无论是否达标都要追踪）
                prev_hwm = high_water_mark.get(attempt_key, 0)
                if profit_rate > prev_hwm:
                    high_water_mark[attempt_key] = profit_rate

                # v12.8: Trailing TP 检测 — 利润曾达标后从峰值回撤触发
                _trailing_triggered = False
                if TRAILING_TP_ENABLED and trailing_tp_active.get(attempt_key) and remaining > 30:
                    hwm = high_water_mark.get(attempt_key, 0)
                    if hwm > 0:
                        atr_val_tp = pos.get("atr_val") or get_atr_from_binance(coin)
                        _, _, diff_atr_tp = calc_realtime_ev(direction, crypto_price, ptb_price, atr_val_tp, entry_price)
                        max_drawdown = compute_trailing_drawdown(hwm, remaining, direction_correct, diff_atr_tp)
                        trailing_floor = hwm * (1.0 - max_drawdown)
                        if profit_rate <= trailing_floor:
                            print(
                                f"  [Trailing TP] 触发! 峰值{hwm*100:.1f}% → 当前{profit_rate*100:.1f}% "
                                f"| 回撤{(1-profit_rate/hwm)*100:.0f}%>{max_drawdown*100:.0f}% "
                                f"| floor={trailing_floor*100:.1f}% | 剩余{remaining:.0f}s"
                            )
                            _trailing_triggered = True
                        elif int(remaining) % 10 < 2:
                            print(
                                f"  [Trailing TP] 追踪中: 峰值{hwm*100:.1f}% 当前{profit_rate*100:.1f}% "
                                f"| 回撤容忍{max_drawdown*100:.0f}% floor={trailing_floor*100:.1f}% | 剩余{remaining:.0f}s"
                            )

                # 回调检测：利润跌回阈值以下 + 之前是 FIRST_TOUCH → 标记 PULLED_BACK
                if profit_rate < profit_threshold and tp_state.get(attempt_key) == "FIRST_TOUCH":
                    tp_state[attempt_key] = "PULLED_BACK"
                    print(f"  [P0] 回调检测: 利润{profit_rate*100:.1f}%跌破阈值{profit_threshold*100:.1f}%，标记PULLED_BACK | 剩余{remaining:.0f}s")

                # 止盈条件：P0阈值达标 OR Trailing TP 触发
                _should_attempt_tp = (profit_rate >= profit_threshold and remaining > 30) or _trailing_triggered

                if _should_attempt_tp:
                    # v12.8: 首次达到P0阈值时激活 trailing TP
                    if profit_rate >= profit_threshold and not trailing_tp_active.get(attempt_key):
                        trailing_tp_active[attempt_key] = True
                        high_water_mark[attempt_key] = max(high_water_mark.get(attempt_key, 0), profit_rate)
                        print(f"  [Trailing TP] 激活! 利润{profit_rate*100:.1f}%达标 | HWM={high_water_mark[attempt_key]*100:.1f}%")

                    # 连续止盈失败3次+方向正确 → 放弃止盈，等$1结算（orderbook无买方）
                    if tp_fail_count.get(attempt_key, 0) >= 3 and direction_correct and not _trailing_triggered:
                        print(f"  💎 止盈连续失败{tp_fail_count[attempt_key]}次，方向正确等结算 | {ev_label_global} | 剩余{remaining:.0f}s")
                        continue

                    # 判断是否应该跳过本次止盈（首次达标+强信号→等结算，但 trailing 触发不跳）
                    cur_tp_state = tp_state.get(attempt_key)
                    should_skip_tp = False

                    if not _trailing_triggered:
                        if cur_tp_state is None:
                            atr_val_tp = pos.get("atr_val") or get_atr_from_binance(coin)
                            _, _, diff_atr_tp = calc_realtime_ev(direction, crypto_price, ptb_price, atr_val_tp, entry_price)
                            if direction_correct and diff_atr_tp and diff_atr_tp >= 3.0 and remaining > 90:
                                tp_state[attempt_key] = "FIRST_TOUCH"
                                atr_str_tp = f"{diff_atr_tp:.1f}ATR" if diff_atr_tp else ""
                                print(
                                    f"  [P0] 首次达标跳过(等trailing): {profit_rate*100:.1f}% >= {profit_threshold*100:.1f}% "
                                    f"| 方向✅ {atr_str_tp}≥3.0 | trailing已激活 | 剩余{remaining:.0f}s"
                                )
                                should_skip_tp = True
                        elif cur_tp_state == "FIRST_TOUCH":
                            print(f"  [P0] 持续持有(trailing保护): 利润{profit_rate*100:.1f}% | 剩余{remaining:.0f}s")
                            should_skip_tp = True

                    # Trailing触发 或 PULLED_BACK 或 无跳过条件 → 执行止盈
                    if not should_skip_tp:
                        if _trailing_triggered:
                            hwm_val = high_water_mark.get(attempt_key, 0)
                            tp_label = f"Trailing止盈(峰值{hwm_val*100:.0f}%)"
                        elif cur_tp_state == "PULLED_BACK":
                            tp_label = "回调后止盈"
                        else:
                            tp_label = "P0 take-profit"

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
                                success, actual = sell_and_confirm(token_id, size, exec_price, timeout_sec=4, position=pos)
                                if success:
                                    sold = True
                                    sold_price = actual or exec_price
                                    self_notify(pos, sold_price, coin, direction, size, tp_label)
                                    close_position(pos, sold_price)
                                    close_attempts.pop(attempt_key, None)
                                    stop_loss_attempts.pop(attempt_key, None)
                                    tp_state.pop(attempt_key, None)
                                    dip_bought.pop(attempt_key, None)
                                    tp_fail_count.pop(attempt_key, None)
                                    direction_wrong_streak.pop(attempt_key, None)
                                    direction_history.pop(attempt_key, None)
                                    continue
                                elif actual == "NO_BALANCE":
                                    print(f"  ⚠️ 余额为零，标记持仓关闭等结算")
                                    _exit_price = _resolve_or_estimate_exit_price(token_id, current_price, entry_price, slug, direction)
                                    self_notify(pos, _exit_price, coin, direction, size, "P0止盈(余额已清)")
                                    close_position(pos, _exit_price)
                                    close_attempts.pop(attempt_key, None)
                                    stop_loss_attempts.pop(attempt_key, None)
                                    tp_state.pop(attempt_key, None)
                                    dip_bought.pop(attempt_key, None)
                                    tp_fail_count.pop(attempt_key, None)
                                    direction_wrong_streak.pop(attempt_key, None)
                                    direction_history.pop(attempt_key, None)
                                    continue
                                else:
                                    # 止盈卖出失败，累计失败次数
                                    tp_fail_count[attempt_key] = tp_fail_count.get(attempt_key, 0) + 1
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
                # ═══ ATR 计算（EV-Gate 和旧逻辑都需要）═══
                _, _, diff_atr = calc_realtime_ev(direction, crypto_price, ptb_price, atr_val, entry_price)
                atr_str = f"{diff_atr:.1f}ATR" if diff_atr else "N/A"

                # 记录入场ATR（首次见到）
                if attempt_key not in entry_atr and diff_atr is not None:
                    entry_atr[attempt_key] = diff_atr
                # 更新ATR历史（速度检测用）
                if diff_atr is not None:
                    if attempt_key not in atr_history:
                        atr_history[attempt_key] = deque(maxlen=5)
                    atr_history[attempt_key].append(diff_atr)
                entry_atr_val = entry_atr.get(attempt_key)

                # ═══ EV-Gate 止损（v11）═══
                if ENABLE_EV_GATE:
                    # --- BN 预警加速：收集 BN 价格快照 ---
                    bn_snapshot = crypto_debug.get("binance") if isinstance(crypto_debug, dict) else None
                    bn_price_now = _safe_float(bn_snapshot.get("price")) if isinstance(bn_snapshot, dict) else None
                    if bn_price_now and bn_price_now > 0:
                        if attempt_key not in bn_price_history:
                            bn_price_history[attempt_key] = deque(maxlen=30)
                        bn_price_history[attempt_key].append((time.time(), bn_price_now))

                    bn_ew = calc_bn_early_warning(
                        direction, ptb_price, atr_val,
                        bn_price_history.get(attempt_key),
                    )

                    # --- EV Gate 计算 ---
                    _ev_bid_net = calc_net_exit_value(token_id, size)
                    ev_gate = calc_ev_comparison(
                        direction, crypto_price, ptb_price, atr_val,
                        remaining, entry_price, _ev_bid_net,
                    )
                    _ev_p_win = ev_gate["p_win"]

                    # BN 预警折扣
                    _bn_ew_label = ""
                    if bn_ew["active"]:
                        _ev_p_win_raw = _ev_p_win
                        _ev_p_win = max(EV_P_WIN_FLOOR, _ev_p_win * bn_ew["discount"])
                        ev_gate["p_win"] = _ev_p_win
                        ev_gate["ev_hold"] = round(_ev_p_win, 4)
                        ev_gate["ev_edge"] = round(_ev_p_win - ev_gate["ev_sell"], 4)
                        ev_gate["should_exit"] = ev_gate["ev_sell"] > _ev_p_win
                        _bn_ew_label = f" | ⚡BN预警:{bn_ew['velocity_atr']:.2f}ATR/s→P×{bn_ew['discount']}"

                    _ev_hold = ev_gate["ev_hold"]
                    _ev_sell = ev_gate["ev_sell"]
                    _ev_edge = ev_gate["ev_edge"]
                    _ev_should_exit = ev_gate["should_exit"]
                    print(
                        f"  [EV] P(win)={_ev_p_win:.1%} | hold=${_ev_hold:.3f} sell=${_ev_sell:.3f} "
                        f"| edge={_ev_edge:+.3f} | {'EXIT' if _ev_should_exit else 'HOLD'} | {atr_str}{_bn_ew_label}"
                    )

                    # --- 电路断路器（绕过 EV Gate）---
                    _cb_triggered = False
                    _cb_reason = None

                    # CB1: oracle 数据缺失
                    if crypto_price is None and remaining < EV_CIRCUIT_BREAKER_BLIND_SECS and profit_rate < -0.30:
                        _cb_triggered = True
                        _cb_reason = f"CB1:oracle缺失+跌{profit_rate*100:+.1f}%"

                    # CB2: token 极端跌幅
                    if not _cb_triggered and profit_rate <= -EV_CIRCUIT_BREAKER_LOSS and remaining < 90:
                        _cb_triggered = True
                        _cb_reason = f"CB2:token跌{profit_rate*100:+.1f}%≤-{EV_CIRCUIT_BREAKER_LOSS*100:.0f}%"

                    # CB3: 结算临近 + tail oracle 确认方向错误
                    if not _cb_triggered and remaining <= 15 and tail_info and tail_info.get("state") == "wrong_confirmed":
                        _cb_triggered = True
                        _cb_reason = "CB3:结算临近+Oracle确认方向错误"

                    if _cb_triggered:
                        print(f"  🚨 断路器触发: {_cb_reason} | 剩余{remaining:.0f}s")
                        attempted_stop_loss = True
                        _arm_close_intent(pos, _cb_reason)
                        close_intents[attempt_key] = {"reason": _cb_reason}
                        cancel_all_orders(token_id)
                        ok, actual_price = market_sell_immediate(token_id, size, position=pos, skip_cancel=True, remaining=0)
                        if ok:
                            sold = True
                            sold_price = actual_price
                            self_notify(pos, sold_price, coin, direction, size, _cb_reason)
                        elif actual_price == "NO_BALANCE":
                            _exit_price = _resolve_or_estimate_exit_price(token_id, current_price, entry_price, slug, direction)
                            self_notify(pos, _exit_price, coin, direction, size, f"{_cb_reason}(余额已清)")
                            _clear_close_intent(pos)
                            close_position(pos, _exit_price)
                            close_attempts.pop(attempt_key, None)
                            close_intents.pop(attempt_key, None)
                            continue
                        if sold:
                            _clear_close_intent(pos)
                            close_position(pos, sold_price)
                            close_attempts.pop(attempt_key, None)
                            stop_loss_attempts.pop(attempt_key, None)
                            dip_bought.pop(attempt_key, None)
                            direction_wrong_streak.pop(attempt_key, None)
                            direction_history.pop(attempt_key, None)
                            close_intents.pop(attempt_key, None)
                            ev_exit_confirm.pop(attempt_key, None)
                            bn_price_history.pop(attempt_key, None)
                            continue
                        stop_loss_attempt_recorded = True
                        stop_loss_attempts[attempt_key] = stop_loss_attempts.get(attempt_key, 0) + 1
                        continue

                    # --- ATR≥2.0 抄底（保留）---
                    if (current_price is not None and entry_price > 0
                            and profit_rate <= -PRICE_DROP_TRIGGER
                            and diff_atr is not None and diff_atr >= ATR_SAFE_THRESHOLD and direction_correct
                            and remaining > DIP_BUY_MIN_REMAINING and not dip_bought.get(attempt_key)):
                        original_size = pos.get("original_size") or size
                        print(f"  🟢 抄底区: 跌{profit_rate*100:+.1f}% | {atr_str}≥{ATR_SAFE_THRESHOLD} | 方向✅ | 剩余{remaining:.0f}s")
                        dip_ok, bought_size, buy_price = execute_dip_buy(token_id, original_size, coin, slug, pos)
                        if dip_ok:
                            dip_bought[attempt_key] = True
                            new_size = _safe_float(pos.get("token_balance")) or round(size + bought_size, 6)
                            # 均价计算：用当前持有份额的成本 + 抄底成本
                            # size 可能被 FAK 部分成交改小过，要用实际持有量而非原始总量
                            _current_holding_cost = round(entry_price * size, 6)
                            _dip_cost = round(buy_price * bought_size, 6)
                            total_cost = round(_current_holding_cost + _dip_cost, 6)
                            new_avg_price = total_cost / new_size if new_size > 0 else entry_price
                            # 健全性检查：均价不应超过1.0（二元市场token价格范围0-1）
                            if new_avg_price > 1.0 or new_avg_price < 0.01:
                                print(f"    ⚠️ 均价异常 ${new_avg_price:.3f}，回退用原入场价")
                                new_avg_price = entry_price
                            pos["original_size"] = pos.get("original_size") or size
                            pos["original_entry_price"] = pos.get("original_entry_price") or entry_price
                            pos["dip_buy_price"] = buy_price
                            pos["dip_buy_size"] = bought_size
                            pos["entry_cost"] = total_cost
                            pos["entry_price"] = new_avg_price
                            update_position(pos, new_size=new_size)
                            pos["size"] = new_size
                            size = new_size
                            print(f"    📊 仓位更新: {size-bought_size}→{size}份 | 均价${entry_price:.2f}→${new_avg_price:.2f}")
                            msg = (
                                f"🟢 <b>ATR抄底加仓</b>\n\n"
                                f"币种: {coin} | 方向: {direction}\n"
                                f"ATR偏离: {atr_str}\n"
                                f"加仓: {bought_size}份 × ${buy_price:.2f}\n"
                                f"总仓: {size}份 | 均价${new_avg_price:.2f}\n"
                                f"剩余: {remaining:.0f}s"
                            )
                            send_telegram(msg)
                        else:
                            print(f"    ⚠️ 抄底未成交，方向安全继续持有 | {atr_str} | 剩余{remaining:.0f}s")
                        continue

                    # --- v12.7: 狙击动态保护 — 基于ATR偏离衰减而非固定时间 ---
                    # 旧方案：固定N秒保护期 → 时间到了不管ATR多少都放行 → 噪音止损
                    # 新方案：只要当前ATR偏离 > 入场ATR的30%，就继续持有（方向没反转）
                    #         ATR偏离跌破30% 或 token跌>15% 才允许EV止损
                    _is_sniper = pos.get("sniper_thread", False)
                    _entry_age = 999.0
                    if _is_sniper and entry_time:
                        try:
                            _entry_dt = datetime.fromisoformat(entry_time)
                            if _entry_dt.tzinfo is None:
                                _entry_dt = _entry_dt.replace(tzinfo=timezone.utc)
                            _entry_age = (now - _entry_dt).total_seconds()
                        except (ValueError, TypeError):
                            pass

                    if _is_sniper and _ev_should_exit:
                        _sniper_entry_atr = pos.get("diff_in_atr", 0)
                        _sniper_atr_floor = float(os.environ.get("SNIPER_ATR_HOLD_RATIO", "0.30"))
                        _sniper_loss_override = float(os.environ.get("SNIPER_LOSS_OVERRIDE", "0.15"))
                        # 当前ATR偏离（从EV-Gate获取，或用0）
                        _current_atr_dev = ev_gate.get("atr_deviation", 0)
                        # 如果 ev_gate 没有 atr_deviation，从日志里的 atr_str 解析
                        if _current_atr_dev == 0:
                            try:
                                _current_atr_dev = float(atr_str.replace("ATR", ""))
                            except (ValueError, AttributeError):
                                _current_atr_dev = 0

                        _token_drop = (current_price - entry_price) / entry_price if entry_price and entry_price > 0 and current_price else 0
                        _atr_still_valid = (_sniper_entry_atr > 0 and
                                           _current_atr_dev >= _sniper_entry_atr * _sniper_atr_floor)
                        _loss_small = _token_drop > -_sniper_loss_override  # drop < 15%

                        # 固定保护期（兜底，防止ATR数据缺失时完全无保护）
                        _in_grace = _entry_age < SNIPER_GRACE_SECONDS

                        if (_atr_still_valid and _loss_small) or _in_grace:
                            _reason = ""
                            if _in_grace:
                                _reason = f"保护期{_entry_age:.1f}s/{SNIPER_GRACE_SECONDS:.0f}s"
                            else:
                                _reason = f"ATR持有({_current_atr_dev:.2f}≥{_sniper_entry_atr*_sniper_atr_floor:.2f})"
                            print(
                                f"  🛡️ 狙击保护: {_reason} | "
                                f"ATR={_current_atr_dev:.2f}/{_sniper_entry_atr:.2f} "
                                f"跌幅={_token_drop:+.1%} | edge={_ev_edge:+.3f}")
                            ev_exit_confirm.pop(attempt_key, None)
                            continue

                    # --- v12.9.2: CL-BN skew 过大时暂停 EV 止损 ---
                    # CL 滞后 BN > 1ATR 时，CL 的方向判断不可信，EV 的 P(win) 被严重低估
                    # 直接从 crypto_debug 快照读取（不依赖 oracle_watch 开关）
                    if _ev_should_exit and isinstance(crypto_debug, dict) and atr_val and atr_val > 0:
                        _cl_snap = crypto_debug.get("chainlink")
                        _bn_snap = crypto_debug.get("binance")
                        if isinstance(_cl_snap, dict) and isinstance(_bn_snap, dict):
                            _cl_p = _cl_snap.get("price")
                            _bn_p = _bn_snap.get("price")
                            if _cl_p and _bn_p:
                                _cl_bn_skew_atr = abs(_cl_p - _bn_p) / atr_val
                                _EV_SKEW_BLOCK = float(os.environ.get("EV_SKEW_BLOCK_ATR", "1.0"))
                                if _cl_bn_skew_atr >= _EV_SKEW_BLOCK:
                                    print(
                                        f"  🛡️ CL-BN偏差暂停EV止损: |skew|={_cl_bn_skew_atr:.2f}ATR"
                                        f"≥{_EV_SKEW_BLOCK}ATR | CL=${_cl_p:.2f} BN=${_bn_p:.2f} 差${abs(_cl_p-_bn_p):.0f}")
                                    ev_exit_confirm.pop(attempt_key, None)
                                    continue

                    # --- P1: 方向正确+CL滞后时，用BN重算P(win)二次确认 ---
                    if _ev_should_exit and direction_correct and isinstance(crypto_debug, dict) and atr_val and atr_val > 0:
                        _bn_snap_p1 = crypto_debug.get("binance")
                        if isinstance(_bn_snap_p1, dict) and _bn_snap_p1.get("price") and ptb_price:
                            _bn_price_p1 = _bn_snap_p1["price"]
                            _bn_gap = _bn_price_p1 - ptb_price
                            # BN说方向正确（gap和持仓方向一致）→ 不信CL的EV止损
                            _bn_dir_correct = (_bn_gap > 0 and direction == "UP") or (_bn_gap < 0 and direction == "DOWN")
                            if _bn_dir_correct:
                                _bn_gap_atr = abs(_bn_gap) / atr_val
                                if _bn_gap_atr >= 0.3:  # BN显示有实质性gap
                                    print(
                                        f"  🛡️ BN方向确认暂停EV: 方向✅ BN gap={_bn_gap:+.0f}({_bn_gap_atr:.2f}ATR) "
                                        f"| CL说卖但BN说方向对，跳过")
                                    ev_exit_confirm.pop(attempt_key, None)
                                    continue

                    # --- EV-Gated 退出 ---
                    if _ev_should_exit:
                        ev_exit_confirm[attempt_key] = ev_exit_confirm.get(attempt_key, 0) + 1
                        _ev_confirm_count = ev_exit_confirm[attempt_key]
                        if _ev_confirm_count < EV_EXIT_CONFIRMATIONS:
                            print(f"  ⏸️ EV退出待确认: {_ev_confirm_count}/{EV_EXIT_CONFIRMATIONS} | {ev_gate['reason']}")
                            continue
                        # 确认达标，执行止损
                        _ev_reason = f"EV止损(P_win={_ev_p_win:.0%},{atr_str})"
                        print(f"  🚨 {_ev_reason} | edge={_ev_edge:+.3f} | {_ev_confirm_count}/{EV_EXIT_CONFIRMATIONS}轮确认")
                        attempted_stop_loss = True
                        _arm_close_intent(pos, _ev_reason)
                        close_intents[attempt_key] = {"reason": _ev_reason}
                        cancel_all_orders(token_id)
                        ok, actual_price = market_sell_immediate(token_id, size, position=pos, skip_cancel=True)
                        if ok:
                            sold = True
                            sold_price = actual_price
                            self_notify(pos, sold_price, coin, direction, size, _ev_reason)
                        elif actual_price == "NO_BALANCE":
                            _exit_price = _resolve_or_estimate_exit_price(token_id, current_price, entry_price, slug, direction)
                            self_notify(pos, _exit_price, coin, direction, size, f"{_ev_reason}(余额已清)")
                            _clear_close_intent(pos)
                            close_position(pos, _exit_price)
                            try_reverse_entry(pos, remaining, atr_val=atr_val, ptb_price=ptb_price)
                            close_attempts.pop(attempt_key, None)
                            close_intents.pop(attempt_key, None)
                            ev_exit_confirm.pop(attempt_key, None)
                            bn_price_history.pop(attempt_key, None)
                            continue
                        if sold:
                            _clear_close_intent(pos)
                            close_position(pos, sold_price)
                            # v12.9.8: EV止损后反向入场
                            try_reverse_entry(pos, remaining, atr_val=atr_val, ptb_price=ptb_price)
                            close_attempts.pop(attempt_key, None)
                            stop_loss_attempts.pop(attempt_key, None)
                            dip_bought.pop(attempt_key, None)
                            direction_wrong_streak.pop(attempt_key, None)
                            direction_history.pop(attempt_key, None)
                            close_intents.pop(attempt_key, None)
                            ev_exit_confirm.pop(attempt_key, None)
                            bn_price_history.pop(attempt_key, None)
                            continue
                        stop_loss_attempt_recorded = True
                        stop_loss_attempts[attempt_key] = stop_loss_attempts.get(attempt_key, 0) + 1
                        continue
                    else:
                        # EV says hold — reset confirmation counter
                        ev_exit_confirm.pop(attempt_key, None)
                        if direction_correct:
                            print(f"  💎 EV持有: 方向✅ P(win)={_ev_p_win:.1%} | {atr_str} | 剩余{remaining:.0f}s")
                        elif _ev_edge > 0.10:
                            print(f"  💎 EV持有: 方向❌但edge=+{_ev_edge:.3f} (bid太低不值得卖) | {atr_str} | 剩余{remaining:.0f}s")

                else:
                    # ═══ 旧止损逻辑（ENABLE_EV_GATE=0）═══
                    # --- 0A. ATR衰减止损 ---
                    atr_decay_armed = should_arm_atr_decay_exit(diff_atr, entry_atr_val, profit_rate) if entry_atr_val and entry_atr_val > 0 else False
                    decay_pct = (1 - diff_atr / entry_atr_val) * 100 if atr_decay_armed and entry_atr_val and entry_atr_val > 0 else None

                    # --- 0B. ATR加速下降止损 ---
                    atr_accel_armed = False
                    atr_accel_speed = None
                    if diff_atr is not None and attempt_key in atr_history:
                        hist = atr_history[attempt_key]
                        if len(hist) >= 3 and profit_rate < -0.10:
                            recent = list(hist)[-3:]
                            if recent[0] > recent[1] > recent[2] and recent[2] < ATR_DECAY_EXIT_THRESHOLD:
                                atr_accel_armed = True
                                atr_accel_speed = recent[0] - recent[2]

                    # --- 方向降级 ---
                    if should_direction_downgrade(direction_correct, remaining, diff_atr, profit_rate):
                        print(f"  ⚠️ 方向降级: 方向✅但ATR={diff_atr:.2f}<{ATR_DOWNGRADE_THRESHOLD}(逼近strike) + 跌{profit_rate*100:+.1f}% → 视为方向❌")
                        direction_correct = False

                    # --- PTB Proximity Buffer ---
                    raw_direction_correct = direction_signal_correct if tail_info and tail_info["active"] else true_direction_correct
                    in_proximity = False
                    if diff_atr is not None and crypto_price and ptb_price and not (tail_info and tail_info["active"]):
                        prox_info = evaluate_proximity_guard(
                            direction_correct=direction_correct,
                            true_direction_correct=true_direction_correct,
                            diff_atr=diff_atr,
                            remaining=remaining,
                            profit_rate=profit_rate,
                            wrong_streak=direction_wrong_streak.get(attempt_key, 0),
                            direction_history=direction_history.get(attempt_key),
                        )
                        in_proximity = prox_info["in_proximity"]
                        if prox_info["release_by_extreme_stop"]:
                            print(
                                f"  🚨 Proximity极端止损: {diff_atr:.2f}ATR<"
                                f"{prox_info['prox_threshold']:.2f} 但跌{profit_rate*100:+.1f}%>"
                                f"{-prox_info['extreme_stop']*100:.0f}% → 不冻结"
                            )
                            in_proximity = False
                        elif prox_info["released"]:
                            print(
                                f"  🔶→🚨 Proximity保护解除: streak={prox_info['projected_streak']}≥"
                                f"{prox_info['streak_threshold']} or ratio={prox_info['wrong_ratio']:.0%}≥75% "
                                f"| {diff_atr:.2f}ATR | 剩余{remaining:.0f}s"
                            )
                            in_proximity = False
                        elif prox_info["freeze"]:
                            print(
                                f"  🔶 PTB临近区: {diff_atr:.2f}ATR<{prox_info['prox_threshold']:.2f} "
                                f"| 方向冻结✅ | streak={prox_info['projected_streak']} "
                                f"ratio={prox_info['wrong_ratio']:.0%} | 剩余{remaining:.0f}s"
                            )
                            direction_correct = True

                    # --- 更新方向错误计数 ---
                    if raw_direction_correct is not None:
                        if attempt_key not in direction_history:
                            direction_history[attempt_key] = deque(maxlen=8)
                        direction_history[attempt_key].append(raw_direction_correct)
                        cur_streak = direction_wrong_streak.get(attempt_key, 0)
                        if raw_direction_correct is False:
                            direction_wrong_streak[attempt_key] = cur_streak + 1
                        else:
                            direction_wrong_streak[attempt_key] = max(0, cur_streak - 1)

                    if atr_decay_armed and true_direction_correct is False:
                        atr_decay_confirm_streak[attempt_key] = atr_decay_confirm_streak.get(attempt_key, 0) + 1
                    else:
                        atr_decay_confirm_streak.pop(attempt_key, None)
                    decay_confirm_count = atr_decay_confirm_streak.get(attempt_key, 0)

                    if atr_decay_armed and true_direction_correct is False and direction_correct is True:
                        print(
                            f"  🔶 ATR衰减冻结: {diff_atr:.2f}ATR/{entry_atr_val:.1f}ATR | "
                            f"streak={decay_confirm_count}/{ATR_DECAY_CONFIRMATIONS} | 剩余{remaining:.0f}s"
                        )
                    elif atr_decay_armed and true_direction_correct is False and decay_confirm_count < ATR_DECAY_CONFIRMATIONS:
                        print(
                            f"  ⏸️ ATR衰减待确认: {diff_atr:.2f}ATR/{entry_atr_val:.1f}ATR | "
                            f"{decay_confirm_count}/{ATR_DECAY_CONFIRMATIONS}轮 | 剩余{remaining:.0f}s"
                        )
                    elif atr_decay_armed and true_direction_correct is False:
                        print(f"  🚨 ATR衰减止损: {diff_atr:.2f}ATR (入场{entry_atr_val:.1f}ATR, 衰减{decay_pct:.0f}%) + 跌{profit_rate*100:+.1f}% | 剩余{remaining:.0f}s")
                        attempted_stop_loss = True
                        _arm_close_intent(pos, f"ATR衰减止损({diff_atr:.1f}/{entry_atr_val:.1f})")
                        close_intents[attempt_key] = {"reason": f"ATR衰减止损({diff_atr:.1f}/{entry_atr_val:.1f})"}
                        cancel_all_orders(token_id)
                        best_bid_sl = get_best_bid_raw(token_id)
                        if best_bid_sl and best_bid_sl >= 0.05:
                            ok, actual_price = market_sell_immediate(token_id, size, price=best_bid_sl, position=pos, skip_cancel=True)
                        else:
                            ok, actual_price = market_sell_immediate(token_id, size, position=pos, skip_cancel=True)
                        if ok:
                            sold = True
                            sold_price = actual_price
                            self_notify(pos, sold_price, coin, direction, size, f"ATR衰减止损({diff_atr:.1f}/{entry_atr_val:.1f})")
                        elif actual_price == "NO_BALANCE":
                            _exit_price = _resolve_or_estimate_exit_price(token_id, current_price, entry_price, slug, direction)
                            self_notify(pos, _exit_price, coin, direction, size, "ATR衰减(余额已清)")
                            close_position(pos, _exit_price)
                            close_attempts.pop(attempt_key, None)
                            stop_loss_attempts.pop(attempt_key, None)
                            dip_bought.pop(attempt_key, None)
                            direction_wrong_streak.pop(attempt_key, None)
                            direction_history.pop(attempt_key, None)
                            continue
                        if sold:
                            _clear_close_intent(pos)
                            close_position(pos, sold_price)
                            close_attempts.pop(attempt_key, None)
                            stop_loss_attempts.pop(attempt_key, None)
                            dip_bought.pop(attempt_key, None)
                            direction_wrong_streak.pop(attempt_key, None)
                            direction_history.pop(attempt_key, None)
                            close_intents.pop(attempt_key, None)
                            continue
                        stop_loss_attempt_recorded = True
                        stop_loss_attempts[attempt_key] = stop_loss_attempts.get(attempt_key, 0) + 1
                        continue

                    if atr_accel_armed and true_direction_correct is False and direction_correct is True:
                        print(f"  🔶 ATR加速下降冻结: Δ{atr_accel_speed:.2f} | proximity保护中 | 剩余{remaining:.0f}s")
                    elif atr_accel_armed and true_direction_correct is False:
                        recent = list(atr_history[attempt_key])[-3:]
                        print(f"  🚨 ATR加速下降: {recent[0]:.2f}→{recent[1]:.2f}→{recent[2]:.2f} (Δ{atr_accel_speed:.2f}) + 跌{profit_rate*100:+.1f}% | 剩余{remaining:.0f}s")
                        attempted_stop_loss = True
                        _arm_close_intent(pos, f"ATR加速下降({atr_accel_speed:.1f})")
                        close_intents[attempt_key] = {"reason": f"ATR加速下降({atr_accel_speed:.1f})"}
                        cancel_all_orders(token_id)
                        best_bid_sl = get_best_bid_raw(token_id)
                        if best_bid_sl and best_bid_sl >= 0.05:
                            ok, actual_price = market_sell_immediate(token_id, size, price=best_bid_sl, position=pos, skip_cancel=True)
                        else:
                            ok, actual_price = market_sell_immediate(token_id, size, position=pos, skip_cancel=True)
                        if ok:
                            sold = True
                            sold_price = actual_price
                            self_notify(pos, sold_price, coin, direction, size, f"ATR加速下降({atr_accel_speed:.1f})")
                        elif actual_price == "NO_BALANCE":
                            _exit_price = _resolve_or_estimate_exit_price(token_id, current_price, entry_price, slug, direction)
                            self_notify(pos, _exit_price, coin, direction, size, "ATR加速(余额已清)")
                            _clear_close_intent(pos)
                            close_position(pos, _exit_price)
                            close_attempts.pop(attempt_key, None)
                            close_intents.pop(attempt_key, None)
                            continue
                        if sold:
                            _clear_close_intent(pos)
                            close_position(pos, sold_price)
                            close_attempts.pop(attempt_key, None)
                            stop_loss_attempts.pop(attempt_key, None)
                            dip_bought.pop(attempt_key, None)
                            direction_wrong_streak.pop(attempt_key, None)
                            direction_history.pop(attempt_key, None)
                            close_intents.pop(attempt_key, None)
                            continue
                        stop_loss_attempt_recorded = True
                        stop_loss_attempts[attempt_key] = stop_loss_attempts.get(attempt_key, 0) + 1
                        continue

                    # --- 1. -25% 硬止损线（方向错误/未知时触发，方向正确交给ATR矩阵）---
                    # [P0] true_direction_correct is not True 门控：BTC真在正确侧时不触发硬止损
                    if (current_price is not None and entry_price > 0
                            and profit_rate <= -PRICE_DROP_HARD_STOP
                            and direction_correct is not True
                            and true_direction_correct is not True):
                        print(f"  🚨 硬止损: {profit_rate*100:+.1f}%超过-{PRICE_DROP_HARD_STOP*100:.0f}% | {atr_str} | 剩余{remaining:.0f}s")
                        attempted_stop_loss = True
                        _arm_close_intent(pos, "硬止损(-25%)")
                        close_intents[attempt_key] = {"reason": "硬止损(-25%)"}
                        cancel_all_orders(token_id)
                        ok, actual_price = market_sell_immediate(token_id, size, position=pos, skip_cancel=True)
                        if ok:
                            sold = True
                            sold_price = actual_price
                            self_notify(pos, sold_price, coin, direction, size, "硬止损(-25%)")
                        elif actual_price == "NO_BALANCE":
                            print(f"  ⚠️ 余额为零，标记持仓关闭等结算")
                            _exit_price = _resolve_or_estimate_exit_price(token_id, current_price, entry_price, slug, direction)
                            self_notify(pos, _exit_price, coin, direction, size, "硬止损(余额已清)")
                            _clear_close_intent(pos)
                            close_position(pos, _exit_price)
                            close_attempts.pop(attempt_key, None)
                            stop_loss_attempts.pop(attempt_key, None)
                            dip_bought.pop(attempt_key, None)
                            direction_wrong_streak.pop(attempt_key, None)
                            direction_history.pop(attempt_key, None)
                            close_intents.pop(attempt_key, None)
                            continue
                        if sold:
                            _clear_close_intent(pos)
                            close_position(pos, sold_price)
                            close_attempts.pop(attempt_key, None)
                            stop_loss_attempts.pop(attempt_key, None)
                            dip_bought.pop(attempt_key, None)
                            direction_wrong_streak.pop(attempt_key, None)
                            direction_history.pop(attempt_key, None)
                            close_intents.pop(attempt_key, None)
                            continue
                        stop_loss_attempt_recorded = True
                        stop_loss_attempts[attempt_key] = stop_loss_attempts.get(attempt_key, 0) + 1
                        continue

                    # --- 2. 方向翻转紧急退出（True→False 时需连续确认）---
                    # 用 raw_direction 追踪翻转（不受 proximity 冻结影响）
                    prev_dir = prev_direction_correct.get(attempt_key)
                    if raw_direction_correct is not None:
                        prev_direction_correct[attempt_key] = raw_direction_correct
                    if prev_dir is True and direction_correct is False:
                        streak = direction_wrong_streak.get(attempt_key, 0)
                        required_confirms = get_direction_flip_required_confirms(diff_atr, remaining)
                        if streak < required_confirms:
                            print(f"  ⏸️ 方向翻转待确认: {streak}/{required_confirms}轮 | {atr_str} | 剩余{remaining:.0f}s")
                            # 恢复prev为True，下轮仍能检测到翻转（否则prev被更新为False，#2永远不再触发）
                            prev_direction_correct[attempt_key] = True
                            continue
                        print(f"  🚨 方向翻转确认: 连续{streak}轮❌(≥{required_confirms}) | {atr_str} | 剩余{remaining:.0f}s → 清仓")
                        attempted_stop_loss = True
                        _arm_close_intent(pos, "方向翻转清仓")
                        close_intents[attempt_key] = {"reason": "方向翻转清仓"}
                        cancel_all_orders(token_id)
                        ok, actual_price = market_sell_immediate(token_id, size, position=pos, skip_cancel=True)
                        if ok:
                            sold = True
                            sold_price = actual_price
                            self_notify(pos, sold_price, coin, direction, size, "方向翻转清仓")
                        elif actual_price == "NO_BALANCE":
                            print(f"  ⚠️ 余额为零，标记持仓关闭等结算")
                            _exit_price = _resolve_or_estimate_exit_price(token_id, current_price, entry_price, slug, direction)
                            self_notify(pos, _exit_price, coin, direction, size, "方向翻转(余额已清)")
                            _clear_close_intent(pos)
                            close_position(pos, _exit_price)
                            close_attempts.pop(attempt_key, None)
                            stop_loss_attempts.pop(attempt_key, None)
                            dip_bought.pop(attempt_key, None)
                            direction_wrong_streak.pop(attempt_key, None)
                            direction_history.pop(attempt_key, None)
                            close_intents.pop(attempt_key, None)
                            continue
                        if sold:
                            _clear_close_intent(pos)
                            close_position(pos, sold_price)
                            close_attempts.pop(attempt_key, None)
                            stop_loss_attempts.pop(attempt_key, None)
                            dip_bought.pop(attempt_key, None)
                            direction_wrong_streak.pop(attempt_key, None)
                            direction_history.pop(attempt_key, None)
                            close_intents.pop(attempt_key, None)
                            continue
                        stop_loss_attempt_recorded = True
                        stop_loss_attempts[attempt_key] = stop_loss_attempts.get(attempt_key, 0) + 1
                        continue

                    # --- 3. Token 跌幅 ≥ 触发线 → ATR 三层决策 ---
                    if (current_price is not None and entry_price > 0
                            and profit_rate <= -PRICE_DROP_TRIGGER):
                        if diff_atr is not None and diff_atr >= ATR_SAFE_THRESHOLD and direction_correct \
                                and remaining > DIP_BUY_MIN_REMAINING and not dip_bought.get(attempt_key):
                            # 🟢 安全区: ATR ≥ 2.0 → 抄底加仓
                            original_size = pos.get("original_size") or size
                            print(f"  🟢 抄底区: 跌{profit_rate*100:+.1f}% | {atr_str}≥{ATR_SAFE_THRESHOLD} | 方向✅ | 剩余{remaining:.0f}s")
                            dip_ok, bought_size, buy_price = execute_dip_buy(token_id, original_size, coin, slug, pos)
                            if dip_ok:
                                dip_bought[attempt_key] = True
                                # 更新仓位: size增加, 记录原始size, 均价调整
                                new_size = _safe_float(pos.get("token_balance")) or round(size + bought_size, 6)
                                total_cost = round(entry_price * size + buy_price * bought_size, 6)
                                new_avg_price = total_cost / new_size if new_size > 0 else entry_price
                                pos["original_size"] = pos.get("original_size") or size
                                pos["original_entry_price"] = pos.get("original_entry_price") or entry_price
                                pos["dip_buy_price"] = buy_price
                                pos["dip_buy_size"] = bought_size
                                pos["entry_cost"] = total_cost
                                pos["entry_price"] = new_avg_price
                                update_position(pos, new_size=new_size)
                                pos["size"] = new_size
                                size = new_size
                                print(f"    📊 仓位更新: {size-bought_size}→{size}份 | 均价${entry_price:.2f}→${new_avg_price:.2f}")
                                msg = (
                                    f"🟢 <b>ATR抄底加仓</b>\n\n"
                                    f"币种: {coin} | 方向: {direction}\n"
                                    f"ATR偏离: {atr_str}\n"
                                    f"加仓: {bought_size}份 × ${buy_price:.2f}\n"
                                    f"总仓: {size}份 | 均价${new_avg_price:.2f}\n"
                                    f"剩余: {remaining:.0f}s"
                                )
                                send_telegram(msg)
                            else:
                                print(f"    ⚠️ 抄底未成交，方向安全继续持有 | {atr_str} | 剩余{remaining:.0f}s")
                            continue

                        elif diff_atr is not None and diff_atr < ATR_DANGER_THRESHOLD and (
                                not direction_correct
                                or (diff_atr < ATR_DIRECTION_CORRECT_STOP and profit_rate <= -0.20
                                    and true_direction_correct is not True)):
                            # 🔴 危险区: ATR < 1.0 且方向错误 → 立即止损
                            # 改进: ATR < 0.5 且 token跌>20% → 即使方向正确也止损（50:50赌局不值得）
                            # [P0] true_direction_correct 门控：BTC真在正确侧时不触发（赢单不是50:50）
                            dir_label = "方向❌" if not direction_correct else f"方向✅但ATR={diff_atr:.2f}<{ATR_DIRECTION_CORRECT_STOP}"
                            print(f"  🔴 ATR止损: 跌{profit_rate*100:+.1f}% | {atr_str}<{ATR_DANGER_THRESHOLD} | {dir_label} | 剩余{remaining:.0f}s")
                            attempted_stop_loss = True
                            _arm_close_intent(pos, f"ATR止损({atr_str})")
                            close_intents[attempt_key] = {"reason": f"ATR止损({atr_str})"}
                            cancel_all_orders(token_id)
                            # 用best_bid卖出（token此时还有价值，不用地板价）
                            best_bid_sl = get_best_bid_raw(token_id)
                            if not best_bid_sl or best_bid_sl < 0.05:
                                # bid极低或无买方，用地板价市价卖
                                ok, actual_price = market_sell_immediate(token_id, size, position=pos, skip_cancel=True)
                            else:
                                ok, actual_price = market_sell_immediate(token_id, size, price=best_bid_sl, position=pos, skip_cancel=True)
                            if ok:
                                sold = True
                                sold_price = actual_price
                                self_notify(pos, sold_price, coin, direction, size, f"ATR止损({atr_str})")
                            elif actual_price == "NO_BALANCE":
                                print(f"  ⚠️ 余额为零，标记持仓关闭等结算")
                                _exit_price = _resolve_or_estimate_exit_price(token_id, current_price, entry_price, slug, direction)
                                self_notify(pos, _exit_price, coin, direction, size, "ATR止损(余额已清)")
                                close_position(pos, _exit_price)
                                close_attempts.pop(attempt_key, None)
                                stop_loss_attempts.pop(attempt_key, None)
                                dip_bought.pop(attempt_key, None)
                                direction_wrong_streak.pop(attempt_key, None)
                                direction_history.pop(attempt_key, None)
                                continue
                            if sold:
                                _clear_close_intent(pos)
                                close_position(pos, sold_price)
                                close_attempts.pop(attempt_key, None)
                                stop_loss_attempts.pop(attempt_key, None)
                                dip_bought.pop(attempt_key, None)
                                direction_wrong_streak.pop(attempt_key, None)
                                direction_history.pop(attempt_key, None)
                                close_intents.pop(attempt_key, None)
                                continue
                            # 未成交，记录重试
                            stop_loss_attempt_recorded = True
                            stop_loss_attempts[attempt_key] = stop_loss_attempts.get(attempt_key, 0) + 1
                            continue

                        else:
                            # 🟡 观望区: ATR 1.0-2.0 → 不加仓也不割肉，继续监控
                            # 预缓存链上余额，万一下轮触发止损可直接用（省掉~300ms查链延迟）
                            _prefetch_balance(token_id)
                            print(f"  🟡 观望: 跌{profit_rate*100:+.1f}% | {atr_str} | 等ATR变化 | 剩余{remaining:.0f}s")
                            continue

                    # --- 4. 跌幅 < 触发线: 方向正确+信号好 → 持有 ---
                    elif direction_correct and remaining > 0:
                        # 市场EV正（token > entry）→ 持有
                        if market_ev is not None and market_ev > 0.03:
                            print(f"  💎 方向正确，持有等结算 | {ev_label_global} {atr_str} | 剩余{remaining:.0f}s")
                            continue
                        # 时间衰减ATR阈值
                        atr_hold_threshold = min(1.0 + max(0, remaining - 60) / 120, 2.0)
                        if diff_atr and diff_atr >= atr_hold_threshold:
                            print(f"  💎 方向正确，ATR强信号持有 | {ev_label_global} {atr_str}≥{atr_hold_threshold:.1f} | 剩余{remaining:.0f}s")
                            continue
                        # 信号不足 → 放行到阶段策略
                        print(f"  ⚠️ 方向正确但信号不足({ev_label_global} {atr_str})，放行到阶段策略 | 剩余{remaining:.0f}s")

                    # --- 5. 全程方向错误止损（remaining > 0，跌幅未达触发线）---
                    if should_attempt_stop_loss(direction_correct) and remaining > 30:
                        elapsed = 300 - remaining

                        # 连续确认要求：偏离越小需要越多轮确认
                        streak = direction_wrong_streak.get(attempt_key, 0)
                        if diff_atr is not None:
                            if diff_atr < 1.0:
                                required_streak = 3   # ~15秒
                            elif diff_atr < 1.5:
                                required_streak = 2   # ~10秒
                            else:
                                required_streak = 1   # 偏离大，快速止损
                        else:
                            required_streak = 2
                        if streak < required_streak:
                            if streak <= 1:
                                print(f"  ⏸️ 方向错误待确认: {streak}/{required_streak}轮 | {atr_str} | 剩余{remaining:.0f}s")
                            continue

                        # 早期容忍窗口
                        atr_val_sl = atr_val or get_atr_from_binance(coin)
                        if atr_val_sl and crypto_price and ptb_price and elapsed < 90:
                            deviation_atr = abs(crypto_price - ptb_price) / atr_val_sl
                            if elapsed < 30:
                                tolerance = 1.0
                            elif elapsed < 60:
                                tolerance = 0.7
                            else:
                                tolerance = 0.5
                            if deviation_atr < tolerance:
                                if close_attempts.get(attempt_key, 0) % 5 == 0:
                                    print(f"  ⏸️ 方向反了但偏离小({deviation_atr:.2f}ATR<{tolerance})，观望 | 持仓{elapsed:.0f}s | 剩余{remaining:.0f}s")
                                continue

                        attempts = stop_loss_attempts.get(attempt_key, 0)
                        best_bid = get_best_bid_raw(token_id)

                        # bid极低或无买方 → P1对冲
                        if not best_bid or best_bid < 0.05:
                            opposite_token = pos.get("opposite_token_id")
                            if opposite_token:
                                opposite_ask = get_best_ask(opposite_token)
                                if opposite_ask and opposite_ask < (1.00 - entry_price - 0.02):
                                    print(f"  🔄 P1对冲: bid=${best_bid or 0:.3f}<$0.05 | 买反向token @ ${opposite_ask:.3f} | 剩余{remaining:.0f}s")
                                    h_success, h_output, h_actual = buy_opposite_token(opposite_token, size, opposite_ask)
                                    if h_success:
                                        hedge_price = h_actual or opposite_ask
                                        net_settle = 1.00 - entry_price - hedge_price
                                        print(f"  ✅ 对冲成功！净利${net_settle:.3f}")
                                        self_notify(pos, entry_price + net_settle, coin, direction, size, "P1对冲止损")
                                        close_position(pos, entry_price + net_settle)
                                        close_attempts.pop(attempt_key, None)
                                        stop_loss_attempts.pop(attempt_key, None)
                                        dip_bought.pop(attempt_key, None)
                                        direction_wrong_streak.pop(attempt_key, None)
                                        direction_history.pop(attempt_key, None)
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
                                    print(f"  🔴 方向错误但无买方(bid=${best_bid or 0:.3f})且无对冲token，等过期结算 | 剩余{remaining:.0f}s")
                            attempted_stop_loss = True
                            stop_loss_attempt_recorded = True
                            stop_loss_attempts[attempt_key] = attempts + 1
                            continue

                        # 市价止损
                        print(f"  🛑 方向错误止损(市价): bid=${best_bid:.3f} | 剩余{remaining:.0f}s")
                        attempted_stop_loss = True
                        _arm_close_intent(pos, "方向错误止损")
                        close_intents[attempt_key] = {"reason": "方向错误止损"}
                        ok, actual_price = market_sell_immediate(token_id, size, price=best_bid, position=pos)
                        if ok:
                            sold = True
                            sold_price = actual_price
                            self_notify(pos, sold_price, coin, direction, size, "方向错误止损")
                        elif actual_price == "NO_BALANCE":
                            print(f"  ⚠️ 余额为零，标记持仓关闭等结算")
                            _exit_price = _resolve_or_estimate_exit_price(token_id, current_price, entry_price, slug, direction)
                            self_notify(pos, _exit_price, coin, direction, size, "止损(余额已清)")
                            close_position(pos, _exit_price)
                            close_attempts.pop(attempt_key, None)
                            stop_loss_attempts.pop(attempt_key, None)
                            dip_bought.pop(attempt_key, None)
                            direction_wrong_streak.pop(attempt_key, None)
                            direction_history.pop(attempt_key, None)
                            continue
                        elif attempts >= 3:
                            print(f"  🔴 市价止损{attempts+1}次无买方，放弃等结算 | 剩余{remaining:.0f}s")
                            stop_loss_attempt_recorded = True
                            stop_loss_attempts[attempt_key] = attempts + 1
                            continue

                        if sold:
                            _clear_close_intent(pos)
                            close_position(pos, sold_price)
                            close_attempts.pop(attempt_key, None)
                            stop_loss_attempts.pop(attempt_key, None)
                            dip_bought.pop(attempt_key, None)
                            direction_wrong_streak.pop(attempt_key, None)
                            direction_history.pop(attempt_key, None)
                            close_intents.pop(attempt_key, None)
                            continue
                        stop_loss_attempt_recorded = True
                        stop_loss_attempts[attempt_key] = attempts + 1
                        continue
                
                # ═══ 阶段2：结束前120-60秒（流动性下降期）═══
                # 方向错误已被全程止损处理，这里处理信号不足的情况
                if 60 < remaining <= 120:
                    # EV-Gate: EV edge > 0.05 → 持有比卖出更优，跳过
                    if ENABLE_EV_GATE and ev_gate and ev_gate.get("ev_edge", 0) > 0.05:
                        print(f"  💎 阶段2: EV持有(edge=+{ev_gate['ev_edge']:.3f}) | P(win)={ev_gate['p_win']:.1%} | 剩余{remaining:.0f}s")
                        continue
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
                        success, actual = sell_and_confirm(token_id, size, price, timeout_sec=4, position=pos)
                        if success:
                            sold = True
                            sold_price = actual
                            self_notify(pos, sold_price, coin, direction, size, "阶段2平仓")
                        elif actual == "NO_BALANCE":
                            print(f"  ⚠️ 余额为零，标记持仓关闭等结算")
                            _exit_price = _resolve_or_estimate_exit_price(token_id, current_price, entry_price, slug, direction)
                            self_notify(pos, _exit_price, coin, direction, size, "阶段2(余额已清)")
                            close_position(pos, _exit_price)
                            close_attempts.pop(attempt_key, None)
                            stop_loss_attempts.pop(attempt_key, None)
                            continue
                        else:
                            price2 = round(best_bid * 0.90, 2)
                            attempted_close = True
                            success2, actual2 = sell_and_confirm(token_id, size, price2, timeout_sec=4, position=pos)
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
                                # 记录部分成交的PnL到daily_pnl（mini-settlement）
                                try:
                                    from trading_state import record_partial_pnl
                                    new_daily = record_partial_pnl(slug, sold_price, sold_count, entry_price)
                                    partial_pnl = (sold_price - entry_price) * sold_count
                                    print(f"    📊 部分成交PnL: ${partial_pnl:+.2f} ({sold_count}份×${sold_price:.2f}) → 今日PnL: ${new_daily:+.2f}")
                                except Exception:
                                    pass
                                self_notify(pos, sold_price, coin, direction, sold_count, f"阶段2分批({sold_count}/{size})")
                                close_attempts.pop(attempt_key, None)
                                continue
                
                # ═══ 阶段3：结束前60-30秒（流动性枯竭期）═══
                if not sold and 30 < remaining <= 60:
                    # EV-Gate: EV edge > 0.05 → 持有比卖出更优，跳过
                    if ENABLE_EV_GATE and ev_gate and ev_gate.get("ev_edge", 0) > 0.05:
                        print(f"  💎 阶段3: EV持有(edge=+{ev_gate['ev_edge']:.3f}) | P(win)={ev_gate['p_win']:.1%} | 剩余{remaining:.0f}s")
                    # 方向正确 → 跳过阶段3，等阶段4持有到结算拿$1
                    elif direction_correct:
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
                                    ok, actual_price = market_sell_immediate(token_id, size, price=best_bid, position=pos)
                                    if actual_price == "NO_BALANCE":
                                        print(f"  ⚠️ 余额为零，标记持仓关闭等结算")
                                        _exit_price = _resolve_or_estimate_exit_price(token_id, current_price, entry_price, slug, direction)
                                        self_notify(pos, _exit_price, coin, direction, size, "阶段3(余额已清)")
                                        close_position(pos, _exit_price)
                                        close_attempts.pop(attempt_key, None)
                                        continue
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
                                ok, actual_price = market_sell_immediate(token_id, size, position=pos)
                                if actual_price == "NO_BALANCE":
                                    print(f"  ⚠️ 余额为零，标记持仓关闭等结算")
                                    _exit_price = _resolve_or_estimate_exit_price(token_id, current_price, entry_price, slug, direction)
                                    self_notify(pos, _exit_price, coin, direction, size, "阶段3(余额已清)")
                                    close_position(pos, _exit_price)
                                    close_attempts.pop(attempt_key, None)
                                    continue
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
                        ok, actual_price = market_sell_immediate(token_id, size, position=pos)
                        if actual_price == "NO_BALANCE":
                            print(f"  ⚠️ 余额为零，标记持仓关闭等结算")
                            _exit_price = _resolve_or_estimate_exit_price(token_id, current_price, entry_price, slug, direction)
                            self_notify(pos, _exit_price, coin, direction, size, "阶段4(余额已清)")
                            close_position(pos, _exit_price)
                            close_attempts.pop(attempt_key, None)
                            continue
                        elif ok:
                            sold = True
                            sold_price = actual_price
                            self_notify(pos, sold_price, coin, direction, size, "阶段4兜底")
                
                if sold:
                    close_position(pos, sold_price)
                    close_attempts.pop(attempt_key, None)
                    dip_bought.pop(attempt_key, None)
                    direction_wrong_streak.pop(attempt_key, None)
                    direction_history.pop(attempt_key, None)
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

        # [P2] v12.8: 双源事件驱动 — Chainlink 或 CLOB orderbook 任一推送即唤醒
        # 先检查 CLOB WS 是否已有事件（~0ms），再等 Chainlink（最多50ms兜底）
        _MONITOR_POLL_MS = float(os.environ.get("MONITOR_POLL_MS", "50")) / 1000.0
        clob_woke = _poly_ws.wait_for_update(timeout=0)  # 非阻塞检查
        if clob_woke:
            last_wake_context = {"label": "clob_ws_push", "detail": "orderbook"}
        else:
            wake_info = _chainlink_stream.wait_for_update(timeout=_MONITOR_POLL_MS, with_details=True)
            if wake_info.get("updated"):
                event = wake_info.get("event") or {}
                event_coin = event.get("coin") or "?"
                event_age = _format_age_ms(event.get("age_ms"))
                last_wake_context = {
                    "label": "chainlink_push",
                    "detail": f"{event_coin}/{event_age}",
                }
            else:
                last_wake_context = {
                    "label": "timeout_poll",
                    "detail": _format_age_ms(wake_info.get("wait_ms")),
                }


def self_notify(pos, sell_price, coin, direction, size, label):
    """统一平仓通知"""
    entry_price = pos["entry_price"]
    # Bug fix: 用 _calculate_total_realized_pnl 聚合 partial_exits，与 outcomes 一致
    profit = _calculate_total_realized_pnl(pos, sell_price)
    total_size = _realized_trade_size(pos)
    total_cost = entry_price * total_size if entry_price > 0 else 0
    pct = (profit / total_cost * 100) if total_cost > 0 else 0
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
    clob_client.init_client()
    monitor()
