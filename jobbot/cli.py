"""Command line entry point: `jobbot --help`."""

import logging
import time
from pathlib import Path

import typer
from sqlalchemy import select

from jobbot import pipeline
from jobbot.config import get_settings
from jobbot.db import Job, JobStatus, ReviewItem, jobs_with_status, log_event, make_sessionmaker, utcnow
from jobbot.llm import LLM
from jobbot.profile import load_profile

app = typer.Typer(no_args_is_help=True, help="Autonomous job search and application bot.")
vault_app = typer.Typer(help="Manage stored site logins.")
gmail_app = typer.Typer(help="Connect your personal Gmail.")
app.add_typer(vault_app, name="vault")
app.add_typer(gmail_app, name="gmail")


def _ctx():
    settings = get_settings()
    return settings, make_sessionmaker(settings.database_url)()


def _gmail(settings, required: bool = False):
    from jobbot.gmail.client import GmailClient

    try:
        return GmailClient(settings.gmail_token_path)
    except Exception as exc:
        if required:
            raise
        typer.echo(f"(Gmail not connected: {exc})")
        return None


@app.callback()
def main(verbose: bool = typer.Option(False, "--verbose", "-v")):
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@vault_app.command("keygen")
def vault_keygen():
    """Print a new encryption key for JOBBOT_VAULT_KEY."""
    from jobbot.vault import generate_key

    typer.echo(generate_key())


@vault_app.command("set")
def vault_set(url: str, username: str, password: str = typer.Option(..., prompt=True, hide_input=True)):
    """Store an existing login for a site (e.g. a Workday tenant you already have an account on)."""
    from jobbot.vault import Vault

    settings, session = _ctx()
    Vault(session, settings.vault_key).put(url, username, password)
    typer.echo("saved")


@gmail_app.command("auth")
def gmail_auth():
    """One-time OAuth consent for your personal Gmail. Run on a machine with a browser."""
    from jobbot.gmail.client import authorize

    settings = get_settings()
    authorize(Path(settings.gmail_client_secrets), settings.gmail_token_path)
    typer.echo(f"Token saved to {settings.gmail_token_path}")


@app.command()
def discover():
    """Pull new postings from the configured company boards."""
    settings, session = _ctx()
    typer.echo(f"{pipeline.discover(session, load_profile(settings.profile_path))} new jobs")


@app.command()
def score(limit: int = 200):
    """Filter and score discovered jobs; good ones are queued to apply."""
    settings, session = _ctx()
    queued, rejected = pipeline.score(session, LLM(settings.model), load_profile(settings.profile_path), settings.min_score, limit)
    typer.echo(f"queued {queued}, filtered out {rejected}")


@app.command()
def apply(job_id: int = typer.Option(None, help="Apply to one specific job")):
    """Run the browser agent on queued jobs (respects the daily limit)."""
    settings, session = _ctx()
    profile = load_profile(settings.profile_path)
    gmail = _gmail(settings)
    if job_id:
        job = session.get(Job, job_id)
        if not job:
            raise typer.BadParameter(f"no job {job_id}")
        typer.echo(pipeline.apply_one(session, settings, profile, job, gmail))
    else:
        typer.echo(pipeline.apply_queued(session, settings, profile, gmail))


@app.command()
def inbox(days: int = 3):
    """Read recruiting email and update application statuses."""
    from jobbot.gmail.inbox import process_inbox

    settings, session = _ctx()
    for alert in process_inbox(session, _gmail(settings, required=True), LLM(settings.model), days):
        typer.echo(alert)


@app.command()
def run(
    loop: bool = typer.Option(False, help="Keep running every --interval minutes"),
    interval: int = 120,
    email_digest: bool = typer.Option(True, help="Email the digest to yourself"),
):
    """Full cycle: discover, score, apply, read inbox, send digest."""
    from jobbot.gmail.inbox import process_inbox

    while True:
        settings, session = _ctx()
        profile = load_profile(settings.profile_path)
        llm = LLM(settings.model)
        gmail = _gmail(settings)
        typer.echo(f"discovered {pipeline.discover(session, profile)}")
        typer.echo("scored: queued %d, filtered %d" % pipeline.score(session, llm, profile, settings.min_score))
        typer.echo(f"applied: {pipeline.apply_queued(session, settings, profile, gmail)}")
        alerts = process_inbox(session, gmail, llm) if gmail else []
        text = pipeline.digest(session, alerts)
        typer.echo(text)
        if gmail and email_digest:
            gmail.send_to_self("Job bot digest" + (" - action needed" if alerts else ""), text)
        session.close()
        if not loop:
            break
        time.sleep(interval * 60)


@app.command()
def status(state: str = typer.Option(None, help="Filter by status, e.g. queued, applied")):
    """List tracked jobs."""
    settings, session = _ctx()
    stmt = select(Job).order_by(Job.updated_at.desc()).limit(100)
    if state:
        stmt = stmt.where(Job.status == state)
    for j in session.scalars(stmt):
        typer.echo(f"#{j.id:<5} {j.status:<13} {str(j.score or '-'):>3}  {j.company[:20]:<20} {j.title[:60]}")


@app.command()
def review():
    """Show everything waiting on you."""
    settings, session = _ctx()
    for r in session.scalars(select(ReviewItem).where(ReviewItem.resolved_at.is_(None))):
        job = session.get(Job, r.job_id) if r.job_id else None
        typer.echo(f"[{r.id}] job #{r.job_id} {job.company + ': ' + job.title if job else ''}\n    {r.reason}\n    {r.screenshot_path}")


def _resolve_reviews(session, job: Job, resolution: str) -> None:
    for r in session.scalars(select(ReviewItem).where(ReviewItem.job_id == job.id, ReviewItem.resolved_at.is_(None))):
        r.resolved_at, r.resolution = utcnow(), resolution


@app.command()
def approve(job_id: int):
    """Approve a job for final submission; the next apply run submits it."""
    settings, session = _ctx()
    job = session.get(Job, job_id)
    job.approved_to_submit, job.status = True, JobStatus.QUEUED
    _resolve_reviews(session, job, "approved")
    log_event(session, job, "approved")
    session.commit()
    typer.echo(f"#{job_id} approved; it will be submitted on the next apply run")


@app.command()
def retry(job_id: int):
    """Put a job back in the queue (e.g. after you added a missing answer to profile.yaml)."""
    settings, session = _ctx()
    job = session.get(Job, job_id)
    job.status = JobStatus.QUEUED
    _resolve_reviews(session, job, "retried")
    session.commit()
    typer.echo(f"#{job_id} re-queued")


@app.command()
def skip(job_id: int):
    """Don't apply to this job."""
    settings, session = _ctx()
    job = session.get(Job, job_id)
    job.status = JobStatus.SKIPPED
    _resolve_reviews(session, job, "skipped")
    session.commit()
    typer.echo(f"#{job_id} skipped")


@app.command()
def mark(job_id: int, new_status: JobStatus):
    """Manually set a job's status (e.g. you applied by hand)."""
    settings, session = _ctx()
    job = session.get(Job, job_id)
    job.status = new_status
    if new_status == JobStatus.APPLIED and not job.applied_at:
        job.applied_at = utcnow()
    log_event(session, job, "manual_status", new_status)
    session.commit()


if __name__ == "__main__":
    app()
