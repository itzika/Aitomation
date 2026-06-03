"""Back-compat shim.

`Workspace` moved to the package root (`aitomation.workspace`) once it became shared
infrastructure for both the CLI and the TUI. This re-export keeps the old import path
(`aitomation.tui.workspace`) working."""

from __future__ import annotations

from ..workspace import SystemRecord, Workspace, slugify

__all__ = ["SystemRecord", "Workspace", "slugify"]
