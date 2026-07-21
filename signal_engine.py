"""
Queries Groq, OpenRouter, and Gemini for trading signals across the
instrument universe, in parallel, grounded in real price/news data
fetched by market_data.py, and parses their responses into ModelVote
objects.

Each provider is asked to return signals ONLY for instruments where it has
>= config.MIN_CONFIDENCE conviction. We still re-validate confidence and
direction locally after parsing — never trust the model's own filtering.
"""

import asyncio
import json
import logging
import re
from typing import List, Dict, Any

import httpx

import config
import runtime_settings
from models import ModelVote, Direction, InstrumentContext

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a disciplined binary options trading signal generator.
You will be given real recent price data (and, where available, recent
headlines) for a list of trading instruments, plus a stated timeframe bias.

For EACH instrument, decide if the provided data supports a high-conviction
directional call over the next ~1 hour.

Rules:
- Base your call ONLY on the price/news data actually provided below. Do not
  invent data points. If the data for an instrument is marked "unavailable",
  do not include it in your output.
- Only include an instrument in your output if your confidence is {min_confidence} or higher.
- Confidence is an integer 0-100 reflecting your certainty in the direction.
- direction must be exactly one of: "bullish", "bearish". Omit instruments you're not confident about — do not output "neutral".
- Return STRICT JSON ONLY, no prose, no markdown fences. Format:

{{"signals": [{{"symbol": "frxEURUSD", "direction": "bullish", "confidence": 85, "rationale": "short reason citing the data"}}]}}

If you have no high-confidence signals, return {{"signals": []}}.
"""

USER_PROMPT_HEADER = """Timeframe bias: {timeframe_bias}

Market data for each instrument follows. Base your signals strictly on this data.

"""


def _build_user_prompt(contexts: List[InstrumentContext]) -> str:
    blocks = [c.to_prompt_block() for c in contexts]
    header = USER_PROMPT_HEADER.format(timeframe_bias=config.TIMEFRAME_BIAS)
    return header + "\n\n".join(blocks) + "\n\nReturn your JSON now."


def _build_system_prompt() -> str:
    return SYSTEM_PROMPT.format(min_confidence=runtime_settings.min_confidence)


def _extract_json(text: str) -> Dict[str, Any]:
    """Models sometimes wrap JSON in markdown fences or add stray text. Strip that defensively."""
    text = text.strip()
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        text = match.group(0)
    return json.loads(text)


def _parse_signals(model_name: str, raw_text: str, valid_symbols: set) -> List[ModelVote]:
    votes: List[ModelVote] = []
    try:
        data = _extract_json(raw_text)
        signals = data.get("signals", [])
    except (json.JSONDecodeError, AttributeError) as e:
        logger.warning(f"[{model_name}] failed to parse JSON response: {e}. Raw: {raw_text[:300]}")
        return votes

    for sig in signals:
        try:
            symbol = str(sig["symbol"]).strip()
            direction_raw = str(sig["direction"]).lower().strip()
            confidence = int(sig["confidence"])
            rationale = str(sig.get("rationale", ""))

            if symbol not in valid_symbols:
                continue
            if direction_raw not in ("bullish", "bearish"):
                continue
            if confidence < runtime_settings.min_confidence:
                continue

            votes.append(ModelVote(
                model_name=model_name,
                symbol=symbol,
                direction=Direction(direction_raw),
                confidence=confidence,
                rationale=rationale,
            ))
        except (KeyError, ValueError, TypeError) as e:
            logger.warning(f"[{model_name}] skipping malformed signal {sig}: {e}")
            continue

    return votes


async def _call_groq(client: httpx.AsyncClient, system: str, user: str, valid_symbols: set) -> List[ModelVote]:
    try:
        resp = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"},
            json={
                "model": config.GROQ_MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
            },
            timeout=90.0,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"]
        return _parse_signals("groq", text, valid_symbols)
    except Exception as e:
        logger.error(f"Groq call failed: {e}")
        return []


async def _call_openrouter(client: httpx.AsyncClient, system: str, user: str, valid_symbols: set) -> List[ModelVote]:
    try:
        resp = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}"},
            json={
                "model": config.OPENROUTER_MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.2,
            },
            timeout=90.0,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"]
        return _parse_signals("openrouter", text, valid_symbols)
    except Exception as e:
        logger.error(f"OpenRouter call failed: {e}")
        return []


async def _call_gemini(client: httpx.AsyncClient, system: str, user: str, valid_symbols: set) -> List[ModelVote]:
    try:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{config.GEMINI_MODEL}:generateContent?key={config.GEMINI_API_KEY}"
        )
        resp = await client.post(
            url,
            json={
                "contents": [{"parts": [{"text": user}]}],
                "systemInstruction": {"parts": [{"text": system}]},
                "generationConfig": {
                    "temperature": 0.2,
                    "responseMimeType": "application/json",
                },
            },
            timeout=90.0,
        )
        resp.raise_for_status()
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        return _parse_signals("gemini", text, valid_symbols)
    except Exception as e:
        logger.error(f"Gemini call failed: {e}")
        return []


async def collect_all_votes(contexts: List[InstrumentContext]) -> List[ModelVote]:
    """
    Fires all three provider calls concurrently using the given real-data
    contexts, returns the combined vote list.
    """
    system = _build_system_prompt()
    user = _build_user_prompt(contexts)
    valid_symbols = {c.symbol for c in contexts}

    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            _call_groq(client, system, user, valid_symbols),
            _call_openrouter(client, system, user, valid_symbols),
            _call_gemini(client, system, user, valid_symbols),
        )
    all_votes = [v for group in results for v in group]
    logger.info(
        f"Collected {len(all_votes)} raw votes "
        f"(groq={len(results[0])}, openrouter={len(results[1])}, gemini={len(results[2])})"
    )
    return all_votes
