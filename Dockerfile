FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY jobbot ./jobbot
RUN pip install --no-cache-dir . \
 && playwright install --with-deps chromium

# Persistent state (database, browser profile with site logins, Gmail token,
# screenshots) lives here. Mount a volume at /data.
ENV JOBBOT_DATA_DIR=/data \
    JOBBOT_PROFILE=/data/profile.yaml \
    GMAIL_CLIENT_SECRETS=/data/gmail_client_secret.json \
    PYTHONUNBUFFERED=1

CMD ["jobbot", "run", "--loop", "--interval", "120"]
