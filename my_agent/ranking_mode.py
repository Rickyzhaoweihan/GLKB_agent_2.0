"""
Ranking mode helpers for literature retrieval.

This module is intentionally independent from the FastAPI service layer so both
the service runner and agent tools can import the same ranking contract.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class RankingMode(str, Enum):
    """Supported article ranking strategies."""

    DEFAULT = "default"
    HIGH_IMPACT = "high_impact"
    RECENT = "recent"

    @classmethod
    def parse(cls, value: Optional[str]) -> "RankingMode":
        """Parse user/API input without rejecting requests on unknown values."""
        if not value:
            return cls.DEFAULT
        try:
            return cls(str(value).lower())
        except ValueError:
            return cls.DEFAULT


@dataclass(frozen=True)
class RankingContext:
    """Resolved ranking metadata for a single agent turn."""

    ranking_mode: RankingMode
    user_message_prefix: str = ""

    def to_payload(self) -> dict:
        return {
            "ranking_mode": self.ranking_mode.value,
        }


def build_ranking_context(ranking_mode: RankingMode) -> RankingContext:
    """Build LLM-facing instructions for the selected ranking strategy."""
    if ranking_mode == RankingMode.HIGH_IMPACT:
        return RankingContext(
            ranking_mode=ranking_mode,
            user_message_prefix=(
                "[ranking-mode: high_impact]\n"
                "Prioritize high-impact biomedical papers. When calling "
                "article_search, pass ranking_mode=\"high_impact\". Rank by "
                "title relevance, citation count, and journal impact factor."
            ),
        )
    if ranking_mode == RankingMode.RECENT:
        return RankingContext(
            ranking_mode=ranking_mode,
            user_message_prefix=(
                "[ranking-mode: recent]\n"
                "Prefer more recent articles while preserving biomedical relevance. "
                "When calling article_search, pass ranking_mode=\"recent\"."
            ),
        )
    return RankingContext(ranking_mode=RankingMode.DEFAULT)
