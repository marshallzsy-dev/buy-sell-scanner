"""
s1_signals.py
=============
Faithful Python port of the B / S markers from "S1 Formula v34" (S1.txt),
which is itself a formula-only port of s1lite/auxiliary_signals.py +
zigzag_signals.py.

The B (buy) / S (sell) markers are always computed.  With include_ibe=True
the IB (institutional accumulation) and E (abnormal-volume event) overlays
are computed as well — the continuity-number overlay is still excluded.

IMPORTANT — repainting:
    The original indicator intentionally repaints. It recomputes every marker
    from scratch over the whole stored window on the last bar. We do the same:
    feed the full OHLCV history and get back per-bar flags for the current
    state of the data. A signal on a historical bar can therefore appear on one
    day's run and be gone the next — that is by design, and detecting exactly
    that is what the "warning / disappeared" panel in the scanner is for.

    IB / E repaint even harder: their rules read NEXT-day data
    (nextAmountRatio / nextReturn) as confirmation, so a bar only earns its
    label after the following bar closes. They are hindsight study labels,
    never same-day triggers — several branches do tolerate na at the latest
    bar (Pine semantics), so last-bar labels exist and can vanish next run.

Public API:
    compute_signals(df, amount_mode="HLC3 x volume", include_ibe=False)
        'dates'   : list[str]  (YYYY-MM-DD, oldest -> newest)
        'B'       : list[bool]  buy marker per bar
        'S'       : list[bool]  sell marker per bar
        'closes'  : list[float]
        'b_dates' : list[str]  dates where B is True
        's_dates' : list[str]  dates where S is True
        include_ibe=True adds (and only adds):
        'IB'      : list[bool]  institutional-accumulation label per bar
        'E'       : list[bool]  abnormal-volume event label per bar
        'ib_dates': list[str]
        'e_dates' : list[str]

    The default keeps the heavy IB/E feature stack off the walk-forward
    profile/backtest paths (scan.py stats + analyze.py) — those call sites
    stay byte-for-byte identical in behaviour and cost.
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
# Pine na-semantics helpers (used by the IB / E overlays)
# ---------------------------------------------------------------------------
def f_get(values, index):
    """array read that returns NAN out of bounds (Pine f_get_float)."""
    return values[index] if 0 <= index < len(values) else NAN


def f_get_bool(values, index) -> bool:
    """bool array read that returns False out of bounds (Pine f_get_bool)."""
    return values[index] if 0 <= index < len(values) else False


def _gt(a, b) -> bool:
    # In Pine a comparison against na yields na, which is falsy in a boolean
    # context — NaN comparisons are already False in Python, but these
    # helpers keep the rule blocks reading like the Pine source.
    return (not _isnan(a)) and a > b


def _ge(a, b) -> bool:
    return (not _isnan(a)) and a >= b


def _le(a, b) -> bool:
    return (not _isnan(a)) and a <= b


def _lt(a, b) -> bool:
    return (not _isnan(a)) and a < b


def _div(a, b):
    """Pine division: x/0 -> na, and na poisons arithmetic."""
    if _isnan(a) or _isnan(b) or b == 0.0:
        return NAN
    return a / b


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
# series helpers for the IB / E overlays (ports of the S1.txt series section)
# ---------------------------------------------------------------------------
def _sma_series(values, length):
    """ta.sma: NaN until `length` bars, and NaN if any window member is NaN."""
    out = [NAN] * len(values)
    for i in range(length - 1, len(values)):
        window = values[i - length + 1:i + 1]
        ok = True
        total = 0.0
        for v in window:
            if _isnan(v):
                ok = False
                break
            total += v
        if ok:
            out[i] = total / length
    return out


def _stdev_series(values, length):
    """ta.stdev(..., biased=true): population stdev, same NaN rules as _sma."""
    out = [NAN] * len(values)
    for i in range(length - 1, len(values)):
        window = values[i - length + 1:i + 1]
        ok = True
        total = 0.0
        for v in window:
            if _isnan(v):
                ok = False
                break
            total += v
        if ok:
            mean = total / length
            out[i] = math.sqrt(sum((v - mean) ** 2 for v in window) / length)
    return out


def _zscore_series(values, length):
    """f_zscore: (v - mean) / stdev, NaN when stdev is NaN or 0."""
    means = _sma_series(values, length)
    stds = _stdev_series(values, length)
    return [_div(values[i] - means[i], stds[i]) for i in range(len(values))]


def _ema_series(values, length):
    """ta.ema: seeded with the first value (Pine), never NaN after bar 0."""
    alpha = 2.0 / (length + 1.0)
    out = [NAN] * len(values)
    prev = NAN
    for i, v in enumerate(values):
        prev = v if _isnan(prev) else alpha * v + (1.0 - alpha) * prev
        out[i] = prev
    return out


def _rsi_series(closes, length=14):
    """ta.rsi: Wilder rma of gains/losses, NaN for the first `length` bars.
    avgLoss == 0 mirrors Pine's x/0 -> na (flat series); avgLoss > 0 with
    avgGain == 0 gives 0."""
    n = len(closes)
    out = [NAN] * n
    if n <= length:
        return out
    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        diff = closes[i] - closes[i - 1]
        gains[i] = diff if diff > 0.0 else 0.0
        losses[i] = -diff if diff < 0.0 else 0.0
    avg_gain = sum(gains[1:length + 1]) / length
    avg_loss = sum(losses[1:length + 1]) / length
    for i in range(length, n):
        if i > length:
            avg_gain = (avg_gain * (length - 1) + gains[i]) / length
            avg_loss = (avg_loss * (length - 1) + losses[i]) / length
        if avg_loss == 0.0:
            out[i] = 100.0 if avg_gain > 0.0 else NAN
        else:
            out[i] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return out


def _percentrank_series(values, length):
    """ta.percentrank / 100: fraction of the PREVIOUS `length` values that
    were <= the current one. Pine's exact tie handling is ambiguous in the
    docs — if a TradingView cross-check shows systematic IB-bottom drift,
    this is the knob to flip first."""
    out = [NAN] * len(values)
    for i in range(length, len(values)):
        v = values[i]
        below = 0
        for cursor in range(i - length, i):
            if values[cursor] <= v:
                below += 1
        out[i] = below / length
    return out


def _td_counters(closes):
    """TD setup counts (S1.txt:345-366): close vs close[4], count 1..13,
    latch 'exhausted' (counter holds 0) until the condition breaks."""
    n = len(closes)
    td_ups = [0] * n
    td_downs = [0] * n
    up = down = 0
    up_exhausted = down_exhausted = False
    for i in range(n):
        if i >= 4:
            up_cond = closes[i] > closes[i - 4]
            down_cond = closes[i] < closes[i - 4]
        else:
            up_cond = down_cond = False
        if not up_cond:
            up = 0
            up_exhausted = False
        elif up_exhausted:
            up = 0
        else:
            up = min(up + 1, 13)
            up_exhausted = up >= 13
        if not down_cond:
            down = 0
            down_exhausted = False
        elif down_exhausted:
            down = 0
        else:
            down = min(down + 1, 13)
            down_exhausted = down >= 13
        td_ups[i] = up
        td_downs[i] = down
    return td_ups, td_downs


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
# IB / E feature stack (port of the S1.txt:313-490 series section)
# ---------------------------------------------------------------------------
def _compute_features(opens, highs, lows, closes, volumes, amounts, atrs):
    """All series the E / IB rule blocks read. The Pine original computes
    these over the full chart and stores the latest 1500 bars; here the ~500
    bars fed in ARE the window, so the first ~60-73 bars carry NaN (warm-up)
    — the one systematic place our output can differ from a long TradingView
    chart, which has full warm-up inside its stored window."""
    n = len(closes)
    f = {}

    f["ret1s"] = [(_div(closes[i], closes[i - 1]) - 1.0) if i >= 1 else NAN for i in range(n)]
    f["ret3s"] = [(_div(closes[i], closes[i - 3]) - 1.0) if i >= 3 else NAN for i in range(n)]
    f["ret6s"] = [(_div(closes[i], closes[i - 6]) - 1.0) if i >= 6 else NAN for i in range(n)]
    f["gaps"] = [(_div(opens[i], closes[i - 1]) - 1.0) if i >= 1 else NAN for i in range(n)]
    f["bodyPcts"] = [_div(closes[i] - opens[i], opens[i]) for i in range(n)]
    f["bodyAbsPcts"] = [abs(f["bodyPcts"][i]) for i in range(n)]
    ranges = [highs[i] - lows[i] for i in range(n)]
    # Pine floors candleRange at mintick, so a flat bar yields closePosition 0
    f["closePositions"] = [_div(closes[i] - lows[i], ranges[i]) if ranges[i] > 0.0 else 0.0 for i in range(n)]
    f["rangeValues"] = ranges

    vol_mean10 = _sma_series(volumes, 10)
    vol_mean20 = _sma_series(volumes, 20)
    amt_mean10 = _sma_series(amounts, 10)
    amt_mean20 = _sma_series(amounts, 20)
    amt_std20 = _stdev_series(amounts, 20)
    rng_mean5 = _sma_series(ranges, 5)
    rng_mean20 = _sma_series(ranges, 20)
    f["volumeRatios10"] = [_div(volumes[i], vol_mean10[i]) for i in range(n)]
    f["volumeRatios20"] = [_div(volumes[i], vol_mean20[i]) for i in range(n)]
    f["amountRatios10"] = [_div(amounts[i], amt_mean10[i]) for i in range(n)]
    f["amountRatios20"] = [_div(amounts[i], amt_mean20[i]) for i in range(n)]
    f["amountZs"] = [_div(amounts[i] - amt_mean20[i], amt_std20[i]) for i in range(n)]
    f["amountRanks60"] = _percentrank_series(amounts, 60)
    f["rangeRatios20"] = [_div(ranges[i], rng_mean20[i]) for i in range(n)]
    f["rangeRatios5To20"] = [_div(rng_mean5[i], rng_mean20[i]) for i in range(n)]

    # turnover-rate proxy (S1.txt default "Volume-ratio proxy"): vol/SMA20(vol)*100
    f["turnoverRates"] = []
    for i in range(n):
        r = _div(volumes[i], vol_mean20[i])
        f["turnoverRates"].append(r * 100.0 if not _isnan(r) else NAN)
    t_mean20 = _sma_series(f["turnoverRates"], 20)
    f["turnoverDeltas"] = [f["turnoverRates"][i] - f["turnoverRates"][i - 1] if i >= 1 else NAN for i in range(n)]
    f["turnoverRatios20"] = [_div(f["turnoverRates"][i], t_mean20[i]) for i in range(n)]
    f["turnoverZs"] = _zscore_series(f["turnoverRates"], 50)

    f["atrZs"] = _zscore_series([_div(atrs[i], closes[i]) for i in range(n)], 60)
    f["rsis"] = _rsi_series(closes, 14)
    f["breakout10s"] = [(_div(closes[i], max(highs[i - 10:i])) - 1.0) if i >= 10 else NAN for i in range(n)]
    f["breakout20s"] = [(_div(closes[i], max(highs[i - 20:i])) - 1.0) if i >= 20 else NAN for i in range(n)]

    ema20 = _ema_series(closes, 20)
    ema34 = _ema_series(closes, 34)
    ema55 = _ema_series(closes, 55)
    ema89 = _ema_series(closes, 89)
    ema144 = _ema_series(closes, 144)
    ema169 = _ema_series(closes, 169)
    f["ema20Slopes"] = [(_div(ema20[i], ema20[i - 10]) - 1.0) if i >= 10 else NAN for i in range(n)]
    f["ema34To89"] = [_div(ema34[i], ema89[i]) - 1.0 for i in range(n)]
    f["trendSlow"] = [(ema34[i] > ema55[i]) + (ema55[i] > ema89[i])
                      + (ema89[i] > ema144[i]) + (ema144[i] > ema169[i]) for i in range(n)]

    signed = [max(-1.0, min(1.0, f["closePositions"][i] * 2.0 - 1.0)) * amounts[i] for i in range(n)]
    for k in (3, 5, 10):
        num = _sma_series(signed, k)
        den = _sma_series(amounts, k)
        f[f"cmf{k}s"] = [_div(num[i], den[i]) for i in range(n)]

    f["tdUps"], f["tdDowns"] = _td_counters(closes)
    return f


# ---------------------------------------------------------------------------
# E = abnormal-volume event day (port of S1.txt:533-580)
# ---------------------------------------------------------------------------
def _compute_events(closes, amounts, volumes, f, count):
    raw_events = [False] * count
    for i in range(count):
        current_amount = amounts[i]
        previous_amount = f_get(amounts, i - 1)
        next_amount = f_get(amounts, i + 1)
        next_amount_ratio = _div(next_amount, current_amount)
        next_close = f_get(closes, i + 1)
        next_return = _div(next_close, closes[i]) - 1.0
        prior_turnover_max = f_previous_max(f["turnoverRates"], i, 5, 2)
        prior_amount_max = f_previous_max(amounts, i, 10, 5)
        prior_volume_mean5 = f_mean(volumes, i - 1, 5, 5)
        prior_amount_mean5 = f_mean(amounts, i - 1, 5, 5)
        cp = f["closePositions"][i]

        abnormal = (_ge(f["amountRatios10"][i], 2.80) or _ge(f["turnoverZs"][i], 4.0)
                    or _ge(f["amountZs"][i], 4.0))
        turnover_peak = (not _isnan(prior_turnover_max)
                         and _ge(f["turnoverRates"][i], prior_turnover_max))
        event_quality = (_ge(f["bodyAbsPcts"][i], 0.05) or _le(cp, 0.50)
                         or _ge(f["turnoverDeltas"][i], 3.0)
                         or (_le(cp, 0.55) and _le(f["rangeRatios5To20"][i], 1.10)))
        abnormal_event = (abnormal and turnover_peak and not _isnan(previous_amount)
                          and current_amount > previous_amount and event_quality)
        amount_shock = (not _isnan(previous_amount) and not _isnan(prior_amount_max)
                        and current_amount >= previous_amount * 3.0
                        and current_amount >= prior_amount_max
                        and (_isnan(next_amount_ratio) or _le(next_amount_ratio, 0.80)))
        primary_explosion = (not _isnan(prior_volume_mean5) and not _isnan(prior_amount_mean5)
                             and volumes[i] >= prior_volume_mean5 * 2.60
                             and current_amount >= prior_amount_mean5 * 1.10)
        low_volume = _le(f["volumeRatios10"][i], 2.0)
        volatility_event = (low_volume and _le(f["turnoverDeltas"][i], 6.8)
                            and _gt(f["atrZs"][i], 3.3)
                            and _le(f["volumeRatios20"][i], 1.50)
                            and _le(f["rangeRatios5To20"][i], 1.15)
                            and _ge(f["rangeRatios20"][i], 1.0)
                            and _le(next_return, 0.05))
        turnover_shock = (low_volume and _gt(f["turnoverDeltas"][i], 6.8)
                          and f["trendSlow"][i] <= 3
                          and _ge(f["ret6s"][i], 0.085))
        td_climax = (low_volume and _le(f["turnoverDeltas"][i], 6.8)
                     and _le(f["atrZs"][i], 3.3) and f["tdUps"][i] >= 13
                     and _ge(f["ret3s"][i], 0.19) and _ge(f["bodyAbsPcts"][i], 0.05))
        amount_increase = _div(current_amount, previous_amount)
        rebound_event = (f_between(amount_increase, 2.30, 2.60)
                         and f_between(f["amountRatios20"][i], 1.80, 2.20)
                         and _ge(f["amountZs"][i], 2.50) and _ge(cp, 0.30)
                         and (_lt(next_return, 0.0) or _isnan(next_return)
                              or (_ge(next_return, 0.05) and _ge(next_amount_ratio, 1.50))))
        confirmed_effort = (_ge(amount_increase, 2.0) and _le(next_amount_ratio, 0.80)
                            and _ge(f["bodyAbsPcts"][i], 0.05) and _le(cp, 0.55))
        confirmed_strict = (_ge(f["volumeRatios20"][i], 2.0) and _ge(f["amountRatios20"][i], 1.80))
        confirmed_relaxed = (_ge(f["volumeRatios20"][i], 1.80) and _ge(f["amountRatios20"][i], 1.50)
                             and _le(next_return, 0.0))
        confirmed_weak = confirmed_effort and (confirmed_strict or confirmed_relaxed)
        confirmed_gap = (_le(f["gaps"][i], -0.10) and _ge(abs(f["ret1s"][i]), 0.10)
                         and _ge(f["volumeRatios20"][i], 2.50) and _ge(f["amountRatios20"][i], 2.20)
                         and (_le(cp, 0.20) or _ge(cp, 0.90)))
        confirmed_absorption = (_le(f["breakout20s"][i], -0.20) and _ge(cp, 0.80)
                                and _ge(f["volumeRatios20"][i], 2.20)
                                and _ge(f["amountRatios20"][i], 2.0)
                                and _ge(amount_increase, 2.0) and _le(next_amount_ratio, 0.40))
        confirmed_explosion = confirmed_weak or confirmed_gap or confirmed_absorption
        # Pine math.max propagates na, so a NaN ret1 makes the whole max NaN
        if _isnan(f["ret1s"][i]):
            max_move = NAN
        else:
            max_move = max(abs(f["ret1s"][i]), f["bodyAbsPcts"][i])
        primary_price_confirmed = (_ge(max_move, 0.02) or _lt(next_return, 0.0)
                                   or _isnan(next_return))
        primary_event = ((abnormal_event or amount_shock) and primary_explosion
                         and primary_price_confirmed)
        raw_events[i] = (primary_event or confirmed_explosion or volatility_event
                         or turnover_shock or td_climax or rebound_event)

    # delayed-confirmation pass (S1.txt:569-580): carry an E forward when the
    # prior bar was an event, gapped up >=20% with a tiny body, and today
    # confirms with a >=10% move on sustained volume
    events = [False] * count
    for i in range(count):
        prev_idx = max(0, i - 1)
        delayed = (f_get_bool(raw_events, i - 1)
                   and _ge(f["gaps"][prev_idx], 0.20)
                   and _le(f["bodyAbsPcts"][prev_idx], 0.03)
                   and not _isnan(f_get(closes, i - 1))
                   and _div(closes[i], f_get(closes, i - 1)) - 1.0 >= 0.10
                   and not _isnan(f_get(amounts, i - 1))
                   and _div(amounts[i], f_get(amounts, i - 1)) >= 0.80)
        events[i] = raw_events[i] or delayed
    return events


# ---------------------------------------------------------------------------
# IB = institutional accumulation day (port of S1.txt:581-649)
# ---------------------------------------------------------------------------
def _compute_institutional(closes, amounts, f, events, count):
    quiet_candidates = [False] * count
    for i in range(count):
        turnover_max = f_window_max(f["turnoverRatios20"], i, 5, 1)
        quiet_candidates[i] = (f_between(f["ret1s"][i], 0.02, 0.04)
                               and _ge(f["breakout20s"][i], 0.02)
                               and not _isnan(turnover_max)
                               and _ge(f["turnoverRatios20"][i], turnover_max))

    institutional = [False] * count
    for i in range(count):
        current_amount = amounts[i]
        previous_amount = f_get(amounts, i - 1)
        previous_amount2 = f_get(amounts, i - 2)
        next_amount = f_get(amounts, i + 1)
        current_close = closes[i]
        next_close = f_get(closes, i + 1)
        next_return = _div(next_close, current_close) - 1.0
        next_amount_ratio = _div(next_amount, current_amount)
        has_next = i + 1 < count

        distribution = has_next and next_amount_ratio > 1.0 and _lt(next_return, 0.0)
        severe_reversal = has_next and _ge(next_amount_ratio, 0.90) and _le(next_return, -0.03)
        weak_no_follow = has_next and _lt(f["ret1s"][i], 0.10) and _lt(f["amountRatios20"][i], 3.50) and _le(next_return, 0.0)
        continuing_gap = has_next and _ge(f["gaps"][i], 0.15) and _gt(next_amount_ratio, 1.0) and _gt(next_return, 0.0)
        extreme_no_effort = _gt(f["closePositions"][i], 0.94) and _lt(f["bodyAbsPcts"][i], 0.08) and _lt(f["amountRatios20"][i], 2.50)
        climactic_failure = _ge(f["bodyPcts"][i], 0.10) and _le(next_return, -0.05)
        structural_rejected = (distribution or severe_reversal or weak_no_follow
                               or continuing_gap or extreme_no_effort or climactic_failure)
        continues_strong_next = (has_next and _ge(next_return, 0.08) and _gt(next_amount_ratio, 1.0)
                                 and _ge(f_get(f["bodyPcts"], i + 1), 0.05)
                                 and _ge(f_get(f["closePositions"], i + 1), 0.85))
        structural = (has_next and _ge(f["ret1s"][i], 0.06) and _ge(f["bodyPcts"][i], 0.03)
                      and _ge(f["closePositions"][i], 0.70) and _ge(f["breakout20s"][i], -0.01)
                      and _ge(f["volumeRatios20"][i], 1.50) and _ge(f["amountRatios20"][i], 2.0)
                      and not structural_rejected and not continues_strong_next)
        amount_maximum = f_window_max(f["amountRatios20"], i, 20, 1)
        peak_confirmed = has_next and _ge(f["amountRatios20"][i], f_get(f["amountRatios20"], i + 1))
        peak = (_ge(f["ret1s"][i], 0.05) and _ge(f["closePositions"][i], 0.85)
                and _ge(f["amountRatios20"][i], 1.60) and _ge(f["amountRatios20"][i], amount_maximum)
                and peak_confirmed and not structural_rejected)
        continues_next = has_next and next_amount > current_amount and next_close > current_close
        delayed_quiet = (i > 0 and quiet_candidates[i - 1] and current_amount > previous_amount
                         and current_close > closes[i - 1])
        quiet = (quiet_candidates[i] and not continues_next) or delayed_quiet
        amount_rising = i >= 2 and current_amount > max(previous_amount, previous_amount2)
        persistent = (_ge(f["ret1s"][i], 0.03) and amount_rising
                      and _ge(f["amountRatios20"][i], 1.50) and _ge(f["cmf10s"][i], 0.50))
        bottom = (_le(f["breakout10s"][i], -0.0019) and _le(f["ema34To89"][i], -0.05)
                  and f_between(f["bodyPcts"][i], 0.0125, 0.02)
                  and _gt(f["amountRanks60"][i], 0.575) and _le(f["amountRanks60"][i], 0.71)
                  and has_next and _ge(next_return, -0.03))
        cmf3_delta = f["cmf3s"][i] - f_get(f["cmf3s"], i - 1)
        cmf5_delta = f["cmf5s"][i] - f_get(f["cmf5s"], i - 1)
        amount_increase = _div(current_amount, previous_amount)
        rr20 = f["rangeRatios20"][i]
        effort = _div(f["amountRatios20"][i], rr20) if (not _isnan(rr20) and rr20 != 0.0) else NAN
        failed_flow = has_next and _le(next_return, -0.03) and _le(next_amount_ratio, 0.80)
        flow = (f_between(amount_increase, 1.05, 1.70) and _ge(cmf3_delta, 0.20)
                and _ge(cmf5_delta, 0.10) and _ge(f["breakout20s"][i], -0.01)
                and (not has_next or _le(next_amount_ratio, 1.10))
                and f_between(f["ret1s"][i], 0.02, 0.07) and _le(f["rsis"][i], 75.0)
                and _ge(effort, 0.60) and not failed_flow)
        long_history = not _isnan(f["amountRatios20"][i])
        short_amount_mean = f_mean(amounts, i, 20, 3)
        short_range_mean = f_mean(f["rangeValues"], i, 20, 3)
        absorption_amount_ratio = (f["amountRatios20"][i] if long_history
                                   else _div(current_amount, short_amount_mean))
        absorption_range_ratio = (f["rangeRatios20"][i] if not _isnan(f["rangeRatios20"][i])
                                  else _div(f["rangeValues"][i], short_range_mean))
        narrow_range = _le(absorption_range_ratio, 1.0)
        short_gap = (not long_history) and _ge(f["gaps"][i], 0.03)
        failed_absorption = (_le(next_return, -0.05) and _le(absorption_range_ratio, 1.0)
                             and _le(f["breakout10s"][i], 0.0))
        standard_absorption = (f_between(f["ret1s"][i], 0.03, 0.09)
                               and f_between(f["bodyPcts"][i], 0.03, 0.10)
                               and f_between(f["closePositions"][i], 0.55, 0.80)
                               and f_between(absorption_amount_ratio, 1.0, 1.5)
                               and (long_history or short_gap)
                               and (_le(cmf3_delta, 0.0) or (_ge(f["gaps"][i], 0.03) and narrow_range))
                               and has_next and _lt(next_return, 0.0)
                               and (narrow_range or _ge(cmf5_delta, 0.0))
                               and not failed_absorption)
        breakout_absorption = (f_between(f["ret1s"][i], 0.09, 0.10)
                               and f_between(f["bodyPcts"][i], 0.03, 0.10)
                               and f_between(f["closePositions"][i], 0.80, 0.82)
                               and f_between(absorption_amount_ratio, 1.0, 1.5)
                               and _ge(f["breakout10s"][i], 0.05)
                               and _ge(absorption_range_ratio, 1.50)
                               and _le(next_return, -0.05))
        absorption = standard_absorption or breakout_absorption
        strong_momentum = (has_next and _ge(f["ret1s"][i], 0.18) and _ge(f["bodyPcts"][i], 0.10)
                           and _ge(f["closePositions"][i], 0.90) and _ge(f["breakout20s"][i], 0.05)
                           and _ge(f["volumeRatios20"][i], 1.30) and _ge(f["amountRatios20"][i], 1.90)
                           and _ge(next_return, 0.0))
        recovery_momentum = (has_next and f_between(f["ret1s"][i], 0.08, 0.12)
                             and _ge(f["ret3s"][i], 0.20) and _ge(f["bodyPcts"][i], 0.10)
                             and _ge(f["closePositions"][i], 0.60) and _le(f["breakout20s"][i], -0.10)
                             and _ge(f["volumeRatios20"][i], 1.30) and _ge(f["amountRatios20"][i], 1.30)
                             and _le(f["ema20Slopes"][i], 0.0) and _ge(next_return, 0.0))
        run_start_close = f_get(closes, i - 3)
        positive_run = (i >= 2 and _gt(f["ret1s"][i], 0.0) and _gt(f_get(f["ret1s"], i - 1), 0.0)
                        and _gt(f_get(f["ret1s"], i - 2), 0.0))
        quiet_run = (positive_run and not _isnan(run_start_close)
                     and _ge(_div(current_close, run_start_close) - 1.0, 0.04)
                     and _le(f["ret1s"][i], 0.015) and _le(f["bodyAbsPcts"][i], 0.01)
                     and _ge(f["closePositions"][i], 0.60) and _le(f["breakout20s"][i], 0.01)
                     and _le(f["volumeRatios20"][i], 1.20) and _ge(f["cmf10s"][i], 0.25)
                     and has_next and _lt(next_return, 0.0))
        previous_return = f_get(f["ret1s"], i - 1)
        two_day_peak = (_ge(previous_return, 0.08) and f_between(f["ret1s"][i], 0.08, 0.12)
                        and _ge(f["bodyPcts"][i], 0.05) and _ge(f["closePositions"][i], 0.85)
                        and _ge(f["breakout20s"][i], 0.05) and _ge(f["volumeRatios20"][i], 2.0)
                        and _ge(f["amountRatios20"][i], 2.50) and _le(next_amount_ratio, 0.80)
                        and _ge(next_return, -0.03))
        non_exhausted = (_le(f["closePositions"][i], 0.991)
                         and (_isnan(f["ema20Slopes"][i]) or _le(f["ema20Slopes"][i], 0.21))
                         and (_isnan(f["amountZs"][i]) or _le(f["amountZs"][i], 3.80)))
        strong_event_structure = (structural and _ge(f["ret1s"][i], 0.15)
                                  and _ge(f["bodyPcts"][i], 0.10))
        base_institutional = (((((structural or peak or quiet) and _ge(f["cmf10s"][i], 0.10))
                                or persistent or bottom or flow or absorption
                                or strong_momentum or recovery_momentum
                                or two_day_peak or quiet_run)
                               and amount_rising and non_exhausted)
                              or (strong_momentum and amount_rising)
                              or (recovery_momentum and amount_rising)
                              or (two_day_peak and amount_rising and non_exhausted)
                              or (strong_event_structure and amount_rising and non_exhausted))
        other_confirmation = (peak or quiet or persistent or bottom or flow or absorption
                              or strong_momentum or recovery_momentum or two_day_peak or quiet_run)
        weak_event_breakout = (events[i] and structural
                               and not (other_confirmation or strong_event_structure)
                               and _lt(f["cmf10s"][i], 0.25))
        institutional[i] = base_institutional and not weak_event_breakout
    return institutional


# ---------------------------------------------------------------------------
# main entry
# ---------------------------------------------------------------------------
def compute_signals(df, amount_mode="HLC3 x volume", include_ibe=False):
    """
    df: pandas DataFrame indexed by date with columns Open, High, Low, Close, Volume
        (oldest -> newest). Returns dict of per-bar B/S markers, mirroring the
        indicator's last-bar recomputation over the stored window.

    include_ibe=True additionally computes the IB / E overlays and adds the
    "IB", "E", "ib_dates", "e_dates" keys. The B/S path is untouched by that
    computation, so b_dates / s_dates are identical either way.
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

    result = {
        "dates": dates,
        "closes": closes,
        "B": final_b,
        "S": final_s,
        "b_dates": b_dates,
        "s_dates": s_dates,
    }

    if include_ibe:
        feats = _compute_features(opens, highs, lows, closes, volumes, amounts, atrs)
        events = _compute_events(closes, amounts, volumes, feats, stored)
        institutional = _compute_institutional(closes, amounts, feats, events, stored)
        result["IB"] = institutional
        result["E"] = events
        result["ib_dates"] = [dates[i] for i in range(stored) if institutional[i]]
        result["e_dates"] = [dates[i] for i in range(stored) if events[i]]

    return result
