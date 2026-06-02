"""Discover stage: turn a system surface into a validated CoverageInventory."""

from .crawl import crawl_site, discover_crawl
from .openapi import discover_openapi, load_spec, summarize_spec

__all__ = [
    "discover_openapi",
    "load_spec",
    "summarize_spec",
    "discover_crawl",
    "crawl_site",
]
