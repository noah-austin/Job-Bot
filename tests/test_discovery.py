import json
from pathlib import Path

import httpx
import respx

from jobbot.discovery import discover_all, html_to_text

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text())


@respx.mock
def test_discover_all_parses_each_board():
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(return_value=httpx.Response(200, json=load("greenhouse.json")))
    respx.get("https://api.lever.co/v0/postings/acme").mock(return_value=httpx.Response(200, json=load("lever.json")))
    respx.get("https://api.ashbyhq.com/posting-api/job-board/acme").mock(return_value=httpx.Response(200, json=load("ashby.json")))
    respx.get("https://api.lever.co/v0/postings/missing").mock(return_value=httpx.Response(404))

    jobs = discover_all({"greenhouse": ["acme"], "lever": ["acme", "missing"], "ashby": ["acme"]})

    by_source = {j.source: j for j in jobs}
    assert len(jobs) == 3  # unlisted Ashby job dropped, 404 board skipped
    gh = by_source["greenhouse"]
    assert gh.company == "Acme" and gh.external_id == "acme:123"
    assert "Build Python services." in gh.description and "<" not in gh.description
    assert by_source["lever"].apply_url.endswith("/apply")
    assert by_source["lever"].posted_at.year == 2025
    assert "Remote" in by_source["ashby"].location


def test_html_to_text():
    assert html_to_text("<p>a &amp; b</p><ul><li>one</li><li>two</li></ul>").split() == ["a", "&", "b", "one", "two"]
