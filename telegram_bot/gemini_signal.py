"""Gemini AI signal validation for PocketOption signals.

Sau khi phân tích kỹ thuật xong, Gemini đọc toàn bộ các chỉ báo và đưa ra
nhận định độc lập (CALL / PUT / WAIT) cùng mức tin cậy 0-100.

Blending logic
──────────────
• Gemini đồng ý với TA  → confidence = TA*0.55 + Gemini*0.45, hướng giữ nguyên
• Gemini nói WAIT       → confidence -= 0.10, flip về WAIT nếu < MIN_CONF
• Gemini bất đồng TA    → chuyển WAIT; Gemini không được tự ý đảo chiều TA

Nếu API lỗi / timeout → giữ nguyên tín hiệu TA, ghi warning.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

import httpx
from loguru import logger

from telegram_bot.analyzer import SignalResult
from telegram_bot.config import GEMINI_API_KEY

_GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.0-flash:generateContent?key={key}"
)
_TIMEOUT   = 7.0   # seconds
_MIN_CONF  = 0.62  # same threshold as analyzer


def _build_prompt(signal: SignalResult, asset: str, timeframe: str) -> str:
    ind = signal.indicators
    adx = ind.get("adx", {})
    macd = ind.get("macd", {})
    st   = ind.get("supertrend", {})
    bb   = ind.get("bollinger", {})
    stch = ind.get("stochastic", {})
    mom  = ind.get("momentum", {})
    pats = ind.get("patterns", {})
    bull_p = pats.get("bull", []) if isinstance(pats, dict) else []
    bear_p = pats.get("bear", []) if isinstance(pats, dict) else []
    all_p  = bull_p + bear_p

    return f"""You are an expert binary-options trading analyst. Evaluate the following technical snapshot and decide whether to trade CALL, PUT, or WAIT.

Asset: {asset}   Timeframe: {timeframe}
Current price: {ind.get('price', signal.price):.6f}

── Trend indicators ────────────────────────────────
EMA9:  {ind.get('ema9', 0):.6f}   EMA21: {ind.get('ema21', 0):.6f}   EMA50: {ind.get('ema50', 0):.6f}
MACD histogram: {macd.get('histogram', 0):.7f}   crossover: {macd.get('crossover', 0):+d}
Supertrend direction: {'BULLISH' if st.get('direction') == 1 else 'BEARISH' if st.get('direction') == -1 else 'NONE'}  value: {st.get('value', 0):.6f}
ADX: {adx.get('adx', 0):.1f}   +DI: {adx.get('plus_di', 0):.1f}   -DI: {adx.get('minus_di', 0):.1f}
Heikin-Ashi direction: {ind.get('ha_direction', 0):+d}   Price structure: {ind.get('price_structure', 0):+d}
Market regime: {ind.get('regime', 'unknown')}

── Oscillators ─────────────────────────────────────
RSI(14):         {ind.get('rsi', 50):.1f}
Stochastic K/D:  {stch.get('k', 50):.1f} / {stch.get('d', 50):.1f}
Williams %R:     {ind.get('williams_r', -50):.1f}
CCI(20):         {ind.get('cci', 0):.0f}
RSI divergence:  {ind.get('rsi_divergence', 0):+d}  (+1=bullish, -1=bearish)
Momentum short:  {mom.get('short', 0):.5f}   medium: {mom.get('medium', 0):.5f}

── Price action ─────────────────────────────────────
Bollinger %B: {bb.get('pct_b', 0.5):.3f}  (0=lower band, 1=upper band)
Support:      {ind.get('support', 0):.6f}   Resistance: {ind.get('resistance', 0):.6f}
Candlestick patterns: {', '.join(all_p) if all_p else 'none'}

── TA engine vote ───────────────────────────────────
Direction: {signal.direction}   Confidence: {int(signal.confidence * 100)}%
Category votes  CALL={ind.get('votes', {}).get('call', 0)}  PUT={ind.get('votes', {}).get('put', 0)}  (out of 3)
TA reasons: {'; '.join(signal.reasons[:4])}

── Your task ────────────────────────────────────────
Respond ONLY with a JSON object (no markdown, no extra text):
{{
  "direction": "CALL" or "PUT" or "WAIT",
  "confidence": <integer 0-100>,
  "reason": "<one concise sentence in Vietnamese, ≤ 25 words>"
}}
"""


async def gemini_enhance_signal(
    signal: SignalResult,
    asset: str,
    timeframe: str,
) -> SignalResult:
    """Call Gemini, blend its verdict with the TA signal, return updated SignalResult."""
    if not GEMINI_API_KEY:
        logger.debug("GEMINI_API_KEY not set — skipping AI enhancement")
        return signal

    prompt = _build_prompt(signal, asset, timeframe)
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 120,
            "responseMimeType": "application/json",
        },
    }

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                _GEMINI_URL.format(key=GEMINI_API_KEY),
                json=payload,
            )
            resp.raise_for_status()
    except httpx.TimeoutException:
        logger.warning("Gemini API timeout — using pure TA signal")
        return signal
    except httpx.HTTPStatusError as exc:
        logger.warning(f"Gemini API HTTP {exc.response.status_code} — using pure TA signal")
        return signal
    except Exception as exc:
        logger.warning(f"Gemini API error: {exc} — using pure TA signal")
        return signal

    # ── Parse response ────────────────────────────────────────────────────
    gemini_data = _parse_gemini_response(resp.json())
    if gemini_data is None:
        logger.warning("Could not parse Gemini response — using pure TA signal")
        return signal

    g_dir  = gemini_data.get("direction", "WAIT").upper()
    g_conf = max(0, min(100, int(gemini_data.get("confidence", 50)))) / 100.0
    g_rsn  = gemini_data.get("reason", "")

    logger.info(
        f"Gemini [{asset} {timeframe}]: {g_dir} {int(g_conf*100)}% — {g_rsn}"
    )

    # ── Blend ─────────────────────────────────────────────────────────────
    ta_dir  = signal.direction
    ta_conf = signal.confidence
    new_dir  = ta_dir
    new_conf = ta_conf

    if g_dir == "WAIT":
        new_conf = ta_conf - 0.10

    elif g_dir == ta_dir:
        # Agreement → weighted blend
        new_conf = ta_conf * 0.55 + g_conf * 0.45

    else:
        # AI is a second opinion, not a signal generator. Flipping a
        # short-expiry trade based on one language-model response creates
        # precisely the late entries we want to avoid.
        new_dir = "WAIT"
        new_conf = 0.50
        logger.info(
            f"Gemini vetoed TA: {ta_dir}→WAIT "
            f"(TA conf {ta_conf:.2f}, Gemini conf {g_conf:.2f})"
        )

    new_conf = round(min(0.95, max(0.50, new_conf)), 2)

    # Apply WAIT threshold
    if new_dir != "WAIT" and new_conf < _MIN_CONF:
        new_dir  = "WAIT"

    # Build updated reasons
    new_reasons = list(signal.reasons)
    if g_rsn:
        new_reasons.append(f"🤖 Gemini AI: {g_rsn}")
    new_reasons.append(
        f"🤖 Gemini xác nhận: {g_dir} ({int(g_conf*100)}%)"
        if g_dir == new_dir
        else f"🤖 Gemini bất đồng TA: {g_dir} ({int(g_conf*100)}%)"
    )

    return SignalResult(
        direction=new_dir,
        confidence=new_conf,
        price=signal.price,
        reasons=new_reasons,
        indicators={**signal.indicators, "gemini": gemini_data},
    )


def _parse_gemini_response(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract the JSON payload from a Gemini generateContent response."""
    try:
        text = raw["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return None

    # Strip markdown fences if present
    text = re.sub(r"```(?:json)?|```", "", text).strip()
    try:
        data = json.loads(text)
        if "direction" in data:
            return data
    except (json.JSONDecodeError, ValueError):
        pass

    # Fallback: regex extraction
    m_dir  = re.search(r'"direction"\s*:\s*"(CALL|PUT|WAIT)"', text, re.I)
    m_conf = re.search(r'"confidence"\s*:\s*(\d+)',            text)
    m_rsn  = re.search(r'"reason"\s*:\s*"([^"]+)"',            text)
    if m_dir:
        return {
            "direction":  m_dir.group(1).upper(),
            "confidence": int(m_conf.group(1)) if m_conf else 50,
            "reason":     m_rsn.group(1) if m_rsn else "",
        }
    return None
