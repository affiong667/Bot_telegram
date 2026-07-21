"""
Merges per-model votes into per-instrument consensus signals, applies the
agreement + confidence rules, ranks, and selects the top N to trade.

Rule recap (config-driven):
- An instrument qualifies if >= MIN_MODELS_AGREE models agree on direction,
  each of those agreeing votes individually >= MIN_CONFIDENCE.
- If REQUIRE_THREE_WHEN_AVAILABLE is True: among qualifying instruments,
  3/3-agreement ones are ranked strictly above 2/3-agreement ones,
  regardless of raw confidence average. This is a "prefer full consensus"
  policy, not a hard requirement — 2/3 instruments still qualify and can be
  traded if there aren't enough 3/3 ones to fill NUM_SIGNALS.
"""

import logging
from collections import defaultdict
from typing import List, Dict

import config
from models import ModelVote, ConsensusSignal, Direction

logger = logging.getLogger(__name__)


def build_consensus(votes: List[ModelVote]) -> List[ConsensusSignal]:
    by_symbol: Dict[str, List[ModelVote]] = defaultdict(list)
    for v in votes:
        by_symbol[v.symbol].append(v)

    consensus_signals: List[ConsensusSignal] = []

    for symbol, symbol_votes in by_symbol.items():
        by_direction: Dict[Direction, List[ModelVote]] = defaultdict(list)
        for v in symbol_votes:
            by_direction[v.direction].append(v)

        # Pick whichever direction has the most agreeing models (ties broken by higher avg confidence)
        best_direction = None
        best_votes: List[ModelVote] = []
        for direction, dvotes in by_direction.items():
            if len(dvotes) > len(best_votes) or (
                len(dvotes) == len(best_votes) and best_votes and
                _avg_conf(dvotes) > _avg_conf(best_votes)
            ):
                best_direction = direction
                best_votes = dvotes

        agree_count = len(best_votes)
        avg_conf = _avg_conf(best_votes)

        qualifies = agree_count >= config.MIN_MODELS_AGREE
        reason = ""
        if not qualifies:
            reason = f"only {agree_count} model(s) agreed (need {config.MIN_MODELS_AGREE})"

        consensus_signals.append(ConsensusSignal(
            symbol=symbol,
            direction=best_direction if qualifies else None,
            agreeing_models=[v.model_name for v in best_votes] if qualifies else [],
            avg_confidence=avg_conf,
            votes=symbol_votes,
            qualifies=qualifies,
            reason=reason,
        ))

    return consensus_signals


def _avg_conf(votes: List[ModelVote]) -> float:
    if not votes:
        return 0.0
    return sum(v.confidence for v in votes) / len(votes)


def rank_and_select(signals: List[ConsensusSignal], num_signals: int = None) -> List[ConsensusSignal]:
    """Applies the 3-over-2 preference policy and returns the top `num_signals` to trade.
    Defaults to config.NUM_SIGNALS if not explicitly passed (callers that want
    the Telegram-adjustable value should pass runtime_settings.num_signals)."""
    if num_signals is None:
        num_signals = config.NUM_SIGNALS
    qualifying = [s for s in signals if s.qualifies]

    if config.REQUIRE_THREE_WHEN_AVAILABLE:
        three_way = [s for s in qualifying if len(s.agreeing_models) == 3]
        two_way = [s for s in qualifying if len(s.agreeing_models) == 2]
        three_way.sort(key=lambda s: s.avg_confidence, reverse=True)
        two_way.sort(key=lambda s: s.avg_confidence, reverse=True)
        ordered = three_way + two_way
    else:
        ordered = sorted(qualifying, key=lambda s: (len(s.agreeing_models), s.avg_confidence), reverse=True)

    selected = ordered[:num_signals]

    logger.info(
        f"Consensus: {len(qualifying)} qualifying tickers "
        f"({sum(1 for s in qualifying if len(s.agreeing_models) == 3)} at 3/3, "
        f"{sum(1 for s in qualifying if len(s.agreeing_models) == 2)} at 2/3). "
        f"Selected {len(selected)} for trading."
    )
    for s in selected:
        logger.info(
            f"  -> {s.symbol} {s.direction.value} "
            f"[{'/'.join(s.agreeing_models)}] avg_conf={s.avg_confidence:.1f}"
        )

    return selected
