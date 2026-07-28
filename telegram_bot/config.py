"""Bot configuration loaded from environment variables."""
import os


# Credentials must be provided through Replit Secrets, never committed to source.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
POCKETOPTION_SSID = os.getenv(
    "POCKETOPTION_SSID",
    '''[ "auth", { "session": "a:4:{s:10:\"session_id\";s:32:\"459c46f6ca7a7bbe21999bf1ebd90567\";s:10:\"ip_address\";s:14:\"194.233.82.226\";s:10:\"user_agent\";s:111:\"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36\";s:13:\"last_activity\";i:1784980480;}3570955395f21b7eb7425e4876ae52d5", "isDemo": 0, "uid": 93969941, "platform": 2, "isFastHistory": true, "isOptimized": true } ]''',
)
POCKETOPTION_DEMO = os.getenv("POCKETOPTION_DEMO", "0").lower() in ("1", "true", "yes")
POCKETOPTION_UID = int(os.getenv("POCKETOPTION_UID", "93969941") or "93969941")
POCKETOPTION_PLATFORM = int(os.getenv("POCKETOPTION_PLATFORM", "2") or "2")

# Available timeframes for the bot (in seconds)
TIMEFRAMES = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
}

# Candles used for technical analysis
ANALYSIS_CANDLE_COUNT = 120

# How often to refresh asset lists from the API (seconds)
ASSET_CACHE_TTL = 120

# How many candles to request for result checking
RESULT_CANDLE_COUNT = 5
