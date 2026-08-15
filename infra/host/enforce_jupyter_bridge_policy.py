#!/usr/bin/env python3
"""Restore the no-host-address invariant on the Docker execution bridge.

Docker 28's isolated gateway mode disables IPv6 on the bridge.  Some host
network managers can subsequently claim the Docker-created interface and add a
link-local address.  This root-owned step makes the exact platform bridge
unmanaged for the current network-manager lifetime and restores Docker's
per-interface IPv6 setting before the firewall health check is published.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path
from typing import Any

from hostlib import HostConfigError, load_config, require_commands, run


def validate_network_identity(value: Any, network: dict[str, Any]) -> None:
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise HostConfigError("unexpected Docker network inspect response")
    actual = value[0]
    options = actual.get("Options") or {}
    labels = actual.get("Labels") or {}
    if (
        actual.get("Name") != network["name"]
        or actual.get("Driver") != "bridge"
        or actual.get("Internal") is not True
        or actual.get("EnableIPv6") is not False
        or options.get("com.docker.network.bridge.name") != network["bridge_name"]
        or options.get("com.docker.network.bridge.gateway_mode_ipv4") != "isolated"
        or options.get("com.docker.network.bridge.gateway_mode_ipv6") != "isolated"
        or labels.get("platform.managed") != "true"
        or labels.get("platform.kind") != "jupyter-execution"
    ):
        raise HostConfigError("refusing to modify a bridge with unexpected identity")


def bridge_has_no_ip_addresses(value: Any) -> bool:
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        return False
    addresses = value[0].get("addr_info")
    return isinstance(addresses, list) and all(
        isinstance(item, dict) and item.get("family") not in {"inet", "inet6"}
        for item in addresses
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise HostConfigError("bridge host policy must run as root")
    config = load_config(args.config)
    require_commands(["docker", "ip", "sysctl"])
    network = config["network"]
    inspected = json.loads(
        run(["docker", "network", "inspect", network["name"]]).stdout
    )
    validate_network_identity(inspected, network)

    bridge = network["bridge_name"]
    if shutil.which("nmcli") is not None:
        managed = run(
            ["nmcli", "-g", "GENERAL.NM-MANAGED", "device", "show", bridge],
            check=False,
        )
        if managed.returncode == 0:
            run(["nmcli", "device", "set", bridge, "managed", "no"])

    run(["sysctl", "-q", "-w", f"net.ipv6.conf.{bridge}.disable_ipv6=1"])
    sysctl_path = Path("/proc/sys/net/ipv6/conf") / bridge / "disable_ipv6"
    try:
        disabled = sysctl_path.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise HostConfigError(f"cannot verify bridge IPv6 policy: {exc}") from exc
    if disabled != "1":
        raise HostConfigError("bridge IPv6 policy did not converge")

    addresses = json.loads(run(["ip", "-j", "address", "show", "dev", bridge]).stdout)
    if not bridge_has_no_ip_addresses(addresses):
        raise HostConfigError("isolated bridge still has a host IP address")
    print(bridge)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HostConfigError as exc:
        raise SystemExit(f"ERROR: {exc}")
