"""Shared naming helpers.

Lives at the package root (not under `tui/`) so both the CLI and the TUI derive the SAME
slug for a system — that's what lets `scaffold`, `write`, and the TUI workspace all agree on
the `projects/<slug>/` directory for a given inventory."""

from __future__ import annotations

import re

PROJECTS_ROOT = "projects"


def slugify(name: str) -> str:
    """A filesystem-safe slug for a system name, e.g. 'Rick & Morty API' -> 'rick-morty-api'."""
    s = re.sub(r"[^0-9a-zA-Z]+", "-", name).strip("-").lower()
    return s or "system"
