"""Technical analysis helpers for PocketOption signals.

Upgrade notes
─────────────
* Wilder-smoothed RSI and ADX instead of simple-average approximations.
* Supertrend (ATR-based) added as a primary trend indicator.
* Heikin-Ashi direction smooths out noise candle-by-candle.
* Price structure (HH/HL vs LH/LL) detects confirmed trend direction.
* RSI divergence (bullish / bearish) catches early reversals.
* MACD crossover detection replaces simple histogram-sign test.
* EMA-50 added as a long-term trend filter.
* Extended candle patterns: Morning Star, Evening Star,
  Three White Soldiers, Three Black Crows, Tweezer Top/Bottom.
* Categorical confluence scoring:
    TREND     – EMA, Supertrend, HA, ADX, MACD crossover, price structure
    OSCILLATOR – RSI, Stochastic, Williams %R, CCI, momentum, divergence
    PRICE_ACT  – patterns, Bollinger, S/R proximity
  Signal is only emitted when ≥ 2 of 3 categories agree; this prevents a
  single strong indicator from overriding a conflicted market.
* Volatility filter: very low ATR suppresses confidence.
* Walk-forward validation kept as optional calibration layer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from pocketoptionapi_async.models import Candle


# ──────────────────────────────────────────────────────────────────────────────
# Data structure
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class SignalResult:
    direction: str          # "CALL", "PUT", or "WAIT"
    confidence: float       # 0.0 – 1.0
    price: float
    reasons: List[str]
    indicators: Dict[str, Any]


# ──────────────────────────────────────────────────────────────────────────────
# Low-level maths helpers
# ──────────────────────────────────────────────────────────────────────────────

def _sma(values: List[float], period: int) -> float:
    window = values[-period:]
    return sum(window) / len(window) if window else 0.0


def _ema(values: List[float], period: int) -> List[float]:
    """Standard EMA – returns the full series aligned to *values*."""
    if len(values) < period:
        return values[:]
    k = 2.0 / (period + 1.0)
    result = [_sma(values[:period], period)]
    for v in values[period:]:
        result.append(v * k + result[-1] * (1.0 - k))
    return result


def _wilder_smooth(values: List[float], period: int) -> List[float]:
    """Wilder's smoothing (used by RSI and ADX)."""
    if len(values) < period:
        return [0.0] * len(values)
    seed = sum(values[:period]) / period
    result = [0.0] * (period - 1) + [seed]
    k = 1.0 / period
    for v in values[period:]:
        result.append(result[-1] * (1.0 - k) + v * k)
    return result


def _rsi(closes: List[float], period: int = 14) -> float:
    """RSI via Wilder's smoothing (the standard definition)."""
    if len(closes) < period + 1:
        return 50.0
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(c, 0.0) for c in changes]
    losses = [max(-c, 0.0) for c in changes]
    s_gains = _wilder_smooth(gains, period)
    s_losses = _wilder_smooth(losses, period)
    ag, al = s_gains[-1], s_losses[-1]
    if al == 0.0:
        return 100.0
    rs = ag / al
    return 100.0 - 100.0 / (1.0 + rs)


def _rsi_series(closes: List[float], period: int = 14) -> List[float]:
    """Full RSI series (needed for divergence detection)."""
    if len(closes) < period + 1:
        return [50.0] * len(closes)
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(c, 0.0) for c in changes]
    losses = [max(-c, 0.0) for c in changes]
    s_gains = _wilder_smooth(gains, period)
    s_losses = _wilder_smooth(losses, period)
    rsi = []
    for ag, al in zip(s_gains, s_losses):
        if al == 0.0:
            rsi.append(100.0)
        else:
            rsi.append(100.0 - 100.0 / (1.0 + ag / al))
    # Pad the front to match closes length (one fewer change)
    return [50.0] + rsi


def _macd(closes: List[float], fast: int = 12, slow: int = 26,
          signal: int = 9) -> Dict[str, float]:
    ef = _ema(closes, fast)
    es = _ema(closes, slow)
    macd_line = [f - s for f, s in zip(ef[-len(es):], es)]
    if len(macd_line) < signal:
        return {"macd": 0.0, "signal": 0.0, "histogram": 0.0,
                "crossover": 0, "prev_histogram": 0.0}
    sig_line = _ema(macd_line, signal)
    histogram = macd_line[-1] - sig_line[-1]
    prev_histogram = (macd_line[-2] - sig_line[-2]) if len(macd_line) >= 2 and len(sig_line) >= 2 else 0.0
    # Crossover: +1 bullish (MACD crossed above signal), -1 bearish
    crossover = 0
    if len(macd_line) >= 2 and len(sig_line) >= 2:
        if macd_line[-2] <= sig_line[-2] and macd_line[-1] > sig_line[-1]:
            crossover = 1
        elif macd_line[-2] >= sig_line[-2] and macd_line[-1] < sig_line[-1]:
            crossover = -1
    return {
        "macd": macd_line[-1],
        "signal": sig_line[-1],
        "histogram": histogram,
        "prev_histogram": prev_histogram,
        "crossover": crossover,
    }


def _bollinger(closes: List[float], period: int = 20,
               std_dev: float = 2.0) -> Dict[str, float]:
    if len(closes) < period:
        return {"upper": closes[-1], "middle": closes[-1], "lower": closes[-1],
                "bandwidth": 0.0, "percent_b": 0.5}
    window = closes[-period:]
    middle = sum(window) / period
    variance = sum((x - middle) ** 2 for x in window) / period
    band = math.sqrt(variance) * std_dev
    upper, lower = middle + band, middle - band
    width = upper - lower
    percent_b = (closes[-1] - lower) / width if width > 1e-12 else 0.5
    return {"upper": upper, "middle": middle, "lower": lower,
            "bandwidth": width, "percent_b": percent_b}


def _atr(highs: List[float], lows: List[float], closes: List[float],
         period: int = 14) -> float:
    if len(closes) < 2:
        return 0.0
    trs = [
        max(highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]))
        for i in range(1, len(closes))
    ]
    smoothed = _wilder_smooth(trs, period)
    return smoothed[-1] if smoothed else 0.0


def _stochastic(highs: List[float], lows: List[float], closes: List[float],
                k_period: int = 14, d_period: int = 3) -> Dict[str, float]:
    if len(closes) < k_period:
        return {"k": 50.0, "d": 50.0}
    k_values: List[float] = []
    for end in range(k_period, len(closes) + 1):
        h = max(highs[end - k_period:end])
        l = min(lows[end - k_period:end])
        k_values.append(50.0 if h == l else 100.0 * (closes[end - 1] - l) / (h - l))
    d = _sma(k_values, d_period)
    return {"k": k_values[-1], "d": d}


def _williams_r(highs: List[float], lows: List[float], closes: List[float],
                period: int = 14) -> float:
    if len(closes) < period:
        return -50.0
    h = max(highs[-period:])
    l = min(lows[-period:])
    return -50.0 if h == l else -100.0 * (h - closes[-1]) / (h - l)


def _cci(highs: List[float], lows: List[float], closes: List[float],
         period: int = 20) -> float:
    if len(closes) < period:
        return 0.0
    typical = [(h + l + c) / 3.0 for h, l, c in zip(highs, lows, closes)]
    window = typical[-period:]
    mean = sum(window) / period
    deviation = sum(abs(v - mean) for v in window) / period
    return 0.0 if deviation == 0 else (window[-1] - mean) / (0.015 * deviation)


def _adx_wilder(highs: List[float], lows: List[float], closes: List[float],
                period: int = 14) -> Dict[str, float]:
    """ADX with proper Wilder smoothing."""
    if len(closes) < period + 2:
        return {"adx": 0.0, "plus_di": 0.0, "minus_di": 0.0}
    trs, plus_dm, minus_dm = [], [], []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        plus_dm.append(up if up > dn and up > 0 else 0.0)
        minus_dm.append(dn if dn > up and dn > 0 else 0.0)

    str14 = _wilder_smooth(trs, period)
    pdi14 = _wilder_smooth(plus_dm, period)
    mdi14 = _wilder_smooth(minus_dm, period)
    if not str14:
        return {"adx": 0.0, "plus_di": 0.0, "minus_di": 0.0}
    atr14 = str14[-1]
    plus_di = 100.0 * pdi14[-1] / atr14 if atr14 else 0.0
    minus_di = 100.0 * mdi14[-1] / atr14 if atr14 else 0.0
    dx_vals = []
    for a, b, c in zip(str14, pdi14, mdi14):
        if a == 0:
            continue
        pdi = 100.0 * b / a
        mdi = 100.0 * c / a
        denom = pdi + mdi
        dx_vals.append(100.0 * abs(pdi - mdi) / denom if denom else 0.0)
    adx_series = _wilder_smooth(dx_vals, period)
    return {
        "adx": adx_series[-1] if adx_series else 0.0,
        "plus_di": plus_di,
        "minus_di": minus_di,
    }


def _supertrend(highs: List[float], lows: List[float], closes: List[float],
                period: int = 10, multiplier: float = 3.0) -> Dict[str, Any]:
    """Supertrend indicator – returns current direction and value."""
    if len(closes) < period + 1:
        return {"direction": 0, "value": closes[-1]}

    hl2 = [(h + l) / 2.0 for h, l in zip(highs, lows)]
    atr_series: List[float] = []
    for i in range(1, len(closes)):
        atr_series.append(max(highs[i] - lows[i],
                              abs(highs[i] - closes[i - 1]),
                              abs(lows[i] - closes[i - 1])))
    smoothed_atr = _wilder_smooth(atr_series, period)
    # Align ATR series with closes (one shorter, shift by 1)
    atr_aligned = [atr_series[0]] + smoothed_atr  # approximate head

    upper_raw = [hl2[i] + multiplier * atr_aligned[i] for i in range(len(closes))]
    lower_raw = [hl2[i] - multiplier * atr_aligned[i] for i in range(len(closes))]

    upper = list(upper_raw)
    lower = list(lower_raw)
    direction = [1] * len(closes)  # 1 = bullish (below price), -1 = bearish

    for i in range(1, len(closes)):
        upper[i] = min(upper_raw[i], upper[i - 1]) if closes[i - 1] <= upper[i - 1] else upper_raw[i]
        lower[i] = max(lower_raw[i], lower[i - 1]) if closes[i - 1] >= lower[i - 1] else lower_raw[i]
        if direction[i - 1] == -1 and closes[i] > upper[i - 1]:
            direction[i] = 1
        elif direction[i - 1] == 1 and closes[i] < lower[i - 1]:
            direction[i] = -1
        else:
            direction[i] = direction[i - 1]

    st_value = lower[-1] if direction[-1] == 1 else upper[-1]
    return {"direction": direction[-1], "value": st_value}


def _heikin_ashi_direction(candles: List[Candle]) -> int:
    """Return +1 (bullish HA), -1 (bearish HA), 0 (mixed) over last 3 HA candles."""
    if len(candles) < 4:
        return 0
    ha_open = (candles[0].open + candles[0].close) / 2.0
    ha_opens = [ha_open]
    ha_closes = [(c.open + c.high + c.low + c.close) / 4.0 for c in candles]
    for i in range(1, len(candles)):
        ha_opens.append((ha_opens[-1] + ha_closes[i - 1]) / 2.0)
    # Direction of last 3 HA candles
    last = [(ha_closes[i] > ha_opens[i]) for i in range(-3, 0)]
    if all(last):
        return 1
    if not any(last):
        return -1
    return 0


def _price_structure(highs: List[float], lows: List[float],
                     window: int = 20) -> int:
    """Return +1 for HH+HL (uptrend), -1 for LH+LL (downtrend), 0 for mixed.

    Splits the window into two halves and compares swing highs/lows.
    """
    if len(highs) < window:
        return 0
    h = highs[-window:]
    l = lows[-window:]
    mid = window // 2
    prev_high, curr_high = max(h[:mid]), max(h[mid:])
    prev_low, curr_low = min(l[:mid]), min(l[mid:])
    hh = curr_high > prev_high
    hl = curr_low > prev_low
    lh = curr_high < prev_high
    ll = curr_low < prev_low
    if hh and hl:
        return 1
    if lh and ll:
        return -1
    return 0


def _rsi_divergence(closes: List[float], rsi: List[float],
                    lookback: int = 14) -> int:
    """Detect RSI divergence over the last *lookback* bars.

    Returns +1 for bullish divergence (price lower low, RSI higher low),
    -1 for bearish divergence (price higher high, RSI lower high), 0 for none.
    """
    if len(closes) < lookback + 1 or len(rsi) < lookback + 1:
        return 0
    p = closes[-lookback:]
    r = rsi[-lookback:]
    # Find the extremes excluding the most recent bar
    price_lo_idx = p[:-1].index(min(p[:-1]))
    price_hi_idx = p[:-1].index(max(p[:-1]))
    # Bullish: price makes a new low but RSI does not
    if p[-1] < p[price_lo_idx] and r[-1] > r[price_lo_idx]:
        return 1
    # Bearish: price makes a new high but RSI does not
    if p[-1] > p[price_hi_idx] and r[-1] < r[price_hi_idx]:
        return -1
    return 0


def _momentum(closes: List[float]) -> Dict[str, float]:
    if len(closes) < 10:
        return {"short": 0.0, "medium": 0.0, "agreement": 0.0}
    base = max(abs(closes[-1]), 1e-12)
    short = (closes[-1] - closes[-4]) / base
    medium = (closes[-1] - closes[-10]) / base
    agreement = 1.0 if short * medium > 0 else 0.0
    return {"short": short, "medium": medium, "agreement": agreement}


# ──────────────────────────────────────────────────────────────────────────────
# Candle pattern detection
# ──────────────────────────────────────────────────────────────────────────────

def _candle_patterns(candles: List[Candle]) -> List[str]:
    """Detect multi-candle and single-candle patterns on the most recent bar."""
    if len(candles) < 3:
        return []

    c0, c1, c2 = candles[-3], candles[-2], candles[-1]
    body2 = abs(c2.close - c2.open)
    rng2 = max(c2.high - c2.low, 1e-12)
    upper2 = c2.high - max(c2.open, c2.close)
    lower2 = min(c2.open, c2.close) - c2.low
    body1 = c1.close - c1.open  # signed
    body0 = c0.close - c0.open

    patterns: List[str] = []

    # ── Single-candle ──────────────────────────────────────────────────────
    if body2 / rng2 <= 0.10:
        patterns.append("Doji")
    if lower2 >= body2 * 2.0 and upper2 <= max(body2, rng2 * 0.20):
        patterns.append("Hammer")
    if upper2 >= body2 * 2.0 and lower2 <= max(body2, rng2 * 0.20):
        patterns.append("Shooting Star")

    # ── Two-candle ────────────────────────────────────────────────────────
    if (c2.close > c2.open and body1 < 0
            and c2.open <= c1.close and c2.close >= c1.open):
        patterns.append("Bullish Engulfing")
    if (c2.close < c2.open and body1 > 0
            and c2.open >= c1.close and c2.close <= c1.open):
        patterns.append("Bearish Engulfing")

    # Tweezer Top/Bottom (same high/low within 10% of ATR)
    atr_approx = max(c2.high - c2.low, c1.high - c1.low)
    tol = atr_approx * 0.10
    if (body1 > 0 and c2.close < c2.open
            and abs(c1.high - c2.high) <= tol):
        patterns.append("Tweezer Top")
    if (body1 < 0 and c2.close > c2.open
            and abs(c1.low - c2.low) <= tol):
        patterns.append("Tweezer Bottom")

    # ── Three-candle ──────────────────────────────────────────────────────
    # Morning Star: big bearish → small body → bullish closing in c0 body
    if (body0 < 0 and abs(body1) < abs(body0) * 0.4
            and c2.close > c2.open
            and c2.close > (c0.open + c0.close) / 2.0):
        patterns.append("Morning Star")
    # Evening Star: big bullish → small body → bearish closing in c0 body
    if (body0 > 0 and abs(body1) < abs(body0) * 0.4
            and c2.close < c2.open
            and c2.close < (c0.open + c0.close) / 2.0):
        patterns.append("Evening Star")
    # Three White Soldiers
    if (c0.close > c0.open and c1.close > c1.open and c2.close > c2.open
            and c1.close > c0.close and c2.close > c1.close
            and c1.open > c0.open and c2.open > c1.open):
        patterns.append("Three White Soldiers")
    # Three Black Crows
    if (c0.close < c0.open and c1.close < c1.open and c2.close < c2.open
            and c1.close < c0.close and c2.close < c1.close
            and c1.open < c0.open and c2.open < c1.open):
        patterns.append("Three Black Crows")

    return patterns


# ──────────────────────────────────────────────────────────────────────────────
# Walk-forward validation (kept from original, used as calibration)
# ──────────────────────────────────────────────────────────────────────────────

def _walk_forward_validation(candles: List[Candle],
                             min_history: int = 40) -> Dict[str, Any]:
    if len(candles) < min_history + 8:
        return {"samples": 0, "accuracy": 0.5,
                "call_accuracy": 0.5, "put_accuracy": 0.5, "edge": 0.0}
    wins = calls = puts = call_wins = put_wins = 0
    for point in range(min_history, len(candles) - 1):
        hist = analyze_candles(candles[:point], run_validation=False)
        if hist.direction == "WAIT":
            continue
        actual_up = candles[point].close > candles[point - 1].close
        predicted_up = hist.direction == "CALL"
        won = predicted_up == actual_up
        wins += int(won)
        if predicted_up:
            calls += 1
            call_wins += int(won)
        else:
            puts += 1
            put_wins += int(won)
    samples = calls + puts
    acc = wins / samples if samples else 0.5
    return {
        "samples": samples,
        "accuracy": round(acc, 3),
        "call_accuracy": round(call_wins / calls if calls else 0.5, 3),
        "put_accuracy": round(put_wins / puts if puts else 0.5, 3),
        "edge": round(acc - 0.5, 3),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Main analysis function
# ──────────────────────────────────────────────────────────────────────────────

def analyze_candles(candles: List[Candle],
                    run_validation: bool = True) -> SignalResult:
    """Analyze a candle history and return a CALL / PUT / WAIT signal.

    Scoring architecture
    ────────────────────
    Three independent categories vote:
      TREND     – trend-following indicators (high weight)
      OSCILLATOR – mean-reversion / momentum oscillators
      PRICE_ACT  – candle patterns and S/R proximity

    A direction is emitted only when ≥ 2 categories agree.  Within each
    category the margin between call/put votes determines a category score
    (0.0–1.0).  The final confidence is the weighted average of category
    scores, optionally calibrated by walk-forward accuracy.
    """
    if not candles:
        raise ValueError("No candles provided for analysis")

    sorted_candles = sorted(candles, key=lambda c: c.timestamp)
    closes = [c.close for c in sorted_candles]
    highs  = [c.high  for c in sorted_candles]
    lows   = [c.low   for c in sorted_candles]
    current_price = closes[-1]

    if len(closes) < 30:
        return SignalResult(
            direction="WAIT",
            confidence=0.5,
            price=current_price,
            reasons=["WAIT: chưa đủ lịch sử để phân tích"],
            indicators={"price": current_price, "candles": len(closes)},
        )

    # ── Compute all indicators ─────────────────────────────────────────────
    ema9_series  = _ema(closes, 9)
    ema21_series = _ema(closes, 21)
    ema50_series = _ema(closes, 50)
    ema9  = ema9_series[-1]  if ema9_series  else current_price
    ema21 = ema21_series[-1] if ema21_series else current_price
    ema50 = ema50_series[-1] if ema50_series else current_price

    rsi_full = _rsi_series(closes, 14)
    rsi_val  = rsi_full[-1]
    macd_val = _macd(closes)
    bb       = _bollinger(closes)
    stoch    = _stochastic(highs, lows, closes)
    willy    = _williams_r(highs, lows, closes)
    cci_val  = _cci(highs, lows, closes)
    atr_val  = _atr(highs, lows, closes)
    adx      = _adx_wilder(highs, lows, closes)
    supertr  = _supertrend(highs, lows, closes)
    ha_dir   = _heikin_ashi_direction(sorted_candles)
    struct   = _price_structure(highs, lows)
    div      = _rsi_divergence(closes, rsi_full)
    mom      = _momentum(closes)
    patterns = _candle_patterns(sorted_candles)

    support    = min(lows[-20:])
    resistance = max(highs[-20:])
    span       = max(resistance - support, 1e-12)

    trending = adx["adx"] >= 20

    # ── Volatility filter ─────────────────────────────────────────────────
    # ATR as percentage of price; when price barely moves, signals are noise
    atr_pct = atr_val / max(current_price, 1e-12)
    low_volatility = atr_pct < 0.0005   # below 0.05% per candle

    # ── TREND category ────────────────────────────────────────────────────
    t_call = t_put = 0.0
    trend_reasons: List[str] = []

    # EMA 9/21 crossover (weight 2.0)
    if ema9 > ema21:
        t_call += 2.0
        trend_reasons.append(f"EMA9 > EMA21 (trend tăng)")
    else:
        t_put += 2.0
        trend_reasons.append(f"EMA9 < EMA21 (trend giảm)")

    # EMA 50 long-term filter (weight 1.5)
    if len(ema50_series) >= 50:
        if current_price > ema50:
            t_call += 1.5
            trend_reasons.append("Giá trên EMA50 (xu hướng dài hạn tăng)")
        else:
            t_put += 1.5
            trend_reasons.append("Giá dưới EMA50 (xu hướng dài hạn giảm)")

    # MACD crossover (weight 2.0) or histogram direction (weight 1.0)
    co = macd_val["crossover"]
    if co == 1:
        t_call += 2.0
        trend_reasons.append("MACD cắt lên đường tín hiệu ✦ crossover tăng")
    elif co == -1:
        t_put += 2.0
        trend_reasons.append("MACD cắt xuống đường tín hiệu ✦ crossover giảm")
    else:
        if macd_val["histogram"] > 0:
            t_call += 1.0
            trend_reasons.append("MACD histogram dương")
        else:
            t_put += 1.0
            trend_reasons.append("MACD histogram âm")

    # Supertrend (weight 2.0)
    if supertr["direction"] == 1:
        t_call += 2.0
        trend_reasons.append(f"Supertrend: bullish ({supertr['value']:.5f})")
    elif supertr["direction"] == -1:
        t_put += 2.0
        trend_reasons.append(f"Supertrend: bearish ({supertr['value']:.5f})")

    # Heikin-Ashi (weight 1.5)
    if ha_dir == 1:
        t_call += 1.5
        trend_reasons.append("Heikin-Ashi: 3 nến liên tiếp tăng")
    elif ha_dir == -1:
        t_put += 1.5
        trend_reasons.append("Heikin-Ashi: 3 nến liên tiếp giảm")

    # Price structure HH/HL or LH/LL (weight 1.5)
    if struct == 1:
        t_call += 1.5
        trend_reasons.append("Cấu trúc giá: HH + HL (uptrend)")
    elif struct == -1:
        t_put += 1.5
        trend_reasons.append("Cấu trúc giá: LH + LL (downtrend)")

    # ADX direction (weight 1.2, only when trend is strong)
    if adx["adx"] >= 20:
        if adx["plus_di"] > adx["minus_di"]:
            t_call += 1.2
            trend_reasons.append(f"ADX {adx['adx']:.1f}: +DI > -DI (lực mua)")
        else:
            t_put += 1.2
            trend_reasons.append(f"ADX {adx['adx']:.1f}: -DI > +DI (lực bán)")

    # Momentum (weight 0.8)
    if mom["agreement"]:
        if mom["short"] > 0:
            t_call += 0.8
        elif mom["short"] < 0:
            t_put += 0.8

    t_total = t_call + t_put
    t_direction = 1 if t_call >= t_put else -1
    t_score = max(t_call, t_put) / t_total if t_total else 0.5

    # ── OSCILLATOR category ───────────────────────────────────────────────
    o_call = o_put = 0.0
    osc_reasons: List[str] = []
    osc_w = 0.5 if trending else 1.0   # oscillators are less reliable in trends

    # RSI with trend context
    if rsi_val <= 30:
        o_call += 1.5 * osc_w
        osc_reasons.append(f"RSI {rsi_val:.1f}: vùng quá bán")
    elif rsi_val <= 40:
        o_call += 0.6 * osc_w
    elif rsi_val >= 70:
        o_put += 1.5 * osc_w
        osc_reasons.append(f"RSI {rsi_val:.1f}: vùng quá mua")
    elif rsi_val >= 60:
        o_put += 0.6 * osc_w
    else:
        # Neutral zone: slight lean
        if rsi_val > 52:
            o_call += 0.2 * osc_w
        elif rsi_val < 48:
            o_put += 0.2 * osc_w

    # RSI divergence (weight 1.8 – high predictive value)
    if div == 1:
        o_call += 1.8
        osc_reasons.append("RSI divergence tăng: giá thấp hơn nhưng RSI cao hơn")
    elif div == -1:
        o_put += 1.8
        osc_reasons.append("RSI divergence giảm: giá cao hơn nhưng RSI thấp hơn")

    # Stochastic
    if stoch["k"] <= 20 and stoch["k"] >= stoch["d"]:
        o_call += 1.4 * osc_w
        osc_reasons.append(f"Stochastic {stoch['k']:.1f}: bật lên từ vùng quá bán")
    elif stoch["k"] >= 80 and stoch["k"] <= stoch["d"]:
        o_put += 1.4 * osc_w
        osc_reasons.append(f"Stochastic {stoch['k']:.1f}: quay đầu từ vùng quá mua")
    elif stoch["k"] < 50:
        o_put += 0.3 * osc_w
    elif stoch["k"] > 50:
        o_call += 0.3 * osc_w

    # Williams %R
    if willy <= -80:
        o_call += 1.0 * osc_w
        osc_reasons.append(f"Williams %R {willy:.1f}: quá bán")
    elif willy >= -20:
        o_put += 1.0 * osc_w
        osc_reasons.append(f"Williams %R {willy:.1f}: quá mua")

    # CCI
    if cci_val <= -100:
        o_call += 1.0 * osc_w
        osc_reasons.append(f"CCI {cci_val:.0f}: quá bán")
    elif cci_val >= 100:
        o_put += 1.0 * osc_w
        osc_reasons.append(f"CCI {cci_val:.0f}: quá mua")

    o_total = o_call + o_put
    o_direction = 1 if o_call >= o_put else -1
    o_score = max(o_call, o_put) / o_total if o_total else 0.5

    # ── PRICE ACTION category ─────────────────────────────────────────────
    pa_call = pa_put = 0.0
    pa_reasons: List[str] = []

    # Bollinger position
    pb = bb["percent_b"]
    if pb <= 0.10:
        pa_call += 1.8
        pa_reasons.append("Bollinger: giá chạm/phá dải dưới")
    elif pb <= 0.20:
        pa_call += 0.8
    elif pb >= 0.90:
        pa_put += 1.8
        pa_reasons.append("Bollinger: giá chạm/phá dải trên")
    elif pb >= 0.80:
        pa_put += 0.8

    # Support / Resistance proximity
    if current_price <= support + span * 0.10:
        pa_call += 1.2
        pa_reasons.append("Giá gần vùng hỗ trợ 20 nến")
    elif current_price >= resistance - span * 0.10:
        pa_put += 1.2
        pa_reasons.append("Giá gần vùng kháng cự 20 nến")

    # Candle patterns
    CALL_PATTERNS = {"Hammer", "Bullish Engulfing", "Morning Star",
                     "Three White Soldiers", "Tweezer Bottom"}
    PUT_PATTERNS  = {"Shooting Star", "Bearish Engulfing", "Evening Star",
                     "Three Black Crows", "Tweezer Top"}
    STRONG_PATTERNS = {"Three White Soldiers", "Three Black Crows",
                       "Morning Star", "Evening Star",
                       "Bullish Engulfing", "Bearish Engulfing"}

    for pat in patterns:
        if pat in CALL_PATTERNS:
            w = 1.8 if pat in STRONG_PATTERNS else 1.0
            pa_call += w
            pa_reasons.append(f"Mẫu nến CALL: {pat}")
        elif pat in PUT_PATTERNS:
            w = 1.8 if pat in STRONG_PATTERNS else 1.0
            pa_put += w
            pa_reasons.append(f"Mẫu nến PUT: {pat}")
        elif pat == "Doji":
            pa_reasons.append("Doji: thị trường lưỡng lự")

    # Body strength of the last candle as a weak signal
    if len(sorted_candles) >= 2:
        last = sorted_candles[-1]
        body = last.close - last.open
        rng = max(last.high - last.low, 1e-12)
        if abs(body) / rng >= 0.60:
            if body > 0:
                pa_call += 0.4
            else:
                pa_put += 0.4

    pa_total = pa_call + pa_put
    pa_direction = 1 if pa_call >= pa_put else -1
    pa_score = max(pa_call, pa_put) / pa_total if pa_total else 0.5

    # ── Categorical confluence ────────────────────────────────────────────
    dirs = [t_direction, o_direction, pa_direction]
    call_votes = sum(1 for d in dirs if d == 1)
    put_votes  = sum(1 for d in dirs if d == -1)

    # Must win at least 2 of 3 categories
    if call_votes >= 2:
        direction = "CALL"
    elif put_votes >= 2:
        direction = "PUT"
    else:
        direction = "WAIT"

    # Weighted confidence (TREND carries most weight)
    weights = (2.5, 1.5, 1.0)   # trend, oscillator, price_action
    scores = (t_score, o_score, pa_score)
    raw_confidence = sum(w * s for w, s in zip(weights, scores)) / sum(weights)
    confidence = min(0.95, max(0.50, raw_confidence))

    # Low-volatility penalty
    if low_volatility:
        confidence = max(0.50, confidence - 0.06)

    # ── Walk-forward calibration ───────────────────────────────────────────
    validation = (
        _walk_forward_validation(sorted_candles)
        if run_validation
        else {"samples": 0, "accuracy": 0.5,
              "call_accuracy": 0.5, "put_accuracy": 0.5, "edge": 0.0}
    )
    if validation["samples"] >= 12 and direction != "WAIT":
        hist_acc = validation[
            "call_accuracy" if direction == "CALL" else "put_accuracy"
        ]
        opp_acc = validation[
            "put_accuracy" if direction == "CALL" else "call_accuracy"
        ]
        # Blend indicator confidence with historical accuracy (35% weight)
        confidence = confidence * 0.65 + hist_acc * 0.35
        # Flip only when historical evidence strongly contradicts indicators
        if confidence < 0.54 and opp_acc - hist_acc >= 0.10:
            direction = "PUT" if direction == "CALL" else "CALL"
            confidence = opp_acc

    # Minimum edge threshold
    if direction != "WAIT" and confidence < 0.54:
        direction = "WAIT"

    confidence = round(confidence, 2)

    # ── Compile reasons ───────────────────────────────────────────────────
    all_reasons: List[str] = []
    # Only include reasons from the winning side to keep the message clean
    if direction == "CALL":
        all_reasons += [r for r in trend_reasons if "tăng" in r or "bullish" in r.lower() or "buy" in r.lower() or "+" in r or "trên" in r or "hỗ trợ" in r]
        all_reasons += [r for r in osc_reasons if "bán" in r or "tăng" in r or "divergence tăng" in r]
        all_reasons += pa_reasons[:3]
    elif direction == "PUT":
        all_reasons += [r for r in trend_reasons if "giảm" in r or "bearish" in r.lower() or "dưới" in r or "kháng cự" in r]
        all_reasons += [r for r in osc_reasons if "mua" in r or "giảm" in r or "divergence giảm" in r]
        all_reasons += pa_reasons[:3]
    else:
        all_reasons.append("WAIT: tín hiệu từ các nhóm chỉ báo chưa thống nhất")
        all_reasons += trend_reasons[:2]

    if validation["samples"] >= 12:
        all_reasons.append(
            f"Backtest {validation['samples']} mẫu: "
            f"{validation['accuracy'] * 100:.0f}% chính xác"
        )
    if low_volatility:
        all_reasons.append("Biến động thấp – giảm độ tin cậy")

    return SignalResult(
        direction=direction,
        confidence=confidence,
        price=current_price,
        reasons=all_reasons or ["Phân tích hoàn tất"],
        indicators={
            "price": current_price,
            "ema9": ema9, "ema21": ema21, "ema50": ema50,
            "rsi": rsi_val,
            "macd": macd_val,
            "bollinger": bb,
            "stochastic": stoch,
            "williams_r": willy,
            "cci": cci_val,
            "adx": adx,
            "atr": atr_val,
            "atr_pct": round(atr_pct * 100, 4),
            "supertrend": supertr,
            "ha_direction": ha_dir,
            "price_structure": struct,
            "rsi_divergence": div,
            "momentum": mom,
            "support": support,
            "resistance": resistance,
            "patterns": patterns,
            "t_call": round(t_call, 2), "t_put": round(t_put, 2),
            "o_call": round(o_call, 2), "o_put": round(o_put, 2),
            "pa_call": round(pa_call, 2), "pa_put": round(pa_put, 2),
            "category_votes": {"call": call_votes, "put": put_votes},
            "candles": len(closes),
            "walk_forward": validation,
        },
    )


# ──────────────────────────────────────────────────────────────────────────────
# Signal evaluation (unchanged interface)
# ──────────────────────────────────────────────────────────────────────────────

def evaluate_signal(signal: SignalResult, current_price: float) -> Dict[str, Any]:
    """Compare exit price against entry price to determine win/loss."""
    if signal.direction == "CALL":
        won = current_price > signal.price
    else:
        won = current_price < signal.price
    diff = current_price - signal.price
    pct  = (diff / signal.price) * 100 if signal.price else 0.0
    return {
        "won": won,
        "direction": signal.direction,
        "entry_price": signal.price,
        "exit_price": current_price,
        "diff": diff,
        "diff_pct": pct,
    }
