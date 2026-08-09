from dataclasses import dataclass


@dataclass(slots=True)
class MatchResult:
    """Ranked translation-memory match returned by Lokit matching APIs."""

    unit_id: str
    score: float
    kind: str
    source_equal: bool
    tags_equal: bool
    previous_equal: bool
    next_equal: bool
