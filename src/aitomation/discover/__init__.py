"""Discover stage: turn a system surface into a validated CoverageInventory."""

from .asyncapi import discover_asyncapi, summarize_asyncapi
from .crawl import crawl_site, discover_crawl
from .database import discover_db, parse_ddl, reflect_database
from .openapi import discover_openapi, load_spec, summarize_spec
from .registry import discover_registry, fetch_registry

__all__ = [
    "crawl_site",
    "discover_asyncapi",
    "discover_crawl",
    "discover_db",
    "discover_openapi",
    "discover_registry",
    "fetch_registry",
    "load_spec",
    "parse_ddl",
    "reflect_database",
    "summarize_asyncapi",
    "summarize_spec",
]
