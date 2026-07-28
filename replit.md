# Telegram PocketOption Signal Bot

A Telegram bot that connects to the PocketOption binary options trading platform and sends trading signals to users.

## Stack

- **Python 3.12** with `uv` for package management
- **python-telegram-bot** — Telegram bot framework
- **pocketoptionapi-async** — PocketOption WebSocket API client
- **loguru** — structured logging

## Running

```
uv run python main.py
```

The bot uses long-polling (no web server required).

## Configuration

All configuration is loaded from environment variables (set via Replit Secrets):

| Secret | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token from [@BotFather](https://t.me/BotFather) |
| `POCKETOPTION_SSID` | PocketOption session cookie (JSON auth string) |
| `POCKETOPTION_DEMO` | `1` to use demo account, `0` for live (default: `0`) |
| `POCKETOPTION_UID` | PocketOption user ID |
| `POCKETOPTION_PLATFORM` | PocketOption platform ID (default: `2`) |

## Project structure

```
main.py              — Entry point, starts the bot and PocketOption connection
config.py            — Loads configuration from environment variables
telegram_bot/
  bot.py             — Telegram handlers and conversation flow
  analyzer.py        — Technical analysis (candle patterns, signal evaluation)
  pocket_client.py   — PocketOption API wrapper
```

## User preferences

- Keep the existing project structure and stack
