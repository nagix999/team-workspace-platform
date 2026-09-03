#!/usr/bin/env python3
"""Reject production Docker subnets that collide with host or foreign routes."""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import subprocess
import sys
from typing import Any, Sequence


TARGET_NETWORKS = {
    "jupyter": ipaddress.IPv4Network("172.29.0.0/24"),
    "ingress": ipaddress.IPv4Network("172.29.1.0/24"),
    "control": ipaddress.IPv4Network("172.29.2.0/24"),
    "edge": ipaddress.IPv4Network("172.29.3.0/24"),
    "egress-out": ipaddress.IPv4Network("172.29.4.0/24"),
}
SAFE_PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


class ConflictError(RuntimeError):
    pass


def _run(arguments: Sequence[str]) -> str:
    try:
        result = subprocess.run(
            list(arguments), check=False, capture_output=True, text=True, timeout=20
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise ConflictError(f"required command failed: {arguments[0]}") from exc
    if result.returncode != 0:
        raise ConflictError(f"required command failed: {arguments[0]}")
    return result.stdout


def _json(raw: str, label: str) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ConflictError(f"{label} returned invalid JSON") from exc


def _overlapping_targets(network: ipaddress.IPv4Network) -> set[str]:
    return {
        name for name, target in TARGET_NETWORKS.items() if network.overlaps(target)
    }


def validate_network_inventory(
    inventory: Any, compose_project: str
) -> dict[str, tuple[str, ipaddress.IPv4Network]]:
    if not isinstance(inventory, list):
        raise ConflictError("Docker network inventory is invalid")
    allowed_devices: dict[str, tuple[str, ipaddress.IPv4Network]] = {}
    seen_owned: set[str] = set()
    for item in inventory:
        if not isinstance(item, dict):
            raise ConflictError("Docker network inventory is invalid")
        labels = item.get("Labels")
        labels = labels if isinstance(labels, dict) else {}
        network_id = item.get("Id")
        driver = item.get("Driver")
        configs = (
            item.get("IPAM", {}).get("Config")
            if isinstance(item.get("IPAM"), dict)
            else None
        )
        if not isinstance(configs, list):
            configs = []
        for config in configs:
            if not isinstance(config, dict) or not isinstance(
                config.get("Subnet"), str
            ):
                continue
            try:
                subnet = ipaddress.ip_network(config["Subnet"], strict=True)
            except ValueError as exc:
                raise ConflictError(
                    "Docker network contains a non-canonical subnet"
                ) from exc
            if not isinstance(subnet, ipaddress.IPv4Network):
                continue
            overlaps = _overlapping_targets(subnet)
            if not overlaps:
                continue
            network_name = labels.get("com.docker.compose.network")
            expected_docker_name = (
                "platform-jupyter-compose-production"
                if network_name == "jupyter"
                else f"{compose_project}_{network_name}"
            )
            owned = (
                labels.get("com.docker.compose.project") == compose_project
                and isinstance(network_name, str)
                and network_name in TARGET_NETWORKS
                and subnet == TARGET_NETWORKS[network_name]
                and driver == "bridge"
                and isinstance(network_id, str)
                and re.fullmatch(r"[0-9a-f]{64}", network_id) is not None
                and item.get("Name") == expected_docker_name
            )
            if not owned:
                names = ",".join(sorted(overlaps))
                raise ConflictError(
                    f"Docker network overlaps reserved production subnet(s): {names}"
                )
            if network_name in seen_owned:
                raise ConflictError(f"duplicate managed Docker network: {network_name}")
            seen_owned.add(network_name)
            options = item.get("Options")
            explicit_bridge = (
                options.get("com.docker.network.bridge.name")
                if isinstance(options, dict)
                else None
            )
            if explicit_bridge is not None and (
                not isinstance(explicit_bridge, str)
                or re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", explicit_bridge) is None
            ):
                raise ConflictError("managed Docker bridge name is invalid")
            device = explicit_bridge or f"br-{network_id[:12]}"
            if device in allowed_devices:
                raise ConflictError("managed Docker networks share a bridge device")
            allowed_devices[device] = (network_name, subnet)
    return allowed_devices


def validate_host_routes(
    routes: Any,
    allowed_devices: dict[str, tuple[str, ipaddress.IPv4Network]],
) -> None:
    if not isinstance(routes, list):
        raise ConflictError("host route inventory is invalid")
    for item in routes:
        if not isinstance(item, dict):
            raise ConflictError("host route inventory is invalid")
        raw_destination = item.get("dst")
        if raw_destination in (None, "default"):
            continue
        if not isinstance(raw_destination, str):
            raise ConflictError("host route destination is invalid")
        try:
            destination = ipaddress.ip_network(raw_destination, strict=False)
        except ValueError as exc:
            raise ConflictError("host route destination is invalid") from exc
        if not isinstance(destination, ipaddress.IPv4Network):
            continue
        overlaps = _overlapping_targets(destination)
        if not overlaps:
            continue
        device = item.get("dev")
        if not isinstance(device, str) or device not in allowed_devices:
            names = ",".join(sorted(overlaps))
            raise ConflictError(
                f"host route overlaps reserved production subnet(s): {names}"
            )
        network_name, expected = allowed_devices[device]
        route_type = item.get("type", "unicast")
        # Ownership/conflict checking runs before the execution-network drift
        # repair. Its exact owned route may therefore still exist here; the
        # subsequent prepare action removes an idle jupyter network whose
        # isolated bridge has IPv4. Broader or foreign routes remain fatal.
        if destination == expected and route_type == "unicast":
            continue
        if route_type in {"local", "broadcast"} and destination.subnet_of(expected):
            continue
        raise ConflictError(
            f"managed Docker bridge has an unexpected route in {expected}"
        )


def check(compose_project: str) -> None:
    if SAFE_PROJECT_RE.fullmatch(compose_project) is None:
        raise ConflictError("Compose project name is invalid")
    ids = _run(["docker", "network", "ls", "--quiet", "--no-trunc"]).splitlines()
    if any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in ids):
        raise ConflictError("Docker network ID inventory is invalid")
    inventory = (
        []
        if not ids
        else _json(
            _run(["docker", "network", "inspect", *ids]), "Docker network inspect"
        )
    )
    devices = validate_network_inventory(inventory, compose_project)
    routes = _json(
        _run(["ip", "-j", "-4", "route", "show", "table", "all"]),
        "host route inspection",
    )
    validate_host_routes(routes, devices)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compose-project", required=True)
    arguments = parser.parse_args(argv)
    try:
        check(arguments.compose_project)
    except ConflictError as exc:
        print(f"production-subnets: {exc}", file=sys.stderr)
        return 1
    print("production subnet conflict check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
