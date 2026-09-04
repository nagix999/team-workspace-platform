from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_SCRIPT = ROOT / "scripts" / "production.sh"


class ProductionHostContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.project = Path(self.temporary_directory.name)
        (self.project / "scripts").mkdir()
        shutil.copy2(PRODUCTION_SCRIPT, self.project / "scripts" / "production.sh")
        shutil.copy2(
            ROOT / "scripts" / "check_production_subnet_conflicts.py",
            self.project / "scripts" / "check_production_subnet_conflicts.py",
        )
        self.fake_bin = self.project / "fake-bin"
        self.fake_bin.mkdir()
        self.docker_log = self.project / "docker.log"
        self.ip_log = self.project / "ip.log"
        self.cert_file = self.project / "fullchain.pem"
        self.key_file = self.project / "privkey.pem"
        self.ingress_file = self.project / "ingress-cidrs.txt"
        self.ingress_file.write_text("192.0.2.0/24\n", encoding="utf-8")
        self._write_executable(
            "docker",
            """#!/bin/sh
set -eu
printf '%s\n' "$*" >>"${FAKE_DOCKER_LOG:?}"
case "${1:-}" in
  version)
    test "${FAKE_DOCKER_VERSION_STATUS:-0}" -eq 0 \
      || exit "${FAKE_DOCKER_VERSION_STATUS}"
    printf '%s\n' "${FAKE_DOCKER_VERSION:-}"
    ;;
  compose)
    test "${2:-} ${3:-}" = "version --short" || exit 96
    test "${FAKE_COMPOSE_VERSION_STATUS:-0}" -eq 0 \
      || exit "${FAKE_COMPOSE_VERSION_STATUS}"
    printf '%s\n' "${FAKE_COMPOSE_VERSION:-}"
    ;;
  ps)
    case "$*" in
      *label=platform.kind=jupyter-singleuser*)
        test -z "${FAKE_ACTIVE_SINGLEUSER:-}" \
          || printf '%s\n' "${FAKE_ACTIVE_SINGLEUSER}"
        ;;
    esac
    ;;
  network)
    test "${2:-} ${3:-} ${4:-}" = "ls --quiet --no-trunc" || exit 95
    ;;
  *) exit 97 ;;
esac
""",
        )
        self._write_executable(
            "ip",
            """#!/bin/sh
set -eu
printf '%s\n' "$*" >>"${FAKE_IP_LOG:?}"
if [ "${1:-}" = -j ]; then
  printf '%s\n' '[]'
else
  printf '%s\n' "${FAKE_IP_OUTPUT:-}"
fi
""",
        )
        self.environment = os.environ.copy()
        self.environment.update(
            {
                "PATH": f"{self.fake_bin}{os.pathsep}{self.environment['PATH']}",
                "FAKE_DOCKER_LOG": str(self.docker_log),
                "FAKE_IP_LOG": str(self.ip_log),
                "FAKE_DOCKER_VERSION": "28.0.0",
                "FAKE_DOCKER_VERSION_STATUS": "0",
                "FAKE_COMPOSE_VERSION": "2.24.4",
                "FAKE_COMPOSE_VERSION_STATUS": "0",
                "FAKE_IP_OUTPUT": "",
                "FAKE_ACTIVE_SINGLEUSER": "",
            }
        )
        (self.project / ".env.production").write_text(
            "\n".join(
                (
                    "PRODUCTION_COMPOSE_PROJECT_NAME=test-production",
                    "PLATFORM_GATEWAY_BIND_IP=192.0.2.24",
                    f"PLATFORM_TLS_CERT_FILE={self.cert_file}",
                    f"PLATFORM_TLS_KEY_FILE={self.key_file}",
                    f"PLATFORM_INGRESS_CIDRS_FILE={self.ingress_file}",
                    f"PLATFORM_TLS_GID={os.getgid()}",
                    "",
                )
            ),
            encoding="utf-8",
        )

    def _write_executable(self, name: str, contents: str) -> None:
        path = self.fake_bin / name
        path.write_text(contents, encoding="utf-8")
        path.chmod(0o755)

    def _run_preflight(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.project / "scripts" / "production.sh"), "preflight"],
            cwd=self.project,
            env=self.environment,
            check=False,
            capture_output=True,
            text=True,
        )

    def _generate_certificate(self, sans: tuple[str, ...]) -> None:
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "2",
                "-subj",
                "/CN=cyberailabs.team",
                "-addext",
                "subjectAltName=" + ",".join(f"DNS:{san}" for san in sans),
                "-keyout",
                str(self.key_file),
                "-out",
                str(self.cert_file),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.key_file.chmod(0o600)

    def test_gpu_opt_in_requires_an_absolute_regular_policy_before_docker(self) -> None:
        env_file = self.project / ".env.production"
        env_file.write_text(
            env_file.read_text(encoding="utf-8")
            + "PLATFORM_GPU_RUNTIME_CONFIG_FILE=/missing/gpu-runtime.json\n",
            encoding="utf-8",
        )

        result = self._run_preflight()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GPU runtime policy must be one readable absolute", result.stderr)
        self.assertFalse(self.docker_log.exists())
        self.assertFalse(self.ip_log.exists())

    def test_engine_26_is_rejected_before_any_other_host_or_docker_check(self) -> None:
        self.environment["FAKE_DOCKER_VERSION"] = "26.1.4"

        result = self._run_preflight()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Docker Server Engine 27.1.2 or newer is required", result.stderr)
        self.assertEqual(
            self.docker_log.read_text(encoding="utf-8").splitlines(),
            ["version --format {{.Server.Version}}"],
        )
        self.assertFalse(self.ip_log.exists())

    def test_unavailable_or_unrecognized_engine_version_fails_closed(self) -> None:
        cases = (
            ("28.0.0", "42", "could not query the Docker Server Engine version"),
            ("not-a-version", "0", "returned an unrecognized version"),
            ("28", "0", "returned an unrecognized version"),
            ("28.0", "0", "returned an unrecognized version"),
            ("27.5.1-rc.1", "0", "returned an unrecognized version"),
            ("28.0.0-beta.1", "0", "returned an unrecognized version"),
            ("27.5.1~ubuntu", "0", "returned an unrecognized version"),
        )
        for version, status, expected_error in cases:
            with self.subTest(version=version, status=status):
                self.environment["FAKE_DOCKER_VERSION"] = version
                self.environment["FAKE_DOCKER_VERSION_STATUS"] = status
                self.docker_log.unlink(missing_ok=True)
                self.ip_log.unlink(missing_ok=True)

                result = self._run_preflight()

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected_error, result.stderr)
                self.assertFalse(self.ip_log.exists())

    def test_engine_versions_before_27_1_2_are_rejected(self) -> None:
        for version in ("27.0.0", "27.1.0", "27.1.1"):
            with self.subTest(version=version):
                self.environment["FAKE_DOCKER_VERSION"] = version
                self.docker_log.unlink(missing_ok=True)
                self.ip_log.unlink(missing_ok=True)

                result = self._run_preflight()

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    "Docker Server Engine 27.1.2 or newer is required",
                    result.stderr,
                )
                self.assertFalse(self.ip_log.exists())

    def test_engine_27_1_2_and_newer_vendor_versions_reach_next_host_check(
        self,
    ) -> None:
        for version in (
            "27.1.2",
            "27.1.2-1~ubuntu.24.04~noble",
            "27.5.1",
            "27.5.1-1~ubuntu.24.04~noble",
            "28.5.1",
            "29.1.2-1~ubuntu.24.04~noble",
        ):
            with self.subTest(version=version):
                self.environment["FAKE_DOCKER_VERSION"] = version
                self.environment["FAKE_DOCKER_VERSION_STATUS"] = "0"
                self.docker_log.unlink(missing_ok=True)
                self.ip_log.unlink(missing_ok=True)

                result = self._run_preflight()

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("this host does not own 192.0.2.24", result.stderr)
                self.assertNotIn("or newer is required", result.stderr)
                self.assertTrue(self.ip_log.exists())

    def test_older_supported_engine_27_release_emits_upgrade_warning(self) -> None:
        for version in ("27.1.2", "27.4.99", "27.5.0"):
            with self.subTest(version=version):
                self.environment["FAKE_DOCKER_VERSION"] = version
                self.docker_log.unlink(missing_ok=True)
                self.ip_log.unlink(missing_ok=True)

                result = self._run_preflight()

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    f"WARNING: Docker Server Engine {version} is supported",
                    result.stderr,
                )
                self.assertIn("upgrade to the final patched 27.5.1", result.stderr)

    def test_final_27_release_and_newer_do_not_emit_upgrade_warning(self) -> None:
        for version in ("27.5.1", "28.0.0"):
            with self.subTest(version=version):
                self.environment["FAKE_DOCKER_VERSION"] = version
                self.docker_log.unlink(missing_ok=True)
                self.ip_log.unlink(missing_ok=True)

                result = self._run_preflight()

                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("WARNING: Docker Server Engine", result.stderr)

    def test_engine_27_requires_compose_override_support_before_host_checks(
        self,
    ) -> None:
        self.environment["FAKE_DOCKER_VERSION"] = "27.1.2"
        self.environment["FAKE_COMPOSE_VERSION"] = "2.24.3"

        result = self._run_preflight()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Docker Compose 2.24.4 or newer is required", result.stderr)
        self.assertFalse(self.ip_log.exists())

    def test_engine_27_rejects_prerelease_compose_version(self) -> None:
        self.environment["FAKE_DOCKER_VERSION"] = "27.1.2"
        self.environment["FAKE_COMPOSE_VERSION"] = "2.24.4-rc.1"

        result = self._run_preflight()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Docker Compose returned an unrecognized version", result.stderr)
        self.assertFalse(self.ip_log.exists())

    def test_gateway_bind_ip_rejects_noncanonical_and_unsafe_addresses(self) -> None:
        env_file = self.project / ".env.production"
        original = env_file.read_text(encoding="utf-8")
        for address in (
            "REPLACE_WITH_INTERNAL_SERVER_IP",
            "0.0.0.0",
            "127.0.0.1",
            "224.0.0.1",
            "192.000.002.024",
            "999.1.1.1",
            "2001:db8::1",
        ):
            with self.subTest(address=address):
                env_file.write_text(
                    original.replace("192.0.2.24", address), encoding="utf-8"
                )
                self.ip_log.unlink(missing_ok=True)

                result = self._run_preflight()

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    "PLATFORM_GATEWAY_BIND_IP must be a canonical IPv4 address",
                    result.stderr,
                )
                self.assertFalse(self.ip_log.exists())

    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL is unavailable")
    def test_apex_and_wildcard_certificate_covers_portal_without_explicit_san(
        self,
    ) -> None:
        self._generate_certificate(("cyberailabs.team", "*.cyberailabs.team"))
        self.environment["FAKE_IP_OUTPUT"] = (
            "2: eth0    inet 192.0.2.24/24 brd 192.0.2.255 scope global eth0"
        )
        self.environment["FAKE_ACTIVE_SINGLEUSER"] = "active-workspace"

        result = self._run_preflight()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "stop every workspace before production deployment", result.stderr
        )
        self.assertNotIn("TLS certificate", result.stderr)

    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL is unavailable")
    def test_explicit_portal_san_does_not_replace_required_wildcard(self) -> None:
        self._generate_certificate(("cyberailabs.team", "platform.cyberailabs.team"))
        self.environment["FAKE_IP_OUTPUT"] = (
            "2: eth0    inet 192.0.2.24/24 brd 192.0.2.255 scope global eth0"
        )

        result = self._run_preflight()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "TLS certificate is missing SAN DNS:*.cyberailabs.team", result.stderr
        )

    def test_portal_hostname_uses_openssl_hostname_validation(self) -> None:
        script = PRODUCTION_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("-checkhost platform.cyberailabs.team", script)
        self.assertNotIn("'DNS:platform.cyberailabs.team'", script)


class ProductionSnapshotFailurePropagationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.project = Path(self.temporary_directory.name)
        scripts_directory = self.project / "scripts"
        scripts_directory.mkdir()

        script = PRODUCTION_SCRIPT.read_text(encoding="utf-8")
        dispatch_marker = '\ncase "${1:-}" in\n'
        self.assertIn(dispatch_marker, script)
        function_definitions = script.split(dispatch_marker, maxsplit=1)[0]
        self.production_functions = scripts_directory / "production-functions.sh"
        self.production_functions.write_text(
            function_definitions + "\n", encoding="utf-8"
        )

        self.fake_bin = self.project / "fake-bin"
        self.fake_bin.mkdir()
        self.docker_log = self.project / "docker.log"
        self.python_log = self.project / "python.log"
        self.bundle = self.project / "prepared-backup"
        self._write_executable(
            "docker",
            """#!/bin/sh
set -eu
printf '%s\n' "$*" >>"${FAKE_DOCKER_LOG:?}"
if [ "${1:-}" = volume ] && [ "${2:-}" = inspect ]; then
  exit 0
fi
if [ "${1:-}" = run ]; then
  case " $* " in
    *" --source /source/platform.db "*)
      [ "${FAKE_FAILURE_STAGE:-}" != platform ] || exit 52
      ;;
    *" --source /source/jupyterhub.sqlite "*)
      [ "${FAKE_FAILURE_STAGE:-}" != hub ] || exit 53
      ;;
    *) exit 91 ;;
  esac
  exit 0
fi
exit 92
""",
        )
        self._write_executable(
            "python3",
            """#!/bin/sh
set -eu
printf '%s\n' "$*" >>"${FAKE_PYTHON_LOG:?}"
case " $* " in
  *"domain_test_database_snapshot.py prepare "*)
    [ "${FAKE_FAILURE_STAGE:-}" != prepare ] || exit 51
    printf '%s\n' "${FAKE_BUNDLE:?}"
    ;;
  *"domain_test_database_snapshot.py finalize "*)
    [ "${FAKE_FAILURE_STAGE:-}" != finalize ] || exit 54
    ;;
  *) exit 93 ;;
esac
""",
        )
        self.environment = os.environ.copy()
        self.environment.update(
            {
                "PATH": f"{self.fake_bin}{os.pathsep}{self.environment['PATH']}",
                "FAKE_DOCKER_LOG": str(self.docker_log),
                "FAKE_PYTHON_LOG": str(self.python_log),
                "FAKE_BUNDLE": str(self.bundle),
                "PRODUCTION_COMPOSE_PROJECT_NAME": "snapshot-test",
            }
        )

    def _write_executable(self, name: str, contents: str) -> None:
        path = self.fake_bin / name
        path.write_text(contents, encoding="utf-8")
        path.chmod(0o755)

    def _run_snapshot(self, failure_stage: str) -> subprocess.CompletedProcess[str]:
        environment = self.environment.copy()
        environment["FAKE_FAILURE_STAGE"] = failure_stage
        return subprocess.run(
            [
                "bash",
                "-c",
                """
source "$1"
unset PRODUCTION_LAST_BACKUP
snapshot_status=0
snapshot_existing_databases || snapshot_status=$?
printf 'snapshot_status=%s\n' "${snapshot_status}"
if [[ -v PRODUCTION_LAST_BACKUP ]]; then
  printf 'backup_state=set:%s\n' "${PRODUCTION_LAST_BACKUP}"
else
  printf 'backup_state=unset\n'
fi
""",
                "snapshot-failure-test",
                str(self.production_functions),
            ],
            cwd=self.project,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_each_snapshot_stage_failure_is_propagated_without_backup_state(
        self,
    ) -> None:
        cases = {
            "prepare": "could not prepare the database backup bundle",
            "platform": "Platform database snapshot failed",
            "hub": "JupyterHub database snapshot failed",
            "finalize": "database backup finalization failed",
        }
        for failure_stage, expected_error in cases.items():
            with self.subTest(failure_stage=failure_stage):
                self.docker_log.unlink(missing_ok=True)
                self.python_log.unlink(missing_ok=True)

                result = self._run_snapshot(failure_stage)

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("snapshot_status=1", result.stdout)
                self.assertIn("backup_state=unset", result.stdout)
                self.assertNotIn("backup_state=set:", result.stdout)
                self.assertIn(expected_error, result.stderr)

                docker_calls = self.docker_log.read_text(encoding="utf-8").splitlines()
                python_calls = self.python_log.read_text(encoding="utf-8").splitlines()
                self.assertEqual(
                    sum(call.startswith("volume inspect ") for call in docker_calls),
                    2,
                )
                platform_snapshots = sum(
                    "--source /source/platform.db" in call for call in docker_calls
                )
                hub_snapshots = sum(
                    "--source /source/jupyterhub.sqlite" in call
                    for call in docker_calls
                )
                finalize_calls = sum(" finalize " in call for call in python_calls)
                expected_calls = {
                    "prepare": (0, 0, 0),
                    "platform": (1, 0, 0),
                    "hub": (1, 1, 0),
                    "finalize": (1, 1, 1),
                }
                self.assertEqual(
                    (platform_snapshots, hub_snapshots, finalize_calls),
                    expected_calls[failure_stage],
                )


class ProductionAccountBackupGuardTests(unittest.TestCase):
    container_id = "b" * 64

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.project = Path(self.temporary_directory.name)
        scripts_directory = self.project / "scripts"
        scripts_directory.mkdir()

        script = PRODUCTION_SCRIPT.read_text(encoding="utf-8")
        dispatch_marker = '\ncase "${1:-}" in\n'
        self.assertIn(dispatch_marker, script)
        function_definitions = script.split(dispatch_marker, maxsplit=1)[0]
        self.production_functions = scripts_directory / "production-functions.sh"
        self.production_functions.write_text(
            function_definitions + "\n", encoding="utf-8"
        )

        self.docker_log = self.project / "docker.log"
        self.compose_log = self.project / "compose.log"
        self.python_log = self.project / "python.log"
        self.operator_execution_marker = self.project / "operator-executed"
        self.recovery_log = self.project / "recovery.log"
        self.environment = os.environ.copy()
        self.environment.update(
            {
                "FAKE_CONTAINER_ID": self.container_id,
                "FAKE_DOCKER_LOG": str(self.docker_log),
                "FAKE_COMPOSE_LOG": str(self.compose_log),
                "FAKE_PYTHON_LOG": str(self.python_log),
                "FAKE_OPERATOR_EXECUTION_MARKER": str(self.operator_execution_marker),
                "FAKE_RECOVERY_LOG": str(self.recovery_log),
            }
        )

    def _run_account_command(self, action: str) -> subprocess.CompletedProcess[str]:
        for path in (
            self.docker_log,
            self.compose_log,
            self.python_log,
            self.operator_execution_marker,
            self.recovery_log,
        ):
            path.unlink(missing_ok=True)
        return subprocess.run(
            [
                "bash",
                "-c",
                r"""
source "$1"
acquire_operator_lock() { :; }
load_environment() {
  PRODUCTION_COMPOSE_PROJECT_NAME=account-guard-test
  PLATFORM_ADMIN_USERNAME=platform-admin
  PRODUCTION_REQUIRE_EMPTY=true
  unset PRODUCTION_TARGET_USERNAME PRODUCTION_FRESH_DATABASES
  PRODUCTION_LAST_BACKUP=/stale/previous-backup
}
validate_host_contract() { :; }
require_idle() { :; }
validate_database_inventory() { :; }
production_network_contract() { :; }
production_database_is_idle() { :; }
running_healthy_service_id() { printf '%s\n' "${FAKE_CONTAINER_ID:?}"; }
stop_existing_container_checked() { :; }
restart_account_control_plane_checked() {
  printf 'restarted:%s\n' "$*" >>"${FAKE_RECOVERY_LOG:?}"
}
docker() {
  printf '%s\n' "$*" >>"${FAKE_DOCKER_LOG:?}"
  if [[ "${1:-}" == volume && "${2:-}" == inspect ]]; then
    return 1
  fi
  return 90
}
python3() {
  printf '%s\n' "$*" >>"${FAKE_PYTHON_LOG:?}"
  return 91
}
compose() {
  printf '%s\n' "$*" >>"${FAKE_COMPOSE_LOG:?}"
  case "$*" in
    "ps --status running -q jupyterhub")
      printf '%s\n' "${FAKE_CONTAINER_ID:?}"
      ;;
    "--profile operator build native-user-admin")
      ;;
    "--profile operator run --rm --no-deps native-user-admin "*)
      : >"${FAKE_OPERATOR_EXECUTION_MARKER:?}"
      return 92
      ;;
    *)
      return 93
      ;;
  esac
}
create_user "$2"
""",
                "account-backup-guard-test",
                str(self.production_functions),
                action,
            ],
            cwd=self.project,
            env=self.environment,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_create_and_reset_never_run_operator_when_volumes_look_fresh(
        self,
    ) -> None:
        for action in ("create", "reset-admin-password"):
            with self.subTest(action=action):
                result = self._run_account_command(action)

                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(
                    "account administration requires an existing verified database backup",
                    result.stderr,
                )
                self.assertIn(
                    "original control plane was restored after the account command failed",
                    result.stderr,
                )
                self.assertNotIn("unbound variable", result.stderr)
                self.assertIn(
                    "production: new database volumes will be initialized",
                    result.stdout,
                )
                self.assertEqual(
                    self.docker_log.read_text(encoding="utf-8").splitlines(),
                    [
                        "volume inspect account-guard-test_platform_data",
                        "volume inspect account-guard-test_jupyterhub_data",
                    ],
                )
                compose_calls = self.compose_log.read_text(
                    encoding="utf-8"
                ).splitlines()
                self.assertIn(
                    "--profile operator build native-user-admin", compose_calls
                )
                self.assertFalse(
                    any(
                        call.startswith(
                            "--profile operator run --rm --no-deps native-user-admin"
                        )
                        for call in compose_calls
                    )
                )
                self.assertFalse(self.operator_execution_marker.exists())
                self.assertFalse(self.python_log.exists())
                self.assertEqual(
                    self.recovery_log.read_text(encoding="utf-8").splitlines(),
                    ["restarted:" + " ".join((self.container_id,) * 5)],
                )

    def test_account_backup_guard_precedes_verification_and_operator_run(
        self,
    ) -> None:
        script = PRODUCTION_SCRIPT.read_text(encoding="utf-8")
        create_user_start = script.index("\ncreate_user() {")
        create_user_end = script.index("\noffline_quiesce() {", create_user_start)
        create_user = script[create_user_start:create_user_end]
        snapshot = create_user.index("snapshot_existing_databases")
        verified_backup_guard = create_user.index(
            '[[ "${PRODUCTION_FRESH_DATABASES}" == false '
            '&& -n "${PRODUCTION_LAST_BACKUP:-}" ]]'
        )
        backup_verification = create_user.index(
            "domain_test_database_snapshot.py verify"
        )
        operator_run = create_user.index(
            "compose --profile operator run --rm --no-deps native-user-admin"
        )

        self.assertLess(snapshot, verified_backup_guard)
        self.assertLess(verified_backup_guard, backup_verification)
        self.assertLess(backup_verification, operator_run)
        self.assertIn("\n    create)", create_user)
        self.assertIn("\n    reset-admin-password)", create_user)


class ProductionContainerRecoveryHelperTests(unittest.TestCase):
    container_id = "a" * 64

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.project = Path(self.temporary_directory.name)
        scripts_directory = self.project / "scripts"
        scripts_directory.mkdir()

        script = PRODUCTION_SCRIPT.read_text(encoding="utf-8")
        dispatch_marker = '\ncase "${1:-}" in\n'
        self.assertIn(dispatch_marker, script)
        function_definitions = script.split(dispatch_marker, maxsplit=1)[0]
        self.production_functions = scripts_directory / "production-functions.sh"
        self.production_functions.write_text(
            function_definitions + "\n", encoding="utf-8"
        )

        self.fake_bin = self.project / "fake-bin"
        self.fake_bin.mkdir()
        self.docker_log = self.project / "docker.log"
        self.sleep_log = self.project / "sleep.log"
        self.state_file = self.project / "container.state"
        self.health_file = self.project / "container.health"
        self.health_inspections_file = self.project / "health-inspections"
        self._write_executable(
            "docker",
            """#!/bin/sh
set -eu
printf '%s\n' "$*" >>"${FAKE_DOCKER_LOG:?}"
case "${1:-}" in
  inspect)
    [ "${2:-}" = --format ] || exit 91
    [ "${4:-}" = "${FAKE_CONTAINER_ID:?}" ] || exit 92
    state="$(cat "${FAKE_STATE_FILE:?}")"
    health="$(cat "${FAKE_HEALTH_FILE:?}")"
    case "${3:-}" in
      *State.Running*)
        if [ "${state}" = running ]; then
          printf 'true\n'
        else
          printf 'false\n'
        fi
        ;;
      *State.Health*)
        inspections="$(cat "${FAKE_HEALTH_INSPECTIONS_FILE:?}")"
        inspections=$((inspections + 1))
        printf '%s\n' "${inspections}" >"${FAKE_HEALTH_INSPECTIONS_FILE}"
        if [ "${state}" = running ] \
          && [ "${health}" = starting ] \
          && [ "${inspections}" -ge "${FAKE_HEALTHY_AFTER_INSPECTIONS:-1}" ]; then
          health=healthy
          printf '%s\n' "${health}" >"${FAKE_HEALTH_FILE}"
        fi
        printf '%s|%s\n' "${state}" "${health}"
        ;;
      *State.Status*)
        printf '%s\n' "${state}"
        ;;
      *) exit 93 ;;
    esac
    ;;
  start)
    [ "$#" -eq 2 ] || exit 94
    [ "${2:-}" = "${FAKE_CONTAINER_ID:?}" ] || exit 95
    printf 'running\n' >"${FAKE_STATE_FILE:?}"
    printf '%s\n' "${FAKE_HEALTH_AFTER_START:-healthy}" \
      >"${FAKE_HEALTH_FILE:?}"
    printf '0\n' >"${FAKE_HEALTH_INSPECTIONS_FILE:?}"
    printf '%s\n' "${FAKE_CONTAINER_ID}"
    ;;
  stop)
    [ "$#" -eq 2 ] || exit 96
    [ "${2:-}" = "${FAKE_CONTAINER_ID:?}" ] || exit 97
    printf 'exited\n' >"${FAKE_STATE_FILE:?}"
    printf 'none\n' >"${FAKE_HEALTH_FILE:?}"
    printf '%s\n' "${FAKE_CONTAINER_ID}"
    ;;
  *) exit 98 ;;
esac
""",
        )
        self._write_executable(
            "sleep",
            """#!/bin/sh
set -eu
printf '%s\n' "$*" >>"${FAKE_SLEEP_LOG:?}"
""",
        )
        self.environment = os.environ.copy()
        self.environment.update(
            {
                "PATH": f"{self.fake_bin}{os.pathsep}{self.environment['PATH']}",
                "FAKE_CONTAINER_ID": self.container_id,
                "FAKE_DOCKER_LOG": str(self.docker_log),
                "FAKE_SLEEP_LOG": str(self.sleep_log),
                "FAKE_STATE_FILE": str(self.state_file),
                "FAKE_HEALTH_FILE": str(self.health_file),
                "FAKE_HEALTH_INSPECTIONS_FILE": str(self.health_inspections_file),
                "FAKE_HEALTH_AFTER_START": "starting",
                "FAKE_HEALTHY_AFTER_INSPECTIONS": "2",
            }
        )

    def _write_executable(self, name: str, contents: str) -> None:
        path = self.fake_bin / name
        path.write_text(contents, encoding="utf-8")
        path.chmod(0o755)

    def _run_helper(
        self,
        commands: str,
        *,
        state: str,
        health: str,
    ) -> subprocess.CompletedProcess[str]:
        self.state_file.write_text(state + "\n", encoding="utf-8")
        self.health_file.write_text(health + "\n", encoding="utf-8")
        self.health_inspections_file.write_text("0\n", encoding="utf-8")
        self.docker_log.unlink(missing_ok=True)
        self.sleep_log.unlink(missing_ok=True)
        return subprocess.run(
            [
                "bash",
                "-c",
                'source "$1"\n' + commands,
                "container-recovery-helper-test",
                str(self.production_functions),
            ],
            cwd=self.project,
            env=self.environment,
            check=False,
            capture_output=True,
            text=True,
        )

    def _docker_calls(self) -> list[str]:
        if not self.docker_log.exists():
            return []
        return self.docker_log.read_text(encoding="utf-8").splitlines()

    def test_start_running_healthy_container_is_already_successful(self) -> None:
        result = self._run_helper(
            'start_existing_container_checked api "${FAKE_CONTAINER_ID}"',
            state="running",
            health="healthy",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(
            any(call.startswith("start ") for call in self._docker_calls())
        )
        self.assertFalse(self.sleep_log.exists())

    def test_start_exited_container_uses_exact_id_and_waits_for_health(self) -> None:
        result = self._run_helper(
            'start_existing_container_checked jupyterhub "${FAKE_CONTAINER_ID}"',
            state="exited",
            health="none",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [call for call in self._docker_calls() if call.startswith("start ")],
            [f"start {self.container_id}"],
        )
        self.assertEqual(self.sleep_log.read_text(encoding="utf-8").splitlines(), ["2"])
        self.assertEqual(self.state_file.read_text(encoding="utf-8").strip(), "running")
        self.assertEqual(
            self.health_file.read_text(encoding="utf-8").strip(), "healthy"
        )

    def test_start_rejects_unhealthy_and_invalid_container_states(self) -> None:
        cases = (
            ("running", "unhealthy", "failed while starting"),
            ("paused", "healthy", "cannot be started from state paused"),
        )
        for state, health, expected_error in cases:
            with self.subTest(state=state, health=health):
                result = self._run_helper(
                    'start_existing_container_checked worker "${FAKE_CONTAINER_ID}"',
                    state=state,
                    health=health,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected_error, result.stderr)
                self.assertFalse(
                    any(call.startswith("start ") for call in self._docker_calls())
                )
                self.assertFalse(self.sleep_log.exists())

    def test_stop_already_stopped_container_is_already_successful(self) -> None:
        result = self._run_helper(
            'stop_existing_container_checked gateway "${FAKE_CONTAINER_ID}"',
            state="exited",
            health="none",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(call.startswith("stop ") for call in self._docker_calls()))

    def test_stop_running_container_uses_exact_id_and_verifies_stopped(self) -> None:
        result = self._run_helper(
            'stop_existing_container_checked gateway "${FAKE_CONTAINER_ID}"',
            state="running",
            health="healthy",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [call for call in self._docker_calls() if call.startswith("stop ")],
            [f"stop {self.container_id}"],
        )
        self.assertEqual(self.state_file.read_text(encoding="utf-8").strip(), "exited")

    def test_repeated_start_and_stop_recovery_is_idempotent(self) -> None:
        result = self._run_helper(
            "\n".join(
                (
                    'start_existing_container_checked api "${FAKE_CONTAINER_ID}"',
                    'start_existing_container_checked api "${FAKE_CONTAINER_ID}"',
                    'stop_existing_container_checked api "${FAKE_CONTAINER_ID}"',
                    'stop_existing_container_checked api "${FAKE_CONTAINER_ID}"',
                )
            ),
            state="exited",
            health="none",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        docker_calls = self._docker_calls()
        self.assertEqual(
            [call for call in docker_calls if call.startswith("start ")],
            [f"start {self.container_id}"],
        )
        self.assertEqual(
            [call for call in docker_calls if call.startswith("stop ")],
            [f"stop {self.container_id}"],
        )
        self.assertEqual(self.state_file.read_text(encoding="utf-8").strip(), "exited")


class ProductionGpuOrchestrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = PRODUCTION_SCRIPT.read_text(encoding="utf-8")

    def test_gpu_is_explicit_opt_in_and_validated_uuid_pool_is_exported(self) -> None:
        load_environment = self.script.split("load_environment() {", 1)[1].split(
            "\n}\n\ncompose()", 1
        )[0]

        self.assertIn(
            '"${PLATFORM_GPU_RUNTIME_CONFIG_FILE:-disabled}"', load_environment
        )
        self.assertIn("--print-device-ids", load_environment)
        self.assertIn("require_regular_file", load_environment)
        self.assertIn("export PLATFORM_NVIDIA_GPU_DEVICE_IDS", load_environment)
        self.assertIn("PLATFORM_NVIDIA_GPU_COUNT", load_environment)
        self.assertNotIn("NVIDIA_VISIBLE_DEVICES", load_environment)

    def test_cuda_runtime_probe_precedes_policy_generation_and_db_mutation(
        self,
    ) -> None:
        prepare = self.script.split("prepare_images_and_policy() {", 1)[1].split(
            "\n}\n\ndatabase_volume_names()", 1
        )[0]
        start_offset = self.script.index("\nstart() {")
        start = self.script[
            start_offset : self.script.index("\nstop() {", start_offset)
        ]

        cpu_build = prepare.index("compose build singleuser-image")
        cuda_build = prepare.index("docker build \\")
        cuda_probe = prepare.index("infra/host/check_gpu_runtime.py", cuda_build)
        generate = prepare.index("generate_production_profile_policy.py", cuda_probe)
        image_check = prepare.index("profile_image_check.py", generate)
        self.assertLess(cpu_build, cuda_build)
        self.assertLess(cuda_build, cuda_probe)
        self.assertLess(cuda_probe, generate)
        self.assertLess(generate, image_check)
        self.assertIn('cpu_image_hex="${image_id#sha256:}"', prepare)
        self.assertIn('[[ "${gpu_image_id}" =~ ^sha256:', prepare)
        self.assertIn("io.team-workspace.cpu-base.image-id", prepare)
        self.assertIn('--gpu-image-id "${gpu_image_id}"', prepare)
        self.assertIn('--gpu-count "${PLATFORM_NVIDIA_GPU_COUNT}"', prepare)
        self.assertIn(
            '--nvidia-gpu-device-ids "${PLATFORM_NVIDIA_GPU_DEVICE_IDS}"',
            prepare,
        )

        preflight_offset = self.script.index("preflight_impl() {")
        self.assertLess(
            self.script.index("prepare_images_and_policy", preflight_offset),
            self.script.index("production_database_is_idle", preflight_offset),
        )
        self.assertLess(
            start.index("preflight_impl"), start.index("migration_started=true")
        )


if __name__ == "__main__":
    unittest.main()
