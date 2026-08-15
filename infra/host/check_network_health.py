#!/usr/bin/env python3
"""Verify execution-network invariants and atomically publish network_health.json."""

from __future__ import annotations

import argparse
import ipaddress
import json
import shlex
import sys
from pathlib import Path
from typing import Callable

from hostlib import (
    HostConfigError,
    load_config,
    require_commands,
    run,
    write_health_manifest,
)


CHAIN = "PLATFORM-JUPYTER"
EGRESS_CHAIN = "PLATFORM-JUPYTER-EGRESS"
FORBIDDEN_EGRESS_V4 = (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.0.2.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "198.51.100.0/24",
    "203.0.113.0/24",
    "224.0.0.0/4",
    "240.0.0.0/4",
)
CHECK_NAMES = (
    "docker_version_exact",
    "network_exists",
    "internal",
    "ipv6_disabled",
    "icc_disabled",
    "isolated_gateway",
    "subnet_exact",
    "ip_range_exact",
    "aux_addresses_exact",
    "bridge_has_no_ip",
    "firewall_policy_exact",
    "no_unexpected_published_ports",
    "singleuser_runtime_policy",
)


def iptables_check(chain: str, rule: list[str]) -> bool:
    return (
        run(["iptables", "-w", "10", "-C", chain, *rule], check=False).returncode == 0
    )


def normalize_iptables_rule(tokens: list[str]) -> list[str]:
    """Normalize iptables-save spelling without weakening rule semantics.

    iptables-nft prints host addresses with an explicit /32, injects the
    protocol's match module, and may reorder a comma-separated conntrack state
    set. The kernel treats those forms identically. All other tokens, including
    chain, rule order, protocol, ports, and jump targets, remain exact.
    """

    normalized: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in {"-s", "--source", "-d", "--destination"}:
            if index + 1 >= len(tokens):
                raise HostConfigError("iptables address option is missing a value")
            try:
                network = ipaddress.ip_network(tokens[index + 1], strict=False)
            except ValueError as exc:
                raise HostConfigError(
                    "iptables rule contains an invalid address"
                ) from exc
            if network.version != 4:
                raise HostConfigError("platform iptables policy must remain IPv4-only")
            normalized.extend(
                ["-s" if token in {"-s", "--source"} else "-d", str(network)]
            )
            index += 2
            continue
        if token == "-m" and index + 1 < len(tokens) and tokens[index + 1] == "tcp":
            # `-p tcp` loads this match implicitly; iptables-nft emits it on -S.
            index += 2
            continue
        if token == "--ctstate":
            if index + 1 >= len(tokens):
                raise HostConfigError("iptables conntrack state is missing")
            states = tokens[index + 1].split(",")
            if not states or any(not state for state in states):
                raise HostConfigError("iptables conntrack state is invalid")
            normalized.extend([token, ",".join(sorted(states))])
            index += 2
            continue
        normalized.append(token)
        index += 1
    return normalized


def normalized_iptables_rules(output: str) -> list[list[str]]:
    return [
        normalize_iptables_rule(shlex.split(line))
        for line in output.splitlines()
        if line.startswith("-A ")
    ]


def bridge_has_no_ip_addresses(value: object) -> bool:
    """Require Docker's isolated bridge to have no IPv4 or IPv6 address."""

    if not isinstance(value, list) or len(value) != 1:
        return False
    info = value[0]
    if not isinstance(info, dict):
        return False
    addresses = info.get("addr_info", [])
    if not isinstance(addresses, list):
        return False
    return all(
        isinstance(item, dict) and item.get("family") not in {"inet", "inet6"}
        for item in addresses
    )


def expected_firewall_rules(network: dict) -> list[list[str]]:
    return [
        ["-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
        [
            "-s",
            network["hub_ip"],
            "-d",
            network["dynamic_ip_range"],
            "-p",
            "tcp",
            "--dport",
            str(network["singleuser_port"]),
            "-j",
            "ACCEPT",
        ],
        [
            "-s",
            network["dynamic_ip_range"],
            "-d",
            network["hub_ip"],
            "-p",
            "tcp",
            "--dport",
            str(network["hub_api_port"]),
            "-j",
            "ACCEPT",
        ],
        [
            "-s",
            network["dynamic_ip_range"],
            "-d",
            network["egress_proxy_ip"],
            "-p",
            "tcp",
            "--dport",
            str(network["egress_proxy_port"]),
            "-j",
            "ACCEPT",
        ],
        ["-s", network["dynamic_ip_range"], "-j", "DROP"],
        ["-d", network["dynamic_ip_range"], "-j", "DROP"],
        ["-j", "RETURN"],
    ]


def expected_egress_firewall_rules(network: dict) -> list[list[str]]:
    return [
        [
            "-s",
            network["egress_out_proxy_ip"],
            "-d",
            destination,
            "-j",
            "DROP",
        ]
        for destination in FORBIDDEN_EGRESS_V4
    ] + [["-j", "RETURN"]]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    config = load_config(args.config)
    require_commands(["docker", "ip", "iptables"])
    network = config["network"]
    checks = {name: False for name in CHECK_NAMES}
    errors: list[str] = []

    def check(name: str, callback: Callable[[], bool]) -> None:
        try:
            checks[name] = callback() is True
            if not checks[name]:
                errors.append(f"{name}: condition is false")
        except Exception as exc:
            errors.append(f"{name}: {exc}")

    def docker_version_exact() -> bool:
        actual = run(
            ["docker", "version", "--format", "{{.Server.Version}}"]
        ).stdout.strip()
        expected = config["docker"]["engine_version"]
        return expected == actual or (
            config["environment"] == "local-dev"
            and expected == "ALLOW_CURRENT_LOCAL_DEV"
        )

    inspected: list[dict] = []

    def inspect_network() -> dict:
        if not inspected:
            value = json.loads(
                run(["docker", "network", "inspect", network["name"]]).stdout
            )
            if not isinstance(value, list) or len(value) != 1:
                raise HostConfigError("unexpected docker network inspect response")
            inspected.append(value[0])
        return inspected[0]

    check("docker_version_exact", docker_version_exact)
    check("network_exists", lambda: inspect_network().get("Name") == network["name"])
    check("internal", lambda: inspect_network().get("Internal") is True)
    check("ipv6_disabled", lambda: inspect_network().get("EnableIPv6") is False)
    check(
        "icc_disabled",
        lambda: inspect_network()
        .get("Options", {})
        .get("com.docker.network.bridge.enable_icc")
        == "false",
    )
    check(
        "isolated_gateway",
        lambda: inspect_network()
        .get("Options", {})
        .get("com.docker.network.bridge.gateway_mode_ipv4")
        == "isolated"
        and inspect_network()
        .get("Options", {})
        .get("com.docker.network.bridge.gateway_mode_ipv6")
        == "isolated",
    )

    def first_ipam() -> dict:
        values = inspect_network().get("IPAM", {}).get("Config", [])
        if not isinstance(values, list) or len(values) != 1:
            raise HostConfigError("network must contain exactly one IPAM config")
        return values[0]

    check("subnet_exact", lambda: first_ipam().get("Subnet") == network["subnet"])
    check(
        "ip_range_exact",
        lambda: first_ipam().get("IPRange") == network["dynamic_ip_range"],
    )
    check(
        "aux_addresses_exact",
        # Static service IPs are outside dynamic_ip_range and are assigned by
        # Compose. IPAM aux-addresses would reserve them and can prevent attach.
        lambda: not (first_ipam().get("AuxiliaryAddresses") or {}),
    )

    def bridge_has_no_ip() -> bool:
        value = json.loads(
            run(["ip", "-j", "address", "show", "dev", network["bridge_name"]]).stdout
        )
        return bridge_has_no_ip_addresses(value)

    check("bridge_has_no_ip", bridge_has_no_ip)

    def firewall_policy_exact() -> bool:
        source_jump = ["-s", network["subnet"], "-j", CHAIN]
        destination_jump = ["-d", network["subnet"], "-j", CHAIN]
        egress_jump = ["-s", network["egress_out_proxy_ip"], "-j", EGRESS_CHAIN]
        user_lines = normalized_iptables_rules(
            run(["iptables", "-w", "10", "-S", "DOCKER-USER"]).stdout
        )
        expected_source = normalize_iptables_rule(["-A", "DOCKER-USER", *source_jump])
        expected_destination = normalize_iptables_rule(
            ["-A", "DOCKER-USER", *destination_jump]
        )
        expected_egress = normalize_iptables_rule(["-A", "DOCKER-USER", *egress_jump])
        if (
            user_lines.count(expected_source) != 1
            or user_lines.count(expected_destination) != 1
            or user_lines.count(expected_egress) != 1
            or not (
                user_lines.index(expected_source)
                < user_lines.index(expected_destination)
                < user_lines.index(expected_egress)
            )
        ):
            return False
        actual_chain = normalized_iptables_rules(
            run(["iptables", "-w", "10", "-S", CHAIN]).stdout
        )
        expected_chain = [
            normalize_iptables_rule(["-A", CHAIN, *rule])
            for rule in expected_firewall_rules(network)
        ]
        actual_egress_chain = normalized_iptables_rules(
            run(["iptables", "-w", "10", "-S", EGRESS_CHAIN]).stdout
        )
        expected_egress_chain = [
            normalize_iptables_rule(["-A", EGRESS_CHAIN, *rule])
            for rule in expected_egress_firewall_rules(network)
        ]
        return (
            actual_chain == expected_chain
            and actual_egress_chain == expected_egress_chain
        )

    check("firewall_policy_exact", firewall_policy_exact)

    def no_unexpected_published_ports() -> bool:
        ids = run(
            [
                "docker",
                "ps",
                "-aq",
                "--filter",
                "label=platform.kind=jupyter-singleuser",
            ]
        ).stdout.split()
        if not ids:
            return True
        values = json.loads(run(["docker", "inspect", *ids]).stdout)
        return all(
            not (container.get("HostConfig", {}).get("PortBindings") or {})
            and container.get("HostConfig", {}).get("NetworkMode") == network["name"]
            and container.get("HostConfig", {}).get("Privileged") is False
            for container in values
        )

    check("no_unexpected_published_ports", no_unexpected_published_ports)

    def singleuser_runtime_policy() -> bool:
        ids = run(
            [
                "docker",
                "ps",
                "-q",
                "--filter",
                "label=platform.kind=jupyter-singleuser",
            ]
        ).stdout.split()
        if not ids:
            return True
        values = json.loads(run(["docker", "inspect", *ids]).stdout)
        for container in values:
            host = container.get("HostConfig", {})
            user = str(container.get("Config", {}).get("User", ""))
            security = {str(value) for value in (host.get("SecurityOpt") or [])}
            cap_drop = {str(value).upper() for value in (host.get("CapDrop") or [])}
            mounts = container.get("Mounts") or []
            if (
                not user
                or user.split(":", 1)[0] in {"0", "root"}
                or host.get("Privileged") is not False
                or host.get("ReadonlyRootfs") is not True
                or "ALL" not in cap_drop
                or not any(value.startswith("no-new-privileges") for value in security)
                or int(host.get("PidsLimit") or 0) <= 0
                or int(host.get("Memory") or 0) <= 0
                or (
                    int(host.get("CpuQuota") or 0) <= 0
                    and int(host.get("NanoCpus") or 0) <= 0
                )
                or host.get("NetworkMode") != network["name"]
                or host.get("PortBindings")
                or any(mount.get("Type") != "volume" for mount in mounts)
                or any(
                    mount.get("Source") == "/var/run/docker.sock" for mount in mounts
                )
            ):
                return False
        return True

    check("singleuser_runtime_policy", singleuser_runtime_policy)
    destination = write_health_manifest(config, "network", checks)
    for error in errors:
        print(error, file=sys.stderr)
    print(destination)
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HostConfigError as exc:
        raise SystemExit(f"ERROR: {exc}")
