"""Fail-closed offline maintenance for the production Platform database.

This module deliberately has no Docker access.  The production host wrapper is
responsible for proving that every writer is stopped and for creating a verified
backup bundle before it invokes the mutating command.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import stat
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Sequence
from urllib.parse import quote

from .db import _sqlite_version_is_safe


EXPECTED_DATABASE_REVISION = "0004"
PRODUCTION_DATABASE_URL = "sqlite:////var/lib/platform/platform.db"
PRODUCTION_DATABASE_PATH = Path("/var/lib/platform/platform.db")
ACTIVE_STATUSES = ("PENDING", "RUNNING", "WAITING_EXTERNAL")
ACTIVE_OBSERVED_STATES = ("STARTING", "RUNNING", "STOPPING")
QUIESCABLE_OBSERVED_STATES = ("STOPPED", "NOT_FOUND")
BACKUP_BUNDLE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class OfflineMaintenanceError(RuntimeError):
    """An expected fail-closed refusal, safe to report to an operator."""

    def __init__(
        self, code: str, message: str, *, details: dict[str, object] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _safe_json(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _open_database(path: Path) -> sqlite3.Connection:
    if not path.is_absolute():
        raise OfflineMaintenanceError(
            "DATABASE_PATH_INVALID", "Platform database path must be absolute"
        )
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise OfflineMaintenanceError(
            "DATABASE_UNAVAILABLE", "Platform database is unavailable"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise OfflineMaintenanceError(
            "DATABASE_PATH_INVALID",
            "Platform database must be a regular non-symlink file",
        )
    uri = f"file:{quote(str(path), safe='/')}?mode=rw"
    try:
        connection = sqlite3.connect(
            uri,
            uri=True,
            isolation_level=None,
            timeout=5,
        )
    except sqlite3.Error as exc:
        raise OfflineMaintenanceError(
            "DATABASE_UNAVAILABLE", "Platform database is unavailable"
        ) from exc
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()
        if foreign_keys is None or foreign_keys[0] != 1:
            raise OfflineMaintenanceError(
                "DATABASE_SAFETY_UNAVAILABLE",
                "SQLite foreign-key enforcement is unavailable",
            )
    except Exception:
        connection.close()
        raise
    return connection


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _require_exact_schema(connection: sqlite3.Connection) -> None:
    try:
        revisions = [
            str(row[0])
            for row in connection.execute(
                "SELECT version_num FROM alembic_version ORDER BY version_num"
            )
        ]
    except sqlite3.Error as exc:
        raise OfflineMaintenanceError(
            "DATABASE_SCHEMA_INVALID", "Platform database schema is unavailable"
        ) from exc
    if revisions != [EXPECTED_DATABASE_REVISION]:
        raise OfflineMaintenanceError(
            "DATABASE_REVISION_MISMATCH",
            f"Platform database must be at revision {EXPECTED_DATABASE_REVISION}",
            details={"actual_revisions": revisions},
        )

    required_columns = {
        "workspaces": {
            "id",
            "desired_state",
            "observed_state",
            "spec_version",
            "row_version",
            "updated_at",
            "deletion_started_at",
            "deletion_checkpoint",
            "archived_at",
        },
        "operations": {"id", "status"},
        "user_provisioning_jobs": {"user_id", "status"},
        "workspace_deletion_jobs": {"workspace_id", "status"},
        "spawn_authorizations": {
            "id",
            "workspace_id",
            "consumed_at",
            "revoked_at",
        },
        "audit_events": {
            "id",
            "actor_user_id",
            "workspace_id",
            "action",
            "result",
            "request_id",
            "safe_metadata_json",
            "created_at",
        },
    }
    missing: dict[str, list[str]] = {}
    for table, expected in required_columns.items():
        absent = sorted(expected - _table_columns(connection, table))
        if absent:
            missing[table] = absent
    if missing:
        raise OfflineMaintenanceError(
            "DATABASE_SCHEMA_INVALID",
            "Platform database does not match the offline maintenance contract",
            details={"missing_columns": missing},
        )


def _tuples(count: int) -> str:
    return ",".join("?" for _ in range(count))


def _ids(connection: sqlite3.Connection, sql: str, values: Sequence[str]) -> list[str]:
    return [str(row[0]) for row in connection.execute(sql, tuple(values))]


def _require_quiescent_database(connection: sqlite3.Connection) -> None:
    active_placeholders = _tuples(len(ACTIVE_STATUSES))
    observed_placeholders = _tuples(len(ACTIVE_OBSERVED_STATES))
    quiescable_placeholders = _tuples(len(QUIESCABLE_OBSERVED_STATES))
    violations: dict[str, list[str]] = {
        "active_operations": _ids(
            connection,
            f"SELECT id FROM operations WHERE status IN ({active_placeholders}) "
            "ORDER BY id",
            ACTIVE_STATUSES,
        ),
        "active_provisioning_jobs": _ids(
            connection,
            "SELECT user_id FROM user_provisioning_jobs "
            f"WHERE status IN ({active_placeholders}) ORDER BY user_id",
            ACTIVE_STATUSES,
        ),
        "active_deletion_jobs": _ids(
            connection,
            "SELECT workspace_id FROM workspace_deletion_jobs "
            f"WHERE status IN ({active_placeholders}) ORDER BY workspace_id",
            ACTIVE_STATUSES,
        ),
        "actively_observed_workspaces": _ids(
            connection,
            "SELECT id FROM workspaces WHERE archived_at IS NULL "
            f"AND observed_state IN ({observed_placeholders}) ORDER BY id",
            ACTIVE_OBSERVED_STATES,
        ),
        "unsupported_running_intent": _ids(
            connection,
            "SELECT id FROM workspaces WHERE archived_at IS NULL "
            "AND desired_state = 'RUNNING' "
            "AND (deletion_started_at IS NOT NULL "
            "OR deletion_checkpoint IS NOT NULL "
            f"OR observed_state NOT IN ({quiescable_placeholders})) ORDER BY id",
            QUIESCABLE_OBSERVED_STATES,
        ),
    }
    present = {name: ids for name, ids in violations.items() if ids}
    if present:
        raise OfflineMaintenanceError(
            "DATABASE_NOT_QUIESCENT",
            "Platform database contains state that cannot be normalized offline",
            details=present,
        )


def _targets(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    placeholders = _tuples(len(QUIESCABLE_OBSERVED_STATES))
    return connection.execute(
        "SELECT id, desired_state, observed_state, spec_version, row_version "
        "FROM workspaces WHERE archived_at IS NULL "
        "AND deletion_started_at IS NULL AND deletion_checkpoint IS NULL "
        "AND desired_state = 'RUNNING' "
        f"AND observed_state IN ({placeholders}) ORDER BY id",
        QUIESCABLE_OBSERVED_STATES,
    ).fetchall()


def quiesce_stopped_intent(
    database_path: Path,
    *,
    apply: bool = False,
    expected_count: int | None = None,
    backup_bundle_id: str | None = None,
    enforce_safe_sqlite: bool = False,
) -> dict[str, object]:
    """Preview or atomically quiesce orphaned RUNNING intent.

    Runtime observation and all lifecycle history are intentionally preserved.
    """

    if apply:
        if expected_count is None or expected_count <= 0:
            raise OfflineMaintenanceError(
                "EXPECTED_COUNT_REQUIRED",
                "Apply requires a positive --expected-count",
            )
        if backup_bundle_id is None or not BACKUP_BUNDLE_ID_RE.fullmatch(
            backup_bundle_id
        ):
            raise OfflineMaintenanceError(
                "BACKUP_BUNDLE_REQUIRED",
                "Apply requires a safe --backup-bundle-id",
            )
    elif expected_count is not None or backup_bundle_id is not None:
        raise OfflineMaintenanceError(
            "APPLY_ARGUMENT_WITHOUT_APPLY",
            "Expected count and backup bundle are valid only with --apply",
        )
    if enforce_safe_sqlite and not _sqlite_version_is_safe(sqlite3.sqlite_version_info):
        raise OfflineMaintenanceError(
            "UNSAFE_SQLITE_RUNTIME",
            "SQLite runtime does not satisfy the production WAL safety contract",
            details={"sqlite_version": sqlite3.sqlite_version},
        )

    connection = _open_database(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_exact_schema(connection)
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise OfflineMaintenanceError(
                "DATABASE_FOREIGN_KEY_INVALID",
                "Platform database has foreign-key violations",
            )
        _require_quiescent_database(connection)
        targets = _targets(connection)
        workspace_ids = [str(row["id"]) for row in targets]
        result: dict[str, object] = {
            "action": "quiesce-stopped-intent",
            "applied": apply,
            "schema_revision": EXPECTED_DATABASE_REVISION,
            "target_count": len(targets),
            "workspace_ids": workspace_ids,
        }
        if not apply:
            connection.rollback()
            return result
        if len(targets) != expected_count:
            raise OfflineMaintenanceError(
                "EXPECTED_COUNT_MISMATCH",
                "Offline maintenance target count changed",
                details={
                    "expected_count": expected_count,
                    "actual_count": len(targets),
                    "workspace_ids": workspace_ids,
                },
            )

        now = datetime.utcnow().isoformat(sep=" ", timespec="microseconds")
        request_id = f"offline-maintenance:{uuid.uuid4().hex}"
        revoked_total = 0
        for target in targets:
            workspace_id = str(target["id"])
            revoked = connection.execute(
                "UPDATE spawn_authorizations SET revoked_at = ? "
                "WHERE workspace_id = ? AND consumed_at IS NULL "
                "AND revoked_at IS NULL",
                (now, workspace_id),
            ).rowcount
            revoked_total += revoked
            updated = connection.execute(
                "UPDATE workspaces SET desired_state = 'STOPPED', "
                "spec_version = spec_version + 1, row_version = row_version + 1, "
                "updated_at = ? WHERE id = ? AND archived_at IS NULL "
                "AND deletion_started_at IS NULL AND deletion_checkpoint IS NULL "
                "AND desired_state = 'RUNNING' "
                "AND observed_state = ? AND spec_version = ? AND row_version = ?",
                (
                    now,
                    workspace_id,
                    str(target["observed_state"]),
                    int(target["spec_version"]),
                    int(target["row_version"]),
                ),
            ).rowcount
            if updated != 1:
                raise OfflineMaintenanceError(
                    "TARGET_CHANGED",
                    "A workspace changed during offline maintenance",
                    details={"workspace_id": workspace_id},
                )
            metadata = {
                "backup_bundle_id": backup_bundle_id,
                "previous_desired_state": str(target["desired_state"]),
                "preserved_observed_state": str(target["observed_state"]),
                "reason": "CONTAINERS_REMOVED_WHILE_STOPPED",
                "revoked_unconsumed_authorizations": revoked,
                "schema_version": 1,
            }
            connection.execute(
                "INSERT INTO audit_events "
                "(id, actor_user_id, workspace_id, action, result, request_id, "
                "safe_metadata_json, created_at) VALUES (?, NULL, ?, ?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    workspace_id,
                    "OFFLINE_WORKSPACE_QUIESCED",
                    "APPLIED",
                    request_id,
                    _safe_json(metadata),
                    now,
                ),
            )

        if _targets(connection):
            raise OfflineMaintenanceError(
                "POSTCONDITION_FAILED",
                "Offline maintenance left quiescable RUNNING intent behind",
            )
        _require_quiescent_database(connection)
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise OfflineMaintenanceError(
                "DATABASE_FOREIGN_KEY_INVALID",
                "Offline maintenance produced a foreign-key violation",
            )
        connection.commit()
        result.update(
            {
                "audit_event_count": len(targets),
                "backup_bundle_id": backup_bundle_id,
                "revoked_unconsumed_authorizations": revoked_total,
            }
        )
        return result
    except OfflineMaintenanceError:
        if connection.in_transaction:
            connection.rollback()
        raise
    except sqlite3.Error as exc:
        if connection.in_transaction:
            connection.rollback()
        raise OfflineMaintenanceError(
            "DATABASE_OPERATION_FAILED", "Offline maintenance database operation failed"
        ) from exc
    finally:
        connection.close()


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fail-closed offline Platform database maintenance"
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    quiesce = subcommands.add_parser("quiesce-stopped-intent")
    quiesce.add_argument("--apply", action="store_true")
    quiesce.add_argument("--expected-count", type=_positive_integer)
    quiesce.add_argument("--backup-bundle-id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        database_url = os.environ.get("PLATFORM_DATABASE_URL", "")
        if database_url != PRODUCTION_DATABASE_URL:
            raise OfflineMaintenanceError(
                "DATABASE_URL_INVALID",
                "Offline maintenance requires the exact production database URL",
            )
        enforce_safe_sqlite = (
            os.environ.get("PLATFORM_ENFORCE_SAFE_SQLITE", "").strip().lower()
        )
        if enforce_safe_sqlite not in {"true", "false"}:
            raise OfflineMaintenanceError(
                "SQLITE_SAFETY_SETTING_INVALID",
                "PLATFORM_ENFORCE_SAFE_SQLITE must be explicitly true or false",
            )
        result = quiesce_stopped_intent(
            PRODUCTION_DATABASE_PATH,
            apply=bool(args.apply),
            expected_count=args.expected_count,
            backup_bundle_id=args.backup_bundle_id,
            enforce_safe_sqlite=enforce_safe_sqlite == "true",
        )
    except OfflineMaintenanceError as exc:
        print(
            _safe_json(
                {
                    "error": {
                        "code": exc.code,
                        "details": exc.details,
                        "message": str(exc),
                    },
                    "ok": False,
                }
            ),
            file=sys.stderr,
        )
        return 1
    print(_safe_json({"ok": True, **result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
