from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from ..db import begin_immediate
from ..domain import UserRole, UserStatus
from ..errors import AppError
from ..hub.base import HubAuthError, HubUnavailableError, JupyterHubProvider
from ..models import AuthTransaction, AuditEvent, User, UserSession
from ..security import (
    TokenCipher,
    csrf_token,
    json_dumps_safe,
    keyed_hash,
    pkce_challenge,
    random_token,
    safe_redirect_path,
    validate_hub_username,
)


SESSION_COOKIE = "__Host-platform-session"
PREAUTH_COOKIE = "__Host-platform-preauth"


@dataclass(frozen=True)
class LoginStart:
    authorization_url: str
    preauth_cookie: str


@dataclass(frozen=True)
class LoginResult:
    session_cookie: str
    redirect_path: str
    user: User


@dataclass(frozen=True)
class SessionContext:
    raw_cookie: str
    session: UserSession
    user: User


class AuthService:
    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session],
        hub: JupyterHubProvider,
        cipher: TokenCipher,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.hub = hub
        self.cipher = cipher

    def begin_login(self, redirect_path: str | None) -> LoginStart:
        now = datetime.utcnow()
        preauth_cookie = random_token()
        state = random_token()
        verifier = random_token(48)
        id_hash = keyed_hash(preauth_cookie, self.settings.session_hash_key)
        transaction = AuthTransaction(
            id_hash=id_hash,
            state_hash=keyed_hash(state, self.settings.session_hash_key),
            pkce_verifier_cipher=self.cipher.encrypt(
                verifier, purpose=f"pkce:{id_hash}"
            ),
            redirect_path=safe_redirect_path(redirect_path),
            expires_at=now + timedelta(seconds=self.settings.auth_transaction_seconds),
        )
        with self.session_factory() as db:
            db.add(transaction)
            db.commit()
        return LoginStart(
            authorization_url=self.hub.authorization_url(
                state=state, code_challenge=pkce_challenge(verifier)
            ),
            preauth_cookie=preauth_cookie,
        )

    async def finish_login(
        self, *, preauth_cookie: str | None, state: str, code: str, request_id: str
    ) -> LoginResult:
        if not preauth_cookie:
            raise AppError(
                400, "OAUTH_STATE_INVALID", "Login transaction cookie is missing"
            )
        now = datetime.utcnow()
        id_hash = keyed_hash(preauth_cookie, self.settings.session_hash_key)
        state_hash = keyed_hash(state, self.settings.session_hash_key)

        with self.session_factory() as db:
            begin_immediate(db)
            transaction = db.get(AuthTransaction, id_hash)
            if (
                transaction is None
                or transaction.state_hash != state_hash
                or transaction.consumed_at is not None
                or transaction.expires_at <= now
            ):
                db.rollback()
                raise AppError(
                    400,
                    "OAUTH_STATE_INVALID",
                    "Login state is invalid, expired, or reused",
                )
            transaction.consumed_at = now
            verifier_cipher = transaction.pkce_verifier_cipher
            redirect_path = transaction.redirect_path
            db.commit()

        verifier = self.cipher.decrypt(verifier_cipher, purpose=f"pkce:{id_hash}")
        try:
            oauth_token = await self.hub.exchange_code(
                code=code, pkce_verifier=verifier
            )
            principal = await self.hub.resolve_principal(oauth_token.access_token)
        except HubAuthError as exc:
            raise AppError(
                401, "HUB_AUTH_FAILED", "JupyterHub rejected the login"
            ) from exc
        except HubUnavailableError as exc:
            raise AppError(
                503, "HUB_UNAVAILABLE", "JupyterHub login is temporarily unavailable"
            ) from exc

        try:
            username = validate_hub_username(principal.username)
        except ValueError as exc:
            raise AppError(403, "USERNAME_POLICY_REJECTED", str(exc)) from exc
        if not principal.can_manage_own_servers():
            raise AppError(
                403, "HUB_SCOPE_MISSING", "Required delegated server scope is missing"
            )

        raw_session = random_token()
        session_hash = keyed_hash(raw_session, self.settings.session_hash_key)
        absolute_expires = min(
            now + timedelta(seconds=self.settings.session_absolute_seconds),
            oauth_token.expires_at,
        )
        idle_expires = min(
            now + timedelta(seconds=self.settings.session_idle_seconds),
            absolute_expires,
        )

        with self.session_factory() as db:
            begin_immediate(db)
            user = db.scalar(
                select(User).where(
                    User.auth_provider == "jupyterhub", User.auth_subject == username
                )
            )
            if user is None:
                user = User(
                    id=str(uuid.uuid4()),
                    auth_provider="jupyterhub",
                    auth_subject=username,
                    hub_username=username,
                    role=(
                        UserRole.ADMIN.value
                        if username in self.settings.admin_usernames
                        else UserRole.USER.value
                    ),
                    status=UserStatus.PROVISIONING.value,
                )
                db.add(user)
                db.flush()
            if user.hub_username != username:
                db.rollback()
                raise AppError(
                    403, "IDENTITY_MISMATCH", "Stored Hub identity does not match"
                )
            if user.status == UserStatus.DISABLED.value:
                db.add(
                    AuditEvent(
                        id=str(uuid.uuid4()),
                        actor_user_id=user.id,
                        action="LOGIN",
                        result="DENIED",
                        request_id=request_id,
                        safe_metadata_json=json_dumps_safe({"reason": "USER_DISABLED"}),
                    )
                )
                db.commit()
                raise AppError(403, "USER_DISABLED", "The platform account is disabled")

            expected_role = (
                UserRole.ADMIN.value
                if username in self.settings.admin_usernames
                else UserRole.USER.value
            )
            if user.role != expected_role:
                previous_role = user.role
                user.role = expected_role
                user.updated_at = now
                db.add(
                    AuditEvent(
                        id=str(uuid.uuid4()),
                        actor_user_id=user.id,
                        action="ADMIN_ROLE_RECONCILED",
                        result="UPDATED",
                        request_id=request_id,
                        safe_metadata_json=json_dumps_safe(
                            {
                                "previous_role": previous_role,
                                "current_role": expected_role,
                                "source": "CONFIGURED_ADMIN_ALLOWLIST",
                            }
                        ),
                    )
                )

            portal_session = UserSession(
                id_hash=session_hash,
                user_id=user.id,
                hub_oauth_token_cipher=self.cipher.encrypt(
                    oauth_token.access_token, purpose=f"hub-oauth:{session_hash}"
                ),
                hub_scopes_json=json.dumps(list(principal.scopes)),
                hub_oauth_expires_at=oauth_token.expires_at,
                created_at=now,
                last_seen_bucket=now,
                absolute_expires_at=absolute_expires,
                idle_expires_at=idle_expires,
            )
            db.add(portal_session)
            db.add(
                AuditEvent(
                    id=str(uuid.uuid4()),
                    actor_user_id=user.id,
                    action="LOGIN",
                    result="SUCCEEDED",
                    request_id=request_id,
                    safe_metadata_json="{}",
                )
            )
            db.commit()
            db.refresh(user)
            return LoginResult(raw_session, redirect_path, user)

    def authenticate(
        self,
        db: Session,
        raw_cookie: str | None,
        *,
        request_id: str = "role-reconciliation",
    ) -> SessionContext:
        if not raw_cookie:
            raise AppError(401, "AUTH_REQUIRED", "Portal authentication is required")
        now = datetime.utcnow()
        session_hash = keyed_hash(raw_cookie, self.settings.session_hash_key)
        portal_session = db.get(UserSession, session_hash)
        if (
            portal_session is None
            or portal_session.revoked_at is not None
            or portal_session.absolute_expires_at <= now
            or portal_session.idle_expires_at <= now
        ):
            raise AppError(401, "AUTH_REQUIRED", "Portal session is invalid or expired")
        user = db.get(User, portal_session.user_id)
        if user is None or user.status == UserStatus.DISABLED.value:
            raise AppError(401, "AUTH_REQUIRED", "Portal account is not available")

        expected_role = (
            UserRole.ADMIN.value
            if user.hub_username in self.settings.admin_usernames
            else UserRole.USER.value
        )
        role_changed = user.role != expected_role
        if role_changed:
            previous_role = user.role
            user.role = expected_role
            user.updated_at = now
            db.add(
                AuditEvent(
                    id=str(uuid.uuid4()),
                    actor_user_id=user.id,
                    action="ADMIN_ROLE_RECONCILED",
                    result="UPDATED",
                    request_id=request_id,
                    safe_metadata_json=json_dumps_safe(
                        {
                            "previous_role": previous_role,
                            "current_role": expected_role,
                            "source": "CONFIGURED_ADMIN_ALLOWLIST",
                        }
                    ),
                )
            )

        # Bucket activity writes so polling does not turn every request into a SQLite writer.
        activity_changed = portal_session.last_seen_bucket <= now - timedelta(minutes=5)
        if activity_changed:
            portal_session.last_seen_bucket = now.replace(second=0, microsecond=0)
            portal_session.idle_expires_at = min(
                now + timedelta(seconds=self.settings.session_idle_seconds),
                portal_session.absolute_expires_at,
            )
        if role_changed or activity_changed:
            db.commit()
        return SessionContext(raw_cookie, portal_session, user)

    def csrf_for(self, context: SessionContext) -> str:
        return csrf_token(context.raw_cookie, self.settings.session_hash_key)

    def verify_csrf(
        self, context: SessionContext, supplied: str | None, origin: str | None
    ) -> None:
        if origin != self.settings.portal_origin:
            raise AppError(
                403, "CSRF_ORIGIN_INVALID", "Request Origin is not the portal origin"
            )
        expected = self.csrf_for(context)
        if not supplied or not __import__("hmac").compare_digest(expected, supplied):
            raise AppError(
                403, "CSRF_TOKEN_INVALID", "CSRF token is missing or invalid"
            )

    def logout(self, db: Session, context: SessionContext, request_id: str) -> str:
        now = datetime.utcnow()
        context.session.revoked_at = now
        context.session.hub_oauth_token_cipher = None
        db.add(
            AuditEvent(
                id=str(uuid.uuid4()),
                actor_user_id=context.user.id,
                action="LOGOUT",
                result="SUCCEEDED",
                request_id=request_id,
                safe_metadata_json="{}",
            )
        )
        db.commit()
        # Portal and Hub browser sessions use different host-only cookies. The
        # browser must visit the Hub logout handler after the portal session is
        # revoked, otherwise the next OAuth authorize request silently reuses
        # the previous Hub identity.
        return f"{self.settings.hub_public_url.rstrip('/')}/hub/logout"

    def password_change_url(self) -> str:
        """Return the exact NativeAuthenticator self-service password route.

        Password fields are submitted directly to JupyterHub.  The portal only
        provides an authenticated navigation boundary and never receives or
        stores either the old or new password.
        """

        return f"{self.settings.hub_public_url.rstrip('/')}/hub/change-password"

    def decrypt_hub_token(self, portal_session: UserSession) -> str:
        now = datetime.utcnow()
        if (
            portal_session.revoked_at is not None
            or portal_session.hub_oauth_token_cipher is None
            or portal_session.hub_oauth_expires_at <= now
            or portal_session.absolute_expires_at <= now
        ):
            raise AppError(401, "AUTH_REQUIRED", "A fresh JupyterHub login is required")
        return self.cipher.decrypt(
            portal_session.hub_oauth_token_cipher,
            purpose=f"hub-oauth:{portal_session.id_hash}",
        )
