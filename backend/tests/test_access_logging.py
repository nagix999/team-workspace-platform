from __future__ import annotations

import logging

from uvicorn.logging import AccessFormatter

from app.access_logging import (
    OAuthCallbackAccessLogFilter,
    install_uvicorn_access_log_filter,
)


def _access_record(
    full_path: str,
    *,
    client: str = "203.0.113.8:43110",
    method: str = "GET",
    status_code: int = 303,
) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=(client, method, full_path, "1.1", status_code),
        exc_info=None,
    )


def _render(record: logging.LogRecord) -> str:
    formatter = AccessFormatter(
        fmt='%(client_addr)s - "%(request_line)s" %(status_code)s',
        use_colors=False,
    )
    return formatter.format(record)


def test_callback_access_log_omits_query_but_keeps_request_metadata():
    callback_filter = OAuthCallbackAccessLogFilter()
    record = _access_record(
        "/api/v1/auth/callback?code=oauth-code-secret&state=oauth-state-secret"
    )

    assert callback_filter.filter(record) is True

    rendered = _render(record)
    assert "oauth-code-secret" not in rendered
    assert "oauth-state-secret" not in rendered
    assert rendered == (
        '203.0.113.8:43110 - "GET /api/v1/auth/callback HTTP/1.1" 303 See Other'
    )


def test_non_callback_access_log_is_unchanged():
    callback_filter = OAuthCallbackAccessLogFilter()
    full_path = "/api/v1/auth/login?redirect_path=%2Fworkspace"
    record = _access_record(
        full_path,
        client="198.51.100.4:51200",
        method="POST",
        status_code=429,
    )
    original_arguments = record.args

    assert callback_filter.filter(record) is True

    assert record.args == original_arguments
    assert _render(record) == (
        '198.51.100.4:51200 - "POST '
        '/api/v1/auth/login?redirect_path=%2Fworkspace HTTP/1.1" '
        "429 Too Many Requests"
    )


def test_unexpected_access_record_shape_remains_loggable():
    callback_filter = OAuthCallbackAccessLogFilter()
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="unstructured access event",
        args=(),
        exc_info=None,
    )

    assert callback_filter.filter(record) is True
    assert record.args == ()


def test_filter_installation_is_idempotent():
    access_logger = logging.getLogger("uvicorn.access")
    original_filters = list(access_logger.filters)
    access_logger.filters = [
        existing_filter
        for existing_filter in original_filters
        if not isinstance(existing_filter, OAuthCallbackAccessLogFilter)
    ]
    try:
        install_uvicorn_access_log_filter()
        install_uvicorn_access_log_filter()

        installed = [
            existing_filter
            for existing_filter in access_logger.filters
            if isinstance(existing_filter, OAuthCallbackAccessLogFilter)
        ]
        assert len(installed) == 1
    finally:
        access_logger.filters = original_filters


def test_app_factory_installs_access_log_filter(app_env):
    app, _hub, _client = app_env

    assert app is not None
    assert any(
        isinstance(existing_filter, OAuthCallbackAccessLogFilter)
        for existing_filter in logging.getLogger("uvicorn.access").filters
    )
