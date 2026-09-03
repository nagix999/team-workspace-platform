from pathlib import Path


DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"


def test_backend_image_pins_and_enforces_safe_sqlite_runtime() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "ARG PYTHON_BASE_IMAGE" not in dockerfile
    assert dockerfile.count("FROM python:3.12.13-slim-bookworm@sha256:") == 2
    assert "SQLITE_VERSION=3.53.4" in dockerfile
    assert "SQLITE_AUTOCONF_VERSION=3530400" in dockerfile
    assert (
        "SQLITE_SOURCE_SHA256="
        "0e9483900e92cd5de8fd48d16bf9200145a61f7fd5be542a5ac81d8a9516eb9c" in dockerfile
    )
    assert (
        "SQLITE_SOURCE_SHA3_256="
        "454e45f61c6bd75b7420e7190732dea03ce6639c63ada47bbc592f67fc340338" in dockerfile
    )
    assert "https://www.sqlite.org/2026/sqlite-autoconf-${" in dockerfile
    assert "sha256sum --check --strict" in dockerfile
    assert "hashlib.sha3_256(payload).hexdigest()" in dockerfile
    assert "LD_LIBRARY_PATH=/opt/platform/sqlite/lib" in dockerfile
    assert "from app.db import _sqlite_version_is_safe" in dockerfile
