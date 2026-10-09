"""Pull one-time codes and verification links out of emails."""

import re

_CODE_CONTEXT = re.compile(
    r"(?:code|passcode|pin|otp)[^0-9A-Z]{0,40}\b([A-Z0-9]{4,8})\b",
    re.I,
)
_STANDALONE_DIGITS = re.compile(r"(?<![\d-])(\d{6})(?![\d-])")
_LINK = re.compile(r"https?://[^\s\"'<>)]+")
_VERIFY_WORDS = ("verify", "confirm", "activate", "validate", "magic", "login", "sign-in", "signin")


def extract_code(text: str) -> str | None:
    match = _CODE_CONTEXT.search(text)
    if match and any(c.isdigit() for c in match.group(1)):
        return match.group(1)
    match = _STANDALONE_DIGITS.search(text)
    return match.group(1) if match else None


def extract_verification_link(text: str) -> str | None:
    for url in _LINK.findall(text):
        if any(w in url.lower() for w in _VERIFY_WORDS):
            return url.rstrip(".,")
    return None
