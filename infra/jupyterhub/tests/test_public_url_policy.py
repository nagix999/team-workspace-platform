from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from public_url_policy import (  # noqa: E402
    canonical_oauth_callback,
    canonical_origin,
    validate_public_origin_pair,
    validate_runtime_mode,
    validate_signup_mode,
)


class RuntimeModeTests(unittest.TestCase):
    def test_existing_http_localhost_mode_remains_explicitly_supported(self) -> None:
        mode = validate_runtime_mode(
            platform_env="local-dev",
            unsafe_local_dev=True,
            unsafe_domain_test=False,
        )
        self.assertFalse(mode.secure_public_urls)
        self.assertTrue(mode.unsafe_local_runtime)
        hub = canonical_origin(
            "http://hub.localhost:8080",
            name="JUPYTERHUB_SUBDOMAIN_HOST",
            https_required=mode.secure_public_urls,
            dns_host_required=True,
        )
        callback, portal = canonical_oauth_callback(
            "http://platform.localhost:8080/api/v1/auth/callback",
            name="PLATFORM_OAUTH_REDIRECT_URI",
            https_required=mode.secure_public_urls,
        )
        self.assertEqual(
            callback,
            "http://platform.localhost:8080/api/v1/auth/callback",
        )
        validate_public_origin_pair(
            hub_origin=hub,
            portal_origin=portal,
            mode=mode,
        )

        with self.assertRaisesRegex(RuntimeError, "restricted to localhost"):
            validate_public_origin_pair(
                hub_origin="http://hub.example.net:8080",
                portal_origin="http://platform.example.com:8080",
                mode=mode,
            )

    def test_production_is_secure_and_has_no_unsafe_test_flag(self) -> None:
        mode = validate_runtime_mode(
            platform_env="production",
            unsafe_local_dev=False,
            unsafe_domain_test=False,
        )
        self.assertTrue(mode.production)
        self.assertTrue(mode.secure_public_urls)
        self.assertFalse(mode.unsafe_local_runtime)

    def test_domain_test_is_https_with_an_explicit_separate_flag(self) -> None:
        mode = validate_runtime_mode(
            platform_env="domain-test",
            unsafe_local_dev=False,
            unsafe_domain_test=True,
        )
        self.assertFalse(mode.production)
        self.assertTrue(mode.secure_public_urls)
        self.assertTrue(mode.unsafe_local_runtime)

    def test_insecure_local_flag_cannot_enable_domain_test(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "exactly match"):
            validate_runtime_mode(
                platform_env="domain-test",
                unsafe_local_dev=True,
                unsafe_domain_test=False,
            )

    def test_production_rejects_every_unsafe_mode_flag(self) -> None:
        for flags in ((True, False), (False, True), (True, True)):
            with (
                self.subTest(flags=flags),
                self.assertRaisesRegex(RuntimeError, "exactly match"),
            ):
                validate_runtime_mode(
                    platform_env="production",
                    unsafe_local_dev=flags[0],
                    unsafe_domain_test=flags[1],
                )

    def test_signup_is_forbidden_only_in_production(self) -> None:
        production = validate_runtime_mode(
            platform_env="production",
            unsafe_local_dev=False,
            unsafe_domain_test=False,
        )
        with self.assertRaisesRegex(RuntimeError, "forbidden in production"):
            validate_signup_mode(mode=production, enabled=True)

        domain_test = validate_runtime_mode(
            platform_env="domain-test",
            unsafe_local_dev=False,
            unsafe_domain_test=True,
        )
        validate_signup_mode(mode=domain_test, enabled=True)


class PublicURLTests(unittest.TestCase):
    def test_domain_test_origins_and_callback_are_exact_https(self) -> None:
        hub = canonical_origin(
            "https://hub.workspace.test:3030",
            name="JUPYTERHUB_SUBDOMAIN_HOST",
            https_required=True,
        )
        callback, portal = canonical_oauth_callback(
            "https://platform.workspace.test:3030/api/v1/auth/callback",
            name="PLATFORM_OAUTH_REDIRECT_URI",
            https_required=True,
        )
        self.assertEqual(hub, "https://hub.workspace.test:3030")
        self.assertEqual(
            callback,
            "https://platform.workspace.test:3030/api/v1/auth/callback",
        )
        mode = validate_runtime_mode(
            platform_env="domain-test",
            unsafe_local_dev=False,
            unsafe_domain_test=True,
        )
        validate_public_origin_pair(
            hub_origin=hub,
            portal_origin=portal,
            mode=mode,
        )

    def test_public_origins_reject_paths_credentials_and_noncanonical_hosts(
        self,
    ) -> None:
        values = (
            "https://hub.workspace.test/",
            "https://hub.workspace.test/hub",
            "https://user@hub.workspace.test",
            "https://HUB.workspace.test",
            "http://hub.workspace.test",
        )
        for value in values:
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                canonical_origin(
                    value,
                    name="JUPYTERHUB_SUBDOMAIN_HOST",
                    https_required=True,
                )

    def test_user_subdomain_origin_rejects_ip_literals(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "not an IP address"):
            canonical_origin(
                "https://127.0.0.1:3030",
                name="JUPYTERHUB_SUBDOMAIN_HOST",
                https_required=True,
                dns_host_required=True,
            )

    def test_callback_rejects_every_non_exact_variant(self) -> None:
        values = (
            "https://platform.workspace.test/api/v1/auth/callback/",
            "https://platform.workspace.test/api/v1/auth/callback?next=/",
            "https://platform.workspace.test/other",
            "http://platform.workspace.test/api/v1/auth/callback",
        )
        for value in values:
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                canonical_oauth_callback(
                    value,
                    name="PLATFORM_OAUTH_REDIRECT_URI",
                    https_required=True,
                )

    def test_portal_and_hub_control_origins_must_differ(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "different public origins"):
            validate_public_origin_pair(
                hub_origin="https://hub.example.net",
                portal_origin="https://hub.example.net",
                mode=validate_runtime_mode(
                    platform_env="production",
                    unsafe_local_dev=False,
                    unsafe_domain_test=False,
                ),
            )


class ImagePackagingTests(unittest.TestCase):
    def test_public_url_policy_is_packaged_with_hub_config_and_agent(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("public_url_policy.py", dockerfile)


if __name__ == "__main__":
    unittest.main()
