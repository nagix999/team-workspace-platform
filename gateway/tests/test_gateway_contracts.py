from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
GATEWAY = ROOT / "gateway"


def docker_daemon_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        result = subprocess.run(
            ["docker", "info"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DomainTestComposeContractTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("docker"), "Docker Compose CLI is unavailable")
    def test_rendered_overlay_is_loopback_tls_only(self) -> None:
        environment = os.environ.copy()
        environment.update(
            {
                "DOMAIN_TEST_TLS_CERT_FILE": "/dev/null",
                "DOMAIN_TEST_TLS_KEY_FILE": "/dev/null",
                "DOMAIN_TEST_TLS_GID": "1000",
                "DOCKER_GID": environment.get("DOCKER_GID", "999"),
                "PLATFORM_SECRET_GID": environment.get("PLATFORM_SECRET_GID", "1000"),
            }
        )
        result = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                "compose.yaml",
                "-f",
                "compose.domain-test.yaml",
                "config",
                "--format",
                "json",
            ],
            cwd=ROOT,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        config = json.loads(result.stdout)
        gateway = config["services"]["gateway"]
        self.assertEqual(
            gateway["ports"],
            [
                {
                    "mode": "ingress",
                    "host_ip": "127.0.0.1",
                    "target": 3030,
                    "published": "443",
                    "protocol": "tcp",
                }
            ],
        )
        self.assertEqual(gateway["environment"]["PLATFORM_GATEWAY_MODE"], "domain-test")
        self.assertEqual(gateway["networks"]["edge"]["ipv4_address"], "172.28.0.10")
        self.assertEqual(
            config["services"]["api"]["environment"]["FORWARDED_ALLOW_IPS"],
            "172.28.0.10",
        )
        self.assertEqual(
            config["services"]["api"]["environment"]["PLATFORM_DOMAIN_TEST"], "true"
        )
        self.assertEqual(
            config["services"]["jupyterhub"]["environment"]["PLATFORM_ENV"],
            "domain-test",
        )
        for mount in gateway["volumes"]:
            self.assertTrue(mount["read_only"])
            self.assertIsNot(mount.get("bind", {}).get("create_host_path"), True)

        reconciler = config["services"]["reconciler"]
        self.assertEqual(reconciler["networks"], {"control": None})
        self.assertNotIn("ports", reconciler)
        self.assertEqual(
            reconciler["environment"],
            {
                "JUPYTERHUB_INTERNAL_URL": "http://jupyterhub:8081",
                "JUPYTERHUB_RECONCILER_TOKEN_FILE": (
                    "/run/platform-secrets/reconciler_token"
                ),
                "PLATFORM_DATABASE_URL": ("sqlite:////var/lib/platform/platform.db"),
                "PLATFORM_ENFORCE_SAFE_SQLITE": "false",
                "PLATFORM_RECONCILIATION_FRESHNESS_SECONDS": "30",
                "PLATFORM_RECONCILIATION_INTERVAL_SECONDS": "5",
            },
        )
        self.assertEqual(
            {
                (mount["target"], mount.get("read_only", False))
                for mount in reconciler["volumes"]
            },
            {
                ("/var/lib/platform", False),
                ("/run/platform-secrets/reconciler_token", True),
            },
        )
        self.assertEqual(reconciler["cap_drop"], ["ALL"])
        self.assertTrue(reconciler["read_only"])

    @unittest.skipUnless(shutil.which("docker"), "Docker Compose CLI is unavailable")
    def test_production_compose_is_standalone_and_exactly_published(self) -> None:
        environment = os.environ.copy()
        environment.update(
            {
                "PLATFORM_GATEWAY_BIND_IP": "192.0.2.24",
                "PLATFORM_TLS_CERT_FILE": "/tmp/fullchain.pem",
                "PLATFORM_TLS_KEY_FILE": "/tmp/privkey.pem",
                "PLATFORM_INGRESS_CIDRS_FILE": "/tmp/ingress-cidrs.txt",
                "PLATFORM_TLS_GID": "1000",
                "DOCKER_GID": "999",
                "PLATFORM_SECRET_GID": "1000",
                "PLATFORM_NVIDIA_GPU_DEVICE_ID": (
                    "GPU-01234567-89ab-cdef-0123-456789abcdef"
                ),
            }
        )
        result = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                "compose.production.yaml",
                "--profile",
                "operator",
                "config",
                "--format",
                "json",
            ],
            cwd=ROOT,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        config = json.loads(result.stdout)
        gateway = config["services"]["gateway"]
        self.assertEqual(
            gateway["ports"],
            [
                {
                    "mode": "ingress",
                    "host_ip": "192.0.2.24",
                    "target": 3030,
                    "published": "3030",
                    "protocol": "tcp",
                }
            ],
        )
        self.assertEqual(
            gateway["environment"]["PLATFORM_HUB_HOST"], "cyberailabs.team"
        )
        self.assertEqual(
            gateway["environment"]["PLATFORM_PORTAL_HOST"],
            "platform.cyberailabs.team",
        )
        api = config["services"]["api"]["environment"]
        self.assertEqual(api["PLATFORM_INSECURE_LOCAL_DEV"], "false")
        self.assertEqual(api["PLATFORM_DOMAIN_TEST"], "false")
        self.assertEqual(api["PLATFORM_ALLOW_SAME_SITE_USER_CONTENT"], "true")
        self.assertEqual(api["FORWARDED_ALLOW_IPS"], "172.29.3.10")
        for service in ("migrate", "bootstrap-profile", "api", "worker"):
            self.assertEqual(
                config["services"][service]["environment"][
                    "PLATFORM_NVIDIA_GPU_DEVICE_IDS"
                ],
                "GPU-01234567-89ab-cdef-0123-456789abcdef",
            )
        hub = config["services"]["jupyterhub"]["environment"]
        self.assertEqual(hub["PLATFORM_ENV"], "production")
        self.assertEqual(hub["JUPYTERHUB_SUBDOMAIN_HOST"], "https://cyberailabs.team")
        self.assertEqual(
            hub["PLATFORM_PRODUCTION_DOCKER_VOLUME_PROVISIONING_ENABLED"],
            "true",
        )
        self.assertEqual(
            hub["JUPYTERHUB_NVIDIA_GPU_DEVICE_ID"],
            "GPU-01234567-89ab-cdef-0123-456789abcdef",
        )
        published_services = {
            name for name, service in config["services"].items() if service.get("ports")
        }
        self.assertEqual(published_services, {"gateway"})

        maintenance = config["services"]["offline-maintenance"]
        self.assertEqual(maintenance["profiles"], ["operator"])
        self.assertEqual(
            maintenance["entrypoint"], ["python", "-m", "app.offline_maintenance"]
        )
        self.assertEqual(maintenance["command"], ["quiesce-stopped-intent"])
        self.assertEqual(maintenance["network_mode"], "none")
        self.assertNotIn("ports", maintenance)
        self.assertTrue(maintenance["read_only"])
        self.assertEqual(maintenance["cap_drop"], ["ALL"])
        self.assertEqual(
            {
                (mount["target"], mount.get("read_only", False))
                for mount in maintenance["volumes"]
            },
            {("/var/lib/platform", False)},
        )

        native_admin = config["services"]["native-user-admin"]
        self.assertEqual(native_admin["profiles"], ["operator"])
        self.assertEqual(
            native_admin["entrypoint"],
            ["python", "/etc/jupyterhub/native_user_admin.py"],
        )
        self.assertEqual(
            native_admin["image"], "team-workspace-jupyterhub-admin:production"
        )
        self.assertNotEqual(
            native_admin["image"], config["services"]["jupyterhub"]["image"]
        )
        self.assertEqual(native_admin["network_mode"], "none")
        self.assertNotIn("ports", native_admin)
        self.assertNotIn("environment", native_admin)
        self.assertTrue(native_admin["read_only"])
        self.assertEqual(native_admin["cap_drop"], ["ALL"])
        self.assertEqual(
            {
                (mount["target"], mount.get("read_only", False))
                for mount in native_admin["volumes"]
            },
            {("/srv/jupyterhub", False)},
        )
        native_database_mount = next(
            mount
            for mount in native_admin["volumes"]
            if mount["target"] == "/srv/jupyterhub"
        )
        hub_database_mount = next(
            mount
            for mount in config["services"]["jupyterhub"]["volumes"]
            if mount["target"] == "/srv/jupyterhub"
        )
        self.assertEqual(native_database_mount["type"], "volume")
        self.assertEqual(native_database_mount["source"], hub_database_mount["source"])
        self.assertEqual(native_database_mount["source"], "jupyterhub_data")


class ProductionTransitionContractTests(unittest.TestCase):
    def test_candidate_policy_backup_and_migration_order_is_fail_closed(self) -> None:
        script = (ROOT / "scripts" / "production.sh").read_text(encoding="utf-8")
        start_body = script.split("start() {", 1)[1].split("\nstop() {", 1)[0]

        self.assertIn("profiles.candidate.json", script)
        self.assertIn("validate_database_inventory", script)
        self.assertIn("production_database_is_idle", script)
        self.assertIn("--context production", script)
        self.assertIn("PRODUCTION_FRESH_DATABASES=true", script)
        self.assertNotIn("down -v", script)
        self.assertNotIn("sudo ", script)
        self.assertNotIn("firewall", script)
        self.assertLess(
            script.index("snapshot_existing_databases"),
            script.index("migration_started=true"),
        )
        self.assertLess(
            script.index("migration_started=true"),
            script.index('--promote "${candidate_policy_file}"'),
        )
        self.assertLess(
            script.index('--promote "${candidate_policy_file}"'),
            script.index("compose run --rm --no-deps migrate"),
        )
        self.assertLess(
            script.index("compose run --rm --no-deps migrate"),
            script.index("compose up -d --build --wait"),
        )
        self.assertLess(
            start_body.index(
                "compose stop gateway worker reconciler api jupyterhub frontend egress-proxy"
            ),
            start_body.index("production_database_is_idle"),
        )
        self.assertLess(
            start_body.index("production_database_is_idle"),
            start_body.index("snapshot_existing_databases"),
        )

    def test_fresh_failure_removes_only_exact_production_database_volumes(self) -> None:
        script = (ROOT / "scripts" / "production.sh").read_text(encoding="utf-8")
        self.assertIn('"${PRODUCTION_COMPOSE_PROJECT_NAME}_platform_data"', script)
        self.assertIn('"${PRODUCTION_COMPOSE_PROJECT_NAME}_jupyterhub_data"', script)
        self.assertIn('docker volume rm "${fresh_volume}"', script)
        self.assertIn('rm -f -- "${policy_file}"', script)
        self.assertIn("image_identity team-workspace-backend:production", script)
        self.assertIn("image_identity team-workspace-jupyterhub:production", script)
        self.assertNotIn("platform.db:999:999", script)

    def test_offline_quiesce_is_backed_up_counted_and_fail_closed(self) -> None:
        script = (ROOT / "scripts" / "production.sh").read_text(encoding="utf-8")
        body = script.split("offline_quiesce() {", 1)[1].split("\nrestore() {", 1)[0]

        self.assertIn("PRODUCTION_EXPECTED_COUNT", body)
        self.assertIn("snapshot_existing_databases", body)
        self.assertIn("domain_test_database_snapshot.py verify", body)
        self.assertIn("--backup-bundle-id", body)
        self.assertIn("--expected-count", body)
        self.assertIn("production_database_is_idle", body)
        self.assertGreaterEqual(body.count("require_offline_recovery_state"), 3)
        self.assertLess(
            body.index("snapshot_existing_databases"),
            body.index("--apply"),
        )
        self.assertLess(
            body.rindex("require_offline_recovery_state"),
            body.index("--apply"),
        )
        self.assertNotIn("docker rm", body)
        self.assertNotIn("docker volume rm", body)

    def test_offline_quiesce_requires_absent_containers_and_exact_db_pair(self) -> None:
        script = (ROOT / "scripts" / "production.sh").read_text(encoding="utf-8")
        body = script.split("require_offline_recovery_state() {", 1)[1].split(
            "\nprepare_images_and_policy() {", 1
        )[0]

        self.assertIn("docker ps --all", body)
        self.assertIn(
            "com.docker.compose.project=${PRODUCTION_COMPOSE_PROJECT_NAME}", body
        )
        self.assertIn("label=platform.kind=jupyter-singleuser", body)
        self.assertIn('"$(database_volume_count)" == 2', body)
        self.assertIn('--filter "volume=${volume_name}"', body)
        self.assertIn("no container to mount database volume", body)

    def test_mutating_production_commands_share_an_operator_lock(self) -> None:
        script = (ROOT / "scripts" / "production.sh").read_text(encoding="utf-8")
        self.assertIn("flock -n", script)
        preflight_body = script.split("preflight() {", 1)[1].split("\nstart() {", 1)[0]
        self.assertIn("acquire_operator_lock", preflight_body)
        self.assertIn("preflight_impl", preflight_body)
        for start_marker, end_marker in (
            ("start() {", "\nstop() {"),
            ("stop() {", "\ncreate_user() {"),
            ("create_user() {", "\noffline_quiesce() {"),
            ("offline_quiesce() {", "\nrestore() {"),
            ("restore() {", '\ncase "${1:-}" in'),
        ):
            body = script.split(start_marker, 1)[1].split(end_marker, 1)[0]
            self.assertIn("acquire_operator_lock", body)

    def test_make_requires_explicit_offline_quiesce_count(self) -> None:
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        target = makefile.split("production-offline-quiesce:", 1)[1].split(
            "\nproduction-restore:", 1
        )[0]
        self.assertIn('test -n "$(EXPECTED_COUNT)"', target)
        self.assertIn('PRODUCTION_EXPECTED_COUNT="$(EXPECTED_COUNT)"', target)

    def test_admin_password_reset_reuses_fail_closed_account_maintenance(self) -> None:
        script = (ROOT / "scripts" / "production.sh").read_text(encoding="utf-8")
        restart_body = script.split("restart_account_control_plane_checked() {", 1)[
            1
        ].split("\nvalidate_host_contract() {", 1)[0]
        body = script.split("create_user() {", 1)[1].split("\noffline_quiesce() {", 1)[
            0
        ]

        self.assertIn("reset-admin-password) create_user reset-admin-password", script)
        self.assertIn('PRODUCTION_TARGET_USERNAME="${PLATFORM_ADMIN_USERNAME}"', body)
        self.assertIn("reset_password_flag+=(--reset-admin-password)", body)
        self.assertNotIn("PRODUCTION_PASSWORD", script)
        self.assertIn("running_healthy_service_id", body)
        self.assertIn('original_ids["${service}"]', body)
        first_trap = body.index('trap "${account_recovery_trap}" EXIT')
        self.assertLess(
            body.index("compose --profile operator build native-user-admin"),
            first_trap,
        )
        gateway_stop = body.index(
            'stop_existing_container_checked gateway "${original_ids[gateway]}"'
        )
        api_stop = body.index(
            'stop_existing_container_checked api "${original_ids[api]}"'
        )
        post_stop_idle = body.index("require_idle", api_stop)
        database_idle = body.index("production_database_is_idle", post_stop_idle)
        snapshot = body.index("snapshot_existing_databases", database_idle)
        admin_tool = body.index(
            "compose --profile operator run --rm --no-deps native-user-admin",
            snapshot,
        )
        backup_required = body.index(
            '"${PRODUCTION_FRESH_DATABASES}" == false', snapshot
        )
        backup_verified = body.index(
            "domain_test_database_snapshot.py verify", backup_required
        )
        committed = body.index("printf -v account_recovery_trap", admin_tool)
        committed_trap = body.index('trap "${account_recovery_trap}" EXIT', committed)
        final_restart = body.rindex("restart_account_control_plane_checked")
        self.assertLess(first_trap, gateway_stop)
        self.assertLess(gateway_stop, api_stop)
        self.assertLess(api_stop, post_stop_idle)
        self.assertLess(post_stop_idle, database_idle)
        self.assertLess(database_idle, snapshot)
        self.assertLess(snapshot, backup_required)
        self.assertLess(backup_required, backup_verified)
        self.assertLess(backup_verified, admin_tool)
        self.assertLess(admin_tool, committed)
        self.assertLess(committed, committed_trap)
        self.assertLess(committed_trap, final_restart)
        self.assertEqual(body.count('trap "${account_recovery_trap}" EXIT'), 2)
        self.assertIn("recover_after_user_admin_failure", restart_body)
        self.assertIn("trap - EXIT", restart_body)
        self.assertIn("start_existing_container_checked api", restart_body)
        self.assertIn("start_existing_container_checked jupyterhub", restart_body)
        self.assertIn("start_existing_container_checked gateway", restart_body)
        self.assertNotIn("compose up", restart_body)

        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        target = makefile.split("production-reset-admin-password:", 1)[1].split(
            "\nproduction-offline-quiesce-dry-run:", 1
        )[0]
        self.assertIn("bash scripts/production.sh reset-admin-password", target)


class NginxContractTests(unittest.TestCase):
    def test_hub_account_navigation_is_exact_in_every_runtime_mode(self) -> None:
        for name, portal_root in (
            ("dev.conf", "http://platform.localhost:8080/"),
            ("domain-test.conf", "https://platform.workspace.test/"),
            ("production.conf", "https://__PORTAL_HOST__/"),
        ):
            with self.subTest(name=name):
                config = (GATEWAY / name).read_text(encoding="utf-8")
                password_change = re.search(
                    r"location = /hub/change-password\s*\{(?P<body>.*?)"
                    r"(?=\n\s*location )",
                    config,
                    re.DOTALL,
                )
                self.assertIsNotNone(password_change)
                body = password_change.group("body") if password_change else ""
                self.assertRegex(
                    body,
                    r"limit_except GET POST\s*\{\s*deny all;\s*\}",
                )
                self.assertIn("proxy_pass http://jupyterhub:8000;", body)
                self.assertRegex(
                    config,
                    r"location \^~ /hub/change-password/\s*\{\s*return 404;\s*\}",
                )
                self.assertRegex(
                    config,
                    r"location = /hub/spawn\s*\{\s*return 303 "
                    + re.escape(portal_root)
                    + r"\s*;?\s*\}",
                )
                self.assertRegex(
                    config,
                    r"location ~ \^/hub/\(spawn\|token\|admin\)\(/\|\$\)",
                )

    def test_domain_test_and_production_fail_closed(self) -> None:
        domain = (GATEWAY / "domain-test.conf").read_text(encoding="utf-8")
        production = (GATEWAY / "production.conf").read_text(encoding="utf-8")
        proxy = (GATEWAY / "proxy-https.conf").read_text(encoding="utf-8")
        entrypoint = (GATEWAY / "entrypoint-production.sh").read_text(encoding="utf-8")

        for config, portal_root in (
            (domain, "https://platform.workspace.test/"),
            (production, "https://__PORTAL_HOST__/"),
        ):
            self.assertIn("listen 3030 ssl default_server", config)
            self.assertIn("ssl_reject_handshake on", config)
            self.assertIn("client_max_body_size 2g", config)
            self.assertIn("proxy_request_buffering off", config)
            self.assertNotIn("~*^[a-z0-9]", config)
            callback = re.search(
                r"location = /api/v1/auth/callback\s*\{(?P<body>.*?)\n\s*\}",
                config,
                re.DOTALL,
            )
            self.assertIsNotNone(callback)
            callback_body = callback.group("body") if callback else ""
            self.assertIn(
                "access_log /var/log/nginx/access.log safe_path;", callback_body
            )
            self.assertIn("error_log /var/log/nginx/error.log crit;", callback_body)
            self.assertIn("proxy_pass http://api:8000;", callback_body)
            self.assertIn(
                "include /etc/nginx/includes/proxy-https.conf;", callback_body
            )
            self.assertRegex(
                config,
                r'location ~ "\^/user/.*?/oauth_callback\$"\s*\{'
                r"(?s:.*?)access_log /var/log/nginx/access\.log safe_path;"
                r"(?s:.*?)error_log /var/log/nginx/error\.log crit;",
            )
            self.assertRegex(
                config,
                r"location ~ \^/hub/\(login\|signup\).*?\{"
                r"(?s:.*?)access_log /var/log/nginx/access\.log safe_path;"
                r"(?s:.*?)error_log /var/log/nginx/error\.log crit;",
            )
            password_change = re.search(
                r"location = /hub/change-password\s*\{(?P<body>.*?)"
                r"(?=\n\s*location )",
                config,
                re.DOTALL,
            )
            self.assertIsNotNone(password_change)
            password_change_body = (
                password_change.group("body") if password_change else ""
            )
            self.assertIn("proxy_pass http://jupyterhub:8000;", password_change_body)
            self.assertIn("limit_req zone=hub_login", password_change_body)
            self.assertRegex(
                password_change_body,
                r"limit_except GET POST\s*\{\s*deny all;\s*\}",
            )
            self.assertRegex(
                config,
                r"location \^~ /hub/change-password/\s*\{\s*return 404;\s*\}",
            )
            spawn_fallback = re.search(
                r"location = /hub/spawn\s*\{(?P<body>.*?)\n\s*\}",
                config,
                re.DOTALL,
            )
            self.assertIsNotNone(spawn_fallback)
            self.assertIn(f"return 303 {portal_root};", spawn_fallback.group("body"))
            self.assertRegex(config, r"location ~ \^/hub/\(spawn\|token\|admin\)")
        self.assertNotIn("company-vpn-allowlist", domain)
        self.assertIn("include /tmp/company-vpn-allowlist.conf", production)
        self.assertIn("if ($platform_ingress_allowed = 0) { return 444; }", production)
        self.assertIn("X-Forwarded-Proto https", proxy)
        self.assertIn("X-Forwarded-Port 443", proxy)
        self.assertIn("X-Forwarded-For $remote_addr", proxy)
        self.assertNotIn("$proxy_add_x_forwarded_for", proxy)
        main_config = (GATEWAY / "nginx-main.production.conf").read_text(
            encoding="utf-8"
        )
        safe_format = re.search(
            r"log_format safe_path (?P<body>.*?);", main_config, re.DOTALL
        )
        self.assertIsNotNone(safe_format)
        safe_body = safe_format.group("body") if safe_format else ""
        self.assertIn("$request_method $uri $server_protocol", safe_body)
        self.assertNotIn("$request_uri", safe_body)
        self.assertNotIn("$args", safe_body)
        self.assertIn("production company/VPN CIDR allowlist", entrypoint)
        self.assertIn("DNS:*.hub.workspace.test", entrypoint)
        self.assertIn("PLATFORM_PORTAL_HOST", entrypoint)
        self.assertIn("PLATFORM_HUB_HOST", entrypoint)
        self.assertIn("PLATFORM_USER_DOMAIN", entrypoint)
        self.assertIn(
            'openssl x509 -in "${cert_path}" -noout -checkhost "${portal_host}"',
            entrypoint,
        )
        self.assertIn(
            "TLS leaf certificate does not cover portal host ${portal_host}",
            entrypoint,
        )
        self.assertIn("include /tmp/platform-server.conf", main_config)
        self.assertNotIn("platform.example.com", production)
        self.assertNotIn("hub.example.net", production)

    def test_build_and_runtime_modes_are_fail_closed_and_coupled(self) -> None:
        dockerfile = (GATEWAY / "Dockerfile.production").read_text(encoding="utf-8")
        entrypoint = (GATEWAY / "entrypoint-production.sh").read_text(encoding="utf-8")

        self.assertIn(
            "GATEWAY_SERVER_CONFIG must be production.conf or domain-test.conf",
            dockerfile,
        )
        self.assertIn(
            "production.conf forbids ALLOW_MUTABLE_BASE_IMAGE=true", dockerfile
        )
        self.assertIn("/usr/local/share/platform-gateway-config-mode", dockerfile)
        self.assertIn('test "${gateway_mode}" = "${baked_mode}"', entrypoint)
        self.assertIn(
            "PLATFORM_GATEWAY_MODE does not match the baked Nginx server config",
            entrypoint,
        )
        exact_san_checks = entrypoint.split("for required_san in", 1)[1].split(
            "cert_public_key_digest=", 1
        )[0]
        self.assertIn('"${hub_san}"', exact_san_checks)
        self.assertIn('"${wildcard_hub_san}"', exact_san_checks)
        self.assertNotIn('"${portal_san}"', exact_san_checks)
        self.assertNotIn("portal_san=", entrypoint)
        self.assertIn("$0 == required { found = 1 }", exact_san_checks)


@unittest.skipUnless(docker_daemon_available(), "a reachable Docker daemon is required")
class GatewayImageModeIntegrationTests(unittest.TestCase):
    def test_production_build_cannot_use_the_mutable_base_escape(self) -> None:
        result = subprocess.run(
            [
                "docker",
                "build",
                "--file",
                str(GATEWAY / "Dockerfile.production"),
                "--build-arg",
                "GATEWAY_SERVER_CONFIG=production.conf",
                "--build-arg",
                "ALLOW_MUTABLE_BASE_IMAGE=true",
                str(GATEWAY),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "production.conf forbids ALLOW_MUTABLE_BASE_IMAGE=true",
            result.stdout + result.stderr,
        )

    def test_domain_test_image_rejects_production_runtime_mode(self) -> None:
        image = f"team-platform-gateway-mode-contract:audit-{os.getpid()}"
        build = subprocess.run(
            [
                "docker",
                "build",
                "--tag",
                image,
                "--file",
                str(GATEWAY / "Dockerfile.production"),
                "--build-arg",
                "GATEWAY_SERVER_CONFIG=domain-test.conf",
                "--build-arg",
                "ALLOW_MUTABLE_BASE_IMAGE=true",
                str(GATEWAY),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
        try:
            started = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--env",
                    "PLATFORM_GATEWAY_MODE=production",
                    image,
                    "true",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        finally:
            subprocess.run(
                ["docker", "image", "rm", image],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        self.assertNotEqual(started.returncode, 0)
        self.assertIn(
            "PLATFORM_GATEWAY_MODE does not match the baked Nginx server config",
            started.stdout + started.stderr,
        )


class HostPreflightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module("check_domain_test", GATEWAY / "check_domain_test.py")

    def test_exact_user_hosts(self) -> None:
        self.assertEqual(
            self.module.expected_hosts(["alice", "bob-2"]),
            [
                "platform.workspace.test",
                "hub.workspace.test",
                "alice.hub.workspace.test",
                "bob-2.hub.workspace.test",
            ],
        )

    def test_invalid_user_is_rejected(self) -> None:
        for username in ("Alice", "a--b", "../x", "-alice"):
            with self.subTest(username=username), self.assertRaises(ValueError):
                self.module.expected_hosts([username])

    def test_forbidden_loopback_probe_fails_closed_without_traceback(self) -> None:
        with mock.patch.object(
            self.module.socket, "socket", side_effect=PermissionError("blocked")
        ):
            self.assertFalse(self.module.port_443_is_free())


class IdleDatabaseGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Path(self.tempdir.name) / "platform.db"
        connection = sqlite3.connect(self.database)
        connection.executescript(
            """
            CREATE TABLE operations (
                id TEXT, operation_type TEXT, status TEXT, requested_at TEXT
            );
            CREATE TABLE workspaces (
                id TEXT, desired_state TEXT, observed_state TEXT, archived_at TEXT
            );
            CREATE TABLE user_provisioning_jobs (user_id TEXT, status TEXT);
            """
        )
        connection.commit()
        connection.close()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def run_gate(
        self, context: str = "domain-test"
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/check-domain-test-idle.py"),
                "--database",
                str(self.database),
                "--context",
                context,
            ],
            capture_output=True,
            text=True,
        )

    def test_idle_database_passes(self) -> None:
        self.assertEqual(self.run_gate().returncode, 0)

    def test_production_context_is_visible_to_the_operator(self) -> None:
        result = self.run_gate("production")

        self.assertEqual(result.returncode, 0)
        self.assertIn("production idle check using:", result.stderr)
        self.assertIn("production idle check passed", result.stdout)

    def test_pending_operation_fails(self) -> None:
        connection = sqlite3.connect(self.database)
        connection.execute(
            "INSERT INTO operations VALUES ('op-1', 'START', 'PENDING', 'now')"
        )
        connection.commit()
        connection.close()
        result = self.run_gate()
        self.assertEqual(result.returncode, 1)
        self.assertIn("busy operation", result.stderr)

    def test_waiting_external_operation_fails(self) -> None:
        connection = sqlite3.connect(self.database)
        connection.execute(
            "INSERT INTO operations VALUES "
            "('op-delete', 'DELETE', 'WAITING_EXTERNAL', 'now')"
        )
        connection.commit()
        connection.close()
        result = self.run_gate()
        self.assertEqual(result.returncode, 1)
        self.assertIn("WAITING_EXTERNAL", result.stderr)

    def test_running_deletion_fails_when_0004_table_exists(self) -> None:
        connection = sqlite3.connect(self.database)
        connection.execute(
            "CREATE TABLE workspace_deletion_jobs ("
            "workspace_id TEXT, operation_id TEXT, status TEXT, requested_at TEXT)"
        )
        connection.execute(
            "INSERT INTO workspace_deletion_jobs VALUES "
            "('ws-delete', 'op-delete', 'RUNNING', 'now')"
        )
        connection.commit()
        connection.close()
        result = self.run_gate()
        self.assertEqual(result.returncode, 1)
        self.assertIn("busy deletion", result.stderr)

    def test_0003_schema_without_deletion_table_still_passes(self) -> None:
        self.assertEqual(self.run_gate().returncode, 0)


class DomainTestDatabaseSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module(
            "domain_test_database_snapshot",
            ROOT / "scripts/domain_test_database_snapshot.py",
        )

    @staticmethod
    def _database(path: Path, revision: str) -> None:
        with sqlite3.connect(path) as database:
            database.execute("CREATE TABLE alembic_version (version_num TEXT)")
            database.execute("INSERT INTO alembic_version VALUES (?)", (revision,))
            database.commit()

    def test_secure_bundle_has_private_verified_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "backups"
            parent.mkdir(mode=0o700)
            bundle = self.module.prepare_bundle(parent, "domain-test-safe")
            for filename, revision in (
                ("platform.sqlite", "0005"),
                ("jupyterhub.sqlite", "4621fec11365"),
            ):
                self._database(bundle / filename, revision)
            manifest = self.module.finalize_bundle(bundle)

            self.assertEqual(bundle.stat().st_mode & 0o777, 0o700)
            self.assertEqual((bundle / "manifest.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                manifest["files"]["platform.sqlite"]["schema_revision"], "0005"
            )
            self.module.verify_bundle(bundle)

    def test_setgid_runtime_parent_is_normalized_to_exact_private_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "backups"
            parent.mkdir(mode=0o700)
            parent.chmod(0o2700)

            bundle = self.module.prepare_bundle(parent, "domain-test-setgid")

            self.assertEqual(parent.stat().st_mode & 0o7777, 0o2700)
            self.assertEqual(bundle.stat().st_mode & 0o7777, 0o700)

    def test_online_snapshot_includes_committed_wal_and_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "live.sqlite"
            output = root / "snapshot.sqlite"
            with sqlite3.connect(source) as database:
                database.execute("PRAGMA journal_mode=WAL")
                database.execute("CREATE TABLE marker (value TEXT NOT NULL)")
                database.execute("INSERT INTO marker VALUES ('committed')")
                database.commit()
                self.module.snapshot_database(source, output)

            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            with sqlite3.connect(output) as snapshot:
                self.assertEqual(
                    snapshot.execute("SELECT value FROM marker").fetchone(),
                    ("committed",),
                )
                self.assertEqual(
                    snapshot.execute("PRAGMA quick_check").fetchone(), ("ok",)
                )

    def test_online_snapshot_succeeds_after_one_transient_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "live.sqlite"
            output = root / "snapshot.sqlite"
            self._database(source, "0005")
            real_connect = self.module._connect_snapshot_source
            attempts = 0

            def connect_after_transient_lock(path: Path):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise sqlite3.OperationalError("database is locked")
                return real_connect(path)

            with (
                mock.patch.object(
                    self.module,
                    "_connect_snapshot_source",
                    side_effect=connect_after_transient_lock,
                ),
                mock.patch.object(self.module.time, "sleep") as sleep,
            ):
                self.module.snapshot_database(source, output)

            self.assertEqual(attempts, 2)
            sleep.assert_called_once_with(1)
            with sqlite3.connect(output) as snapshot:
                self.assertEqual(
                    snapshot.execute(
                        "SELECT version_num FROM alembic_version"
                    ).fetchone(),
                    ("0005",),
                )

    def test_group_access_and_symlink_bundle_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "backups"
            parent.mkdir(mode=0o750)
            with self.assertRaisesRegex(RuntimeError, "mode 0700"):
                self.module.prepare_bundle(parent, "domain-test-unsafe")

            target = Path(directory) / "target"
            target.mkdir(mode=0o700)
            parent.rmdir()
            parent.symlink_to(target, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, "non-symlink"):
                self.module.prepare_bundle(parent, "domain-test-symlink")

    def test_manifest_rejects_group_readable_backup_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "backups"
            parent.mkdir(mode=0o700)
            bundle = self.module.prepare_bundle(parent, "domain-test-tamper")
            for filename in ("platform.sqlite", "jupyterhub.sqlite"):
                self._database(bundle / filename, "0004")
            self.module.finalize_bundle(bundle)
            (bundle / "platform.sqlite").chmod(0o640)
            with self.assertRaisesRegex(RuntimeError, "mode 0600"):
                self.module.verify_bundle(bundle)


class DomainTestTransitionContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script = (ROOT / "scripts/domain-test.sh").read_text(encoding="utf-8")

    def test_in_place_upgrade_allows_only_exact_managed_tls_listener(self) -> None:
        helper = self.script[
            self.script.index("managed_domain_test_listener()") : self.script.index(
                "run_idle_check()"
            )
        ]
        self.assertIn('container_is_running "$container_id"', helper)
        self.assertIn("com.docker.compose.service", helper)
        self.assertIn("PLATFORM_GATEWAY_MODE", helper)
        self.assertIn('docker port "$container_id" 3030/tcp', helper)
        self.assertIn("127.0.0.1:443", helper)

        preflight = self.script[
            self.script.index("preflight()") : self.script.index("snapshot_service()")
        ]
        self.assertIn("local -a port_guard=(--check-port-free)", preflight)
        self.assertIn("if managed_domain_test_listener; then", preflight)
        self.assertIn("port_guard=()", preflight)
        self.assertIn('host_args+=("${port_guard[@]}")', preflight)

    def test_worker_and_health_services_use_their_real_state_contracts(self) -> None:
        self.assertNotIn("up -d --build --wait", self.script)
        self.assertIn("wait_for_service worker running", self.script)
        self.assertIn(
            "for service in api frontend egress-proxy jupyterhub reconciler gateway",
            self.script,
        )
        self.assertIn('wait_for_service "$service" healthy', self.script)
        for service in ("migrate", "bootstrap-profile", "singleuser-image"):
            self.assertIn(f"require_completed_service {service}", self.script)

    def test_preflight_never_reconfigures_or_removes_live_edge_network(self) -> None:
        preflight = self.script[
            self.script.index("preflight()") : self.script.index("snapshot_service()")
        ]
        self.assertIn(
            "compose_domain build singleuser-image api jupyterhub frontend gateway egress-proxy",
            preflight,
        )
        self.assertIn("compose_base run --rm --no-deps singleuser-image", preflight)
        self.assertNotIn("compose_domain run", preflight)
        self.assertNotIn("stop_long_running_static_services", preflight)
        self.assertNotIn("network disconnect", self.script)
        self.assertNotIn("docker rm", preflight)
        self.assertNotIn("compose_domain down", preflight)

    def test_every_static_endpoint_stops_and_backup_precedes_network_replacement(
        self,
    ) -> None:
        services_match = re.search(
            r"readonly long_running_static_services=\((?P<body>.*?)\n\)",
            self.script,
            re.DOTALL,
        )
        self.assertIsNotNone(services_match)
        services = services_match.group("body").split() if services_match else []
        self.assertEqual(
            services,
            [
                "gateway",
                "worker",
                "reconciler",
                "api",
                "jupyterhub",
                "frontend",
                "egress-proxy",
            ],
        )
        helper = self.script[
            self.script.index(
                "stop_long_running_static_services()"
            ) : self.script.index("start_local_stack()")
        ]
        self.assertIn('compose_base stop "${long_running_static_services[@]}"', helper)

        start = self.script.index("start_domain_test()")
        start_body = self.script[
            start : self.script.index("restore_database_backup()", start)
        ]
        self.assertLess(
            start_body.index("stop_long_running_static_services"),
            start_body.index("create_database_backup"),
        )
        self.assertLess(
            start_body.index("create_database_backup"),
            start_body.index("compose_domain down --remove-orphans"),
        )

    def test_network_recreation_never_mutates_host_firewall(self) -> None:
        start = self.script.index("start_domain_test()")
        start_body = self.script[
            start : self.script.index("restore_database_backup()", start)
        ]
        self.assertNotIn("sudo", self.script)
        self.assertNotIn("firewall-apply", self.script)
        self.assertNotIn("apply_jupyter_firewall", self.script)
        self.assertLess(
            start_body.index("compose_domain up -d --build"),
            start_body.index("wait_for_domain_test_stack"),
        )

    def test_post_down_failure_stays_down_and_prints_restore_command(self) -> None:
        failure = self.script[
            self.script.index("report_transition_failure()") : self.script.index(
                "start_domain_test()"
            )
        ]
        self.assertIn('[ "$transition_is_destructive" = true ]', failure)
        self.assertIn("database writers remain stopped", failure)
        self.assertIn("make domain-test-restore BACKUP=", failure)
        destructive_branch = failure.split("return 0", 1)[0]
        self.assertNotIn("start_local_stack", destructive_branch)

    def test_database_restore_rejects_stopped_or_running_volume_users(self) -> None:
        restore = self.script[self.script.index("restore_database_backup()") :]
        self.assertIn(
            'docker ps --all --filter "volume=${project_name}_platform_data"',
            restore,
        )
        self.assertIn(
            'docker ps --all --filter "volume=${project_name}_jupyterhub_data"',
            restore,
        )


if __name__ == "__main__":
    unittest.main()
