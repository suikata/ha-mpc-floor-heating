"""Time series helpers: forecasts, price lists, resampling (no HA imports)."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, tzinfo
import math
from typing import Any

import numpy as np

TIME_KEYS = ("start_time", "start", "startsAt", "starts_at", "from", "datetime", "time", "date")
VALUE_KEYS = (
    "price_per_kwh",
    "price",
    "total",
    "value",
    "price_ct_per_kwh",
    "marketprice",
    "market_price",
    "electricity_price",
)

Point = tuple[float, float]  # (unix timestamp, value)


def to_timestamp(
    value: Any,
    parse: Callable[[str], datetime | None] | None = None,
    default_tz: tzinfo | None = None,
) -> float | None:
    """Convert datetime, ISO string or epoch (s or ms) to a unix timestamp.

    Times without a time zone (some weather integrations send those) are read
    in `default_tz`, Home Assistant's time zone; without it they are refused.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            if default_tz is None:
                return None
            value = value.replace(tzinfo=default_tz)
        return value.timestamp()
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value) / 1000.0 if value > 1e11 else float(value)
    if isinstance(value, str):
        parsed = parse(value) if parse else None
        if parsed is None:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
        return to_timestamp(parsed, None, default_tz)
    return None


def to_float(value: Any) -> float | None:
    """Float or None."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def extract_price_points(
    attributes: dict[str, Any],
    parse: Callable[[str], datetime | None] | None = None,
    default_tz: tzinfo | None = None,
) -> list[Point]:
    """Find price lists in a sensor's attributes.

    Works with every attribute that is a list of dicts carrying a start time
    and a value, e.g. EPEX Spot (`data`: start_time/price_per_kwh),
    Nord Pool (`raw_today`/`raw_tomorrow`: start/value) or Tibber-like
    (`startsAt`/`total`).
    """
    points: dict[float, float] = {}
    for attr in attributes.values():
        if not isinstance(attr, list) or not attr or not isinstance(attr[0], dict):
            continue
        sample = attr[0]
        tkey = next((k for k in TIME_KEYS if k in sample), None)
        vkey = next((k for k in VALUE_KEYS if k in sample), None)
        if tkey is None or vkey is None:
            continue
        for item in attr:
            if not isinstance(item, dict):
                continue
            ts = to_timestamp(item.get(tkey), parse, default_tz)
            val = to_float(item.get(vkey))
            if ts is not None and val is not None:
                points[ts] = val
    return sorted(points.items())


def step_series(points: Iterable[Point], slot_starts: np.ndarray, slot_len: float = 3600.0) -> np.ndarray:
    """Value valid at the middle of each slot for a step function (prices).

    A point is valid from its timestamp until the next point. Slots before
    the first point or more than `slot_len` after the last one get NaN.
    """
    pts = sorted(points)
    out = np.full(len(slot_starts), np.nan)
    if not pts:
        return out
    times = np.array([p[0] for p in pts])
    vals = np.array([p[1] for p in pts])
    if len(times) > 1:
        last_len = float(np.median(np.diff(times)))
    else:
        last_len = slot_len
    mids = np.asarray(slot_starts, dtype=float) + slot_len / 2.0
    idx = np.searchsorted(times, mids, side="right") - 1
    for i, j in enumerate(idx):
        if j < 0:
            continue
        if j == len(times) - 1 and mids[i] > times[j] + last_len:
            continue
        out[i] = vals[j]
    return out


def outdoor_series(
    forecast: Iterable[Point],
    slot_starts: np.ndarray,
    now_ts: float,
    current: float | None,
    decay_hours: float = 6.0,
    slot_len: float = 3600.0,
) -> np.ndarray:
    """Outdoor temperature for each slot.

    Linear interpolation of the forecast at the slot middles, shifted by the
    difference between measured and forecast temperature right now. That
    correction fades out with `decay_hours`. Without a forecast the current
    value is held (persistence).
    """
    pts = sorted(forecast)
    mids = np.asarray(slot_starts, dtype=float) + slot_len / 2.0
    if len(pts) < 2:
        if current is None:
            raise ValueError("neither outdoor forecast nor outdoor measurement available")
        return np.full(len(mids), float(current))
    times = np.array([p[0] for p in pts])
    vals = np.array([p[1] for p in pts])
    series = np.interp(mids, times, vals)
    if current is not None:
        offset = float(current) - float(np.interp(now_ts, times, vals))
        hours_ahead = np.maximum(0.0, (mids - now_ts) / 3600.0)
        series = series + offset * np.exp(-hours_ahead / decay_hours)
    return series


def hourly_mean_from_points(
    points: Iterable[Point], start_ts: float, slots: int, slot_len: float = 3600.0
) -> np.ndarray:
    """Time weighted mean per slot from state changes (sample and hold).

    `points` must contain the state valid at start_ts (or earlier) first.
    NaN values mark unavailable periods and are excluded from the mean.
    """
    pts = sorted(points)
    out = np.full(slots, np.nan)
    if not pts:
        return out
    end_ts = start_ts + slots * slot_len
    acc = np.zeros(slots)
    weight = np.zeros(slots)
    for i, (ts, val) in enumerate(pts):
        seg_start = max(ts, start_ts)
        seg_end = pts[i + 1][0] if i + 1 < len(pts) else end_ts
        seg_end = min(seg_end, end_ts)
        if seg_end <= seg_start or not math.isfinite(val):
            continue
        first = int((seg_start - start_ts) // slot_len)
        last = int(math.ceil((seg_end - start_ts) / slot_len))
        for s in range(max(0, first), min(slots, last)):
            a = max(seg_start, start_ts + s * slot_len)
            b = min(seg_end, start_ts + (s + 1) * slot_len)
            if b > a:
                acc[s] += val * (b - a)
                weight[s] += b - a
    ok = weight > slot_len * 0.25  # need at least 15 min of data
    out[ok] = acc[ok] / weight[ok]
    return out


def window_mean(points: Iterable[Point], start_ts: float, end_ts: float) -> float | None:
    """Time weighted mean over one window from state changes (sample and hold).

    NaN marks unavailable periods and is excluded. None if less than a quarter
    of the window is covered by data.
    """
    if end_ts <= start_ts:
        return None
    value = float(hourly_mean_from_points(points, start_ts, 1, end_ts - start_ts)[0])
    return value if math.isfinite(value) else None


def mean_of_rows(rows: list[np.ndarray]) -> np.ndarray:
    """Mean over sensors per hour, ignoring NaNs (NaN if all are NaN)."""
    if not rows:
        return np.array([])
    stack = np.vstack(rows)
    count = np.sum(np.isfinite(stack), axis=0)
    total = np.nansum(stack, axis=0)
    out = np.full(stack.shape[1], np.nan)
    ok = count > 0
    out[ok] = total[ok] / count[ok]
    return out


def fill_edges(arr: np.ndarray, last_value: float | None = None) -> np.ndarray:
    """Fill NaNs: interior linearly, edges with nearest value.

    `last_value` (e.g. the current state) is used as an extra anchor one slot
    after the end, so a missing last hour is taken from the live value.
    """
    arr = np.asarray(arr, dtype=float).copy()
    n = len(arr)
    idx = np.arange(n, dtype=float)
    good = np.isfinite(arr)
    xs = list(idx[good])
    ys = list(arr[good])
    if last_value is not None and math.isfinite(last_value):
        xs.append(float(n))
        ys.append(float(last_value))
    if not xs:
        return arr
    return np.interp(idx, np.array(xs), np.array(ys))
