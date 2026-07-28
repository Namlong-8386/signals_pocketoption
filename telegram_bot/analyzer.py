"""Technical analysis engine for PocketOption signals.

Architecture
────────────
1. Market-regime detection (trending vs ranging) via ADX + Bollinger bandwidth.
2. Three independent voting categories:
     TREND      – EMA stack, Supertrend, Heikin-Ashi, MACD crossover,
                  price structure, ADX direction
     OSCILLATOR – RSI (Wilder), Stochastic, Williams %R, CCI, momentum,
                  RSI divergence
     PRICE_ACT  – candle patterns, Bollinger bands, S/R proximity
3. Regime-aware weighting:
     trending  → TREND carries 55 %, OSCILLATOR 25 %, PRICE_ACT 20 %
     ranging   → OSCILLATOR carries 50 %, TREND 20 %, PRICE_ACT 30 %
4. Signal fires only when ≥ 2 of 3 categories agree AND
   the weighted category-score margin exceeds a threshold.
5. Walk-forward validation removed entirely.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

from pocketoptionapi_async.models import Candle


# ─────────────────────────────────────────────────────────────────────────────
# Data class
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SignalResult:
    direction: str          # "CALL" | "PUT" | "WAIT"
    confidence: float       # 0.0 – 1.0
    price: float
    reasons: List[str]
    indicators: Dict[str, Any] = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
# Pure maths helpers
# ─────────────────────────────────────────────────────────────────────────────

def _sma(v: List[float], p: int) -> float:
    w = v[-p:]
    return sum(w) / len(w) if w else 0.0


def _ema(values: List[float], period: int) -> List[float]:
    if len(values) < period:
        return list(values)
    k = 2.0 / (period + 1.0)
    out = [_sma(values[:period], period)]
    for x in values[period:]:
        out.append(x * k + out[-1] * (1.0 - k))
    return out


def _wilder(values: List[float], period: int) -> List[float]:
    """Wilder's smoothing – used by RSI and ADX."""
    if len(values) < period:
        return [0.0] * len(values)
    seed = sum(values[:period]) / period
    out: List[float] = [0.0] * (period - 1) + [seed]
    k = 1.0 / period
    for x in values[period:]:
        out.append(out[-1] * (1.0 - k) + x * k)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Indicators
# ─────────────────────────────────────────────────────────────────────────────

def _rsi(closes: List[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    chg = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    sg = _wilder([max(c, 0.0) for c in chg], period)
    sl = _wilder([max(-c, 0.0) for c in chg], period)
    ag, al = sg[-1], sl[-1]
    return 100.0 if al == 0.0 else 100.0 - 100.0 / (1.0 + ag / al)


def _rsi_series(closes: List[float], period: int = 14) -> List[float]:
    if len(closes) < period + 1:
        return [50.0] * len(closes)
    chg = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    sg = _wilder([max(c, 0.0) for c in chg], period)
    sl = _wilder([max(-c, 0.0) for c in chg], period)
    series = [50.0]
    for ag, al in zip(sg, sl):
        series.append(100.0 if al == 0.0 else 100.0 - 100.0 / (1.0 + ag / al))
    return series


def _macd(closes: List[float], fast=12, slow=26, sig=9) -> Dict[str, float]:
    ef = _ema(closes, fast)
    es = _ema(closes, slow)
    ml = [f - s for f, s in zip(ef[-len(es):], es)]
    if len(ml) < sig:
        return {"macd": 0.0, "signal": 0.0, "histogram": 0.0, "crossover": 0}
    sl2 = _ema(ml, sig)
    hist = ml[-1] - sl2[-1]
    xo = 0
    if len(ml) >= 2 and len(sl2) >= 2:
        if ml[-2] <= sl2[-2] and ml[-1] > sl2[-1]:
            xo = 1
        elif ml[-2] >= sl2[-2] and ml[-1] < sl2[-1]:
            xo = -1
    return {"macd": ml[-1], "signal": sl2[-1], "histogram": hist, "crossover": xo}


def _bollinger(closes: List[float], period=20, mult=2.0) -> Dict[str, float]:
    if len(closes) < period:
        return {"upper": closes[-1], "middle": closes[-1],
                "lower": closes[-1], "pct_b": 0.5, "bandwidth": 0.0}
    w = closes[-period:]
    mid = sum(w) / period
    std = math.sqrt(sum((x - mid) ** 2 for x in w) / period)
    up, lo = mid + mult * std, mid - mult * std
    bw = up - lo
    pct = (closes[-1] - lo) / bw if bw > 1e-12 else 0.5
    return {"upper": up, "middle": mid, "lower": lo,
            "pct_b": pct, "bandwidth": bw}


def _atr(hi: List[float], lo: List[float], cl: List[float], period=14) -> float:
    if len(cl) < 2:
        return 0.0
    trs = [max(hi[i] - lo[i], abs(hi[i] - cl[i-1]), abs(lo[i] - cl[i-1]))
           for i in range(1, len(cl))]
    return _wilder(trs, period)[-1]


def _stochastic(hi: List[float], lo: List[float], cl: List[float],
                kp=14, dp=3) -> Dict[str, float]:
    if len(cl) < kp:
        return {"k": 50.0, "d": 50.0}
    ks = []
    for end in range(kp, len(cl) + 1):
        h, l = max(hi[end-kp:end]), min(lo[end-kp:end])
        ks.append(50.0 if h == l else 100.0 * (cl[end-1] - l) / (h - l))
    return {"k": ks[-1], "d": _sma(ks, dp)}


def _williams_r(hi: List[float], lo: List[float], cl: List[float], p=14) -> float:
    if len(cl) < p:
        return -50.0
    h, l = max(hi[-p:]), min(lo[-p:])
    return -50.0 if h == l else -100.0 * (h - cl[-1]) / (h - l)


def _cci(hi: List[float], lo: List[float], cl: List[float], p=20) -> float:
    if len(cl) < p:
        return 0.0
    tp = [(h + l + c) / 3 for h, l, c in zip(hi, lo, cl)]
    w = tp[-p:]
    m = sum(w) / p
    dev = sum(abs(x - m) for x in w) / p
    return 0.0 if dev == 0 else (w[-1] - m) / (0.015 * dev)


def _adx(hi: List[float], lo: List[float], cl: List[float],
         period=14) -> Dict[str, float]:
    if len(cl) < period + 2:
        return {"adx": 0.0, "plus_di": 0.0, "minus_di": 0.0}
    trs, pdm, mdm = [], [], []
    for i in range(1, len(cl)):
        trs.append(max(hi[i]-lo[i], abs(hi[i]-cl[i-1]), abs(lo[i]-cl[i-1])))
        up = hi[i] - hi[i-1]
        dn = lo[i-1] - lo[i]
        pdm.append(up if up > dn and up > 0 else 0.0)
        mdm.append(dn if dn > up and dn > 0 else 0.0)
    atr_w = _wilder(trs, period)
    pdi_w = _wilder(pdm, period)
    mdi_w = _wilder(mdm, period)
    last_atr = atr_w[-1]
    if last_atr == 0:
        return {"adx": 0.0, "plus_di": 0.0, "minus_di": 0.0}
    plus_di  = 100.0 * pdi_w[-1] / last_atr
    minus_di = 100.0 * mdi_w[-1] / last_atr
    dx_list: List[float] = []
    for a, p2, m in zip(atr_w, pdi_w, mdi_w):
        if a == 0:
            continue
        pdi = 100.0 * p2 / a
        mdi = 100.0 * m / a
        s = pdi + mdi
        dx_list.append(100.0 * abs(pdi - mdi) / s if s else 0.0)
    adx_w = _wilder(dx_list, period)
    return {"adx": adx_w[-1] if adx_w else 0.0,
            "plus_di": plus_di, "minus_di": minus_di}


def _supertrend(hi: List[float], lo: List[float], cl: List[float],
                period=10, mult=3.0) -> Dict[str, Any]:
    if len(cl) < period + 1:
        return {"direction": 0, "value": cl[-1]}
    hl2 = [(h + l) / 2 for h, l in zip(hi, lo)]
    trs = [max(hi[i]-lo[i], abs(hi[i]-cl[i-1]), abs(lo[i]-cl[i-1]))
           for i in range(1, len(cl))]
    atr_s = [trs[0]] + _wilder(trs, period)

    upper_r = [hl2[i] + mult * atr_s[i] for i in range(len(cl))]
    lower_r = [hl2[i] - mult * atr_s[i] for i in range(len(cl))]
    upper, lower = list(upper_r), list(lower_r)
    direction = [1] * len(cl)
    for i in range(1, len(cl)):
        upper[i] = min(upper_r[i], upper[i-1]) if cl[i-1] <= upper[i-1] else upper_r[i]
        lower[i] = max(lower_r[i], lower[i-1]) if cl[i-1] >= lower[i-1] else lower_r[i]
        if direction[i-1] == -1 and cl[i] > upper[i-1]:
            direction[i] = 1
        elif direction[i-1] == 1 and cl[i] < lower[i-1]:
            direction[i] = -1
        else:
            direction[i] = direction[i-1]
    val = lower[-1] if direction[-1] == 1 else upper[-1]
    return {"direction": direction[-1], "value": val}


def _heikin_ashi(candles: List[Candle]) -> int:
    """Return +1 (3 consecutive bullish HA), -1 (bearish), 0 (mixed)."""
    if len(candles) < 4:
        return 0
    ha_c = [(c.open + c.high + c.low + c.close) / 4 for c in candles]
    ha_o = [(candles[0].open + candles[0].close) / 2]
    for i in range(1, len(candles)):
        ha_o.append((ha_o[-1] + ha_c[i-1]) / 2)
    bulls = [ha_c[i] > ha_o[i] for i in range(-3, 0)]
    if all(bulls):  return 1
    if not any(bulls): return -1
    return 0


def _price_structure(hi: List[float], lo: List[float], window=20) -> int:
    """HH+HL → +1 (uptrend), LH+LL → -1 (downtrend), 0 mixed."""
    if len(hi) < window:
        return 0
    h, l = hi[-window:], lo[-window:]
    mid = window // 2
    if max(h[mid:]) > max(h[:mid]) and min(l[mid:]) > min(l[:mid]):
        return 1
    if max(h[mid:]) < max(h[:mid]) and min(l[mid:]) < min(l[:mid]):
        return -1
    return 0


def _rsi_divergence(cl: List[float], rsi: List[float], lb=16) -> int:
    """
    Bullish divergence  (+1): price makes lower low, RSI makes higher low.
    Bearish divergence  (-1): price makes higher high, RSI makes lower high.
    Looks at the most recent price extreme within *lb* bars, then compares to
    the current bar. Requires the extreme to be at least 5 bars ago to avoid
    detecting the same bar.
    """
    if len(cl) < lb + 1 or len(rsi) < lb + 1:
        return 0
    p = cl[-lb:]
    r = rsi[-lb:]
    # Most extreme price position (excluding last bar)
    lo_idx = p[:-1].index(min(p[:-1]))
    hi_idx = p[:-1].index(max(p[:-1]))
    # Must be at least 3 bars ago to be a meaningful pivot
    if lo_idx > lb - 4:
        lo_idx = 0
    if hi_idx > lb - 4:
        hi_idx = 0
    bullish = p[-1] < p[lo_idx] and r[-1] > r[lo_idx]
    bearish = p[-1] > p[hi_idx] and r[-1] < r[hi_idx]
    if bullish:  return 1
    if bearish:  return -1
    return 0


def _momentum(cl: List[float]) -> Dict[str, float]:
    if len(cl) < 12:
        return {"short": 0.0, "medium": 0.0, "agree": 0.0}
    base = max(abs(cl[-1]), 1e-12)
    short  = (cl[-1] - cl[-4])  / base
    medium = (cl[-1] - cl[-12]) / base
    return {"short": short, "medium": medium,
            "agree": 1.0 if short * medium > 0 else 0.0}


# ─────────────────────────────────────────────────────────────────────────────
# Candle patterns
# ─────────────────────────────────────────────────────────────────────────────

def _patterns(candles: List[Candle]) -> Tuple[List[str], List[str]]:
    """Return (bullish_patterns, bearish_patterns) for the last bar."""
    if len(candles) < 3:
        return [], []
    c0, c1, c2 = candles[-3], candles[-2], candles[-1]

    body2  = abs(c2.close - c2.open)
    rng2   = max(c2.high - c2.low, 1e-12)
    upper2 = c2.high - max(c2.open, c2.close)
    lower2 = min(c2.open, c2.close) - c2.low
    b1 = c1.close - c1.open   # signed
    b0 = c0.close - c0.open

    bull: List[str] = []
    bear: List[str] = []

    # ── Single-candle ────────────────────────────────────────────
    if lower2 >= body2 * 2.0 and upper2 <= max(body2, rng2 * 0.25):
        bull.append("Hammer")
    if upper2 >= body2 * 2.0 and lower2 <= max(body2, rng2 * 0.25):
        bear.append("Shooting Star")

    # ── Two-candle ───────────────────────────────────────────────
    if (c2.close > c2.open and b1 < 0
            and c2.open <= c1.close and c2.close >= c1.open):
        bull.append("Bullish Engulfing")
    if (c2.close < c2.open and b1 > 0
            and c2.open >= c1.close and c2.close <= c1.open):
        bear.append("Bearish Engulfing")

    tol = max(c2.high - c2.low, c1.high - c1.low) * 0.12
    if b1 > 0 and c2.close < c2.open and abs(c1.high - c2.high) <= tol:
        bear.append("Tweezer Top")
    if b1 < 0 and c2.close > c2.open and abs(c1.low - c2.low) <= tol:
        bull.append("Tweezer Bottom")

    # ── Three-candle ─────────────────────────────────────────────
    if (b0 < 0 and abs(b1) < abs(b0) * 0.4 and c2.close > c2.open
            and c2.close > (c0.open + c0.close) / 2):
        bull.append("Morning Star")
    if (b0 > 0 and abs(b1) < abs(b0) * 0.4 and c2.close < c2.open
            and c2.close < (c0.open + c0.close) / 2):
        bear.append("Evening Star")

    if (b0 > 0 and b1 > 0 and c2.close > c2.open
            and c1.close > c0.close and c2.close > c1.close
            and c1.open > c0.open and c2.open > c1.open):
        bull.append("Three White Soldiers")
    if (b0 < 0 and b1 < 0 and c2.close < c2.open
            and c1.close < c0.close and c2.close < c1.close
            and c1.open < c0.open and c2.open < c1.open):
        bear.append("Three Black Crows")

    return bull, bear


# ─────────────────────────────────────────────────────────────────────────────
# Helper: normalised score for one category
# ─────────────────────────────────────────────────────────────────────────────

def _cat_score(call: float, put: float) -> Tuple[int, float]:
    """Return (direction_int, normalised_margin_score).
    direction: +1 CALL, -1 PUT, 0 tie.
    score: 0.50 = tie … 1.00 = total dominance.
    """
    total = call + put
    if total < 1e-9:
        return 0, 0.50
    d = 1 if call > put else (-1 if put > call else 0)
    score = max(call, put) / total
    return d, score


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def analyze_candles(candles: List[Candle],
                    run_validation: bool = False) -> SignalResult:
    """
    Analyse a candle history and return CALL / PUT / WAIT.

    Parameters
    ----------
    candles         : list of Candle objects (any order; will be sorted).
    run_validation  : kept for interface compatibility, always ignored.
    """
    if not candles:
        raise ValueError("No candles provided")

    sc = sorted(candles, key=lambda c: c.timestamp)
    cl = [c.close for c in sc]
    hi = [c.high  for c in sc]
    lo = [c.low   for c in sc]
    price = cl[-1]

    if len(cl) < 35:
        return SignalResult("WAIT", 0.5, price,
                            ["Chưa đủ nến để phân tích (cần ≥ 35)"],
                            {"price": price, "candles": len(cl)})

    # ── Compute all indicators ────────────────────────────────────────────
    ema9s  = _ema(cl, 9);   ema9  = ema9s[-1]
    ema21s = _ema(cl, 21);  ema21 = ema21s[-1]
    ema50s = _ema(cl, 50);  ema50 = ema50s[-1]

    rsi_s   = _rsi_series(cl, 14);  rsi = rsi_s[-1]
    macd    = _macd(cl)
    bb      = _bollinger(cl)
    stoch   = _stochastic(hi, lo, cl)
    willy   = _williams_r(hi, lo, cl)
    cci_v   = _cci(hi, lo, cl)
    atr_v   = _atr(hi, lo, cl)
    adx_v   = _adx(hi, lo, cl)
    st      = _supertrend(hi, lo, cl)
    ha      = _heikin_ashi(sc)
    struct  = _price_structure(hi, lo)
    div     = _rsi_divergence(cl, rsi_s)
    mom     = _momentum(cl)
    bull_p, bear_p = _patterns(sc)
    all_p   = bull_p + bear_p

    sup = min(lo[-20:])
    res = max(hi[-20:])
    span = max(res - sup, 1e-12)

    # ── Market regime ─────────────────────────────────────────────────────
    # ADX ≥ 25 = trending; bandwidth squeeze = potential breakout
    trending  = adx_v["adx"] >= 25
    very_flat = adx_v["adx"] < 15          # not worth trading
    bw_avg    = _sma([abs(cl[i] - cl[i-1]) for i in range(1, len(cl))], 20)
    atr_pct   = atr_v / max(price, 1e-12)
    low_vol   = atr_pct < 0.0004            # < 0.04 % per candle

    # ── TREND category ────────────────────────────────────────────────────
    t_c = t_p = 0.0
    t_rsn: List[str] = []

    # EMA 9/21 (2.5)
    if ema9 > ema21:
        t_c += 2.5; t_rsn.append(f"EMA9 ({ema9:.5f}) > EMA21 ({ema21:.5f}) — trend tăng")
    else:
        t_p += 2.5; t_rsn.append(f"EMA9 ({ema9:.5f}) < EMA21 ({ema21:.5f}) — trend giảm")

    # EMA 50 (1.5) — only meaningful when we have enough bars
    if len(ema50s) >= 50:
        if price > ema50:
            t_c += 1.5; t_rsn.append(f"Giá trên EMA50 ({ema50:.5f})")
        else:
            t_p += 1.5; t_rsn.append(f"Giá dưới EMA50 ({ema50:.5f})")

    # MACD crossover (2.5) > histogram direction (1.0)
    if macd["crossover"] == 1:
        t_c += 2.5; t_rsn.append("MACD crossover tăng ✦")
    elif macd["crossover"] == -1:
        t_p += 2.5; t_rsn.append("MACD crossover giảm ✦")
    elif macd["histogram"] > 0:
        t_c += 1.0; t_rsn.append(f"MACD histogram dương ({macd['histogram']:.6f})")
    else:
        t_p += 1.0; t_rsn.append(f"MACD histogram âm ({macd['histogram']:.6f})")

    # Supertrend (2.5)
    if st["direction"] == 1:
        t_c += 2.5; t_rsn.append(f"Supertrend bullish (support {st['value']:.5f})")
    elif st["direction"] == -1:
        t_p += 2.5; t_rsn.append(f"Supertrend bearish (resistance {st['value']:.5f})")

    # Heikin-Ashi (1.5)
    if ha == 1:
        t_c += 1.5; t_rsn.append("Heikin-Ashi: 3 nến liên tiếp tăng")
    elif ha == -1:
        t_p += 1.5; t_rsn.append("Heikin-Ashi: 3 nến liên tiếp giảm")

    # Price structure (1.5)
    if struct == 1:
        t_c += 1.5; t_rsn.append("Cấu trúc HH+HL (uptrend)")
    elif struct == -1:
        t_p += 1.5; t_rsn.append("Cấu trúc LH+LL (downtrend)")

    # ADX direction (1.5) — only when trending
    if adx_v["adx"] >= 20:
        if adx_v["plus_di"] > adx_v["minus_di"]:
            t_c += 1.5; t_rsn.append(f"ADX {adx_v['adx']:.1f}: +DI > -DI (lực mua)")
        else:
            t_p += 1.5; t_rsn.append(f"ADX {adx_v['adx']:.1f}: -DI > +DI (lực bán)")

    # Momentum alignment (0.8)
    if mom["agree"]:
        if mom["short"] > 0:
            t_c += 0.8
        else:
            t_p += 0.8

    t_dir, t_score = _cat_score(t_c, t_p)

    # ── OSCILLATOR category ───────────────────────────────────────────────
    o_c = o_p = 0.0
    o_rsn: List[str] = []
    # Oscillators are less reliable inside a strong trend
    ow = 0.6 if trending else 1.0

    # RSI (2.0)
    if rsi <= 28:
        o_c += 2.0 * ow; o_rsn.append(f"RSI {rsi:.1f} — quá bán mạnh")
    elif rsi <= 38:
        o_c += 1.0 * ow; o_rsn.append(f"RSI {rsi:.1f} — vùng quá bán")
    elif rsi >= 72:
        o_p += 2.0 * ow; o_rsn.append(f"RSI {rsi:.1f} — quá mua mạnh")
    elif rsi >= 62:
        o_p += 1.0 * ow; o_rsn.append(f"RSI {rsi:.1f} — vùng quá mua")
    elif rsi > 53:
        o_c += 0.3 * ow
    elif rsi < 47:
        o_p += 0.3 * ow

    # RSI divergence (2.2) — high predictive value, not dampened by trend
    if div == 1:
        o_c += 2.2; o_rsn.append("RSI bullish divergence — đảo chiều tăng")
    elif div == -1:
        o_p += 2.2; o_rsn.append("RSI bearish divergence — đảo chiều giảm")

    # Stochastic (1.5)
    if stoch["k"] <= 20 and stoch["k"] >= stoch["d"]:
        o_c += 1.5 * ow; o_rsn.append(f"Stochastic {stoch['k']:.1f} bật lên từ quá bán")
    elif stoch["k"] >= 80 and stoch["k"] <= stoch["d"]:
        o_p += 1.5 * ow; o_rsn.append(f"Stochastic {stoch['k']:.1f} quay đầu từ quá mua")
    elif stoch["k"] < 40:
        o_p += 0.4 * ow
    elif stoch["k"] > 60:
        o_c += 0.4 * ow

    # Williams %R (1.0)
    if willy <= -80:
        o_c += 1.0 * ow; o_rsn.append(f"Williams %R {willy:.1f} — quá bán")
    elif willy >= -20:
        o_p += 1.0 * ow; o_rsn.append(f"Williams %R {willy:.1f} — quá mua")

    # CCI (1.0)
    if cci_v <= -100:
        o_c += 1.0 * ow; o_rsn.append(f"CCI {cci_v:.0f} — quá bán")
    elif cci_v >= 100:
        o_p += 1.0 * ow; o_rsn.append(f"CCI {cci_v:.0f} — quá mua")

    o_dir, o_score = _cat_score(o_c, o_p)

    # ── PRICE ACTION category ─────────────────────────────────────────────
    a_c = a_p = 0.0
    a_rsn: List[str] = []

    # Bollinger position (1.8)
    pb = bb["pct_b"]
    if pb <= 0.10:
        a_c += 1.8; a_rsn.append("Giá chạm/phá dải Bollinger dưới")
    elif pb <= 0.22:
        a_c += 0.8
    elif pb >= 0.90:
        a_p += 1.8; a_rsn.append("Giá chạm/phá dải Bollinger trên")
    elif pb >= 0.78:
        a_p += 0.8

    # S/R proximity (1.2)
    if price <= sup + span * 0.10:
        a_c += 1.2; a_rsn.append(f"Giá gần vùng hỗ trợ ({sup:.5f})")
    elif price >= res - span * 0.10:
        a_p += 1.2; a_rsn.append(f"Giá gần vùng kháng cự ({res:.5f})")

    # Candle patterns
    STRONG = {"Three White Soldiers", "Three Black Crows",
              "Morning Star", "Evening Star",
              "Bullish Engulfing", "Bearish Engulfing"}
    for pat in bull_p:
        w = 1.8 if pat in STRONG else 1.0
        a_c += w; a_rsn.append(f"Mẫu nến tăng: {pat}")
    for pat in bear_p:
        w = 1.8 if pat in STRONG else 1.0
        a_p += w; a_rsn.append(f"Mẫu nến giảm: {pat}")

    # Last-candle body strength (0.4)
    last = sc[-1]
    body = last.close - last.open
    rng  = max(last.high - last.low, 1e-12)
    if abs(body) / rng >= 0.60:
        if body > 0:
            a_c += 0.4
        else:
            a_p += 0.4

    a_dir, a_score = _cat_score(a_c, a_p)

    # ── Confluence: require ≥ 2 of 3 categories agree ────────────────────
    dirs  = [t_dir, o_dir, a_dir]
    call_votes = sum(1 for d in dirs if d == 1)
    put_votes  = sum(1 for d in dirs if d == -1)

    if call_votes >= 2:
        direction = "CALL"
    elif put_votes >= 2:
        direction = "PUT"
    else:
        direction = "WAIT"

    # ── Regime-aware weighted confidence ─────────────────────────────────
    if trending:
        # trend-following mode: trust TREND most
        weights = (0.55, 0.25, 0.20)
    else:
        # mean-reversion mode: trust OSCILLATOR most
        weights = (0.20, 0.50, 0.30)

    cat_scores = (t_score, o_score, a_score)
    # For the winning direction we want the score of categories that agree
    if direction == "CALL":
        aligned = [s for d, s in zip(dirs, cat_scores) if d == 1]
        opposed = [s for d, s in zip(dirs, cat_scores) if d == -1]
    elif direction == "PUT":
        aligned = [s for d, s in zip(dirs, cat_scores) if d == -1]
        opposed = [s for d, s in zip(dirs, cat_scores) if d == 1]
    else:
        aligned, opposed = [], []

    if aligned:
        raw_conf = sum(weights[i] * cat_scores[i]
                       for i in range(3)
                       if dirs[i] == (1 if direction == "CALL" else -1))
        norm_w   = sum(weights[i] for i in range(3)
                       if dirs[i] == (1 if direction == "CALL" else -1))
        raw_conf = raw_conf / norm_w if norm_w else 0.5
    else:
        raw_conf = 0.5

    # Penalty: every flat-market indicator reduces confidence
    if very_flat:
        raw_conf -= 0.10
    if low_vol:
        raw_conf -= 0.05

    confidence = round(min(0.95, max(0.50, raw_conf)), 2)

    # Raise confidence bar: only emit strong signals
    MIN_CONF = 0.62
    if direction != "WAIT" and confidence < MIN_CONF:
        direction = "WAIT"

    # ── Build reason list ─────────────────────────────────────────────────
    reasons: List[str] = []
    if direction == "CALL":
        reasons += [r for r in t_rsn  if "tăng" in r or "bullish" in r.lower() or "dương" in r or "trên" in r or "mua" in r or "✦" in r]
        reasons += [r for r in o_rsn  if "bán"  in r or "tăng" in r or "divergence" in r.lower()]
        reasons += a_rsn[:4]
    elif direction == "PUT":
        reasons += [r for r in t_rsn  if "giảm" in r or "bearish" in r.lower() or "âm" in r or "dưới" in r or "bán" in r or "✦" in r]
        reasons += [r for r in o_rsn  if "mua"  in r or "giảm" in r or "divergence" in r.lower()]
        reasons += a_rsn[:4]
    else:
        reasons.append("Tín hiệu từ các nhóm chỉ báo chưa đủ thống nhất — không vào lệnh")
        # Show what's happening in each category
        if t_dir == 1:   reasons.append(f"Trend: CALL ({t_score*100:.0f}%)")
        elif t_dir == -1: reasons.append(f"Trend: PUT ({t_score*100:.0f}%)")
        if o_dir == 1:   reasons.append(f"Oscillator: CALL ({o_score*100:.0f}%)")
        elif o_dir == -1: reasons.append(f"Oscillator: PUT ({o_score*100:.0f}%)")
        if a_dir == 1:   reasons.append(f"Price Action: CALL ({a_score*100:.0f}%)")
        elif a_dir == -1: reasons.append(f"Price Action: PUT ({a_score*100:.0f}%)")

    if not reasons:
        reasons = [f"Phân tích hoàn tất ({direction})"]

    if low_vol:
        reasons.append("⚠️ Biến động thấp — giảm độ tin cậy")
    if very_flat:
        reasons.append("⚠️ ADX thấp — thị trường sideway")

    return SignalResult(
        direction=direction,
        confidence=confidence,
        price=price,
        reasons=reasons,
        indicators={
            "price":          price,
            "ema9":           ema9,
            "ema21":          ema21,
            "ema50":          ema50,
            "rsi":            rsi,
            "macd":           macd,
            "bollinger":      bb,
            "stochastic":     stoch,
            "williams_r":     willy,
            "cci":            cci_v,
            "adx":            adx_v,
            "atr":            atr_v,
            "atr_pct":        round(atr_pct * 100, 4),
            "supertrend":     st,
            "ha_direction":   ha,
            "price_structure": struct,
            "rsi_divergence": div,
            "momentum":       mom,
            "support":        sup,
            "resistance":     res,
            "patterns":       {"bull": bull_p, "bear": bear_p},
            "regime":         "trending" if trending else ("flat" if very_flat else "ranging"),
            "t_call":  round(t_c, 2), "t_put":  round(t_p, 2),
            "o_call":  round(o_c, 2), "o_put":  round(o_p, 2),
            "a_call":  round(a_c, 2), "a_put":  round(a_p, 2),
            "votes":          {"call": call_votes, "put": put_votes},
            "candles":        len(cl),
            "walk_forward":   {"samples": 0, "accuracy": 0.5,
                               "call_accuracy": 0.5, "put_accuracy": 0.5,
                               "edge": 0.0},   # stub – kept for UI compatibility
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# Signal evaluation (unchanged interface)
# ─────────────────────────────────────────────────────────────────────────────

def evaluate_signal(signal: SignalResult, current_price: float) -> Dict[str, Any]:
    won  = current_price > signal.price if signal.direction == "CALL" \
           else current_price < signal.price
    diff = current_price - signal.price
    pct  = (diff / signal.price * 100) if signal.price else 0.0
    return {"won": won, "direction": signal.direction,
            "entry_price": signal.price, "exit_price": current_price,
            "diff": diff, "diff_pct": pct}
