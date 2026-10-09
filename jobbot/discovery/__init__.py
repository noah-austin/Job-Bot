"""Job discovery from public ATS job-board feeds (no login, no scraping)."""

from jobbot.discovery.base import JobPosting, html_to_text
from jobbot.discovery.boards import AshbySource, GreenhouseSource, LeverSource, discover_all

__all__ = ["JobPosting", "html_to_text", "GreenhouseSource", "LeverSource", "AshbySource", "discover_all"]
