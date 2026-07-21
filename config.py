"""
Central configuration for the binary/digital options consensus bot.
Edit the values below to tune behavior. Secrets are read from environment
variables (see .env.example) — never hardcode API keys here.
"""

import os
from typing import List

from dotenv import load_dotenv
load_dotenv()

# ---------------------------------------------------------------------------
# Deriv API credentials
# ---------------------------------------------------------------------------
# Register an app at https://api.deriv.com/ to get your own app_id (1089 is
# Deriv's public test app_id — fine for development, get your own for prod).
DERIV_APP_ID = os.getenv("DERIV_APP_ID", "1089")
DERIV_API_TOKEN = os.getenv("DERIV_API_TOKEN", "")  # demo account token to start
DERIV_WS_URL = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"
DERIV_IS_DEMO = os.getenv("DERIV_IS_DEMO", "true").lower() == "true"

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Market data / news (free tier sources)
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "")  # https://twelvedata.com — free tier, forex+crypto+indices
NEWSDATA_API_KEY = os.getenv("NEWSDATA_API_KEY", "")       # https://newsdata.io — free tier financial news
# If TWELVEDATA_API_KEY is blank, price grounding falls back to Deriv's own
# tick history (always available, no extra key needed) — see market_data.py.

# ---------------------------------------------------------------------------
# Model selection
# ---------------------------------------------------------------------------
GROQ_MODEL = "llama-3.3-70b-versatile"
OPENROUTER_MODEL = "openai/gpt-4o-mini"
GEMINI_MODEL = "gemini-2.0-flash"

# ---------------------------------------------------------------------------
# Trading presets
# ---------------------------------------------------------------------------
NUM_SIGNALS: int = 5            # top-N signals traded per hourly cycle
STAKE_PER_TRADE: float = 10.0   # USD stake per binary contract (this is the full stake, not notional)
MIN_CONFIDENCE: int = 60        # minimum confidence (0-100) per model for a vote to count
REQUIRE_THREE_WHEN_AVAILABLE: bool = True  # prefer 3/3 consensus; fall back to 2/3 only if a ticker has no 3/3 match
MIN_MODELS_AGREE: int = 2       # absolute floor — never trade on a single model's opinion

TRADE_DURATION_MINUTES: int = 60  # binary contract duration
CYCLE_INTERVAL_MINUTES: int = 60  # how often a new cycle starts

TIMEFRAME_BIAS: str = "15-minute candles, trend context over the last 4 hours"

# Real-data grounding
PRICE_LOOKBACK_CANDLES: int = 30    # how many recent candles to fetch per instrument
PRICE_CANDLE_GRANULARITY_SEC: int = 900  # 15 min candles (matches TIMEFRAME_BIAS)
NEWS_HEADLINES_PER_INSTRUMENT: int = 3   # max headlines injected per instrument prompt
NEWS_LOOKBACK_HOURS: int = 12

# Binary contract type mapping: bullish -> CALL ("Rise"), bearish -> PUT ("Fall")
CONTRACT_TYPE_BULLISH = "CALL"
CONTRACT_TYPE_BEARISH = "PUT"

# ---------------------------------------------------------------------------
# Instrument universe (~50 symbols — Deriv symbol codes)
# Forex majors/crosses + Deriv synthetic indices, both tradeable 24/7.
# ---------------------------------------------------------------------------
INSTRUMENTS: List[str] = [
    # Forex majors + crosses (Deriv symbol format)
    "frxEURUSD", "frxGBPUSD", "frxUSDJPY", "frxUSDCHF", "frxAUDUSD",
    "frxUSDCAD", "frxNZDUSD", "frxEURGBP", "frxEURJPY", "frxGBPJPY",
    "frxEURAUD", "frxEURCHF", "frxAUDJPY", "frxGBPAUD", "frxGBPCAD",
    "frxAUDCAD", "frxAUDCHF", "frxAUDNZD", "frxCADCHF", "frxCADJPY",
    "frxCHFJPY", "frxEURCAD", "frxEURNZD", "frxGBPCHF", "frxGBPNZD",
    "frxNZDCAD", "frxNZDCHF", "frxNZDJPY",
    # Deriv synthetic / volatility indices (always open, no market-hours gating needed)
    "R_10", "R_25", "R_50", "R_75", "R_100",
    "1HZ10V", "1HZ25V", "1HZ50V", "1HZ75V", "1HZ100V",
    "BOOM300N", "BOOM500", "BOOM1000",
    "CRASH300N", "CRASH500", "CRASH1000",
    "JD10", "JD25", "JD50", "JD75", "JD100",
    "stpRNG",
]

# Human-readable labels for prompts/Telegram (falls back to raw symbol if missing)
INSTRUMENT_LABELS = {
    "frxEURUSD": "EUR/USD", "frxGBPUSD": "GBP/USD", "frxUSDJPY": "USD/JPY",
    "frxUSDCHF": "USD/CHF", "frxAUDUSD": "AUD/USD", "frxUSDCAD": "USD/CAD",
    "frxNZDUSD": "NZD/USD", "frxEURGBP": "EUR/GBP", "frxEURJPY": "EUR/JPY",
    "frxGBPJPY": "GBP/JPY", "frxEURAUD": "EUR/AUD", "frxEURCHF": "EUR/CHF",
    "frxAUDJPY": "AUD/JPY", "frxGBPAUD": "GBP/AUD", "frxGBPCAD": "GBP/CAD",
    "frxAUDCAD": "AUD/CAD", "frxAUDCHF": "AUD/CHF", "frxAUDNZD": "AUD/NZD",
    "frxCADCHF": "CAD/CHF", "frxCADJPY": "CAD/JPY", "frxCHFJPY": "CHF/JPY",
    "frxEURCAD": "EUR/CAD", "frxEURNZD": "EUR/NZD", "frxGBPCHF": "GBP/CHF",
    "frxGBPNZD": "GBP/NZD", "frxNZDCAD": "NZD/CAD", "frxNZDCHF": "NZD/CHF",
    "frxNZDJPY": "NZD/JPY",
    "R_10": "Volatility 10 Index", "R_25": "Volatility 25 Index",
    "R_50": "Volatility 50 Index", "R_75": "Volatility 75 Index",
    "R_100": "Volatility 100 Index",
    "1HZ10V": "Volatility 10 (1s) Index", "1HZ25V": "Volatility 25 (1s) Index",
    "1HZ50V": "Volatility 50 (1s) Index", "1HZ75V": "Volatility 75 (1s) Index",
    "1HZ100V": "Volatility 100 (1s) Index",
    "BOOM300N": "Boom 300 Index", "BOOM500": "Boom 500 Index",
    "BOOM1000": "Boom 1000 Index",
    "CRASH300N": "Crash 300 Index", "CRASH500": "Crash 500 Index",
    "CRASH1000": "Crash 1000 Index",
    "JD10": "Jump 10 Index", "JD25": "Jump 25 Index", "JD50": "Jump 50 Index",
    "JD75": "Jump 75 Index", "JD100": "Jump 100 Index",
    "stpRNG": "Step Index",
}

# Which instruments are real-world assets with news relevance (forex) vs
# purely synthetic (no real-world news applies — skip news fetch for these).
NEWS_RELEVANT_PREFIXES = ("frx",)

# ---------------------------------------------------------------------------
# Misc / operational
# ---------------------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
# On Railway, mount a Volume at /data and it will survive redeploys.
# Locally (no volume), this just falls back to a file in the working directory.
STATE_DB_PATH = os.getenv("STATE_DB_PATH", "/data/state.sqlite3" if os.path.isdir("/data") else "state.sqlite3")
# Synthetic indices trade 24/7; forex has normal market hours. We do NOT
# gate the whole cycle on "market hours" anymore since the universe is mixed —
# per-instrument tradability is checked against Deriv's active_symbols instead.


def validate() -> List[str]:
    """Returns a list of missing/invalid config problems, empty if OK."""
    problems = []
    if not DERIV_API_TOKEN:
        problems.append("Deriv API token missing")
    if not GROQ_API_KEY:
        problems.append("Groq API key missing")
    if not OPENROUTER_API_KEY:
        problems.append("OpenRouter API key missing")
    if not GEMINI_API_KEY:
        problems.append("Gemini API key missing")
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        problems.append("Telegram bot token / chat id missing")
    if len(INSTRUMENTS) < NUM_SIGNALS:
        problems.append("NUM_SIGNALS exceeds size of INSTRUMENTS universe")
    if MIN_MODELS_AGREE < 2:
        problems.append("MIN_MODELS_AGREE must be >= 2 (this is a consensus bot)")
    if not TWELVEDATA_API_KEY:
        problems.append(
            "TWELVEDATA_API_KEY missing — price grounding will fall back to Deriv "
            "tick history only (still works, but no independent cross-check source)"
        )
    return problems
