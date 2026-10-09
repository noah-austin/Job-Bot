import html
import re
from dataclasses import dataclass
from datetime import datetime


@dataclass
class JobPosting:
    source: str
    external_id: str
    company: str
    title: str
    location: str
    url: str
    apply_url: str
    description: str
    posted_at: datetime | None = None


_TAG = re.compile(r"<[^>]+>")
_BLOCK = re.compile(r"</?(p|div|br|li|ul|ol|h[1-6])[^>]*>", re.I)
_WS = re.compile(r"[ \t]+")
_NL = re.compile(r"\n{3,}")


def html_to_text(raw: str) -> str:
    """Rough HTML -> text. Greenhouse double-escapes, so unescape first."""
    text = html.unescape(raw or "")
    text = _BLOCK.sub("\n", text)
    text = html.unescape(_TAG.sub("", text))
    text = _WS.sub(" ", text)
    return _NL.sub("\n\n", "\n".join(line.strip() for line in text.splitlines())).strip()
