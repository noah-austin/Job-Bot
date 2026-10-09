import os

import pytest

if not os.environ.get("JOBBOT_CHROMIUM_PATH") and os.path.exists("/opt/pw-browsers/chromium"):
    os.environ["JOBBOT_CHROMIUM_PATH"] = "/opt/pw-browsers/chromium"

from jobbot.db import make_sessionmaker
from jobbot.profile import Profile


@pytest.fixture
def session(tmp_path):
    return make_sessionmaker(f"sqlite:///{tmp_path / 't.db'}")()


@pytest.fixture
def profile(tmp_path):
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 fake")
    return Profile.model_validate(
        {
            "personal": {"first_name": "Jane", "last_name": "Doe", "email": "jane@example.com"},
            "resume_path": str(resume),
            "resume_text": "Backend engineer, 6 years Python.",
            "search": {
                "titles": ["Backend Engineer"],
                "exclude_keywords": ["clearance"],
                "locations": ["Austin"],
                "remote_ok": True,
            },
            "answers": {"work_authorization_us": "Yes"},
        }
    )
