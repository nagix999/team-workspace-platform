FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2 AS sqlite-builder

ARG SQLITE_AUTOCONF=3530400
ARG SQLITE_SHA3=454e45f61c6bd75b7420e7190732dea03ce6639c63ada47bbc592f67fc340338

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN SQLITE_AUTOCONF="${SQLITE_AUTOCONF}" \
    SQLITE_SHA3="${SQLITE_SHA3}" \
    python - <<'PY'
import hashlib
import os
import urllib.request

version = os.environ["SQLITE_AUTOCONF"]
expected = os.environ["SQLITE_SHA3"]
url = f"https://www.sqlite.org/2026/sqlite-autoconf-{version}.tar.gz"

with urllib.request.urlopen(url) as response:
    data = response.read()

actual = hashlib.sha3_256(data).hexdigest()
if actual != expected:
    raise SystemExit(f"SQLite archive checksum mismatch: {actual} != {expected}")

with open("/tmp/sqlite.tar.gz", "wb") as archive:
    archive.write(data)
PY

RUN mkdir -p /tmp/sqlite-src \
    && tar -xzf /tmp/sqlite.tar.gz -C /tmp/sqlite-src --strip-components=1 \
    && cd /tmp/sqlite-src \
    && ./configure --prefix=/opt/sqlite --disable-static --enable-shared \
    && make -j"$(nproc)" \
    && make install

FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2 AS runtime

COPY --from=sqlite-builder /opt/sqlite/lib/libsqlite3.so* /usr/local/lib/

ENV LD_LIBRARY_PATH=/usr/local/lib \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLATFORM_DATABASE_URL=sqlite:////var/lib/platform/platform.db

WORKDIR /opt/platform/backend

RUN python -c 'import sqlite3; print("SQLite runtime:", sqlite3.sqlite_version); assert sqlite3.sqlite_version_info >= (3, 51, 3)'

RUN groupadd --system platform && useradd --system --gid platform --home /nonexistent platform \
    && mkdir -p /var/lib/platform && chown platform:platform /var/lib/platform

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --upgrade pip==26.1.2 \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

USER platform
EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).read()"]

CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

FROM runtime AS test

FROM runtime AS final
