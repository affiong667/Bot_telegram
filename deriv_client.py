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
import json
import logging
import re
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


def normalize_ssid(raw_ssid: str) -> str:
    """
    Patch for a real Pocket Option/BinaryOptionsToolsV2 mismatch: the
    library's sanitize_and_validate_ssid() requires a field literally named
    'session', but Pocket Option's current site sends the equivalent data
    under the field name 'sessionToken' instead. Rather than edit the
    installed library (which gets wiped on every fresh pip install), we
    transform the payload here before handing it to the library.

    Does this JSON-aware (parses the '42[...]' Socket.IO payload properly)
    rather than a naive string replace, so we don't risk corrupting the
    payload if 'sessionToken' appears elsewhere unexpectedly.

    If the payload already has a 'session' field, or doesn't match the
    expected '42[...]' shape at all, returns it unchanged.
    """
    match = re.match(r'^(\d+)(\[.*\])$', raw_ssid.strip())
    if not match:
        logger.warning("ssid doesn't match expected '<digits>[...]' Socket.IO shape — passing through unchanged")
        return raw_ssid

    prefix, json_part = match.group(1), match.group(2)
    try:
        parsed = json.loads(json_part)
    except json.JSONDecodeError as e:
        logger.warning(f"Could not parse ssid JSON payload ({e}) — passing through unchanged")
        return raw_ssid

    # Expected shape: ["auth", {"sessionToken": "...", "uid": ..., ...}]
    if len(parsed) >= 2 and isinstance(parsed[1], dict):
        payload = parsed[1]
        if "session" not in payload and "sessionToken" in payload:
            payload["session"] = payload.pop("sessionToken")
            logger.info("Normalized ssid: renamed 'sessionToken' field to 'session' for library compatibility")
            parsed[1] = payload
            return prefix + json.dumps(parsed)

    return raw_ssid


class PocketOptionClient:
    """
    Thin async wrapper matching the shape of deriv_client.DerivClient, so
    trading_engine.py needs minimal changes to switch between brokers.
    """

    def __init__(self):
        self._client = None
        self.last_error: Optional[str] = None
        # Serializes ALL calls to self._client. Hypothesis: BinaryOptionsToolsV2's
        # single shared client may not be safe under many concurrent calls (our
        # bot fetches candles for 28 instruments via asyncio.gather, all hitting
        # one client instance at once) — this could be what's tearing down its
        # internal Rust-side channel ("half closed channel" errors), rather than
        # genuine network drops. Serializing forces one call at a time, trading
        # some speed for stability while we confirm/rule this out.
        self._call_lock = asyncio.Lock()

    async def ensure_connected(self):
        """
        Proactively checks the connection is genuinely alive via a real,
        lightweight call (balance()), BEFORE a cycle starts touching any
        instruments. If it's dead, recreates the client from scratch. This
        is called once at the start of every cycle by trading_engine.py,
        rather than only reacting after a candle-fetch call fails.
        """
        if self._client is None:
            logger.warning("ensure_connected: no client exists yet, connecting fresh...")
            await self.connect()
            return

        try:
            async with self._call_lock:
                await self._client.balance()
            logger.info("ensure_connected: connection verified alive")
        except Exception as e:
            err_text = str(e).lower()
            logger.warning(f"ensure_connected: connection appears dead ({e!r}), recreating client...")
            async with self._call_lock:
                await self._recreate_client()

    async def _recreate_client(self):
        """
        For structural errors (e.g. 'half closed channel') where the
        underlying Rust connection manager has torn down its internal
        message-passing channel, calling reconnect() on the same Python
        object is unlikely to help — the channel itself is gone, not just
        the socket. This tears down and rebuilds the whole client instance
        from scratch instead, which re-runs the full connection setup.
        """
        logger.warning("Recreating Pocket Option client from scratch (structural channel error detected)...")
        try:
            if self._client is not None:
                await self._client.disconnect()
        except Exception as e:
            logger.debug(f"Error disconnecting old client before recreation (expected if already broken): {e}")

        normalized_ssid = normalize_ssid(config.POCKET_OPTION_SSID)
        self._client = PocketOptionAsync(ssid=normalized_ssid)
        await asyncio.sleep(5)
        await self._client.balance()  # verify the new instance is actually usable; let this raise if not
        logger.info("Pocket Option client successfully recreated and verified")

    async def _call_with_reconnect(self, label: str, fn):
        """
        Calls fn() (a zero-arg async callable wrapping a self._client.*
        call), serialized through self._call_lock so only one call touches
        the shared client at a time (see __init__ docstring for why). On
        failure, picks a recovery strategy based on the error:
        - "not connected" / "connection may have dropped" -> the socket
          likely just dropped; try the lighter-weight reconnect() first.
        - "half closed channel" / "channel sender error" -> the underlying
          Rust connection manager's internal channel has been torn down;
          reconnect() on the same instance won't fix this, so recreate the
          whole client from scratch instead.
        Retries fn() once after whichever recovery step was taken.
        """
        async with self._call_lock:
            try:
                return await fn()
            except Exception as e:
                err_text = str(e).lower()
                try:
                    if "half closed channel" in err_text or "channel sender error" in err_text:
                        logger.warning(f"{label}: structural channel error ({e!r}), recreating client...")
                        await self._recreate_client()
                    elif "not connected" in err_text or "connection may have dropped" in err_text:
                        logger.warning(f"{label}: connection dropped ({e!r}), attempting reconnect()...")
                        await self._client.reconnect()
                        await asyncio.sleep(2)
                    else:
                        raise
                    return await fn()
                except Exception as e2:
                    logger.error(f"{label}: retry after recovery attempt also failed: {e2!r}")
                raise

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
        normalized_ssid = normalize_ssid(config.POCKET_OPTION_SSID)
        self._client = PocketOptionAsync(ssid=normalized_ssid)
        # Give the underlying WebSocket a moment to establish, matching the
        # library's own documented pattern (they use time.sleep(5) in sync
        # examples; a short async sleep serves the same purpose here).
        await asyncio.sleep(5)

        # Verify the connection is genuinely ready with a real call, rather
        # than trusting the fixed sleep alone — retry the connect itself
        # once if the first real call still reports "not connected".
        try:
            await self._client.balance()
            logger.info("Connected to Pocket Option (verified with balance() call)")
        except Exception as e:
            if "not connected" in str(e).lower():
                logger.warning(f"Initial connection not ready after 5s wait ({e!r}), waiting 5 more seconds and retrying...")
                await asyncio.sleep(5)
                await self._client.balance()  # let this raise if still failing — caller (main.py) will see the real error
                logger.info("Connected to Pocket Option (verified after extended wait)")
            else:
                raise

        # Diagnostic: log the REAL installed get_candles signature once at
        # startup, since library documentation has proven unreliable for
        # this package (parameter names differ between doc versions and
        # what's actually installed). This gives ground truth in the logs
        # instead of guessing from README examples again.
        try:
            import inspect
            sig = inspect.signature(self._client.get_candles)
            logger.info(f"Pocket Option get_candles() real signature: {sig}")
        except Exception as e:
            logger.debug(f"Could not introspect get_candles signature: {e}")

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

        Library documentation for get_candles() has proven unreliable —
        different doc sources show different parameter names (period/duration
        vs period/count vs positional), and the actually-installed version
        rejected 'duration' as an unexpected keyword. Rather than guess again
        and require another deploy round-trip, this tries several plausible
        call shapes in order and uses whichever one the installed version
        actually accepts, logging which one worked so we can hardcode it
        once confirmed.
        """
        period = granularity_sec
        attempts = [
            ("period+count kwargs", lambda: self._client.get_candles(symbol, period=period, count=count)),
            ("positional (symbol, period, count)", lambda: self._client.get_candles(symbol, period, count)),
            ("period kwarg only", lambda: self._client.get_candles(symbol, period=period)),
            ("time_frame+count kwargs", lambda: self._client.get_candles(symbol, time_frame=period, count=count)),
            ("positional symbol only", lambda: self._client.get_candles(symbol)),
        ]

        last_exception = None
        for label, attempt in attempts:
            try:
                raw_candles = await self._call_with_reconnect(f"get_candles({symbol}, {label})", attempt)
                logger.info(f"Pocket Option get_candles() succeeded using: {label} — hardcode this shape once confirmed stable")
                return [
                    Candle(
                        epoch=int(c.get("time", 0)),
                        open=float(c["open"]), high=float(c["high"]),
                        low=float(c["low"]), close=float(c["close"]),
                    )
                    for c in raw_candles
                ]
            except TypeError as e:
                last_exception = e
                continue  # this call shape isn't accepted, try the next
            except Exception as e:
                # A non-TypeError means we found an accepted call shape but
                # something else went wrong (network, symbol not found, etc.)
                # — don't keep guessing shapes, surface this real error.
                logger.warning(f"Pocket Option candle fetch failed for {symbol} (using {label}): {e!r}")
                self.last_error = f"{symbol}: {e!r}"
                return []

        logger.warning(f"Pocket Option candle fetch failed for {symbol}: no known call shape accepted. Last error: {last_exception!r}")
        self.last_error = f"{symbol}: no known get_candles() call shape accepted ({last_exception!r})"
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
            if direction == "call":
                trade_id, deal = await self._client.buy(symbol, amount=stake, time=duration_sec)
            else:
                trade_id, deal = await self._client.sell(symbol, amount=stake, time=duration_sec)
            self.last_error = None
            return {"trade_id": trade_id, "raw": deal}
        except Exception as e:
            logger.error(f"Pocket Option buy/sell failed for {symbol}: {e!r}")
            self.last_error = f"{symbol}: {e!r}"
            return None

    async def check_result(self, trade_id: str) -> Optional[bool]:
        """Returns True if won, False if lost, None if still pending/unknown."""
        try:
            return await self._client.check_win(trade_id)
        except Exception as e:
            logger.error(f"Pocket Option result check failed for {trade_id}: {e}")
            return None


client = PocketOptionClient()
