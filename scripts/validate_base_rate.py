#!/usr/bin/env python3
"""
Base Rate 阈值校准验证脚本

读取 logs/outcomes.jsonl，按 diff_in_atr 分桶统计实际胜率，
与 base_rate 保守先验对比，帮助校准阈值。

用法: python scripts/validate_base_rate.py
"""
import json
import os
import sys

# 项目根目录
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTCOMES_FILE = os.path.join(ROOT, "logs", "outcomes.jsonl")
DECISIONS_FILE = os.path.join(ROOT, "logs", "decisions_v2.jsonl")

# 与 base_rate.py 保持一致的先验
BASE_RATE_PRIORS = {
    "0-0.7":   0.50,
    "0.7-1.0": 0.55,
    "1.0-1.5": 0.60,
    "1.5-2.0": 0.65,
    "2.0-3.0": 0.72,
    "3.0-4.0": 0.78,
    "4.0+":    0.85,
}

ATR_BANDS = ["0-0.7", "0.7-1.0", "1.0-1.5", "1.5-2.0", "2.0-3.0", "3.0-4.0", "4.0+"]


def atr_band(diff_in_atr: float) -> str:
    d = abs(diff_in_atr)
    if d >= 4.0:
        return "4.0+"
    elif d >= 3.0:
        return "3.0-4.0"
    elif d >= 2.0:
        return "2.0-3.0"
    elif d >= 1.5:
        return "1.5-2.0"
    elif d >= 1.0:
        return "1.0-1.5"
    elif d >= 0.7:
        return "0.7-1.0"
    else:
        return "0-0.7"


def load_outcomes():
    """加载 outcomes.jsonl"""
    records = []
    if not os.path.exists(OUTCOMES_FILE):
        return records
    with open(OUTCOMES_FILE) as f:
        for line in f:
            if line.strip():
                try:
                    records.append(json.loads(line))
                except Exception:
                    pass
    return records


def load_decisions():
    """加载 decisions_v2.jsonl（补充分析：所有决策的 diff_in_atr 分布）"""
    records = []
    if not os.path.exists(DECISIONS_FILE):
        return records
    with open(DECISIONS_FILE) as f:
        for line in f:
            if line.strip():
                try:
                    records.append(json.loads(line))
                except Exception:
                    pass
    return records


def analyze_outcomes(records):
    """按 ATR 带分桶统计胜率"""
    buckets = {band: {"wins": 0, "total": 0} for band in ATR_BANDS}

    for rec in records:
        band = rec.get("atr_band") or atr_band(rec.get("diff_in_atr", 0))
        if band not in buckets:
            continue
        buckets[band]["total"] += 1
        if rec.get("won"):
            buckets[band]["wins"] += 1

    return buckets


def analyze_decisions(records):
    """统计决策中 diff_in_atr 分布 + BET/SKIP 比例"""
    buckets = {band: {"bet": 0, "skip": 0} for band in ATR_BANDS}

    for rec in records:
        d = rec.get("diff_in_atr", 0)
        band = atr_band(d)
        if band not in buckets:
            continue
        if rec.get("action") == "BET":
            buckets[band]["bet"] += 1
        else:
            buckets[band]["skip"] += 1

    return buckets


def print_report(outcome_buckets, decision_buckets):
    """打印校准报告"""
    total_outcomes = sum(b["total"] for b in outcome_buckets.values())
    total_decisions = sum(b["bet"] + b["skip"] for b in decision_buckets.values())

    print("=" * 80)
    print("Base Rate 校准验证报告")
    print(f"数据来源: outcomes={total_outcomes}条, decisions={total_decisions}条")
    print("=" * 80)

    # 表头
    print(f"\n{'ATR带':<10} {'先验':>6} {'实证胜率':>10} {'样本':>6} {'胜':>4} {'负':>4} {'可信?':>6} {'决策BET':>8} {'决策SKIP':>9}")
    print("-" * 80)

    for band in ATR_BANDS:
        prior = BASE_RATE_PRIORS[band]
        ob = outcome_buckets[band]
        db = decision_buckets[band]
        n = ob["total"]
        wins = ob["wins"]
        losses = n - wins

        if n > 0:
            empirical = wins / n
            emp_str = f"{empirical:.1%}"
            # 差异标注
            diff = empirical - prior
            if abs(diff) > 0.10:
                emp_str += f" ({diff:+.0%}!)"
            elif abs(diff) > 0.05:
                emp_str += f" ({diff:+.0%})"
        else:
            emp_str = "N/A"

        reliable = "✅" if n >= 30 else f"({n}/30)"
        bet_count = db["bet"]
        skip_count = db["skip"]

        print(f"{band:<10} {prior:>6.0%} {emp_str:>10} {n:>6} {wins:>4} {losses:>4} {reliable:>6} {bet_count:>8} {skip_count:>9}")

    print("-" * 80)

    # 总结
    total_wins = sum(b["wins"] for b in outcome_buckets.values())
    if total_outcomes > 0:
        overall = total_wins / total_outcomes
        print(f"\n总体胜率: {overall:.1%} ({total_wins}/{total_outcomes})")

        # Kelly 阈值分析
        below_55 = sum(1 for b in ATR_BANDS
                       if outcome_buckets[b]["total"] >= 10
                       and outcome_buckets[b]["wins"] / outcome_buckets[b]["total"] < 0.55)
        print(f"胜率<55%的带(≥10样本): {below_55}个 → Kelly×0.5 阈值{'合理' if below_55 > 0 else '可能偏保守'}")
    else:
        print("\n⚠️ 无 outcome 数据。运行交易后再执行此脚本。")
        print(f"   outcomes 文件: {OUTCOMES_FILE}")

    if total_decisions > 0:
        total_bets = sum(b["bet"] for b in decision_buckets.values())
        print(f"\n下注率: {total_bets}/{total_decisions} = {total_bets/total_decisions:.1%}")


def main():
    outcomes = load_outcomes()
    decisions = load_decisions()

    outcome_buckets = analyze_outcomes(outcomes)
    decision_buckets = analyze_decisions(decisions)

    print_report(outcome_buckets, decision_buckets)


if __name__ == "__main__":
    main()
