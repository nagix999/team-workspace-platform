from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

from preflight_profile_import import (  # noqa: E402
    PREFLIGHT_DATABASE_PATH,
    _reset_preflight_database,
    _source_snapshot_path,
    clone_live_database,
)


class SqlitePreflightCloneTests(unittest.TestCase):
    def test_backup_api_clones_committed_wal_state_and_replaces_stale_target(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "live.db"
            destination = root / "clone.db"
            live = sqlite3.connect(source)
            self.addCleanup(live.close)
            live.execute("PRAGMA journal_mode=WAL")
            live.execute("CREATE TABLE profile_marker (value TEXT NOT NULL)")
            live.execute("INSERT INTO profile_marker VALUES ('current')")
            live.commit()
            destination.write_bytes(b"stale")

            with mock.patch(
                "preflight_profile_import.PREFLIGHT_DATABASE_PATH", destination
            ):
                cloned = clone_live_database(source, destination)

            self.assertTrue(cloned)
            source_snapshot = _source_snapshot_path(destination)
            self.assertTrue(source_snapshot.is_file())
            self.assertEqual(source_snapshot.stat().st_mode & 0o777, 0o400)
            with sqlite3.connect(destination) as copied:
                value = copied.execute("SELECT value FROM profile_marker").fetchone()
            self.assertEqual(value, ("current",))
            with sqlite3.connect(
                f"file:{source_snapshot}?mode=ro&immutable=1", uri=True
            ) as copied_source:
                source_value = copied_source.execute(
                    "SELECT value FROM profile_marker"
                ).fetchone()
            self.assertEqual(source_value, ("current",))

    def test_missing_live_database_fails_closed_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "clone.db"
            destination.write_bytes(b"stale")
            with mock.patch(
                "preflight_profile_import.PREFLIGHT_DATABASE_PATH", destination
            ):
                with self.assertRaisesRegex(RuntimeError, "database is missing"):
                    clone_live_database(root / "missing.db", destination)

            self.assertFalse(destination.exists())

    def test_fresh_database_fallback_requires_explicit_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "clone.db"
            destination.write_bytes(b"stale")
            with mock.patch(
                "preflight_profile_import.PREFLIGHT_DATABASE_PATH", destination
            ):
                cloned = clone_live_database(
                    root / "missing.db", destination, allow_fresh=True
                )

            self.assertFalse(cloned)
            self.assertFalse(destination.exists())

    def test_reset_refuses_any_path_other_than_exact_preflight_target(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "non-preflight"):
            _reset_preflight_database(PREFLIGHT_DATABASE_PATH.with_name("other.db"))


class DeploymentReadinessTests(unittest.TestCase):
    def _readiness(
        self, *, recovery: str, active: bool = False
    ) -> subprocess.CompletedProcess[str]:
        script = r"""
docker() {
  local last="${!#}"
  if [[ "$1" == ps ]]; then
    if [[ "${FAKE_ACTIVE}" == true ]]; then printf '%s\n' ws-active; fi
    return 0
  fi
  if [[ "$1" == compose && "$2" == ps && "$3" == --status ]]; then
    if [[ "${last}" != jupyterhub ]]; then printf 'cid-%s\n' "${last}"; fi
    return 0
  fi
  if [[ "$1" == compose && "$2" == ps && "$3" == --all ]]; then
    printf 'cid-%s\n' "${last}"
    return 0
  fi
  if [[ "$1" == inspect ]]; then
    if [[ "${last}" == cid-frontend ]]; then printf '%s\n' missing; else printf '%s\n' healthy; fi
    return 0
  fi
  if [[ "$1" == compose && "$2" == exec && "${last}" == http://127.0.0.1:8080/healthz ]]; then
    return 0
  fi
  return 97
}
source scripts/deploy-profiles.sh
validate_deploy_options
require_no_active_singleusers && require_control_plane_ready
"""
        environment = {
            "PATH": "/usr/bin:/bin",
            "PROFILE_DEPLOY_ALLOW_UNHEALTHY_JUPYTERHUB": recovery,
            "FAKE_ACTIVE": "true" if active else "false",
        }
        return subprocess.run(
            ["bash", "-c", script],
            cwd=REPOSITORY_ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_default_readiness_rejects_unhealthy_hub(self) -> None:
        result = self._readiness(recovery="false")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("jupyterhub health is not-running", result.stderr)

    def test_explicit_recovery_accepts_only_hub_health_and_checks_frontend(
        self,
    ) -> None:
        result = self._readiness(recovery="true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("explicit recovery mode", result.stderr)

    def test_recovery_never_bypasses_active_singleuser_check(self) -> None:
        result = self._readiness(recovery="true", active=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stop every workspace first", result.stderr)

    def test_deploy_builds_and_health_waits_for_frontend(self) -> None:
        deploy = (REPOSITORY_ROOT / "scripts" / "deploy-profiles.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "docker compose build singleuser-image api jupyterhub frontend", deploy
        )
        self.assertIn(
            "docker compose up -d --no-deps --force-recreate --wait frontend",
            deploy,
        )
        self.assertIn(
            "docker compose stop gateway worker reconciler api",
            deploy,
        )
        self.assertIn(
            "docker compose up -d --no-deps --force-recreate --wait reconciler",
            deploy,
        )
        self.assertIn(
            "docker compose --profile maintenance run --rm --no-deps migration-preflight",
            deploy,
        )


if __name__ == "__main__":
    unittest.main()
