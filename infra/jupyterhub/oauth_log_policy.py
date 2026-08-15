"""Fail closed if JupyterHub stops scrubbing OAuth credentials from access logs."""

from __future__ import annotations

from collections.abc import Callable


_OAUTH_LOG_SAMPLES = (
    (
        "authorize",
        "/hub/api/oauth2/authorize"
        "?client_id=service-platform-api"
        "&response_type=code"
        "&state=PLATFORM_AUDIT_STATE"
        "&code_challenge=PLATFORM_AUDIT_CHALLENGE"
        "&code_challenge_method=S256",
        ("PLATFORM_AUDIT_STATE", "PLATFORM_AUDIT_CHALLENGE"),
    ),
    (
        "callback",
        "https://platform.workspace.test/api/v1/auth/callback"
        "?code=PLATFORM_AUDIT_CODE&state=PLATFORM_AUDIT_STATE",
        ("PLATFORM_AUDIT_CODE", "PLATFORM_AUDIT_STATE"),
    ),
)


def validate_oauth_log_scrubber(scrub_uri: Callable[[str], str]) -> None:
    """Verify that Hub's active URI scrubber hides OAuth one-time credentials.

    JupyterHub 5.5 applies its URI scrubber before writing access-log messages.
    This startup assertion deliberately accepts either value replacement or
    removal of the whole query string, while rejecting an upgrade that exposes
    any representative authorization or callback credential.
    """

    for flow, uri, secrets in _OAUTH_LOG_SAMPLES:
        try:
            scrubbed = scrub_uri(uri)
        except Exception as exc:
            raise RuntimeError(
                f"JupyterHub OAuth access-log scrubber failed for {flow}"
            ) from exc
        if not isinstance(scrubbed, str) or any(
            secret in scrubbed for secret in secrets
        ):
            raise RuntimeError(
                f"JupyterHub OAuth access-log scrubber exposes {flow} credentials"
            )
