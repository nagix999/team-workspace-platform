from __future__ import annotations

import base64
import ipaddress
import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit


_DEV_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode()
_DNS_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _float_env(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _comma_values(name: str, default: str) -> tuple[str, ...]:
    return tuple(
        item.strip() for item in os.getenv(name, default).split(",") if item.strip()
    )


def _canonical_dns_name(value: str, *, name: str) -> str:
    if (
        not value
        or value != value.lower()
        or value.startswith(".")
        or value.endswith(".")
    ):
        raise RuntimeError(f"{name} must be a canonical lowercase DNS name")
    try:
        ascii_value = value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise RuntimeError(f"{name} must be a canonical lowercase DNS name") from exc
    labels = value.split(".")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        raise RuntimeError(f"{name} must be a DNS name, not an IP address")
    if (
        ascii_value != value
        or len(value) > 253
        or len(labels) < 2
        or any(not _DNS_LABEL_RE.fullmatch(label) for label in labels)
    ):
        raise RuntimeError(f"{name} must be a canonical lowercase DNS name")
    return value


def _canonical_host(value: str, *, name: str) -> str:
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return _canonical_dns_name(value, name=name)


def _validate_origin(value: str, *, name: str, scheme: str) -> tuple[str, int | None]:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a canonical {scheme} origin") from exc
    if (
        parsed.scheme != scheme
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError(f"{name} must be a canonical {scheme} origin")
    host = _canonical_host(parsed.hostname, name=name)
    rendered_host = f"[{host}]" if ":" in host else host
    if port is not None:
        rendered_host = f"{rendered_host}:{port}"
    if value != f"{scheme}://{rendered_host}":
        raise RuntimeError(f"{name} must be a canonical {scheme} origin")
    return host, port


def _validate_proxy_addresses(values: tuple[str, ...]) -> None:
    if not values:
        raise RuntimeError(
            "FORWARDED_ALLOW_IPS must contain at least one trusted proxy IP"
        )
    for value in values:
        if value == "*":
            raise RuntimeError("FORWARDED_ALLOW_IPS must never trust every peer")
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise RuntimeError(
                "FORWARDED_ALLOW_IPS entries must be exact proxy IP addresses"
            ) from exc
        if address.is_unspecified or address.is_multicast:
            raise RuntimeError("FORWARDED_ALLOW_IPS contains an unsafe proxy address")


def _required_secret(name: str, allow_insecure: bool, fallback: str) -> str:
    value = os.getenv(name)
    file_path = os.getenv(f"{name}_FILE")
    if value and file_path:
        raise RuntimeError(f"Set only one of {name} or {name}_FILE")
    if file_path:
        try:
            value = open(file_path, encoding="utf-8").read().rstrip("\r\n")
        except OSError as exc:
            raise RuntimeError(f"Cannot read {name}_FILE") from exc
    if value:
        return value
    if allow_insecure:
        return fallback
    raise RuntimeError(
        f"{name} is required. For an isolated local demo only, set "
        "PLATFORM_ALLOW_INSECURE_DEV_SECRETS=1."
    )


@dataclass(frozen=True)
class Settings:
    database_url: str
    portal_origin: str
    hub_internal_url: str
    hub_public_url: str
    hub_user_domain: str
    oauth_client_id: str
    oauth_client_secret: str
    oauth_redirect_uri: str
    token_encryption_key: str
    token_encryption_key_id: str
    session_hash_key: str
    internal_hmac_key: str
    cookie_secure: bool = True
    session_absolute_seconds: int = 8 * 60 * 60
    session_idle_seconds: int = 60 * 60
    auth_transaction_seconds: int = 5 * 60
    spawn_ticket_seconds: int = 60
    worker_lease_seconds: int = 60
    worker_retry_seconds: int = 3
    worker_max_attempts: int = 5
    lifecycle_timeout_seconds: int = 15 * 60
    reconciliation_freshness_seconds: int = 30
    web_provisioning_enabled: bool = False
    production_docker_volume_provisioning: bool = False
    storage_policy_mode: str = ""
    provisioning_lease_seconds: int = 5 * 60
    provisioning_max_attempts: int = 3
    hub_progress_sample_seconds: float = 1.0
    max_workspaces_per_user: int = 5
    max_active_workspaces: int = 15
    workspace_cpu_budget_millicores: int = 8_000
    workspace_memory_budget_mb: int = 4_096
    admin_usernames: tuple[str, ...] = ()
    enforce_safe_sqlite: bool = False
    execution_host_healthy: bool = True
    insecure_local_dev: bool = False
    domain_test: bool = False
    forwarded_allow_ips: tuple[str, ...] = ("127.0.0.1",)
    portal_security_domain: str | None = None
    hub_user_security_domain: str | None = None
    allow_same_site_user_content: bool = False
    admin_lifecycle_token_file: str | None = None
    deletion_lease_seconds: int = 5 * 60
    deletion_max_attempts: int = 3
    workspace_deletion_enabled: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        allow_insecure = _bool_env("PLATFORM_ALLOW_INSECURE_DEV_SECRETS", False)
        insecure_local_dev = _bool_env("PLATFORM_INSECURE_LOCAL_DEV", False)
        domain_test = _bool_env("PLATFORM_DOMAIN_TEST", False)
        if insecure_local_dev and domain_test:
            raise RuntimeError(
                "PLATFORM_INSECURE_LOCAL_DEV and PLATFORM_DOMAIN_TEST are mutually exclusive"
            )
        if allow_insecure and not (insecure_local_dev or domain_test):
            raise RuntimeError(
                "insecure development secrets are forbidden outside an explicit test mode"
            )
        portal_origin = os.getenv(
            "PLATFORM_PORTAL_ORIGIN", "https://platform.example.com"
        )
        return cls(
            database_url=os.getenv(
                "PLATFORM_DATABASE_URL", "sqlite:///./data/platform.db"
            ),
            portal_origin=portal_origin,
            hub_internal_url=os.getenv(
                "JUPYTERHUB_INTERNAL_URL", "http://jupyterhub:8081"
            ).rstrip("/"),
            hub_public_url=os.getenv(
                "JUPYTERHUB_PUBLIC_URL", "https://hub.example.net"
            ),
            hub_user_domain=os.getenv("JUPYTERHUB_USER_DOMAIN", "hub.example.net"),
            oauth_client_id=os.getenv(
                "JUPYTERHUB_OAUTH_CLIENT_ID", "service-platform-api"
            ),
            oauth_client_secret=_required_secret(
                "JUPYTERHUB_OAUTH_CLIENT_SECRET",
                allow_insecure,
                "insecure-local-oauth-secret",
            ),
            oauth_redirect_uri=os.getenv(
                "JUPYTERHUB_OAUTH_REDIRECT_URI", f"{portal_origin}/api/v1/auth/callback"
            ),
            token_encryption_key=_required_secret(
                "PLATFORM_TOKEN_ENCRYPTION_KEY", allow_insecure, _DEV_KEY
            ),
            token_encryption_key_id=os.getenv("PLATFORM_TOKEN_ENCRYPTION_KEY_ID", "v1"),
            session_hash_key=_required_secret(
                "PLATFORM_SESSION_HASH_KEY",
                allow_insecure,
                "insecure-local-session-hash-key-32",
            ),
            internal_hmac_key=_required_secret(
                "PLATFORM_INTERNAL_HMAC_KEY",
                allow_insecure,
                "insecure-local-internal-hmac-key",
            ),
            cookie_secure=_bool_env("PLATFORM_COOKIE_SECURE", True),
            session_absolute_seconds=_int_env(
                "PLATFORM_SESSION_ABSOLUTE_SECONDS", 8 * 60 * 60
            ),
            session_idle_seconds=_int_env("PLATFORM_SESSION_IDLE_SECONDS", 60 * 60),
            auth_transaction_seconds=_int_env(
                "PLATFORM_AUTH_TRANSACTION_SECONDS", 5 * 60
            ),
            spawn_ticket_seconds=_int_env("PLATFORM_SPAWN_TICKET_SECONDS", 60),
            worker_lease_seconds=_int_env("PLATFORM_WORKER_LEASE_SECONDS", 60),
            worker_retry_seconds=_int_env("PLATFORM_WORKER_RETRY_SECONDS", 3),
            worker_max_attempts=_int_env("PLATFORM_WORKER_MAX_ATTEMPTS", 5),
            lifecycle_timeout_seconds=_int_env(
                "PLATFORM_LIFECYCLE_TIMEOUT_SECONDS", 15 * 60
            ),
            reconciliation_freshness_seconds=_int_env(
                "PLATFORM_RECONCILIATION_FRESHNESS_SECONDS", 30
            ),
            web_provisioning_enabled=_bool_env(
                "PLATFORM_WEB_PROVISIONING_ENABLED", False
            ),
            production_docker_volume_provisioning=_bool_env(
                "PLATFORM_PRODUCTION_DOCKER_VOLUME_PROVISIONING_ENABLED", False
            ),
            storage_policy_mode=os.getenv("PLATFORM_STORAGE_POLICY_MODE", "").strip(),
            provisioning_lease_seconds=_int_env(
                "PLATFORM_PROVISIONING_LEASE_SECONDS", 5 * 60
            ),
            provisioning_max_attempts=_int_env("PLATFORM_PROVISIONING_MAX_ATTEMPTS", 3),
            hub_progress_sample_seconds=_float_env(
                "PLATFORM_HUB_PROGRESS_SAMPLE_SECONDS", 1.0
            ),
            max_workspaces_per_user=_int_env("PLATFORM_MAX_WORKSPACES_PER_USER", 5),
            max_active_workspaces=_int_env("PLATFORM_MAX_ACTIVE_WORKSPACES", 15),
            workspace_cpu_budget_millicores=_int_env(
                "PLATFORM_WORKSPACE_CPU_BUDGET_MILLICORES", 8_000
            ),
            workspace_memory_budget_mb=_int_env(
                "PLATFORM_WORKSPACE_MEMORY_BUDGET_MB", 4_096
            ),
            admin_usernames=tuple(
                x.strip().lower()
                for x in os.getenv("PLATFORM_ADMIN_USERNAMES", "").split(",")
                if x.strip()
            ),
            enforce_safe_sqlite=_bool_env("PLATFORM_ENFORCE_SAFE_SQLITE", False),
            execution_host_healthy=_bool_env("PLATFORM_EXECUTION_HOST_HEALTHY", True),
            insecure_local_dev=insecure_local_dev,
            domain_test=domain_test,
            forwarded_allow_ips=_comma_values("FORWARDED_ALLOW_IPS", "127.0.0.1"),
            portal_security_domain=(
                os.getenv("PLATFORM_PORTAL_SECURITY_DOMAIN", "").strip() or None
            ),
            hub_user_security_domain=(
                os.getenv("JUPYTERHUB_USER_SECURITY_DOMAIN", "").strip() or None
            ),
            allow_same_site_user_content=_bool_env(
                "PLATFORM_ALLOW_SAME_SITE_USER_CONTENT", False
            ),
            admin_lifecycle_token_file=(
                os.getenv("JUPYTERHUB_ADMIN_LIFECYCLE_TOKEN_FILE", "").strip() or None
            ),
            deletion_lease_seconds=_int_env("PLATFORM_DELETION_LEASE_SECONDS", 5 * 60),
            deletion_max_attempts=_int_env("PLATFORM_DELETION_MAX_ATTEMPTS", 3),
            workspace_deletion_enabled=_bool_env(
                "PLATFORM_WORKSPACE_DELETION_ENABLED", False
            ),
        )

    def validate(self) -> None:
        allowed_scheme = "http" if self.insecure_local_dev else "https"
        if self.insecure_local_dev and self.domain_test:
            raise RuntimeError(
                "insecure local dev and domain-test modes are mutually exclusive"
            )
        portal_host, _portal_port = _validate_origin(
            self.portal_origin,
            name="PLATFORM_PORTAL_ORIGIN",
            scheme=allowed_scheme,
        )
        hub_host, _hub_port = _validate_origin(
            self.hub_public_url,
            name="JUPYTERHUB_PUBLIC_URL",
            scheme=allowed_scheme,
        )
        expected_redirect = f"{self.portal_origin}/api/v1/auth/callback"
        if self.oauth_redirect_uri != expected_redirect:
            raise RuntimeError("OAuth redirect must byte-match the portal callback URL")
        if self.oauth_client_id != "service-platform-api":
            raise RuntimeError(
                "JUPYTERHUB_OAUTH_CLIENT_ID must match the fixed Hub service client"
            )
        _canonical_dns_name(self.hub_user_domain, name="JUPYTERHUB_USER_DOMAIN")
        if hub_host != self.hub_user_domain:
            raise RuntimeError(
                "JUPYTERHUB_PUBLIC_URL host must exactly match JUPYTERHUB_USER_DOMAIN"
            )
        if portal_host == hub_host:
            raise RuntimeError("portal and JupyterHub must use different origins")
        _validate_proxy_addresses(self.forwarded_allow_ips)
        if self.insecure_local_dev:

            def _is_loopback_name(host: str | None) -> bool:
                return bool(
                    host
                    and (
                        host in {"localhost", "127.0.0.1", "::1"}
                        or host.endswith(".localhost")
                    )
                )

            if not _is_loopback_name(portal_host) or not _is_loopback_name(hub_host):
                raise RuntimeError(
                    "Insecure local dev origins are restricted to localhost names"
                )
            if not _is_loopback_name(self.hub_user_domain):
                raise RuntimeError(
                    "Insecure local dev user domain must be localhost-scoped"
                )
            if self.cookie_secure:
                raise RuntimeError(
                    "Insecure HTTP local dev must explicitly set PLATFORM_COOKIE_SECURE=false"
                )
        if not self.insecure_local_dev and not self.cookie_secure:
            raise RuntimeError("Production portal cookies must be Secure")
        if not (self.insecure_local_dev or self.domain_test):
            if not self.portal_security_domain or not self.hub_user_security_domain:
                raise RuntimeError(
                    "production requires explicit portal and user-content security domains"
                )
            portal_site = _canonical_dns_name(
                self.portal_security_domain,
                name="PLATFORM_PORTAL_SECURITY_DOMAIN",
            )
            hub_site = _canonical_dns_name(
                self.hub_user_security_domain,
                name="JUPYTERHUB_USER_SECURITY_DOMAIN",
            )
            portal_in_site = portal_host == portal_site or portal_host.endswith(
                f".{portal_site}"
            )
            hub_in_site = hub_host == hub_site or hub_host.endswith(f".{hub_site}")
            if not portal_in_site or not hub_in_site:
                raise RuntimeError(
                    "public hosts must be below their declared security domains"
                )
            if portal_site == hub_site and not self.allow_same_site_user_content:
                raise RuntimeError(
                    "portal and user content require different registrable security domains"
                )
        if self.max_workspaces_per_user != 5 or self.max_active_workspaces != 15:
            raise RuntimeError(
                "MVP quota invariants are fixed at 5 workspaces/user and 15 active"
            )
        if self.workspace_cpu_budget_millicores <= 0:
            raise RuntimeError(
                "PLATFORM_WORKSPACE_CPU_BUDGET_MILLICORES must be greater than 0"
            )
        if self.workspace_memory_budget_mb <= 0:
            raise RuntimeError(
                "PLATFORM_WORKSPACE_MEMORY_BUDGET_MB must be greater than 0"
            )
        if self.session_idle_seconds > self.session_absolute_seconds:
            raise RuntimeError("session idle expiry cannot exceed absolute expiry")
        if self.worker_max_attempts < 2:
            raise RuntimeError(
                "PLATFORM_WORKER_MAX_ATTEMPTS must be at least 2 for stop and remove"
            )
        if self.lifecycle_timeout_seconds <= 0:
            raise RuntimeError(
                "PLATFORM_LIFECYCLE_TIMEOUT_SECONDS must be greater than 0"
            )
        if not 5 <= self.reconciliation_freshness_seconds <= 300:
            raise RuntimeError(
                "PLATFORM_RECONCILIATION_FRESHNESS_SECONDS must be between 5 and 300"
            )
        if self.production_docker_volume_provisioning and self.unsafe_local_runtime:
            raise RuntimeError(
                "production Docker-volume provisioning is forbidden in local test modes"
            )
        if self.web_provisioning_enabled and not self.unsafe_local_runtime:
            if not self.production_docker_volume_provisioning:
                raise RuntimeError(
                    "production web provisioning requires its explicit Docker-volume capability flag"
                )
            if self.storage_policy_mode != "docker-volume-unlimited-v1":
                raise RuntimeError(
                    "production web provisioning requires docker-volume-unlimited-v1 storage"
                )
        if self.provisioning_lease_seconds <= 0:
            raise RuntimeError(
                "PLATFORM_PROVISIONING_LEASE_SECONDS must be greater than 0"
            )
        if self.provisioning_max_attempts <= 0:
            raise RuntimeError(
                "PLATFORM_PROVISIONING_MAX_ATTEMPTS must be greater than 0"
            )
        if self.admin_lifecycle_token_file is not None and not os.path.isabs(
            self.admin_lifecycle_token_file
        ):
            raise RuntimeError(
                "JUPYTERHUB_ADMIN_LIFECYCLE_TOKEN_FILE must be an absolute path"
            )
        if self.deletion_lease_seconds <= 0 or self.deletion_max_attempts <= 0:
            raise RuntimeError("workspace deletion lease and attempts must be positive")
        if len(self.internal_hmac_key.encode("utf-8")) < 32:
            raise RuntimeError("PLATFORM_INTERNAL_HMAC_KEY must be at least 32 bytes")
        if not 0 < self.hub_progress_sample_seconds <= 5:
            raise RuntimeError(
                "PLATFORM_HUB_PROGRESS_SAMPLE_SECONDS must be greater than 0 and at most 5"
            )

    @property
    def session_cookie_name(self) -> str:
        return (
            "platform-session-dev"
            if self.insecure_local_dev
            else "__Host-platform-session"
        )

    @property
    def preauth_cookie_name(self) -> str:
        return (
            "platform-preauth-dev"
            if self.insecure_local_dev
            else "__Host-platform-preauth"
        )

    @property
    def unsafe_local_runtime(self) -> bool:
        return self.insecure_local_dev or self.domain_test
