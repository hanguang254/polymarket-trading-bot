"""
Base Rate 校准模块 v1.0

基于 Trading Desk Entry Protocol — Filter 2:
  "Before assigning probability, check historical base rate for the event category."
  "If base rate is below 12%, reduce position size by 50%."
  "This filter improved desk hit rate from 61% to 74%."

对于5分钟二元市场，base rate = 历史上该 ATR 带的胜率。
初始使用保守先验，数据积累后（≥30样本/带）自动切换为实证值。
"""
import json
import os
import time

OUTCOMES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs", "outcomes.jsonl")
BASE_RATES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs", "base_rates.json")

# 保守先验（故意低于 estimated_value 表，避免高估）
BASE_RATE_PRIORS = {
    "0-0.7":   0.50,   # 几乎平盘，无优势
    "0.7-1.0": 0.55,
    "1.0-1.5": 0.60,
    "1.5-2.0": 0.65,
    "2.0-3.0": 0.72,
    "3.0-4.0": 0.78,
    "4.0-5.0": 0.88,
    "5.0-6.0": 0.92,
    "6.0-8.0": 0.95,
    "8.0+":    0.97,
}

# 实证数据最少样本数
MIN_SAMPLES = 30

# 内存缓存
_cache = {"rates": None, "loaded_at": 0}
_CACHE_TTL = 60  # 60秒刷新


def _atr_band(diff_in_atr: float) -> str:
    """将 diff_in_atr 映射到离散带 key"""
    if diff_in_atr >= 8.0:
        return "8.0+"
    elif diff_in_atr >= 6.0:
        return "6.0-8.0"
    elif diff_in_atr >= 5.0:
        return "5.0-6.0"
    elif diff_in_atr >= 4.0:
        return "4.0-5.0"
    elif diff_in_atr >= 3.0:
        return "3.0-4.0"
    elif diff_in_atr >= 2.0:
        return "2.0-3.0"
    elif diff_in_atr >= 1.5:
        return "1.5-2.0"
    elif diff_in_atr >= 1.0:
        return "1.0-1.5"
    elif diff_in_atr >= 0.7:
        return "0.7-1.0"
    else:
        return "0-0.7"


def _load_empirical_rates() -> dict:
    """加载实证胜率（带缓存）"""
    now = time.time()
    if _cache["rates"] is not None and (now - _cache["loaded_at"]) < _CACHE_TTL:
        return _cache["rates"]

    rates = {}
    try:
        if os.path.exists(BASE_RATES_FILE):
            with open(BASE_RATES_FILE, "r") as f:
                data = json.load(f)
                # 格式: {"band": {"rate": float, "n": int}, ...}
                for band, info in data.items():
                    if info.get("n", 0) >= MIN_SAMPLES:
                        rates[band] = info["rate"]
    except Exception:
        pass

    _cache["rates"] = rates
    _cache["loaded_at"] = now
    return rates


def get_base_rate(diff_in_atr: float) -> float:
    """
    返回 ATR 带对应的胜率。
    有足够实证数据时用实证值，否则用保守先验。

    Args:
        diff_in_atr: 价格偏离 PTB 的 ATR 倍数（绝对值）
    Returns:
        float in [0.50, 0.97]
    """
    band = _atr_band(abs(diff_in_atr))

    # 先查实证数据
    empirical = _load_empirical_rates()
    if band in empirical:
        return empirical[band]

    # 回退到先验
    return BASE_RATE_PRIORS.get(band, 0.50)


def record_outcome(slug: str, direction: str, diff_in_atr: float, won: bool, extra: dict = None) -> None:
    """
    记录一次交易结果到 outcomes.jsonl

    Args:
        slug: 市场 slug
        direction: UP / DOWN
        diff_in_atr: 入场时的 ATR 偏离
        won: 是否盈利
        extra: 额外信息（entry_price, exit_price 等）
    """
    record = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "slug": slug,
        "direction": direction,
        "diff_in_atr": round(abs(diff_in_atr), 2),
        "atr_band": _atr_band(abs(diff_in_atr)),
        "won": won,
    }
    if extra:
        record.update(extra)

    try:
        os.makedirs(os.path.dirname(OUTCOMES_FILE), exist_ok=True)
        with open(OUTCOMES_FILE, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass

    # 每50条自动校准一次
    try:
        count = sum(1 for _ in open(OUTCOMES_FILE))
        if count % 50 == 0:
            calibrate()
    except Exception:
        pass


def calibrate() -> dict:
    """
    读 outcomes.jsonl，按 ATR 带计算实证胜率，存入 base_rates.json。

    Returns:
        dict: {"band": {"rate": float, "n": int, "wins": int}, ...}
    """
    bands = {}

    try:
        if not os.path.exists(OUTCOMES_FILE):
            return {}

        with open(OUTCOMES_FILE, "r") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                    band = rec.get("atr_band", _atr_band(rec.get("diff_in_atr", 0)))
                    if band not in bands:
                        bands[band] = {"wins": 0, "total": 0}
                    bands[band]["total"] += 1
                    if rec.get("won"):
                        bands[band]["wins"] += 1
                except Exception:
                    continue

        result = {}
        for band, stats in bands.items():
            n = stats["total"]
            wins = stats["wins"]
            rate = wins / n if n > 0 else 0.5
            result[band] = {
                "rate": round(rate, 4),
                "n": n,
                "wins": wins,
            }

        os.makedirs(os.path.dirname(BASE_RATES_FILE), exist_ok=True)
        with open(BASE_RATES_FILE, "w") as f:
            json.dump(result, f, indent=2)

        # 刷新缓存
        _cache["rates"] = None
        _cache["loaded_at"] = 0

        return result

    except Exception:
        return {}
