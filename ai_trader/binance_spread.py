"""Binance/Chainlink spread calibration helpers.

Polymarket 5m crypto markets settle from Chainlink prices, while Binance
trades often move first.  This module estimates the short-lived
Chainlink-Binance offset and applies it to Price-to-Beat before comparing the
latest Binance price.
"""
from __future__ import annotations

import os
import statistics
import time
from dataclasses import dataclass
from typing import Iterable, Optional


DEFAULT_ALIGN_WINDOW_SEC = 60.0
DEFAULT_ALIGN_MIN_SPAN_SEC = 10.0
DEFAULT_ALIGN_BUCKET_MS = 500
DEFAULT_MAD_FLOOR = 10.0
DEFAULT_MIN_DIFF_ATR = 0.30


@dataclass(frozen=True)
class _Point:
    ts: float
    price: float


@dataclass(frozen=True)
class _OffsetDetails:
    offset: Optional[float]
    method: str
    n_buckets: int = 0
    span_sec: float = 0.0
    reliable: bool = False


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def _coerce_timestamp(value) -> Optional[float]:
    if value is None:
        return None
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    if ts > 1e15:
        ts /= 1_000_000.0
    elif ts > 1e12:
        ts /= 1_000.0
    if ts <= 0:
        return None
    return ts


def _coerce_price(value) -> Optional[float]:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    return price


def _extract_point(raw) -> Optional[_Point]:
    if isinstance(raw, dict):
        ts = None
        for key in ("ts", "t", "time", "timestamp", "received_at", "last_update_ts"):
            ts = _coerce_timestamp(raw.get(key))
            if ts is not None:
                break
        price = None
        for key in ("price", "p", "value"):
            price = _coerce_price(raw.get(key))
            if price is not None:
                break
        if ts is not None and price is not None:
            return _Point(ts, price)
        return None

    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        ts = _coerce_timestamp(raw[0])
        price = _coerce_price(raw[1])
        if ts is not None and price is not None:
            return _Point(ts, price)
    return None


def _normalize_points(history: Iterable) -> list[_Point]:
    points = []
    if not history:
        return points
    for raw in history:
        point = _extract_point(raw)
        if point is not None:
            points.append(point)
    points.sort(key=lambda p: p.ts)
    return points


def _latest_price(points: list[_Point]) -> Optional[float]:
    return points[-1].price if points else None


def _trimmed_mean(values: list[float], trim_ratio: float = 0.15) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    trim = int(len(ordered) * trim_ratio)
    if trim > 0 and trim * 2 < len(ordered):
        ordered = ordered[trim:-trim]
    return sum(ordered) / len(ordered)


def _bucket_medians(points: list[_Point], start: float, end: float, bucket_ms: int) -> dict[int, float]:
    buckets: dict[int, list[float]] = {}
    bucket_sec = max(bucket_ms, 1) / 1000.0
    for point in points:
        if point.ts < start or point.ts > end:
            continue
        bucket = int((point.ts - start) / bucket_sec)
        buckets.setdefault(bucket, []).append(point.price)
    return {bucket: statistics.median(prices) for bucket, prices in buckets.items()}


def _calculate_binance_offset_details(
    binance_history,
    chainlink_history,
    *,
    now: Optional[float] = None,
    allow_latest_fallback: bool = False,
    window_sec: Optional[float] = None,
    min_span_sec: Optional[float] = None,
    bucket_ms: Optional[int] = None,
    mad_floor: Optional[float] = None,
) -> _OffsetDetails:
    window_sec = DEFAULT_ALIGN_WINDOW_SEC if window_sec is None else float(window_sec)
    min_span_sec = DEFAULT_ALIGN_MIN_SPAN_SEC if min_span_sec is None else float(min_span_sec)
    bucket_ms = DEFAULT_ALIGN_BUCKET_MS if bucket_ms is None else int(bucket_ms)
    mad_floor = DEFAULT_MAD_FLOOR if mad_floor is None else float(mad_floor)

    binance_points = _normalize_points(binance_history)
    chainlink_points = _normalize_points(chainlink_history)
    if not binance_points or not chainlink_points:
        return _latest_offset_details(binance_points, chainlink_points, allow_latest_fallback)

    if now is None:
        now = max(binance_points[-1].ts, chainlink_points[-1].ts, time.time())
    cutoff = now - window_sec
    binance_recent = [p for p in binance_points if cutoff <= p.ts <= now]
    chainlink_recent = [p for p in chainlink_points if cutoff <= p.ts <= now]
    if not binance_recent or not chainlink_recent:
        return _latest_offset_details(binance_points, chainlink_points, allow_latest_fallback)

    start = max(binance_recent[0].ts, chainlink_recent[0].ts, cutoff)
    end = min(binance_recent[-1].ts, chainlink_recent[-1].ts, now)
    span_sec = max(0.0, end - start)
    if span_sec < min_span_sec and not allow_latest_fallback:
        return _OffsetDetails(None, "span_too_short", span_sec=span_sec)

    binance_buckets = _bucket_medians(binance_recent, start, end, bucket_ms)
    chainlink_buckets = _bucket_medians(chainlink_recent, start, end, bucket_ms)
    diffs = [
        chainlink_buckets[bucket] - binance_buckets[bucket]
        for bucket in sorted(set(binance_buckets) & set(chainlink_buckets))
    ]
    if not diffs:
        return _latest_offset_details(binance_points, chainlink_points, allow_latest_fallback, span_sec)

    if len(diffs) < 5:
        offset = sum(diffs) / len(diffs)
        return _OffsetDetails(
            offset,
            "bucket_mean",
            n_buckets=len(diffs),
            span_sec=span_sec,
            reliable=span_sec >= min_span_sec,
        )

    center = statistics.median(diffs)
    deviations = [abs(value - center) for value in diffs]
    mad = statistics.median(deviations)
    threshold = max(mad_floor, mad * 3.0)
    filtered = [value for value in diffs if abs(value - center) <= threshold]
    usable = filtered if len(filtered) >= 3 else diffs
    offset = _trimmed_mean(usable)
    return _OffsetDetails(
        offset,
        "bucket_mad_trimmed_mean",
        n_buckets=len(diffs),
        span_sec=span_sec,
        reliable=span_sec >= min_span_sec,
    )


def _latest_offset_details(
    binance_points: list[_Point],
    chainlink_points: list[_Point],
    allow_latest_fallback: bool,
    span_sec: float = 0.0,
) -> _OffsetDetails:
    if not allow_latest_fallback:
        return _OffsetDetails(None, "no_overlap", span_sec=span_sec)
    binance_price = _latest_price(binance_points)
    chainlink_price = _latest_price(chainlink_points)
    if binance_price is None or chainlink_price is None:
        return _OffsetDetails(None, "missing_latest", span_sec=span_sec)
    return _OffsetDetails(chainlink_price - binance_price, "latest_fallback", span_sec=span_sec)


def calculate_binance_offset(
    binance_history,
    chainlink_history,
    *,
    now: Optional[float] = None,
    allow_latest_fallback: bool = False,
    window_sec: Optional[float] = None,
    min_span_sec: Optional[float] = None,
    bucket_ms: Optional[int] = None,
    mad_floor: Optional[float] = None,
) -> Optional[float]:
    """Estimate Chainlink minus Binance offset from recent aligned buckets."""
    details = _calculate_binance_offset_details(
        binance_history,
        chainlink_history,
        now=now,
        allow_latest_fallback=allow_latest_fallback,
        window_sec=window_sec,
        min_span_sec=min_span_sec,
        bucket_ms=bucket_ms,
        mad_floor=mad_floor,
    )
    return details.offset


def calculate_spread_snapshot(
    *,
    coin: str,
    ptb: float,
    atr_val: float,
    binance_price: float,
    offset: float,
    min_diff_atr: Optional[float] = None,
    chainlink_price: Optional[float] = None,
    offset_method: Optional[str] = None,
    offset_reliable: bool = False,
    offset_buckets: int = 0,
    offset_span_sec: float = 0.0,
) -> Optional[dict]:
    """Return Binance lead versus offset-adjusted Price-to-Beat."""
    ptb = _coerce_price(ptb)
    atr_val = _coerce_price(atr_val)
    binance_price = _coerce_price(binance_price)
    try:
        offset = float(offset)
    except (TypeError, ValueError):
        offset = None
    if ptb is None or atr_val is None or binance_price is None or offset is None:
        return None

    min_diff_atr = (
        _env_float("BINANCE_SPREAD_MIN_DIFF_ATR", DEFAULT_MIN_DIFF_ATR)
        if min_diff_atr is None
        else float(min_diff_atr)
    )
    adjusted_ptb = ptb - offset
    diff = binance_price - adjusted_ptb
    diff_atr_signed = diff / atr_val
    diff_atr = abs(diff_atr_signed)
    if diff > 0:
        direction = "UP"
    elif diff < 0:
        direction = "DOWN"
    else:
        direction = "FLAT"
    return {
        "coin": str(coin).upper(),
        "binance_price": binance_price,
        "chainlink_price": chainlink_price,
        "ptb": ptb,
        "offset": offset,
        "adjusted_ptb": adjusted_ptb,
        "diff": diff,
        "diff_atr": diff_atr,
        "diff_atr_signed": diff_atr_signed,
        "direction": direction,
        "supports_up": direction == "UP" and diff_atr >= min_diff_atr,
        "supports_down": direction == "DOWN" and diff_atr >= min_diff_atr,
        "min_diff_atr": min_diff_atr,
        "offset_method": offset_method,
        "offset_reliable": bool(offset_reliable),
        "offset_buckets": int(offset_buckets or 0),
        "offset_span_sec": float(offset_span_sec or 0.0),
    }


def get_spread_snapshot(
    coin: str,
    ptb: float,
    atr_val: float,
    *,
    allow_latest_fallback: Optional[bool] = None,
    min_diff_atr: Optional[float] = None,
) -> Optional[dict]:
    """Build a live spread snapshot from the global Binance and RTDS streams."""
    if os.environ.get("BINANCE_SPREAD_ENABLED", "1") != "1":
        return None

    coin = str(coin or "BTC").upper()
    allow_latest_fallback = (
        os.environ.get("BINANCE_SPREAD_ALLOW_LATEST_FALLBACK", "1") == "1"
        if allow_latest_fallback is None
        else bool(allow_latest_fallback)
    )
    window_sec = _env_float("BINANCE_SPREAD_WINDOW_SEC", DEFAULT_ALIGN_WINDOW_SEC)
    min_span_sec = _env_float("BINANCE_SPREAD_MIN_SPAN_SEC", DEFAULT_ALIGN_MIN_SPAN_SEC)
    bucket_ms = _env_int("BINANCE_SPREAD_BUCKET_MS", DEFAULT_ALIGN_BUCKET_MS)
    mad_floor = _env_float("BINANCE_SPREAD_MAD_FLOOR", DEFAULT_MAD_FLOOR)

    from ai_trader.binance_api import price_stream
    from ai_trader.polymarket_rtds import chainlink_stream

    binance_snapshot = price_stream.get_snapshot(coin)
    chainlink_snapshot = chainlink_stream.get_snapshot(coin)
    binance_price = price_stream.get_price(coin)
    chainlink_price = chainlink_stream.get_price(coin)
    if binance_price is None:
        return None
    if not isinstance(chainlink_snapshot, dict) or chainlink_snapshot.get("stale"):
        return None

    binance_history = _stream_history(price_stream, coin, window_sec)
    chainlink_history = _stream_history(chainlink_stream, coin, window_sec)
    details = _calculate_binance_offset_details(
        binance_history,
        chainlink_history,
        now=time.time(),
        allow_latest_fallback=allow_latest_fallback,
        window_sec=window_sec,
        min_span_sec=min_span_sec,
        bucket_ms=bucket_ms,
        mad_floor=mad_floor,
    )
    if details.offset is None and chainlink_price is not None and allow_latest_fallback:
        details = _OffsetDetails(chainlink_price - binance_price, "snapshot_fallback")
    if details.offset is None:
        return None

    snapshot = calculate_spread_snapshot(
        coin=coin,
        ptb=ptb,
        atr_val=atr_val,
        binance_price=binance_price,
        chainlink_price=chainlink_price,
        offset=details.offset,
        min_diff_atr=min_diff_atr,
        offset_method=details.method,
        offset_reliable=details.reliable,
        offset_buckets=details.n_buckets,
        offset_span_sec=details.span_sec,
    )
    if snapshot is None:
        return None
    if isinstance(binance_snapshot, dict):
        snapshot["binance_age_ms"] = binance_snapshot.get("age_ms")
    if isinstance(chainlink_snapshot, dict):
        snapshot["chainlink_age_ms"] = chainlink_snapshot.get("age_ms")
    return snapshot


def _stream_history(stream, coin: str, window_sec: float):
    getter = getattr(stream, "get_price_history", None)
    if getter is None:
        return []
    try:
        return getter(coin, window_sec=window_sec)
    except TypeError:
        return getter(coin)
    except Exception:
        return []
