# PocketOption Signal Telegram Bot

A Telegram bot that connects to PocketOption and sends trading signals using technical analysis (EMA, RSI, MACD, Bollinger Bands).

## How to run

```
python main.py
```

The workflow "Start application" is configured to run `python main.py`.

## Project structure

```
main.py                  # Entry point
telegram_bot/
  bot.py                 # Telegram bot handlers and conversation flow
  config.py              # Configuration loaded from env vars / secrets
  analyzer.py            # Technical analysis (EMA, RSI, MACD, Bollinger)
  pocket_client.py       # PocketOption WebSocket client wrapper
  __init__.py
```

## Required secrets

Set these in Replit Secrets before starting:

- `TELEGRAM_BOT_TOKEN` — from @BotFather on Telegram
- `POCKETOPTION_SSID` — your PocketOption session string (the `42["auth",{...}]` format)

## Optional env vars

- `POCKETOPTION_DEMO` — set to `1` to use demo account (default: `0`)
- `POCKETOPTION_UID` — your PocketOption user ID (default: from SSID)
- `POCKETOPTION_PLATFORM` — platform ID (default: `2`)

## User preferences

- Keep credentials in Replit Secrets, never hardcoded in source files.
