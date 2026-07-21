"""
Handles incoming Telegram commands via long-polling (getUpdates), so the
bot can actually respond to /status, /pause, /resume, /history instead of
being notify-only.

This runs as a background asyncio task alongside the hourly scheduler in
main.py. It does NOT use webhooks (no public HTTPS endpoint needed) —
long-polling works fine for a single-user bot like this and needs zero
extra infrastructure.

Pause state is a simple in-memory flag checked by trading_engine.run_cycle()
before it opens any new trades. Pausing does NOT cancel outcome-checks for
already-open trades — those always resolve normally.
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

import httpx

import config
import state
import telegram_bot
import runtime_settings
from deriv_client import client as deriv_client

logger = logging.getLogger(__name__)

# Recorded once, at import time (i.e. process/container start). If /status
# ever shows a recent-looking start time when you didn't just redeploy,
# that's proof the container silently restarted (crash-looped) on its own.
PROCESS_STARTED_AT = datetime.utcnow()

# Shared pause flag — checked by trading_engine.run_cycle().
# Module-level and process-local: if the bot restarts, it resumes un-paused
# (Railway doesn't persist this across deploys, only within one running process).
is_paused = False

_API_BASE = f"https://api.telegram.org/bot{{token}}"


def _label(symbol: str) -> str:
    return config.INSTRUMENT_LABELS.get(symbol, symbol)


async def _get_updates(client: httpx.AsyncClient, offset: Optional[int]) -> list:
    url = _API_BASE.format(token=config.TELEGRAM_BOT_TOKEN) + "/getUpdates"
    params = {"timeout": 25}
    if offset is not None:
        params["offset"] = offset
    try:
        resp = await client.get(url, params=params, timeout=30)
        if resp.status_code != 200:
            logger.error(f"getUpdates failed: {resp.status_code} {resp.text}")
            return []
        return resp.json().get("result", [])
    except httpx.ReadTimeout:
        return []
    except Exception as e:
        logger.error(f"getUpdates exception: {e}")
        return []


def _format_status() -> str:
    open_trades = state.get_open_trades()
    uptime = datetime.utcnow() - PROCESS_STARTED_AT
    uptime_str = f"{int(uptime.total_seconds() // 3600)}h {int((uptime.total_seconds() % 3600) // 60)}m"
    lines = [
        f"{'⏸️ PAUSED' if is_paused else '▶️ RUNNING'} — new trades "
        f"{'will NOT' if is_paused else 'will'} be opened next cycle.",
        "",
        f"Process uptime: {uptime_str} (started {PROCESS_STARTED_AT.strftime('%Y-%m-%d %H:%M UTC')})",
        f"Stake per trade: ${runtime_settings.stake_per_trade:.2f} | Signals per cycle: {runtime_settings.num_signals}",
        f"Min confidence: {runtime_settings.min_confidence}% | Trade duration: {runtime_settings.trade_duration_minutes}min",
        "",
        f"Open positions: {len(open_trades)}",
    ]
    for t in open_trades[:10]:
        lines.append(
            f"  • {_label(t.symbol)} {t.direction.upper()} "
            f"stake ${t.stake:.2f}, opened {t.entry_time.strftime('%H:%M UTC')}"
        )
    if len(open_trades) > 10:
        lines.append(f"  ...and {len(open_trades) - 10} more")
    return "\n".join(lines)


def _format_history(limit: int = 10) -> str:
    recent = state.get_recent_trades(limit=limit)
    closed = [t for t in recent if t.status in ("won", "lost")]
    if not closed:
        return "No closed trades yet."
    lines = [f"📜 <b>Last {len(closed)} closed trade(s)</b>\n"]
    wins = sum(1 for t in closed if t.status == "won")
    total_pnl = sum(t.pnl or 0 for t in closed)
    lines.append(f"Win rate: {wins}/{len(closed)} | Total P&L: ${total_pnl:+.2f}\n")
    for t in closed:
        emoji = "✅" if t.status == "won" else "❌"
        lines.append(
            f"{emoji} {_label(t.symbol)} {t.direction.upper()} "
            f"${t.pnl:+.2f} ({t.exit_time.strftime('%m/%d %H:%M UTC') if t.exit_time else 'n/a'})"
        )
    return "\n".join(lines)


def _help_text() -> str:
    return (
        "🤖 <b>Bot commands</b>\n"
        "/status — pause state, current settings, open positions\n"
        "/pause — stop opening new trades\n"
        "/resume — resume opening new trades\n"
        "/history — last 10 closed trades + win rate\n"
        "/balance — current Deriv account balance\n"
        "/settings — view current settings\n"
        "/setstake &lt;amount&gt; — change stake per trade (e.g. /setstake 15)\n"
        "/setsignals &lt;count&gt; — change signals traded per cycle (e.g. /setsignals 3)\n"
        "/setconfidence &lt;1-100&gt; — change minimum model confidence (e.g. /setconfidence 65)\n"
        "/setduration &lt;minutes&gt; — change new trades' duration (e.g. /setduration 30)\n"
        "/help — show this message"
    )


async def _handle_command(client: httpx.AsyncClient, chat_id: str, text: str):
    global is_paused
    parts = text.strip().split()
    command = parts[0].lower() if parts else ""
    args = parts[1:]

    if command == "/status":
        await telegram_bot.send_message(_format_status())
    elif command == "/pause":
        is_paused = True
        await telegram_bot.send_message("⏸️ Paused. No new trades will open until you send /resume. Any already-open trades will still settle normally.")
    elif command == "/resume":
        is_paused = False
        await telegram_bot.send_message("▶️ Resumed. New trades will open on the next hourly cycle.")
    elif command == "/history":
        await telegram_bot.send_message(_format_history())
    elif command == "/setstake":
        if not args:
            await telegram_bot.send_message(
                f"Current stake: ${runtime_settings.stake_per_trade:.2f}\nUsage: /setstake 15  (sets $15 per trade)"
            )
            return
        try:
            value = float(args[0])
            if value <= 0:
                raise ValueError("must be positive")
            if value > 1000:
                await telegram_bot.send_message(
                    f"⚠️ ${value:.2f} seems very high for a single binary options stake. "
                    f"Send /setstake {args[0]} again to confirm, or pick a smaller amount."
                )
                # Simple one-shot guard against fat-finger entry — does not
                # persist a "pending confirmation" state, just asks the
                # person to consciously repeat an unusually large value.
                return
            runtime_settings.set_stake(value)
            await telegram_bot.send_message(f"✅ Stake per trade set to ${value:.2f} (takes effect next cycle).")
        except ValueError:
            await telegram_bot.send_message("Couldn't parse that. Usage: /setstake 15")
    elif command == "/setsignals":
        if not args:
            await telegram_bot.send_message(
                f"Current signals per cycle: {runtime_settings.num_signals}\nUsage: /setsignals 3  (trade top 3 signals per hour)"
            )
            return
        try:
            value = int(args[0])
            if value <= 0:
                raise ValueError("must be positive")
            if value > len(config.INSTRUMENTS):
                await telegram_bot.send_message(
                    f"⚠️ Only {len(config.INSTRUMENTS)} instruments exist in the universe — "
                    f"can't select more than that. Try a smaller number."
                )
                return
            runtime_settings.set_num_signals(value)
            await telegram_bot.send_message(f"✅ Signals per cycle set to {value} (takes effect next cycle).")
        except ValueError:
            await telegram_bot.send_message("Couldn't parse that. Usage: /setsignals 3")
    elif command == "/setconfidence":
        if not args:
            await telegram_bot.send_message(
                f"Current min confidence: {runtime_settings.min_confidence}%\nUsage: /setconfidence 60  (accept signals at 60%+ confidence)"
            )
            return
        try:
            value = int(args[0])
            if not (1 <= value <= 100):
                raise ValueError("must be 1-100")
            if value < 50:
                await telegram_bot.send_message(
                    f"⚠️ {value}% is quite low for a confidence floor — expect more, lower-quality signals "
                    f"and likely more losing trades. Send /setconfidence {args[0]} again to confirm."
                )
                return
            runtime_settings.set_min_confidence(value)
            await telegram_bot.send_message(f"✅ Minimum confidence set to {value}% (takes effect next cycle).")
        except ValueError:
            await telegram_bot.send_message("Couldn't parse that. Usage: /setconfidence 60 (must be 1-100)")
    elif command == "/setduration":
        if not args:
            await telegram_bot.send_message(
                f"Current trade duration: {runtime_settings.trade_duration_minutes} min\n"
                f"Usage: /setduration 60  (contracts last 60 minutes, and new cycles run every 60 min too)\n"
                f"Note: only affects NEW trades — already-open trades keep their original duration."
            )
            return
        try:
            value = int(args[0])
            if not (1 <= value <= 1440):
                raise ValueError("must be 1-1440 (max 24h)")
            runtime_settings.set_trade_duration(value)

            # Deferred import: trading_engine imports bot_commands (for the
            # is_paused check), so importing it at module level here would
            # create a circular import. By the time this handler actually
            # runs, both modules are fully loaded, so this is safe.
            import trading_engine
            trading_engine.reschedule_cycle(value)

            await telegram_bot.send_message(
                f"✅ Trade duration set to {value} min for new trades, and new signal "
                f"cycles will now run every {value} min too (already-open trades are unaffected)."
            )
        except ValueError:
            await telegram_bot.send_message("Couldn't parse that. Usage: /setduration 60 (minutes, 1-1440)")
    elif command == "/start" or command == "/help":
        await telegram_bot.send_message(_help_text())
    elif command == "/balance":
        balance = await deriv_client.get_balance()
        if balance is None:
            await telegram_bot.send_message("⚠️ Couldn't fetch balance from Deriv right now — try again shortly.")
        else:
            amount = balance.get("balance", "n/a")
            currency = balance.get("currency", "")
            loginid = balance.get("loginid", "")
            account_kind = "DEMO" if config.DERIV_IS_DEMO else "REAL ⚠️"
            await telegram_bot.send_message(
                f"💰 <b>Deriv Balance</b>\n"
                f"Account: {loginid} ({account_kind})\n"
                f"Balance: {amount} {currency}"
            )
    elif command == "/settings":
        await telegram_bot.send_message(
            "⚙️ <b>Current settings</b>\n"
            f"Stake per trade: ${runtime_settings.stake_per_trade:.2f}\n"
            f"Signals per cycle: {runtime_settings.num_signals}\n"
            f"Min confidence: {runtime_settings.min_confidence}%\n"
            f"Contract duration: {runtime_settings.trade_duration_minutes}min\n\n"
            "To change any of these, use:\n"
            "/setstake &lt;amount&gt;\n"
            "/setsignals &lt;count&gt;\n"
            "/setconfidence &lt;1-100&gt;\n"
            "/setduration &lt;minutes&gt;"
        )
    # Unrecognized text is silently ignored — no need to spam replies to
    # random messages sent to the bot.


async def poll_commands():
    """
    Long-running background task: polls Telegram for new messages and
    dispatches recognized commands. Runs forever until cancelled.
    """
    offset = None
    print("[bot_commands] poll_commands() task entered", flush=True)
    logger.info("Telegram command polling started")
    try:
        await telegram_bot.send_message("🔌 Command listener online — /status, /pause, /resume, /history are ready.")
    except Exception as e:
        logger.error(f"Failed to send command-listener-online ping: {e}")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                updates = await _get_updates(client, offset)
                for update in updates:
                    offset = update["update_id"] + 1
                    message = update.get("message") or {}
                    chat_id = str(message.get("chat", {}).get("id", ""))
                    text = message.get("text", "")

                    # Only respond to the configured chat — ignore anyone else
                    # who might message the bot (defense in depth; the token
                    # is private, but this costs nothing to add).
                    if chat_id != str(config.TELEGRAM_CHAT_ID):
                        logger.warning(f"Ignored message from unexpected chat_id={chat_id}")
                        continue

                    if text.startswith("/"):
                        await _handle_command(client, chat_id, text)
            except asyncio.CancelledError:
                logger.info("Telegram command polling stopped")
                raise
            except Exception as e:
                logger.error(f"Command polling loop error: {e}")
                await asyncio.sleep(5)  # brief backoff before retrying
