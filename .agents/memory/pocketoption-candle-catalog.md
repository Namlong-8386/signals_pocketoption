---
name: PocketOption candle and Forex catalog handling
description: Reliable candle requests and complete Forex menu behavior for PocketOption.
---

Use `BinaryOptionsToolsV2`'s maintained `get_candles_live()` flow for PocketOption history/live candles. The older `pocketoptionapi_async` history endpoints can connect successfully but return no response even for valid active symbols.

**Why:** The broker's payout/updateAssets snapshot is not a complete or stable market catalogue, and the legacy history protocol is no longer reliable. The maintained client successfully returned OHLC data for both REAL and OTC symbols after normalizing the SSID.

**How to apply:** Load active symbols from the maintained client's catalogue. Do not add invented REAL pairs from a hardcoded fallback; a symbol absent from the active catalogue should not appear in the menu. Keep OTC restricted to two valid currency codes, and convert live-candle rows into the project's `Candle` model.