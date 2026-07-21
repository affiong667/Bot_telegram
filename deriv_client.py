"""
Deriv WebSocket API client. Handles connection, authorization, fetching
candle/tick history, getting price proposals, buying binary contracts, and
checking contract outcomes.

Deriv's API is request/response over a single persistent WebSocket
connection, with each request tagged by a client-chosen `req_id` so
responses can be matched to requests even when multiple are in flight.

Docs: https://developers.deriv.com/
"""

import asyncio
import json
import logging
import itertools
from typing import Optional, List, Dict, Any

import websockets

import config
from models import Candle

logger = logging.getLogger(__name__)

_req_id_counter = itertools.count(1)


class DerivClient:
    """
    A single shared WebSocket connection to Deriv, reused across the bot's
    lifetime. Call `connect()` once at startup and `close()` at shutdown.
    Reconnects automatically if the connection drops mid-session.
    """

    def __init__(self):
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._lock = asyncio.Lock()
        self._pending: Dict[int, asyncio.Future] = {}
        self._listener_task: Optional[asyncio.Task] = None
        self.last_error: Optional[str] = None  # most recent buy/proposal error message, for surfacing to Telegram
        self._authorized = False

    async def connect(self):
        self._ws = await websockets.connect(config.DERIV_WS_URL, ping_interval=20, ping_timeout=10)
        self._listener_task = asyncio.create_task(self._listen())
        await self._authorize()
        logger.info("Connected and authorized with Deriv API")

    async def close(self):
        if self._listener_task:
            self._listener_task.cancel()
        if self._ws:
            await self._ws.close()

    async def _listen(self):
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                req_id = msg.get("req_id")
                if req_id in self._pending:
                    fut = self._pending.pop(req_id)
                    if not fut.done():
                        fut.set_result(msg)
        except websockets.exceptions.ConnectionClosed:
            logger.warning("Deriv WebSocket connection closed. Reconnecting...")
            await self._reconnect()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Deriv listener error: {e}")
            await self._reconnect()

    async def _reconnect(self):
        try:
            self._ws = await websockets.connect(config.DERIV_WS_URL, ping_interval=20, ping_timeout=10)
            self._listener_task = asyncio.create_task(self._listen())
            self._authorized = False
            await self._authorize()
            logger.info("Reconnected and re-authorized with Deriv API")
        except Exception as e:
            logger.error(f"Deriv reconnect failed: {e}")

    async def _authorize(self):
        resp = await self._send({"authorize": config.DERIV_API_TOKEN})
        if resp.get("error"):
            raise RuntimeError(f"Deriv authorization failed: {resp['error']}")
        self._authorized = True

    async def _send(self, payload: Dict[str, Any], timeout: float = 20.0) -> Dict[str, Any]:
        req_id = next(_req_id_counter)
        payload = {**payload, "req_id": req_id}
        fut = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut
        async with self._lock:
            await self._ws.send(json.dumps(payload))
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            logger.error(f"Deriv request timed out: {payload}")
            return {"error": {"message": "timeout"}}

    # -----------------------------------------------------------------
    # Market data
    # -----------------------------------------------------------------

    async def get_candles(self, symbol: str, count: int, granularity_sec: int) -> List[Candle]:
        resp = await self._send({
            "ticks_history": symbol,
            "adjust_start_time": 1,
            "count": count,
            "end": "latest",
            "style": "candles",
            "granularity": granularity_sec,
        })
        if resp.get("error"):
            logger.warning(f"Candle fetch failed for {symbol}: {resp['error'].get('message')}")
            return []
        raw_candles = resp.get("candles", [])
        return [
            Candle(epoch=c["epoch"], open=float(c["open"]), high=float(c["high"]),
                   low=float(c["low"]), close=float(c["close"]))
            for c in raw_candles
        ]

    async def get_last_price(self, symbol: str) -> Optional[float]:
        resp = await self._send({"ticks_history": symbol, "count": 1, "end": "latest", "style": "ticks"})
        if resp.get("error"):
            logger.warning(f"Tick fetch failed for {symbol}: {resp['error'].get('message')}")
            return None
        prices = resp.get("history", {}).get("prices", [])
        return float(prices[-1]) if prices else None

    async def is_symbol_open(self, symbol: str) -> bool:
        resp = await self._send({"active_symbols": "brief", "product_type": "basic"})
        if resp.get("error"):
            logger.warning(f"active_symbols check failed: {resp['error'].get('message')}")
            return True  # fail-open; the proposal call will reject it anyway if truly closed
        for s in resp.get("active_symbols", []):
            if s.get("symbol") == symbol:
                return bool(s.get("exchange_is_open"))
        return False

    # -----------------------------------------------------------------
    # Trading
    # -----------------------------------------------------------------

    async def get_proposal(self, symbol: str, contract_type: str, stake: float,
                            duration_sec: int) -> Optional[Dict[str, Any]]:
        resp = await self._send({
            "proposal": 1,
            "amount": stake,
            "basis": "stake",
            "contract_type": contract_type,   # "CALL" or "PUT"
            "currency": "USD",
            "duration": duration_sec,
            "duration_unit": "s",
            "symbol": symbol,
        })
        if resp.get("error"):
            error_msg = resp["error"].get("message", "unknown error")
            error_code = resp["error"].get("code", "")
            logger.error(f"Proposal failed for {symbol} {contract_type}: [{error_code}] {error_msg}")
            self.last_error = f"[{error_code}] {error_msg}" if error_code else error_msg
            return None
        self.last_error = None
        return resp.get("proposal")

    async def buy_contract(self, proposal_id: str, price: float) -> Optional[Dict[str, Any]]:
        resp = await self._send({"buy": proposal_id, "price": price})
        if resp.get("error"):
            error_msg = resp["error"].get("message", "unknown error")
            error_code = resp["error"].get("code", "")
            logger.error(f"Buy failed for proposal {proposal_id}: [{error_code}] {error_msg}")
            self.last_error = f"[{error_code}] {error_msg}" if error_code else error_msg
            return None
        self.last_error = None
        return resp.get("buy")

    async def get_contract_status(self, contract_id: str) -> Optional[Dict[str, Any]]:
        resp = await self._send({"proposal_open_contract": 1, "contract_id": contract_id})
        if resp.get("error"):
            logger.error(f"Contract status fetch failed for {contract_id}: {resp['error'].get('message')}")
            return None
        return resp.get("proposal_open_contract")

    async def get_balance(self) -> Optional[Dict[str, Any]]:
        """Returns {'balance': float, 'currency': str, 'loginid': str} for the authorized account."""
        resp = await self._send({"balance": 1})
        if resp.get("error"):
            logger.error(f"Balance fetch failed: {resp['error'].get('message')}")
            return None
        return resp.get("balance")


# Module-level singleton — one connection shared by the whole bot process.
client = DerivClient()


def calc_binary_pnl(trade_stake: float, payout: Optional[float], won: bool) -> float:
    """Binary options: win -> payout - stake; lose -> -stake."""
    if won:
        return (payout or 0.0) - trade_stake
    return -trade_stake
