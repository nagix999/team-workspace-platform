"""Shared, dependency-free helpers for root-owned host provisioning scripts."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable


class HostConfigError(RuntimeError):
    pass


CONFIG_KEYS = {"schema_version", "environment", "docker", "network", "storage", "health"}
DOCKER_KEYS = {"engine_version", "firewall_backend"}
NETWORK_KEYS = {
    "name",
    "bridge_name",
    "subnet",
    "dynamic_ip_range",
    "hub_ip",
    "egress_proxy_ip",
    "egress_out_subnet",
    "egress_out_proxy_ip",
    "hub_api_port",
    "singleuser_port",
    "egress_proxy_port",
}
STORAGE_KEYS = {
    "enabled",
    "mount_path",
    "private_root",
    "shared_path",
    "manifest_dir",
    "inventory_file",
    "shared_volume_name",
    "slot_count",
    "uid",
    "gid",
    "shared_gid",
    "private_hard_limit_bytes",
    "shared_hard_limit_bytes",
    "shared_project_id",
    "docker_data_root",
    "docker_storage_driver",
}
HEALTH_KEYS = {"output_dir", "ttl_seconds"}
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def exact_keys(value: dict[str, Any], keys: set[str], where: str) -> None:
    if set(value) != keys:
        raise HostConfigError(
            f"{where} keys mismatch: missing={sorted(keys - set(value))}, "
            f"extra={sorted(set(value) - keys)}"
        )


def load_config(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
        if stat.st_mode & 0o022:
            raise HostConfigError("host config must not be group/world writable")
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HostConfigError(f"cannot read host config: {exc}") from exc
    if not isinstance(config, dict):
        raise HostConfigError("host config must be an object")
    exact_keys(config, CONFIG_KEYS, "host config")
    if config["schema_version"] != 1:
        raise HostConfigError("host config schema_version must be 1")
    if config["environment"] not in {"production", "local-dev"}:
        raise HostConfigError("environment must be production or local-dev")
    if config["environment"] == "production" and (
        os.geteuid() != 0 or stat.st_uid != 0
    ):
        raise HostConfigError("production config and command must be root-owned/root-run")
    for key, expected in (
        ("docker", DOCKER_KEYS),
        ("network", NETWORK_KEYS),
        ("storage", STORAGE_KEYS),
        ("health", HEALTH_KEYS),
    ):
        if not isinstance(config[key], dict):
            raise HostConfigError(f"{key} must be an object")
        exact_keys(config[key], expected, key)
    if config["environment"] == "production":
        if str(config["docker"]["engine_version"]).startswith("REPLACE_"):
            raise HostConfigError("production Docker Engine version is not set")
        if config["docker"]["firewall_backend"] != "iptables":
            raise HostConfigError("these scripts require the reviewed iptables backend")
        if config["storage"]["enabled"] is not True:
            raise HostConfigError("production storage checks cannot be disabled")
    bridge_name = config["network"]["bridge_name"]
    if (
        not isinstance(bridge_name, str)
        or len(bridge_name.encode("ascii", errors="ignore")) != len(bridge_name)
        or not 1 <= len(bridge_name) <= 15
        or not re.fullmatch(r"[A-Za-z0-9_.-]+", bridge_name)
    ):
        raise HostConfigError(
            "network.bridge_name must be a valid Linux interface name of at most 15 ASCII bytes"
        )
    ttl = config["health"]["ttl_seconds"]
    if isinstance(ttl, bool) or not isinstance(ttl, int) or not 15 <= ttl <= 300:
        raise HostConfigError("health.ttl_seconds must be between 15 and 300")
    try:
        subnet = ipaddress.ip_network(config["network"]["subnet"], strict=True)
        dynamic = ipaddress.ip_network(
            config["network"]["dynamic_ip_range"], strict=True
        )
        hub_ip = ipaddress.ip_address(config["network"]["hub_ip"])
        egress_ip = ipaddress.ip_address(config["network"]["egress_proxy_ip"])
        egress_out_subnet = ipaddress.ip_network(
            config["network"]["egress_out_subnet"], strict=True
        )
        egress_out_proxy_ip = ipaddress.ip_address(
            config["network"]["egress_out_proxy_ip"]
        )
    except ValueError as exc:
        raise HostConfigError(f"network addressing is invalid: {exc}") from exc
    if (
        subnet.version != 4
        or not dynamic.subnet_of(subnet)
        or hub_ip not in subnet
        or egress_ip not in subnet
        or hub_ip in dynamic
        or egress_ip in dynamic
        or hub_ip == egress_ip
        or egress_out_subnet.version != 4
        or egress_out_proxy_ip not in egress_out_subnet
        or egress_out_proxy_ip in {
            egress_out_subnet.network_address,
            egress_out_subnet.network_address + 1,
            egress_out_subnet.broadcast_address,
        }
        or subnet.overlaps(egress_out_subnet)
    ):
        raise HostConfigError(
            "execution and egress addressing must use distinct valid IPv4 subnets/IPs"
        )
    return config


def canonical_sha256(value: Any) -> str:
    raw = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def policy_digest(config: dict[str, Any], kind: str) -> str:
    if kind == "network":
        value = {
            "schema_version": config["schema_version"],
            "docker": config["docker"],
            "network": config["network"],
        }
    elif kind == "storage":
        value = {
            "schema_version": config["schema_version"],
            "storage": config["storage"],
        }
    else:
        raise HostConfigError(f"unknown policy kind: {kind}")
    return canonical_sha256(value)


def require_commands(names: Iterable[str]) -> None:
    missing = [name for name in names if shutil.which(name) is None]
    if missing:
        raise HostConfigError(f"missing required commands: {', '.join(missing)}")


def run(
    args: list[str], *, check: bool = True, input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        check=check,
        input=input_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def atomic_json(path: Path, value: dict[str, Any], mode: int = 0o644) -> None:
    atomic_write(
        path,
        (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8"),
        mode,
    )


def boot_id() -> str:
    value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    if not value:
        raise HostConfigError("host boot ID is empty")
    return value


def docker_generation(output_dir: Path) -> str:
    path = output_dir / "docker_generation"
    try:
        value = path.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise HostConfigError(f"Docker generation is unavailable: {exc}") from exc
    if not value:
        raise HostConfigError("Docker generation is empty")
    return value


def write_health_manifest(
    config: dict[str, Any], kind: str, checks: dict[str, bool]
) -> Path:
    now = int(time.time())
    output_dir = Path(config["health"]["output_dir"])
    manifest = {
        "schema_version": 1,
        "kind": kind,
        "healthy": bool(checks) and all(value is True for value in checks.values()),
        "checked_at_unix": now,
        "expires_at_unix": now + config["health"]["ttl_seconds"],
        "boot_id": boot_id(),
        "docker_generation": docker_generation(output_dir),
        "policy_sha256": policy_digest(config, kind),
        "checks": checks,
    }
    destination = output_dir / f"{kind}_health.json"
    atomic_json(destination, manifest)
    return destination


def read_json(path: Path, *, require_root_safe: bool = True) -> Any:
    try:
        stat = path.stat()
        if require_root_safe and (stat.st_uid != 0 or stat.st_mode & 0o022):
            raise HostConfigError(f"{path} ownership/mode is unsafe")
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HostConfigError(f"cannot read JSON {path}: {exc}") from exc
