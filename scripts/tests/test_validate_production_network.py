from __future__ import annotations

import importlib.util
import ipaddress
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "validate_production_network.py"
SPEC = importlib.util.spec_from_file_location("validate_production_network", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
network_contract = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = network_contract
SPEC.loader.exec_module(network_contract)


def contract(mode: str = "inhibit-ipv4") -> network_contract.NetworkContract:
    return network_contract.NetworkContract(
        network_name="platform-jupyter-compose-production",
        compose_project="team-workspace-production",
        isolation_mode=mode,
        subnet=ipaddress.ip_network("172.40.0.0/24"),
        ip_range=ipaddress.ip_network("172.40.0.128/25"),
        required_endpoints=frozenset(
            (ipaddress.ip_address("172.40.0.10"), ipaddress.ip_address("172.40.0.20"))
        ),
    )


def inspected_network(mode: str = "inhibit-ipv4") -> dict:
    options = (
        network_contract.INHIBIT_IPV4_OPTIONS
        if mode == "inhibit-ipv4"
        else network_contract.ISOLATED_OPTIONS
    )
    return {
        "Name": "platform-jupyter-compose-production",
        "Id": "a" * 64,
        "Driver": "bridge",
        "Scope": "local",
        "Internal": True,
        "EnableIPv6": False,
        "Attachable": False,
        "Ingress": False,
        "Options": dict(options),
        "Labels": {
            "com.docker.compose.config-hash": "b" * 64,
            "com.docker.compose.network": "jupyter",
            "com.docker.compose.project": "team-workspace-production",
            "com.docker.compose.version": "2.24.4",
            **network_contract.PLATFORM_LABELS,
        },
        "IPAM": {
            "Driver": "default",
            "Options": None,
            "Config": [{"Subnet": "172.40.0.0/24", "IPRange": "172.40.0.128/25"}],
        },
        "Containers": {
            "hub": {
                "Name": "team-workspace-production-jupyterhub-1",
                "IPv4Address": "172.40.0.10/24",
                "IPv6Address": "",
            },
            "proxy": {
                "Name": "team-workspace-production-egress-proxy-1",
                "IPv4Address": "172.40.0.20/24",
                "IPv6Address": "",
            },
        },
    }


class VersionContractTests(unittest.TestCase):
    def test_supported_stable_and_vendor_versions(self) -> None:
        cases = {
            "27.1.2": (27, 1, 2),
            "27.1.2-1~ubuntu.24.04~noble": (27, 1, 2),
            "27.5.1+azure.1": (27, 5, 1),
            "28.0.0": (28, 0, 0),
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(network_contract.parse_stable_version(raw), expected)

    def test_old_malformed_and_prerelease_versions_fail_closed(self) -> None:
        for raw in (
            "27.1.1",
            "27.1",
            "027.1.2",
            "27.1.2-rc.1",
            "28.0.0-beta.1",
            "27.1.2~ubuntu",
        ):
            with (
                self.subTest(raw=raw),
                self.assertRaises(network_contract.ContractError),
            ):
                network_contract.parse_stable_version(raw)


class DeclarativeNetworkContractTests(unittest.TestCase):
    def test_selected_engine_27_and_engine_28_contracts_are_accepted(self) -> None:
        for mode, version in (
            ("inhibit-ipv4", (27, 1, 2)),
            ("isolated", (28, 0, 0)),
        ):
            with self.subTest(mode=mode, version=version):
                inspected = inspected_network(mode)
                self.assertEqual(
                    network_contract.validate_network_configuration(
                        inspected, contract(mode), version
                    ),
                    "a" * 64,
                )

    def test_contract_mode_must_match_engine_generation(self) -> None:
        for mode, version in (
            ("isolated", (27, 5, 1)),
            ("inhibit-ipv4", (28, 0, 0)),
        ):
            with (
                self.subTest(mode=mode, version=version),
                self.assertRaises(network_contract.ContractError),
            ):
                network_contract.validate_network_configuration(
                    inspected_network(mode), contract(mode), version
                )

    def test_ipam_gateway_or_extra_option_is_rejected(self) -> None:
        gateway = inspected_network()
        gateway["IPAM"]["Config"][0]["Gateway"] = "172.40.0.1"
        extra_option = inspected_network()
        extra_option["Options"][
            "com.docker.network.bridge.enable_ip_masquerade"
        ] = "true"
        for inspected in (gateway, extra_option):
            with self.assertRaises(network_contract.ContractError):
                network_contract.validate_network_configuration(
                    inspected, contract(), (27, 1, 2)
                )

    def test_platform_and_compose_ownership_labels_are_exact(self) -> None:
        for mutation in (
            "platform-extra",
            "compose-extra",
            "wrong-project",
            "foreign-label",
        ):
            inspected = inspected_network()
            if mutation == "platform-extra":
                inspected["Labels"]["platform.unreviewed"] = "true"
            elif mutation == "compose-extra":
                inspected["Labels"]["com.docker.compose.unreviewed"] = "true"
            elif mutation == "wrong-project":
                inspected["Labels"]["com.docker.compose.project"] = "foreign"
            else:
                inspected["Labels"]["foreign.owner"] = "true"
            with (
                self.subTest(mutation=mutation),
                self.assertRaises(network_contract.ContractError),
            ):
                network_contract.validate_managed_identity(inspected, contract())

    def test_compose_2_29_labels_without_optional_config_hash_are_accepted(
        self,
    ) -> None:
        inspected = inspected_network()
        inspected["Labels"].pop("com.docker.compose.config-hash")

        self.assertEqual(
            network_contract.validate_managed_identity(inspected, contract()),
            "a" * 64,
        )

    def test_endpoint_ipv6_and_missing_reserved_endpoint_are_rejected(self) -> None:
        ipv6 = inspected_network()
        ipv6["Containers"]["hub"]["IPv6Address"] = "fd00::10/64"
        missing = inspected_network()
        missing["Containers"].pop("proxy")
        with self.assertRaises(network_contract.ContractError):
            network_contract.validate_endpoints(ipv6, contract(), require_reserved=True)
        with self.assertRaises(network_contract.ContractError):
            network_contract.validate_endpoints(
                missing, contract(), require_reserved=True
            )


class SafePreparationTests(unittest.TestCase):
    def test_idle_owned_network_with_old_options_can_be_removed(self) -> None:
        inspected = inspected_network("isolated")
        inspected["Containers"] = {}

        action, network_id = network_contract.prepare_action(
            inspected, contract("inhibit-ipv4"), (27, 1, 2)
        )

        self.assertEqual(action, "remove")
        self.assertEqual(network_id, "a" * 64)

    def test_drifted_network_with_endpoints_is_never_removed(self) -> None:
        inspected = inspected_network("isolated")

        with self.assertRaisesRegex(
            network_contract.ContractError, "attached containers"
        ):
            network_contract.prepare_action(
                inspected, contract("inhibit-ipv4"), (27, 1, 2)
            )

    def test_unowned_idle_network_is_never_removed(self) -> None:
        inspected = inspected_network("isolated")
        inspected["Containers"] = {}
        inspected["Labels"]["platform.managed"] = "false"

        with self.assertRaisesRegex(
            network_contract.ContractError, "cannot be removed automatically"
        ):
            network_contract.prepare_action(
                inspected, contract("inhibit-ipv4"), (27, 1, 2)
            )

    def test_current_network_is_kept(self) -> None:
        action, _ = network_contract.prepare_action(
            inspected_network(), contract(), (27, 1, 2)
        )
        self.assertEqual(action, "keep")


class LiveNetworkContractTests(unittest.TestCase):
    def test_host_bridge_allows_only_link_local_ipv6(self) -> None:
        bridge_name = "br-" + "a" * 12
        network_contract.validate_bridge_addresses(
            '[{"ifname":"%s","addr_info":[]}]' % bridge_name, bridge_name
        )
        network_contract.validate_bridge_addresses(
            '[{"ifname":"%s","addr_info":'
            '[{"family":"inet6","local":"fe80::1"}]}]' % bridge_name,
            bridge_name,
        )

    def test_host_bridge_rejects_ipv4_and_non_link_local_ipv6(self) -> None:
        bridge_name = "br-" + "a" * 12
        for address in (
            {"family": "inet", "local": "172.40.0.1"},
            {"family": "inet6", "local": "2001:db8::1"},
        ):
            with (
                self.subTest(address=address),
                self.assertRaises(network_contract.ContractError),
            ):
                network_contract.validate_bridge_addresses(
                    '[{"ifname":"%s","addr_info":%s}]'
                    % (bridge_name, json.dumps([address])),
                    bridge_name,
                )

    def test_probe_is_single_network_non_root_capability_free_and_read_only(
        self,
    ) -> None:
        command = network_contract.build_probe_command(
            "platform-jupyter-compose-production",
            "team-workspace-backend:production",
            Path("/tmp/private-probe/container-id"),
        )

        self.assertEqual(
            [item for item in command if item.startswith("--network=")],
            ["--network=platform-jupyter-compose-production"],
        )
        for required in (
            "--rm",
            "--pull=never",
            "--read-only",
            "--user=65534:65534",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--pids-limit=32",
        ):
            self.assertIn(required, command)
        self.assertFalse(any(item.startswith("--volume") for item in command))
        self.assertIn("--cidfile=/tmp/private-probe/container-id", command)
        self.assertIn("/proc/net/route", network_contract.PROBE_PROGRAM)
        self.assertIn("/proc/net/if_inet6", network_contract.PROBE_PROGRAM)


class ProductionOrchestrationContractTests(unittest.TestCase):
    def test_gateway_starts_only_after_live_network_validation(self) -> None:
        script = (ROOT / "scripts" / "production.sh").read_text(encoding="utf-8")

        core_up = script.index(
            "compose up -d --build --wait \\\n    api worker reconciler frontend egress-proxy jupyterhub"
        )
        live_check = script.index("production_network_contract validate", core_up)
        gateway_up = script.index(
            'start_gateway_checked || die "gateway did not start safely"', live_check
        )
        gateway_helper = script.split("start_gateway_checked() {", 1)[1].split(
            "\n}", 1
        )[0]
        self.assertLess(core_up, live_check)
        self.assertLess(live_check, gateway_up)
        self.assertIn("validate_gateway_health", gateway_helper)
        self.assertIn("stop_gateway_fail_closed", gateway_helper)

    def test_gateway_maintenance_paths_keep_the_same_fail_closed_gate(self) -> None:
        script = (ROOT / "scripts" / "production.sh").read_text(encoding="utf-8")
        recreate = script.split("recreate_gateway() {", 1)[1].split(
            "\ncreate_user() {", 1
        )[0]
        account_restart = script.split("restart_account_control_plane_checked() {", 1)[
            1
        ].split("\n}", 1)[0]

        self.assertLess(
            recreate.index("stop_gateway_or_die"),
            recreate.index("production_network_contract validate"),
        )
        self.assertLess(
            recreate.index("production_network_contract validate"),
            recreate.index("start_gateway_checked --force-recreate"),
        )
        self.assertLess(
            account_restart.index("start_existing_container_checked api"),
            account_restart.index("start_existing_container_checked jupyterhub"),
        )
        self.assertLess(
            account_restart.index("start_existing_container_checked jupyterhub"),
            account_restart.index("production_network_contract validate"),
        )
        self.assertLess(
            account_restart.index("production_network_contract validate"),
            account_restart.index("start_existing_container_checked gateway"),
        )
        self.assertLess(
            account_restart.index("start_existing_container_checked gateway"),
            account_restart.index("validate_gateway_health"),
        )
        self.assertIn("stop_existing_container_checked gateway", account_restart)
        self.assertNotIn("compose up", account_restart)

    def test_gateway_health_requires_both_public_hosts(self) -> None:
        script = (ROOT / "scripts" / "production.sh").read_text(encoding="utf-8")
        health = script.split("validate_gateway_health() {", 1)[1].split("\n}", 1)[0]

        self.assertEqual(health.count("curl --fail --silent --show-error"), 2)
        self.assertEqual(health.count("|| return 1"), 2)
        self.assertIn("https://platform.cyberailabs.team/healthz", health)
        self.assertIn("https://cyberailabs.team/healthz", health)

    def test_engine_27_overlay_replaces_driver_options(self) -> None:
        base = (ROOT / "compose.production.yaml").read_text(encoding="utf-8")
        overlay = (ROOT / "compose.production.docker27.yaml").read_text(
            encoding="utf-8"
        )

        self.assertIn("gateway_mode_ipv4: isolated", base)
        self.assertIn("gateway_mode_ipv6: isolated", base)
        self.assertIn("driver_opts: !override", overlay)
        self.assertIn('inhibit_ipv4: "true"', overlay)

    def test_worker_defines_compose_wait_compatible_liveness_check(self) -> None:
        compose = (ROOT / "compose.production.yaml").read_text(encoding="utf-8")
        worker = compose.split("\n  worker:\n", 1)[1].split("\n  reconciler:\n", 1)[0]

        self.assertNotIn("disable: true", worker)
        self.assertIn(
            'test: ["CMD", "python", "-c", "import os; os.kill(1, 0)"]',
            worker,
        )
        self.assertIn("interval: 10s", worker)
        self.assertIn("timeout: 3s", worker)
        self.assertIn("start_period: 5s", worker)
        self.assertIn("retries: 3", worker)

    @unittest.skipUnless(shutil.which("docker"), "Docker Compose CLI is unavailable")
    def test_engine_27_overlay_renders_exact_driver_option_set(self) -> None:
        compose_version = subprocess.run(
            ["docker", "compose", "version"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        if compose_version.returncode != 0:
            self.skipTest("Docker Compose CLI is unavailable")
        rendered = subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                ".env.production.example",
                "-f",
                "compose.production.yaml",
                "-f",
                "compose.production.docker27.yaml",
                "config",
                "--format",
                "json",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        configuration = json.loads(rendered.stdout)
        self.assertEqual(
            configuration["networks"]["jupyter"]["driver_opts"],
            network_contract.INHIBIT_IPV4_OPTIONS,
        )
        self.assertEqual(
            configuration["services"]["worker"]["healthcheck"],
            {
                "test": ["CMD", "python", "-c", "import os; os.kill(1, 0)"],
                "timeout": "3s",
                "interval": "10s",
                "retries": 3,
                "start_period": "5s",
            },
        )


if __name__ == "__main__":
    unittest.main()
