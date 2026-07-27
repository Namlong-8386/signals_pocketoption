---
name: Telegram bot token conflicts
description: Why a Telegram bot may silently fail when the same token runs on multiple instances.
---

# Telegram bot token conflicts

Telegram only allows one active long-polling session per bot token. If the same token is already running somewhere else (another server, local machine, etc.), the new instance will receive `Conflict: terminated by other getUpdates request` and neither instance will reliably receive updates.

**Why:** This is a Telegram API limitation, not a bug in the bot code. The conflict is silent from the user's perspective unless the logs are checked.

**How to apply:**
- Stop the other running instance before starting the bot on Replit, or
- Create a new bot (or revoke/regenerate the token via @BotFather) so the token is unique to this instance.
