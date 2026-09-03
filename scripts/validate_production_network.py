#!/usr/bin/env python3
"""Fail-closed checks for the firewall-free production execution network."""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import secrets
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


MINIMUM_ENGINE_VERSION = (27, 1, 2)
ISOLATED_MINIMUM_ENGINE_VERSION = (28, 0, 0)
STABLE_VERSION_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:\+[0-9A-Za-z][0-9A-Za-z._-]*|-[0-9][0-9A-Za-z._+~:-]*)?$"
)
NETWORK_ID_RE = re.compile(r"^[0-9a-f]{64}$")
INHIBIT_IPV4_OPTIONS = {
    "com.docker.network.bridge.enable_icc": "true",
    "com.docker.network.bridge.inhibit_ipv4": "true",
}
ISOLATED_OPTIONS = {
    "com.docker.network.bridge.enable_icc": "true",
    "com.docker.network.bridge.gateway_mode_ipv4": "isolated",
    "com.docker.network.bridge.gateway_mode_ipv6": "isolated",
}
PLATFORM_LABELS = {
    "platform.managed": "true",
    "platform.kind": "jupyter-execution",
    "platform.network.policy": "compose-internal-trusted-v1",
}
REQUIRED_COMPOSE_LABEL_KEYS = {
    "com.docker.compose.network",
    "com.docker.compose.project",
    "com.docker.compose.version",
}
OPTIONAL_COMPOSE_LABEL_KEYS = {"com.docker.compose.config-hash"}
CONFIG_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
PROBE_SUCCESS = "production-network-probe-ok"


class ContractError(RuntimeError):
    """The live Docker network does not meet the reviewed contract."""


@dataclass(frozen=True)
class NetworkContract:
    network_name: str
    compose_project: str
    isolation_mode: str
    subnet: ipaddress.IPv4Network
    ip_range: ipaddress.IPv4Network
    required_endpoints: frozenset[ipaddress.IPv4Address]


def parse_stable_version(raw_version: Any) -> tuple[int, int, int]:
    if not isinstance(raw_version, str):
        raise ContractError("Docker Server Engine version is invalid")
    matched = STABLE_VERSION_RE.fullmatch(raw_version)
    if matched is None:
        raise ContractError("Docker Server Engine version is invalid")
    version = tuple(int(part) for part in matched.groups()[:3])
    if version < MINIMUM_ENGINE_VERSION:
        raise ContractError("Docker Server Engine 27.1.2 or newer is required")
    return version  # type: ignore[return-value]


def _run(
    arguments: Sequence[str],
    *,
    timeout: int = 15,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            list(arguments),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise ContractError(f"required command is unavailable: {arguments[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ContractError(f"command timed out: {arguments[0]}") from exc
    if check and completed.returncode != 0:
        detail = completed.stderr.strip().replace("\n", " ")[:800]
        suffix = f": {detail}" if detail else ""
        raise ContractError(
            f"command failed ({arguments[0]}, exit {completed.returncode}){suffix}"
        )
    return completed


def _load_json(raw: str, label: str) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ContractError(f"{label} returned invalid JSON") from exc


def inspect_docker_server() -> tuple[int, int, int]:
    completed = _run(["docker", "version", "--format", "{{json .Server}}"], timeout=10)
    server = _load_json(completed.stdout, "Docker Server Engine")
    if not isinstance(server, dict) or server.get("Os") != "linux":
        raise ContractError("Docker Server Engine must be Linux")
    return parse_stable_version(server.get("Version"))


def inspect_network_optional(network_name: str) -> dict[str, Any] | None:
    completed = _run(
        ["docker", "network", "inspect", network_name], timeout=10, check=False
    )
    if completed.returncode == 0:
        payload = _load_json(completed.stdout, "Docker network inspect")
        if not isinstance(payload, list) or len(payload) != 1:
            raise ContractError("Docker network inspect returned an invalid result")
        inspected = payload[0]
        if not isinstance(inspected, dict):
            raise ContractError("Docker network inspect returned an invalid object")
        return inspected

    inventory = _run(["docker", "network", "ls", "--format", "{{.Name}}"], timeout=10)
    if network_name in inventory.stdout.splitlines():
        raise ContractError(
            "Docker network exists but could not be inspected; refusing to continue"
        )
    return None


def _expected_options(
    isolation_mode: str, docker_version: tuple[int, int, int]
) -> dict[str, str]:
    if isolation_mode == "inhibit-ipv4":
        if not (MINIMUM_ENGINE_VERSION <= docker_version < (28, 0, 0)):
            raise ContractError(
                "the inhibit-ipv4 overlay is permitted only on Docker Engine 27"
            )
        return INHIBIT_IPV4_OPTIONS
    if isolation_mode == "isolated":
        if docker_version < ISOLATED_MINIMUM_ENGINE_VERSION:
            raise ContractError(
                "isolated gateway mode requires Docker Engine 28 or newer"
            )
        return ISOLATED_OPTIONS
    raise ContractError("production network isolation mode is invalid")


def validate_managed_identity(inspected: Any, contract: NetworkContract) -> str:
    if not isinstance(inspected, dict):
        raise ContractError("Docker execution network inspect is invalid")
    if (
        inspected.get("Name") != contract.network_name
        or inspected.get("Driver") != "bridge"
        or inspected.get("Scope") != "local"
        or inspected.get("Internal") is not True
        or inspected.get("EnableIPv6") is not False
        or inspected.get("Attachable") is not False
        or inspected.get("Ingress") is not False
    ):
        raise ContractError("Docker execution network identity is not managed")

    network_id = inspected.get("Id")
    if not isinstance(network_id, str) or NETWORK_ID_RE.fullmatch(network_id) is None:
        raise ContractError("Docker execution network ID is invalid")

    labels = inspected.get("Labels")
    if not isinstance(labels, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in labels.items()
    ):
        raise ContractError("Docker execution network labels are invalid")
    platform_labels = {
        key: value for key, value in labels.items() if key.startswith("platform.")
    }
    if platform_labels != PLATFORM_LABELS:
        raise ContractError("Docker execution network ownership labels drifted")
    compose_labels = {
        key: value
        for key, value in labels.items()
        if key.startswith("com.docker.compose.")
    }
    compose_label_keys = set(compose_labels)
    if not (
        compose_label_keys == REQUIRED_COMPOSE_LABEL_KEYS
        or compose_label_keys
        == REQUIRED_COMPOSE_LABEL_KEYS | OPTIONAL_COMPOSE_LABEL_KEYS
    ):
        raise ContractError("Docker execution network Compose labels drifted")
    if (
        compose_labels.get("com.docker.compose.network") != "jupyter"
        or compose_labels.get("com.docker.compose.project") != contract.compose_project
        or STABLE_VERSION_RE.fullmatch(
            compose_labels.get("com.docker.compose.version", "")
        )
        is None
    ):
        raise ContractError("Docker execution network Compose labels drifted")
    config_hash = compose_labels.get("com.docker.compose.config-hash")
    if config_hash is not None and CONFIG_HASH_RE.fullmatch(config_hash) is None:
        raise ContractError("Docker execution network Compose labels drifted")
    if any(
        not key.startswith("platform.") and not key.startswith("com.docker.compose.")
        for key in labels
    ):
        raise ContractError("Docker execution network has an unreviewed label")
    return network_id


def _canonical_ipv4_network(value: Any, label: str) -> ipaddress.IPv4Network:
    if not isinstance(value, str):
        raise ContractError(f"{label} is invalid")
    try:
        network = ipaddress.ip_network(value, strict=True)
    except ValueError as exc:
        raise ContractError(f"{label} is invalid") from exc
    if not isinstance(network, ipaddress.IPv4Network):
        raise ContractError(f"{label} must be IPv4")
    return network


def validate_network_configuration(
    inspected: dict[str, Any],
    contract: NetworkContract,
    docker_version: tuple[int, int, int],
) -> str:
    network_id = validate_managed_identity(inspected, contract)
    if inspected.get("Options") != _expected_options(
        contract.isolation_mode, docker_version
    ):
        raise ContractError("Docker execution network driver options drifted")

    ipam = inspected.get("IPAM")
    if not isinstance(ipam, dict):
        raise ContractError("Docker execution network IPAM is invalid")
    if ipam.get("Driver") != "default" or ipam.get("Options") not in (None, {}):
        raise ContractError("Docker execution network IPAM driver drifted")
    configs = ipam.get("Config")
    if not isinstance(configs, list) or len(configs) != 1:
        raise ContractError("Docker execution network IPAM is invalid")
    config = configs[0]
    if not isinstance(config, dict) or set(config) != {"Subnet", "IPRange"}:
        raise ContractError("Docker execution network IPAM must not contain a gateway")
    if (
        _canonical_ipv4_network(config.get("Subnet"), "Docker network subnet")
        != contract.subnet
        or _canonical_ipv4_network(config.get("IPRange"), "Docker network IP range")
        != contract.ip_range
    ):
        raise ContractError("Docker execution network IPAM ranges drifted")
    return network_id


def validate_endpoints(
    inspected: dict[str, Any], contract: NetworkContract, *, require_reserved: bool
) -> None:
    endpoints = inspected.get("Containers")
    if not isinstance(endpoints, dict):
        raise ContractError("Docker execution network endpoints are invalid")
    ipv4_addresses: set[ipaddress.IPv4Address] = set()
    for endpoint in endpoints.values():
        if not isinstance(endpoint, dict):
            raise ContractError("Docker execution network endpoint is invalid")
        if endpoint.get("IPv6Address") != "":
            raise ContractError("Docker execution network endpoint has IPv6 enabled")
        raw_ipv4 = endpoint.get("IPv4Address")
        if not isinstance(raw_ipv4, str):
            raise ContractError("Docker execution network endpoint IPv4 is invalid")
        try:
            address = ipaddress.ip_interface(raw_ipv4).ip
        except ValueError as exc:
            raise ContractError(
                "Docker execution network endpoint IPv4 is invalid"
            ) from exc
        if not isinstance(address, ipaddress.IPv4Address):
            raise ContractError("Docker execution network endpoint IPv4 is invalid")
        ipv4_addresses.add(address)
    if require_reserved and not contract.required_endpoints.issubset(ipv4_addresses):
        raise ContractError(
            "JupyterHub and egress proxy are not attached at their reserved IPv4 addresses"
        )


def prepare_action(
    inspected: dict[str, Any],
    contract: NetworkContract,
    docker_version: tuple[int, int, int],
) -> tuple[str, str]:
    """Return (keep|remove, network ID), refusing unsafe automatic removal."""

    try:
        network_id = validate_network_configuration(inspected, contract, docker_version)
    except ContractError as drift:
        try:
            network_id = validate_managed_identity(inspected, contract)
        except ContractError as ownership_error:
            raise ContractError(
                f"existing execution network drifted ({drift}) and cannot be "
                f"removed automatically ({ownership_error})"
            ) from ownership_error
        endpoints = inspected.get("Containers")
        if not isinstance(endpoints, dict):
            raise ContractError(
                "drifted execution network has invalid endpoint inventory; "
                "refusing automatic removal"
            )
        if endpoints:
            names = sorted(
                str(endpoint.get("Name", "<unnamed>"))
                for endpoint in endpoints.values()
                if isinstance(endpoint, dict)
            )
            detail = ", ".join(names) if names else "unknown endpoints"
            raise ContractError(
                "drifted execution network still has attached containers "
                f"({detail}); stop and remove them, then rerun production preflight"
            )
        return "remove", network_id

    validate_endpoints(inspected, contract, require_reserved=False)
    return "keep", network_id


def validate_bridge_addresses(raw_json: str, bridge_name: str) -> None:
    payload = _load_json(raw_json, "host bridge address inspection")
    if not isinstance(payload, list) or len(payload) != 1:
        raise ContractError("host execution bridge does not exist exactly once")
    bridge = payload[0]
    if not isinstance(bridge, dict) or bridge.get("ifname") != bridge_name:
        raise ContractError("host execution bridge identity drifted")
    addresses = bridge.get("addr_info")
    if not isinstance(addresses, list):
        raise ContractError("host execution bridge address inventory is invalid")
    for address in addresses:
        if not isinstance(address, dict):
            raise ContractError("host execution bridge address is invalid")
        family = address.get("family")
        local = address.get("local")
        if family == "inet":
            raise ContractError("host execution bridge unexpectedly has IPv4")
        if family == "inet6":
            try:
                parsed = ipaddress.IPv6Address(local)
            except (ValueError, TypeError) as exc:
                raise ContractError("host execution bridge IPv6 is invalid") from exc
            if not parsed.is_link_local:
                raise ContractError("host execution bridge has non-link-local IPv6")


def validate_host_bridge(network_id: str) -> None:
    bridge_name = f"br-{network_id[:12]}"
    bridge = _run(["ip", "-j", "address", "show", "dev", bridge_name], timeout=10)
    validate_bridge_addresses(bridge.stdout, bridge_name)


PROBE_PROGRAM = r"""
import os
from pathlib import Path

if os.geteuid() == 0 or os.getegid() == 0:
    raise SystemExit("probe unexpectedly runs as root")

status = {}
for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
    if ":" in line:
        key, value = line.split(":", 1)
        status[key] = value.strip()
for capability in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"):
    if int(status.get(capability, "-1"), 16) != 0:
        raise SystemExit("probe unexpectedly has Linux capabilities")
if status.get("NoNewPrivs") != "1":
    raise SystemExit("probe does not have no-new-privileges")

interfaces = {path.name for path in Path("/sys/class/net").iterdir()}
if interfaces != {"eth0", "lo"}:
    raise SystemExit("probe has an unexpected network attachment")

for line in Path("/proc/net/route").read_text(encoding="ascii").splitlines()[1:]:
    fields = line.split()
    if len(fields) >= 8 and fields[0] == "eth0" and fields[1] == fields[7] == "00000000":
        raise SystemExit("probe has an IPv4 default route")

for line in Path("/proc/net/ipv6_route").read_text(encoding="ascii").splitlines():
    fields = line.split()
    if len(fields) >= 10 and fields[0] == "0" * 32 and fields[1] == "00" and fields[-1] == "eth0":
        raise SystemExit("probe has an IPv6 default route")

for line in Path("/proc/net/if_inet6").read_text(encoding="ascii").splitlines():
    if line.split()[-1:] == ["eth0"]:
        raise SystemExit("probe eth0 unexpectedly has IPv6")

print("production-network-probe-ok")
""".strip()


def build_probe_command(
    network_name: str, probe_image: str, cidfile: Path
) -> list[str]:
    return [
        "docker",
        "run",
        "--rm",
        "--pull=never",
        f"--cidfile={cidfile}",
        f"--label=platform.probe.nonce={secrets.token_hex(16)}",
        f"--network={network_name}",
        "--read-only",
        "--user=65534:65534",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--pids-limit=32",
        "--entrypoint=python",
        probe_image,
        "-I",
        "-c",
        PROBE_PROGRAM,
    ]


def run_probe(network_name: str, probe_image: str) -> None:
    with tempfile.TemporaryDirectory(prefix="platform-network-probe-") as directory:
        cidfile = Path(directory) / "container-id"
        try:
            completed = _run(
                build_probe_command(network_name, probe_image, cidfile),
                timeout=30,
            )
        finally:
            try:
                container_id = cidfile.read_text(encoding="ascii").strip()
            except (FileNotFoundError, OSError, UnicodeError):
                container_id = ""
            if NETWORK_ID_RE.fullmatch(container_id) is not None:
                try:
                    _run(
                        ["docker", "rm", "--force", container_id],
                        timeout=10,
                        check=False,
                    )
                except ContractError:
                    pass
    if completed.stdout.strip() != PROBE_SUCCESS:
        raise ContractError("execution-network probe returned an invalid result")


def prepare_network(
    contract: NetworkContract, docker_version: tuple[int, int, int]
) -> None:
    _expected_options(contract.isolation_mode, docker_version)
    inspected = inspect_network_optional(contract.network_name)
    if inspected is None:
        print("production execution network is absent and will be created")
        return
    action, network_id = prepare_action(inspected, contract, docker_version)
    if action == "remove":
        _run(["docker", "network", "rm", network_id], timeout=15)
        print("removed an idle, managed execution network with stale options")
        return
    validate_host_bridge(network_id)
    print("existing production execution network contract is current")


def validate_live_network(
    contract: NetworkContract,
    docker_version: tuple[int, int, int],
    probe_image: str,
) -> None:
    inspected = inspect_network_optional(contract.network_name)
    if inspected is None:
        raise ContractError("production execution network does not exist")
    network_id = validate_network_configuration(inspected, contract, docker_version)
    validate_endpoints(inspected, contract, require_reserved=True)
    validate_host_bridge(network_id)
    run_probe(contract.network_name, probe_image)
    print("production execution network live contract passed")


def _parse_contract(arguments: argparse.Namespace) -> NetworkContract:
    subnet = _canonical_ipv4_network(arguments.subnet, "expected subnet")
    ip_range = _canonical_ipv4_network(arguments.ip_range, "expected IP range")
    if not ip_range.subnet_of(subnet):
        raise ContractError("expected IP range is outside the expected subnet")
    endpoints: set[ipaddress.IPv4Address] = set()
    for raw_endpoint in arguments.required_endpoint:
        try:
            endpoint = ipaddress.ip_address(raw_endpoint)
        except ValueError as exc:
            raise ContractError("required endpoint address is invalid") from exc
        if not isinstance(endpoint, ipaddress.IPv4Address) or endpoint not in subnet:
            raise ContractError("required endpoint must be IPv4 inside the subnet")
        if endpoint in ip_range:
            raise ContractError(
                "required endpoint must be outside the dynamic IP range"
            )
        endpoints.add(endpoint)
    if len(endpoints) != len(arguments.required_endpoint):
        raise ContractError("required endpoint addresses must be unique")
    return NetworkContract(
        network_name=arguments.network_name,
        compose_project=arguments.compose_project,
        isolation_mode=arguments.isolation_mode,
        subnet=subnet,
        ip_range=ip_range,
        required_endpoints=frozenset(endpoints),
    )


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("prepare", "validate"))
    parser.add_argument("--network-name", required=True)
    parser.add_argument("--compose-project", required=True)
    parser.add_argument(
        "--isolation-mode", choices=("inhibit-ipv4", "isolated"), required=True
    )
    parser.add_argument("--subnet", required=True)
    parser.add_argument("--ip-range", required=True)
    parser.add_argument("--required-endpoint", action="append", required=True)
    parser.add_argument("--probe-image")
    arguments = parser.parse_args(argv)
    if arguments.operation == "validate" and not arguments.probe_image:
        parser.error("--probe-image is required for validate")
    if arguments.operation == "prepare" and arguments.probe_image:
        parser.error("--probe-image is only valid for validate")
    return arguments


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = parse_arguments(argv)
        contract = _parse_contract(arguments)
        docker_version = inspect_docker_server()
        if arguments.operation == "prepare":
            prepare_network(contract, docker_version)
        else:
            validate_live_network(contract, docker_version, arguments.probe_image)
    except ContractError as exc:
        print(f"production-network: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
