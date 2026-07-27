"""
Pocket Option client, built on the unofficial BinaryOptionsToolsV2 library
(Rust-backed, actively maintained by ChipaDevTeam: pip install BinaryOptionsToolsV2).

IMPORTANT — read before relying on this in production:
- This is an UNOFFICIAL, reverse-engineered integration. Pocket Option has no
  published API; this works by mimicking their internal WebSocket protocol.
- Authentication uses your Pocket Option session ID (ssid), extracted MANUALLY
  from your browser's cookies (see get_ssid_instructions() below). Sessions
  expire periodically — when that happens, every call will start failing and
  you'll need to log into pocketoption.com again, re-extract a fresh ssid,
  and update the POCKET_OPTION_SSID environment variable + redeploy.
- There is no automatic session refresh possible with this approach.

This module exposes the same shape of interface as deriv_client.py
(get_candles, get_last_price, is_symbol_open, get_proposal-equivalent via
buy(), buy_contract, get_contract_status) so trading_engine.py can be
adapted with minimal structural changes if you switch brokers.
"""

import asyncio
import logging
from typing import Optional, List, Dict, Any

import config
from models import Candle

logger = logging.getLogger(__name__)

try:
    from BinaryOptionsToolsV2.pocketoption import PocketOptionAsync
except ImportError:
    PocketOptionAsync = None  # allows the rest of the app to import this
    # module without crashing if the package isn't installed yet; actual
    # use will raise a clear error at connect() time instead.


def get_ssid_instructions() -> str:
    return (
        "To get your Pocket Option session ID (ssid):\n"
        "1. Log into pocketoption.com in your browser\n"
        "2. Open DevTools (F12) -> Application tab -> Cookies -> https://pocketoption.com\n"
        "3. Find the cookie named 'ssid' and copy its value\n"
        "4. Set it as the POCKET_OPTION_SSID environment variable in Railway\n"
        "Note: this session WILL expire periodically. When it does, every call\n"
        "will start failing — repeat these steps to get a fresh ssid."
    )


class PocketOptionClient:
    """
    Thin async wrapper matching the shape of deriv_client.DerivClient, so
    trading_engine.py needs minimal changes to switch between brokers.
    """

    def __init__(self):
        self._client = None
        self.last_error: Optional[str] = None

    async def connect(self):
        if PocketOptionAsync is None:
            raise RuntimeError(
                "BinaryOptionsToolsV2 is not installed. Run: pip install BinaryOptionsToolsV2\n"
                "(also add it to requirements.txt)"
            )
        if not config.POCKET_OPTION_SSID:
            raise RuntimeError(
                "POCKET_OPTION_SSID is not set.\n\n" + get_ssid_instructions()
            )
        self._client = PocketOptionAsync(ssid=config.POCKET_OPTION_SSID)
        # Give the underlying WebSocket a moment to establish, matching the
        # library's own documented pattern (they use time.sleep(5) in sync
        # examples; a short async sleep serves the same purpose here).
        await asyncio.sleep(5)
        logger.info("Connected to Pocket Option")

    async def close(self):
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception as e:
                logger.warning(f"Error during Pocket Option disconnect: {e}")

    # -----------------------------------------------------------------
    # Market data
    # -----------------------------------------------------------------

    async def get_balance(self) -> Optional[Dict[str, Any]]:
        try:
            balance = await self._client.balance()
            return {"balance": balance, "currency": "USD", "loginid": "pocket_option"}
        except Exception as e:
            logger.error(f"Pocket Option balance fetch failed: {e}")
            self.last_error = str(e)
            return None

    async def get_candles(self, symbol: str, count: int, granularity_sec: int) -> List[Candle]:
        """
        Pocket Option symbols typically use an _otc suffix for
        always-open (over-the-counter) assets, e.g. 'EURUSD_otc'.
        """
        try:
            raw_candles = await self._client.get_candles(symbol, granularity_sec, count)
            return [
                Candle(
                    epoch=int(c.get("time", 0)),
                    open=float(c["open"]), high=float(c["high"]),
                    low=float(c["low"]), close=float(c["close"]),
                )
                for c in raw_candles
            ]
        except Exception as e:
            logger.warning(f"Pocket Option candle fetch failed for {symbol}: {e}")
            return []

    async def is_symbol_open(self, symbol: str) -> bool:
        """
        Pocket Option's _otc (over-the-counter) symbols are synthetic and
        trade 24/7, so they're effectively always open. Non-_otc symbols
        follow real market hours; this library doesn't expose a direct
        market-status endpoint in its documented API, so we approximate:
        _otc symbols -> always open, everything else -> attempt and let
        the buy() call itself fail if genuinely closed.
        """
        return symbol.endswith("_otc") or True  # see docstring: best-effort approximation

    # -----------------------------------------------------------------
    # Trading
    # -----------------------------------------------------------------

    async def buy_contract(self, symbol: str, stake: float, duration_sec: int,
                            direction: str) -> Optional[Dict[str, Any]]:
        """
        direction: "call" (bullish/up) or "put" (bearish/down)
        Returns {"trade_id": ..., "raw": ...} on success, None on failure.
        """
        try:
            trade_id, deal = await self._client.buy(symbol, stake, duration_sec) \
                if direction == "call" else \
                await self._client.sell(symbol, stake, duration_sec)
            self.last_error = None
            return {"trade_id": trade_id, "raw": deal}
        except Exception as e:
            logger.error(f"Pocket Option buy/sell failed for {symbol}: {e}")
            self.last_error = str(e)
            return None

    async def check_result(self, trade_id: str) -> Optional[bool]:
        """Returns True if won, False if lost, None if still pending/unknown."""
        try:
            return await self._client.check_win(trade_id)
        except Exception as e:
            logger.error(f"Pocket Option result check failed for {trade_id}: {e}")
            return None


client = PocketOptionClient()
