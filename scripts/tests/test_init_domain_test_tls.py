#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest


PROJECT_DIR = Path(__file__).resolve().parents[2]
SCRIPT = PROJECT_DIR / "scripts" / "init-domain-test-tls.sh"
EXPECTED_SANS = [
    "DNS:platform.workspace.test",
    "DNS:hub.workspace.test",
    "DNS:*.hub.workspace.test",
]


def run_script(
    tls_dir: Path, group: str | None = None
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["DOMAIN_TEST_TLS_DIR"] = str(tls_dir)
    environment["DOMAIN_TEST_TLS_GROUP"] = (
        group if group is not None else str(os.getgid())
    )
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=PROJECT_DIR,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def snapshot(paths: list[Path]) -> dict[str, tuple[str, int, int]]:
    result: dict[str, tuple[str, int, int]] = {}
    for path in paths:
        metadata = path.stat()
        result[path.name] = (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            stat.S_IMODE(metadata.st_mode),
            metadata.st_gid,
        )
    return result


@unittest.skipUnless(
    shutil.which("openssl") and shutil.which("getent"),
    "OpenSSL and getent are required",
)
class InitDomainTestTlsTest(unittest.TestCase):
    def test_generation_idempotence_and_mismatch_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            tls_dir = Path(temporary_directory) / "domain-test"
            first = run_script(tls_dir)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn("Domain-test TLS material: created", first.stdout)
            ca_export = f"export DOMAIN_TEST_CA_CERT_FILE={tls_dir / 'ca.crt'}"
            self.assertEqual(first.stdout.count(ca_export), 1)
            self.assertIn(f"export DOMAIN_TEST_TLS_GID={os.getgid()}", first.stdout)
            self.assertIn("did not modify any OS or browser trust store", first.stdout)
            self.assertNotIn("PRIVATE KEY-----", first.stdout + first.stderr)

            ca_key = tls_dir / "ca.key"
            ca_cert = tls_dir / "ca.crt"
            tls_key = tls_dir / "tls.key"
            tls_cert = tls_dir / "tls.crt"
            paths = [ca_key, ca_cert, tls_key, tls_cert]
            for path in paths:
                self.assertTrue(path.is_file(), path)
                self.assertFalse(path.is_symlink(), path)

            self.assertEqual(stat.S_IMODE(tls_dir.stat().st_mode), 0o750)
            self.assertEqual(stat.S_IMODE(ca_key.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(tls_key.stat().st_mode), 0o640)
            self.assertEqual(stat.S_IMODE(ca_cert.stat().st_mode), 0o644)
            self.assertEqual(stat.S_IMODE(tls_cert.stat().st_mode), 0o644)
            self.assertEqual({path.stat().st_gid for path in paths}, {os.getgid()})

            san_result = subprocess.run(
                [
                    "openssl",
                    "x509",
                    "-in",
                    str(tls_cert),
                    "-noout",
                    "-ext",
                    "subjectAltName",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            san_items = [
                item.strip()
                for line in san_result.stdout.splitlines()[1:]
                for item in line.split(",")
                if item.strip()
            ]
            self.assertEqual(san_items, EXPECTED_SANS)
            self.assertEqual(
                tls_cert.read_text(encoding="ascii").count(
                    "-----BEGIN CERTIFICATE-----"
                ),
                2,
            )
            subprocess.run(
                [
                    "openssl",
                    "verify",
                    "-purpose",
                    "sslserver",
                    "-CAfile",
                    str(ca_cert),
                    str(tls_cert),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )

            initial_snapshot = snapshot(paths)
            second = run_script(tls_dir)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("Domain-test TLS material: validated existing", second.stdout)
            self.assertEqual(snapshot(paths), initial_snapshot)

            tls_cert.write_bytes(ca_cert.read_bytes())
            broken_snapshot = snapshot(paths)
            mismatch = run_script(tls_dir)
            self.assertNotEqual(mismatch.returncode, 0)
            self.assertIn("refusing to overwrite", mismatch.stderr)
            self.assertEqual(snapshot(paths), broken_snapshot)

    def test_partial_material_is_not_filled_in(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            tls_dir = Path(temporary_directory) / "domain-test"
            tls_dir.mkdir()
            existing_key = tls_dir / "tls.key"
            existing_key.write_bytes(b"do-not-overwrite\n")
            before = existing_key.read_bytes()

            result = run_script(tls_dir)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("refusing to create or overwrite", result.stderr)
            self.assertEqual(existing_key.read_bytes(), before)
            self.assertEqual(
                sorted(path.name for path in tls_dir.iterdir()), ["tls.key"]
            )

    def test_invalid_group_fails_before_creating_material(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            tls_dir = Path(temporary_directory) / "domain-test"

            result = run_script(tls_dir, "-not-a-group")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                "must be an existing group name or numeric GID", result.stderr
            )
            self.assertFalse(tls_dir.exists())

    def test_script_contains_no_trust_store_mutation_commands(self) -> None:
        script_text = SCRIPT.read_text(encoding="utf-8")
        for forbidden_command in (
            "update-ca-certificates",
            "trust anchor",
            "certutil -A",
            "security add-trusted-cert",
            "/etc/ssl/certs",
            "/etc/hosts",
        ):
            self.assertNotIn(forbidden_command, script_text)


if __name__ == "__main__":
    unittest.main()
