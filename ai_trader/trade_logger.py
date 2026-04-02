"""
v14.1 Trade Decision Logger — 结构化决策快照

每次入场/追价/止盈/止损/forced-sell 写一条 JSONL 记录到 logs/trade_decisions.jsonl
用于 after-fee 分路径收益归因，替代日志讲故事。
"""
import json
import os
import threading
import time

_LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
_LOG_FILE = os.path.join(_LOG_DIR, "trade_decisions.jsonl")
_lock = threading.Lock()


def log_decision(event_type, **kwargs):
    """写一条结构化决策记录

    event_type: "entry" | "reprice" | "tp_hit" | "stop_loss" | "forced_sell" | "expire" | "direction_flip" | "shadow"
    kwargs: 决策上下文（自动加 timestamp）
    """
    record = {
        "ts": time.time(),
        "event": event_type,
    }
    record.update(kwargs)
    try:
        with _lock:
            os.makedirs(_LOG_DIR, exist_ok=True)
            with open(_LOG_FILE, "a") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass  # 日志不能影响交易


def log_entry(slug, coin, entry_path, **ctx):
    """记录入场决策"""
    log_decision("entry", slug=slug, coin=coin, entry_path=entry_path, **ctx)


def log_exit(slug, coin, exit_reason, **ctx):
    """记录退出决策"""
    log_decision(exit_reason, slug=slug, coin=coin, **ctx)


def log_shadow(slug, coin, entry_path, **ctx):
    """记录 shadow 模式信号（不下单，只记录）"""
    log_decision("shadow", slug=slug, coin=coin, entry_path=entry_path, **ctx)


def read_decisions(path=None):
    """读取所有决策记录，返回 list[dict]"""
    path = path or _LOG_FILE
    records = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
    except FileNotFoundError:
        pass
    return records


def summarize_by_path(records=None):
    """按 entry_path 分路径汇总 after-fee 统计

    Returns: dict[path] -> {trades, wins, losses, avg_pnl, tp_rate, sl_rate, forced_rate, ...}
    """
    if records is None:
        records = read_decisions()

    # 配对 entry + exit
    entries = {}  # slug -> entry record
    paired = []   # (entry, exit) pairs

    for r in records:
        slug = r.get("slug", "")
        if r["event"] == "entry":
            entries[slug] = r
        elif r["event"] in ("tp_hit", "stop_loss", "forced_sell", "expire", "direction_flip"):
            entry = entries.pop(slug, None)
            if entry:
                paired.append((entry, r))

    # 按 entry_path 分组
    paths = {}
    for entry, exit_r in paired:
        path = entry.get("entry_path", "unknown")
        if path not in paths:
            paths[path] = {
                "trades": 0, "wins": 0, "losses": 0,
                "total_pnl": 0, "total_fee": 0,
                "exit_reasons": {},
                "avg_hold_sec": 0, "_hold_secs": [],
                "maker_entries": 0, "taker_entries": 0,
            }
        s = paths[path]
        s["trades"] += 1

        pnl = exit_r.get("realized_pnl", 0)
        s["total_pnl"] += pnl
        if pnl > 0:
            s["wins"] += 1
        else:
            s["losses"] += 1

        fee = entry.get("fee_estimate", 0) + exit_r.get("fee_estimate", 0)
        s["total_fee"] += fee

        reason = exit_r["event"]
        s["exit_reasons"][reason] = s["exit_reasons"].get(reason, 0) + 1

        hold = exit_r.get("ts", 0) - entry.get("ts", 0)
        s["_hold_secs"].append(hold)

        if entry.get("maker_or_taker") == "maker":
            s["maker_entries"] += 1
        else:
            s["taker_entries"] += 1

    # 计算汇总
    for path, s in paths.items():
        n = s["trades"]
        s["avg_pnl"] = round(s["total_pnl"] / n, 4) if n > 0 else 0
        s["win_rate"] = round(s["wins"] / n, 4) if n > 0 else 0
        s["avg_hold_sec"] = round(sum(s["_hold_secs"]) / n, 1) if n > 0 else 0
        s["avg_fee"] = round(s["total_fee"] / n, 4) if n > 0 else 0
        # 退出原因占比
        for reason in s["exit_reasons"]:
            s[f"{reason}_rate"] = round(s["exit_reasons"][reason] / n, 4)
        del s["_hold_secs"]

    return paths
