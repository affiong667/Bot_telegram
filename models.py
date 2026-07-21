"""Shared data structures used across the bot."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Optional


class Direction(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


@dataclass
class Candle:
    epoch: int
    open: float
    high: float
    low: float
    close: float


@dataclass
class NewsItem:
    title: str
    published_at: str = ""
    source: str = ""


@dataclass
class InstrumentContext:
    """Real market data gathered for one instrument, injected into LLM prompts."""
    symbol: str
    label: str
    candles: List[Candle]
    last_price: Optional[float] = None
    pct_change_lookback: Optional[float] = None  # % change over the candle window
    news: List[NewsItem] = field(default_factory=list)

    def to_prompt_block(self) -> str:
        lines = [f"### {self.label} ({self.symbol})"]
        if self.last_price is not None:
            lines.append(f"Last price: {self.last_price}")
        if self.pct_change_lookback is not None:
            lines.append(f"Change over lookback window: {self.pct_change_lookback:+.3f}%")
        if self.candles:
            recent = self.candles[-5:]
            candle_str = "; ".join(
                f"O:{c.open:.5f} H:{c.high:.5f} L:{c.low:.5f} C:{c.close:.5f}" for c in recent
            )
            lines.append(f"Last {len(recent)} candles: {candle_str}")
        else:
            lines.append("Price data: unavailable")
        if self.news:
            lines.append("Recent headlines:")
            for n in self.news:
                lines.append(f"  - {n.title}")
        return "\n".join(lines)


@dataclass
class ModelVote:
    """A single AI model's opinion on a single instrument."""
    model_name: str          # "groq" | "openrouter" | "gemini"
    symbol: str
    direction: Direction
    confidence: int          # 0-100
    rationale: str = ""      # short free-text reason, optional, for logging/telegram


@dataclass
class ConsensusSignal:
    """The result of merging votes for one instrument into a tradeable (or rejected) signal."""
    symbol: str
    direction: Optional[Direction]
    agreeing_models: List[str]
    avg_confidence: float
    votes: List[ModelVote]
    qualifies: bool
    reason: str = ""


@dataclass
class TradeRecord:
    id: Optional[int] = None
    symbol: str = ""
    direction: str = ""              # "bullish" | "bearish"
    contract_type: str = ""          # "CALL" | "PUT"
    deriv_contract_id: str = ""
    entry_spot: float = 0.0
    entry_time: Optional[datetime] = None
    exit_spot: Optional[float] = None
    exit_time: Optional[datetime] = None
    stake: float = 0.0
    payout: Optional[float] = None   # potential payout quoted at purchase
    duration_minutes: int = 60       # duration THIS trade was opened with (not the current global setting)
    avg_confidence: float = 0.0
    agreeing_models: str = ""        # comma-joined for storage simplicity
    status: str = "open"             # "open" | "won" | "lost" | "failed"
    pnl: Optional[float] = None
    pnl_pct: Optional[float] = None
