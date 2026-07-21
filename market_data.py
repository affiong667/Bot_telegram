"""
Fetches real market data to ground the AI models' signals — this is what
makes the prompts based on actual price action/news rather than pure
model imagination.

Price data: Deriv's own tick/candle history is always available (no extra
key). If TWELVEDATA_API_KEY is set, we additionally cross-check with an
independent source for forex instruments (Deriv is itself a broker/market
maker, so an independent price feed is a useful sanity check — divergence
can indicate a data issue).

News data: NewsData.io free tier, queried per forex pair's base/quote
currency. Synthetic/volatility indices are Deriv-internal random-walk
instruments with no real-world news correlate, so news fetch is skipped
for them (see config.NEWS_RELEVANT_PREFIXES).
"""

import asyncio
import logging
from typing import List, Optional

import httpx

import config
from models import InstrumentContext, NewsItem
from deriv_client import client as deriv_client

logger = logging.getLogger(__name__)

# Maps Deriv forex symbols to (base, quote) currency codes for news queries
_FX_CURRENCY_MAP = {
    "frxEURUSD": ("EUR", "USD"), "frxGBPUSD": ("GBP", "USD"), "frxUSDJPY": ("USD", "JPY"),
    "frxUSDCHF": ("USD", "CHF"), "frxAUDUSD": ("AUD", "USD"), "frxUSDCAD": ("USD", "CAD"),
    "frxNZDUSD": ("NZD", "USD"), "frxEURGBP": ("EUR", "GBP"), "frxEURJPY": ("EUR", "JPY"),
    "frxGBPJPY": ("GBP", "JPY"), "frxEURAUD": ("EUR", "AUD"), "frxEURCHF": ("EUR", "CHF"),
    "frxAUDJPY": ("AUD", "JPY"), "frxGBPAUD": ("GBP", "AUD"), "frxGBPCAD": ("GBP", "CAD"),
}


def _is_news_relevant(symbol: str) -> bool:
    return symbol.startswith(config.NEWS_RELEVANT_PREFIXES)


async def _fetch_candles_deriv(symbol: str) -> List:
    return await deriv_client.get_candles(
        symbol, config.PRICE_LOOKBACK_CANDLES, config.PRICE_CANDLE_GRANULARITY_SEC
    )


async def _fetch_twelvedata_price(client: httpx.AsyncClient, symbol: str) -> Optional[float]:
    """Independent cross-check price for forex pairs, if TWELVEDATA_API_KEY is configured."""
    if not config.TWELVEDATA_API_KEY or symbol not in _FX_CURRENCY_MAP:
        return None
    base, quote = _FX_CURRENCY_MAP[symbol]
    try:
        resp = await client.get(
            "https://api.twelvedata.com/price",
            params={"symbol": f"{base}/{quote}", "apikey": config.TWELVEDATA_API_KEY},
            timeout=15,
        )
        data = resp.json()
        if "price" in data:
            return float(data["price"])
        logger.debug(f"TwelveData no price for {symbol}: {data}")
    except Exception as e:
        logger.debug(f"TwelveData fetch failed for {symbol}: {e}")
    return None


async def _fetch_news(client: httpx.AsyncClient, symbol: str) -> List[NewsItem]:
    if not config.NEWSDATA_API_KEY or not _is_news_relevant(symbol):
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

    candles = await _fetch_candles_deriv(symbol)
    last_price = candles[-1].close if candles else await deriv_client.get_last_price(symbol)

    pct_change = None
    if len(candles) >= 2 and candles[0].close:
        pct_change = (candles[-1].close - candles[0].close) / candles[0].close * 100

    # Independent cross-check (logged only, not injected into the prompt to
    # avoid confusing the model with two slightly different numbers —
    # but a large divergence is worth knowing about operationally).
    if last_price is not None:
        cross_check = await _fetch_twelvedata_price(http_client, symbol)
        if cross_check is not None and last_price:
            divergence_pct = abs(cross_check - last_price) / last_price * 100
            if divergence_pct > 0.5:
                logger.warning(
                    f"{symbol}: Deriv price {last_price} vs TwelveData {cross_check} "
                    f"diverge by {divergence_pct:.2f}%"
                )

    news = await _fetch_news(http_client, symbol)

    return InstrumentContext(
        symbol=symbol,
        label=label,
        candles=candles,
        last_price=last_price,
        pct_change_lookback=pct_change,
        news=news,
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
