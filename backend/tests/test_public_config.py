from __future__ import annotations

from dataclasses import replace

import pytest


def test_production_public_url_contract_is_exact(settings):
    settings.validate()

    invalid = (
        replace(settings, portal_origin="https://platform.example.com/"),
        replace(settings, portal_origin="https://platform.example.com/app"),
        replace(settings, portal_origin="https://platform.example.com?debug=1"),
        replace(settings, portal_origin="https://user@platform.example.com"),
        replace(settings, portal_origin="https://PLATFORM.example.com"),
    )
    for candidate in invalid:
        with pytest.raises(RuntimeError, match="canonical https origin"):
            candidate.validate()


def test_oauth_callback_byte_matches_the_configured_portal_origin(settings):
    configured = replace(
        settings,
        portal_origin="https://platform.example.com:3030",
        oauth_redirect_uri=("https://platform.example.com:3030/api/v1/auth/callback"),
    )
    configured.validate()

    for callback in (
        "https://platform.example.com/api/v1/auth/callback",
        "https://platform.example.com:3030/api/v1/auth/callback/",
        "https://platform.example.com:3030/api/v1/auth/callback?next=/",
        "https://platform.example.com:3030/other",
    ):
        with pytest.raises(RuntimeError, match="byte-match"):
            replace(configured, oauth_redirect_uri=callback).validate()


def test_oauth_client_id_matches_the_fixed_hub_service(settings):
    with pytest.raises(RuntimeError, match="fixed Hub service client"):
        replace(settings, oauth_client_id="another-client").validate()


def test_hub_public_origin_is_bound_to_the_user_subdomain_base(settings):
    with pytest.raises(RuntimeError, match="exactly match"):
        replace(settings, hub_user_domain="users.example.net").validate()

    numeric = replace(
        settings,
        hub_public_url="https://127.0.0.1",
        hub_user_domain="127.0.0.1",
    )
    with pytest.raises(RuntimeError, match="not an IP address"):
        numeric.validate()


def test_same_site_user_content_is_a_fail_closed_production_gate(settings):
    same_site = replace(
        settings,
        portal_origin="https://platform.workspace.test",
        hub_public_url="https://hub.workspace.test",
        hub_user_domain="hub.workspace.test",
        oauth_redirect_uri=("https://platform.workspace.test/api/v1/auth/callback"),
        portal_security_domain="workspace.test",
        hub_user_security_domain="workspace.test",
    )
    with pytest.raises(RuntimeError, match="different registrable"):
        same_site.validate()

    replace(same_site, allow_same_site_user_content=True).validate()


def test_https_domain_test_keeps_browser_security_and_local_provisioning(settings):
    domain_test = replace(
        settings,
        portal_origin="https://platform.workspace.test:3030",
        hub_public_url="https://hub.workspace.test:3030",
        hub_user_domain="hub.workspace.test",
        oauth_redirect_uri=(
            "https://platform.workspace.test:3030/api/v1/auth/callback"
        ),
        domain_test=True,
        web_provisioning_enabled=True,
    )
    domain_test.validate()
    assert domain_test.unsafe_local_runtime is True
    assert domain_test.cookie_secure is True
    assert domain_test.session_cookie_name == "__Host-platform-session"
    assert domain_test.preauth_cookie_name == "__Host-platform-preauth"

    with pytest.raises(RuntimeError, match="cookies must be Secure"):
        replace(domain_test, cookie_secure=False).validate()


def test_insecure_local_dev_never_accepts_public_domains(settings):
    insecure = replace(
        settings,
        portal_origin="http://platform.workspace.test:3030",
        hub_public_url="http://hub.workspace.test:3030",
        hub_user_domain="hub.workspace.test",
        oauth_redirect_uri=("http://platform.workspace.test:3030/api/v1/auth/callback"),
        cookie_secure=False,
        insecure_local_dev=True,
    )
    with pytest.raises(RuntimeError, match="restricted to localhost"):
        insecure.validate()


@pytest.mark.parametrize("value", [("*",), ("172.20.0.0/24",), ("gateway",)])
def test_forwarded_header_trust_requires_exact_peer_ips(settings, value):
    with pytest.raises(RuntimeError, match="FORWARDED_ALLOW_IPS"):
        replace(settings, forwarded_allow_ips=value).validate()


def test_test_modes_are_mutually_exclusive(settings):
    with pytest.raises(RuntimeError, match="mutually exclusive"):
        replace(settings, insecure_local_dev=True, domain_test=True).validate()


def test_insecure_fallback_secrets_are_forbidden_in_production(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("PLATFORM_ALLOW_INSECURE_DEV_SECRETS", "1")
    monkeypatch.delenv("PLATFORM_INSECURE_LOCAL_DEV", raising=False)
    monkeypatch.delenv("PLATFORM_DOMAIN_TEST", raising=False)
    with pytest.raises(RuntimeError, match="forbidden outside"):
        Settings.from_env()
