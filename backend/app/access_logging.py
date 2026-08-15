from __future__ import annotations

import logging


_OAUTH_CALLBACK_PATH = "/api/v1/auth/callback"


class OAuthCallbackAccessLogFilter(logging.Filter):
    """Remove the OAuth callback query string from Uvicorn access records."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Uvicorn's access logger supplies:
        # (client_addr, method, path_with_query, http_version, status_code).
        # Unexpected records must remain loggable instead of breaking requests.
        arguments = record.args
        if not isinstance(arguments, tuple) or len(arguments) != 5:
            return True

        full_path = arguments[2]
        if not isinstance(full_path, str):
            return True

        path, separator, _query = full_path.partition("?")
        if separator and path == _OAUTH_CALLBACK_PATH:
            record.args = (
                arguments[0],
                arguments[1],
                path,
                arguments[3],
                arguments[4],
            )
        return True


def install_uvicorn_access_log_filter() -> None:
    """Install one process-local filter without changing Uvicorn handlers."""

    access_logger = logging.getLogger("uvicorn.access")
    if not any(
        isinstance(existing_filter, OAuthCallbackAccessLogFilter)
        for existing_filter in access_logger.filters
    ):
        access_logger.addFilter(OAuthCallbackAccessLogFilter())
