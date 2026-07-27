"""
Broker abstraction layer. trading_engine.py calls through THIS module only —
it never imports deriv_client or pocket_option_client directly. This keeps
the broker choice (config.BROKER = "deriv" | "pocket_option") as a single
switch rather than scattering if/else branches throughout the trading logic.

Each underlying client exposes a slightly different native interface (Deriv:
proposal+buy two-step, contract IDs, is_symbol_open via active_symbols;
Pocket Option: single buy() call, trade IDs, best-effort market-open check).
This module normalizes both into one consistent shape.

IMPORTANT CAVEAT for Pocket Option payouts: unlike Deriv, which quotes an
exact payout BEFORE you buy (via proposal), Pocket Option's documented API
doesn't expose a pre-trade payout quote in the same way. The P&L calculation
for Pocket Option trades below uses an assumed ~80% payout when the actual
figure isn't available — this is an approximation, not the real number.
Check your actual Pocket Option trade history to verify real payouts if you
rely on this for anything beyond rough tracking.
"""

import logging
from typing import Optional, List, Dict, Any

import config
from models import Candle

logger = logging.getLogger(__name__)


def get_instruments() -> List[str]:
    """Returns the correct instrument list for whichever broker is active."""
    if config.BROKER == "pocket_option":
        return config.POCKET_OPTION_INSTRUMENTS
    return config.INSTRUMENTS


def get_label(symbol: str) -> str:
    if config.BROKER == "pocket_option":
        return config.POCKET_OPTION_LABELS.get(symbol, symbol)
    return config.INSTRUMENT_LABELS.get(symbol, symbol)


async def connect():
    if config.BROKER == "pocket_option":
        from pocket_option_client import client
        await client.connect()
    else:
        from deriv_client import client
        await client.connect()


async def close():
    if config.BROKER == "pocket_option":
        from pocket_option_client import client
        await client.close()
    else:
        from deriv_client import client
        await client.close()


async def get_balance() -> Optional[Dict[str, Any]]:
    if config.BROKER == "pocket_option":
        from pocket_option_client import client
        return await client.get_balance()
    else:
        from deriv_client import client
        return await client.get_balance()


async def get_candles(symbol: str, count: int, granularity_sec: int) -> List[Candle]:
    if config.BROKER == "pocket_option":
        from pocket_option_client import client
        return await client.get_candles(symbol, count, granularity_sec)
    else:
        from deriv_client import client
        return await client.get_candles(symbol, count, granularity_sec)


async def is_symbol_open(symbol: str) -> bool:
    if config.BROKER == "pocket_option":
        from pocket_option_client import client
        return await client.is_symbol_open(symbol)
    else:
        from deriv_client import client
        return await client.is_symbol_open(symbol)


async def open_trade(symbol: str, direction: str, stake: float, duration_sec: int) -> Optional[Dict[str, Any]]:
    """
    direction: "bullish" or "bearish" (normalized at this layer so callers
    don't need to know each broker's own CALL/PUT/call/put naming).
    Returns a normalized dict: {"trade_ref": ..., "entry_price": ..., "payout": ...}
    or None on failure. trade_ref is whatever ID the specific broker needs
    later to check the outcome (Deriv: contract_id, Pocket Option: trade_id).
    """
    if config.BROKER == "pocket_option":
        from pocket_option_client import client
        po_direction = "call" if direction == "bullish" else "put"
        result = await client.buy_contract(symbol, stake, duration_sec, po_direction)
        if result is None:
            return None
        return {
            "trade_ref": str(result["trade_id"]),
            "entry_price": None,  # Pocket Option's buy() doesn't return an entry spot in the documented API
            "payout": None,       # payout isn't known upfront in the same way Deriv's proposal gives it
        }
    else:
        from deriv_client import client
        contract_type = config.CONTRACT_TYPE_BULLISH if direction == "bullish" else config.CONTRACT_TYPE_BEARISH
        proposal = await client.get_proposal(symbol=symbol, contract_type=contract_type,
                                              stake=stake, duration_sec=duration_sec)
        if proposal is None:
            return None
        buy_result = await client.buy_contract(proposal["id"], float(proposal.get("ask_price", stake)))
        if buy_result is None:
            return None
        return {
            "trade_ref": str(buy_result.get("contract_id", "")),
            "entry_price": float(proposal.get("spot", 0.0)),
            "payout": float(buy_result.get("payout", proposal.get("payout", 0.0))),
        }


async def check_outcome(trade_ref: str, stake: float, payout: Optional[float]) -> Optional[Dict[str, Any]]:
    """
    Returns {"won": bool, "pnl": float, "exit_price": Optional[float]} or
    None if the outcome couldn't be determined yet / an error occurred.
    """
    if config.BROKER == "pocket_option":
        from pocket_option_client import client
        won = await client.check_result(trade_ref)
        if won is None:
            return None
        pnl = (payout or stake * 0.8) - stake if won else -stake  # ~80% payout assumption if unknown; see caveat above
        return {"won": won, "pnl": pnl, "exit_price": None}
    else:
        from deriv_client import client, calc_binary_pnl
        status = await client.get_contract_status(trade_ref)
        if status is None:
            return None
        is_expired = bool(status.get("is_expired") or status.get("is_sold"))
        if not is_expired:
            return None  # not settled yet, caller should retry later
        won = bool(status.get("status") == "won") or float(status.get("profit", -1)) > 0
        exit_price = float(status.get("exit_tick") or status.get("sell_spot") or 0.0) or None
        actual_payout = float(status.get("payout", payout or 0.0))
        pnl = float(status.get("profit", calc_binary_pnl(stake, actual_payout, won)))
        return {"won": won, "pnl": pnl, "exit_price": exit_price}


def get_last_error() -> Optional[str]:
    if config.BROKER == "pocket_option":
        from pocket_option_client import client
        return client.last_error
    else:
        from deriv_client import client
        return client.last_error
