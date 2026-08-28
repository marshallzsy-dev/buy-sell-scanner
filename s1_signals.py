"""
s1_signals.py
=============
Faithful Python port of the B / S markers from "S1 Formula v34" (S1.txt),
which is itself a formula-only port of s1lite/auxiliary_signals.py +
zigzag_signals.py.

Only the B (buy) and S (sell) markers are reproduced here — the IB / E /
continuity-number overlays are display-only and not needed for the scanner.

IMPORTANT — repainting:
    The original indicator intentionally repaints. It recomputes every marker
    from scratch over the whole stored window on the last bar. We do the same:
    feed the full OHLCV history and get back per-bar B/S flags for the current
    state of the data. A signal on a historical bar can therefore appear on one
    day's run and be gone the next — that is by design, and detecting exactly
    that is what the "warning / disappeared" panel in the scanner is for.

Public API:
    compute_signals(df) -> dict with:
        'dates'  : list[str]  (YYYY-MM-DD, oldest -> newest)
        'B'      : list[bool]  buy marker per bar
        'S'      : list[bool]  sell marker per bar
        'closes' : list[float]
        'b_dates': list[str]  dates where B is True
        's_dates': list[str]  dates where S is True
"""

from __future__ import annotations
import math

NAN = float("nan")


def _isnan(x) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


# ---------------------------------------------------------------------------
# array helpers (ports of f_mean / f_window_max / f_previous_max)
# ---------------------------------------------------------------------------
def f_mean(values, index, length, minimum):
    if index < 0:
        return NAN
    total = 0.0
    count = 0
    first = max(0, index - length + 1)
    for cursor in range(first, index + 1):
        v = values[cursor]
        if not _isnan(v):
            total += v
            count += 1
    return total / count if count >= minimum else NAN


def f_window_max(values, index, length, minimum):
    result = NAN
    count = 0
    first = max(0, index - length + 1)
    if index >= 0:
        for cursor in range(first, index + 1):
            v = values[cursor]
            if not _isnan(v):
                result = v if _isnan(result) else max(result, v)
                count += 1
    return result if count >= minimum else NAN


def f_previous_max(values, index, length, minimum):
    return f_window_max(values, index - 1, length, minimum) if index > 0 else NAN


def f_between(value, lower, upper) -> bool:
    return (not _isnan(value)) and value >= lower and value <= upper


# ---------------------------------------------------------------------------
# ATR (Wilder rma of true range), matches ta.rma(ta.tr(true), 14)
# ---------------------------------------------------------------------------
def _atr(highs, lows, closes, length=14):
    n = len(closes)
    tr = [NAN] * n
    for i in range(n):
        if i == 0:
            tr[i] = highs[i] - lows[i]
        else:
            pc = closes[i - 1]
            tr[i] = max(highs[i] - lows[i], abs(highs[i] - pc), abs(lows[i] - pc))
    atr = [NAN] * n
    if n >= length:
        seed = sum(tr[0:length]) / length
        atr[length - 1] = seed
        for i in range(length, n):
            atr[i] = (atr[i - 1] * (length - 1) + tr[i]) / length
    return atr


# ---------------------------------------------------------------------------
# zigzag candidate scan (port of f_set_candidate + f_zigzag_candidates)
# ---------------------------------------------------------------------------
def _set_candidate(active, extremes, confirmations, kinds, marker, extreme, confirmation, kind):
    if marker is not None and 0 <= marker < len(active):
        active[marker] = True
        extremes[marker] = extreme
        confirmations[marker] = confirmation
        kinds[marker] = kind


def _zigzag_candidates(closes, threshold, wanted_side, active, extremes, confirmations, kinds):
    count = len(closes)
    direction = 0
    low_index = 0
    high_index = 0
    candidate_b = None
    candidate_s = None
    if count > 1:
        for index in range(1, count):
            current = closes[index]
            if current < closes[low_index]:
                low_index = index
                candidate_b = None
            if current > closes[high_index]:
                high_index = index
                candidate_s = None

            if direction == 0:
                if index == low_index + 1 and candidate_b is None:
                    candidate_b = index
                if index == high_index + 1 and candidate_s is None:
                    candidate_s = index
                if current >= closes[low_index] * (1.0 + threshold):
                    if wanted_side == 1:
                        _set_candidate(active, extremes, confirmations, kinds,
                                       low_index + 1 if candidate_b is None else candidate_b,
                                       low_index, index, 1)
                    candidate_s = None
                    direction = 1
                    high_index = index
                elif current <= closes[high_index] * (1.0 - threshold):
                    if wanted_side == -1:
                        _set_candidate(active, extremes, confirmations, kinds,
                                       high_index + 1 if candidate_s is None else candidate_s,
                                       high_index, index, 1)
                    candidate_b = None
                    direction = -1
                    low_index = index
            elif direction == 1:
                if index == high_index + 1 and candidate_s is None:
                    candidate_s = index
                if current <= closes[high_index] * (1.0 - threshold):
                    if wanted_side == -1:
                        _set_candidate(active, extremes, confirmations, kinds,
                                       high_index + 1 if candidate_s is None else candidate_s,
                                       high_index, index, 1)
                    direction = -1
                    low_index = index
                    candidate_b = None
            else:  # direction == -1
                if index == low_index + 1 and candidate_b is None:
                    candidate_b = index
                if current >= closes[low_index] * (1.0 + threshold):
                    if wanted_side == 1:
                        _set_candidate(active, extremes, confirmations, kinds,
                                       low_index + 1 if candidate_b is None else candidate_b,
                                       low_index, index, 1)
                    direction = 1
                    high_index = index
                    candidate_s = None

        pending_marker = candidate_b if wanted_side == 1 else candidate_s
        pending_extreme = low_index if wanted_side == 1 else high_index
        if pending_marker is not None and not active[pending_marker]:
            _set_candidate(active, extremes, confirmations, kinds,
                           pending_marker, pending_extreme, count - 1, 2)


# ---------------------------------------------------------------------------
# reject filters (ports of f_reject_buy / f_reject_sell)
# ---------------------------------------------------------------------------
def _reject_buy(opens, highs, lows, closes, volumes, amounts, atrs, index) -> bool:
    ret1 = closes[index] / closes[index - 1] - 1.0 if index > 0 else NAN
    ret5 = closes[index] / closes[index - 5] - 1.0 if index >= 5 else NAN
    ret20 = closes[index] / closes[index - 20] - 1.0 if index >= 20 else NAN
    body_pct = closes[index] / opens[index] - 1.0
    candle_range = highs[index] - lows[index]
    close_pos = (closes[index] - lows[index]) / candle_range if candle_range > 0.0 else NAN
    atr_pct = atrs[index] / closes[index]
    vol_mean50 = f_mean(volumes, index, 50, 20)
    vol_ratio50 = volumes[index] / vol_mean50 if (not _isnan(vol_mean50) and vol_mean50 > 0.0) else NAN
    amt_mean20 = f_mean(amounts, index, 20, 10)
    amt_ratio20 = amounts[index] / amt_mean20 if (not _isnan(amt_mean20) and amt_mean20 > 0.0) else NAN
    prior_high20 = f_previous_max(highs, index, 20, 5)
    breakout20 = closes[index] / prior_high20 - 1.0 if (not _isnan(prior_high20) and prior_high20 > 0.0) else NAN

    def gt(a, b):
        return (not _isnan(a)) and a > b

    def ge(a, b):
        return (not _isnan(a)) and a >= b

    def le(a, b):
        return (not _isnan(a)) and a <= b

    def lt(a, b):
        return (not _isnan(a)) and a < b

    calm_bullish_rebound = le(atr_pct, 0.025) and f_between(ret1, 0.015, 0.03) and gt(body_pct, 0.0) and ge(close_pos, 0.60)
    strong_capitulation_rebound = ge(ret1, 0.04) and gt(body_pct, 0.0) and ge(close_pos, 0.40)
    red_jump = gt(ret1, 0.15) and lt(body_pct, 0.0)
    low_volatility_stall = le(atr_pct, 0.04) and gt(ret20, -0.02) and le(vol_ratio50, 2.59) and not calm_bullish_rebound
    capitulation_bounce = f_between(vol_ratio50, 2.59, 3.0) and gt(vol_ratio50, 2.59) and le(ret5, -0.13) and not strong_capitulation_rebound
    abnormal_low_breakout_volume = gt(amt_ratio20, 3.25) and gt(close_pos, 0.72) and lt(breakout20, -0.15)
    return red_jump or low_volatility_stall or capitulation_bounce or abnormal_low_breakout_volume


def _reject_sell(opens, highs, lows, closes, index) -> bool:
    ret1 = closes[index] / closes[index - 1] - 1.0 if index > 0 else NAN
    ret20 = closes[index] / closes[index - 20] - 1.0 if index >= 20 else NAN
    body_pct = closes[index] / opens[index] - 1.0
    candle_range = highs[index] - lows[index]
    range_pct = candle_range / closes[index]
    close_pos = (closes[index] - lows[index]) / candle_range if candle_range > 0.0 else NAN
    upper_wick_pct = (highs[index] - max(opens[index], closes[index])) / candle_range if candle_range > 0.0 else NAN

    def gt(a, b):
        return (not _isnan(a)) and a > b

    def lt(a, b):
        return (not _isnan(a)) and a < b

    def le(a, b):
        return (not _isnan(a)) and a <= b

    strong_momentum_peak = gt(ret20, 0.50)
    narrow_upper_rejection = f_between(ret1, -0.02, 0.0) and lt(ret1, 0.0) and le(range_pct, 0.03) and gt(upper_wick_pct, 0.41) and gt(close_pos, 0.20)
    small_red_green_body = gt(ret1, -0.005) and gt(body_pct, 0.015) and gt(close_pos, 0.60) and not strong_momentum_peak
    weak_red_large_green_body = gt(ret1, -0.02) and gt(body_pct, 0.04) and not strong_momentum_peak
    return narrow_upper_rejection or small_red_green_body or weak_red_large_green_body


def _apply_marker_filters(opens, highs, lows, closes, volumes, amounts, atrs, buys, sells):
    count = len(closes)
    for index in range(count):
        if buys[index] and _reject_buy(opens, highs, lows, closes, volumes, amounts, atrs, index):
            buys[index] = False
        if sells[index] and _reject_sell(opens, highs, lows, closes, index):
            sells[index] = False


# ---------------------------------------------------------------------------
# fallback buys / turning sells (ports of f_add_fallback_buys / f_add_turning_sells)
# ---------------------------------------------------------------------------
def _add_fallback_buys(opens, highs, lows, closes, volumes, amounts, atrs, active, extremes, confirmations, kinds):
    count = len(closes)
    first = max(1, count - 1 - 6)
    if count > 1 and first <= count - 1:
        for index in range(first, count):
            lookback_start = max(0, index - 8)
            prior_minimum = closes[lookback_start]
            for cursor in range(lookback_start, index):
                prior_minimum = min(prior_minimum, closes[cursor])
            prior_is_low = closes[index - 1] <= prior_minimum * 1.000001
            return1 = closes[index] / closes[index - 1] - 1.0
            enough_return = 0.05 <= return1 <= 0.152901
            green = closes[index] > opens[index]
            future_minimum = closes[index]
            for cursor in range(index, count):
                future_minimum = min(future_minimum, closes[cursor])
            survives = future_minimum / closes[index] - 1.0 > -0.173247
            if (prior_is_low and enough_return and green and survives
                    and not _reject_buy(opens, highs, lows, closes, volumes, amounts, atrs, index)
                    and not active[index]):
                _set_candidate(active, extremes, confirmations, kinds, index, index - 1, index, 3)


def _add_turning_sells(opens, highs, lows, closes, volumes, active, extremes, confirmations, kinds):
    count = len(closes)
    first = max(13, count - 1 - 10)
    if count >= 14 and first <= count - 1:
        for index in range(first, count):
            previous_close = closes[index - 1]
            prior_maximum = closes[index - 13]
            for cursor in range(index - 13, index):
                prior_maximum = max(prior_maximum, closes[cursor])
            prior_run = previous_close / closes[index - 9] - 1.0
            return1 = closes[index] / previous_close - 1.0
            candle_range = highs[index] - lows[index]
            close_pos = (closes[index] - lows[index]) / candle_range if candle_range > 0.0 else 1.0
            average_volume = f_mean(volumes, index - 1, 20, 1)
            volume_ratio = volumes[index] / average_volume if (not _isnan(average_volume) and average_volume > 0.0) else 0.0
            strong_turn = -0.05 <= return1 <= -0.01 and close_pos <= 0.2 and volume_ratio <= 1.2
            weak_turn = (-0.01 <= return1 <= -0.001 and 0.3 <= close_pos <= 0.5
                         and 1.0 <= volume_ratio <= 1.2)
            prior_local_low = closes[max(0, index - 8)]
            for cursor in range(max(0, index - 8), index):
                prior_local_low = min(prior_local_low, closes[cursor])
            prior_rebound = previous_close / prior_local_low - 1.0
            red_candle = closes[index] < opens[index]
            terminal_rebound_turn = (index == count - 1 and prior_rebound >= 0.10
                                     and -0.02 <= return1 < 0.0 and (close_pos <= 0.3 or red_candle))
            regular_turn = (previous_close >= prior_maximum * 0.999 and prior_run >= 0.01
                            and red_candle and (strong_turn or weak_turn))
            if (regular_turn or terminal_rebound_turn) and not active[index]:
                _set_candidate(active, extremes, confirmations, kinds, index, index - 1, index, 4)


# ---------------------------------------------------------------------------
# main entry
# ---------------------------------------------------------------------------
def compute_signals(df, amount_mode="HLC3 x volume"):
    """
    df: pandas DataFrame indexed by date with columns Open, High, Low, Close, Volume
        (oldest -> newest). Returns dict of per-bar B/S markers, mirroring the
        indicator's last-bar recomputation over the stored window.
    """
    opens = [float(x) for x in df["Open"].tolist()]
    highs = [float(x) for x in df["High"].tolist()]
    lows = [float(x) for x in df["Low"].tolist()]
    closes = [float(x) for x in df["Close"].tolist()]
    volumes = [float(x) for x in df["Volume"].tolist()]
    dates = [d.strftime("%Y-%m-%d") for d in df.index]

    n = len(closes)
    hlc3 = [(highs[i] + lows[i] + closes[i]) / 3.0 for i in range(n)]
    if amount_mode == "Close x volume":
        amounts = [closes[i] * volumes[i] for i in range(n)]
    else:  # default HLC3 x volume
        amounts = [hlc3[i] * volumes[i] for i in range(n)]
    atrs = _atr(highs, lows, closes, 14)

    stored = n
    raw_b = [False] * stored
    raw_s = [False] * stored
    b_extreme = [-1] * stored
    s_extreme = [-1] * stored
    b_confirmation = [-1] * stored
    s_confirmation = [-1] * stored
    b_kind = [0] * stored
    s_kind = [0] * stored

    _zigzag_candidates(closes, 0.08, 1, raw_b, b_extreme, b_confirmation, b_kind)
    _zigzag_candidates(closes, 0.10, -1, raw_s, s_extreme, s_confirmation, s_kind)

    for index in range(0, min(2, stored - 1) + 1):
        raw_b[index] = False
        raw_s[index] = False

    _apply_marker_filters(opens, highs, lows, closes, volumes, amounts, atrs, raw_b, raw_s)
    _add_fallback_buys(opens, highs, lows, closes, volumes, amounts, atrs, raw_b, b_extreme, b_confirmation, b_kind)
    _add_turning_sells(opens, highs, lows, closes, volumes, raw_s, s_extreme, s_confirmation, s_kind)

    # S locking: keep only the first S after each B; a B unlocks the next S
    locked_s = [False] * stored
    sell_locked = False
    for index in range(stored):
        if raw_b[index]:
            sell_locked = False
        if raw_s[index] and not sell_locked:
            locked_s[index] = True
            sell_locked = True

    final_b = raw_b
    final_s = locked_s

    b_dates = [dates[i] for i in range(stored) if final_b[i]]
    s_dates = [dates[i] for i in range(stored) if final_s[i]]

    return {
        "dates": dates,
        "closes": closes,
        "B": final_b,
        "S": final_s,
        "b_dates": b_dates,
        "s_dates": s_dates,
    }
