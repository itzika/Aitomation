"""Discover stage: turn a system surface into a validated CoverageInventory."""

from .asyncapi import discover_asyncapi, summarize_asyncapi
from .crawl import crawl_site, discover_crawl
from .database import discover_db, parse_ddl, reflect_database
from .openapi import discover_openapi, load_spec, summarize_spec
from .registry import discover_registry, fetch_registry

__all__ = [
    "discover_openapi",
    "load_spec",
    "summarize_spec",
    "discover_crawl",
    "crawl_site",
    "discover_asyncapi",
    "summarize_asyncapi",
    "discover_registry",
    "fetch_registry",
    "discover_db",
    "parse_ddl",
    "reflect_database",
]
