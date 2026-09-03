FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2 AS sqlite-builder

# Debian bookworm's libsqlite3 is too old for the WAL safety contract enforced
# by the production services. Build the official SQLite amalgamation once and
# verify it with both the project's SHA-256 pin and SQLite's published SHA3-256.
ENV SQLITE_VERSION=3.53.4 \
    SQLITE_AUTOCONF_VERSION=3530400 \
    SQLITE_SOURCE_SHA256=0e9483900e92cd5de8fd48d16bf9200145a61f7fd5be542a5ac81d8a9516eb9c \
    SQLITE_SOURCE_SHA3_256=454e45f61c6bd75b7420e7190732dea03ce6639c63ada47bbc592f67fc340338

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        curl \
        gcc \
        libc6-dev \
        make \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp/sqlite-source

RUN curl --fail --location --proto '=https' --retry 5 --show-error --silent \
        "https://www.sqlite.org/2026/sqlite-autoconf-${SQLITE_AUTOCONF_VERSION}.tar.gz" \
        --output /tmp/sqlite.tar.gz \
    && printf '%s  %s\n' "${SQLITE_SOURCE_SHA256}" /tmp/sqlite.tar.gz \
        | sha256sum --check --strict \
    && python -c "import hashlib, pathlib; payload = pathlib.Path('/tmp/sqlite.tar.gz').read_bytes(); actual = hashlib.sha3_256(payload).hexdigest(); expected = '${SQLITE_SOURCE_SHA3_256}'; assert actual == expected, f'SQLite source SHA3-256 mismatch: {actual}'" \
    && tar --extract --gzip --file /tmp/sqlite.tar.gz \
        --directory /tmp/sqlite-source --strip-components=1 \
    && ./configure \
        --prefix=/opt/platform/sqlite \
        --disable-static \
        --enable-shared \
        CFLAGS='-O2 -fPIC -fstack-protector-strong -D_FORTIFY_SOURCE=2' \
        LDFLAGS='-Wl,-z,relro,-z,now' \
    && make -j2 \
    && make install

FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2 AS runtime

COPY --from=sqlite-builder /opt/platform/sqlite/lib/ /opt/platform/sqlite/lib/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLATFORM_DATABASE_URL=sqlite:////var/lib/platform/platform.db \
    LD_LIBRARY_PATH=/opt/platform/sqlite/lib

# Fail the image build if CPython's stdlib extension does not resolve the
# verified runtime library. This also protects against a future base-image
# change that statically links _sqlite3 or changes the dynamic loader path.
RUN python -c "import sqlite3; actual = sqlite3.sqlite_version; expected = '3.53.4'; assert actual == expected, f'expected SQLite {expected}, got {actual}'"

WORKDIR /opt/platform/backend

RUN groupadd --system platform && useradd --system --gid platform --home /nonexistent platform \
    && mkdir -p /var/lib/platform && chown platform:platform /var/lib/platform

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --upgrade pip==26.1.2 \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

# The application and the image must agree on the accepted SQLite branches.
RUN python -c "import sqlite3, sys; from app.db import _sqlite_version_is_safe; sys.exit(f'unsafe SQLite runtime: {sqlite3.sqlite_version}') if not _sqlite_version_is_safe(sqlite3.sqlite_version_info) else None"

USER platform
EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).read()"]

CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

FROM runtime AS test

FROM runtime AS final
