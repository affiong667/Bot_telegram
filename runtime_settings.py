"""
Runtime-adjustable settings, separate from config.py's static defaults.

config.py values are the defaults/fallback (and what a fresh restart uses).
Anything changed via Telegram (/setstake, /setsignals) lives here instead,
so a single hourly cycle always reads the CURRENT values rather than
whatever was frozen in at process startup.

Process-local only: a Railway restart/redeploy resets these back to
config.py's defaults. That's an intentional, simple tradeoff — persisting
this to the state DB would be easy to add later if you want settings to
survive restarts too.
"""

import config

# Start as copies of the static config defaults; /setstake, /setsignals,
# /setconfidence, /setduration mutate these, trading_engine.py and
# signal_engine.py read these (not config.py directly) for the values that
# should be Telegram-adjustable without a redeploy.
stake_per_trade: float = config.STAKE_PER_TRADE
num_signals: int = config.NUM_SIGNALS
min_confidence: int = config.MIN_CONFIDENCE
trade_duration_minutes: int = config.TRADE_DURATION_MINUTES


def set_stake(value: float):
    global stake_per_trade
    stake_per_trade = value


def set_num_signals(value: int):
    global num_signals
    num_signals = value


def set_min_confidence(value: int):
    global min_confidence
    min_confidence = value


def set_trade_duration(value: int):
    """
    Changes duration for future trades only. Already-open trades keep
    whichever duration was active when they were opened (their exit-check
    timer was scheduled using that value at entry time) — changing this
    does NOT retroactively reschedule trades already in flight.
    """
    global trade_duration_minutes
    trade_duration_minutes = value


def reset_to_defaults():
    global stake_per_trade, num_signals, min_confidence, trade_duration_minutes
    stake_per_trade = config.STAKE_PER_TRADE
    num_signals = config.NUM_SIGNALS
    min_confidence = config.MIN_CONFIDENCE
    trade_duration_minutes = config.TRADE_DURATION_MINUTES
