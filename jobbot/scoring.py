"""Decide which discovered jobs are worth applying to."""

from pydantic import BaseModel

from jobbot.llm import LLM
from jobbot.profile import Profile

SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "description": "0-100 fit score"},
        "dealbreaker": {"type": "boolean", "description": "true if a hard requirement rules the candidate out"},
        "reason": {"type": "string", "description": "two or three sentences explaining the score"},
    },
    "required": ["score", "dealbreaker", "reason"],
    "additionalProperties": False,
}

SCORE_SYSTEM = """You screen job postings for one candidate. Score how well the posting fits the
candidate's experience AND stated preferences, 0-100. 80+ means a strong, realistic match worth
applying to; 50-79 is plausible; below 50 is a poor fit. Set dealbreaker=true when the posting
has a hard requirement the candidate clearly does not meet (required clearance, citizenship,
license, degree, or years of experience far above theirs), or contradicts a stated preference
(location, remote, seniority, salary). Be honest; inflated scores waste applications."""


class ScoreResult(BaseModel):
    score: int
    dealbreaker: bool
    reason: str


def hard_filter(profile: Profile, title: str, location: str, description: str) -> str | None:
    """Cheap rules that run before any LLM call. Returns a reason to reject, or None."""
    s = profile.search
    haystack = f"{title}\n{description}".lower()
    for kw in s.exclude_keywords:
        if kw.lower() in haystack:
            return f"contains excluded keyword '{kw}'"
    if s.titles and not any(t.lower() in title.lower() for t in s.titles):
        # Titles are a soft include list: require at least one word overlap.
        title_words = {w for t in s.titles for w in t.lower().split() if len(w) > 3}
        if not any(w in title.lower() for w in title_words):
            return "title doesn't match any target title"
    if s.locations:
        loc = location.lower()
        remote_match = s.remote_ok and "remote" in loc
        if not remote_match and not any(l.lower() in loc for l in s.locations) and loc:
            return f"location '{location}' not in target locations"
    return None


def score_job(llm: LLM, profile: Profile, company: str, title: str, location: str, description: str) -> ScoreResult:
    prompt = (
        f"<candidate>\n{profile.facts_for_llm()}\n</candidate>\n\n"
        f"<preferences>\n{profile.search.model_dump_json(exclude={'companies'})}\n</preferences>\n\n"
        f"<posting>\nCompany: {company}\nTitle: {title}\nLocation: {location}\n\n{description[:30000]}\n</posting>"
    )
    return ScoreResult.model_validate(llm.json(SCORE_SYSTEM, prompt, SCORE_SCHEMA, effort="low"))
