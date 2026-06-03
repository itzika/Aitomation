"""Discovery Toolkit — compresses the cold-start of test automation.

Discover (the differentiated core) → Scaffold → Write. AI is the analyst and author,
never the judge: the toolkit discovers, scaffolds, and drafts, but committed tests use
deterministic assertions and nothing auto-merges.
"""

from .config import LLMConfig
from .models import (
    AuthScheme,
    CoverageInventory,
    InputField,
    Journey,
    JourneyStep,
    TestableElement,
)
from .providers import LLMProvider, PydanticAIProvider

__version__ = "0.1.0"

__all__ = [
    "AuthScheme",
    "CoverageInventory",
    "InputField",
    "Journey",
    "JourneyStep",
    "LLMConfig",
    "LLMProvider",
    "PydanticAIProvider",
    "TestableElement",
]
