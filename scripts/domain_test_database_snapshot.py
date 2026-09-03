#!/usr/bin/env python3
"""Create and validate security-sensitive domain-test SQLite snapshots.

The snapshot subcommand runs inside a networkless maintenance container whose
source volume is read-only. Bundle preparation/finalization runs on the host so
the operator, rather than a container UID, owns every backup artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


BACKUP_FILENAMES = ("platform.sqlite", "jupyterhub.sqlite")
MANIFEST_FILENAME = "manifest.json"
SOURCE_PATH_CANDIDATES = (
    "platform.db",
    "platform.sqlite",
    "platform.sqlite3",
    "platform.sqlite.db",
    "platform_data.db",
    "jupyterhub.db",
    "jupyterhub.sqlite",
    "jupyterhub.sqlite3",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_secure_directory(
    path: Path, *, owner_uid: int, exact_mode: bool = True
) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RuntimeError(f"backup directory is unavailable: {path}") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != owner_uid
        or (
            stat.S_IMODE(metadata.st_mode) != 0o700
            if exact_mode
            else stat.S_IMODE(metadata.st_mode) & 0o077 != 0
        )
        or path.resolve(strict=True) != path.absolute()
    ):
        raise RuntimeError(
            "backup directory must be a non-symlink directory owned by the "
            "operator with mode 0700"
        )


def _require_secure_file(path: Path, *, owner_uid: int) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RuntimeError(f"backup file is unavailable: {path.name}") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != owner_uid
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        raise RuntimeError(
            f"backup file {path.name} must be operator-owned, regular, "
            "single-linked and mode 0600"
        )


def prepare_bundle(parent: Path, bundle_name: str) -> Path:
    if not bundle_name or any(
        character
        not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
        for character in bundle_name
    ):
        raise RuntimeError("backup bundle name is invalid")
    owner_uid = os.getuid()
    # Refuse symlinks in both operator-controlled parent components. The domain
    # transition passes the fixed repository path `.runtime/backups` here.
    _require_secure_directory(parent, owner_uid=owner_uid, exact_mode=False)
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    bundle_fd: int | None = None
    try:
        os.mkdir(bundle_name, mode=0o700, dir_fd=parent_fd)
        # `.runtime` is intentionally setgid so Hub/host allocator files share
        # one group. Linux propagates that bit to a newly-created backup
        # directory even with mode=0700; normalize the exact private bundle
        # contract through a no-follow descriptor before exposing the path.
        bundle_fd = os.open(
            bundle_name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        os.fchmod(bundle_fd, 0o700)
        os.fsync(bundle_fd)
        os.fsync(parent_fd)
    except FileExistsError as exc:
        raise RuntimeError("backup bundle already exists") from exc
    finally:
        if bundle_fd is not None:
            os.close(bundle_fd)
        os.close(parent_fd)
    bundle = parent / bundle_name
    _require_secure_directory(bundle, owner_uid=owner_uid)
    return bundle


def _sqlite_revision(path: Path, query: str) -> str:
    try:
        with sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True) as database:
            row = database.execute(query).fetchone()
            integrity = database.execute("PRAGMA quick_check").fetchone()
    except sqlite3.Error as exc:
        raise RuntimeError(f"backup SQLite validation failed: {path.name}") from exc
    if integrity != ("ok",):
        raise RuntimeError(f"backup SQLite integrity failed: {path.name}")
    if row is None or not isinstance(row[0], str) or not row[0]:
        raise RuntimeError(f"backup schema revision is missing: {path.name}")
    return row[0]


def finalize_bundle(bundle: Path) -> dict[str, object]:
    owner_uid = os.getuid()
    _require_secure_directory(bundle, owner_uid=owner_uid)
    manifest_path = bundle / MANIFEST_FILENAME
    if manifest_path.exists() or manifest_path.is_symlink():
        raise RuntimeError("backup manifest already exists")

    revisions = {
        "platform.sqlite": _sqlite_revision(
            bundle / "platform.sqlite", "SELECT version_num FROM alembic_version"
        ),
        "jupyterhub.sqlite": _sqlite_revision(
            bundle / "jupyterhub.sqlite", "SELECT version_num FROM alembic_version"
        ),
    }
    files: dict[str, dict[str, object]] = {}
    for filename in BACKUP_FILENAMES:
        path = bundle / filename
        # docker cp creates local files as the invoking operator. Normalize mode
        # before checking so a permissive client umask can never leak secrets.
        try:
            copied_metadata = path.lstat()
        except OSError as exc:
            raise RuntimeError(f"backup file is unavailable: {filename}") from exc
        if (
            not stat.S_ISREG(copied_metadata.st_mode)
            or stat.S_ISLNK(copied_metadata.st_mode)
            or copied_metadata.st_uid != owner_uid
            or copied_metadata.st_nlink != 1
        ):
            raise RuntimeError(f"backup file identity is unsafe: {filename}")
        os.chmod(path, 0o600, follow_symlinks=False)
        _require_secure_file(path, owner_uid=owner_uid)
        files[filename] = {
            "sha256": _sha256(path),
            "size_bytes": path.stat().st_size,
            "schema_revision": revisions[filename],
        }

    manifest: dict[str, object] = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }
    payload = (
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    descriptor = os.open(
        manifest_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory_fd = os.open(bundle, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    verify_bundle(bundle)
    return manifest


def verify_bundle(bundle: Path) -> dict[str, object]:
    owner_uid = os.getuid()
    _require_secure_directory(bundle, owner_uid=owner_uid)
    manifest_path = bundle / MANIFEST_FILENAME
    _require_secure_file(manifest_path, owner_uid=owner_uid)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("backup manifest is invalid") from exc
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"schema_version", "created_at", "files"}
        or manifest["schema_version"] != 1
        or not isinstance(manifest["created_at"], str)
        or not isinstance(manifest["files"], dict)
        or set(manifest["files"]) != set(BACKUP_FILENAMES)
    ):
        raise RuntimeError("backup manifest schema is invalid")
    for filename in BACKUP_FILENAMES:
        path = bundle / filename
        _require_secure_file(path, owner_uid=owner_uid)
        record = manifest["files"][filename]
        if (
            not isinstance(record, dict)
            or set(record) != {"sha256", "size_bytes", "schema_revision"}
            or not isinstance(record["sha256"], str)
            or len(record["sha256"]) != 64
            or not isinstance(record["size_bytes"], int)
            or record["size_bytes"] <= 0
            or not isinstance(record["schema_revision"], str)
            or path.stat().st_size != record["size_bytes"]
            or _sha256(path) != record["sha256"]
        ):
            raise RuntimeError(f"backup manifest mismatch: {filename}")
    return manifest


def _resolve_database_source(source: Path) -> Path:
    candidates: list[Path]
    if source.is_dir():
        candidates = _collect_sources_from_parent(source)
        if not candidates:
            raise RuntimeError(f"snapshot source is unavailable: {source}")
        return candidates[0]
    if source.is_file():
        return source
    parent = source.parent
    candidates = _collect_sources_from_parent(parent, preferred_stem=source.stem)
    if candidates:
        return candidates[0]
    raise RuntimeError(f"snapshot source is unavailable: {source}")


def _connect_snapshot_source(source: Path) -> sqlite3.Connection:
    """Open a source DB without ever requiring writes to its mounted volume.

    Normal read-only mode is preferred because it observes committed WAL data.
    Docker's read-only named-volume mount can prevent SQLite from creating its
    lock/journal side files even when no WAL exists. After all database writers
    have been stopped, immutable mode is safe only when no WAL/SHM sidecar is
    present; fail closed instead of silently omitting committed WAL records.
    """

    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)
        connection.execute("PRAGMA schema_version").fetchone()
        return connection
    except sqlite3.OperationalError:
        if connection is not None:
            connection.close()

    wal_path = Path(f"{source}-wal")
    shm_path = Path(f"{source}-shm")
    if (wal_path.exists() and wal_path.stat().st_size > 0) or shm_path.exists():
        raise RuntimeError(
            "read-only SQLite source requires immutable fallback but WAL/SHM "
            "sidecars are present; stop writers and checkpoint before snapshot"
        )

    connection = sqlite3.connect(
        f"file:{source}?mode=ro&immutable=1", uri=True, timeout=30
    )
    connection.execute("PRAGMA schema_version").fetchone()
    return connection


def _collect_sources_from_parent(
    parent: Path, *, preferred_stem: str | None = None
) -> list[Path]:
    raw_candidates = []
    for pattern in ("*.db", "*.sqlite", "*.sqlite3"):
        raw_candidates.extend(sorted(parent.glob(pattern)))
    for name in SOURCE_PATH_CANDIDATES:
        candidate = parent / name
        if candidate.is_file():
            raw_candidates.append(candidate)

    deduped: list[Path] = []
    for candidate in raw_candidates:
        if candidate.is_symlink():
            continue
        if candidate not in deduped:
            deduped.append(candidate)
    if preferred_stem:
        preferred_stem = preferred_stem.lower()
        priority: list[Path] = []
        fallback: list[Path] = []
        for candidate in deduped:
            lowered = candidate.name.lower()
            if preferred_stem in lowered:
                priority.append(candidate)
            else:
                fallback.append(candidate)
        return sorted(priority) if priority else sorted(fallback)
    return sorted(deduped)


def snapshot_database(
    source: Path,
    output: Path,
    *,
    hold_seconds: int = 0,
    owner_uid: int | None = None,
    owner_gid: int | None = None,
) -> None:
    if (owner_uid is None) != (owner_gid is None) or any(
        value is not None and (value < 0 or value > 2**31 - 1)
        for value in (owner_uid, owner_gid)
    ):
        raise RuntimeError("snapshot owner UID/GID must be a valid pair")
    try:
        source = _resolve_database_source(source)
    except OSError as exc:
        raise RuntimeError(f"snapshot source is unavailable: {source}") from exc
    print(f"snapshot_database: source={source} output={output}", file=sys.stderr)
    try:
        source_metadata = source.lstat()
    except OSError as exc:
        raise RuntimeError(
            f"snapshot source database is unavailable: {source}"
        ) from exc
    if not stat.S_ISREG(source_metadata.st_mode) or stat.S_ISLNK(
        source_metadata.st_mode
    ):
        raise RuntimeError(
            f"snapshot source must be a regular non-symlink file: {source} "
            f"(mode={source_metadata.st_mode:o})"
        )
    if output.exists() or output.is_symlink():
        raise RuntimeError(f"snapshot output already exists or is a symlink: {output}")
    last_error: sqlite3.Error | None = None
    last_attempt = 0
    for attempt in range(1, 13):
        last_attempt = attempt
        try:
            with (
                _connect_snapshot_source(source) as live,
                sqlite3.connect(output, timeout=30) as snapshot,
            ):
                live.execute("PRAGMA busy_timeout = 30000")
                snapshot.execute("PRAGMA busy_timeout = 30000")
                live.backup(snapshot)
                try:
                    live.execute("PRAGMA wal_checkpoint(PASSIVE)")
                except sqlite3.Error:
                    pass
                if snapshot.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise RuntimeError("snapshot SQLite integrity check failed")
            last_error = None
            break
        except sqlite3.OperationalError as exc:
            last_error = exc
            # Retry only for transient lock/timeouts; persist other failures so we
            # can fail fast with original trace.
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                break
            if attempt >= 12:
                break
            time.sleep(1)
            continue
        except sqlite3.Error as exc:
            last_error = exc
            break
    if last_error is not None:
        output.unlink(missing_ok=True)
        raise RuntimeError(
            f"SQLite online backup failed after {last_attempt} attempt(s): "
            f"{last_error.__class__.__name__}: {last_error}"
        ) from last_error
    os.chmod(output, 0o600, follow_symlinks=False)
    if owner_uid is not None and owner_gid is not None:
        os.chown(output, owner_uid, owner_gid, follow_symlinks=False)
    with output.open("rb") as snapshot_file:
        os.fsync(snapshot_file.fileno())
    print(json.dumps({"sha256": _sha256(output), "size_bytes": output.stat().st_size}))
    sys.stdout.flush()
    if hold_seconds:
        ready = Path(f"{output}.ready")
        descriptor = os.open(
            ready,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        try:
            os.write(descriptor, b"ready\n")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        time.sleep(hold_seconds)


def restore_database(
    source: Path,
    destination: Path,
    *,
    expected_sha256: str,
    owner_uid: int,
    owner_gid: int,
) -> None:
    allowed = {
        (Path("/restore/input.sqlite"), Path("/source/platform.db"), 999, 999),
        (
            Path("/restore/input.sqlite"),
            Path("/source/jupyterhub.sqlite"),
            10001,
            10001,
        ),
    }
    if (source, destination, owner_uid, owner_gid) not in allowed:
        raise RuntimeError("database restore target is outside the fixed contract")
    if len(expected_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in expected_sha256
    ):
        raise RuntimeError("database restore digest is invalid")
    try:
        source_metadata = source.lstat()
        destination_parent = destination.parent.lstat()
    except OSError as exc:
        raise RuntimeError("database restore path is unavailable") from exc
    if (
        not stat.S_ISREG(source_metadata.st_mode)
        or stat.S_ISLNK(source_metadata.st_mode)
        or not stat.S_ISDIR(destination_parent.st_mode)
        or stat.S_ISLNK(destination_parent.st_mode)
        or _sha256(source) != expected_sha256
    ):
        raise RuntimeError("database restore source or destination is invalid")

    temporary = destination.parent / f".{destination.name}.restore-{os.getpid()}"
    if temporary.exists() or temporary.is_symlink():
        raise RuntimeError("database restore temporary path already exists")
    companions = tuple(
        Path(f"{destination}{suffix}") for suffix in ("-wal", "-shm", "-journal")
    )
    try:
        with (
            sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=10) as backup,
            sqlite3.connect(temporary, timeout=10) as restored,
        ):
            backup.backup(restored)
            if restored.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise RuntimeError("restored SQLite integrity check failed")
        with temporary.open("rb") as restored_file:
            os.fsync(restored_file.fileno())
        os.chmod(temporary, 0o600, follow_symlinks=False)
        os.chown(temporary, owner_uid, owner_gid, follow_symlinks=False)
        for companion in companions:
            companion.unlink(missing_ok=True)
        os.replace(temporary, destination)
        for companion in companions:
            companion.unlink(missing_ok=True)
        directory_fd = os.open(
            destination.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("database restore failed") from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="action", required=True)
    snapshot = subparsers.add_parser("snapshot")
    snapshot.add_argument("--source", type=Path, required=True)
    snapshot.add_argument("--output", type=Path, required=True)
    snapshot.add_argument("--hold-seconds", type=int, choices=range(0, 601), default=0)
    snapshot.add_argument("--owner-uid", type=int)
    snapshot.add_argument("--owner-gid", type=int)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--parent", type=Path, required=True)
    prepare.add_argument("--name", required=True)
    finalize = subparsers.add_parser("finalize")
    finalize.add_argument("--bundle", type=Path, required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--bundle", type=Path, required=True)
    digest = subparsers.add_parser("digest")
    digest.add_argument("--bundle", type=Path, required=True)
    digest.add_argument("--filename", choices=BACKUP_FILENAMES, required=True)
    restore = subparsers.add_parser("restore")
    restore.add_argument("--source", type=Path, required=True)
    restore.add_argument("--destination", type=Path, required=True)
    restore.add_argument("--owner-uid", type=int, required=True)
    restore.add_argument("--owner-gid", type=int, required=True)
    args = parser.parse_args()
    try:
        if args.action == "snapshot":
            snapshot_database(
                args.source,
                args.output,
                hold_seconds=args.hold_seconds,
                owner_uid=args.owner_uid,
                owner_gid=args.owner_gid,
            )
        elif args.action == "prepare":
            print(prepare_bundle(args.parent, args.name))
        elif args.action == "finalize":
            finalize_bundle(args.bundle)
            print(args.bundle)
        elif args.action == "verify":
            verify_bundle(args.bundle)
            print(args.bundle)
        elif args.action == "digest":
            manifest = verify_bundle(args.bundle)
            print(manifest["files"][args.filename]["sha256"])
        else:
            expected_sha256 = os.environ.get("PLATFORM_RESTORE_EXPECTED_SHA256", "")
            restore_database(
                args.source,
                args.destination,
                expected_sha256=expected_sha256,
                owner_uid=args.owner_uid,
                owner_gid=args.owner_gid,
            )
    except RuntimeError as exc:
        print(f"database snapshot: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
