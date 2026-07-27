---
name: PocketOption websockets compatibility
description: Why the pocketoptionapi_async library needs an older websockets version on Replit.
---

# PocketOption websockets compatibility

The `pocketoptionapi_async` library passes `extra_headers` to `websockets.connect()`. Starting with `websockets` 14.0, that parameter was renamed to `additional_headers` and `extra_headers` was removed. The library has not been updated for this change.

**Why:** Replit's default environment installs the latest `websockets` (16.x at the time), which causes every PocketOption connection to fail with `BaseEventLoop.create_connection() got an unexpected keyword argument 'extra_headers'`.

**How to apply:** Pin `websockets<14.0` in the project. Version `13.0` works and was verified.
