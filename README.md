# Binary Options Consensus Bot (Deriv + Groq/OpenRouter/Gemini + Telegram)

Hourly cycle: fetches real price data (and news, for forex) for a 30-instrument
universe, asks 3 AI models for high-confidence directional signals grounded
in that data, requires 2-of-3 or 3-of-3 model agreement at ≥80% confidence
each, trades the top N ranked signals as Rise/Fall binary contracts on Deriv,
holds each for 60 minutes to expiry, and reports everything to Telegram.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# edit .env with your real keys
python main.py
```

## Deploying on Railway (from GitHub, works entirely on mobile)

1. Push this project to a GitHub repo (see below if starting from scratch).
2. On [railway.app](https://railway.app), **New Project → Deploy from GitHub repo** → select your repo.
3. Railway auto-detects Python and uses `railway.json` / `Procfile` to run `python main.py` as a worker (no web port needed — this bot doesn't serve HTTP).
4. **Add a Volume**: Service → Settings → Volumes → mount at `/data`. This makes `state.sqlite3` (your trade history + open-position tracking) survive redeploys — without it, a redeploy loses track of any contract still awaiting settlement.
5. **Add environment variables**: Service → Variables → add every key from `.env.example` (`DERIV_API_TOKEN`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `GEMINI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and optionally `TWELVEDATA_API_KEY` / `NEWSDATA_API_KEY`). Never commit these to the repo — `.gitignore` already excludes `.env`.
6. Deploy. Check the **Logs** tab — you should see "Connected and authorized with Deriv API" and a Telegram startup message within a few seconds.
7. To update the bot later: edit files on GitHub (mobile web works fine for small edits) or push from a machine — Railway auto-redeploys on every push to your default branch.

### Getting a Deriv API token
1. Create a free account at https://deriv.com (a demo account is created automatically).
2. Go to https://app.deriv.com/account/api-token
3. Create a token with **Read** + **Trade** scopes.
4. Put it in `.env` as `DERIV_API_TOKEN`. Keep `DERIV_IS_DEMO=true` until you're
   confident in the bot's behavior.
5. Optionally register your own app at https://api.deriv.com/ for a dedicated
   `app_id` (the default `1089` is Deriv's shared public test app — fine for
   development).

## How it works

1. **market_data.py** — fetches real recent candles (Deriv's own tick history,
   always available) and last price for every instrument. For forex pairs, if
   `TWELVEDATA_API_KEY` is set, an independent price cross-check runs (logged
   if it diverges >0.5%). If `NEWSDATA_API_KEY` is set, recent business/forex
   headlines are pulled per currency pair. Synthetic indices skip news (see
   caveats below — they're not real-world assets).
2. **signal_engine.py** — builds one prompt per model containing the real
   price/candle/news data for all 30 instruments, and asks each of Groq,
   OpenRouter, and Gemini for directional calls at ≥80% confidence, grounded
   explicitly in that data (not general knowledge).
3. **consensus.py** — merges votes per instrument. Qualifies with ≥2 models
   agreeing (each ≥80% confidence). 3/3-agreement instruments rank above 2/3
   ones regardless of raw confidence (toggle via `REQUIRE_THREE_WHEN_AVAILABLE`).
   Top `NUM_SIGNALS` selected.
4. **deriv_client.py** — for each selected signal: gets a live price proposal
   for a CALL (bullish) or PUT (bearish) contract at the configured stake and
   duration, then buys it via Deriv's WebSocket API.
5. **trading_engine.py** — schedules an outcome check ~60 minutes later
   (binary contracts settle automatically at expiry; we poll Deriv for the
   result rather than "closing" anything ourselves), records win/loss + P&L
   to SQLite.
6. **telegram_bot.py** — sends a cycle summary, then an entry message per
   trade, then a win/loss result message per trade.
7. **main.py** — connects to Deriv, runs on an hourly APScheduler loop, and
   on startup recovers any open positions from `state.sqlite3` so a restart
   never leaves a contract outcome unchecked.

## Key config (`config.py`)

| Setting | Default | Meaning |
|---|---|---|
| `NUM_SIGNALS` | 5 | trades placed per hourly cycle |
| `STAKE_PER_TRADE` | $10 | stake per binary contract (full amount at risk) |
| `MIN_CONFIDENCE` | 80 | min confidence per model vote |
| `MIN_MODELS_AGREE` | 2 | floor for a valid consensus |
| `REQUIRE_THREE_WHEN_AVAILABLE` | True | rank 3/3 above 2/3 |
| `INSTRUMENTS` | 30 symbols | forex majors + Deriv synthetic indices |
| `TRADE_DURATION_MINUTES` | 60 | binary contract duration |
| `PRICE_LOOKBACK_CANDLES` | 30 | candles fetched per instrument for grounding |

## Important caveats — read before running

- **Binary options are all-or-nothing.** A losing contract loses the entire
  stake; a winning one pays the quoted payout. There's no partial exit or
  stop-loss — the forced-duration design you asked for maps naturally onto
  this contract type (unlike the earlier Alpaca options version, where forced
  duration was an artificial constraint).
- **1-hour binaries are less common than 5–15 min ones.** Liquidity/pricing
  for 60-minute Rise/Fall contracts is fine on Deriv but double-check the
  quoted payout percentage looks reasonable before scaling up stakes — very
  long or very short durations sometimes get less competitive payouts.
- **Real data grounding is now wired in**, but scope it honestly:
  - Forex instruments get real recent candles + (optionally) real news headlines.
  - Synthetic/volatility indices (`R_10`, `BOOM300N`, etc.) are Deriv-internal
    randomized processes with **no real-world news correlate** — there's
    nothing to "ground" beyond their own price history, which the bot does
    fetch and feed in. Don't expect news-driven edge on these.
  - Without `TWELVEDATA_API_KEY`/`NEWSDATA_API_KEY` set, the bot still runs
    fully on Deriv's own price history (never on model imagination alone),
    just without the independent cross-check or news layer.
- **This is a research/paper-testbed for consensus-based signal generation**,
  not a system with a proven statistical edge. Binary options as a category
  have structurally negative expected value for the buyer unless your signal
  has genuine predictive power beyond the house's pricing. Start on the demo
  account and track the win rate in `state.sqlite3` for a meaningful sample
  size (100+ trades) before considering real funds.
- `DERIV_IS_DEMO=true` is the default — this only affects the startup Telegram
  message wording; the actual account mode is determined by which token
  (demo vs real) you put in `DERIV_API_TOKEN`. Double check you're using a
  demo token before running unattended.

## Files

- `config.py` — all tunable presets + secrets loading
- `models.py` — dataclasses (ModelVote, ConsensusSignal, InstrumentContext, TradeRecord)
- `market_data.py` — real price (Deriv + optional TwelveData) and news (NewsData.io) fetching
- `signal_engine.py` — LLM API calls, prompt construction with real data, response parsing
- `consensus.py` — agreement/ranking rules
- `deriv_client.py` — Deriv WebSocket connection, proposals, buy, outcome polling
- `state.py` — SQLite trade log + open-position recovery
- `telegram_bot.py` — notification formatting/sending
- `trading_engine.py` — cycle orchestration, entry/outcome logic
- `main.py` — scheduler entrypoint
