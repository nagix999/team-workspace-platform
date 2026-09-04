"""JupyterHub 5.5 configuration for the platform execution plane.

Production defaults are intentionally unusable without explicit secrets, URLs,
profile and network/storage policy modes. Legacy host-health modes additionally
require exact policy digests. Local development still requires the FastAPI spawn
validator.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

from jupyterhub.app import subdomain_hook_idna
from jupyterhub.log import _scrub_uri as jupyterhub_scrub_uri
from jupyterhub.roles import get_default_roles
from dockerspawner import DockerSpawner


sys.path.insert(0, str(Path(__file__).resolve().parent))

from profile_policy import (  # noqa: E402
    ProfilePolicyError,
    accelerator_contract,
    load_profile_policy,
)
from platform_spawner import PlatformDockerSpawnerMixin  # noqa: E402
from local_volume_policy import (  # noqa: E402
    validate_web_provisioning_mode,
    validate_workspace_deletion_mode,
)
from local_provisioner_agent import managed_service_environment  # noqa: E402
from nativeauth_compat import apply_native_login_render_compatibility  # noqa: E402
from oauth_log_policy import validate_oauth_log_scrubber  # noqa: E402
from public_url_policy import (  # noqa: E402
    canonical_oauth_callback,
    canonical_origin,
    validate_public_origin_pair,
    validate_runtime_mode,
    validate_signup_mode,
)
from rbac_policy import (  # noqa: E402
    PLATFORM_ADMIN_LIFECYCLE_SERVICE,
    PLATFORM_API_SERVICE,
    PLATFORM_RECONCILER_SERVICE,
    platform_load_roles,
    validate_builtin_admin_browser_access,
    validate_singleuser_browser_oauth_contract,
)
from resource_usage_handler import RESOURCE_USAGE_HANDLERS  # noqa: E402
from spawn_guard import GuardConfig, configure as configure_spawn_guard  # noqa: E402
import spawn_guard  # noqa: E402


# JupyterHub 5.5 scrubs these values before access logging. Treat that behavior
# as a startup contract so a future dependency change cannot silently expose
# OAuth authorization codes, state, or PKCE challenges.
validate_oauth_log_scrubber(jupyterhub_scrub_uri)
validate_builtin_admin_browser_access(get_default_roles())
validate_singleuser_browser_oauth_contract(DockerSpawner)


class PlatformDockerSpawner(PlatformDockerSpawnerMixin, DockerSpawner):
    """DockerSpawner with fail-closed Hub plaintext cleanup."""


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value or value.startswith("REPLACE_"):
        raise RuntimeError(f"required environment variable {name} is unset")
    return value


def boolean_env(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized not in {"true", "false"}:
        raise RuntimeError(f"{name} must be exactly true or false")
    return normalized == "true"


def positive_int_env(name: str, *, maximum: int | None = None) -> int:
    raw = required_env(name)
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be an integer") from None
    if value <= 0 or (maximum is not None and value > maximum):
        raise RuntimeError(f"{name} is outside the permitted range")
    return value


def read_secret_file(env_name: str, *, minimum_bytes: int = 32) -> bytes:
    path = Path(required_env(env_name))
    try:
        stat = path.stat()
        if stat.st_mode & 0o022:
            raise RuntimeError(f"secret file from {env_name} is group/world writable")
        value = path.read_bytes().strip()
    except OSError as exc:
        raise RuntimeError(f"cannot read secret file from {env_name}: {exc}") from exc
    if len(value) < minimum_bytes:
        raise RuntimeError(f"secret file from {env_name} is too short")
    return value


def read_text_secret(env_name: str, *, minimum_bytes: int = 32) -> str:
    raw = read_secret_file(env_name, minimum_bytes=minimum_bytes)
    try:
        return raw.decode("ascii")
    except UnicodeDecodeError:
        raise RuntimeError(f"secret file from {env_name} must contain ASCII") from None


def read_hex_secret(env_name: str, *, decoded_bytes: int = 32) -> bytes:
    value = read_text_secret(env_name, minimum_bytes=decoded_bytes * 2)
    if not re.fullmatch(rf"[0-9a-fA-F]{{{decoded_bytes * 2}}}", value):
        raise RuntimeError(
            f"secret file from {env_name} must contain exactly {decoded_bytes} hex-encoded bytes"
        )
    return bytes.fromhex(value)


def require_absolute_url(name: str, *, production_https: bool) -> str:
    value = required_env(name)
    parsed = urlsplit(value)
    allowed_schemes = {"https"} if production_https else {"http", "https"}
    if (
        parsed.scheme not in allowed_schemes
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError(f"{name} is not an allowed absolute URL")
    return value.rstrip("/")


platform_env = os.environ.get("PLATFORM_ENV", "production").strip()
unsafe_local_dev = boolean_env("ALLOW_UNSAFE_LOCAL_DEV", default=False)
unsafe_domain_test = boolean_env("ALLOW_UNSAFE_DOMAIN_TEST", default=False)
runtime_mode = validate_runtime_mode(
    platform_env=platform_env,
    unsafe_local_dev=unsafe_local_dev,
    unsafe_domain_test=unsafe_domain_test,
)
unsafe_local_runtime = runtime_mode.unsafe_local_runtime
web_provisioning_enabled = boolean_env(
    "PLATFORM_WEB_PROVISIONING_ENABLED", default=False
)
production_docker_volume_provisioning = boolean_env(
    "PLATFORM_PRODUCTION_DOCKER_VOLUME_PROVISIONING_ENABLED", default=False
)
storage_policy_mode = required_env("JUPYTER_STORAGE_POLICY_MODE")
workspace_deletion_enabled = boolean_env(
    "PLATFORM_WORKSPACE_DELETION_ENABLED", default=False
)
validate_web_provisioning_mode(
    platform_env=platform_env,
    unsafe_local_dev=unsafe_local_dev,
    unsafe_domain_test=unsafe_domain_test,
    enabled=web_provisioning_enabled,
    production_docker_volume=production_docker_volume_provisioning,
    storage_policy_mode=storage_policy_mode,
)

profile_file = required_env("JUPYTERHUB_PROFILE_ALLOWLIST_FILE")
try:
    profile_policy = load_profile_policy(
        profile_file, allow_unsafe_images=unsafe_local_runtime
    )
except ProfilePolicyError as exc:
    raise RuntimeError(f"profile allowlist rejected: {exc}") from exc

enabled_profiles = {
    key: value
    for key, value in profile_policy["profiles"].items()
    if value["enabled"] is True
}
first_profile = enabled_profiles[sorted(enabled_profiles)[0]]
enabled_gpu_profiles = [
    key
    for key, profile in enabled_profiles.items()
    if (accelerator_contract(profile) or {}).get("kind") == "nvidia"
]
nvidia_gpu_device_ids_value = os.environ.get(
    "JUPYTERHUB_NVIDIA_GPU_DEVICE_IDS", ""
).strip()
if enabled_gpu_profiles:
    try:
        nvidia_gpu_device_ids = spawn_guard.validate_nvidia_gpu_device_ids(
            nvidia_gpu_device_ids_value
        )
    except spawn_guard.SpawnGuardError as exc:
        raise RuntimeError(
            "enabled NVIDIA profiles require a canonical trusted physical GPU UUID pool"
        ) from exc
    largest_enabled_gpu_profile = max(
        accelerator_contract(enabled_profiles[key])["count"]
        for key in enabled_gpu_profiles
    )
    if largest_enabled_gpu_profile > len(nvidia_gpu_device_ids):
        raise RuntimeError(
            "enabled NVIDIA profile history exceeds the trusted physical GPU pool"
        )
elif nvidia_gpu_device_ids_value:
    raise RuntimeError(
        "JUPYTERHUB_NVIDIA_GPU_DEVICE_IDS is set without an enabled NVIDIA profile"
    )
else:
    nvidia_gpu_device_ids = ()

admin_users = {
    item.strip()
    for item in required_env("JUPYTERHUB_ADMIN_USERS").split(",")
    if item.strip()
}
username_pattern = re.compile(r"^(?!.*--)[a-z](?:[a-z0-9-]{0,30}[a-z0-9])?$")
if not admin_users or any(not username_pattern.fullmatch(item) for item in admin_users):
    raise RuntimeError("JUPYTERHUB_ADMIN_USERS contains an invalid username")

blocked_users_file = Path(required_env("JUPYTERHUB_BLOCKED_USERS_FILE"))
try:
    blocked_users_value = json.loads(blocked_users_file.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
    raise RuntimeError(f"cannot read blocked-users file: {exc}") from exc
if not isinstance(blocked_users_value, list) or any(
    not isinstance(item, str) or not username_pattern.fullmatch(item)
    for item in blocked_users_value
):
    raise RuntimeError("blocked-users file must be a JSON array of valid usernames")
blocked_users = set(blocked_users_value)
if blocked_users & admin_users:
    raise RuntimeError(
        "an admin username is also blocked; resolve offboarding explicitly"
    )

subdomain_host = canonical_origin(
    required_env("JUPYTERHUB_SUBDOMAIN_HOST"),
    name="JUPYTERHUB_SUBDOMAIN_HOST",
    https_required=runtime_mode.secure_public_urls,
    dns_host_required=True,
)
oauth_redirect_uri, portal_origin = canonical_oauth_callback(
    required_env("PLATFORM_OAUTH_REDIRECT_URI"),
    name="PLATFORM_OAUTH_REDIRECT_URI",
    https_required=runtime_mode.secure_public_urls,
)
validate_public_origin_pair(
    hub_origin=subdomain_host,
    portal_origin=portal_origin,
    mode=runtime_mode,
)
egress_proxy_url = require_absolute_url(
    "JUPYTER_EGRESS_PROXY_URL", production_https=False
)

spawn_hmac_key = read_secret_file("SPAWN_VALIDATOR_HMAC_KEY_FILE", minimum_bytes=32)
platform_oauth_secret = read_text_secret(
    "PLATFORM_OAUTH_CLIENT_SECRET_FILE", minimum_bytes=32
)
reconciler_token = read_text_secret("PLATFORM_RECONCILER_TOKEN_FILE", minimum_bytes=32)
admin_lifecycle_token = read_text_secret(
    "PLATFORM_ADMIN_LIFECYCLE_TOKEN_FILE", minimum_bytes=32
)
proxy_auth_token = read_text_secret("CONFIGPROXY_AUTH_TOKEN_FILE", minimum_bytes=32)
os.environ["CONFIGPROXY_AUTH_TOKEN"] = proxy_auth_token

network_policy_mode = required_env("JUPYTER_NETWORK_POLICY_MODE")
if network_policy_mode not in {
    spawn_guard.COMPOSE_INTERNAL_NETWORK_POLICY,
    spawn_guard.LEGACY_HOST_FIREWALL_NETWORK_POLICY,
}:
    raise RuntimeError("JUPYTER_NETWORK_POLICY_MODE is unsupported")
network_subnet = required_env("JUPYTER_NETWORK_SUBNET")
network_dynamic_ip_range = required_env("JUPYTER_NETWORK_DYNAMIC_IP_RANGE")
try:
    validate_workspace_deletion_mode(
        enabled=workspace_deletion_enabled,
        storage_policy_mode=storage_policy_mode,
    )
except RuntimeError as exc:
    raise RuntimeError(str(exc)) from None
network_policy_digest = os.environ.get("EXPECTED_NETWORK_POLICY_SHA256", "").strip()
storage_policy_digest = os.environ.get("EXPECTED_STORAGE_POLICY_SHA256", "").strip()
digest_pattern = re.compile(r"^sha256:[0-9a-f]{64}$")
try:
    spawn_guard.validate_storage_policy_configuration(
        storage_policy_mode,
        storage_policy_digest,
        production=runtime_mode.production,
    )
except spawn_guard.SpawnGuardError as exc:
    raise RuntimeError(str(exc)) from None
if runtime_mode.production:
    if network_policy_mode == spawn_guard.LEGACY_HOST_FIREWALL_NETWORK_POLICY:
        if not digest_pattern.fullmatch(network_policy_digest):
            raise RuntimeError("legacy network policy digest must be exact sha256")
    elif network_policy_digest:
        raise RuntimeError(
            "Compose network mode must not claim a host network health digest"
        )

singleuser_command = required_env("JUPYTER_SINGLEUSER_COMMAND")
if not singleuser_command.startswith("/") or any(
    ch.isspace() for ch in singleuser_command
):
    raise RuntimeError(
        "JUPYTER_SINGLEUSER_COMMAND must be one absolute executable path"
    )

network_name = required_env("JUPYTER_NETWORK_NAME")
if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}", network_name):
    raise RuntimeError("JUPYTER_NETWORK_NAME is invalid")

hub_connect_url = required_env("JUPYTERHUB_HUB_CONNECT_URL")
hub_connect_host = urlsplit(hub_connect_url).hostname
if not hub_connect_host:
    raise RuntimeError("JUPYTERHUB_HUB_CONNECT_URL must contain a host")

configure_spawn_guard(
    GuardConfig(
        consume_url=required_env("SPAWN_VALIDATOR_CONSUME_URL"),
        check_url=required_env("SPAWN_VALIDATOR_CHECK_URL"),
        hmac_key=spawn_hmac_key,
        profiles=profile_policy["profiles"],
        shared_volume=profile_policy["shared_volume"],
        network_name=network_name,
        network_policy_mode=network_policy_mode,
        network_subnet=network_subnet,
        network_dynamic_ip_range=network_dynamic_ip_range,
        storage_policy_mode=storage_policy_mode,
        hub_connect_host=hub_connect_host,
        egress_proxy_url=egress_proxy_url,
        singleuser_command=singleuser_command,
        health_dir=Path(os.environ.get("PLATFORM_HEALTH_DIR", "/run/platform-health")),
        expected_network_policy_sha256=network_policy_digest,
        expected_storage_policy_sha256=storage_policy_digest,
        validator_timeout_seconds=float(
            os.environ.get("SPAWN_VALIDATOR_TIMEOUT_SECONDS", "5")
        ),
        health_bypass_local_dev=unsafe_local_runtime,
        unsafe_local_dev=unsafe_local_runtime,
        max_cpu_millicores=positive_int_env("PLATFORM_WORKSPACE_CPU_BUDGET_MILLICORES"),
        max_memory_mb=positive_int_env("PLATFORM_WORKSPACE_MEMORY_BUDGET_MB"),
        nvidia_gpu_device_ids=nvidia_gpu_device_ids,
    )
)


# Authentication: NativeAuthenticator remains the only password store.
apply_native_login_render_compatibility()
c.JupyterHub.authenticator_class = "native"
c.Authenticator.username_pattern = username_pattern.pattern
c.Authenticator.allow_all = True
c.Authenticator.allow_existing_users = False
c.Authenticator.admin_users = admin_users
c.Authenticator.blocked_users = blocked_users
c.NativeAuthenticator.open_signup = False
native_enable_signup = boolean_env("NATIVE_ENABLE_SIGNUP", default=unsafe_local_runtime)
validate_signup_mode(mode=runtime_mode, enabled=native_enable_signup)
c.NativeAuthenticator.enable_signup = native_enable_signup
c.NativeAuthenticator.minimum_password_length = 12
c.NativeAuthenticator.check_common_password = True
c.NativeAuthenticator.allowed_failed_logins = 5
c.NativeAuthenticator.seconds_before_next_try = 900


# Hub/proxy URLs. TLS terminates at the gateway; the public subdomain URL is HTTPS
# in production while these bind/connect URLs stay on private Docker networks.
c.JupyterHub.bind_url = required_env("JUPYTERHUB_BIND_URL")
c.JupyterHub.hub_bind_url = required_env("JUPYTERHUB_HUB_BIND_URL")
c.JupyterHub.hub_connect_url = hub_connect_url
c.JupyterHub.subdomain_host = subdomain_host
# This Hub is an execution plane, not the user-facing control plane.  A direct
# NativeAuthenticator login has no ``next`` query argument; JupyterHub would
# otherwise fall back to ``/hub/spawn`` and expose a route the public gateway
# deliberately blocks.  OAuth and single-user login requests carry an explicit
# validated ``next`` value, which JupyterHub gives precedence over this default.
c.JupyterHub.default_url = f"{portal_origin}/"
# Passing the default string through config can remain an unvalidated string in
# JupyterHub 5.5.0 and later crash service initialization. Use the documented
# callable directly so per-user/service subdomains are deterministic.
c.JupyterHub.subdomain_hook = subdomain_hook_idna
c.JupyterHub.cookie_host_prefix_enabled = runtime_mode.secure_public_urls
c.JupyterHub.cookie_max_age_days = 8 / 24
c.JupyterHub.oauth_token_expires_in = 8 * 60 * 60
c.JupyterHub.token_expires_in_max_seconds = 8 * 60 * 60
# Load the bind-mounted value after our own mode/content checks. Pointing Hub at
# the local-dev 0444 bind mount would make JupyterHub reject the file as
# other-readable even though the container runs as an isolated non-root UID.
c.JupyterHub.cookie_secret = read_hex_secret("JUPYTERHUB_COOKIE_SECRET_FILE")
c.JupyterHub.db_url = os.environ.get(
    "JUPYTERHUB_DB_URL", "sqlite:////srv/jupyterhub/jupyterhub.sqlite"
)


# The portal is an external OAuth service. Its service credential has no lifecycle
# role; server mutations must use each logged-in user's delegated OAuth token.
hub_services = [
    {
        "name": PLATFORM_API_SERVICE,
        "api_token": platform_oauth_secret,
        "oauth_client_id": "service-platform-api",
        "oauth_redirect_uri": oauth_redirect_uri,
        "oauth_client_allowed_scopes": ["servers!user"],
        "display": False,
    },
    {
        "name": PLATFORM_RECONCILER_SERVICE,
        "api_token": reconciler_token,
        "display": False,
    },
    {
        "name": PLATFORM_ADMIN_LIFECYCLE_SERVICE,
        "api_token": admin_lifecycle_token,
        "display": False,
    },
]
if web_provisioning_enabled or workspace_deletion_enabled:
    provisioning_paths = {}
    if web_provisioning_enabled:
        provisioning_paths.update(
            {
                "PLATFORM_PROVISIONING_CLAIM_URL": "/internal/v1/user-provisioning/claim",
                "PLATFORM_PROVISIONING_COMPLETE_URL": "/internal/v1/user-provisioning/complete",
                "PLATFORM_PROVISIONING_FAIL_URL": "/internal/v1/user-provisioning/fail",
            }
        )
    if workspace_deletion_enabled:
        provisioning_paths.update(
            {
                "PLATFORM_DELETION_CLAIM_URL": "/internal/v1/workspace-deletions/claim",
                "PLATFORM_DELETION_COMPLETE_URL": "/internal/v1/workspace-deletions/complete",
                "PLATFORM_DELETION_FAIL_URL": "/internal/v1/workspace-deletions/fail",
            }
        )
    for env_name, expected_path in provisioning_paths.items():
        value = require_absolute_url(env_name, production_https=False)
        parsed = urlsplit(value)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "api"
            or parsed.port != 8000
            or parsed.path != expected_path
        ):
            raise RuntimeError(
                f"{env_name} must use the exact internal provisioning path"
            )
    if required_env("PLATFORM_LOCAL_PROVISIONING_STATE_DIR") != (
        "/srv/jupyterhub/local-provisioning"
    ):
        raise RuntimeError(
            "PLATFORM_LOCAL_PROVISIONING_STATE_DIR must use the fixed shared-state mount"
        )
    hub_services.append(
        {
            "name": "platform-local-provisioner",
            "command": [
                sys.executable,
                "/etc/jupyterhub/local_provisioner_agent.py",
            ],
            # Hub-managed services do not inherit the Hub process environment.
            # Pass only the local agent's public configuration and secret *file
            # path*. Raw secrets and unrelated Hub credentials stay excluded.
            "environment": managed_service_environment(os.environ),
            "display": False,
        }
    )
c.JupyterHub.services = hub_services
c.JupyterHub.load_roles = platform_load_roles()
c.JupyterHub.extra_handlers = RESOURCE_USAGE_HANDLERS


# Hub-level quota is the final defense even when portal checks race or are bypassed.
c.JupyterHub.allow_named_servers = True
c.JupyterHub.named_server_limit_per_user = 5
c.JupyterHub.active_server_limit = 15
c.JupyterHub.concurrent_spawn_limit = positive_int_env(
    "JUPYTERHUB_CONCURRENT_SPAWN_LIMIT", maximum=15
)


# DockerSpawner creates dynamic single-user containers on the pre-provisioned
# external jupyter network. No native options form is exposed.
c.JupyterHub.spawner_class = PlatformDockerSpawner
c.Spawner.options_form = ""
c.Spawner.apply_user_options = spawn_guard.apply_user_options
c.Spawner.pre_spawn_hook = spawn_guard.pre_spawn_hook
c.Spawner.post_stop_hook = spawn_guard.post_stop_hook
c.Spawner.start_timeout = 120
c.Spawner.http_timeout = 60

c.DockerSpawner.network_name = network_name
c.DockerSpawner.use_internal_ip = True
c.DockerSpawner.use_internal_hostname = False
c.DockerSpawner.ip = "0.0.0.0"
c.DockerSpawner.port = 8888
c.DockerSpawner.name_template = "jupyter-{username}--{servername}"
c.DockerSpawner.remove = True
c.DockerSpawner.pull_policy = "ifnotpresent" if unsafe_local_runtime else "never"
c.DockerSpawner.disable_user_config = True
c.DockerSpawner.default_url = "/lab"
c.DockerSpawner.env_keep = []
c.DockerSpawner.image = first_profile["image"]
c.DockerSpawner.allowed_images = [
    profile["image"] for profile in enabled_profiles.values()
]
c.DockerSpawner.extra_create_kwargs = spawn_guard.extra_create_kwargs
c.DockerSpawner.extra_host_config = spawn_guard.extra_host_config


# Keep operational logs useful without enabling request-body/debug logging that can
# expose persisted user_options or credentials.
c.JupyterHub.log_level = os.environ.get("JUPYTERHUB_LOG_LEVEL", "INFO")
c.DockerSpawner.debug = False
