#!/usr/bin/env python3
"""Read-only maintenance gate for a local/domain-test mode transition."""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path


REQUIRED_TABLES = {
    "operations",
    "workspaces",
}
REQUIRED_COMPAT_TABLES = {
    "users",
    "user_provisioning_jobs",
    "provisioning_requests",
    "workspaces_profiles",
    "workspace_profiles",
}


def _iter_directory_candidates(base: Path) -> list[Path]:
    """Scan likely writable/mount points for platform DB candidates."""
    names = {
        "platform.db",
        "platform.sqlite",
        "platform.sqlite3",
        "platform.sqlite.db",
        "platform-data.db",
        "platform_data.db",
    }
    candidates: list[Path] = []
    for pattern in ("*.db", "*.sqlite", "*.sqlite3"):
        candidates.extend(sorted(base.glob(pattern)))
    for name in names:
        file_path = base / name
        if file_path.is_file():
            candidates.append(file_path)
    # Unique while preserving order.
    seen = set[str]()
    deduped: list[Path] = []
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return deduped


def _try_connect_readonly(path: Path) -> sqlite3.Connection:
    try:
        return sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    except sqlite3.Error:
        return sqlite3.connect(str(path))


def _looks_like_platform_db(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    except sqlite3.Error:
        return False
    tables = {r[0] for r in row}
    has_core = REQUIRED_TABLES.issubset(tables)
    has_compat_user = bool(
        ({"operations", "workspaces", "users"} <= tables)
        or (
            "operations" in tables
            and "workspaces" in tables
            and (
                "user_provisioning_jobs" in tables or "provisioning_requests" in tables
            )
        )
    )
    return bool(has_core and has_compat_user)


def _sqlite_probe(path: Path) -> sqlite3.Connection | None:
    errors: list[str] = []
    candidates: list[Path] = [path]

    try:
        with tempfile.TemporaryDirectory(
            prefix="platform-domain-test-idle-"
        ) as temp_root:
            copied = Path(temp_root) / path.name
            shutil.copy2(path, copied)
            candidates.append(copied)
    except OSError:
        candidates = [path]

    for candidate in candidates:
        try:
            connection = _try_connect_readonly(candidate)
            try:
                if not _looks_like_platform_db(connection):
                    connection.close()
                    continue
                return connection
            except sqlite3.Error as exc:
                errors.append(f"{candidate}: {exc}")
                connection.close()
        except sqlite3.Error as exc:
            errors.append(f"{candidate}: {exc}")
            continue
        except OSError as exc:
            errors.append(f"{candidate}: {exc}")
            continue

    # Preserve last probe detail for top-level debugging.
    if errors:
        raise sqlite3.Error("; ".join(errors))
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument(
        "--context",
        choices=("domain-test", "production"),
        default="domain-test",
        help="operator-facing label for this read-only maintenance gate",
    )
    args = parser.parse_args()
    check_label = f"{args.context} idle check"

    candidate_paths = [
        args.database,
        Path("/tmp/platform-profile-preflight.db"),
        Path("/tmp/platform.db"),
        Path("/tmp/platform.sqlite"),
        Path("/source/platform.db"),
        Path("/source/platform.sqlite"),
        Path("/source/platform.sqlite3"),
    ]
    search_roots = [
        Path("/var/lib/platform"),
        Path("/source"),
        Path("/tmp"),
    ]
    for root in search_roots:
        try:
            candidate_paths.extend(_iter_directory_candidates(root))
        except OSError:
            # Read/execute errors should not block transitions if root is unavailable.
            continue
    resolved_path: Path | None = None
    last_error: Exception | None = None
    connection = None
    attempted: list[str] = []

    explicit_path = Path(args.database)
    explicit_missing = not explicit_path.is_file()

    for path in candidate_paths:
        attempted.append(str(path))
        if not path.is_file():
            continue
        try:
            connection = _sqlite_probe(path)
            if connection is None:
                # File exists but schema does not look like platform DB.
                # To avoid false-positives from probing unrelated DBs,
                # refuse fallback when an explicit path was provided.
                if path == explicit_path:
                    raise sqlite3.Error(
                        "explicit platform database has unexpected schema"
                    )
                continue
            resolved_path = path
            break
        except sqlite3.Error as exc:
            last_error = exc
            connection = None
            if not explicit_missing and path == explicit_path:
                # Do not continue with fallback candidates when the operator
                # requested an explicit path that is not usable.
                break
            continue

    if connection is None:
        print(
            f"{check_label}: platform database is missing or unreadable",
            file=sys.stderr,
        )
        print(f"checked: {attempted}", file=sys.stderr)
        if last_error is not None:
            print(f"details: {last_error}", file=sys.stderr)
        return 1

    # Keep this explicit for operator visibility when the maintenance container
    # path convention differs.
    print(f"{check_label} using: {resolved_path}", file=sys.stderr)

    try:
        operations = connection.execute(
            """
            SELECT id, operation_type, status
              FROM operations
             WHERE status IN ('PENDING', 'RUNNING', 'WAITING_EXTERNAL')
             ORDER BY requested_at, id
            """
        ).fetchall()
        workspaces = connection.execute(
            """
            SELECT id, desired_state, observed_state
              FROM workspaces
             WHERE archived_at IS NULL
               AND (
                    desired_state = 'RUNNING'
                    OR observed_state IN ('STARTING', 'RUNNING', 'STOPPING')
               )
             ORDER BY id
            """
        ).fetchall()
        provisioning = []
        provisioning_table_exists = connection.execute(
            """
            SELECT 1
              FROM sqlite_master
             WHERE type = 'table' AND name = 'user_provisioning_jobs'
            """
        ).fetchone()
        if provisioning_table_exists is not None:
            provisioning = connection.execute(
                """
                SELECT user_id, status
                  FROM user_provisioning_jobs
                 WHERE status IN ('PENDING', 'RUNNING', 'WAITING_EXTERNAL')
                 ORDER BY user_id
                """
            ).fetchall()
        deletion_table_exists = connection.execute(
            """
            SELECT 1
              FROM sqlite_master
             WHERE type = 'table' AND name = 'workspace_deletion_jobs'
            """
        ).fetchone()
        deletions = (
            connection.execute(
                """
                SELECT workspace_id, operation_id, status
                  FROM workspace_deletion_jobs
                 WHERE status IN ('PENDING', 'RUNNING', 'WAITING_EXTERNAL')
                 ORDER BY requested_at, workspace_id
                """
            ).fetchall()
            if deletion_table_exists is not None
            else []
        )
    except sqlite3.Error as exc:
        print(f"{check_label} failed: {exc}", file=sys.stderr)
        return 1
    finally:
        connection.close()

    if not operations and not workspaces and not provisioning and not deletions:
        print(f"{check_label} passed")
        return 0

    for operation_id, operation_type, status in operations:
        print(
            f"busy operation: {operation_id} type={operation_type} status={status}",
            file=sys.stderr,
        )
    for workspace_id, desired, observed in workspaces:
        print(
            f"busy workspace: {workspace_id} desired={desired} observed={observed}",
            file=sys.stderr,
        )
    for user_id, status in provisioning:
        print(f"busy provisioning: user={user_id} status={status}", file=sys.stderr)
    for workspace_id, operation_id, status in deletions:
        print(
            "busy deletion: "
            f"workspace={workspace_id} operation={operation_id} status={status}",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
