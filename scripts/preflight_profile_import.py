#!/usr/bin/env python3
"""Exercise the built backend migration/import path on a disposable database."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sqlite3
import stat
from pathlib import Path
import sys

PREFLIGHT_DATABASE_URL = "sqlite:////tmp/platform-profile-preflight.db"
PREFLIGHT_DATABASE_PATH = Path("/tmp/platform-profile-preflight.db")
LIVE_DATABASE_PATH = Path("/var/lib/platform/platform.db")
LIVE_DATABASE_FALLBACK_PATHS = (
    Path("/source/platform.db"),
    Path("/source/platform.sqlite"),
    Path("/source/platform.sqlite3"),
    LIVE_DATABASE_PATH,
    Path("/var/lib/platform/platform.sqlite"),
    Path("/var/lib/platform/platform.sqlite3"),
)


def _source_snapshot_path(destination: Path) -> Path:
    return destination.with_name(f".{destination.name}.source-snapshot")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_live_database(source: Path | None = None) -> Path:
    # An explicitly supplied source is an exact trust boundary.  Falling back
    # to some other database would make a typo appear to pass preflight while
    # rehearsing the wrong data set.
    candidates = [source] if source is not None else list(LIVE_DATABASE_FALLBACK_PATHS)

    for candidate in candidates:
        candidate = candidate.expanduser()
        if candidate.exists() and candidate.is_file():
            print(
                f"preflight: cloning from live DB candidate {candidate}",
                file=sys.stderr,
            )
            return candidate

    if source is not None:
        raise RuntimeError("live SQLite database is missing")

    for parent in (Path("/source"), Path("/var/lib/platform"), Path("/tmp")):
        if not parent.is_dir():
            continue
        for candidate_name in (
            "platform.db",
            "platform.sqlite",
            "platform.sqlite3",
            "platform.sqlite.db",
            "platform_data.db",
        ):
            candidate = parent / candidate_name
            if candidate.exists() and candidate.is_file():
                print(
                    f"preflight: cloning from parent-scanned candidate {candidate}",
                    file=sys.stderr,
                )
                return candidate
    raise RuntimeError("live SQLite database is missing")


def _reset_preflight_database(path: Path) -> None:
    if path not in {
        PREFLIGHT_DATABASE_PATH,
        _source_snapshot_path(PREFLIGHT_DATABASE_PATH),
    }:
        raise RuntimeError("refusing to remove a non-preflight database path")
    for candidate in (
        path,
        Path(f"{path}-wal"),
        Path(f"{path}-shm"),
        Path(f"{path}-journal"),
    ):
        candidate.unlink(missing_ok=True)


def _ensure_writable_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _clone_via_tempfile(source_path: Path, destination: Path, *, label: str) -> Path:
    """Create a temporary local clone when live mount access is problematic."""
    output = destination.with_suffix(f"{destination.suffix}.copy.snapshot")
    copied = destination.with_suffix(f"{destination.suffix}.copy")
    for path in (copied, output):
        path.unlink(missing_ok=True)
    try:
        shutil.copy2(source_path, copied)
    except OSError as exc:
        raise RuntimeError(f"could not copy live database file: {label}") from exc
    with (
        sqlite3.connect(
            f"file:{copied}?mode=ro&immutable=1", uri=True, timeout=10
        ) as immutable_source,
        sqlite3.connect(output, timeout=10) as clone,
    ):
        immutable_source.backup(clone)
    copied_metadata = copied.lstat()
    if not stat.S_ISREG(copied_metadata.st_mode) or stat.S_ISLNK(
        copied_metadata.st_mode
    ):
        # Defensive: temp copy should remain regular file, avoid weird mounts.
        copied.unlink(missing_ok=True)
        raise RuntimeError(f"temporary copy path is invalid: {copied}")
    return output


def clone_live_database(
    source: Path | None = None,
    destination: Path = PREFLIGHT_DATABASE_PATH,
    *,
    allow_fresh: bool = False,
) -> bool:
    """Clone SQLite consistently through its backup API, including WAL state."""

    _reset_preflight_database(destination)
    source_snapshot = _source_snapshot_path(destination)
    _reset_preflight_database(source_snapshot)
    try:
        source_path = _resolve_live_database(source)
    except RuntimeError:
        if not allow_fresh:
            raise
        return False
    source_uri = f"file:{source_path}?mode=ro"
    try:
        _ensure_writable_parent(source_snapshot)
        try:
            with (
                sqlite3.connect(source_uri, uri=True, timeout=10) as live,
                sqlite3.connect(source_snapshot, timeout=10) as snapshot,
            ):
                live.backup(snapshot)
        except sqlite3.OperationalError as exc:
            error_message = str(exc).lower()
            # Fallback for mount/path permission quirks inside maintenance
            # containers that can report "open database file" even when source is
            # readable via direct copy.
            if (
                "unable to open database file" in error_message
                or "readonly" in error_message
                or "locked" in error_message
            ):
                copied = _clone_via_tempfile(
                    source_path, destination, label=str(source_path)
                )
                try:
                    with sqlite3.connect(copied, timeout=10) as copied_live:
                        with sqlite3.connect(source_snapshot, timeout=10) as snapshot:
                            copied_live.backup(snapshot)
                finally:
                    copied.unlink(missing_ok=True)
            else:
                raise
        source_digest = _sha256(source_snapshot)
        source_snapshot.chmod(0o400)
        with (
            sqlite3.connect(
                f"file:{source_snapshot}?mode=ro&immutable=1", uri=True, timeout=10
            ) as immutable_source,
            sqlite3.connect(destination, timeout=10) as clone,
        ):
            immutable_source.backup(clone)
        if _sha256(source_snapshot) != source_digest:
            raise RuntimeError("immutable preflight source snapshot changed")
    except sqlite3.Error as exc:
        _reset_preflight_database(destination)
        _reset_preflight_database(source_snapshot)
        raise RuntimeError("could not clone the live SQLite database") from exc
    return True


def main() -> int:
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import func, select

    from app.admin import import_profiles
    from app.config import Settings
    from app.db import create_database_engine, create_session_factory
    from app.models import WorkspaceProfile

    parser = argparse.ArgumentParser(
        description="Validate backend migrations and profile import without live DB writes"
    )
    parser.add_argument("--policy", required=True)
    parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help="explicit live sqlite path for maintenance preflight cloning",
    )
    parser.add_argument(
        "--allow-fresh",
        action="store_true",
        help="explicitly allow an empty source DB for standalone bootstrap checks",
    )
    args = parser.parse_args()

    settings = Settings.from_env()
    settings.validate()
    if settings.database_url != PREFLIGHT_DATABASE_URL:
        raise RuntimeError("preflight refuses to use a non-disposable database URL")
    policy = Path(args.policy)
    if not policy.is_file():
        raise RuntimeError("profile policy is unavailable inside preflight container")

    cloned = clone_live_database(
        source=args.source,
        allow_fresh=args.allow_fresh,
    )
    source_snapshot = _source_snapshot_path(PREFLIGHT_DATABASE_PATH)
    source_digest = _sha256(source_snapshot) if cloned else None
    print(
        "backend profile preflight source: "
        + ("consistent live SQLite backup" if cloned else "fresh empty database")
    )

    backend_root = Path(__file__).resolve().parent
    # The helper is bind-mounted beside the image's alembic.ini. Resolve the
    # checked-in backend root explicitly instead of inheriting a caller path.
    if not (backend_root / "alembic.ini").is_file():
        backend_root = Path("/opt/platform/backend")
    config = Config(str(backend_root / "alembic.ini"))
    command.upgrade(config, "head")
    import_profiles(settings, str(policy))

    if cloned and (source_digest is None or _sha256(source_snapshot) != source_digest):
        raise RuntimeError("immutable preflight source snapshot changed")

    engine = create_database_engine(settings)
    factory = create_session_factory(engine)
    with factory() as db:
        count = db.scalar(select(func.count()).select_from(WorkspaceProfile))
    engine.dispose()
    if not isinstance(count, int) or count <= 0:
        raise RuntimeError("preflight profile import produced no rows")
    print(f"backend profile import preflight passed: rows={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
