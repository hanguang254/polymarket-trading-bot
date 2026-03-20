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
        "daily_pnl_date": str(date.today()),
    }

def save_state(state):
    """保存交易状态"""
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def _reset_daily_if_needed(state):
    """日期切换时重置当日PnL，返回是否发生了重置"""
    today = str(date.today())
    if state.get("daily_pnl_date") != today:
        state["daily_pnl"] = 0.0
        state["daily_pnl_date"] = today
        return True
    return False

def should_trade():
    """检查是否应该交易（考虑冷却期 + 日亏损上限）"""
    state = load_state()
    if state["cooldown_remaining"] > 0:
        return False
    # 日期切换时重置当日PnL
    today = str(date.today())
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
        pending = state.setdefault("pending_costs", {})
        pending[slug] = round(pending.get(slug, 0.0) + cost, 4)
        save_state(state)
        return state["daily_pnl"]


def settle_bet_cost(slug, actual_pnl):
    """平仓时用实际盈亏替换预扣的成本
    actual_pnl: 实际盈亏（正=盈利，负=亏损）
    """
    with _StateLock():
        state = load_state()
        pending = state.get("pending_costs", {})
        cost = pending.pop(slug, 0.0)
        # 先回补预扣的成本，再加上实际盈亏
        state["daily_pnl"] = round(state.get("daily_pnl", 0.0) + cost + actual_pnl, 4)
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
        _reset_daily_if_needed(state)
        # 回补这些份额的预扣成本 + 加上实际盈亏
        state["daily_pnl"] = round(state.get("daily_pnl", 0.0) + partial_cost + partial_pnl, 4)
        # 减少 pending_costs（这些份额已结算）
        pending = state.get("pending_costs", {})
        if slug in pending:
            pending[slug] = round(max(0, pending[slug] - partial_cost), 4)
        state["pending_costs"] = pending
        save_state(state)
        return state["daily_pnl"]


def record_bet_result(success, slug, pnl=0.0):
    """记录下注结果，pnl 为本次盈亏（正=盈利，负=亏损）"""
    with _StateLock():
        state = load_state()
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
