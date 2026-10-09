"""The stages of a run: discover -> score -> apply -> read inbox -> digest."""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jobbot.config import Settings
from jobbot.db import Job, JobStatus, ReviewItem, jobs_with_status, log_event, utcnow
from jobbot.discovery import discover_all
from jobbot.llm import LLM, LLMError
from jobbot.profile import Profile
from jobbot.scoring import hard_filter, score_job

log = logging.getLogger(__name__)


def discover(session: Session, profile: Profile) -> int:
    companies = profile.search.companies.model_dump()
    new = 0
    for p in discover_all(companies):
        exists = session.scalar(select(Job.id).where(Job.source == p.source, Job.external_id == p.external_id))
        if exists:
            continue
        job = Job(
            source=p.source,
            external_id=p.external_id,
            company=p.company,
            title=p.title,
            location=p.location,
            url=p.url,
            apply_url=p.apply_url,
            description=p.description,
            posted_at=p.posted_at,
        )
        session.add(job)
        session.flush()
        log_event(session, job, "discovered", p.source)
        new += 1
    session.commit()
    return new


def score(session: Session, llm: LLM, profile: Profile, min_score: int, limit: int = 200) -> tuple[int, int]:
    queued = rejected = 0
    for job in jobs_with_status(session, JobStatus.DISCOVERED, limit):
        reason = hard_filter(profile, job.title, job.location, job.description)
        if reason:
            job.status, job.score_reason = JobStatus.FILTERED_OUT, reason
            rejected += 1
        else:
            try:
                result = score_job(llm, profile, job.company, job.title, job.location, job.description)
            except LLMError as exc:
                log_event(session, job, "score_error", str(exc))
                session.commit()
                continue
            job.score, job.score_reason = result.score, result.reason
            if result.dealbreaker or result.score < min_score:
                job.status = JobStatus.FILTERED_OUT
                rejected += 1
            else:
                job.status = JobStatus.QUEUED
                queued += 1
        log_event(session, job, "scored", f"{job.score} {job.score_reason}")
        session.commit()
    return queued, rejected


def applied_today(session: Session) -> int:
    since = utcnow() - timedelta(days=1)
    return session.scalar(select(func.count()).select_from(Job).where(Job.applied_at >= since)) or 0


OUTCOME_STATUS = {
    "applied": JobStatus.APPLIED,
    "already_applied": JobStatus.APPLIED,
    "ready_to_submit": JobStatus.NEEDS_REVIEW,
    "needs_review": JobStatus.NEEDS_REVIEW,
    "job_closed": JobStatus.SKIPPED,
    "failed": JobStatus.FAILED,
}


def apply_one(session: Session, settings: Settings, profile: Profile, job: Job, gmail=None) -> str:
    # Imported here so discovery/scoring work without Playwright installed.
    from jobbot.apply.agent import ApplicationAgent
    from jobbot.apply.browser import PlaywrightBrowser
    from jobbot.vault import Vault

    def wait_for_email(hint: str, since: datetime) -> str | None:
        mail = gmail.wait_for(f'"{hint}" (verify OR verification OR code OR confirm)', since)
        return f"{mail.subject}\n\n{mail.body}" if mail else None

    job.status = JobStatus.APPLYING
    session.commit()
    browser = PlaywrightBrowser(settings.browser_state_dir, headless=settings.headless)
    try:
        agent = ApplicationAgent(
            browser=browser,
            profile=profile,
            vault=Vault(session, settings.vault_key),
            model=settings.model,
            artifacts_dir=settings.artifacts_dir,
            auto_submit=settings.auto_submit or job.approved_to_submit,
            max_steps=settings.max_agent_steps,
            wait_for_email=wait_for_email if gmail else None,
        )
        summary = f"{job.company} - {job.title} ({job.location})\n{job.url}\n\n{job.description[:6000]}"
        result = agent.run(job.apply_url or job.url, summary)
    except Exception as exc:
        log.exception("apply crashed for job %s", job.id)
        job.status = JobStatus.FAILED
        log_event(session, job, "apply_crashed", f"{type(exc).__name__}: {exc}")
        session.commit()
        return "failed"
    finally:
        browser.close()

    job.status = OUTCOME_STATUS.get(result.outcome, JobStatus.FAILED)
    if job.status == JobStatus.APPLIED:
        job.applied_at = utcnow()
    log_event(session, job, f"apply_{result.outcome}", f"{result.notes}\nsteps={result.steps}\nactions={result.actions}")
    if job.status == JobStatus.NEEDS_REVIEW:
        reason = "Ready to submit; approve with `jobbot approve %d`" % job.id if result.outcome == "ready_to_submit" else result.notes
        session.add(ReviewItem(job_id=job.id, reason=reason, screenshot_path=str(result.screenshot or "")))
    session.commit()
    return result.outcome


def apply_queued(session: Session, settings: Settings, profile: Profile, gmail=None) -> dict[str, int]:
    budget = max(0, settings.max_applications_per_day - applied_today(session))
    # Approved jobs go first, then best scores.
    approved = list(session.scalars(select(Job).where(Job.status == JobStatus.QUEUED, Job.approved_to_submit.is_(True))))
    rest = [j for j in jobs_with_status(session, JobStatus.QUEUED, budget) if not j.approved_to_submit]
    counts: dict[str, int] = {}
    for job in (approved + rest)[:budget]:
        outcome = apply_one(session, settings, profile, job, gmail)
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


def digest(session: Session, alerts: list[str]) -> str:
    since = utcnow() - timedelta(days=1)
    applied = list(session.scalars(select(Job).where(Job.applied_at >= since)))
    open_reviews = list(session.scalars(select(ReviewItem).where(ReviewItem.resolved_at.is_(None))))
    by_status = dict(session.execute(select(Job.status, func.count()).group_by(Job.status)).all())
    lines = [f"Job bot digest - {datetime.now(timezone.utc):%Y-%m-%d}", ""]
    if alerts:
        lines += ["NEEDS YOUR ATTENTION:"] + [f"  * {a}" for a in alerts] + [""]
    lines.append(f"Applied in the last 24h ({len(applied)}):")
    lines += [f"  - {j.company}: {j.title} (score {j.score})" for j in applied] or ["  (none)"]
    lines += ["", f"Open review items ({len(open_reviews)}):"]
    for r in open_reviews:
        job = session.get(Job, r.job_id) if r.job_id else None
        label = f"#{job.id} {job.company}: {job.title}" if job else "-"
        lines.append(f"  [{r.id}] {label} -- {r.reason}")
    lines += ["", "Pipeline: " + ", ".join(f"{k}={v}" for k, v in sorted(by_status.items()))]
    return "\n".join(lines)
