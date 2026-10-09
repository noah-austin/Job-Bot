"""Runtime settings, read from environment variables (see .env.example)."""

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None or val == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("JOBBOT_DATA_DIR", "./data")))
    database_url: str = os.environ.get("DATABASE_URL", "")
    model: str = os.environ.get("JOBBOT_MODEL", "claude-opus-5-5")
    vault_key: str = os.environ.get("JOBBOT_VAULT_KEY", "")
    gmail_client_secrets: str = os.environ.get("GMAIL_CLIENT_SECRETS", "./data/gmail_client_secret.json")
    auto_submit: bool = _bool("JOBBOT_AUTO_SUBMIT", False)
    min_score: int = int(os.environ.get("JOBBOT_MIN_SCORE", "70"))
    max_applications_per_day: int = int(os.environ.get("JOBBOT_MAX_APPLICATIONS_PER_DAY", "10"))
    headless: bool = _bool("JOBBOT_HEADLESS", True)
    max_agent_steps: int = int(os.environ.get("JOBBOT_MAX_AGENT_STEPS", "60"))

    def __post_init__(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.database_url:
            self.database_url = f"sqlite:///{self.data_dir / 'jobbot.db'}"

    @property
    def profile_path(self) -> Path:
        return Path(os.environ.get("JOBBOT_PROFILE", "profile.yaml"))

    @property
    def browser_state_dir(self) -> Path:
        return self.data_dir / "browser"

    @property
    def artifacts_dir(self) -> Path:
        path = self.data_dir / "artifacts"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def gmail_token_path(self) -> Path:
        return self.data_dir / "gmail_token.json"


def get_settings() -> Settings:
    return Settings()
