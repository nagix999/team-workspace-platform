#!/usr/bin/env python3
"""Create the external isolated bridge once; never mutate an existing network."""

from __future__ import annotations

import argparse
from pathlib import Path

from hostlib import HostConfigError, load_config, require_commands, run


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    config = load_config(args.config)
    require_commands(["docker"])
    network = config["network"]
    inspected = run(["docker", "network", "inspect", network["name"]], check=False)
    if inspected.returncode == 0:
        raise HostConfigError(
            "network already exists; run check_network_health.py and replace it only "
            "during an audited maintenance window"
        )
    command = [
        "docker",
        "network",
        "create",
        "--driver=bridge",
        "--internal",
        "--ipv6=false",
        f"--subnet={network['subnet']}",
        f"--ip-range={network['dynamic_ip_range']}",
        "--opt",
        f"com.docker.network.bridge.name={network['bridge_name']}",
        "--opt",
        "com.docker.network.bridge.enable_icc=false",
        "--opt",
        "com.docker.network.bridge.gateway_mode_ipv4=isolated",
        "--opt",
        "com.docker.network.bridge.gateway_mode_ipv6=isolated",
        "--label",
        "platform.managed=true",
        "--label",
        "platform.kind=jupyter-execution",
        network["name"],
    ]
    created = run(command)
    print(created.stdout.strip())
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HostConfigError as exc:
        raise SystemExit(f"ERROR: {exc}")
