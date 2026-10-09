"""Encrypted storage for application-site logins.

Passwords are encrypted at rest with a Fernet key held only in the
JOBBOT_VAULT_KEY environment variable, and are never sent to the LLM: the
browser agent asks for "the password field" and this module types it in.
"""

import secrets
import string
from urllib.parse import urlparse

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobbot.db import Credential


def generate_key() -> str:
    return Fernet.generate_key().decode()


def generate_password(length: int = 20) -> str:
    """Strong password that satisfies typical ATS rules (upper, lower, digit, symbol)."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.islower() for c in pw)
            and any(c.isupper() for c in pw)
            and any(c.isdigit() for c in pw)
            and any(c in "!@#$%^&*-_" for c in pw)
        ):
            return pw


def site_key(url: str) -> str:
    """Workday and friends issue one account per tenant host, so key on the full hostname."""
    host = urlparse(url).hostname or url
    return host.lower().removeprefix("www.")


class Vault:
    def __init__(self, session: Session, key: str):
        if not key:
            raise RuntimeError("JOBBOT_VAULT_KEY is not set. Run `jobbot vault keygen` and add it to your environment.")
        self.session = session
        self.fernet = Fernet(key.encode())

    def get(self, url: str) -> tuple[str, str] | None:
        cred = self.session.scalar(select(Credential).where(Credential.domain == site_key(url)))
        if cred is None:
            return None
        return cred.username, self.fernet.decrypt(cred.password_enc.encode()).decode()

    def put(self, url: str, username: str, password: str) -> None:
        domain = site_key(url)
        enc = self.fernet.encrypt(password.encode()).decode()
        cred = self.session.scalar(select(Credential).where(Credential.domain == domain))
        if cred is None:
            self.session.add(Credential(domain=domain, username=username, password_enc=enc))
        else:
            cred.username, cred.password_enc = username, enc
        self.session.commit()

    def get_or_create(self, url: str, username: str) -> tuple[str, str, bool]:
        """Returns (username, password, created). Creates and stores a new password if none exists."""
        existing = self.get(url)
        if existing:
            return existing[0], existing[1], False
        password = generate_password()
        self.put(url, username, password)
        return username, password, True
