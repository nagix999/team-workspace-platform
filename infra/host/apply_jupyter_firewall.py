#!/usr/bin/env python3
"""Install the reviewed IPv4 policy in Docker's supported DOCKER-USER chain."""

from __future__ import annotations

import argparse
from pathlib import Path

from hostlib import HostConfigError, load_config, require_commands, run


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


def iptables(*args: str, check: bool = True):
    return run(["iptables", "-w", "10", *args], check=check)


def delete_all(rule: list[str]) -> None:
    while iptables("-C", "DOCKER-USER", *rule, check=False).returncode == 0:
        iptables("-D", "DOCKER-USER", *rule)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    config = load_config(args.config)
    if config["docker"]["firewall_backend"] != "iptables":
        raise HostConfigError("apply_jupyter_firewall requires firewall_backend=iptables")
    require_commands(["iptables"])
    network = config["network"]
    if iptables("-S", "DOCKER-USER", check=False).returncode != 0:
        raise HostConfigError("DOCKER-USER chain is absent; Docker firewall is not ready")
    if iptables("-S", CHAIN, check=False).returncode != 0:
        iptables("-N", CHAIN)
    if iptables("-S", EGRESS_CHAIN, check=False).returncode != 0:
        iptables("-N", EGRESS_CHAIN)
    iptables("-F", CHAIN)
    iptables("-F", EGRESS_CHAIN)
    rules = [
        ["-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
        [
            "-s", network["hub_ip"], "-d", network["dynamic_ip_range"],
            "-p", "tcp", "--dport", str(network["singleuser_port"]), "-j", "ACCEPT",
        ],
        [
            "-s", network["dynamic_ip_range"], "-d", network["hub_ip"],
            "-p", "tcp", "--dport", str(network["hub_api_port"]), "-j", "ACCEPT",
        ],
        [
            "-s", network["dynamic_ip_range"], "-d", network["egress_proxy_ip"],
            "-p", "tcp", "--dport", str(network["egress_proxy_port"]), "-j", "ACCEPT",
        ],
        ["-s", network["dynamic_ip_range"], "-j", "DROP"],
        ["-d", network["dynamic_ip_range"], "-j", "DROP"],
        ["-j", "RETURN"],
    ]
    for rule in rules:
        iptables("-A", CHAIN, *rule)
    for destination in FORBIDDEN_EGRESS_V4:
        iptables(
            "-A",
            EGRESS_CHAIN,
            "-s",
            network["egress_out_proxy_ip"],
            "-d",
            destination,
            "-j",
            "DROP",
        )
    iptables("-A", EGRESS_CHAIN, "-j", "RETURN")
    source_jump = ["-s", network["subnet"], "-j", CHAIN]
    destination_jump = ["-d", network["subnet"], "-j", CHAIN]
    egress_jump = ["-s", network["egress_out_proxy_ip"], "-j", EGRESS_CHAIN]
    delete_all(source_jump)
    delete_all(destination_jump)
    delete_all(egress_jump)
    # Insert in reverse order so execution source/destination are evaluated
    # before the proxy's separate outbound private-address deny list.
    iptables("-I", "DOCKER-USER", "1", *egress_jump)
    iptables("-I", "DOCKER-USER", "1", *destination_jump)
    iptables("-I", "DOCKER-USER", "1", *source_jump)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HostConfigError as exc:
        raise SystemExit(f"ERROR: {exc}")
