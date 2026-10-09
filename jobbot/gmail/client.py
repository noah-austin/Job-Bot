"""Direct Gmail API access to your *personal* inbox, independent of any Claude connector.

One-time setup (see README): create an OAuth "Desktop app" client in Google Cloud,
download its JSON, then run `jobbot gmail auth` once on a machine with a browser.
The resulting token (with a refresh token) is stored in the data dir and reused
headlessly by the server.
"""

import base64
from email.message import EmailMessage as MimeMessage
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# readonly: read replies and verification codes. modify: label/archive processed mail.
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


@dataclass
class Mail:
    id: str
    thread_id: str
    sender: str
    subject: str
    received_at: datetime
    body: str


def authorize(client_secrets: Path, token_path: Path) -> None:
    """Interactive consent. Prints a URL; works over SSH via the console flow fallback."""
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=False, access_type="offline", prompt="consent")
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())


def _load_credentials(token_path: Path) -> Credentials:
    if not token_path.exists():
        raise RuntimeError(f"No Gmail token at {token_path}. Run `jobbot gmail auth` first.")
    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json())
    return creds


def _decode_body(payload: dict) -> str:
    """Prefer text/plain, fall back to the first text/html part."""
    def walk(part: dict, mime: str) -> str | None:
        if part.get("mimeType") == mime and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
        for child in part.get("parts", []) or []:
            found = walk(child, mime)
            if found:
                return found
        return None

    text = walk(payload, "text/plain")
    if text:
        return text
    from jobbot.discovery.base import html_to_text

    return html_to_text(walk(payload, "text/html") or "")


class GmailClient:
    def __init__(self, token_path: Path, service=None):
        self.service = service or build("gmail", "v1", credentials=_load_credentials(token_path), cache_discovery=False)

    def search(self, query: str, max_results: int = 50) -> list[str]:
        resp = self.service.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
        return [m["id"] for m in resp.get("messages", [])]

    def get(self, message_id: str) -> Mail:
        msg = self.service.users().messages().get(userId="me", id=message_id, format="full").execute()
        headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
        return Mail(
            id=msg["id"],
            thread_id=msg["threadId"],
            sender=headers.get("from", ""),
            subject=headers.get("subject", ""),
            received_at=datetime.fromtimestamp(int(msg["internalDate"]) / 1000, tz=timezone.utc),
            body=_decode_body(msg["payload"]),
        )

    def wait_for(self, query: str, since: datetime, timeout_s: int = 180, poll_s: int = 5) -> Mail | None:
        """Poll until a message matching `query` arrives after `since` (for verification emails)."""
        deadline = time.monotonic() + timeout_s
        after = int(since.timestamp()) - 60
        while time.monotonic() < deadline:
            for mid in self.search(f"{query} after:{after}", max_results=5):
                mail = self.get(mid)
                if mail.received_at >= since:
                    return mail
            time.sleep(poll_s)
        return None

    def send_to_self(self, subject: str, body: str) -> None:
        me = self.service.users().getProfile(userId="me").execute()["emailAddress"]
        msg = MimeMessage()
        msg["To"] = msg["From"] = me
        msg["Subject"] = subject
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        self.service.users().messages().send(userId="me", body={"raw": raw}).execute()
