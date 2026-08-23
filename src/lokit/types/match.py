from dataclasses import dataclass


@dataclass(slots=True)
class MatchResult:
    """Ranked TM match with a placeholder-safe, ready-to-use translation.

    ``translation`` is ``None`` when the target cannot be safely mapped to the
    query's placeholder graph.  ``can_apply`` additionally respects a required
    inline-tag signature.
    """

    unit_id: str
    score: float
    kind: str
    source_equal: bool
    tags_equal: bool
    previous_equal: bool
    next_equal: bool
    # These fields are intentionally defaulted so the original seven-argument
    # positional constructor remains source compatible.
    translation: str | None = None
    placeholders_reformed: bool = False
    can_apply: bool = False
