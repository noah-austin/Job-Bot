"""Read replies to applications and update each job's status."""

import logging
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobbot.db import EmailMessage, Job, JobStatus, ReviewItem, log_event
from jobbot.gmail.client import GmailClient, Mail
from jobbot.llm import LLM

log = logging.getLogger(__name__)

CATEGORIES = ["confirmation", "rejection", "interview_request", "assessment", "offer", "verification", "other_recruiting", "unrelated"]

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": CATEGORIES},
        "job_id": {"type": ["integer", "null"], "description": "id from the list of applications, or null"},
        "summary": {"type": "string", "description": "one sentence; include any deadline or requested action"},
    },
    "required": ["category", "job_id", "summary"],
    "additionalProperties": False,
}

CLASSIFY_SYSTEM = """You triage a job seeker's inbox. Classify the email and, if it concerns one of
the listed applications, return that application's id. Match on company name, role title, and
sender domain. Return job_id null if unsure; a wrong match is worse than no match."""

STATUS_FOR = {
    "rejection": JobStatus.REJECTED,
    "interview_request": JobStatus.INTERVIEWING,
    "assessment": JobStatus.INTERVIEWING,
    "offer": JobStatus.OFFER,
    "other_recruiting": JobStatus.RESPONDED,
}
NEEDS_YOU = {"interview_request", "assessment", "offer"}

# Recruiting mail tends to come from ATS domains or contain these words.
SEARCH_QUERY = (
    "(application OR interview OR position OR role OR candidate OR recruiting OR "
    "from:greenhouse.io OR from:lever.co OR from:ashbyhq.com OR from:myworkday.com) "
    "-category:promotions"
)


class Classification(BaseModel):
    category: str
    job_id: int | None
    summary: str


def classify(llm: LLM, mail: Mail, applications: list[Job]) -> Classification:
    apps = "\n".join(f"{j.id}: {j.company} - {j.title}" for j in applications) or "(none)"
    prompt = (
        f"<applications>\n{apps}\n</applications>\n\n"
        f"<email>\nFrom: {mail.sender}\nSubject: {mail.subject}\n\n{mail.body[:8000]}\n</email>"
    )
    return Classification.model_validate(llm.json(CLASSIFY_SYSTEM, prompt, CLASSIFY_SCHEMA))


def process_inbox(session: Session, gmail: GmailClient, llm: LLM, lookback_days: int = 3) -> list[str]:
    """Classify new recruiting mail. Returns human-readable alerts for things that need you."""
    since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y/%m/%d")
    applications = list(
        session.scalars(select(Job).where(Job.applied_at.is_not(None)).order_by(Job.applied_at.desc()).limit(300))
    )
    alerts: list[str] = []
    for mid in gmail.search(f"{SEARCH_QUERY} after:{since}", max_results=100):
        if session.scalar(select(EmailMessage).where(EmailMessage.gmail_id == mid)):
            continue
        mail = gmail.get(mid)
        result = classify(llm, mail, applications)
        job = session.get(Job, result.job_id) if result.job_id else None
        session.add(
            EmailMessage(
                gmail_id=mail.id,
                job_id=job.id if job else None,
                sender=mail.sender,
                subject=mail.subject,
                category=result.category,
                summary=result.summary,
                received_at=mail.received_at,
            )
        )
        if job and result.category in STATUS_FOR:
            job.status = STATUS_FOR[result.category]
            log_event(session, job, f"email_{result.category}", result.summary)
        if result.category in NEEDS_YOU:
            label = f"{job.company} - {job.title}" if job else mail.sender
            alerts.append(f"[{result.category}] {label}: {result.summary}")
            session.add(ReviewItem(job_id=job.id if job else None, reason=f"{result.category}: {result.summary}"))
        session.commit()
    return alerts
