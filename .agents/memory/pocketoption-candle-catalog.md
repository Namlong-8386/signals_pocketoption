---
name: PocketOption candle and Forex catalog handling
description: Reliable candle requests and complete Forex menu behavior for PocketOption.
---

Use the PocketOption client's public candle method for symbols present in its catalogue; direct private candle requests are more fragile because response routing depends on websocket stream handlers. For server-only symbols, retain the raw request path as a compatibility fallback.

**Why:** The broker's payout/updateAssets snapshot is not a complete or stable market catalogue. It can omit otherwise requestable REAL pairs or mark them temporarily closed, and direct candle requests can return empty data even for valid pairs.

**How to apply:** Keep the Forex menu based on validated six-letter currency pairs and a curated common-pair universe, rather than filtering only by the current `tradable` flag. Keep OTC restricted to two valid currency codes.