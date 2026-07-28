"""PocketOption client wrapper used by the Telegram bot."""
import asyncio
import json
import re
from collections import defaultdict
from typing import Dict, List, Optional, Any
from datetime import datetime

from loguru import logger

from pocketoptionapi_async.client import AsyncPocketOptionClient
from pocketoptionapi_async.models import Candle

from pocketoptionapi_async.constants import ASSETS as LIBRARY_ASSETS
from BinaryOptionsToolsV2.pocketoption import PocketOptionAsync

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

_COMMON_FOREX_PAIRS = {
    # Major and commonly offered minor pairs.  The broker sometimes omits
    # REAL pairs from its payout snapshot even though candle history supports
    # them, so the menu must not depend exclusively on that snapshot.
    "AUDCAD", "AUDCHF", "AUDJPY", "AUDNZD", "AUDUSD",
    "CADCHF", "CADJPY", "CHFJPY", "EURAUD", "EURCAD",
    "EURCHF", "EURGBP", "EURJPY", "EURNZD", "EURUSD",
    "GBPAUD", "GBPCAD", "GBPCHF", "GBPJPY", "GBPNZD", "GBPUSD",
    "NZDCAD", "NZDJPY", "NZDUSD", "USDCAD", "USDCHF",
    "USDJPY", "USDMXN", "USDNOK", "USDPLN", "USDSEK", "USDTRY",
}

# The live PocketOption catalogue currently exposes this REAL set. Do not
# invent extra REAL symbols from the library fallback: a symbol missing from
# the broker catalogue will always fail candle requests.
_ACTIVE_REAL_FOREX_PAIRS = {
    "AUDCAD", "AUDCHF", "AUDJPY", "AUDUSD", "CADCHF", "CADJPY",
    "CHFJPY", "EURAUD", "EURCAD", "EURCHF", "EURGBP", "EURJPY",
    "EURUSD", "GBPAUD", "GBPCAD", "GBPCHF", "GBPJPY", "GBPUSD",
    "USDCAD", "USDCHF", "USDJPY",
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


def is_forex_pair(symbol: str) -> bool:
    """Return True only for a plain six-letter currency pair."""
    raw = symbol.upper().strip()
    return (
        re.fullmatch(r"[A-Z]{6}", raw) is not None
        and raw[:3] in _FOREX_CURRENCIES
        and raw[3:] in _FOREX_CURRENCIES
    )


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
        self._history_client: Optional[PocketOptionAsync] = None
        self._assets: Dict[str, Dict[str, Any]] = {}
        self._assets_last_update: Optional[float] = None
        self._connected = False
        self._connection_lock = asyncio.Lock()
        self._candle_request_lock = asyncio.Lock()
        self._latest_prices: Dict[str, float] = {}
        self._history_request_index = 1
        self._history_requests: Dict[int, Dict[str, Any]] = {}

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
            # The legacy client still provides the live tick callbacks used by
            # the bot, while the maintained upstream client handles the
            # current history/live-candle protocol.
            try:
                self._history_client = PocketOptionAsync(ssid)
                await asyncio.wait_for(self._history_client.connect(), timeout=timeout)
                await asyncio.wait_for(
                    self._history_client.wait_for_assets(timeout=10.0),
                    timeout=12.0,
                )
                active_assets = await self._history_client.active_assets()
                self._assets = {
                    item["symbol"]: {
                        "is_otc": bool(item.get("is_otc")),
                        "tradable": bool(item.get("is_active", True)),
                        "payout": item.get("payout"),
                        "asset_type": item.get("asset_type"),
                        "id": item.get("id"),
                    }
                    for item in active_assets
                    if isinstance(item, dict) and item.get("symbol")
                }
                self._assets_last_update = datetime.now().timestamp()
                logger.info(f"Loaded {len(self._assets)} active assets from PocketOption")
            except Exception as exc:
                self._history_client = None
                logger.warning(f"Could not load maintained asset catalog: {exc}")
            # Wait briefly for asset list to arrive
            if not self._assets:
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
            # Keep the normal REAL currency universe available.  The server's
            # payout/updateAssets packet is not a complete market catalogue
            # and may mark an otherwise requestable pair as closed temporarily.
            for symbol in _ACTIVE_REAL_FOREX_PAIRS:
                self._assets.setdefault(
                    symbol,
                    {"is_otc": False, "tradable": True, "id": LIBRARY_ASSETS.get(symbol)},
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
        count = 0
        for item in data:
            if isinstance(item, list) and len(item) >= 4:
                # list format: [id, symbol, name, type, ?, payout, ...]
                symbol = str(item[1]) if len(item) > 1 else None
                if not symbol:
                    continue
                payout = item[5] if len(item) > 5 else 0
                is_otc = get_asset_category(symbol) == "otc"
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
                        "is_otc": get_asset_category(str(symbol)) == "otc",
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
            is_otc = get_asset_category(symbol) == "otc"
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

    @staticmethod
    def _parse_history_rows(rows: Any, asset: str, timeframe: int) -> List[Candle]:
        """Parse binary history rows returned by PocketOption."""
        if not isinstance(rows, list):
            return []
        candles: List[Candle] = []
        ticks: List[tuple[float, float]] = []
        for row in rows:
            try:
                if isinstance(row, dict):
                    timestamp = row.get("time", row.get("timestamp"))
                    if timestamp is None:
                        continue
                    if all(row.get(key) is not None for key in ("open", "close", "high", "low")):
                        candles.append(Candle(
                            timestamp=datetime.fromtimestamp(float(timestamp)),
                            open=float(row["open"]),
                            high=float(row["high"]),
                            low=float(row["low"]),
                            close=float(row["close"]),
                            volume=float(row.get("volume") or 0),
                            asset=asset,
                            timeframe=timeframe,
                        ))
                    elif row.get("price") is not None:
                        ticks.append((float(timestamp), float(row["price"])))
                elif isinstance(row, (list, tuple)) and len(row) >= 5:
                    candles.append(Candle(
                        timestamp=datetime.fromtimestamp(float(row[0])),
                        open=float(row[1]),
                        high=float(row[3]),
                        low=float(row[4]),
                        close=float(row[2]),
                        volume=float(row[5]) if len(row) > 5 else 0,
                        asset=asset,
                        timeframe=timeframe,
                    ))
                elif isinstance(row, (list, tuple)) and len(row) >= 2:
                    ticks.append((float(row[0]), float(row[1])))
            except (TypeError, ValueError, IndexError):
                continue

        if candles:
            return sorted(candles, key=lambda candle: candle.timestamp)
        if not ticks:
            return []

        buckets: Dict[int, List[float]] = defaultdict(list)
        for timestamp, price in ticks:
            # Some responses use milliseconds.
            if timestamp > 10_000_000_000:
                timestamp /= 1000
            bucket = int(timestamp // timeframe) * timeframe
            buckets[bucket].append(price)
        for bucket, prices in sorted(buckets.items()):
            candles.append(Candle(
                timestamp=datetime.fromtimestamp(bucket),
                open=prices[0],
                high=max(prices),
                low=min(prices),
                close=prices[-1],
                volume=0,
                asset=asset,
                timeframe=timeframe,
            ))
        return candles

    def _resolve_history_response(self, response: Dict[str, Any]) -> None:
        try:
            index = int(response["index"])
            request = self._history_requests.pop(index, None)
            if request is None:
                return
            candles = self._parse_history_rows(
                response.get("data", []),
                request["asset"],
                request["timeframe"],
            )
            future = request["future"]
            if not future.done():
                future.set_result(candles)
            logger.info(
                f"Resolved historical candles for {request['asset']}: {len(candles)}"
            )
        except Exception as exc:
            logger.warning(f"Could not parse historical response: {exc}")

    async def _request_history_candles(
        self, asset: str, timeframe: int, count: int, end_time: datetime
    ) -> List[Candle]:
        """Request historical candles through the history endpoint.

        The installed PocketOption client sends ``changeSymbol`` for its
        candle method. On some broker regions that only changes the live
        stream and never emits historical candles, so use the native history
        event directly and let the client's existing ``candles_received``
        handler resolve the pending request.
        """
        if self._client is None:
            raise RuntimeError("Client chưa kết nối")

        future = asyncio.get_running_loop().create_future()
        index = self._history_request_index
        self._history_request_index += 1
        payload = {
            "asset": str(asset),
            "index": index,
            "period": int(timeframe),
            "time": int(end_time.timestamp()),
            "offset": int(count),
        }
        # PocketOption routes small history windows through the fast endpoint.
        # The regular endpoint can stay silent for short requests on some
        # regions even though the same symbol is available.
        event_name = "loadHistoryPeriodFast" if count <= 100 else "loadHistoryPeriod"
        message = f'42["{event_name}",{json.dumps(payload)}]'
        logger.info(
            f"Requesting historical candles with {event_name} for {asset} "
            f"(timeframe={timeframe}, count={count})"
        )
        try:
            self._history_requests[index] = {
                "future": future,
                "asset": asset,
                "timeframe": timeframe,
            }
            await self._client._websocket.send_message(message)
            candles = await asyncio.wait_for(future, timeout=12.0)
            if candles:
                return candles[-count:]
            return []
        finally:
            self._history_requests.pop(index, None)

    def _on_json_data(self, data: Any) -> None:
        """Capture live tick prices from binary JSON attachments.

        PocketOption sends live ticks as [[symbol, timestamp, price], ...].
        This is the primary source of real-time entry/exit prices.
        """
        try:
            if isinstance(data, dict) and "index" in data and "data" in data:
                self._resolve_history_response(data)
                return
            if isinstance(data, list):
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
                if is_forex_pair(sym) and sym in _ACTIVE_REAL_FOREX_PAIRS
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
            [
                sym for sym, info in self._assets.items()
                if (
                    is_forex_pair(sym) and sym in _ACTIVE_REAL_FOREX_PAIRS
                    if category == "forex"
                    else get_asset_category(sym) == category and info.get("tradable")
                )
            ]
        )

    async def get_candles(self, asset: str, timeframe: int, count: int = ANALYSIS_CANDLE_COUNT) -> List[Candle]:
        await self.ensure_connected()
        if self._history_client is None:
            raise RuntimeError("Client chưa kết nối")
        from pocketoptionapi_async.exceptions import PocketOptionError

        if not self._history_client.is_connected():
            raise RuntimeError("Chưa kết nối đến PocketOption")

        async with self._candle_request_lock:
            logger.info(
                f"Requesting {count} live candles for {asset} "
                f"(timeframe={timeframe})"
            )
            hours = max(0.2, (count * timeframe) / 3600 + 0.1)
            try:
                stream = self._history_client.get_candles_live(
                    asset, timeframe, hours=hours, max_rows=count
                )
                closed, forming = await asyncio.wait_for(anext(stream), timeout=20.0)
                await stream.aclose()
                rows = list(closed)
                if forming:
                    rows.append(forming)
                candles = [
                    Candle(
                        timestamp=datetime.fromtimestamp(
                            float(row.get("time", row.get("timestamp", 0)))
                        ),
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row.get("volume") or 0),
                        asset=asset,
                        timeframe=timeframe,
                    )
                    for row in rows
                    if isinstance(row, dict)
                    and all(row.get(key) is not None for key in ("open", "high", "low", "close"))
                ]
                candles = sorted(candles, key=lambda candle: candle.timestamp)[-count:]
                if not candles:
                    raise PocketOptionError(
                        f"PocketOption không trả dữ liệu nến cho {asset}."
                    )
                self._latest_prices[asset] = candles[-1].close
                logger.info(f"Retrieved {len(candles)} candles for {asset}")
                return candles
            except Exception as exc:
                logger.error(f"Failed to get candles for {asset}: {exc}")
                raise PocketOptionError(f"Failed to get candles: {exc}") from exc

    async def get_latest_price(self, asset: str, timeframe: int = 60) -> float:
        """Return the latest close price by fetching a few recent candles."""
        candles = await self.get_candles(asset, timeframe, count=5)
        if not candles:
            raise RuntimeError(f"Không lấy được giá cho {asset}")
        return float(candles[-1].close)

    async def close(self) -> None:
        if self._history_client:
            await self._history_client.disconnect()
            self._history_client = None
        if self._client:
            await self._client.disconnect()
            self._client = None
        self._connected = False
