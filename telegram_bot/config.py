"""Bot configuration loaded from environment variables."""
import os


# ⚠️ Cảnh báo: lưu token/SSID trực tiếp trong code có rủi ro bảo mật.
# Nên dùng Replit Secrets (TELEGRAM_BOT_TOKEN / POCKETOPTION_SSID) khi triển khai lâu dài.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
POCKETOPTION_SSID = os.getenv("POCKETOPTION_SSID", "")
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
ANALYSIS_CANDLE_COUNT = 80

# How often to refresh asset lists from the API (seconds)
ASSET_CACHE_TTL = 120

# How many candles to request for result checking
RESULT_CANDLE_COUNT = 5
