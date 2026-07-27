"""Technical analysis helpers for PocketOption signals."""
from typing import List, Dict, Any
from dataclasses import dataclass
import math

from pocketoptionapi_async.models import Candle


@dataclass
class SignalResult:
    direction: str  # "CALL" or "PUT"
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


def analyze_candles(candles: List[Candle]) -> SignalResult:
    """Analyze a list of candles and return a CALL/PUT signal."""
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
            direction="CALL",
            confidence=0.5,
            price=current_price,
            reasons=["Không đủ nến để phân tích sâu"],
            indicators={"price": current_price, "candles": len(closes)},
        )

    ema9 = _ema(closes, 9)
    ema21 = _ema(closes, 21)
    rsi_val = _rsi(closes, 14)
    macd_val = _macd(closes)
    bb = _bollinger(closes)

    ema9_current = ema9[-1] if ema9 else current_price
    ema21_current = ema21[-1] if ema21 else current_price

    # Trend strength by recent candles
    last_5 = closes[-5:]
    trend_up = sum(1 for i in range(1, len(last_5)) if last_5[i] > last_5[i - 1])
    trend_down = sum(1 for i in range(1, len(last_5)) if last_5[i] < last_5[i - 1])

    call_score = 0.0
    put_score = 0.0
    reasons: List[str] = []

    # EMA crossover / trend
    if ema9_current > ema21_current:
        call_score += 1.0
        reasons.append(f"EMA9 ({ema9_current:.5f}) > EMA21 ({ema21_current:.5f})")
    else:
        put_score += 1.0
        reasons.append(f"EMA9 ({ema9_current:.5f}) < EMA21 ({ema21_current:.5f})")

    # RSI
    if rsi_val < 70:
        call_score += 0.7
    else:
        put_score += 0.5
        reasons.append(f"RSI={rsi_val:.1f} vùng quá mua")

    if rsi_val > 30:
        put_score += 0.7
    else:
        call_score += 0.5
        reasons.append(f"RSI={rsi_val:.1f} vùng quá bán")

    # MACD
    histogram = macd_val.get("histogram", 0.0)
    if histogram > 0:
        call_score += 0.8
        reasons.append(f"MACD histogram dương ({histogram:.6f})")
    else:
        put_score += 0.8
        reasons.append(f"MACD histogram âm ({histogram:.6f})")

    # Bollinger position
    if current_price < bb["lower"]:
        call_score += 0.8
        reasons.append("Giá chạm dải Bollinger dưới - có khả năng hồi")
    elif current_price > bb["upper"]:
        put_score += 0.8
        reasons.append("Giá chạm dải Bollinger trên - có khả năng điều chỉnh")
    else:
        call_score += 0.3
        put_score += 0.3

    # Recent short trend
    if trend_up >= 3:
        call_score += 0.6
        reasons.append(f"Xu hướng 5 nến gần nhất tăng ({trend_up}/4)")
    if trend_down >= 3:
        put_score += 0.6
        reasons.append(f"Xu hướng 5 nến gần nhất giảm ({trend_down}/4)")

    total = call_score + put_score
    if total == 0:
        direction = "CALL"
        confidence = 0.5
    else:
        if call_score > put_score:
            direction = "CALL"
            confidence = round(call_score / total, 2)
        else:
            direction = "PUT"
            confidence = round(put_score / total, 2)

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
            "trend_up": trend_up,
            "trend_down": trend_down,
            "candles": len(closes),
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
