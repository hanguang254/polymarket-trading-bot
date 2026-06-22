import json
from dataclasses import dataclass
from itertools import product
from pathlib import Path


@dataclass(frozen=True)
class DecisionSample:
    direction: str
    ev: float
    confidence: float
    diff_atr: float
    price: float
    price_source: str
    action: str = ""
    coin: str = ""
    slug: str = ""


def _float_or_none(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_decision(record):
    direction = str(record.get("direction") or "").upper()
    if direction not in ("UP", "DOWN"):
        return None

    ev = _float_or_none(record.get("ev", record.get("expected_value")))
    confidence = _float_or_none(record.get("confidence"))
    diff_atr = _float_or_none(record.get("diff_in_atr"))

    price = _float_or_none(record.get("exec_price"))
    price_source = "exec_price"
    if price is None:
        price = _float_or_none(record.get("target_odds"))
        price_source = "target_odds"
    if price is None:
        price_key = "up_odds" if direction == "UP" else "down_odds"
        price = _float_or_none(record.get(price_key))
        price_source = "direction_odds"

    if ev is None or confidence is None or diff_atr is None or price is None:
        return None

    return DecisionSample(
        direction=direction,
        ev=ev,
        confidence=confidence,
        diff_atr=diff_atr,
        price=price,
        price_source=price_source,
        action=str(record.get("action") or ""),
        coin=str(record.get("coin") or ""),
        slug=str(record.get("slug") or ""),
    )


def load_decision_samples(path):
    stats = {"lines": 0, "bad_json": 0, "unusable": 0, "fallback_price": 0}
    samples = []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        stats["lines"] += 1
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            stats["bad_json"] += 1
            continue
        sample = normalize_decision(record)
        if sample is None:
            stats["unusable"] += 1
            continue
        if sample.price_source != "exec_price":
            stats["fallback_price"] += 1
        samples.append(sample)
    return samples, stats


def accepts(sample, max_price, min_ev, min_atr, min_confidence):
    return (
        sample.price < max_price
        and sample.ev > min_ev
        and sample.diff_atr >= min_atr
        and sample.confidence >= min_confidence
    )


def evaluate_grid(
    samples,
    max_prices,
    min_evs,
    min_atrs,
    min_confidences,
    target_rate=0.5,
):
    total = len(samples)
    results = []
    for max_price, min_ev, min_atr, min_conf in product(
        max_prices, min_evs, min_atrs, min_confidences
    ):
        accepted_samples = [
            sample for sample in samples
            if accepts(sample, max_price, min_ev, min_atr, min_conf)
        ]
        accepted = len(accepted_samples)
        evs = [sample.ev for sample in accepted_samples]
        avg_ev = sum(evs) / accepted if accepted else 0.0
        rate = accepted / total if total else 0.0
        results.append({
            "max_price": max_price,
            "min_ev": min_ev,
            "min_atr": min_atr,
            "min_confidence": min_conf,
            "accepted": accepted,
            "total": total,
            "rate": rate,
            "avg_ev": avg_ev,
            "min_sample_ev": min(evs) if evs else None,
            "target_gap": abs(rate - target_rate),
        })
    return sorted(results, key=lambda item: (item["target_gap"], -item["avg_ev"]))


def format_grid_results(results, limit=15):
    lines = [
        "rank max_price min_ev min_atr min_conf accepted rate avg_ev min_ev_seen"
    ]
    for idx, item in enumerate(results[:limit], 1):
        min_seen = item["min_sample_ev"]
        min_seen_s = "NA" if min_seen is None else f"{min_seen:+.4f}"
        lines.append(
            f"{idx:>4} "
            f"{item['max_price']:.2f} "
            f"{item['min_ev']:.3f} "
            f"{item['min_atr']:.2f} "
            f"{item['min_confidence']:.2f} "
            f"{item['accepted']:>4}/{item['total']:<4} "
            f"{item['rate']*100:>5.1f}% "
            f"{item['avg_ev']:+.4f} "
            f"{min_seen_s}"
        )
    return "\n".join(lines)
