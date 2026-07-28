---
name: Signal analysis policy
description: Durable rules for improving the trading signal analyzer without overfitting or counter-trend noise.
---

The analyzer should validate historical decisions walk-forward, use only candles available before each prediction, and treat the result as calibration rather than a guarantee.

**Why:** Indicator votes can conflict in strong trends; treating overbought/oversold as an automatic reversal produced weak or WAIT-heavy signals. Flat synthetic/low-edge markets should remain filtered rather than forced.

**How to apply:** Keep trend confirmation (EMA/MACD/ADX and multi-horizon momentum) dominant in strong trends, reduce counter-trend oscillator weight there, and retain a WAIT outcome when directional margin is genuinely weak. For expiry checks, never accept a candle whose close is before the expiry timestamp; wait for the matching broker candle or leave the result unclassified.