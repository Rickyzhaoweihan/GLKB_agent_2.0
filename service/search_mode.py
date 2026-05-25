"""
SearchMode enum + ModeContext + build_mode_context for the GLKB Agent.

Two opt-in modes plus a backward-compat AUTO fallback:

- AUTO       : no filter; identical to pre-feature behavior. Used when the
               HTTP request omits `mode` and the session has no default.
- REVIEW     : retrieve review/synthesis literature. Wrapper expands the
               PubMed/Cypher query to match either NLM publication-type tags
               OR title-based heuristics (balanced recall + precision).
- NON_REVIEW : retrieve everything except reviews. Wrapper restricts the
               query to NOT match those same publication types and titles.

All filter logic lives in the tool wrappers (`my_agent/tools.py`); this
module only owns the enum + the user-message prefix that tells the agent
what mode is active and to pass `mode="..."` verbatim.

See docs/plans/2026-05-22-search-mode-design.md.
"""

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional


class SearchMode(str, Enum):
    """The three search modes. `str` mixin lets these serialize cleanly into JSON state."""

    AUTO = "auto"
    REVIEW = "review"
    NON_REVIEW = "non_review"

    @classmethod
    def parse(cls, value: Optional[str]) -> "SearchMode":
        """Tolerant parser: None or unknown values silently fall back to AUTO.

        This lets legacy clients sending `mode="research"` (from the v1 design)
        coerce to AUTO instead of raising a 400.
        """
        if not value:
            return cls.AUTO
        try:
            return cls(str(value).lower())
        except ValueError:
            return cls.AUTO


# NLM PublicationType tags for REVIEW include / NON_REVIEW exclude.
# Kept here so the wrapper code and any future debug payload reference the
# same canonical list.
REVIEW_PUBTYPES: List[str] = [
    "Review",
    "Systematic Review",
    "Meta-Analysis",
]


@dataclass(frozen=True)
class ModeContext:
    """Resolved mode metadata. Pure data, no behavior.

    Built by `build_mode_context`. Consumed by `service/runner.py` (which
    prepends `user_message_prefix` to the LLM-facing message) and by the SSE
    Complete event builder in `service/api.py`.
    """

    mode: SearchMode
    user_message_prefix: str = ""

    def to_constraints_payload(self) -> dict:
        """Serializable summary for the SSE Complete event."""
        return {"mode": self.mode.value}


def build_mode_context(mode: SearchMode) -> ModeContext:
    """Map a SearchMode to its user-message prefix.

    The prefix tells the agent (a) what mode is active and (b) to pass
    `mode="review"` / `mode="non_review"` to `search_pubmed` / `article_search`.
    All actual query expansion / restriction is done inside the tool wrappers —
    the agent does NOT need to manage `article_types` / `exclude_types` lists.
    """
    if mode == SearchMode.REVIEW:
        return ModeContext(
            mode=mode,
            user_message_prefix=(
                "[search-mode: review]\n"
                "Prioritize narrative reviews, systematic reviews, and meta-analyses. "
                "When calling search_pubmed or article_search, pass mode=\"review\". "
                "The wrapper handles all query expansion."
            ),
        )
    if mode == SearchMode.NON_REVIEW:
        return ModeContext(
            mode=mode,
            user_message_prefix=(
                "[search-mode: non_review]\n"
                "Exclude reviews, systematic reviews, and meta-analyses. "
                "When calling search_pubmed or article_search, pass mode=\"non_review\". "
                "The wrapper handles all query restriction."
            ),
        )
    # AUTO: no filters, no prefix — caller treats as current behavior.
    return ModeContext(mode=SearchMode.AUTO)
