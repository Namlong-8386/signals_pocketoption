---
name: Live price validation
description: Rules for validating PocketOption entry and result prices.
---

Result evaluation must use a live tick whose broker timestamp is newer than the
entry tick. If no fresh tick arrives, the bot should report insufficient price
data rather than classify WIN or LOSS.

**Why:** A price-only cache can return the same stale value at expiry, which
creates false zero-difference results and can incorrectly classify a signal as
LOSS.

**How to apply:** Preserve `(price, broker_timestamp)` for every live tick and
require `timestamp > entry_timestamp` before evaluating a result.