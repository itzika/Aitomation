"""Inventory → Copier context, and the Copier run that emits the scaffold.

Deterministic by design: same inventory in, same project out. The scaffold targets
pytest + pytest-playwright and adapts to what was discovered — page objects for web
pages/forms, an API client for endpoints, and an auth fixture chosen from
`inventory.auth_strategy`.
"""

from __future__ import annotations

import re
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any

from ..models import CoverageInventory

# auth_strategy values that map to a bearer-token style header.
_BEARER_LIKE = {"bearer", "oauth2", "oauth", "apikey", "api_key", "token", "jwt"}


def _slug(name: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "_", name).strip("_").lower()
    return s or "system_under_test"


def _class_name(name: str) -> str:
    parts = [p for p in re.split(r"[^0-9a-zA-Z]+", name) if p]
    cn = "".join(p[:1].upper() + p[1:] for p in parts) or "Page"
    if not cn.endswith("Page"):
        cn += "Page"
    return "P" + cn if cn[0].isdigit() else cn


def _func_name(name: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "_", name).strip("_").lower() or "step"
    return "f_" + s if s[0].isdigit() else s


def _normalize_auth(auth_strategy: str | None) -> str:
    """Collapse the free-text auth strategy into a template-friendly token. Fallback for
    inventories without structured `auth_schemes` (e.g. the crawl path)."""
    if not auth_strategy:
        return "none"
    a = auth_strategy.strip().lower()
    if a in {"none", "null", ""}:
        return "none"
    if a == "basic":
        return "basic"
    if a == "session":
        return "session"
    if any(tok in a for tok in _BEARER_LIKE):
        return "bearer"
    return "session" if "cookie" in a else "bearer"


# Pick a primary scheme deterministically when a spec declares several: prefer the ones a
# single env token serves cleanly (apiKey-in-header, then bearer), then basic, then the rest.
def _scheme_rank(s: "AuthScheme") -> int:  # type: ignore[name-defined]
    t = (s.type or "").lower()
    loc = (s.location or "").lower()
    if t == "apikey" and loc in ("", "header"):
        return 0
    if t in ("http", "oauth2", "openidconnect") and (s.scheme or "bearer").lower() != "basic":
        return 1
    if t == "http" and (s.scheme or "").lower() == "basic":
        return 2
    if t == "apikey":  # query/cookie
        return 3
    return 4


def _auth_context(inv: "CoverageInventory") -> dict[str, Any]:  # type: ignore[name-defined]
    """Resolve the auth fixture shape from structured schemes (preferred) or the free-text
    strategy (fallback). Returns auth_kind + the header/param details the template needs."""
    schemes = list(getattr(inv, "auth_schemes", []) or [])
    if not schemes:
        return {
            "auth_kind": _normalize_auth(inv.auth_strategy),
            "auth_header_name": "Authorization",
            "auth_in": "header",
        }

    primary = min(schemes, key=_scheme_rank)
    t = (primary.type or "").lower()
    if t == "apikey":
        return {
            "auth_kind": "apikey",
            "auth_header_name": primary.name or "X-API-Key",
            "auth_in": (primary.location or "header").lower(),
        }
    if t == "http" and (primary.scheme or "").lower() == "basic":
        return {"auth_kind": "basic", "auth_header_name": "Authorization", "auth_in": "header"}
    if t in ("http", "oauth2", "openidconnect"):
        return {"auth_kind": "bearer", "auth_header_name": "Authorization", "auth_in": "header"}
    return {"auth_kind": "none", "auth_header_name": "Authorization", "auth_in": "header"}


def inventory_to_context(inv: CoverageInventory) -> dict[str, Any]:
    """Build the Copier render context from an inventory. Pure and deterministic."""
    pages = [
        {
            "name": e.name,
            "class_name": _class_name(e.name),
            "location": e.location,
            "description": e.description.replace('"', "'"),
            "is_form": e.kind == "form",
            # discovered form fields → seeded locators from the crawl's *observed* locator
            # (preferring data-qa/label/placeholder), with .first for non-unique ones.
            "fields": [
                {
                    "attr": _func_name(i.name),
                    # full locator expression incl. .first for non-unique matches
                    "locator": (i.locator or f'get_by_label("{i.name}")')
                    + ("" if i.unique else ".first"),
                }
                for i in e.inputs
                if i.where in ("form", "unknown")
            ],
        }
        for e in inv.elements
        if e.kind in ("page", "form")
    ]
    endpoints = [
        {
            "name": e.name,
            "func_name": _func_name(e.name),
            "method": (e.method or "GET").upper(),
            "path": e.location,
            # Playwright joins base_url + path via URL resolution, so request paths must be
            # RELATIVE (no leading slash) or a base path like `/api` gets discarded.
            "rel_path": e.location.lstrip("/"),
            "description": e.description.replace('"', "'"),
        }
        for e in inv.elements
        if e.kind == "endpoint"
    ]
    journeys = [
        {
            "name": j.name,
            "func_name": _func_name(j.name),
            "description": j.description.replace('"', "'"),
            "steps": [s.action for s in j.steps],
        }
        for j in inv.suggested_journeys
    ]

    # Smoke test does a GET, so prefer a GET endpoint with no path params (relative path).
    smoke_path = ""
    if endpoints:
        chosen = (
            next((e for e in endpoints if e["method"] == "GET" and "{" not in e["path"]), None)
            or next((e for e in endpoints if "{" not in e["path"]), None)
            or endpoints[0]
        )
        smoke_path = chosen["rel_path"]

    auth = _auth_context(inv)
    return {
        "project_name": inv.system_name,
        "package_slug": _slug(inv.system_name),
        # normalise: a trailing slash makes f"{base_url}/path" produce a double slash
        "base_url": inv.base_url.rstrip("/") or inv.base_url,
        "auth_strategy": auth["auth_kind"],  # display + back-compat
        "auth_kind": auth["auth_kind"],
        "auth_header_name": auth["auth_header_name"],
        "auth_in": auth["auth_in"],
        "has_browser": bool(pages),
        "has_api": bool(endpoints),
        "pages": pages,
        "endpoints": endpoints,
        "journeys": journeys,
        "smoke_path": smoke_path,
        "source": inv.source,
        "generated_at": inv.generated_at.isoformat(),
    }


def scaffold_project(
    inventory: CoverageInventory, dest: Path | str, *, overwrite: bool = True
) -> Path:
    """Render the pytest+playwright scaffold for `inventory` into `dest`. Returns dest."""
    from copier import run_copy

    dest = Path(dest)
    context = inventory_to_context(inventory)
    template_root = files("aitomation.scaffold").joinpath("template")
    with as_file(template_root) as template_path:
        run_copy(
            str(template_path),
            str(dest),
            data=context,
            defaults=True,
            overwrite=overwrite,
            quiet=True,
        )
    return dest
