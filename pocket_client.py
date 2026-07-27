"""PocketOption client wrapper used by the Telegram bot."""
import asyncio
import json
from typing import Dict, List, Optional, Any
from datetime import datetime

from loguru import logger

from pocketoptionapi_async.client import AsyncPocketOptionClient
from pocketoptionapi_async.models import Candle

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
            # Attach directly to the websocket client so we receive the raw payout message
            self._client._websocket.add_event_handler("payout_update", self._on_payout_update)
            self._client.add_event_callback("disconnected", self._on_disconnected)

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
            # Wait briefly for payout/asset list to arrive
            await asyncio.sleep(2.5)
            logger.info("Connected to PocketOption.")
            return True

    async def ensure_connected(self) -> None:
        if not self._connected or not self._client or not self._client.is_connected:
            await self.connect()

    def _on_payout_update(self, data: Dict[str, Any]) -> None:
        assets = data.get("assets", {})
        logger.info(f"BotPocketClient received payout_update with {len(assets)} assets")
        if assets:
            self._assets = assets
            self._assets_last_update = datetime.now().timestamp()
            logger.info(f"Asset list updated: {len(self._assets)} assets")

    def _on_disconnected(self, _data: Any) -> None:
        self._connected = False
        logger.warning("Disconnected from PocketOption")

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
