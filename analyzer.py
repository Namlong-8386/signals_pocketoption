"""Root-level re-export — canonical implementation lives in telegram_bot/analyzer.py."""
from telegram_bot.analyzer import (  # noqa: F401
    SignalResult,
    analyze_candles,
    evaluate_signal,
)
