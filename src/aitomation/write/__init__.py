"""Write stage: generate first-draft pytest+Playwright tests, one per journey, into a
scaffold for human review.

The AI is the author here, never the judge: it writes deterministic assertions once, the
test runner evaluates them forever. Drafts land as files; nothing auto-merges or runs as
truth. Grounded in the inventory so the tests reference real paths/fields, not guesses.
"""

from .generator import (
    EnableResult,
    HealReport,
    HealResult,
    WriteReport,
    draft_tests,
    enable_drafts,
    find_skipped_drafts,
    heal_failing_tests,
    is_destructive,
    select_journeys,
)

__all__ = [
    "draft_tests",
    "heal_failing_tests",
    "select_journeys",
    "is_destructive",
    "enable_drafts",
    "find_skipped_drafts",
    "WriteReport",
    "HealReport",
    "HealResult",
    "EnableResult",
]
