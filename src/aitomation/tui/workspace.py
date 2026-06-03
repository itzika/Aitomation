"""Workspace persistence: the set of systems you've discovered, browsable across sessions.

Layout is visible and organised by the tested app, with one timestamped directory per
generation run so nothing clobbers and runs are diffable/archivable:

    <output_root>/<app-slug>/
        .aito/system.json        # our metadata + saved inventory (index)
        e2e/run-<YYYYMMDD-HHMMSS>/   # a self-contained scaffold + drafted tests per run

This persists the artifacts we produce — NOT test results (the deferred triage product)."""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..models import CoverageInventory
from ..naming import slugify  # re-exported for callers importing it from here

__all__ = ["SystemRecord", "Workspace", "slugify"]


@dataclass(slots=True)
class SystemRecord:
    slug: str
    name: str
    source: str
    origin: str
    base_url: str
    n_elements: int
    n_journeys: int
    scaffolded: bool
    drafted: bool
    updated_at: str
    latest_run: str | None = None  # path to the most recent e2e run directory

    @property
    def stage_dots(self) -> str:
        return "".join(["●", "●" if self.scaffolded else "○", "●" if self.drafted else "○"])


class Workspace:
    def __init__(self, output_root: str | Path = ".") -> None:
        self.root = Path(output_root)

    # -- paths --------------------------------------------------------------------------

    def app_dir(self, slug: str) -> Path:
        return self.root / slug

    def e2e_root(self, slug: str) -> Path:
        return self.app_dir(slug) / "e2e"

    def _meta(self, slug: str) -> Path:
        return self.app_dir(slug) / ".aito" / "system.json"

    def new_run(self, slug: str) -> Path:
        """Create and return a fresh timestamped run directory for this system."""
        stamp = datetime.now().strftime("run-%Y%m%d-%H%M%S")
        run = self.e2e_root(slug) / stamp
        n = 2
        while run.exists():
            run = self.e2e_root(slug) / f"{stamp}-{n}"
            n += 1
        run.mkdir(parents=True, exist_ok=True)
        return run

    def latest_run(self, slug: str) -> Path | None:
        root = self.e2e_root(slug)
        if not root.is_dir():
            return None
        runs = sorted((d for d in root.iterdir() if d.is_dir()), key=lambda d: d.name)
        return runs[-1] if runs else None

    # -- index --------------------------------------------------------------------------

    def list_systems(self) -> list[SystemRecord]:
        if not self.root.is_dir():
            return []
        records: list[SystemRecord] = []
        for d in self.root.iterdir():
            meta = d / ".aito" / "system.json"
            if meta.is_file():
                try:
                    records.append(
                        SystemRecord(**json.loads(meta.read_text(encoding="utf-8"))["meta"])
                    )
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue
        records.sort(key=lambda r: r.updated_at, reverse=True)
        return records

    def save(
        self,
        inv: CoverageInventory,
        *,
        origin: str,
        scaffolded: bool | None = None,
        drafted: bool | None = None,
    ) -> SystemRecord:
        """Persist the inventory + index entry. Re-discovering an existing system is
        NON-DESTRUCTIVE: prior pipeline flags and the run that holds the already-scaffolded
        files + drafted tests are PRESERVED (the diff reports what actually changed), so an
        unchanged re-discover never makes you scaffold/write from scratch again. Pass an
        explicit scaffolded/drafted to override."""
        slug = slugify(inv.system_name)
        prev = self._read_meta(slug)
        record = SystemRecord(
            slug=slug,
            name=inv.system_name,
            source=inv.source,
            origin=origin,
            base_url=inv.base_url,
            n_elements=len(inv.elements),
            n_journeys=len(inv.suggested_journeys),
            scaffolded=bool(prev["scaffolded"])
            if scaffolded is None and prev
            else bool(scaffolded),
            drafted=bool(prev["drafted"]) if drafted is None and prev else bool(drafted),
            updated_at=datetime.now(UTC).isoformat(),
            latest_run=prev.get("latest_run") if prev else None,
        )
        self._meta(slug).parent.mkdir(parents=True, exist_ok=True)
        payload = {"meta": asdict(record), "inventory": inv.model_dump(mode="json")}
        self._meta(slug).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return record

    def _read_meta(self, slug: str) -> dict | None:
        """The stored `meta` dict for a system, or None if absent/unreadable."""
        f = self._meta(slug)
        if not f.is_file():
            return None
        try:
            return json.loads(f.read_text(encoding="utf-8")).get("meta")
        except (json.JSONDecodeError, OSError):
            return None

    def load_inventory(self, slug: str) -> CoverageInventory:
        data = json.loads(self._meta(slug).read_text(encoding="utf-8"))
        return CoverageInventory.model_validate(data["inventory"])

    def try_load_inventory(self, slug: str) -> CoverageInventory | None:
        """The currently-saved inventory for `slug`, or None if there isn't one yet. Read
        BEFORE a re-discover overwrites it, to use as the baseline for an incremental diff."""
        meta = self._meta(slug)
        if not meta.is_file():
            return None
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
            return CoverageInventory.model_validate(data["inventory"])
        except Exception:
            return None

    def set_flags(
        self,
        slug: str,
        *,
        scaffolded: bool | None = None,
        drafted: bool | None = None,
        latest_run: str | None = None,
    ) -> SystemRecord:
        f = self._meta(slug)
        data = json.loads(f.read_text(encoding="utf-8"))
        meta = data["meta"]
        if scaffolded is not None:
            meta["scaffolded"] = scaffolded
        if drafted is not None:
            meta["drafted"] = drafted
        if latest_run is not None:
            meta["latest_run"] = latest_run
        meta["updated_at"] = datetime.now(UTC).isoformat()
        f.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return SystemRecord(**meta)

    def delete(self, slug: str) -> None:
        d = self.app_dir(slug)
        if d.is_dir():
            shutil.rmtree(d)
