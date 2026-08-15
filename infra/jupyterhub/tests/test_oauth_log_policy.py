from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from oauth_log_policy import validate_oauth_log_scrubber  # noqa: E402


class OAuthLogPolicyTests(unittest.TestCase):
    def test_accepts_value_redaction_for_authorize_and_callback_queries(self) -> None:
        def redact(uri: str) -> str:
            for secret in (
                "PLATFORM_AUDIT_STATE",
                "PLATFORM_AUDIT_CHALLENGE",
                "PLATFORM_AUDIT_CODE",
            ):
                uri = uri.replace(secret, "[secret]")
            return uri

        validate_oauth_log_scrubber(redact)

    def test_accepts_removing_the_query_string(self) -> None:
        validate_oauth_log_scrubber(lambda uri: uri.partition("?")[0])

    def test_rejects_a_scrubber_that_leaves_credentials_visible(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "exposes authorize credentials"):
            validate_oauth_log_scrubber(lambda uri: uri)

    def test_rejects_partial_redaction_that_leaves_callback_code_visible(self) -> None:
        def redact_state_and_challenge(uri: str) -> str:
            return uri.replace("PLATFORM_AUDIT_STATE", "[secret]").replace(
                "PLATFORM_AUDIT_CHALLENGE", "[secret]"
            )

        with self.assertRaisesRegex(RuntimeError, "exposes callback credentials"):
            validate_oauth_log_scrubber(redact_state_and_challenge)

    def test_rejects_a_broken_scrubber(self) -> None:
        def broken(_uri: str) -> str:
            raise ValueError("broken")

        with self.assertRaisesRegex(RuntimeError, "scrubber failed for authorize"):
            validate_oauth_log_scrubber(broken)


class OAuthLogPolicyPackagingTests(unittest.TestCase):
    def test_policy_is_packaged_and_enforced_by_hub_config(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        config = (ROOT / "jupyterhub_config.py").read_text(encoding="utf-8")

        self.assertIn("oauth_log_policy.py", dockerfile)
        self.assertIn(
            "validate_oauth_log_scrubber(jupyterhub_scrub_uri)",
            config,
        )


if __name__ == "__main__":
    unittest.main()
