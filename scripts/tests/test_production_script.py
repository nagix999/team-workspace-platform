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
  *) exit 97 ;;
esac
""",
        )
        self._write_executable(
            "ip",
            """#!/bin/sh
set -eu
printf '%s\n' "$*" >>"${FAKE_IP_LOG:?}"
printf '%s\n' "${FAKE_IP_OUTPUT:-}"
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
                    "PLATFORM_GATEWAY_BIND_IP=10.155.1.24",
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
                self.assertIn("this host does not own 10.155.1.24", result.stderr)
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

    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL is unavailable")
    def test_apex_and_wildcard_certificate_covers_portal_without_explicit_san(
        self,
    ) -> None:
        self._generate_certificate(("cyberailabs.team", "*.cyberailabs.team"))
        self.environment["FAKE_IP_OUTPUT"] = (
            "2: eth0    inet 10.155.1.24/24 brd 10.155.1.255 scope global eth0"
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
            "2: eth0    inet 10.155.1.24/24 brd 10.155.1.255 scope global eth0"
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


if __name__ == "__main__":
    unittest.main()
