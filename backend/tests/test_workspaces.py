from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.errors import AppError
from app.models import Workspace
from app.services.workspaces import validate_launch_url

from conftest import login, mutation_headers, provision


def test_create_is_idempotent_and_enforces_five_workspace_quota(app_env):
    app, hub, client = app_env
    me, *_ = login(client, hub, "alice")
    provision(app, "alice")

    missing_csrf = client.post(
        "/api/v1/workspaces",
        json={"profile_id": "python-standard", "profile_version": 1},
        headers={"Idempotency-Key": "missing-csrf"},
    )
    assert missing_csrf.status_code == 403

    ids = []
    for number in range(5):
        response = client.post(
            "/api/v1/workspaces",
            json={"profile_id": "python-standard", "profile_version": 1},
            headers=mutation_headers(me, f"create-{number}"),
        )
        assert response.status_code == 202, response.text
        ids.append(response.json()["workspace"]["id"])
    replay = client.post(
        "/api/v1/workspaces",
        json={"profile_id": "python-standard", "profile_version": 1},
        headers=mutation_headers(me, "create-0"),
    )
    assert replay.status_code == 202
    assert replay.json()["workspace"]["id"] == ids[0]

    sixth = client.post(
        "/api/v1/workspaces",
        json={"profile_id": "python-standard", "profile_version": 1},
        headers=mutation_headers(me, "create-6"),
    )
    assert sixth.status_code == 409
    assert sixth.json()["error"]["code"] == "WORKSPACE_QUOTA_EXCEEDED"
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count(Workspace.id))) == 5
        assert (
            db.scalar(
                select(func.count(func.distinct(Workspace.private_volume_slot_id)))
            )
            == 5
        )


def test_non_owner_get_is_404(app_env):
    app, hub, alice_client = app_env
    alice, *_ = login(alice_client, hub, "alice")
    provision(app, "alice")
    created = alice_client.post(
        "/api/v1/workspaces",
        json={"profile_id": "python-standard", "profile_version": 1},
        headers=mutation_headers(alice, "alice-create"),
    ).json()

    # A separate browser session for Bob must not observe Alice's id.
    from fastapi.testclient import TestClient

    with TestClient(app, base_url="https://platform.example.com") as bob_client:
        login(bob_client, hub, "bob")
        response = bob_client.get(f"/api/v1/workspaces/{created['workspace']['id']}")
        assert response.status_code == 404


def test_stopped_workspace_creation_does_not_reserve_runtime_capacity(app_env):
    app, hub, client = app_env
    me, *_ = login(client, hub, "alice")
    provision(app, "alice")
    # Creation only binds an isolated storage slot. Runtime capacity is reserved
    # later, when the owner explicitly starts the configured workspace.
    for number in range(5):
        response = client.post(
            "/api/v1/workspaces",
            json={"profile_id": "python-standard", "profile_version": 1},
            headers=mutation_headers(me, f"reservation-{number}"),
        )
        assert response.status_code == 202
    capacity = client.get("/api/v1/capacity").json()
    assert capacity["global"]["active"] == 0
    assert capacity["global"]["resources"] == {
        "cpu_millicores": {"reserved": 0, "limit": 16_000},
        "memory_mb": {"reserved": 0, "limit": 16_384},
    }


def test_local_launch_url_requires_exact_configured_port(settings):
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
    valid = "http://alice.hub.localhost:8080/user/alice/ws-12345678/"
    assert (
        validate_launch_url(
            valid, username="alice", server_name="ws-12345678", settings=local
        )
        == valid
    )


def test_domain_launch_url_requires_exact_user_subdomain_origin_and_path(settings):
    domain = settings.__class__(
        **{
            **settings.__dict__,
            "portal_origin": "https://platform.workspace.test:3030",
            "hub_public_url": "https://hub.workspace.test:3030",
            "hub_user_domain": "hub.workspace.test",
            "oauth_redirect_uri": (
                "https://platform.workspace.test:3030/api/v1/auth/callback"
            ),
            "domain_test": True,
        }
    )
    domain.validate()
    valid = "https://alice.hub.workspace.test:3030/user/alice/ws-12345678/"
    assert (
        validate_launch_url(
            valid, username="alice", server_name="ws-12345678", settings=domain
        )
        == valid
    )

    invalid = (
        "https://alice.hub.workspace.test/user/alice/ws-12345678/",
        "http://alice.hub.workspace.test:3030/user/alice/ws-12345678/",
        "https://bob.hub.workspace.test:3030/user/alice/ws-12345678/",
        "https://alice.hub.workspace.test.attacker.test:3030/user/alice/ws-12345678/",
        "https://alice.hub.workspace.test:3030/user/alice/ws-other/",
        "https://alice.hub.workspace.test:3030/user/alice/ws-12345678/?next=/",
    )
    for value in invalid:
        with pytest.raises(AppError, match="exact origin/path"):
            validate_launch_url(
                value,
                username="alice",
                server_name="ws-12345678",
                settings=domain,
            )
