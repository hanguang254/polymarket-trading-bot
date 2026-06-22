#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_trader.strategy_grid import (
    evaluate_grid,
    format_grid_results,
    load_decision_samples,
)


def _floats(csv):
    return [float(part.strip()) for part in csv.split(",") if part.strip()]


def main():
    parser = argparse.ArgumentParser(
        description="Replay main-strategy decision logs against threshold grids."
    )
    parser.add_argument("--decisions", default="logs/decisions_v2.jsonl")
    parser.add_argument("--target-rate", type=float, default=0.50)
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--max-prices", default="0.65,0.70,0.72,0.75,0.80")
    parser.add_argument("--min-evs", default="0.00,0.01,0.02,0.03,0.04")
    parser.add_argument("--min-atrs", default="0.50,0.75,1.00,1.25")
    parser.add_argument("--min-confs", default="0.25,0.30,0.35,0.40,0.45")
    args = parser.parse_args()

    path = Path(args.decisions)
    if not path.exists():
        raise SystemExit(
            f"decision log not found: {path}\n"
            "Run auto_bot_v3.py until logs/decisions_v2.jsonl has fresh records, "
            "or pass --decisions PATH."
        )
    samples, stats = load_decision_samples(path)
    results = evaluate_grid(
        samples,
        max_prices=_floats(args.max_prices),
        min_evs=_floats(args.min_evs),
        min_atrs=_floats(args.min_atrs),
        min_confidences=_floats(args.min_confs),
        target_rate=args.target_rate,
    )

    print(f"decisions={path}")
    print(
        "loaded={loaded} usable={usable} bad_json={bad_json} unusable={unusable} "
        "fallback_price={fallback_price}".format(
            loaded=stats["lines"],
            usable=len(samples),
            bad_json=stats["bad_json"],
            unusable=stats["unusable"],
            fallback_price=stats["fallback_price"],
        )
    )
    if stats["fallback_price"]:
        print(
            "warning: some records lack exec_price, so direction odds were used as a "
            "price fallback. Future decisions will be more accurate after exec_price logging."
        )
    print(format_grid_results(results, limit=args.top))


if __name__ == "__main__":
    main()
