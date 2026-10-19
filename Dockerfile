# bizops: dashboard + scheduler + public business sites + the agents' browser in one container.
# Debian 12 (bookworm): the release Playwright's `install --with-deps` supports.
FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 BIZOPS_HOME=/data
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && npm install -g @anthropic-ai/claude-code axe-core@4.10.2 \
 && apt-get clean && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
RUN pip install --no-cache-dir -e '.[browser]' \
 && python -m playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK CMD curl -fs http://127.0.0.1:8080/healthz || exit 1
CMD ["bizops", "serve", "--host", "0.0.0.0", "--port", "8080"]
