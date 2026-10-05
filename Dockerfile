# Ubuntu is required by playwright.
# Pin to 24.04 LTS: ubuntu:latest floats to 26.04, which Playwright 1.58
# cannot install firefox deps for (no libgtk-3 -> camoufox fails to launch).
FROM ubuntu:24.04@sha256:561618e2c15bf2397621dd04f96926663a3b5616c189cf7e38db7e82f5c538ea AS base

ARG GITHUB_BUILD=false
ARG PYTHON_VERSION=3.14.2

ENV GITHUB_BUILD=${GITHUB_BUILD}\
    PYTHONUNBUFFERED=1 \
    # prevents python creating .pyc files
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    PORT=8191 \
    XDG_CACHE_HOME=/cache \
    HOME=/home/byparr

RUN apt-get update &&\
    apt-get install -y --no-install-recommends curl ca-certificates git tini unzip &&\
    apt-get clean &&\
    rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.9.26@sha256:9a23023be68b2ed09750ae636228e903a54a05ea56ed03a934d00fe9fbeded4b /uv /uvx /bin/

FROM base AS browser
ARG CAMOUFOX_URL=https://github.com/daijro/camoufox/releases/download/v135.0.1-beta.24/camoufox-135.0.1-beta.24-lin.x86_64.zip
ARG CAMOUFOX_ARCHIVE_SHA256=61e1ec455e021720af38a5cc5ff7566121363cb5b82b72f24e381ba2676a4888
ARG CAMOUFOX_BINARY_SHA256=d3999a025212c4fe8ecce8b799912fdf8bd12ca6a5062c87709056041a20c767
RUN mkdir -p /cache/camoufox &&\
    curl -fL --retry 3 -o /tmp/camoufox.zip "$CAMOUFOX_URL" &&\
    echo "$CAMOUFOX_ARCHIVE_SHA256  /tmp/camoufox.zip" | sha256sum -c - &&\
    (unzip -q /tmp/camoufox.zip -d /cache/camoufox || [ "$?" -eq 1 ]) &&\
    echo "$CAMOUFOX_BINARY_SHA256  /cache/camoufox/camoufox-bin" | sha256sum -c - &&\
    printf '{"version":"135.0.1","release":"beta.24"}\n' > /cache/camoufox/version.json &&\
    chmod -R 755 /cache/camoufox &&\
    rm /tmp/camoufox.zip

FROM browser AS app
WORKDIR /app

ARG GEOIP_DATABASE_URL=https://github.com/P3TERX/GeoLite.mmdb/releases/download/2026.10.04/GeoLite2-City.mmdb
ARG GEOIP_DATABASE_SHA256=ffedb2751cae16fdd886814a6c8480633c915ec136c1582ca5c9fc5861f781aa
COPY pyproject.toml uv.lock ./
RUN mkdir -p /cache &&\
    uv python install "$PYTHON_VERSION" &&\
    uv sync --locked --python "$PYTHON_VERSION" &&\
    curl -fL --retry 3 -o /tmp/GeoLite2-City.mmdb "$GEOIP_DATABASE_URL" &&\
    echo "$GEOIP_DATABASE_SHA256  /tmp/GeoLite2-City.mmdb" | sha256sum -c - &&\
    install -m 0444 /tmp/GeoLite2-City.mmdb /app/.venv/lib/python3.14/site-packages/camoufox/GeoLite2-City.mmdb &&\
    rm /tmp/GeoLite2-City.mmdb &&\
    apt-get update &&\
    uv run playwright install-deps firefox &&\
    uv cache clean &&\
    apt-get clean &&\
    rm -rf /var/lib/apt/lists/*

COPY . .

RUN mkdir -p /home/byparr &&\
    chmod -R o+rX /app &&\
    chmod -R a+rwX /cache /home/byparr &&\
    find /app/.venv -path "*/camoufox_add_init_script/addon" -type d -exec chmod -R o+rwX {} + &&\
    uv run python scripts/verify_runtime.py

FROM app AS devcontainer
ENTRYPOINT [ "sleep", "infinity" ]

FROM app AS test
RUN \
    uv sync --group test &&\
    uv run pytest -rs --retries 5 &&\
    find /app/.venv -path "*/camoufox_add_init_script/addon" -type d -exec chmod -R o+rwX {} +
USER 1000
RUN --network=none /app/.venv/bin/python -m scripts.verify_runtime_launch

FROM app
ARG VERSION
ENV VERSION=${VERSION}
USER 1000
EXPOSE $PORT
HEALTHCHECK --interval=15m --timeout=30s --start-period=5s --retries=3 CMD curl "http://127.0.0.1:${PORT}/health"
ENTRYPOINT ["tini", "--", "/app/.venv/bin/python", "main.py"]
