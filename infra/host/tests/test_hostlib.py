from __future__ import annotations

import json
import ipaddress
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path


HOST_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = HOST_ROOT.parents[1]
sys.path.insert(0, str(HOST_ROOT))

from hostlib import HostConfigError, load_config  # noqa: E402
from check_network_health import (  # noqa: E402
    bridge_has_no_ip_addresses,
    normalize_iptables_rule,
    normalized_iptables_rules,
)
from enforce_jupyter_bridge_policy import validate_network_identity  # noqa: E402


class HostConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = json.loads(
            (HOST_ROOT / "config.local-dev.json").read_text(encoding="utf-8")
        )

    def _load(self, config: dict) -> dict:
        with tempfile.TemporaryDirectory(dir=REPOSITORY_ROOT) as directory:
            path = Path(directory) / "host.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            os.chmod(path, 0o600)
            return load_config(path)

    def test_local_config_has_valid_distinct_execution_and_egress_networks(
        self,
    ) -> None:
        loaded = self._load(self.config)
        self.assertEqual(loaded["network"]["bridge_name"], "br-plt-jlocal")

    def test_linux_bridge_name_over_15_bytes_is_rejected(self) -> None:
        self.config["network"]["bridge_name"] = "br-platform-jupyter"
        with self.assertRaisesRegex(HostConfigError, "at most 15"):
            self._load(self.config)

    def test_overlapping_proxy_egress_and_execution_subnets_are_rejected(self) -> None:
        self.config["network"]["egress_out_subnet"] = "172.31.0.0/25"
        self.config["network"]["egress_out_proxy_ip"] = "172.31.0.2"
        with self.assertRaisesRegex(HostConfigError, "distinct valid"):
            self._load(self.config)


class DockerGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = json.loads(
            (HOST_ROOT / "config.local-dev.json").read_text(encoding="utf-8")
        )

    def _write_config(self, directory: Path) -> tuple[Path, Path]:
        output = directory / "health"
        self.config["health"]["output_dir"] = str(output)
        path = directory / "host.json"
        path.write_text(json.dumps(self.config), encoding="utf-8")
        os.chmod(path, 0o600)
        return path, output

    def _rotate(self, config: Path, *arguments: str) -> None:
        subprocess.run(
            [
                sys.executable,
                str(HOST_ROOT / "rotate_docker_generation.py"),
                "--config",
                str(config),
                *arguments,
            ],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

    def test_rotation_bootstraps_uuid_and_invalidates_health(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPOSITORY_ROOT) as raw_directory:
            directory = Path(raw_directory)
            config, output = self._write_config(directory)
            output.mkdir()
            (output / "network_health.json").write_text("{}", encoding="utf-8")
            (output / "storage_health.json").write_text("{}", encoding="utf-8")

            self._rotate(config)

            generation = (
                (output / "docker_generation").read_text(encoding="ascii").strip()
            )
            self.assertEqual(str(uuid.UUID(generation)), generation)
            self.assertFalse((output / "network_health.json").exists())
            self.assertFalse((output / "storage_health.json").exists())

    def test_invalidate_only_preserves_current_generation(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPOSITORY_ROOT) as raw_directory:
            directory = Path(raw_directory)
            config, output = self._write_config(directory)
            output.mkdir()
            current = str(uuid.uuid4())
            (output / "docker_generation").write_text(current + "\n", encoding="ascii")
            (output / "network_health.json").write_text("{}", encoding="utf-8")

            self._rotate(config, "--invalidate-only")

            self.assertEqual(
                (output / "docker_generation").read_text(encoding="ascii").strip(),
                current,
            )
            self.assertFalse((output / "network_health.json").exists())


class LocalBootstrapContractTests(unittest.TestCase):
    def test_legacy_firewall_apply_rotates_before_apply_and_health_check(self) -> None:
        result = subprocess.run(
            [
                "make",
                "--no-print-directory",
                "--dry-run",
                "legacy-host-firewall-apply",
            ],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        commands = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(
            commands,
            [
                "sudo python3 infra/host/rotate_docker_generation.py "
                "--config infra/host/config.local-dev.json",
                "sudo python3 infra/host/enforce_jupyter_bridge_policy.py "
                "--config infra/host/config.local-dev.json",
                "sudo python3 infra/host/apply_jupyter_firewall.py "
                "--config infra/host/config.local-dev.json",
                "sudo python3 infra/host/check_network_health.py "
                "--config infra/host/config.local-dev.json",
            ],
        )

    @unittest.skipUnless(shutil.which("docker"), "Docker Compose CLI is unavailable")
    def test_default_compose_network_needs_no_host_firewall(self) -> None:
        environment = os.environ.copy()
        environment.setdefault("DOCKER_GID", "999")
        environment.setdefault("PLATFORM_SECRET_GID", "1000")
        result = subprocess.run(
            ["docker", "compose", "config", "--format", "json"],
            cwd=REPOSITORY_ROOT,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        compose = json.loads(result.stdout)
        network = compose["networks"]["jupyter"]
        self.assertEqual(network["name"], "platform-jupyter-compose-local")
        self.assertEqual(network["driver"], "bridge")
        self.assertIs(network["internal"], True)
        self.assertIs(network["enable_ipv6"], False)
        self.assertEqual(
            network["driver_opts"],
            {
                "com.docker.network.bridge.enable_icc": "true",
                "com.docker.network.bridge.gateway_mode_ipv4": "isolated",
                "com.docker.network.bridge.gateway_mode_ipv6": "isolated",
            },
        )
        self.assertEqual(
            network["labels"],
            {
                "platform.kind": "jupyter-execution",
                "platform.managed": "true",
                "platform.network.policy": "compose-internal-trusted-v1",
            },
        )
        self.assertEqual(
            network["ipam"]["config"],
            [{"subnet": "172.30.0.0/24", "ip_range": "172.30.0.128/25"}],
        )

        hub = compose["services"]["jupyterhub"]
        proxy = compose["services"]["egress-proxy"]
        self.assertEqual(hub["environment"]["JUPYTER_NETWORK_NAME"], network["name"])
        self.assertEqual(
            hub["environment"]["JUPYTER_NETWORK_POLICY_MODE"],
            "compose-internal-trusted-v1",
        )
        self.assertEqual(
            hub["environment"]["JUPYTERHUB_HUB_CONNECT_URL"],
            "http://172.30.0.10:8081",
        )
        self.assertEqual(
            hub["environment"]["JUPYTER_EGRESS_PROXY_URL"],
            "http://172.30.0.20:3128",
        )
        self.assertEqual(hub["networks"]["jupyter"]["ipv4_address"], "172.30.0.10")
        self.assertEqual(proxy["networks"]["jupyter"]["ipv4_address"], "172.30.0.20")
        self.assertEqual(proxy["networks"]["egress-out"]["ipv4_address"], "172.24.0.2")

        # Existing external-network/firewall state is intentionally left in
        # place. New addresses must not overlap or match its source jumps.
        legacy = json.loads(
            (HOST_ROOT / "config.local-dev.json").read_text(encoding="utf-8")
        )["network"]
        self.assertFalse(
            ipaddress.ip_network("172.30.0.0/24").overlaps(
                ipaddress.ip_network(legacy["subnet"])
            )
        )
        self.assertFalse(
            ipaddress.ip_network("172.24.0.0/16").overlaps(
                ipaddress.ip_network(legacy["egress_out_subnet"])
            )
        )
        self.assertNotEqual("172.24.0.2", legacy["egress_out_proxy_ip"])


class NetworkHealthNormalizationTests(unittest.TestCase):
    def test_bridge_policy_refuses_unexpected_network_identity(self) -> None:
        network = (
            self.config["network"]
            if hasattr(self, "config")
            else json.loads(
                (HOST_ROOT / "config.local-dev.json").read_text(encoding="utf-8")
            )["network"]
        )
        inspected = [
            {
                "Name": network["name"],
                "Driver": "bridge",
                "Internal": True,
                "EnableIPv6": False,
                "Options": {
                    "com.docker.network.bridge.name": network["bridge_name"],
                    "com.docker.network.bridge.gateway_mode_ipv4": "isolated",
                    "com.docker.network.bridge.gateway_mode_ipv6": "isolated",
                },
                "Labels": {
                    "platform.managed": "true",
                    "platform.kind": "jupyter-execution",
                },
            }
        ]
        validate_network_identity(inspected, network)
        inspected[0]["Internal"] = False
        with self.assertRaisesRegex(HostConfigError, "unexpected identity"):
            validate_network_identity(inspected, network)

    def test_docker_isolated_bridge_rejects_every_ip_address(self) -> None:
        self.assertTrue(bridge_has_no_ip_addresses([{"addr_info": []}]))
        for address in (
            {"family": "inet", "local": "172.31.0.1", "scope": "global"},
            {"family": "inet6", "local": "fd00::1", "scope": "global"},
            {"family": "inet6", "local": "fe80::1", "scope": "link"},
        ):
            with self.subTest(address=address):
                self.assertFalse(bridge_has_no_ip_addresses([{"addr_info": [address]}]))

    def test_iptables_nft_display_normalization_preserves_policy(self) -> None:
        expected = normalize_iptables_rule(
            [
                "-A",
                "PLATFORM-JUPYTER",
                "-s",
                "172.31.0.10",
                "-d",
                "172.31.0.128/25",
                "-p",
                "tcp",
                "--dport",
                "8888",
                "-j",
                "ACCEPT",
            ]
        )
        actual = normalized_iptables_rules(
            "-A PLATFORM-JUPYTER -s 172.31.0.10/32 "
            "-d 172.31.0.128/25 -p tcp -m tcp --dport 8888 -j ACCEPT\n"
        )
        self.assertEqual(actual, [expected])
        self.assertEqual(
            normalize_iptables_rule(
                [
                    "-A",
                    "PLATFORM-JUPYTER",
                    "-m",
                    "conntrack",
                    "--ctstate",
                    "RELATED,ESTABLISHED",
                    "-j",
                    "ACCEPT",
                ]
            ),
            normalize_iptables_rule(
                [
                    "-A",
                    "PLATFORM-JUPYTER",
                    "-m",
                    "conntrack",
                    "--ctstate",
                    "ESTABLISHED,RELATED",
                    "-j",
                    "ACCEPT",
                ]
            ),
        )

    def test_iptables_normalization_does_not_hide_policy_changes(self) -> None:
        expected = normalize_iptables_rule(
            [
                "-A",
                "PLATFORM-JUPYTER",
                "-s",
                "172.31.0.10",
                "-p",
                "tcp",
                "--dport",
                "8888",
                "-j",
                "ACCEPT",
            ]
        )
        for changed in (
            "-A PLATFORM-JUPYTER -s 172.31.0.10/32 -p tcp "
            "-m tcp --dport 8889 -j ACCEPT",
            "-A PLATFORM-JUPYTER -s 172.31.0.10/32 -p udp " "--dport 8888 -j ACCEPT",
            "-A PLATFORM-JUPYTER -s 172.31.0.10/32 -p tcp "
            "-m tcp --dport 8888 -j DROP",
        ):
            with self.subTest(changed=changed):
                self.assertNotEqual(normalized_iptables_rules(changed), [expected])


if __name__ == "__main__":
    unittest.main()
