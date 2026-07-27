---
name: PocketOption fallback asset categorization
description: How to categorize assets when the PocketOption server only returns the fallback symbol list.
---

# PocketOption fallback asset categorization

When the server does not send a live `updateAssets` event, the bot falls back to the hardcoded `pocketoptionapi_async.constants.ASSETS` list. Those entries do not include a reliable `type` field, so the API's `type` value is `unknown` for all of them.

**Why:** The bot UI needs to group assets into Forex, Crypto, Stocks, Commodities, Indices, and OTC. Without a live type, we must infer the category from the symbol itself.

**How to apply:**
- OTC: symbol contains `_otc` or `OTC`.
- Stock: symbol starts with `#`.
- Commodity: exact symbol match for `XAUUSD`, `XAGUSD`, `XPTUSD`, `XPDUSD`, `XNGUSD`, `UKBrent`, `USCrude`, or prefix `XAU/XAG/XPT/XPD/XNG`.
- Crypto: exact match or symbols containing `BTC`, `ETH`, `BCH`, `DASH`, `DOT`, `LNK`.
- Index: exact match for `100GBP`, `AEX25`, `AUS200`, `CAC40`, `D30EUR`, `DJI30`, `E35EUR`, `E50EUR`, `F40EUR`, `H33HKD`, `JPN225`, `NASUSD`, `SMI20`, `SP500`, or any symbol containing a digit.
- Forex: everything else.
