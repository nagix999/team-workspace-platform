"""Fail-closed JupyterHub spawn authorization and DockerSpawner policy.

The portal's FastAPI service owns ticket consumption and all database joins. This
module authenticates that internal request with HMAC, verifies the returned facts
against a local immutable profile allowlist, and applies only local policy values.
No image, mount path, command, or Docker option from user_options or the validator
response is passed through to Docker. CPU and memory may be derived from an
allowlisted runtime only after the signed response, digest, and local hard ceiling
all match.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from docker.types import LogConfig
from tornado.httpclient import AsyncHTTPClient, HTTPClientError, HTTPRequest
from tornado.web import HTTPError

from profile_policy import (
    ProfilePolicyError,
    derive_resource_profile,
    is_managed_runtime_profile,
    kernel_runtime_environment,
)


USERNAME_RE = re.compile(r"^(?!.*--)[a-z](?:[a-z0-9-]{0,30}[a-z0-9])?$")
SERVER_NAME_RE = re.compile(r"^ws-[a-z0-9](?:[a-z0-9-]{6,61}[a-z0-9])$")
TICKET_RE = re.compile(r"^[A-Za-z0-9_-]{32,256}$")
OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
VOLUME_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
ENVIRONMENT_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
ENVIRONMENT_DIGEST_RE = re.compile(r"^hmac-sha256:[0-9a-f]{64}$")
DOCKER_STABLE_VERSION_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:\+[0-9A-Za-z][0-9A-Za-z._-]*|-[0-9][0-9A-Za-z._+~:-]*)?$"
)

MAX_ENVIRONMENT_VARIABLES = 128
MAX_ENVIRONMENT_VALUE_BYTES = 16 * 1024
MAX_ENVIRONMENT_CANONICAL_BYTES = 64 * 1024
MAX_VALIDATOR_RESPONSE_BYTES = 96 * 1024
ENVIRONMENT_HMAC_DOMAIN = b"platform-spawn-environment-v1\0"

# User values are inherited by the trusted single-user bootstrap and the Hub
# OAuth server, not only by notebook kernels.  Keep startup, identity, loader,
# egress and platform contract variables outside the user-controlled namespace.
# Matching is case-insensitive because several proxy variables have effective
# lower-case aliases on Linux.
RESERVED_ENVIRONMENT_KEYS = {
    "HOME",
    "PATH",
    "USER",
    "LOGNAME",
    "SHELL",
    "IFS",
    "ENV",
    "BASH_ENV",
    "SHELLOPTS",
    "PS4",
    "PROMPT_COMMAND",
    "CDPATH",
    "GLOBIGNORE",
    "TMPDIR",
    "TMP",
    "TEMP",
    "TEMPDIR",
    "IPYTHONDIR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "FTP_PROXY",
    "NO_PROXY",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "GIT_SSL_CAINFO",
    "HOSTALIASES",
    "LOCALDOMAIN",
    "RES_OPTIONS",
    "GRANT_SUDO",
}
RESERVED_ENVIRONMENT_PREFIXES = (
    "PLATFORM_",
    "JUPYTER_",
    "JUPYTERHUB_",
    "JPY_",
    "DOCKER_",
    "LD_",
    "DYLD_",
    "PYTHON",
    "CONDA_",
    "MAMBA_",
    "XDG_",
    "NB_",
    "CHOWN_",
    "GRANT_SUDO",
    "TMP",
)

COMPOSE_INTERNAL_NETWORK_POLICY = "compose-internal-trusted-v1"
LEGACY_HOST_FIREWALL_NETWORK_POLICY = "legacy-host-firewall-v1"
UNLIMITED_DOCKER_VOLUME_STORAGE_POLICY = "docker-volume-unlimited-v1"
LEGACY_XFS_QUOTA_STORAGE_POLICY = "legacy-xfs-project-quota-v1"
COMPOSE_INTERNAL_INHIBIT_IPV4_OPTIONS = {
    "com.docker.network.bridge.enable_icc": "true",
    "com.docker.network.bridge.inhibit_ipv4": "true",
}
COMPOSE_INTERNAL_ISOLATED_OPTIONS = {
    "com.docker.network.bridge.enable_icc": "true",
    "com.docker.network.bridge.gateway_mode_ipv4": "isolated",
    "com.docker.network.bridge.gateway_mode_ipv6": "isolated",
}
COMPOSE_INTERNAL_NETWORK_LABELS = {
    "platform.managed": "true",
    "platform.kind": "jupyter-execution",
    "platform.network.policy": COMPOSE_INTERNAL_NETWORK_POLICY,
}

CONSUME_OPTION_KEYS = {"profile_id", "profile_version", "spawn_ticket"}
AUTHORIZATION_KEYS = {
    "spawn_authorization_id",
    "workspace_id",
    "operation_id",
    "attempt_no",
    "workspace_spec_version",
    "username",
    "server_name",
    "profile_id",
    "profile_version",
    "profile_config_digest",
    "runtime_base_profile_id",
    "runtime_base_profile_version",
    "runtime_base_profile_config_digest",
    "cpu_limit_millicores",
    "memory_limit_bytes",
    "private_volume_slot_id",
    "private_volume_slot_number",
    "private_volume_name",
    "private_disk_hard_limit_bytes",
    "uid",
    "gid",
    "valid_until_unix",
    "environment",
    "environment_digest",
    "user_environment_generation",
    "workspace_environment_generation",
}
CHECK_AUTHORIZATION_KEYS = AUTHORIZATION_KEYS - {"environment"}
CONSUME_RESPONSE_KEYS = {"schema_version", "authorized", "authorization"}
CHECK_RESPONSE_KEYS = {"schema_version", "authorized", "spawn_authorization_id"}

LEGACY_NETWORK_CHECKS = {
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
}
STORAGE_CHECKS = {
    "filesystem_xfs",
    "docker_storage_driver_exact",
    "docker_data_root_project_quota",
    "project_quota_accounting",
    "project_quota_enforcement",
    "inventory_digest_exact",
    "project_ids_nonzero_unique",
    "projinherit_all",
    "hard_limits_exact",
    "docker_volume_labels_exact",
    "shared_quota_exact",
}


class SpawnGuardError(RuntimeError):
    """A deliberately non-sensitive spawn rejection."""


SINGLEUSER_ROOT_DIR = PurePosixPath("/home/jovyan")


def _singleuser_directory_contract(
    private_mount_path: str, shared_mount_path: str
) -> tuple[str, str]:
    """Return the Jupyter root/default URL for two safe sibling mounts.

    Mounting the shared volume *inside* the user-owned private volume would put
    Docker's mount destination below a path the user can replace with a symlink.
    Keeping both as fixed siblings avoids that ambiguity while a common Jupyter
    root makes both directories available in the Lab file browser.
    """

    private = PurePosixPath(private_mount_path)
    shared = PurePosixPath(shared_mount_path)
    if (
        private.parent != SINGLEUSER_ROOT_DIR
        or shared.parent != SINGLEUSER_ROOT_DIR
        or private == shared
        or private.name != "work"
        or shared.name != "shared"
    ):
        raise SpawnGuardError("private/shared mounts do not match the image contract")
    return str(SINGLEUSER_ROOT_DIR), f"/lab/tree/{private.name}"


@dataclass(frozen=True)
class GuardConfig:
    consume_url: str
    check_url: str
    hmac_key: bytes
    profiles: dict[tuple[str, int], dict[str, Any]]
    shared_volume: dict[str, Any]
    network_name: str
    hub_connect_host: str
    egress_proxy_url: str
    singleuser_command: str
    health_dir: Path
    expected_network_policy_sha256: str
    expected_storage_policy_sha256: str
    validator_timeout_seconds: float = 5.0
    health_bypass_local_dev: bool = False
    unsafe_local_dev: bool = False
    network_policy_mode: str = LEGACY_HOST_FIREWALL_NETWORK_POLICY
    network_subnet: str = ""
    network_dynamic_ip_range: str = ""
    storage_policy_mode: str = LEGACY_XFS_QUOTA_STORAGE_POLICY
    max_cpu_millicores: int = 8_000
    max_memory_mb: int = 4_096


_config: GuardConfig | None = None


def configure(config: GuardConfig) -> None:
    global _config
    _config = config


def _get_config() -> GuardConfig:
    if _config is None:
        raise SpawnGuardError("spawn guard is not configured")
    return _config


def validate_storage_policy_configuration(
    mode: str, expected_policy_sha256: str, *, production: bool
) -> None:
    """Validate the explicit storage mode without an implicit quota bypass."""

    if mode == UNLIMITED_DOCKER_VOLUME_STORAGE_POLICY:
        if expected_policy_sha256:
            raise SpawnGuardError(
                "unlimited Docker-volume mode must not claim a quota policy digest"
            )
        return
    if mode != LEGACY_XFS_QUOTA_STORAGE_POLICY:
        raise SpawnGuardError("storage policy mode is unsupported")
    if production and not SHA256_RE.fullmatch(expected_policy_sha256):
        raise SpawnGuardError("legacy storage policy digest is missing")


def _exact_keys(value: dict[str, Any], expected: set[str], where: str) -> None:
    if set(value) != expected:
        raise SpawnGuardError(f"{where} schema mismatch")


def _safe_id(value: Any, where: str) -> str:
    if not isinstance(value, str) or not OPAQUE_ID_RE.fullmatch(value):
        raise SpawnGuardError(f"{where} is invalid")
    return value


def _positive_int(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SpawnGuardError(f"{where} is invalid")
    return value


def _is_reserved_environment_key(key: str) -> bool:
    normalized = key.upper()
    return normalized in RESERVED_ENVIRONMENT_KEYS or normalized.startswith(
        RESERVED_ENVIRONMENT_PREFIXES
    )


def environment_digest(environment: dict[str, str], key: bytes) -> str:
    """Return the domain-separated HMAC binding for an effective environment."""

    canonical = _canonical_json(environment)
    return (
        "hmac-sha256:"
        + hmac.new(key, ENVIRONMENT_HMAC_DOMAIN + canonical, hashlib.sha256).hexdigest()
    )


def validate_user_environment(
    value: Any,
    *,
    expected_digest: Any,
    hmac_key: bytes,
) -> dict[str, str]:
    """Validate a write-only environment snapshot without exposing its values."""

    if not isinstance(value, dict) or len(value) > MAX_ENVIRONMENT_VARIABLES:
        raise SpawnGuardError("workspace environment is invalid")
    normalized: dict[str, str] = {}
    for name, item in value.items():
        try:
            item_bytes = item.encode("utf-8") if isinstance(item, str) else b""
        except UnicodeEncodeError:
            raise SpawnGuardError("workspace environment is invalid") from None
        if (
            not isinstance(name, str)
            or not ENVIRONMENT_KEY_RE.fullmatch(name)
            or _is_reserved_environment_key(name)
            or not isinstance(item, str)
            or "\x00" in item
            or len(item_bytes) > MAX_ENVIRONMENT_VALUE_BYTES
        ):
            # Do not include a key or value in an exception: both can contain
            # operationally sensitive information.
            raise SpawnGuardError("workspace environment is invalid")
        normalized[name] = item
    canonical = _canonical_json(normalized)
    if len(canonical) > MAX_ENVIRONMENT_CANONICAL_BYTES:
        raise SpawnGuardError("workspace environment is too large")
    if (
        not isinstance(expected_digest, str)
        or not ENVIRONMENT_DIGEST_RE.fullmatch(expected_digest)
        or not hmac.compare_digest(
            environment_digest(normalized, hmac_key), expected_digest
        )
    ):
        raise SpawnGuardError("workspace environment binding is invalid")
    return normalized


def _endpoint_path(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SpawnGuardError("validator URL must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SpawnGuardError(
            "validator URL must not contain credentials, query, or fragment"
        )
    if not parsed.path.startswith("/internal/"):
        raise SpawnGuardError("validator URL must use the internal API path")
    return parsed.path


def _canonical_json(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


async def _signed_post(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    config = _get_config()
    body = _canonical_json(payload)
    timestamp = str(int(time.time()))
    nonce = secrets.token_urlsafe(24)
    path = _endpoint_path(url)
    body_digest = hashlib.sha256(body).hexdigest()
    canonical = "\n".join(("v1", timestamp, nonce, "POST", path, body_digest)).encode(
        "ascii"
    )
    signature = hmac.new(config.hmac_key, canonical, hashlib.sha256).hexdigest()
    request = HTTPRequest(
        url=url,
        method="POST",
        body=body,
        headers={
            "Content-Type": "application/json",
            "X-Platform-HMAC-Version": "v1",
            "X-Platform-Timestamp": timestamp,
            "X-Platform-Nonce": nonce,
            "X-Platform-Content-SHA256": body_digest,
            "X-Platform-Signature": f"v1={signature}",
        },
        request_timeout=config.validator_timeout_seconds,
        connect_timeout=config.validator_timeout_seconds,
        follow_redirects=False,
        allow_nonstandard_methods=False,
    )
    try:
        response = await AsyncHTTPClient().fetch(request, raise_error=True)
    except HTTPClientError as exc:
        status = exc.code if isinstance(exc.code, int) else 599
        raise SpawnGuardError(f"spawn validator request failed ({status})") from None
    if len(response.body) > MAX_VALIDATOR_RESPONSE_BYTES:
        raise SpawnGuardError("spawn validator response is too large")
    try:
        result = json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SpawnGuardError("spawn validator returned invalid JSON") from None
    if not isinstance(result, dict):
        raise SpawnGuardError("spawn validator response must be an object")
    return result


def _health_manifest(kind: str, expected_policy_sha256: str) -> None:
    config = _get_config()
    if config.health_bypass_local_dev:
        return
    if not SHA256_RE.fullmatch(expected_policy_sha256):
        raise SpawnGuardError(f"expected {kind} policy digest is missing")

    path = config.health_dir / f"{kind}_health.json"
    try:
        stat = path.stat()
        if stat.st_uid != 0 or stat.st_mode & 0o022:
            raise SpawnGuardError(f"{kind} health manifest ownership/mode is unsafe")
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except SpawnGuardError:
        raise
    except (OSError, json.JSONDecodeError):
        raise SpawnGuardError(f"{kind} health manifest is missing or invalid") from None

    required = {
        "schema_version",
        "kind",
        "healthy",
        "checked_at_unix",
        "expires_at_unix",
        "boot_id",
        "docker_generation",
        "policy_sha256",
        "checks",
    }
    if not isinstance(manifest, dict) or set(manifest) != required:
        raise SpawnGuardError(f"{kind} health manifest schema mismatch")
    if manifest["schema_version"] != 1 or manifest["kind"] != kind:
        raise SpawnGuardError(f"{kind} health manifest identity mismatch")
    if manifest["healthy"] is not True:
        raise SpawnGuardError(f"{kind} health is not healthy")
    now = int(time.time())
    checked = _positive_int(manifest["checked_at_unix"], "checked_at_unix")
    expires = _positive_int(manifest["expires_at_unix"], "expires_at_unix")
    if checked > now + 30 or expires <= now or expires - checked > 300:
        raise SpawnGuardError(f"{kind} health manifest is stale")
    if manifest["policy_sha256"] != expected_policy_sha256:
        raise SpawnGuardError(f"{kind} health policy digest mismatch")

    try:
        boot_id = (
            Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        )
        generation = (
            (config.health_dir / "docker_generation")
            .read_text(encoding="ascii")
            .strip()
        )
    except OSError:
        raise SpawnGuardError("host generation state is unavailable") from None
    if not boot_id or manifest["boot_id"] != boot_id:
        raise SpawnGuardError(f"{kind} health boot ID mismatch")
    if not generation or manifest["docker_generation"] != generation:
        raise SpawnGuardError(f"{kind} health Docker generation mismatch")

    expected_checks = LEGACY_NETWORK_CHECKS if kind == "network" else STORAGE_CHECKS
    checks = manifest["checks"]
    if not isinstance(checks, dict) or set(checks) != expected_checks:
        raise SpawnGuardError(f"{kind} health checks schema mismatch")
    if any(value is not True for value in checks.values()):
        raise SpawnGuardError(f"{kind} health check failed")


def assert_host_health() -> None:
    """Validate host-owned storage state only.

    The default network boundary is Docker/Compose-owned and is inspected
    separately through the Docker API. The legacy firewall policy remains
    available as an explicit mode, but it is never implicitly required.
    """

    config = _get_config()
    validate_storage_policy_configuration(
        config.storage_policy_mode,
        config.expected_storage_policy_sha256,
        production=not config.unsafe_local_dev,
    )
    if config.storage_policy_mode == UNLIMITED_DOCKER_VOLUME_STORAGE_POLICY:
        return
    _health_manifest("storage", config.expected_storage_policy_sha256)


def _canonical_network(value: Any, where: str) -> ipaddress.IPv4Network:
    if not isinstance(value, str):
        raise SpawnGuardError(f"{where} is invalid")
    try:
        network = ipaddress.ip_network(value, strict=True)
    except ValueError:
        raise SpawnGuardError(f"{where} is invalid") from None
    if not isinstance(network, ipaddress.IPv4Network):
        raise SpawnGuardError(f"{where} must be IPv4")
    return network


def validate_docker_server_version(value: Any) -> tuple[int, int, int]:
    """Return a stable Linux Docker Engine version supported by this policy."""

    if not isinstance(value, dict) or value.get("Os") != "linux":
        raise SpawnGuardError("Docker server platform is unsupported")
    raw_version = value.get("Version")
    if not isinstance(raw_version, str):
        raise SpawnGuardError("Docker server version is invalid")
    matched = DOCKER_STABLE_VERSION_RE.fullmatch(raw_version)
    if matched is None:
        raise SpawnGuardError("Docker server version is invalid")
    version = tuple(int(part) for part in matched.groups()[:3])
    if version < (27, 1, 2):
        raise SpawnGuardError("Docker Engine 27.1.2 or newer is required")
    return version


def validate_compose_internal_network(
    inspected: Any,
    *,
    docker_server_version: tuple[int, int, int],
    network_name: str,
    subnet: str,
    dynamic_ip_range: str,
    hub_connect_host: str,
    egress_proxy_url: str,
) -> None:
    """Validate the firewall-free trusted-team execution bridge contract."""

    if not isinstance(inspected, dict):
        raise SpawnGuardError("Docker execution network inspect is invalid")
    if (
        inspected.get("Name") != network_name
        or inspected.get("Driver") != "bridge"
        or inspected.get("Scope") != "local"
        or inspected.get("Internal") is not True
        or inspected.get("EnableIPv6") is not False
        or inspected.get("Attachable") is not False
        or inspected.get("Ingress") is not False
    ):
        raise SpawnGuardError("Docker execution network identity is unsafe")

    options = inspected.get("Options")
    if options == COMPOSE_INTERNAL_INHIBIT_IPV4_OPTIONS:
        pass
    elif options == COMPOSE_INTERNAL_ISOLATED_OPTIONS:
        if docker_server_version < (28, 0, 0):
            raise SpawnGuardError(
                "Docker isolated gateway mode requires Engine 28 or newer"
            )
    else:
        raise SpawnGuardError("Docker execution network isolation options drifted")
    labels = inspected.get("Labels")
    if not isinstance(labels, dict) or any(
        labels.get(key) != value
        for key, value in COMPOSE_INTERNAL_NETWORK_LABELS.items()
    ):
        raise SpawnGuardError("Docker execution network labels drifted")

    expected_subnet = _canonical_network(subnet, "execution network subnet")
    expected_dynamic = _canonical_network(
        dynamic_ip_range, "execution network dynamic range"
    )
    if not expected_dynamic.subnet_of(expected_subnet):
        raise SpawnGuardError("execution network dynamic range is outside subnet")
    ipam = inspected.get("IPAM")
    ipam_configs = ipam.get("Config") if isinstance(ipam, dict) else None
    if not isinstance(ipam_configs, list) or len(ipam_configs) != 1:
        raise SpawnGuardError("Docker execution network IPAM is invalid")
    ipam_config = ipam_configs[0]
    if not isinstance(ipam_config, dict) or set(ipam_config) != {"Subnet", "IPRange"}:
        raise SpawnGuardError("Docker execution network IPAM is invalid")
    actual_subnet = _canonical_network(ipam_config.get("Subnet"), "Docker subnet")
    actual_dynamic = _canonical_network(
        ipam_config.get("IPRange"), "Docker dynamic range"
    )
    if actual_subnet != expected_subnet or actual_dynamic != expected_dynamic:
        raise SpawnGuardError("Docker execution network IPAM drifted")

    try:
        hub_ip = ipaddress.ip_address(hub_connect_host)
    except ValueError:
        raise SpawnGuardError("Hub connect URL must use an IPv4 address") from None
    parsed_proxy = urlsplit(egress_proxy_url)
    try:
        proxy_ip = ipaddress.ip_address(parsed_proxy.hostname or "")
    except ValueError:
        raise SpawnGuardError("egress proxy URL must use an IPv4 address") from None
    if (
        not isinstance(hub_ip, ipaddress.IPv4Address)
        or hub_ip not in expected_subnet
        or hub_ip in expected_dynamic
        or not isinstance(proxy_ip, ipaddress.IPv4Address)
        or proxy_ip not in expected_subnet
        or proxy_ip in expected_dynamic
        or proxy_ip == hub_ip
    ):
        raise SpawnGuardError("Hub/proxy IPs are outside their reserved range")
    endpoints = inspected.get("Containers")
    if not isinstance(endpoints, dict):
        raise SpawnGuardError("Docker execution network endpoints are invalid")
    endpoint_ips: set[ipaddress.IPv4Address] = set()
    for endpoint in endpoints.values():
        if not isinstance(endpoint, dict):
            continue
        address = endpoint.get("IPv4Address")
        if not isinstance(address, str):
            continue
        try:
            endpoint_ips.add(ipaddress.ip_interface(address).ip)
        except ValueError:
            continue
    if proxy_ip not in endpoint_ips or hub_ip not in endpoint_ips:
        raise SpawnGuardError("Hub/proxy endpoints are not attached at reserved IPs")


async def assert_network_policy(spawner: Any) -> None:
    config = _get_config()
    if config.network_policy_mode == LEGACY_HOST_FIREWALL_NETWORK_POLICY:
        _health_manifest("network", config.expected_network_policy_sha256)
        return
    if config.network_policy_mode != COMPOSE_INTERNAL_NETWORK_POLICY:
        raise SpawnGuardError("execution network policy mode is unsupported")
    try:
        version_info = await spawner.docker("version")
    except Exception:
        raise SpawnGuardError("Docker server version is unavailable") from None
    docker_server_version = validate_docker_server_version(version_info)
    try:
        inspected = await spawner.docker("inspect_network", config.network_name)
    except Exception:
        raise SpawnGuardError("Docker execution network is unavailable") from None
    validate_compose_internal_network(
        inspected,
        docker_server_version=docker_server_version,
        network_name=config.network_name,
        subnet=config.network_subnet,
        dynamic_ip_range=config.network_dynamic_ip_range,
        hub_connect_host=config.hub_connect_host,
        egress_proxy_url=config.egress_proxy_url,
    )


def _validate_user_options(spawner: Any, user_options: Any) -> tuple[str, int, str]:
    if not spawner.name:
        raise SpawnGuardError("default server is forbidden")
    if not SERVER_NAME_RE.fullmatch(spawner.name):
        raise SpawnGuardError("server name is outside the platform namespace")
    if not USERNAME_RE.fullmatch(spawner.user.name):
        raise SpawnGuardError("normalized username is invalid")
    if not isinstance(user_options, dict) or set(user_options) != CONSUME_OPTION_KEYS:
        raise SpawnGuardError("spawn options schema mismatch")
    profile_id = user_options["profile_id"]
    profile_version = user_options["profile_version"]
    ticket = user_options["spawn_ticket"]
    if not isinstance(profile_id, str):
        raise SpawnGuardError("profile_id is invalid")
    _positive_int(profile_version, "profile_version")
    if not isinstance(ticket, str) or not TICKET_RE.fullmatch(ticket):
        raise SpawnGuardError("spawn ticket is invalid")
    return profile_id, profile_version, ticket


def _profile_cpu_millicores(profile: dict[str, Any]) -> int:
    try:
        value = Decimal(str(profile["cpu_limit"])) * 1000
    except (InvalidOperation, KeyError, TypeError, ValueError):
        raise SpawnGuardError("profile CPU is invalid") from None
    if not value.is_finite() or value != value.to_integral_value() or value <= 0:
        raise SpawnGuardError("profile CPU is invalid")
    return int(value)


def _authorized_runtime_profile(
    authorization: dict[str, Any], *, profile_id: str, profile_version: int
) -> dict[str, Any]:
    config = _get_config()
    base_id = authorization["runtime_base_profile_id"]
    base_version = authorization["runtime_base_profile_version"]
    base_digest = authorization["runtime_base_profile_config_digest"]
    if not isinstance(base_id, str) or type(base_version) is not int:
        raise SpawnGuardError("runtime base profile identity is invalid")
    base = config.profiles.get((base_id, base_version))
    if (
        base is None
        or base["enabled"] is not True
        or base["config_digest"] != base_digest
        or not is_managed_runtime_profile(base)
    ):
        raise SpawnGuardError("runtime base profile is not enabled in the allowlist")

    cpu_millicores = authorization["cpu_limit_millicores"]
    memory_bytes = authorization["memory_limit_bytes"]
    _positive_int(cpu_millicores, "cpu_limit_millicores")
    _positive_int(memory_bytes, "memory_limit_bytes")
    if memory_bytes % (1024 * 1024):
        raise SpawnGuardError("authorized memory must be whole MiB")
    memory_mb = memory_bytes // (1024 * 1024)
    if cpu_millicores > config.max_cpu_millicores or memory_mb > config.max_memory_mb:
        raise SpawnGuardError("authorized resources exceed the execution ceiling")

    static = config.profiles.get((profile_id, profile_version))
    if static is not None:
        if (
            static["enabled"] is not True
            or base_id != profile_id
            or base_version != profile_version
            or authorization["profile_config_digest"] != static["config_digest"]
            or _profile_cpu_millicores(static) != cpu_millicores
            or static["memory_limit_bytes"] != memory_bytes
        ):
            raise SpawnGuardError("static profile authorization does not match policy")
        return static

    try:
        derived = derive_resource_profile(
            base, cpu_millicores=cpu_millicores, memory_mb=memory_mb
        )
    except ProfilePolicyError:
        raise SpawnGuardError("derived resource profile is invalid") from None
    if (
        derived["id"] != profile_id
        or derived["version"] != profile_version
        or derived["config_digest"] != authorization["profile_config_digest"]
    ):
        raise SpawnGuardError("derived profile binding does not match policy")
    return derived


def _validate_authorization(
    authorization: Any,
    *,
    username: str,
    server_name: str,
    profile_id: str,
    profile_version: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    config = _get_config()
    if not isinstance(authorization, dict):
        raise SpawnGuardError("spawn authorization is missing")
    _exact_keys(authorization, AUTHORIZATION_KEYS, "spawn authorization")
    for field in (
        "spawn_authorization_id",
        "workspace_id",
        "operation_id",
        "private_volume_slot_id",
    ):
        _safe_id(authorization[field], field)
    for field in (
        "attempt_no",
        "workspace_spec_version",
        "private_volume_slot_number",
        "private_disk_hard_limit_bytes",
        "uid",
        "gid",
        "valid_until_unix",
        "user_environment_generation",
        "workspace_environment_generation",
        "runtime_base_profile_version",
        "cpu_limit_millicores",
        "memory_limit_bytes",
    ):
        _positive_int(authorization[field], field)

    user_environment = validate_user_environment(
        authorization["environment"],
        expected_digest=authorization["environment_digest"],
        hmac_key=config.hmac_key,
    )

    expected = {
        "username": username,
        "server_name": server_name,
        "profile_id": profile_id,
        "profile_version": profile_version,
    }
    if any(authorization[key] != value for key, value in expected.items()):
        raise SpawnGuardError("spawn authorization identity mismatch")
    if authorization["valid_until_unix"] <= int(time.time()):
        raise SpawnGuardError("spawn authorization expired")

    profile = _authorized_runtime_profile(
        authorization, profile_id=profile_id, profile_version=profile_version
    )
    for field in ("uid", "gid", "private_disk_hard_limit_bytes"):
        if authorization[field] != profile[field]:
            raise SpawnGuardError(f"authorized {field} does not match local profile")

    slot_number = authorization["private_volume_slot_number"]
    if slot_number < 1 or slot_number > 5:
        raise SpawnGuardError("private volume slot is outside the allowed range")
    volume_name = authorization["private_volume_name"]
    expected_volume_name = f"jupyter-user-{username}-slot-{slot_number}"
    if (
        not isinstance(volume_name, str)
        or not VOLUME_NAME_RE.fullmatch(volume_name)
        or volume_name != expected_volume_name
    ):
        raise SpawnGuardError("private volume name does not match owner/slot")
    # Environment values are consume-once secrets.  Keep them out of the
    # authorization marker that is sent back to the validator and consulted by
    # Docker labels.  Only this in-memory copy reaches Docker's process env.
    public_authorization = {
        key: value for key, value in authorization.items() if key != "environment"
    }
    _exact_keys(public_authorization, CHECK_AUTHORIZATION_KEYS, "check authorization")
    return public_authorization, profile, user_environment


def _apply_local_policy(
    spawner: Any,
    authorization: dict[str, Any],
    profile: dict[str, Any],
    user_environment: dict[str, str] | None = None,
) -> None:
    config = _get_config()
    managed_runtime = is_managed_runtime_profile(profile)
    notebook_root, default_url = _singleuser_directory_contract(
        profile["private_mount_path"], config.shared_volume["mount_path"]
    )
    spawner.image = profile["image"]
    spawner.cpu_limit = float(profile["cpu_limit"])
    spawner.mem_limit = int(profile["memory_limit_bytes"])
    spawner.notebook_dir = notebook_root
    spawner.default_url = default_url
    spawner.cmd = (
        ["/usr/local/bin/platform-singleuser"]
        if managed_runtime
        else [config.singleuser_command]
    )
    spawner.volumes = {
        authorization["private_volume_name"]: {
            "bind": profile["private_mount_path"],
            "mode": "rw",
        },
        config.shared_volume["name"]: {
            "bind": config.shared_volume["mount_path"],
            "mode": "rw",
        },
    }
    no_proxy = f"{config.hub_connect_host},jupyterhub,localhost,127.0.0.1"
    platform_environment = {
        "HOME": profile["private_mount_path"],
        "JUPYTER_CONFIG_DIR": "/tmp/jupyter-config",
        # The upstream image healthcheck deliberately resets HOME before asking
        # Jupyter for its runtime directory.  Keep the ephemeral server metadata
        # in one explicit tmpfs path so the server and healthcheck cannot diverge
        # when HOME points at the private workspace volume.
        "JUPYTER_RUNTIME_DIR": "/tmp/jupyter-runtime",
        "IPYTHONDIR": "/tmp/ipython",
        "HTTP_PROXY": config.egress_proxy_url,
        "HTTPS_PROXY": config.egress_proxy_url,
        "http_proxy": config.egress_proxy_url,
        "https_proxy": config.egress_proxy_url,
        "NO_PROXY": no_proxy,
        "no_proxy": no_proxy,
        "PLATFORM_WORKSPACE_ID": authorization["workspace_id"],
        "PLATFORM_PRIVATE_MOUNT_PATH": profile["private_mount_path"],
        "PLATFORM_PRIVATE_UID": str(profile["uid"]),
        "PLATFORM_PRIVATE_GID": str(profile["gid"]),
        "PLATFORM_SHARED_MOUNT_PATH": config.shared_volume["mount_path"],
        "PLATFORM_SHARED_GID": str(config.shared_volume["gid"]),
    }
    if managed_runtime:
        platform_environment.update(kernel_runtime_environment(profile))
    if user_environment is None:
        user_environment = getattr(spawner, "_platform_user_environment", {})
    if not isinstance(user_environment, dict):
        raise SpawnGuardError("workspace environment marker is missing")
    # Defense in depth: the platform map is applied last even though protected
    # keys are already rejected, so a future denylist regression cannot replace
    # the Hub token, egress proxy or bootstrap contract.
    spawner.environment = {**user_environment, **platform_environment}
    spawner._platform_spawn_authorization = authorization.copy()
    spawner._platform_profile = profile.copy()
    spawner._platform_user_environment = user_environment.copy()
    spawner._platform_runtime_environment = platform_environment.copy()


async def apply_user_options(spawner: Any, user_options: Any) -> None:
    """Atomically consume a portal-issued ticket and apply local allowlisted policy."""

    try:
        await assert_network_policy(spawner)
        assert_host_health()
        profile_id, profile_version, ticket = _validate_user_options(
            spawner, user_options
        )
        result = await _signed_post(
            _get_config().consume_url,
            {
                "schema_version": 1,
                "username": spawner.user.name,
                "server_name": spawner.name,
                "profile_id": profile_id,
                "profile_version": profile_version,
                "spawn_ticket": ticket,
            },
        )
        _exact_keys(result, CONSUME_RESPONSE_KEYS, "consume response")
        if result["schema_version"] != 1 or result["authorized"] is not True:
            raise SpawnGuardError("spawn ticket was not authorized")
        authorization, profile, user_environment = _validate_authorization(
            result["authorization"],
            username=spawner.user.name,
            server_name=spawner.name,
            profile_id=profile_id,
            profile_version=profile_version,
        )
        _apply_local_policy(spawner, authorization, profile, user_environment)
        # JupyterHub persists user_options. Remove the now-consumed bearer value;
        # a subsequent start must obtain a new ticket from the portal.
        user_options.clear()
        user_options.update(
            {
                "profile_id": profile_id,
                "profile_version": profile_version,
                "spawn_ticket": "consumed",
            }
        )
    except SpawnGuardError as exc:
        clear_user_environment(spawner)
        spawner.log.warning("platform spawn rejected in apply_user_options: %s", exc)
        raise HTTPError(403, reason="platform spawn authorization rejected") from None
    except BaseException:
        # Cancellation or an unexpected implementation error must not extend
        # the lifetime of a snapshot left by this or a previous attempt.
        clear_user_environment(spawner)
        raise


async def _verify_docker_volumes(
    spawner: Any, authorization: dict[str, Any], profile: dict[str, Any]
) -> None:
    config = _get_config()
    quota_enforced = profile.get("private_disk_quota_enforced", False)
    if not isinstance(quota_enforced, bool):
        raise SpawnGuardError("profile disk quota enforcement marker is invalid")
    expected_quota = config.storage_policy_mode == LEGACY_XFS_QUOTA_STORAGE_POLICY
    if quota_enforced is not expected_quota:
        raise SpawnGuardError("profile disk marker does not match storage policy mode")
    expected_mounts = {
        authorization["private_volume_name"]: {
            "bind": profile["private_mount_path"],
            "mode": "rw",
        },
        config.shared_volume["name"]: {
            "bind": config.shared_volume["mount_path"],
            "mode": "rw",
        },
    }
    if getattr(spawner, "volumes", None) != expected_mounts:
        raise SpawnGuardError("Docker volume mount mapping has drifted")
    try:
        private = await spawner.docker(
            "inspect_volume", authorization["private_volume_name"]
        )
        shared = await spawner.docker("inspect_volume", config.shared_volume["name"])
    except Exception:
        raise SpawnGuardError("required Docker volume is missing") from None

    for inspected, expected_name in (
        (private, authorization["private_volume_name"]),
        (shared, config.shared_volume["name"]),
    ):
        options = inspected.get("Options") or {}
        if (
            inspected.get("Name") != expected_name
            or inspected.get("Driver") != "local"
            or inspected.get("Scope", "local") != "local"
            or (
                config.storage_policy_mode == UNLIMITED_DOCKER_VOLUME_STORAGE_POLICY
                and options != {}
            )
        ):
            raise SpawnGuardError("Docker volume identity does not match policy")

    private_labels = private.get("Labels") or {}
    expected_private_labels = {
        "platform.managed": "true",
        "platform.provisioned": "true",
        "platform.owner.username": authorization["username"],
        "platform.volume.slot": str(authorization["private_volume_slot_number"]),
        "platform.volume.slot_id": authorization["private_volume_slot_id"],
        "platform.quota.hard_bytes": str(profile["private_disk_hard_limit_bytes"]),
        "platform.quota.enforced": str(quota_enforced).lower(),
    }
    if set(private_labels) != {
        *expected_private_labels,
        "platform.owner.user_id",
        "platform.quota.project_id",
    } or any(
        private_labels.get(key) != value
        for key, value in expected_private_labels.items()
    ):
        raise SpawnGuardError("private Docker volume labels do not match authorization")
    try:
        owner_id = uuid.UUID(private_labels["platform.owner.user_id"])
        project_id = int(private_labels["platform.quota.project_id"], 10)
        expected_slot_id = uuid.uuid5(
            owner_id,
            f"workspace-volume-slot-{authorization['private_volume_slot_number']}",
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        raise SpawnGuardError("private Docker volume labels are invalid") from None
    if (
        project_id <= 0
        or str(expected_slot_id) != authorization["private_volume_slot_id"]
    ):
        raise SpawnGuardError(
            "private Docker volume owner/slot labels are inconsistent"
        )

    shared_labels = shared.get("Labels") or {}
    expected_shared_labels = {
        "platform.managed": "true",
        "platform.provisioned": "true",
        "platform.shared": "true",
        "platform.quota.enforced": str(expected_quota).lower(),
    }
    if expected_quota:
        expected_shared_keys = {
            *expected_shared_labels,
            "platform.quota.hard_bytes",
            "platform.quota.project_id",
        }
    else:
        expected_shared_keys = set(expected_shared_labels)
    if set(shared_labels) != expected_shared_keys or any(
        shared_labels.get(key) != value for key, value in expected_shared_labels.items()
    ):
        raise SpawnGuardError("shared Docker volume labels are not verified")


async def pre_spawn_hook(spawner: Any) -> None:
    """Re-check mutable owner/state facts immediately before Docker create/start."""

    try:
        if not spawner.name or not SERVER_NAME_RE.fullmatch(spawner.name):
            raise SpawnGuardError("default or non-platform server is forbidden")
        authorization = getattr(spawner, "_platform_spawn_authorization", None)
        profile = getattr(spawner, "_platform_profile", None)
        user_environment = getattr(spawner, "_platform_user_environment", None)
        if (
            not isinstance(authorization, dict)
            or not isinstance(profile, dict)
            or not isinstance(user_environment, dict)
        ):
            raise SpawnGuardError("consumed spawn authorization marker is missing")
        _exact_keys(authorization, CHECK_AUTHORIZATION_KEYS, "check authorization")
        validate_user_environment(
            user_environment,
            expected_digest=authorization["environment_digest"],
            hmac_key=_get_config().hmac_key,
        )
        await assert_network_policy(spawner)
        assert_host_health()
        await _verify_docker_volumes(spawner, authorization, profile)
        result = await _signed_post(
            _get_config().check_url,
            {"schema_version": 1, **authorization},
        )
        _exact_keys(result, CHECK_RESPONSE_KEYS, "check response")
        if (
            result["schema_version"] != 1
            or result["authorized"] is not True
            or result["spawn_authorization_id"]
            != authorization["spawn_authorization_id"]
        ):
            raise SpawnGuardError("spawn authorization is no longer valid")
        # Re-apply only local values so no other hook or persisted option can drift.
        _apply_local_policy(spawner, authorization, profile, user_environment)
    except SpawnGuardError as exc:
        clear_user_environment(spawner)
        spawner.log.warning("platform spawn rejected in pre_spawn_hook: %s", exc)
        raise HTTPError(403, reason="platform pre-spawn validation rejected") from None
    except BaseException:
        clear_user_environment(spawner)
        raise


def clear_user_environment(spawner: Any) -> None:
    """Drop every Hub-side copy of the user-controlled environment map.

    The platform-only map is retained because DockerSpawner may still consult
    its environment after a failed start while JupyterHub performs cleanup.
    Every later start/restart must consume a new ticket and `_apply_local_policy`
    installs a fresh user snapshot before Docker creation.
    """

    user_environment = getattr(spawner, "_platform_user_environment", None)
    if isinstance(user_environment, dict):
        user_environment.clear()
    spawner._platform_user_environment = None

    platform_environment = getattr(spawner, "_platform_runtime_environment", None)
    # Fail closed on an unexpected marker: do not retain the merged map just
    # because a future hook/configuration regression omitted the safe copy.
    spawner.environment = (
        platform_environment.copy() if isinstance(platform_environment, dict) else {}
    )


async def post_stop_hook(spawner: Any) -> None:
    """Idempotent final cleanup for normal stop and failed-spawn teardown."""

    clear_user_environment(spawner)


def extra_create_kwargs(spawner: Any) -> dict[str, Any]:
    authorization = getattr(spawner, "_platform_spawn_authorization", None)
    profile = getattr(spawner, "_platform_profile", None)
    if not isinstance(authorization, dict) or not isinstance(profile, dict):
        raise SpawnGuardError("Docker create requested without authorization marker")
    labels = {
        "platform.managed": "true",
        "platform.kind": "jupyter-singleuser",
        "platform.username": authorization["username"],
        "platform.server_name": authorization["server_name"],
        "platform.workspace_id": authorization["workspace_id"],
        "platform.spawn_authorization_id": authorization["spawn_authorization_id"],
        "platform.profile": f"{profile['id']}@{profile['version']}",
        "platform.profile_digest": profile["config_digest"],
    }
    if all(
        field in profile for field in ("python_version", "kernels", "default_kernel")
    ):
        labels.update(
            {
                "platform.python.version": profile["python_version"],
                "platform.kernel.default": profile["default_kernel"],
                "platform.disk.quota_enforced": str(
                    profile["private_disk_quota_enforced"]
                ).lower(),
            }
        )
    return {
        "user": f"{profile['uid']}:{profile['gid']}",
        "labels": labels,
    }


def extra_host_config(spawner: Any) -> dict[str, Any]:
    profile = getattr(spawner, "_platform_profile", None)
    if not isinstance(profile, dict):
        raise SpawnGuardError("Docker host config requested without profile marker")
    result = {
        "network_mode": _get_config().network_name,
        # User code must resolve package hosts through the HTTP(S) proxy.  A
        # loopback-only resolver prevents direct use of Docker embedded DNS.
        "dns": ["127.0.0.1"],
        "privileged": False,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "read_only": True,
        "pids_limit": profile["pids_limit"],
        # Equal memory+swap and memory limits forbid additional swap use.
        # DockerSpawner 14 merges this beside its own `mem_limit` host key.
        "memswap_limit": profile["memory_limit_bytes"],
        "shm_size": profile["shm_size_bytes"],
        "group_add": [str(_get_config().shared_volume["gid"])],
        "tmpfs": {
            "/tmp": (
                "rw,noexec,nosuid,nodev,mode=1777,size="
                f"{profile['tmpfs_size_bytes']}"
            ),
            "/run": "rw,noexec,nosuid,nodev,mode=0755,size=16777216",
        },
        "log_config": LogConfig(
            type=LogConfig.types.JSON,
            config={
                "max-size": str(profile["log_max_size_bytes"]),
                "max-file": str(profile["log_max_files"]),
            },
        ),
        "init": True,
    }
    # Docker's per-container overlay2 `size` requires an XFS pquota-backed Docker
    # data-root. Only the explicit legacy XFS policy proves that invariant; the
    # unlimited named-volume policy deliberately omits a disk-size storage opt.
    config = _get_config()
    if (
        config.storage_policy_mode == LEGACY_XFS_QUOTA_STORAGE_POLICY
        and profile.get("private_disk_quota_enforced", False) is True
    ):
        result["storage_opt"] = {"size": str(profile["writable_layer_size_bytes"])}
    return result
