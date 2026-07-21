"""
Sends notifications to Telegram. Fire-and-forget style — notification
failures are logged but never block trading logic.
"""

import logging
from typing import List

import httpx

import config
from models import ConsensusSignal, TradeRecord

logger = logging.getLogger(__name__)

API_URL = f"https://api.telegram.org/bot{{token}}/sendMessage"


async def send_message(text: str):
    url = API_URL.format(token=config.TELEGRAM_BOT_TOKEN)
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(
                url,
                json={
                    "chat_id": config.TELEGRAM_CHAT_ID,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=15,
            )
            if resp.status_code != 200:
                logger.error(f"Telegram send failed: {resp.status_code} {resp.text}")
        except Exception as e:
            logger.error(f"Telegram send exception: {e}")


def _label(symbol: str) -> str:
    return config.INSTRUMENT_LABELS.get(symbol, symbol)


def format_cycle_summary(selected: List[ConsensusSignal], all_qualifying: int) -> str:
    if not selected:
        return (
            "⏱️ <b>Hourly cycle</b>\n"
            "No qualifying signals this hour (no instrument reached "
            f"{config.MIN_MODELS_AGREE}+ model agreement at {config.MIN_CONFIDENCE}%+ confidence)."
        )
    lines = [f"⏱️ <b>Hourly cycle</b> — {len(selected)} signal(s) selected ({all_qualifying} qualified total)\n"]
    for s in selected:
        arrow = "🟢" if s.direction.value == "bullish" else "🔴"
        models = "/".join(m.upper() for m in s.agreeing_models)
        lines.append(
            f"{arrow} <b>{_label(s.symbol)}</b> {s.direction.value.upper()} "
            f"— {models} ({len(s.agreeing_models)}/3) — avg conf {s.avg_confidence:.0f}%"
        )
    return "\n".join(lines)


def format_entry(trade: TradeRecord, signal: ConsensusSignal) -> str:
    arrow = "🟢" if trade.direction == "bullish" else "🔴"
    models = "/".join(m.upper() for m in signal.agreeing_models)
    payout_str = f"${trade.payout:.2f}" if trade.payout else "n/a"
    return (
        f"{arrow} <b>ENTRY: {_label(trade.symbol)}</b>\n"
        f"Contract: {trade.contract_type} (Deriv id <code>{trade.deriv_contract_id}</code>)\n"
        f"Direction: {trade.direction.upper()}\n"
        f"Consensus: {models} ({len(signal.agreeing_models)}/3), avg conf {signal.avg_confidence:.0f}%\n"
        f"Entry spot: {trade.entry_spot}\n"
        f"Stake: ${trade.stake:.2f} | Potential payout: {payout_str}\n"
        f"Expires: +{config.TRADE_DURATION_MINUTES} min"
    )


def format_exit(trade: TradeRecord) -> str:
    won = trade.status == "won"
    result_emoji = "✅ WON" if won else ("❌ LOST" if trade.status == "lost" else "⚠️")
    pnl_str = f"{trade.pnl:+.2f}" if trade.pnl is not None else "n/a"
    exit_spot_str = trade.exit_spot if trade.exit_spot is not None else "n/a"
    return (
        f"{result_emoji}: <b>{_label(trade.symbol)}</b>\n"
        f"Entry spot: {trade.entry_spot} → Exit spot: {exit_spot_str}\n"
        f"P&L: ${pnl_str}"
    )


def format_error(context: str, detail: str) -> str:
    return f"⚠️ <b>Error</b> [{context}]\n{detail}"
