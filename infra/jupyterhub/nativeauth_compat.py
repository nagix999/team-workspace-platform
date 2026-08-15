"""Narrow compatibility shim for NativeAuthenticator's login handler.

JupyterHub 5.5 renders login-form errors synchronously and forwards render
keywords to ``LoginHandler._render``. NativeAuthenticator 1.3.0 overrides that
method without ``**kwargs``, so an expired XSRF form can raise a secondary
``TypeError`` instead of rendering the intended 403 page.

The shim patches only that known signature. A future unexpected signature
fails closed instead of silently replacing upstream behavior.
"""

from __future__ import annotations

import inspect
from typing import Any


def _render_synchronous_error(handler: Any, **render_kwargs: Any) -> Any:
    # NativeAuthenticator registers its templates only in JupyterHub's async
    # Jinja environment. The synchronous error path therefore delegates to
    # JupyterHub's built-in login renderer and its sync-capable login template.
    from jupyterhub.handlers.login import LoginHandler as JupyterHubLoginHandler

    return JupyterHubLoginHandler._render(
        handler,
        sync=True,
        **render_kwargs,
    )


def _compatible_native_login_render(
    self: Any,
    login_error: str | None = None,
    username: str | None = None,
    **render_kwargs: Any,
) -> Any:
    synchronous = render_kwargs.pop("sync", False)
    if synchronous is True:
        return _render_synchronous_error(
            self,
            login_error=login_error,
            username=username,
            **render_kwargs,
        )
    if synchronous is not False:
        raise RuntimeError("login render sync flag must be a boolean")

    from tornado.escape import url_escape
    from tornado.httputil import url_concat

    return self.render_template(
        "native-login.html",
        next=url_escape(self.get_argument("next", default="")),
        username=username,
        login_error=login_error,
        custom_html=self.authenticator.custom_html,
        login_url=self.settings["login_url"],
        enable_signup=self.authenticator.enable_signup,
        two_factor_auth=self.authenticator.allow_2fa,
        authenticator_login_url=url_concat(
            self.authenticator.login_url(self.hub.base_url),
            {"next": self.get_argument("next", "")},
        ),
        **render_kwargs,
    )


def apply_native_login_render_compatibility(
    handler_class: type[Any] | None = None,
) -> bool:
    """Patch NativeAuthenticator 1.3's exact legacy handler signature.

    Returns ``True`` when the compatibility patch was applied and ``False``
    when upstream already accepts arbitrary render keywords.
    """
    if handler_class is None:
        from nativeauthenticator.handlers import LoginHandler

        handler_class = LoginHandler

    signature = inspect.signature(handler_class._render)
    parameters = tuple(signature.parameters.values())
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters):
        return False

    if tuple(parameter.name for parameter in parameters) != (
        "self",
        "login_error",
        "username",
    ):
        raise RuntimeError(
            "unsupported NativeAuthenticator LoginHandler._render signature: "
            f"{signature}"
        )

    handler_class._render = _compatible_native_login_render
    return True
