#!/usr/bin/env python3
"""
PnL-based backtest/report for the live Polymarket bot.

Old versions of this repo measured "direction accuracy", which is not the same
as realized trading performance for a strategy with early exits, stop-losses,
partial closes, hedges, and pending-order fills.

This script rebuilds a trading report from local log files only:
  - decisions_v2.jsonl: signal funnel / model diagnostics
  - bets.jsonl: order attempts and fill path
  - outcomes.jsonl: realized trade outcomes

Primary metric is realized PnL, not direction correctness.
"""
import argparse
import json
import math
import os
from collections import defaultdict
from datetime import datetime, timezone


def load_jsonl(path):
    rows = []
    if not os.path.exists(path):
        return rows

    with open(path, "r") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def parse_timestamp(value):
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            return None
    return None


def safe_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def trade_key(record):
    slug = record.get("slug")
    direction = record.get("direction")
    if not slug or not direction:
        return None
    return slug, direction


def slug_to_coin(slug):
    if not slug:
        return "UNKNOWN"
    return slug.split("-")[0].upper()


def pick_latest_by_key(rows):
    latest = {}
    for row in rows:
        key = trade_key(row)
        if not key:
            continue
        ts = parse_timestamp(row.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc)
        current = latest.get(key)
        if current is None:
            latest[key] = row
            continue
        current_ts = parse_timestamp(current.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc)
        if ts >= current_ts:
            latest[key] = row
    return latest


def group_by_key(rows):
    grouped = defaultdict(list)
    for row in rows:
        key = trade_key(row)
        if not key:
            continue
        grouped[key].append(row)

    for key in grouped:
        grouped[key].sort(
            key=lambda r: parse_timestamp(r.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc)
        )
    return grouped


def choose_entry_attempt(attempts):
    if not attempts:
        return None

    success_attempts = [a for a in attempts if a.get("success")]
    if success_attempts:
        return success_attempts[0]

    pending_attempts = [a for a in attempts if a.get("pending")]
    if pending_attempts:
        return pending_attempts[0]

    return attempts[-1]


def compute_trade_pnl(outcome):
    realized = safe_float(outcome.get("realized_pnl"))
    if realized is not None:
        return realized, "realized_pnl"

    entry_price = safe_float(outcome.get("entry_price"), 0.0)
    exit_price = safe_float(outcome.get("exit_price"), 0.0)
    size = safe_float(outcome.get("size"), 0.0)
    return round((exit_price - entry_price) * size, 4), "entry_exit_fallback"


def summarize_trade_slice(trades):
    if not trades:
        return {
            "count": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "gross_pnl": 0.0,
            "gross_profit": 0.0,
            "gross_loss": 0.0,
            "profit_factor": None,
            "avg_pnl": 0.0,
            "avg_roi_pct": 0.0,
            "capital_deployed": 0.0,
        }

    gross_pnl = round(sum(t["pnl"] for t in trades), 4)
    gross_profit = round(sum(t["pnl"] for t in trades if t["pnl"] > 0), 4)
    gross_loss = round(sum(t["pnl"] for t in trades if t["pnl"] < 0), 4)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    losses = sum(1 for t in trades if t["pnl"] < 0)
    capital = round(sum(t["entry_cost"] for t in trades if t["entry_cost"] > 0), 4)
    roi_values = [t["roi_pct"] for t in trades if t["roi_pct"] is not None]
    profit_factor = None
    if gross_loss < 0:
        profit_factor = round(gross_profit / abs(gross_loss), 4)

    return {
        "count": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / len(trades), 4),
        "gross_pnl": gross_pnl,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": profit_factor,
        "avg_pnl": round(gross_pnl / len(trades), 4),
        "avg_roi_pct": round(sum(roi_values) / len(roi_values), 4) if roi_values else 0.0,
        "capital_deployed": capital,
    }


def compute_max_drawdown(trades):
    if not trades:
        return 0.0

    ordered = sorted(
        trades,
        key=lambda t: parse_timestamp(t.get("close_timestamp")) or datetime.min.replace(tzinfo=timezone.utc),
    )
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for trade in ordered:
        equity += trade["pnl"]
        if equity > peak:
            peak = equity
        drawdown = peak - equity
        if drawdown > max_drawdown:
            max_drawdown = drawdown
    return round(max_drawdown, 4)


def make_breakdown(trades, field):
    groups = defaultdict(list)
    for trade in trades:
        groups[trade.get(field) or "UNKNOWN"].append(trade)

    breakdown = []
    for key, group in sorted(groups.items(), key=lambda item: (-sum(t["pnl"] for t in item[1]), item[0])):
        stats = summarize_trade_slice(group)
        stats[field] = key
        breakdown.append(stats)
    return breakdown


def build_report(log_dir="logs"):
    decisions = load_jsonl(os.path.join(log_dir, "decisions_v2.jsonl"))
    bets = load_jsonl(os.path.join(log_dir, "bets.jsonl"))
    outcomes = load_jsonl(os.path.join(log_dir, "outcomes.jsonl"))

    latest_decisions = pick_latest_by_key(decisions)
    attempts_by_key = group_by_key(bets)

    signal_rows = [d for d in decisions if d.get("action") == "BET"]
    successful_attempts = [b for b in bets if b.get("success")]
    pending_attempts = [b for b in bets if b.get("pending")]
    failed_attempts = [
        b for b in bets
        if not b.get("success") and not b.get("pending")
    ]

    trades = []
    for outcome in outcomes:
        key = trade_key(outcome)
        if not key:
            continue

        pnl, pnl_source = compute_trade_pnl(outcome)
        entry_price = safe_float(outcome.get("entry_price"), 0.0)
        exit_price = safe_float(outcome.get("exit_price"), 0.0)
        size = safe_float(outcome.get("size"), 0.0)

        decision = latest_decisions.get(key, {})
        attempts = attempts_by_key.get(key, [])
        chosen_attempt = choose_entry_attempt(attempts) or {}
        entry_cost = safe_float(outcome.get("entry_cost"))
        if entry_cost is None or entry_cost <= 0:
            entry_cost = safe_float(chosen_attempt.get("amount"))
        if entry_cost is None or entry_cost <= 0:
            entry_cost = round(entry_price * size, 4) if entry_price > 0 and size > 0 else 0.0
        else:
            entry_cost = round(entry_cost, 4)
        roi_pct = round((pnl / entry_cost) * 100, 4) if entry_cost > 0 else None
        attempt_mode = "unknown"
        if chosen_attempt.get("success"):
            attempt_mode = "immediate_fill"
        elif chosen_attempt.get("pending"):
            attempt_mode = "pending_fill"
        elif chosen_attempt:
            attempt_mode = "attempt_no_fill"

        trade = {
            "slug": outcome.get("slug"),
            "coin": decision.get("coin") or slug_to_coin(outcome.get("slug")),
            "direction": outcome.get("direction"),
            "atr_band": outcome.get("atr_band"),
            "entry_price": entry_price,
            "exit_price": exit_price,
            "size": size,
            "entry_cost": entry_cost,
            "pnl": pnl,
            "pnl_source": pnl_source,
            "roi_pct": roi_pct,
            "close_timestamp": outcome.get("timestamp"),
            "decision_timestamp": decision.get("timestamp"),
            "bet_timestamp": chosen_attempt.get("timestamp"),
            "entry_mode": attempt_mode,
            "model_confidence": safe_float(decision.get("confidence")),
            "model_ev": safe_float(decision.get("ev")),
            "diff_in_atr": safe_float(outcome.get("diff_in_atr"), safe_float(decision.get("diff_in_atr"))),
        }
        trades.append(trade)

    summary = summarize_trade_slice(trades)
    summary["total_decisions"] = len(decisions)
    summary["bet_signals"] = len(signal_rows)
    summary["order_attempts"] = len(bets)
    summary["immediate_fills"] = len(successful_attempts)
    summary["pending_orders"] = len(pending_attempts)
    summary["failed_orders"] = len(failed_attempts)
    summary["realized_trades"] = len(trades)
    summary["unresolved_attempts"] = max(0, len(bets) - len(trades))
    summary["signal_to_attempt_rate"] = round(len(bets) / len(signal_rows), 4) if signal_rows else 0.0
    summary["attempt_to_realized_rate"] = round(len(trades) / len(bets), 4) if bets else 0.0
    summary["return_on_deployed_capital_pct"] = (
        round((summary["gross_pnl"] / summary["capital_deployed"]) * 100, 4)
        if summary["capital_deployed"] > 0 else 0.0
    )
    summary["max_drawdown"] = compute_max_drawdown(trades)
    summary["estimated_pnl_trades"] = sum(1 for t in trades if t["pnl_source"] != "realized_pnl")

    best_trade = max(trades, key=lambda t: t["pnl"], default=None)
    worst_trade = min(trades, key=lambda t: t["pnl"], default=None)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "log_dir": os.path.abspath(log_dir),
        "summary": summary,
        "best_trade": best_trade,
        "worst_trade": worst_trade,
        "breakdown": {
            "by_coin": make_breakdown(trades, "coin"),
            "by_direction": make_breakdown(trades, "direction"),
            "by_entry_mode": make_breakdown(trades, "entry_mode"),
            "by_atr_band": make_breakdown(trades, "atr_band"),
        },
        "trades": sorted(
            trades,
            key=lambda t: parse_timestamp(t.get("close_timestamp")) or datetime.min.replace(tzinfo=timezone.utc),
        ),
    }
    return report


def fmt_pct(value):
    if value is None:
        return "n/a"
    return f"{value:.2f}%"


def format_report(report):
    summary = report["summary"]
    lines = [
        "PnL Backtest Report",
        f"Logs: {report['log_dir']}",
        "",
        "Funnel",
        f"  Decisions:        {summary['total_decisions']}",
        f"  BET signals:      {summary['bet_signals']}",
        f"  Order attempts:   {summary['order_attempts']}",
        f"  Immediate fills:  {summary['immediate_fills']}",
        f"  Pending orders:   {summary['pending_orders']}",
        f"  Failed attempts:  {summary['failed_orders']}",
        f"  Realized trades:  {summary['realized_trades']}",
        "",
        "PnL",
        f"  Gross PnL:        ${summary['gross_pnl']:+.4f}",
        f"  Avg PnL/trade:    ${summary['avg_pnl']:+.4f}",
        f"  Win rate:         {fmt_pct(summary['win_rate'] * 100)}",
        f"  Avg ROI/trade:    {fmt_pct(summary['avg_roi_pct'])}",
        f"  Profit factor:    {summary['profit_factor'] if summary['profit_factor'] is not None else 'n/a'}",
        f"  Capital deployed: ${summary['capital_deployed']:.4f}",
        f"  Return on cap:    {fmt_pct(summary['return_on_deployed_capital_pct'])}",
        f"  Max drawdown:     ${summary['max_drawdown']:.4f}",
        "",
    ]

    if summary["estimated_pnl_trades"] > 0:
        lines.append(
            f"Note: {summary['estimated_pnl_trades']} trade(s) used entry/exit fallback PnL because realized_pnl was absent."
        )
        lines.append("")

    best_trade = report.get("best_trade")
    if best_trade:
        lines.extend([
            "Best Trade",
            f"  {best_trade['slug']} {best_trade['direction']} {best_trade['coin']} ${best_trade['pnl']:+.4f} ROI {fmt_pct(best_trade['roi_pct'])}",
            "",
        ])

    worst_trade = report.get("worst_trade")
    if worst_trade:
        lines.extend([
            "Worst Trade",
            f"  {worst_trade['slug']} {worst_trade['direction']} {worst_trade['coin']} ${worst_trade['pnl']:+.4f} ROI {fmt_pct(worst_trade['roi_pct'])}",
            "",
        ])

    lines.append("By Coin")
    for row in report["breakdown"]["by_coin"]:
        lines.append(
            f"  {row['coin']}: trades={row['count']} pnl=${row['gross_pnl']:+.4f} win_rate={fmt_pct(row['win_rate'] * 100)}"
        )

    lines.append("")
    lines.append("By Entry Mode")
    for row in report["breakdown"]["by_entry_mode"]:
        lines.append(
            f"  {row['entry_mode']}: trades={row['count']} pnl=${row['gross_pnl']:+.4f} avg_roi={fmt_pct(row['avg_roi_pct'])}"
        )

    return "\n".join(lines)


def save_report(report, output_dir):
    os.makedirs(output_dir, exist_ok=True)
    report_path = os.path.join(output_dir, "backtest_report.json")
    trades_path = os.path.join(output_dir, "backtest_trades.json")

    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    with open(trades_path, "w") as f:
        json.dump(report["trades"], f, indent=2)

    return report_path, trades_path


def main(argv=None):
    parser = argparse.ArgumentParser(description="PnL-based backtest report for Polymarket bot logs")
    parser.add_argument("--log-dir", default="logs", help="Directory containing decisions_v2.jsonl / bets.jsonl / outcomes.jsonl")
    parser.add_argument("--save-dir", default="logs", help="Directory to write backtest_report.json / backtest_trades.json")
    args = parser.parse_args(argv)

    report = build_report(args.log_dir)
    report_path, trades_path = save_report(report, args.save_dir)
    print(format_report(report))
    print("")
    print(f"Saved: {report_path}")
    print(f"Saved: {trades_path}")


if __name__ == "__main__":
    main()
