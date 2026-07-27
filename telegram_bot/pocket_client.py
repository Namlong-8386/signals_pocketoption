"""PocketOption client wrapper used by the Telegram bot."""
import asyncio
import json
from typing import Dict, List, Optional, Any
from datetime import datetime

from loguru import logger

from pocketoptionapi_async.client import AsyncPocketOptionClient
from pocketoptionapi_async.models import Candle

from pocketoptionapi_async.constants import ASSETS as LIBRARY_ASSETS

# Hardcoded categorization for the fallback asset list; the server also sends
# asset types for live assets, but the built-in list only contains symbols.
_CRYPTO_SYMBOLS = {
    "BTCUSD", "BTCJPY", "BTCGBP", "ETHUSD", "BCHUSD", "BCHEUR", "BCHGBP",
    "BCHJPY", "DASH_USD", "DOTUSD", "LNKUSD",
}
_INDEX_SYMBOLS = {
    "100GBP", "AEX25", "AUS200", "CAC40", "D30EUR", "DJI30", "E35EUR",
    "E50EUR", "F40EUR", "H33HKD", "JPN225", "NASUSD", "SMI20", "SP500",
}
_COMMODITY_SYMBOLS = {
    "XAUUSD", "XAGUSD", "XPTUSD", "XPDUSD", "XNGUSD", "UKBRENT", "USCRUDE",
}

_CATEGORY_LABELS = {
    "forex": "💱 Forex",
    "crypto": "₿ Crypto",
    "stock": "📈 Cổ phiếu",
    "commodity": "🛢 Hàng hóa",
    "index": "📊 Chỉ số",
    "otc": "🌙 OTC",
}


def get_asset_category(symbol: str) -> str:
    """Categorize an asset symbol into a market group."""
    s = symbol.upper().replace("_", "")
    if "OTC" in s:
        return "otc"
    if symbol.startswith("#"):
        return "stock"
    if s in _COMMODITY_SYMBOLS or s.startswith(("XAU", "XAG", "XPT", "XPD", "XNG")):
        return "commodity"
    if s in _CRYPTO_SYMBOLS or any(c in s for c in ("BTC", "ETH", "BCH", "DASH", "DOT", "LNK")):
        return "crypto"
    if s in _INDEX_SYMBOLS or any(ch.isdigit() for ch in symbol):
        return "index"
    return "forex"


def get_category_label(category: str) -> str:
    return _CATEGORY_LABELS.get(category, category.upper())

from telegram_bot.config import (
    POCKETOPTION_SSID,
    POCKETOPTION_DEMO,
    POCKETOPTION_UID,
    POCKETOPTION_PLATFORM,
    ANALYSIS_CANDLE_COUNT,
    ASSET_CACHE_TTL,
)


class BotPocketClient:
    """Wrapper around the official async client with helper methods for the bot."""

    def __init__(self) -> None:
        self._client: Optional[AsyncPocketOptionClient] = None
        self._assets: Dict[str, Dict[str, Any]] = {}
        self._assets_last_update: Optional[float] = None
        self._connected = False
        self._connection_lock = asyncio.Lock()
        self._latest_prices: Dict[str, float] = {}

    @staticmethod
    def _format_ssid(raw: str) -> str:
        raw = raw.strip()
        if not raw:
            return raw
        # If user pasted the inner array form, normalize it to 42["auth",{...}]
        if raw.startswith('[') and not raw.startswith('42'):
            normalized = f"42{raw}"
        else:
            normalized = raw

        # Remove spaces after opening bracket and before auth
        normalized = normalized.replace('42[ "', '42["').replace('42[  "', '42["')

        # The PocketOption session string is a PHP serialization that contains raw
        # double quotes. The JSON inside the auth message must escape them, so we
        # locate the session value and escape any unescaped quotes inside it.
        normalized = BotPocketClient._escape_session_quotes(normalized)
        return normalized

    @staticmethod
    def _escape_session_quotes(ssid: str) -> str:
        """Escape the raw double quotes inside the PHP-serialized session value."""
        import re

        # Locate the session value boundaries: it ends right before the next top-level key.
        session_start_match = re.search(r'"session"\s*:\s*"', ssid)
        if not session_start_match:
            return ssid

        start = session_start_match.end()  # after opening quote

        # Find the closing quote by locating the next top-level key and walking back.
        next_key_match = re.search(r'"\s*,\s*"[a-zA-Z]+"\s*:', ssid[start:])
        if not next_key_match:
            # fallback: try to find end of object
            next_key_match = re.search(r'"\s*}\s*]', ssid[start:])
        if not next_key_match:
            return ssid

        end = start + next_key_match.start()  # the closing quote position
        session_value = ssid[start:end]
        escaped_session = session_value.replace('"', '\\"')
        return ssid[:start] + escaped_session + ssid[end:]

    async def connect(self, timeout: float = 25.0) -> bool:
        async with self._connection_lock:
            if self._connected and self._client and self._client.is_connected:
                return True

            ssid = self._format_ssid(POCKETOPTION_SSID)
            if not ssid:
                raise RuntimeError(
                    "PocketOption SSID chưa được cấu hình. Vui lòng thiết lập secret POCKETOPTION_SSID."
                )

            masked = ssid[:25] + "..." + ssid[-15:] if len(ssid) > 50 else ssid
            logger.info(f"Connecting to PocketOption with SSID: {masked}")
            self._client = AsyncPocketOptionClient(
                ssid=ssid,
                is_demo=POCKETOPTION_DEMO,
                uid=POCKETOPTION_UID,
                platform=POCKETOPTION_PLATFORM,
                is_fast_history=True,
                persistent_connection=False,
                auto_reconnect=True,
                enable_logging=True,
            )
            # Attach directly to the websocket client so we receive asset data
            self._client._websocket.add_event_handler("payout_update", self._on_payout_update)
            self._client._websocket.add_event_handler("binary_updateAssets", self._on_assets_update)
            self._client.add_event_callback("disconnected", self._on_disconnected)
            self._client.add_event_callback("stream_update", self._on_stream_update)

            try:
                connected = await asyncio.wait_for(self._client.connect(), timeout=timeout)
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"Kết nối PocketOption quá thời gian ({timeout}s). "
                    "SSID có thể hết hạn hoặc bị chặn bởi mạng."
                )

            if not connected:
                raise RuntimeError("Không thể kết nối đến PocketOption. Kiểm tra lại SSID.")

            self._connected = True
            # Wait briefly for asset list to arrive
            await asyncio.sleep(3.0)
            # If no assets arrived from the server, seed from library's hardcoded list
            if not self._assets:
                logger.warning("No asset list received from server — using built-in fallback list")
                self._assets = {
                    sym: {"is_otc": "_otc" in sym, "tradable": True, "id": asset_id}
                    for sym, asset_id in LIBRARY_ASSETS.items()
                }
                self._assets_last_update = datetime.now().timestamp()
                logger.info(f"Fallback asset list loaded: {len(self._assets)} assets")
            logger.info("Connected to PocketOption.")
            return True

    async def ensure_connected(self) -> None:
        if not self._connected or not self._client or not self._client.is_connected:
            await self.connect()

    def _on_assets_update(self, data: Any) -> None:
        """Handle binary_updateAssets event.
        Server format: list of lists — [id, symbol, name, type, ?, payout, ...]
        Example: [5, '#AAPL', 'Apple', 'stock', 2, 50, 60, 30, 3, 0, 170, 0, [], timestamp, is_open, [timeframes]]
        """
        if not isinstance(data, list):
            return
        count = 0
        for item in data:
            if isinstance(item, list) and len(item) >= 4:
                # list format: [id, symbol, name, type, ?, payout, ...]
                symbol = str(item[1]) if len(item) > 1 else None
                if not symbol:
                    continue
                payout = item[5] if len(item) > 5 else 0
                is_otc = "_otc" in symbol
                # field index 14 (if present) indicates if asset is open/tradable
                tradable = bool(item[14]) if len(item) > 14 else (payout > 0)
                self._assets[symbol] = {
                    "id": item[0],
                    "name": str(item[2]) if len(item) > 2 else symbol,
                    "type": str(item[3]) if len(item) > 3 else "",
                    "payout": payout,
                    "is_otc": is_otc,
                    "tradable": tradable,
                }
                count += 1
            elif isinstance(item, dict):
                symbol = item.get("symbol")
                if symbol:
                    self._assets[symbol] = {
                        "is_otc": "_otc" in symbol,
                        "tradable": item.get("tradable", True),
                        **item,
                    }
                    count += 1
        if count:
            self._assets_last_update = datetime.now().timestamp()
            logger.info(f"Assets from updateAssets: {count} total")

    def _on_payout_update(self, data: Dict[str, Any]) -> None:
        # New library emits one event per asset: {id, symbol, name, type, payout}
        symbol = data.get("symbol")
        if symbol:
            is_otc = symbol.endswith("_otc")
            self._assets[symbol] = {
                "id": data.get("id"),
                "name": data.get("name"),
                "type": data.get("type"),
                "payout": data.get("payout"),
                "is_otc": is_otc,
                "tradable": True,
            }
            self._assets_last_update = datetime.now().timestamp()
            logger.debug(f"Asset added: {symbol} (otc={is_otc})")
        else:
            # Fallback: old format with assets dict
            assets = data.get("assets", {})
            if assets:
                self._assets = assets
                self._assets_last_update = datetime.now().timestamp()
        logger.info(f"Asset list updated: {len(self._assets)} assets")

    def _on_disconnected(self, _data: Any) -> None:
        self._connected = False
        self._latest_prices.clear()
        logger.warning("Disconnected from PocketOption")

    def _on_stream_update(self, data: Any) -> None:
        """Capture live tick prices from the stream so the bot can use the
        current market price as the entry/exit price instead of stale candle
        closes."""
        try:
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, (list, tuple)) and len(item) >= 3:
                        symbol = str(item[0])
                        price = float(item[-1])
                        self._latest_prices[symbol] = price
            elif isinstance(data, dict):
                raw = data.get("data") or data.get("candles")
                if isinstance(raw, list):
                    for candle in raw:
                        if isinstance(candle, dict):
                            symbol = candle.get("asset")
                            price = candle.get("close")
                            if symbol is not None and price is not None:
                                self._latest_prices[str(symbol)] = float(price)
        except Exception as e:
            logger.debug(f"Stream update parse error: {e}")

    def get_current_price(self, asset: str) -> Optional[float]:
        """Return the latest live tick price for an asset, if available."""
        return self._latest_prices.get(asset)

    async def get_assets(self, max_wait: float = 10.0) -> Dict[str, Dict[str, Any]]:
        await self.ensure_connected()
        if self._assets_last_update:
            age = datetime.now().timestamp() - self._assets_last_update
            if age < ASSET_CACHE_TTL:
                return self._assets

        # Wait for the payout message to arrive from the server
        waited = 0.0
        while not self._assets and waited < max_wait:
            await asyncio.sleep(0.5)
            waited += 0.5

        return self._assets

    def list_otc_assets(self) -> List[str]:
        return sorted(
            [sym for sym, info in self._assets.items() if info.get("is_otc") and info.get("tradable")]
        )

    def list_real_assets(self) -> List[str]:
        return sorted(
            [sym for sym, info in self._assets.items() if not info.get("is_otc") and info.get("tradable")]
        )

    def list_all_active_assets(self) -> List[str]:
        return sorted([sym for sym, info in self._assets.items() if info.get("tradable")])

    def list_categories(self) -> List[str]:
        """Return non-empty asset categories in a fixed display order.

        The UI only exposes Forex and OTC; other categories are intentionally
        hidden because the user requested a simpler menu.
        """
        order = ["forex", "otc"]
        found = {
            get_asset_category(sym)
            for sym, info in self._assets.items()
            if info.get("tradable")
        }
        return [c for c in order if c in found]

    def list_assets_by_category(self, category: str) -> List[str]:
        """Return sorted tradable assets for a given category."""
        return sorted(
            [sym for sym, info in self._assets.items()
             if get_asset_category(sym) == category and info.get("tradable")]
        )

    async def get_candles(self, asset: str, timeframe: int, count: int = ANALYSIS_CANDLE_COUNT) -> List[Candle]:
        await self.ensure_connected()
        if self._client is None:
            raise RuntimeError("Client chưa kết nối")
        # Bypass the ASSETS validation in the base client because the server
        # exposes many symbols that are not present in the hardcoded list.
        from datetime import datetime
        from pocketoptionapi_async.exceptions import PocketOptionError

        if not self._client.is_connected:
            raise RuntimeError("Chưa kết nối đến PocketOption")

        max_retries = 2
        for attempt in range(max_retries):
            try:
                candles = await self._client._request_candles(asset, timeframe, count, datetime.now())
                cache_key = f"{asset}_{timeframe}"
                self._client._candles_cache[cache_key] = candles
                logger.info(f"Retrieved {len(candles)} candles for {asset}")
                return candles
            except Exception as e:
                if "WebSocket is not connected" in str(e) and attempt < max_retries - 1:
                    logger.warning(f"Connection lost during candle request for {asset}, retrying...")
                    if self._client.auto_reconnect:
                        reconnected = await self._client._attempt_reconnection()
                        if reconnected:
                            continue
                logger.error(f"Failed to get candles for {asset}: {e}")
                raise PocketOptionError(f"Failed to get candles: {e}")
        raise PocketOptionError(f"Failed to get candles after {max_retries} attempts")

    async def get_latest_price(self, asset: str, timeframe: int = 60) -> float:
        """Return the latest close price by fetching a few recent candles."""
        candles = await self.get_candles(asset, timeframe, count=5)
        if not candles:
            raise RuntimeError(f"Không lấy được giá cho {asset}")
        return float(candles[-1].close)

    async def close(self) -> None:
        if self._client:
            await self._client.disconnect()
            self._client = None
        self._connected = False
