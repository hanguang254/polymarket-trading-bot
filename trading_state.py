#!/usr/bin/env python3
"""
交易状态管理
"""
import json
import os
import fcntl
import threading
from datetime import datetime, timezone, date

MAX_DAILY_LOSS = float(os.environ.get("MAX_DAILY_LOSS", "10.0"))  # 每日最大亏损额（USDC）

# 线程锁 + 文件锁：保护跨线程 + 跨进程的 state 读写
_trade_lock = threading.Lock()

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "trading_state.json")
_LOCK_FILE = STATE_FILE + ".lock"


def _today_str():
    return str(date.today())


def _normalize_pending_costs(state):
    """兼容旧格式 pending_costs，并保留开仓日期避免跨天回补污染新交易日。"""
    pending = state.get("pending_costs", {})
    normalized = {}

    if isinstance(pending, dict):
        fallback_date = str(state.get("daily_pnl_date") or _today_str())
        for slug, value in pending.items():
            try:
                if isinstance(value, dict):
                    cost = float(value.get("cost", 0) or 0)
                    session_date = str(value.get("session_date") or value.get("date") or fallback_date)
                else:
                    cost = float(value or 0)
                    session_date = fallback_date
            except (TypeError, ValueError):
                continue

            if cost > 0:
                normalized[slug] = {
                    "cost": round(cost, 4),
                    "session_date": session_date,
                }

    state["pending_costs"] = normalized
    return normalized


def _get_pending_entry(pending, slug):
    entry = pending.get(slug)
    if not entry:
        return 0.0, None
    if isinstance(entry, dict):
        try:
            return float(entry.get("cost", 0) or 0), entry.get("session_date")
        except (TypeError, ValueError):
            return 0.0, entry.get("session_date")
    try:
        return float(entry or 0), None
    except (TypeError, ValueError):
        return 0.0, None


class _StateLock:
    """线程锁 + 文件锁，保证跨线程和跨进程的原子性"""
    def __enter__(self):
        _trade_lock.acquire()
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        self._f = open(_LOCK_FILE, "w")
        fcntl.flock(self._f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *args):
        fcntl.flock(self._f, fcntl.LOCK_UN)
        self._f.close()
        _trade_lock.release()


def load_state():
    """加载交易状态"""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {
        "consecutive_losses": 0,
        "cooldown_remaining": 0,
        "last_bet_time": None,
        "last_bet_result": None,
        "total_bets": 0,
        "total_wins": 0,
        "total_losses": 0,
        "daily_pnl": 0.0,
        "daily_pnl_date": _today_str(),
    }

def save_state(state):
    """保存交易状态"""
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def _reset_daily_if_needed(state):
    """日期切换时重置当日PnL，返回是否发生了重置"""
    today = _today_str()
    if state.get("daily_pnl_date") != today:
        state["daily_pnl"] = 0.0
        state["daily_pnl_date"] = today
        # Bug fix: 清除前日遗留的 pending_costs，避免隔夜条目永久累积
        # 这些条目的 refund 在 settle_bet_cost 中已为 0（跨日不回补），
        # 但不清理会导致 record_bet_cost 对同 slug 新仓位累加到旧成本上。
        pending = state.get("pending_costs", {})
        if isinstance(pending, dict):
            stale_slugs = [
                slug for slug, entry in pending.items()
                if not isinstance(entry, dict) or entry.get("session_date") != today
            ]
            for slug in stale_slugs:
                pending.pop(slug, None)
            state["pending_costs"] = pending
        return True
    return False

def should_trade():
    """检查是否应该交易（考虑冷却期 + 日亏损上限）"""
    state = load_state()
    _normalize_pending_costs(state)
    if state["cooldown_remaining"] > 0:
        return False
    # 日期切换时重置当日PnL
    today = _today_str()
    if state.get("daily_pnl_date") != today:
        return True
    daily_pnl = state.get("daily_pnl", 0.0)
    if daily_pnl <= -MAX_DAILY_LOSS:
        return False  # 当日亏损超限，停止交易
    return True


def check_daily_loss_limit():
    """线程安全的日亏损检查（在下注前调用）
    Returns: (allowed: bool, daily_pnl: float, limit: float)
    """
    with _StateLock():
        state = load_state()
        _normalize_pending_costs(state)
        if _reset_daily_if_needed(state):
            save_state(state)
            return True, 0.0, MAX_DAILY_LOSS
        daily_pnl = state.get("daily_pnl", 0.0)
        if daily_pnl <= -MAX_DAILY_LOSS:
            return False, daily_pnl, MAX_DAILY_LOSS
        return True, daily_pnl, MAX_DAILY_LOSS


def record_bet_cost(slug, cost):
    """下注时立即扣减 daily_pnl（假设最坏情况全亏），防止并行超限
    cost: 下注金额（正数），会作为负值计入 daily_pnl
    """
    with _StateLock():
        state = load_state()
        _reset_daily_if_needed(state)
        state["daily_pnl"] = round(state.get("daily_pnl", 0.0) - cost, 4)
        # 累加而非覆盖（支持抄底加仓追加成本）
        pending = _normalize_pending_costs(state)
        prev_cost, _ = _get_pending_entry(pending, slug)
        pending[slug] = {
            "cost": round(prev_cost + cost, 4),
            "session_date": state.get("daily_pnl_date", _today_str()),
        }
        save_state(state)
        return state["daily_pnl"]


def settle_bet_cost(slug, actual_pnl):
    """平仓时用实际盈亏替换预扣的成本
    actual_pnl: 实际盈亏（正=盈利，负=亏损）
    """
    with _StateLock():
        state = load_state()
        pending = _normalize_pending_costs(state)
        _reset_daily_if_needed(state)
        cost, entry_session = _get_pending_entry(pending, slug)
        pending.pop(slug, None)
        current_session = state.get("daily_pnl_date", _today_str())
        refund = cost if entry_session == current_session else 0.0
        # 同日开仓的预扣成本需要回补；隔夜持仓只把实际PnL记入新交易日。
        state["daily_pnl"] = round(state.get("daily_pnl", 0.0) + refund + actual_pnl, 4)
        state["pending_costs"] = pending
        save_state(state)
        return state["daily_pnl"]


def record_partial_pnl(slug, sold_price, sold_count, entry_price):
    """分批平仓时记录部分成交的盈亏 + 回补对应份额的预扣成本
    相当于对这些份额做一次mini-settlement
    """
    partial_cost = entry_price * sold_count  # 这些份额的原始预扣成本
    partial_pnl = (sold_price - entry_price) * sold_count  # 这些份额的盈亏
    with _StateLock():
        state = load_state()
        pending = _normalize_pending_costs(state)
        _reset_daily_if_needed(state)
        pending_cost, entry_session = _get_pending_entry(pending, slug)
        settled_cost = min(partial_cost, pending_cost)
        current_session = state.get("daily_pnl_date", _today_str())
        refund = settled_cost if entry_session == current_session else 0.0
        # 隔夜仓位的成本不回补到今天，只记录今天实际实现的PnL。
        state["daily_pnl"] = round(state.get("daily_pnl", 0.0) + refund + partial_pnl, 4)
        # 减少 pending_costs（这些份额已结算）
        if slug in pending:
            remaining_cost = round(max(0.0, pending_cost - settled_cost), 4)
            if remaining_cost > 0:
                pending[slug] = {
                    "cost": remaining_cost,
                    "session_date": entry_session or current_session,
                }
            else:
                pending.pop(slug, None)
        state["pending_costs"] = pending
        save_state(state)
        return state["daily_pnl"]


def record_bet_result(success, slug, pnl=0.0):
    """记录下注结果，pnl 为本次盈亏（正=盈利，负=亏损）"""
    with _StateLock():
        state = load_state()
        _normalize_pending_costs(state)
        state["total_bets"] += 1
        state["last_bet_time"] = datetime.now(timezone.utc).isoformat()
        state["last_bet_result"] = "win" if success else "loss"

        _reset_daily_if_needed(state)
        state["daily_pnl"] = round(state.get("daily_pnl", 0.0) + pnl, 4)

        if success:
            state["total_wins"] += 1
            state["consecutive_losses"] = 0
            state["cooldown_remaining"] = 0
        else:
            state["total_losses"] += 1
            state["consecutive_losses"] += 1
            state["cooldown_remaining"] = 3  # 观望3期

        save_state(state)
        return state

def decrease_cooldown():
    """减少冷却期计数（每次分析后调用）"""
    with _StateLock():
        state = load_state()
        if state["cooldown_remaining"] > 0:
            state["cooldown_remaining"] -= 1
            save_state(state)
        return state["cooldown_remaining"]

def get_state_summary():
    """获取状态摘要"""
    state = load_state()
    return (
        f"总下注: {state['total_bets']} | "
        f"胜: {state['total_wins']} | "
        f"负: {state['total_losses']} | "
        f"连败: {state['consecutive_losses']} | "
        f"观望剩余: {state['cooldown_remaining']}期 | "
        f"今日PnL: ${state.get('daily_pnl', 0.0):+.2f}"
    )
