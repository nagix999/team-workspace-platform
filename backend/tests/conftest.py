from __future__ import annotations

import base64
import uuid
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.db import Base
from app.hub import FakeJupyterHubProvider
from app.main import create_app
from app.models import User, WorkspaceProfile, WorkspaceVolumeSlot
from app.services.profile_offers import ensure_default_offers
from app.services.resource_policy import get_resource_policy


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'platform.db'}",
        portal_origin="https://platform.example.com",
        hub_internal_url="http://hub:8081",
        hub_public_url="https://hub.example.net",
        hub_user_domain="hub.example.net",
        oauth_client_id="service-platform-api",
        oauth_client_secret="test-oauth-client-secret",
        oauth_redirect_uri="https://platform.example.com/api/v1/auth/callback",
        token_encryption_key=base64.urlsafe_b64encode(bytes(range(32))).decode(),
        token_encryption_key_id="test-v1",
        session_hash_key="test-session-hash-key-that-is-long",
        internal_hmac_key="test-internal-hmac-key-that-is-long",
        worker_retry_seconds=0,
        workspace_cpu_budget_millicores=16_000,
        workspace_memory_budget_mb=16_384,
        portal_security_domain="example.com",
        hub_user_security_domain="example.net",
    )


@pytest.fixture
def app_env(settings):
    hub = FakeJupyterHubProvider()
    app = create_app(settings, hub)
    Base.metadata.create_all(app.state.engine)
    with TestClient(app, base_url=settings.portal_origin) as client:
        yield app, hub, client


def login(
    client: TestClient,
    hub: FakeJupyterHubProvider,
    username: str,
    *,
    token: str | None = None,
):
    code = f"code-{username}-{uuid.uuid4().hex}"
    token = token or f"oauth-{username}-{uuid.uuid4().hex}"
    hub.register_login(username, code=code, token=token)
    started = client.get("/api/v1/auth/login", follow_redirects=False)
    assert started.status_code == 302
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    callback = client.get(
        "/api/v1/auth/callback",
        params={"code": code, "state": state},
        follow_redirects=False,
    )
    assert callback.status_code == 303, callback.text
    me = client.get("/api/v1/me")
    assert me.status_code == 200
    return me.json(), state, code, token


def provision(app, username: str, *, slots: int = 5) -> str:
    factory = app.state.session_factory
    with factory() as db:
        user = db.scalar(select(User).where(User.hub_username == username))
        assert user is not None
        user.status = "ACTIVE"
        profile = db.get(WorkspaceProfile, ("python-standard", 1))
        if profile is None:
            db.add(
                WorkspaceProfile(
                    id="python-standard",
                    version=1,
                    name="Python standard",
                    kernel_name="python3",
                    kernel_display_name="Python 3",
                    python_version="3.12.0",
                    image_ref="example.invalid/singleuser@sha256:" + "a" * 64,
                    cpu_limit="1.0",
                    memory_limit_mb=1024,
                    pids_limit=256,
                    private_disk_limit_mb=1024,
                    private_disk_quota_enforced=False,
                    provider_options_json='{"gid":100,"private_disk_hard_limit_bytes":1073741824,"uid":1000}',
                    config_digest="sha256:" + "b" * 64,
                    enabled=True,
                    selectable=True,
                )
            )
        for number in range(1, slots + 1):
            db.add(
                WorkspaceVolumeSlot(
                    id=str(
                        uuid.uuid5(
                            uuid.UUID(user.id), f"workspace-volume-slot-{number}"
                        )
                    ),
                    owner_user_id=user.id,
                    slot_no=number,
                    volume_name=f"jupyter-user-{username}-slot-{number}",
                    quota_project_id=10000 + number + (abs(hash(username)) % 1000) * 10,
                    hard_limit_mb=1024,
                    provision_status="PROVISIONED",
                    verified_at=datetime.utcnow(),
                )
            )
        db.flush()
        ensure_default_offers(db)
        get_resource_policy(db, app.state.settings, create=True)
        db.commit()
        return user.id


def mutation_headers(me: dict, key: str) -> dict[str, str]:
    return {
        "Origin": "https://platform.example.com",
        "X-CSRF-Token": me["csrf_token"],
        "Idempotency-Key": key,
    }
