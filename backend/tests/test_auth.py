from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from sqlalchemy import inspect, select

from app.models import AuditEvent, AuthTransaction, UserSession
from app.security import keyed_hash
from conftest import login, mutation_headers


def test_oauth_state_is_single_use_and_session_is_opaque(app_env, settings):
    app, hub, client = app_env
    code = "code-alice"
    token = "sensitive-delegated-oauth-token"
    hub.register_login("alice", code=code, token=token)

    start = client.get("/api/v1/auth/login", follow_redirects=False)
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    assert "code_challenge" in parse_qs(urlsplit(start.headers["location"]).query)
    preauth = client.cookies.get(settings.preauth_cookie_name)
    assert preauth and state not in preauth

    with app.state.session_factory() as db:
        transaction = db.scalar(select(AuthTransaction))
        assert transaction is not None
        assert transaction.id_hash == keyed_hash(preauth, settings.session_hash_key)
        assert transaction.state_hash != state
        assert "sensitive" not in transaction.pkce_verifier_cipher

    callback = client.get(
        "/api/v1/auth/callback",
        params={"code": code, "state": state},
        follow_redirects=False,
    )
    assert callback.status_code == 303
    raw_session = client.cookies.get(settings.session_cookie_name)
    assert raw_session
    set_cookie = callback.headers["set-cookie"]
    assert "HttpOnly" in set_cookie and "Secure" in set_cookie
    assert "Domain=" not in set_cookie

    with app.state.session_factory() as db:
        portal_session = db.scalar(select(UserSession))
        assert portal_session is not None
        assert portal_session.id_hash != raw_session
        assert portal_session.id_hash == keyed_hash(
            raw_session, settings.session_hash_key
        )
        assert token not in (portal_session.hub_oauth_token_cipher or "")

    replay = client.get(
        "/api/v1/auth/callback",
        params={"code": code, "state": state},
        follow_redirects=False,
    )
    assert replay.status_code == 400
    assert replay.json()["error"]["code"] == "OAUTH_STATE_INVALID"


def test_platform_schema_has_no_password_column(app_env):
    app, _hub, _client = app_env
    inspector = inspect(app.state.engine)
    columns = {
        f"{table}.{column['name']}"
        for table in inspector.get_table_names()
        for column in inspector.get_columns(table)
    }
    assert not [name for name in columns if "password" in name.lower()]


def test_logout_revokes_portal_session_and_requires_hub_cookie_logout(app_env):
    app, hub, client = app_env
    me, *_ = login(client, hub, "alice")

    response = client.post(
        "/api/v1/auth/logout",
        headers=mutation_headers(me, "logout-alice"),
    )

    assert response.status_code == 200
    assert response.json() == {"redirect_url": "https://hub.example.net/hub/logout"}
    assert "Max-Age=0" in response.headers["set-cookie"]
    with app.state.session_factory() as db:
        portal_session = db.scalar(select(UserSession))
        assert portal_session is not None
        assert portal_session.revoked_at is not None
        assert portal_session.hub_oauth_token_cipher is None
        event = db.scalar(
            select(AuditEvent)
            .where(AuditEvent.action == "LOGOUT")
            .order_by(AuditEvent.created_at.desc())
        )
        assert event is not None
        assert event.result == "SUCCEEDED"

    assert client.get("/api/v1/me").status_code == 401


def test_password_change_link_requires_portal_session_and_targets_hub(app_env):
    _app, hub, client = app_env

    anonymous = client.get("/api/v1/auth/change-password", follow_redirects=False)
    assert anonymous.status_code == 401

    login(client, hub, "alice")
    response = client.get("/api/v1/auth/change-password", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == (
        "https://hub.example.net/hub/change-password"
    )


def test_login_callback_keeps_spa_next_and_rejects_external_redirects(app_env):
    _app, hub, client = app_env

    for index, (requested, expected) in enumerate(
        (
            ("/workspaces?view=mine", "/workspaces?view=mine"),
            ("https://attacker.test/collect", "/"),
            ("//attacker.test/collect", "/"),
        )
    ):
        code = f"redirect-code-{index}"
        hub.register_login("alice", code=code, token=f"redirect-token-{index}")
        started = client.get(
            "/api/v1/auth/login",
            params={"redirect_path": requested},
            follow_redirects=False,
        )
        state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]

        callback = client.get(
            "/api/v1/auth/callback",
            params={"code": code, "state": state},
            follow_redirects=False,
        )

        assert callback.status_code == 303
        assert callback.headers["location"] == expected


def test_local_http_uses_non_host_cookie_name(settings):
    local = settings.__class__(
        **{
            **settings.__dict__,
            "portal_origin": "http://platform.localhost:8080",
            "hub_public_url": "http://hub.localhost:8080",
            "hub_user_domain": "hub.localhost",
            "oauth_redirect_uri": "http://platform.localhost:8080/api/v1/auth/callback",
            "cookie_secure": False,
            "insecure_local_dev": True,
        }
    )
    local.validate()
    assert local.session_cookie_name == "platform-session-dev"
    assert not local.session_cookie_name.startswith("__Host-")
