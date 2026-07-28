"""Configuration used by the running Telegram bot.

The active bot imports this module, so keep the shared credentials and broker
settings in the repository's top-level ``config.py`` as requested.
"""

from config import (
    TELEGRAM_BOT_TOKEN,
    POCKETOPTION_SSID,
    POCKETOPTION_DEMO,
    POCKETOPTION_UID,
    POCKETOPTION_PLATFORM,
    GEMINI_API_KEY,
)

# Available timeframes for the bot (in seconds)
TIMEFRAMES = {
    "s15": 15,
    "s30": 30,
    "m1": 60,
    "m3": 180,
    "m5": 300,
}

# Candles used for technical analysis
ANALYSIS_CANDLE_COUNT = 120

# How often to refresh asset lists from the API (seconds)
ASSET_CACHE_TTL = 120

# How many candles to request for result checking
RESULT_CANDLE_COUNT = 5
