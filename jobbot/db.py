"""Database models. SQLite locally, Postgres in production (same code)."""

from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobStatus(StrEnum):
    DISCOVERED = "discovered"      # found, not yet scored
    FILTERED_OUT = "filtered_out"  # failed a hard rule or scored too low
    QUEUED = "queued"              # good fit, waiting to be applied to
    APPLYING = "applying"          # browser agent is working on it
    NEEDS_REVIEW = "needs_review"  # agent stopped and needs a human
    APPLIED = "applied"
    FAILED = "failed"
    RESPONDED = "responded"        # got a reply that isn't a clear yes/no
    INTERVIEWING = "interviewing"
    REJECTED = "rejected"
    OFFER = "offer"
    SKIPPED = "skipped"            # you chose not to apply


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("source", "external_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32))          # greenhouse / lever / ashby / ...
    external_id: Mapped[str] = mapped_column(String(128))
    company: Mapped[str] = mapped_column(String(256))
    title: Mapped[str] = mapped_column(String(512))
    location: Mapped[str] = mapped_column(String(512), default="")
    url: Mapped[str] = mapped_column(Text)                    # posting page
    apply_url: Mapped[str] = mapped_column(Text, default="")  # where the form lives
    description: Mapped[str] = mapped_column(Text, default="")
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status: Mapped[str] = mapped_column(String(32), default=JobStatus.DISCOVERED, index=True)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    score_reason: Mapped[str] = mapped_column(Text, default="")
    approved_to_submit: Mapped[bool] = mapped_column(default=False)  # you OK'd the final submit
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    events: Mapped[list["Event"]] = relationship(back_populates="job", order_by="Event.created_at")


class Event(Base):
    """Append-only timeline per job: what the bot did and what came back."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(64))
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    job: Mapped[Job | None] = relationship(back_populates="events")


class ReviewItem(Base):
    """Something the bot couldn't (or shouldn't) decide on its own."""

    __tablename__ = "review_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), nullable=True, index=True)
    reason: Mapped[str] = mapped_column(Text)
    screenshot_path: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution: Mapped[str] = mapped_column(Text, default="")


class Credential(Base):
    """Login for one application site. Password is encrypted with the vault key."""

    __tablename__ = "credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    domain: Mapped[str] = mapped_column(String(256), unique=True)
    username: Mapped[str] = mapped_column(String(256))
    password_enc: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EmailMessage(Base):
    """An inbound email the inbox monitor has processed."""

    __tablename__ = "emails"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    gmail_id: Mapped[str] = mapped_column(String(64), unique=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), nullable=True, index=True)
    sender: Mapped[str] = mapped_column(Text, default="")
    subject: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(32), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


def make_sessionmaker(database_url: str) -> sessionmaker[Session]:
    engine = create_engine(database_url, future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def log_event(session: Session, job: Job | None, kind: str, detail: str = "") -> None:
    session.add(Event(job_id=job.id if job else None, kind=kind, detail=detail))


def jobs_with_status(session: Session, status: str, limit: int | None = None) -> list[Job]:
    stmt = select(Job).where(Job.status == status).order_by(Job.score.desc().nulls_last(), Job.discovered_at)
    if limit:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt))
