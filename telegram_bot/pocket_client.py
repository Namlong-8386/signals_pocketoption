"""PocketOption client wrapper used by the Telegram bot."""
import asyncio
import json
import re
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
_FOREX_CURRENCIES = {
    "AED", "AUD", "BRL", "CAD", "CHF", "CNY", "CZK", "DKK", "EUR",
    "GBP", "HKD", "HUF", "IDR", "ILS", "INR", "JPY", "KRW", "MXN",
    "MYR", "NOK", "NZD", "PHP", "PLN", "RON", "RUB", "SAR", "SEK",
    "SGD", "THB", "TRY", "TWD", "USD", "VND", "ZAR",
}

_STOCK_NAME_MARKERS = {
    "APPLE", "AMAZON", "AMERICANEXPRESS", "BOEING", "FACEBOOK",
    "MICROSOFT", "NETFLIX", "TESLA", "TWITTER",
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
    raw = symbol.upper().strip()
    s = raw.replace("_", "")
    is_otc = raw.endswith("_OTC")
    base = raw[:-4] if is_otc else raw
    base_compact = base.replace("_", "")
    # OTC should only contain genuine Forex pairs, e.g. USDJPY_otc.
    # Do not classify OTC indices, crypto, stocks, or commodities as OTC.
    otc_match = re.fullmatch(r"([A-Z]{3})([A-Z]{3})_OTC", raw)
    if otc_match:
        base, quote = otc_match.groups()
        if base in _FOREX_CURRENCIES and quote in _FOREX_CURRENCIES:
            return "otc"
    if raw.startswith("#"):
        return "stock"
    # Some server records use names instead of ticker symbols
    # (Facebook_OTC, Tesla_otc, Microsoft_otc). They are not currency pairs.
    if is_otc and (
        base_compact in _STOCK_NAME_MARKERS
        or not re.fullmatch(r"[A-Z]{6}", base_compact)
    ):
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
        self._candle_request_lock = asyncio.Lock()
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
            # PocketOption currently emits the asset snapshot as updateAssets.
            # Keep the legacy binary name as well for older server/client
            # combinations.
            self._client._websocket.add_event_handler("updateAssets", self._on_assets_update)
            self._client._websocket.add_event_handler("binary_updateAssets", self._on_assets_update)
            self._client._websocket.add_event_handler("json_data", self._on_json_data)
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
            # Wait briefly for the server's asset-status snapshot to arrive.
            # Never seed tradable assets from the library's symbol list:
            # symbols can still emit ticks while trading is closed.
            await asyncio.sleep(3.0)
            if not self._assets:
                logger.warning(
                    "No asset-status snapshot received from server; "
                    "no trading pairs will be shown until one arrives"
                )
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
        updated_assets: Dict[str, Dict[str, Any]] = {}
        count = 0
        for item in data:
            if isinstance(item, list) and len(item) >= 4:
                # list format: [id, symbol, name, type, ?, payout, ...]
                symbol = str(item[1]) if len(item) > 1 else None
                if not symbol:
                    continue
                payout = item[5] if len(item) > 5 else 0
                is_otc = get_asset_category(symbol) == "otc"
                # Field index 14 indicates if asset is open/tradable.
                # Missing status must not be inferred from payout or ticks.
                tradable = item[14] is True if len(item) > 14 else False
                updated_assets[symbol] = {
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
                    updated_assets[str(symbol)] = self._normalize_asset(
                        str(symbol), item
                    )
                    count += 1
        if count:
            # updateAssets is a complete server snapshot. Replace the cache so
            # pairs that just closed are not retained from an older snapshot.
            self._assets = updated_assets
            self._assets_last_update = datetime.now().timestamp()
            logger.info(f"Assets from updateAssets: {count} total")

    @staticmethod
    def _normalize_asset(symbol: str, info: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize an asset record without losing the server's tradable flag."""
        normalized = dict(info)
        normalized["is_otc"] = get_asset_category(symbol) == "otc"
        # A missing status is not proof that the market is open. In particular,
        # payout events can contain only symbol/payout while the server still
        # sends price ticks for a closed market.
        normalized["tradable"] = info.get("tradable") is True
        return normalized

    def _on_payout_update(self, data: Dict[str, Any]) -> None:
        # The server may emit one event per asset or a complete {assets: {...}}
        # snapshot. In both cases preserve the explicit tradable status.
        symbol = data.get("symbol")
        if symbol:
            symbol = str(symbol)
            asset = self._normalize_asset(symbol, data)
            if "tradable" not in data and symbol in self._assets:
                # Partial payout updates must not erase a known status.
                asset["tradable"] = self._assets[symbol].get("tradable") is True
            self._assets[symbol] = asset
            self._assets_last_update = datetime.now().timestamp()
            logger.debug(
                f"Asset updated: {symbol} "
                f"(tradable={self._assets[symbol]['tradable']})"
            )
        else:
            # Fallback: old format with assets dict
            assets = data.get("assets", {})
            if assets:
                if isinstance(assets, dict):
                    self._assets = {
                        str(asset_symbol): self._normalize_asset(
                            str(asset_symbol),
                            asset_info if isinstance(asset_info, dict) else {},
                        )
                        for asset_symbol, asset_info in assets.items()
                    }
                elif isinstance(assets, list):
                    self._on_assets_update(assets)
                self._assets_last_update = datetime.now().timestamp()
        logger.info(f"Asset list updated: {len(self._assets)} assets")

    def is_asset_tradable(self, asset: str) -> bool:
        """Return True only when the latest asset snapshot explicitly says so."""
        return self._assets.get(asset, {}).get("tradable") is True

    def _on_disconnected(self, _data: Any) -> None:
        self._connected = False
        self._latest_prices.clear()
        logger.warning("Disconnected from PocketOption")

    def _on_stream_update(self, data: Any) -> None:
        """Capture live tick prices from the stream so the bot can use the
        current market price as the entry/exit price instead of stale candle
        closes."""
        try:
            if isinstance(data, dict):
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

    def _on_json_data(self, data: Any) -> None:
        """Capture live tick prices from binary JSON attachments.

        PocketOption sends live ticks as [[symbol, timestamp, price], ...].
        This is the primary source of real-time entry/exit prices.
        """
        try:
            if isinstance(data, list):
                # The updateAssets Socket.IO message arrives with a
                # placeholder event followed by its full payload through
                # json_data. Asset rows have an id, symbol, metadata and an
                # explicit open/tradable flag at index 14; they are not ticks.
                asset_rows = [
                    item for item in data
                    if isinstance(item, list)
                    and len(item) > 14
                    and isinstance(item[1], str)
                    and isinstance(item[14], bool)
                ]
                if asset_rows:
                    self._on_assets_update(data)
                    return

                for item in data:
                    if isinstance(item, (list, tuple)) and len(item) >= 3:
                        symbol = str(item[0])
                        price = float(item[-1])
                        self._latest_prices[symbol] = price
                        # Do not log every tick: the broker can emit hundreds
                        # of updates during startup and drown out bot errors.
        except Exception as e:
            logger.debug(f"JSON data parse error: {e}")

    def get_current_price(self, asset: str) -> Optional[float]:
        """Return the latest live tick price for an asset, if available."""
        return self._latest_prices.get(asset)

    async def get_current_price_with_timeout(
        self, asset: str, timeout: float = 2.0
    ) -> Optional[float]:
        """Return the latest live tick price, waiting up to `timeout` seconds
        for the stream to deliver the first tick after subscribing to the asset.
        """
        if asset in self._latest_prices:
            return self._latest_prices[asset]

        deadline = datetime.now().timestamp() + timeout
        while datetime.now().timestamp() < deadline:
            if asset in self._latest_prices:
                return self._latest_prices[asset]
            await asyncio.sleep(0.1)
        return None

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
            [
                sym for sym, info in self._assets.items()
                if get_asset_category(sym) == "otc" and info.get("tradable")
            ]
        )

    def list_real_assets(self) -> List[str]:
        return sorted(
            [
                sym for sym, info in self._assets.items()
                if get_asset_category(sym) == "forex" and info.get("tradable")
            ]
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

        # The library keys pending requests by "{asset}_{timeframe}". Serialize
        # requests so two users selecting the same pair cannot replace the
        # other's Future inside the websocket client.
        async with self._candle_request_lock:
            max_retries = 2
            request_counts = [count, min(count, 60)]
            for attempt in range(max_retries):
                requested_count = request_counts[attempt]
                try:
                    logger.info(
                        f"Requesting {requested_count} candles for {asset} "
                        f"(timeframe={timeframe}, attempt={attempt + 1})"
                    )
                    candles = await asyncio.wait_for(
                        self._client._request_candles(
                            asset, timeframe, requested_count, datetime.now()
                        ),
                        timeout=12.0,
                    )
                    if candles:
                        cache_key = f"{asset}_{timeframe}"
                        self._client._candles_cache[cache_key] = candles
                        logger.info(f"Retrieved {len(candles)} candles for {asset}")
                        return candles
                    logger.warning(
                        f"PocketOption returned no candles for {asset} "
                        f"after requesting {requested_count}"
                    )
                except Exception as e:
                    if "WebSocket is not connected" in str(e) and attempt < max_retries - 1:
                        logger.warning(f"Connection lost during candle request for {asset}, retrying...")
                        if self._client.auto_reconnect:
                            reconnected = await self._client._attempt_reconnection()
                            if reconnected:
                                continue
                    if attempt == max_retries - 1:
                        logger.error(f"Failed to get candles for {asset}: {e}")
                        raise PocketOptionError(f"Failed to get candles: {e}")
            raise PocketOptionError(
                f"PocketOption không trả dữ liệu nến cho {asset}. Vui lòng thử lại."
            )

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
