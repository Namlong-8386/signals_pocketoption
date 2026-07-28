"""Technical analysis helpers for PocketOption signals."""
from typing import List, Dict, Any
from dataclasses import dataclass
import math

from pocketoptionapi_async.models import Candle


@dataclass
class SignalResult:
    direction: str  # "CALL", "PUT", or "WAIT"
    confidence: float  # 0.0 - 1.0
    price: float
    reasons: List[str]
    indicators: Dict[str, Any]


def _rsi(closes: List[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        change = closes[-i] - closes[-i - 1]
        if change > 0:
            gains += change
        else:
            losses -= change
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100.0 - (100.0 / (1.0 + rs))


def _ema(values: List[float], period: int) -> List[float]:
    if len(values) < period:
        return values[:]
    multiplier = 2.0 / (period + 1.0)
    ema = [sum(values[:period]) / period]
    for v in values[period:]:
        ema.append((v - ema[-1]) * multiplier + ema[-1])
    return ema


def _macd(closes: List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Dict[str, float]:
    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    # Align slow and fast ema to the same length
    macd_line = [f - s for f, s in zip(ema_fast[-len(ema_slow):], ema_slow)]
    if len(macd_line) < signal:
        return {"macd": 0.0, "signal": 0.0, "histogram": 0.0}
    signal_line = _ema(macd_line, signal)
    return {
        "macd": macd_line[-1],
        "signal": signal_line[-1] if signal_line else macd_line[-1],
        "histogram": macd_line[-1] - signal_line[-1] if signal_line else 0.0,
    }


def _bollinger(closes: List[float], period: int = 20, std_dev: int = 2) -> Dict[str, float]:
    if len(closes) < period:
        return {"upper": closes[-1], "middle": closes[-1], "lower": closes[-1]}
    window = closes[-period:]
    middle = sum(window) / len(window)
    variance = sum((x - middle) ** 2 for x in window) / len(window)
    band = math.sqrt(variance) * std_dev
    return {"upper": middle + band, "middle": middle, "lower": middle - band}


def _mean(values: List[float], period: int) -> float:
    return sum(values[-period:]) / min(period, len(values)) if values else 0.0


def _atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
    if len(closes) < 2:
        return 0.0
    ranges = [
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(1, len(closes))
    ]
    return _mean(ranges, period)


def _stochastic(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> Dict[str, float]:
    if len(closes) < period:
        return {"k": 50.0, "d": 50.0}
    values = []
    for end in range(period, len(closes) + 1):
        high = max(highs[end - period:end])
        low = min(lows[end - period:end])
        values.append(50.0 if high == low else 100.0 * (closes[end - 1] - low) / (high - low))
    return {"k": values[-1], "d": _mean(values, 3)}


def _williams_r(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
    if len(closes) < period:
        return -50.0
    high = max(highs[-period:])
    low = min(lows[-period:])
    return -50.0 if high == low else -100.0 * (high - closes[-1]) / (high - low)


def _candle_patterns(candles: List[Candle]) -> List[str]:
    """Return confirmed patterns on the last candle, if any."""
    if len(candles) < 2:
        return []
    current, previous = candles[-1], candles[-2]
    body = abs(current.close - current.open)
    candle_range = max(current.high - current.low, 1e-12)
    upper = current.high - max(current.open, current.close)
    lower = min(current.open, current.close) - current.low
    previous_body = previous.close - previous.open
    patterns: List[str] = []
    # Doji: body is at most 10% of total range.
    if body / candle_range <= 0.10:
        patterns.append("Doji")
    if lower >= body * 2.0 and upper <= max(body, candle_range * 0.20):
        patterns.append("Hammer")
    if upper >= body * 2.0 and lower <= max(body, candle_range * 0.20):
        patterns.append("Shooting Star")
    if (
        current.close > current.open
        and previous_body < 0
        and current.open <= previous.close
        and current.close >= previous.open
    ):
        patterns.append("Bullish Engulfing")
    if (
        current.close < current.open
        and previous_body > 0
        and current.open >= previous.close
        and current.close <= previous.open
    ):
        patterns.append("Bearish Engulfing")
    return patterns


def _cci(highs: List[float], lows: List[float], closes: List[float], period: int = 20) -> float:
    if len(closes) < period:
        return 0.0
    typical = [(h + l + c) / 3.0 for h, l, c in zip(highs, lows, closes)]
    window = typical[-period:]
    mean = sum(window) / period
    deviation = sum(abs(value - mean) for value in window) / period
    return 0.0 if deviation == 0 else (window[-1] - mean) / (0.015 * deviation)


def _adx(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> Dict[str, float]:
    if len(closes) < period + 2:
        return {"adx": 0.0, "plus_di": 0.0, "minus_di": 0.0}
    trs, plus_dm, minus_dm = [], [], []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
        up, down = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
    tr = _mean(trs, period)
    plus = 100.0 * _mean(plus_dm, period) / tr if tr else 0.0
    minus = 100.0 * _mean(minus_dm, period) / tr if tr else 0.0
    adx = 100.0 * abs(plus - minus) / (plus + minus) if plus + minus else 0.0
    return {"adx": adx, "plus_di": plus, "minus_di": minus}


def _momentum(closes: List[float]) -> Dict[str, float]:
    """Return normalized momentum over several horizons.

    Combining short and medium horizons avoids treating one candle as the whole
    market trend while still reacting quickly enough for the selected timeframe.
    """
    if len(closes) < 10:
        return {"short": 0.0, "medium": 0.0, "agreement": 0.0}
    base = max(abs(closes[-1]), 1e-12)
    short = (closes[-1] - closes[-4]) / base
    medium = (closes[-1] - closes[-10]) / base
    agreement = 1.0 if short * medium > 0 else 0.0
    return {"short": short, "medium": medium, "agreement": agreement}


def _walk_forward_validation(
    candles: List[Candle], min_history: int = 40
) -> Dict[str, Any]:
    """Measure the strategy on prior candles without looking into the future.

    For every historical point, only candles before that point are passed to the
    analyzer.  The next candle is then used as the outcome.  This is deliberately
    a small walk-forward test, not a claim that past results predict the market.
    """
    if len(candles) < min_history + 8:
        return {"samples": 0, "accuracy": 0.5, "call_accuracy": 0.5,
                "put_accuracy": 0.5, "edge": 0.0}

    wins = calls = puts = call_wins = put_wins = 0
    # Leave a little history for indicators while keeping the live request fast.
    first = min_history
    for point in range(first, len(candles) - 1):
        historical = analyze_candles(candles[:point], run_validation=False)
        if historical.direction == "WAIT":
            continue
        actual_up = candles[point].close > candles[point - 1].close
        predicted_up = historical.direction == "CALL"
        won = predicted_up == actual_up
        wins += int(won)
        if predicted_up:
            calls += 1
            call_wins += int(won)
        else:
            puts += 1
            put_wins += int(won)

    samples = calls + puts
    accuracy = wins / samples if samples else 0.5
    call_accuracy = call_wins / calls if calls else 0.5
    put_accuracy = put_wins / puts if puts else 0.5
    return {
        "samples": samples,
        "accuracy": round(accuracy, 3),
        "call_accuracy": round(call_accuracy, 3),
        "put_accuracy": round(put_accuracy, 3),
        "edge": round(accuracy - 0.5, 3),
    }


def analyze_candles(
    candles: List[Candle], run_validation: bool = True
) -> SignalResult:
    """Analyze a full candle history and optionally calibrate it walk-forward."""
    if not candles:
        raise ValueError("No candles provided for analysis")

    sorted_candles = sorted(candles, key=lambda c: c.timestamp)
    closes = [c.close for c in sorted_candles]
    highs = [c.high for c in sorted_candles]
    lows = [c.low for c in sorted_candles]
    current_price = closes[-1]

    # Need enough data
    if len(closes) < 30:
        return SignalResult(
            direction="WAIT",
            confidence=0.5,
            price=current_price,
            reasons=["WAIT: chưa đủ lịch sử để kiểm định chiến lược"],
            indicators={"price": current_price, "candles": len(closes)},
        )

    ema9 = _ema(closes, 9)
    ema21 = _ema(closes, 21)
    rsi_val = _rsi(closes, 14)
    macd_val = _macd(closes)
    bb = _bollinger(closes)
    stochastic = _stochastic(highs, lows, closes)
    williams = _williams_r(highs, lows, closes)
    cci_val = _cci(highs, lows, closes)
    atr_val = _atr(highs, lows, closes)
    adx = _adx(highs, lows, closes)
    momentum = _momentum(closes)
    ema9_current = ema9[-1] if ema9 else current_price
    ema21_current = ema21[-1] if ema21 else current_price
    last_5 = closes[-5:]
    trend_up = sum(last_5[i] > last_5[i - 1] for i in range(1, len(last_5)))
    trend_down = sum(last_5[i] < last_5[i - 1] for i in range(1, len(last_5)))
    patterns = _candle_patterns(sorted_candles)
    call_score = put_score = 0.0
    reasons: List[str] = []

    # Trend strategy: EMA9/21, MACD and ADX must agree.
    if ema9_current > ema21_current:
        call_score += 2.0
        reasons.append("Trend tăng: EMA9 trên EMA21")
    else:
        put_score += 2.0
        reasons.append("Trend giảm: EMA9 dưới EMA21")
    if macd_val["histogram"] > 0:
        call_score += 1.5
        reasons.append("MACD xác nhận đà tăng")
    else:
        put_score += 1.5
        reasons.append("MACD xác nhận đà giảm")
    if adx["adx"] >= 20:
        if adx["plus_di"] > adx["minus_di"]:
            call_score += 1.2
            reasons.append(f"ADX {adx['adx']:.1f}: lực mua chiếm ưu thế")
        else:
            put_score += 1.2
            reasons.append(f"ADX {adx['adx']:.1f}: lực bán chiếm ưu thế")

    # RSI is directional only at meaningful extremes. In a strong trend,
    # overbought/oversold is usually momentum rather than an immediate reversal,
    # so do not let RSI generate a counter-trend vote.
    trending = adx["adx"] >= 25
    if rsi_val <= 35:
        if trending and ema9_current < ema21_current:
            put_score += 0.7
            reasons.append(f"RSI {rsi_val:.1f}: động lượng giảm trong trend mạnh")
        elif not trending:
            call_score += 1.2
            reasons.append(f"RSI {rsi_val:.1f}: vùng quá bán")
    elif rsi_val >= 65:
        if trending and ema9_current > ema21_current:
            call_score += 0.7
            reasons.append(f"RSI {rsi_val:.1f}: động lượng tăng trong trend mạnh")
        elif not trending:
            put_score += 1.2
            reasons.append(f"RSI {rsi_val:.1f}: vùng quá mua")
    elif rsi_val > 52:
        call_score += 0.3
    elif rsi_val < 48:
        put_score += 0.3

    # Multi-horizon momentum confirms a trend only when short and medium
    # horizons point in the same direction.
    if momentum["agreement"]:
        if momentum["short"] > 0:
            call_score += 0.8
            reasons.append("Động lượng ngắn và trung hạn cùng tăng")
        elif momentum["short"] < 0:
            put_score += 0.8
            reasons.append("Động lượng ngắn và trung hạn cùng giảm")

    # Channel strategy: reward only entries near Bollinger edges.
    width = max(bb["upper"] - bb["lower"], 1e-12)
    position = (current_price - bb["lower"]) / width
    if position <= 0.15 and not (trending and ema9_current < ema21_current):
        call_score += 1.8
        reasons.append("Bollinger: giá sát dải dưới")
    elif position >= 0.85 and not (trending and ema9_current > ema21_current):
        put_score += 1.8
        reasons.append("Bollinger: giá sát dải trên")
    # Oscillator strategy: Stochastic and CCI.
    oscillator_weight = 0.65 if trending else 1.0
    if stochastic["k"] <= 20 and stochastic["k"] >= stochastic["d"]:
        call_score += 1.4 * oscillator_weight
        reasons.append(f"Stochastic {stochastic['k']:.1f}: bật lên")
    elif stochastic["k"] >= 80 and stochastic["k"] <= stochastic["d"]:
        put_score += 1.4 * oscillator_weight
        reasons.append(f"Stochastic {stochastic['k']:.1f}: quay đầu")
    if cci_val <= -100:
        call_score += 1.0 * oscillator_weight
        reasons.append(f"CCI {cci_val:.0f}: quá bán")
    elif cci_val >= 100:
        put_score += 1.0 * oscillator_weight
        reasons.append(f"CCI {cci_val:.0f}: quá mua")

    # Williams %R confirms the oscillator reversal zone.
    if williams <= -80:
        call_score += 1.0 * oscillator_weight
        reasons.append(f"Williams %R {williams:.1f}: quá bán")
    elif williams >= -20:
        put_score += 1.0 * oscillator_weight
        reasons.append(f"Williams %R {williams:.1f}: quá mua")

    # Candle confirmation: engulfing, rejection wick, then strong body.
    candle, previous = sorted_candles[-1], sorted_candles[-2]
    body = candle.close - candle.open
    previous_body = previous.close - previous.open
    candle_range = max(candle.high - candle.low, 1e-12)
    body_ratio = abs(body) / candle_range
    upper_wick = candle.high - max(candle.open, candle.close)
    lower_wick = min(candle.open, candle.close) - candle.low
    bullish_engulf = body > 0 and previous_body < 0 and candle.close >= previous.open and candle.open <= previous.close
    bearish_engulf = body < 0 and previous_body > 0 and candle.close <= previous.open and candle.open >= previous.close
    if "Bullish Engulfing" in patterns or "Hammer" in patterns:
        call_score += 1.0
        reasons.append("Mẫu nến CALL: " + ", ".join(
            p for p in patterns if p in ("Bullish Engulfing", "Hammer")
        ))
    elif "Bearish Engulfing" in patterns or "Shooting Star" in patterns:
        put_score += 1.0
        reasons.append("Mẫu nến PUT: " + ", ".join(
            p for p in patterns if p in ("Bearish Engulfing", "Shooting Star")
        ))
    elif "Doji" in patterns:
        reasons.append("Doji: thị trường lưỡng lự, giảm độ tin cậy")
    elif body_ratio >= 0.55:
        if body > 0:
            call_score += 0.45
            reasons.append("Nến động lượng tăng")
        else:
            put_score += 0.45
            reasons.append("Nến động lượng giảm")

    # Market structure: support/resistance and short-term momentum.
    support, resistance = min(lows[-20:]), max(highs[-20:])
    span = max(resistance - support, 1e-12)
    if current_price <= support + span * 0.12:
        call_score += 1.0
        reasons.append("Giá gần hỗ trợ 20 nến")
    elif current_price >= resistance - span * 0.12:
        put_score += 1.0
        reasons.append("Giá gần kháng cự 20 nến")
    if trend_up >= 3:
        call_score += 0.6
    elif trend_down >= 3:
        put_score += 0.6

    total = call_score + put_score
    direction = "CALL" if call_score > put_score else "PUT"
    winning_score = max(call_score, put_score)
    losing_score = min(call_score, put_score)
    # Margin is more meaningful than winner/total when several indicators are
    # neutral. Keep confidence bounded so it remains a calibrated score.
    confidence = min(
        0.95,
        max(0.5, 0.5 + (winning_score - losing_score) / max(total, 1.0)),
    )

    validation = (
        _walk_forward_validation(sorted_candles)
        if run_validation
        else {"samples": 0, "accuracy": 0.5, "call_accuracy": 0.5,
              "put_accuracy": 0.5, "edge": 0.0}
    )
    # Use walk-forward results to calibrate both direction and confidence.
    # Previously this only changed the direction when the technical confidence
    # was already low, so a technically strong but historically poor direction
    # could still be emitted (for example PUT 80% with PUT accuracy 53% while
    # CALL accuracy was 80%).  That makes the displayed confidence misleading.
    if validation["samples"] >= 12:
        call_accuracy = validation["call_accuracy"]
        put_accuracy = validation["put_accuracy"]
        historical_accuracy = call_accuracy if direction == "CALL" else put_accuracy
        opposite_accuracy = put_accuracy if direction == "CALL" else call_accuracy
        if validation["samples"] >= 20:
            # Prefer the better validated side when the difference is larger
            # than a small noise margin. Never force a side whose own measured
            # accuracy is below 50%.
            if opposite_accuracy - historical_accuracy >= 0.04:
                direction = "PUT" if direction == "CALL" else "CALL"
                historical_accuracy = opposite_accuracy
                reasons.append("Walk-forward đổi sang hướng có độ chính xác tốt hơn")
            if historical_accuracy < 0.50:
                direction = "WAIT"
                confidence = 0.50
                reasons.append("WAIT: hướng hiện tại có kiểm định dưới 50%")
        if direction != "WAIT":
            # Validation is the stronger signal for the displayed confidence;
            # technical score is only a secondary confirmation.
            confidence = (confidence * 0.35) + (historical_accuracy * 0.65)
        reasons.append(
            f"Backtest lăn {validation['samples']} mẫu: "
            f"{validation['accuracy'] * 100:.0f}%"
        )

    # Do not force a trade when there is no directional edge.  The lower
    # threshold reduces unnecessary WAIT responses while retaining a tie guard.
    if call_score == put_score or confidence < 0.54:
        direction = "WAIT"
        confidence = round(confidence, 2)
        reasons.append("WAIT: tín hiệu chưa có lợi thế đủ rõ")

    return SignalResult(
        direction=direction,
        confidence=confidence,
        price=current_price,
        reasons=reasons,
        indicators={
            "price": current_price,
            "ema9": ema9_current,
            "ema21": ema21_current,
            "rsi": rsi_val,
            "macd": macd_val,
            "bollinger": bb,
            "stochastic": stochastic,
            "williams_r": williams,
            "cci": cci_val,
            "adx": adx,
            "atr": atr_val,
            "momentum": momentum,
            "support": support,
            "resistance": resistance,
            "call_score": round(call_score, 2),
            "put_score": round(put_score, 2),
            "patterns": patterns,
            "trend_up": trend_up,
            "trend_down": trend_down,
            "candles": len(closes),
            "walk_forward": validation,
        },
    )


def evaluate_signal(signal: SignalResult, current_price: float) -> Dict[str, Any]:
    """Compare current price against the signal price to determine win/loss."""
    if signal.direction == "CALL":
        won = current_price > signal.price
    else:  # PUT
        won = current_price < signal.price

    diff = current_price - signal.price
    pct = (diff / signal.price) * 100 if signal.price else 0.0

    return {
        "won": won,
        "direction": signal.direction,
        "entry_price": signal.price,
        "exit_price": current_price,
        "diff": diff,
        "diff_pct": pct,
    }
