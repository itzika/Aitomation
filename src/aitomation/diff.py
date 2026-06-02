"""Diff two CoverageInventories so re-discovery is incremental, not a full rebuild.

The toolkit is meant to be pointed at the same system repeatedly as it evolves. To draft
tests only for *new* surface (and to flag tests whose surface *changed* underneath them),
we compare a freshly-discovered inventory against the previously-saved baseline.

Identity is by a stable key (method+path for endpoints, location+name otherwise) so a
renamed description or shuffled order doesn't read as add+remove. "Changed" is decided by a
fingerprint over only the *test-relevant* fields (method, path, inputs, preconditions) —
advisory fields like description/priority/example don't make an existing test stale.

Pure functions, no I/O: trivially testable and reused by both the CLI and the TUI.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .models import CoverageInventory, Journey, TestableElement


def element_key(e: TestableElement) -> str:
    """Stable identity for an element across discovers. Deliberately excludes the element's
    `name`, which the LLM phrases differently each run — identity must come from the system,
    not the wording. Endpoints: method+path. Pages: URL. Forms: URL + their input field names
    (from the HTML, so stable) to tell apart multiple forms on one page."""
    if e.kind == "endpoint":
        return f"endpoint {(e.method or '').upper()} {e.location}".strip()
    if e.kind == "page":
        return f"page {e.location}".strip()
    if e.kind == "form":
        fields = ",".join(sorted(i.name for i in e.inputs))
        return f"form {e.location} [{fields}]".strip()
    # flow / auth and anything else: prefer location, fall back to name only if it's blank.
    return f"{e.kind} {e.location or e.name}".strip()


def _name_to_key(inv: CoverageInventory) -> dict[str, str]:
    return {e.name: element_key(e) for e in inv.elements}


def journey_key(journey: Journey, name_to_key: dict[str, str]) -> str:
    """Stable identity for a journey: the SET of element keys it touches, not its name.

    Journey names/phrasing are LLM-generated and drift between discovers, so naming can't be
    identity — the same flow would look new every time. The elements it exercises (resolved to
    their stable method+path keys) are what actually defines the flow. Falls back to the
    normalised name only when a journey resolves to no known elements."""
    keys = sorted({name_to_key[n] for n in journey.elements if n in name_to_key})
    return "flow:" + "|".join(keys) if keys else "flow-name:" + journey.name.strip().lower()


def journey_fingerprint(inv: CoverageInventory, journey: Journey) -> str:
    """Short, file-embeddable hash of a journey's stable identity (see `journey_key`).
    The write stage stamps this into each test so a renamed-but-identical flow isn't
    re-drafted on the next discover."""
    return hashlib.sha256(journey_key(journey, _name_to_key(inv)).encode("utf-8")).hexdigest()[:12]


def element_fingerprint(e: TestableElement) -> str:
    """Hash of only the fields that, if changed, can make an existing test wrong: method,
    path, kind, and each input's (name, where, type, required). Description/priority/example
    are advisory and deliberately excluded — they don't invalidate a drafted test."""
    inputs = sorted(
        (i.name, (i.where or ""), (i.type or ""), bool(i.required)) for i in e.inputs
    )
    payload = (
        (e.method or "").upper(),
        e.location,
        e.kind,
        tuple(inputs),
        tuple(sorted(e.preconditions or [])),
    )
    return hashlib.sha256(repr(payload).encode("utf-8")).hexdigest()[:16]


@dataclass(slots=True)
class InventoryDiff:
    added_elements: list[TestableElement] = field(default_factory=list)
    removed_elements: list[TestableElement] = field(default_factory=list)
    # (old, new) pairs for elements present in both whose fingerprint changed.
    changed_elements: list[tuple[TestableElement, TestableElement]] = field(default_factory=list)
    # Element-driven (NOT a journey-set diff, which churns on LLM rename/regroup):
    added_journeys: list[Journey] = field(default_factory=list)  # touch genuinely-new surface
    removed_journeys: list[Journey] = field(default_factory=list)  # touched now-removed surface
    # Touch changed (but not new) surface — drafted test may be stale; re-draft with --force.
    affected_journeys: list[Journey] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (
            self.added_elements
            or self.removed_elements
            or self.changed_elements
            or self.added_journeys
            or self.removed_journeys
            or self.affected_journeys
        )

    def summary(self) -> str:
        s = (
            f"elements +{len(self.added_elements)} ~{len(self.changed_elements)} "
            f"-{len(self.removed_elements)} · "
            f"journeys +{len(self.added_journeys)} -{len(self.removed_journeys)}"
        )
        if self.affected_journeys:
            s += f" · {len(self.affected_journeys)} existing flow(s) may be stale"
        return s


def diff_inventories(old: CoverageInventory, new: CoverageInventory) -> InventoryDiff:
    """Compare `old` (baseline) against `new` (freshly discovered)."""
    old_by_key = {element_key(e): e for e in old.elements}
    new_by_key = {element_key(e): e for e in new.elements}

    added = [new_by_key[k] for k in sorted(new_by_key.keys() - old_by_key.keys())]
    removed = [old_by_key[k] for k in sorted(old_by_key.keys() - new_by_key.keys())]
    changed = [
        (old_by_key[k], new_by_key[k])
        for k in sorted(old_by_key.keys() & new_by_key.keys())
        if element_fingerprint(old_by_key[k]) != element_fingerprint(new_by_key[k])
    ]

    # Journeys are NOT diffed against each other: the LLM renames AND regroups the same
    # surface into different journeys every discover, so any journey-set comparison churns.
    # Instead, classify journeys purely by the ELEMENT changes they touch — stable, because
    # element identity comes from the system, not the model's wording.
    old_nk, new_nk = _name_to_key(old), _name_to_key(new)
    added_keys = {element_key(e) for e in added}
    changed_keys = {element_key(ne) for _, ne in changed}
    removed_keys = {element_key(e) for e in removed}

    def _touches(journey: Journey, nk: dict[str, str], keys: set[str]) -> bool:
        return any(nk.get(en) in keys for en in journey.elements)

    # "New flow(s) to draft" = flows exercising genuinely-new surface (only ever non-empty
    # when an element was actually added — never from a mere rename/regroup).
    added_journeys = sorted(
        (j for j in new.suggested_journeys if _touches(j, new_nk, added_keys)),
        key=lambda j: j.name,
    )
    # Flows whose surface CHANGED underneath them (but isn't new) → drafted test may be stale.
    affected = sorted(
        (
            j
            for j in new.suggested_journeys
            if not _touches(j, new_nk, added_keys) and _touches(j, new_nk, changed_keys)
        ),
        key=lambda j: j.name,
    )
    # Flows that lost surface (touched an element that's now gone).
    removed_journeys = sorted(
        (j for j in old.suggested_journeys if _touches(j, old_nk, removed_keys)),
        key=lambda j: j.name,
    )

    return InventoryDiff(
        added_elements=added,
        removed_elements=removed,
        changed_elements=changed,
        added_journeys=added_journeys,
        removed_journeys=removed_journeys,
        affected_journeys=affected,
    )
