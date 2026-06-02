"""Scaffold stage: deterministically template a runnable pytest + Playwright project
from a CoverageInventory. No LLM here — the AI's only role (picking sensible defaults like
auth_strategy) already happened during Discover, so generation is fully reproducible."""

from .generator import inventory_to_context, scaffold_project

__all__ = ["scaffold_project", "inventory_to_context"]
