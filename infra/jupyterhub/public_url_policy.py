"""Fail-closed public URL and runtime-mode policy for JupyterHub."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit


_DNS_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
OAUTH_CALLBACK_PATH = "/api/v1/auth/callback"


@dataclass(frozen=True)
class RuntimeMode:
    name: str
    unsafe_local_runtime: bool
    secure_public_urls: bool

    @property
    def production(self) -> bool:
        return self.name == "production"


def validate_runtime_mode(
    *, platform_env: str, unsafe_local_dev: bool, unsafe_domain_test: bool
) -> RuntimeMode:
    expected = {
        "production": (False, False),
        "local-dev": (True, False),
        "domain-test": (False, True),
    }
    if platform_env not in expected:
        raise RuntimeError("PLATFORM_ENV must be production, local-dev, or domain-test")
    if (unsafe_local_dev, unsafe_domain_test) != expected[platform_env]:
        raise RuntimeError(
            "PLATFORM_ENV must exactly match its explicit unsafe test-mode flag"
        )
    return RuntimeMode(
        name=platform_env,
        unsafe_local_runtime=platform_env in {"local-dev", "domain-test"},
        secure_public_urls=platform_env != "local-dev",
    )


def _canonical_dns_name(value: str, *, name: str) -> str:
    if (
        not value
        or value != value.lower()
        or value.startswith(".")
        or value.endswith(".")
    ):
        raise RuntimeError(f"{name} must use a canonical lowercase host")
    try:
        ascii_value = value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise RuntimeError(f"{name} must use a canonical lowercase host") from exc
    labels = value.split(".")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        raise RuntimeError(f"{name} must use a DNS host, not an IP address")
    if (
        ascii_value != value
        or len(value) > 253
        or len(labels) < 2
        or any(not _DNS_LABEL_RE.fullmatch(label) for label in labels)
    ):
        raise RuntimeError(f"{name} must use a canonical lowercase host")
    return value


def _canonical_host(value: str, *, name: str) -> str:
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return _canonical_dns_name(value, name=name)


def canonical_origin(
    value: str, *, name: str, https_required: bool, dns_host_required: bool = False
) -> str:
    parsed = urlsplit(value)
    allowed_schemes = {"https"} if https_required else {"http", "https"}
    try:
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError(f"{name} is not a canonical public origin") from exc
    if (
        parsed.scheme not in allowed_schemes
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError(f"{name} is not a canonical public origin")
    host = _canonical_host(parsed.hostname, name=name)
    if dns_host_required:
        _canonical_dns_name(host, name=name)
    rendered_host = f"[{host}]" if ":" in host else host
    if port is not None:
        rendered_host = f"{rendered_host}:{port}"
    expected = f"{parsed.scheme}://{rendered_host}"
    if value != expected:
        raise RuntimeError(f"{name} is not a canonical public origin")
    return value


def canonical_oauth_callback(
    value: str, *, name: str, https_required: bool
) -> tuple[str, str]:
    parsed = urlsplit(value)
    if parsed.path != OAUTH_CALLBACK_PATH or parsed.query or parsed.fragment:
        raise RuntimeError(f"{name} must use the exact platform OAuth callback path")
    origin = f"{parsed.scheme}://{parsed.netloc}"
    canonical_origin(origin, name=name, https_required=https_required)
    expected = f"{origin}{OAUTH_CALLBACK_PATH}"
    if value != expected:
        raise RuntimeError(f"{name} must byte-match the platform OAuth callback URL")
    return value, origin


def validate_public_origin_pair(
    *, hub_origin: str, portal_origin: str, mode: RuntimeMode
) -> None:
    if urlsplit(hub_origin).hostname == urlsplit(portal_origin).hostname:
        raise RuntimeError("portal and JupyterHub must use different public origins")
    if mode.name == "local-dev":

        def is_loopback_name(value: str | None) -> bool:
            return bool(
                value
                and (
                    value in {"localhost", "127.0.0.1", "::1"}
                    or value.endswith(".localhost")
                )
            )

        if not is_loopback_name(urlsplit(hub_origin).hostname) or not is_loopback_name(
            urlsplit(portal_origin).hostname
        ):
            raise RuntimeError(
                "insecure local-dev public URLs are restricted to localhost names"
            )


def validate_signup_mode(*, mode: RuntimeMode, enabled: bool) -> None:
    if mode.production and enabled:
        raise RuntimeError("NATIVE_ENABLE_SIGNUP is forbidden in production")
