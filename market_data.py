"""
Fetches real market data to ground the AI models' signals — this is what
makes the prompts based on actual price action/news rather than pure
model imagination.

Price data: TwelveData is now the PRIMARY source for candles/price (the
instrument universe is forex-only, which TwelveData covers well on its
free tier). Deriv's own tick/candle history is used only as a FALLBACK if
TwelveData fails or isn't configured — this reduces how often the bot
depends on Deriv's WebSocket connection just to gather prompt context;
Deriv's WebSocket is still required (and always used) for the parts that
can only happen there: contract proposals, buying, and settlement checks.

News data: NewsData.io free tier, queried per forex pair's base/quote
currency.
"""

import asyncio
import logging
from typing import List, Optional

import httpx

import config
from models import InstrumentContext, NewsItem, Candle
from deriv_client import client as deriv_client

logger = logging.getLogger(__name__)

# Maps Deriv forex symbols to (base, quote) currency codes for TwelveData/news queries
_FX_CURRENCY_MAP = {
    "frxEURUSD": ("EUR", "USD"), "frxGBPUSD": ("GBP", "USD"), "frxUSDJPY": ("USD", "JPY"),
    "frxUSDCHF": ("USD", "CHF"), "frxAUDUSD": ("AUD", "USD"), "frxUSDCAD": ("USD", "CAD"),
    "frxNZDUSD": ("NZD", "USD"), "frxEURGBP": ("EUR", "GBP"), "frxEURJPY": ("EUR", "JPY"),
    "frxGBPJPY": ("GBP", "JPY"), "frxEURAUD": ("EUR", "AUD"), "frxEURCHF": ("EUR", "CHF"),
    "frxAUDJPY": ("AUD", "JPY"), "frxGBPAUD": ("GBP", "AUD"), "frxGBPCAD": ("GBP", "CAD"),
    "frxAUDCAD": ("AUD", "CAD"), "frxAUDCHF": ("AUD", "CHF"), "frxAUDNZD": ("AUD", "NZD"),
    "frxCADCHF": ("CAD", "CHF"), "frxCADJPY": ("CAD", "JPY"), "frxCHFJPY": ("CHF", "JPY"),
    "frxEURCAD": ("EUR", "CAD"), "frxEURNZD": ("EUR", "NZD"), "frxGBPCHF": ("GBP", "CHF"),
    "frxGBPNZD": ("GBP", "NZD"), "frxNZDCAD": ("NZD", "CAD"), "frxNZDCHF": ("NZD", "CHF"),
    "frxNZDJPY": ("NZD", "JPY"),
}

# TwelveData interval string for our configured candle granularity
_GRANULARITY_TO_TWELVEDATA_INTERVAL = {
    60: "1min", 300: "5min", 900: "15min", 1800: "30min",
    3600: "1h", 14400: "4h", 86400: "1day",
}


def _twelvedata_interval() -> str:
    return _GRANULARITY_TO_TWELVEDATA_INTERVAL.get(config.PRICE_CANDLE_GRANULARITY_SEC, "15min")


async def _fetch_candles_twelvedata(client: httpx.AsyncClient, symbol: str) -> List[Candle]:
    """Primary candle source. Returns [] if TwelveData isn't configured or the call fails."""
    if not config.TWELVEDATA_API_KEY or symbol not in _FX_CURRENCY_MAP:
        return []
    base, quote = _FX_CURRENCY_MAP[symbol]
    try:
        resp = await client.get(
            "https://api.twelvedata.com/time_series",
            params={
                "symbol": f"{base}/{quote}",
                "interval": _twelvedata_interval(),
                "outputsize": config.PRICE_LOOKBACK_CANDLES,
                "apikey": config.TWELVEDATA_API_KEY,
            },
            timeout=15,
        )
        data = resp.json()
        values = data.get("values")
        if not values:
            logger.debug(f"TwelveData no candle data for {symbol}: {data.get('message', data)}")
            return []
        # TwelveData returns newest-first; we want oldest-first to match Deriv's ordering
        values = list(reversed(values))
        candles = []
        for v in values:
            try:
                candles.append(Candle(
                    epoch=0,  # TwelveData gives a datetime string, not epoch; not needed downstream
                    open=float(v["open"]), high=float(v["high"]),
                    low=float(v["low"]), close=float(v["close"]),
                ))
            except (KeyError, ValueError):
                continue
        return candles
    except Exception as e:
        logger.debug(f"TwelveData candle fetch failed for {symbol}: {e}")
        return []


async def _fetch_candles_deriv_fallback(symbol: str) -> List[Candle]:
    """
    Fallback only — used when TwelveData is unavailable/unconfigured/fails.
    Deriv's WebSocket is expected to drop periodically per Deriv's own docs
    ("connections can be expected to be interrupted several times a day"),
    so one retry after a brief wait gives the client's reconnect logic a
    chance to self-heal before giving up on this instrument for the cycle.
    """
    try:
        candles = await deriv_client.get_candles(
            symbol, config.PRICE_LOOKBACK_CANDLES, config.PRICE_CANDLE_GRANULARITY_SEC
        )
        if candles:
            return candles
    except Exception as e:
        logger.debug(f"Deriv fallback candle fetch failed for {symbol}, will retry once: {e}")

    await asyncio.sleep(3)
    try:
        return await deriv_client.get_candles(
            symbol, config.PRICE_LOOKBACK_CANDLES, config.PRICE_CANDLE_GRANULARITY_SEC
        )
    except Exception as e:
        logger.warning(f"Deriv fallback candle fetch failed for {symbol} even after retry: {e}")
        return []


async def _fetch_news(client: httpx.AsyncClient, symbol: str) -> List[NewsItem]:
    if not config.NEWSDATA_API_KEY:
        return []
    base, quote = _FX_CURRENCY_MAP.get(symbol, (None, None))
    if not base:
        return []
    query = f"{base} {quote} forex"
    try:
        resp = await client.get(
            "https://newsdata.io/api/1/news",
            params={
                "apikey": config.NEWSDATA_API_KEY,
                "q": query,
                "language": "en",
                "category": "business",
            },
            timeout=15,
        )
        data = resp.json()
        results = data.get("results", [])[: config.NEWS_HEADLINES_PER_INSTRUMENT]
        return [
            NewsItem(title=r.get("title", ""), published_at=r.get("pubDate", ""), source=r.get("source_id", ""))
            for r in results if r.get("title")
        ]
    except Exception as e:
        logger.debug(f"News fetch failed for {symbol}: {e}")
        return []


async def build_instrument_context(http_client: httpx.AsyncClient, symbol: str) -> InstrumentContext:
    label = config.INSTRUMENT_LABELS.get(symbol, symbol)

    # Primary: TwelveData. Fallback: Deriv (only touched if TwelveData fails).
    candles = await _fetch_candles_twelvedata(http_client, symbol)
    source = "twelvedata"
    if not candles:
        candles = await _fetch_candles_deriv_fallback(symbol)
        source = "deriv_fallback" if candles else "none"

    last_price = candles[-1].close if candles else None
    if last_price is None:
        # Last resort: a direct Deriv tick, only if both candle sources failed
        try:
            last_price = await deriv_client.get_last_price(symbol)
        except Exception:
            last_price = None

    pct_change = None
    if len(candles) >= 2 and candles[0].close:
        pct_change = (candles[-1].close - candles[0].close) / candles[0].close * 100

    news = await _fetch_news(http_client, symbol)

    try:
        is_open = await deriv_client.is_symbol_open(symbol)
    except Exception as e:
        logger.debug(f"Market status check failed for {symbol}, leaving unknown: {e}")
        is_open = None

    if source == "deriv_fallback":
        logger.info(f"{symbol}: used Deriv fallback for price data (TwelveData unavailable this cycle)")

    return InstrumentContext(
        symbol=symbol,
        label=label,
        candles=candles,
        last_price=last_price,
        pct_change_lookback=pct_change,
        news=news,
        is_market_open=is_open,
    )


async def build_all_contexts() -> List[InstrumentContext]:
    """Fetches price+news context for every instrument in the universe, concurrently."""
    async with httpx.AsyncClient() as http_client:
        tasks = [build_instrument_context(http_client, sym) for sym in config.INSTRUMENTS]
        contexts = await asyncio.gather(*tasks, return_exceptions=True)

    results: List[InstrumentContext] = []
    for sym, ctx in zip(config.INSTRUMENTS, contexts):
        if isinstance(ctx, Exception):
            logger.error(f"Context build failed for {sym}: {ctx}")
            results.append(InstrumentContext(
                symbol=sym, label=config.INSTRUMENT_LABELS.get(sym, sym), candles=[]
            ))
        else:
            results.append(ctx)

    with_data = sum(1 for c in results if c.candles)
    logger.info(f"Built market context for {len(results)} instruments ({with_data} with usable candle data)")
    return results
