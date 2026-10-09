from cryptography.fernet import Fernet

from jobbot.db import Credential
from jobbot.gmail.otp import extract_code, extract_verification_link
from jobbot.scoring import hard_filter
from jobbot.vault import Vault, generate_password, site_key


def test_vault_roundtrip_and_encryption(session):
    vault = Vault(session, Fernet.generate_key().decode())
    user, pw, created = vault.get_or_create("https://acme.wd5.myworkdayjobs.com/en-US/careers/job/1", "jane@example.com")
    assert created and len(pw) == 20
    assert vault.get("https://acme.wd5.myworkdayjobs.com/other") == (user, pw)
    assert vault.get_or_create("https://acme.wd5.myworkdayjobs.com/x", "jane@example.com") == (user, pw, False)
    stored = session.query(Credential).one()
    assert pw not in stored.password_enc


def test_site_key_is_per_tenant():
    assert site_key("https://www.Acme.wd1.myworkdayjobs.com/a") == "acme.wd1.myworkdayjobs.com"
    assert site_key("https://beta.wd1.myworkdayjobs.com/a") != site_key("https://acme.wd1.myworkdayjobs.com/a")


def test_password_has_all_classes():
    pw = generate_password()
    assert any(c.isupper() for c in pw) and any(c.isdigit() for c in pw)


def test_extract_code_and_link():
    assert extract_code("Your verification code is: 482913. It expires soon.") == "482913"
    assert extract_code("Use passcode A7K9Q2 to sign in") == "A7K9Q2"
    assert extract_code("Hello 2026 world") is None
    body = "Click https://acme.myworkdayjobs.com/verify?token=abc. Or ignore https://acme.com/privacy"
    assert extract_verification_link(body) == "https://acme.myworkdayjobs.com/verify?token=abc"


def test_hard_filter(profile):
    assert hard_filter(profile, "Senior Backend Engineer", "Remote - US", "python") is None
    assert hard_filter(profile, "Backend Engineer", "Austin, TX", "") is None
    assert "location" in hard_filter(profile, "Backend Engineer", "London", "")
    assert "excluded" in hard_filter(profile, "Backend Engineer", "Remote", "TS clearance needed")
    assert "title" in hard_filter(profile, "Account Executive", "Remote", "")
