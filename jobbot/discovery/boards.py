"""Greenhouse, Lever and Ashby all publish every company's openings as public JSON."""

import logging
from datetime import datetime, timezone

import httpx

from jobbot.discovery.base import JobPosting, html_to_text

log = logging.getLogger(__name__)


def _iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class GreenhouseSource:
    name = "greenhouse"

    def __init__(self, client: httpx.Client):
        self.client = client

    def fetch(self, slug: str) -> list[JobPosting]:
        resp = self.client.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", params={"content": "true"})
        resp.raise_for_status()
        out = []
        for j in resp.json().get("jobs", []):
            out.append(
                JobPosting(
                    source=self.name,
                    external_id=f"{slug}:{j['id']}",
                    company=j.get("company_name") or slug,
                    title=j.get("title", ""),
                    location=(j.get("location") or {}).get("name", ""),
                    url=j.get("absolute_url", ""),
                    apply_url=j.get("absolute_url", ""),
                    description=html_to_text(j.get("content", "")),
                    posted_at=_iso(j.get("first_published") or j.get("updated_at")),
                )
            )
        return out


class LeverSource:
    name = "lever"

    def __init__(self, client: httpx.Client):
        self.client = client

    def fetch(self, slug: str) -> list[JobPosting]:
        resp = self.client.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
        resp.raise_for_status()
        out = []
        for j in resp.json():
            cats = j.get("categories") or {}
            created = j.get("createdAt")
            out.append(
                JobPosting(
                    source=self.name,
                    external_id=f"{slug}:{j['id']}",
                    company=slug,
                    title=j.get("text", ""),
                    location=cats.get("location", "") or ", ".join(cats.get("allLocations") or []),
                    url=j.get("hostedUrl", ""),
                    apply_url=j.get("applyUrl") or j.get("hostedUrl", ""),
                    description=j.get("descriptionPlain") or html_to_text(j.get("description", "")),
                    posted_at=datetime.fromtimestamp(created / 1000, tz=timezone.utc) if created else None,
                )
            )
        return out


class AshbySource:
    name = "ashby"

    def __init__(self, client: httpx.Client):
        self.client = client

    def fetch(self, slug: str) -> list[JobPosting]:
        resp = self.client.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
        resp.raise_for_status()
        out = []
        for j in resp.json().get("jobs", []):
            if j.get("isListed") is False:
                continue
            location = j.get("location", "")
            if j.get("isRemote") and "remote" not in location.lower():
                location = f"{location} (Remote)".strip()
            out.append(
                JobPosting(
                    source=self.name,
                    external_id=f"{slug}:{j['id']}",
                    company=slug,
                    title=j.get("title", ""),
                    location=location,
                    url=j.get("jobUrl", ""),
                    apply_url=j.get("applyUrl") or j.get("jobUrl", ""),
                    description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml", "")),
                    posted_at=_iso(j.get("publishedAt")),
                )
            )
        return out


def discover_all(companies: dict[str, list[str]], client: httpx.Client | None = None) -> list[JobPosting]:
    """Fetch every configured company board. One bad slug doesn't stop the rest."""
    own_client = client is None
    client = client or httpx.Client(timeout=30, headers={"User-Agent": "jobbot/0.1"}, follow_redirects=True)
    sources = {s.name: s for s in (GreenhouseSource(client), LeverSource(client), AshbySource(client))}
    postings: list[JobPosting] = []
    try:
        for source_name, slugs in companies.items():
            source = sources.get(source_name)
            if source is None:
                log.warning("unknown source %s", source_name)
                continue
            for slug in slugs:
                try:
                    postings.extend(source.fetch(slug))
                except httpx.HTTPError as exc:
                    log.warning("failed to fetch %s/%s: %s", source_name, slug, exc)
    finally:
        if own_client:
            client.close()
    return postings
