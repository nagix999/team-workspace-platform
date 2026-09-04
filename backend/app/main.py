import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Annotated
from urllib.parse import quote, urlsplit

from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import case, delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .access_logging import install_uvicorn_access_log_filter
from .config import Settings
from .db import (
    begin_immediate,
    create_database_engine,
    create_session_factory,
    session_dependency,
)
from .domain import DesiredState, ObservedState, UserRole
from .errors import AppError
from .hub import HTTPJupyterHubProvider, JupyterHubProvider
from .models import (
    AuditEvent,
    InternalRequestNonce,
    Operation,
    User,
    UserProvisioningJob,
    Workspace,
    WorkspaceDeletionJob,
    WorkspaceProfile,
    WorkspaceProfileOffer,
    WorkspaceVolumeSlot,
)
from .schemas import (
    EnvironmentVariablePut,
    InternalEgressPolicyRetry,
    InternalEgressRuleCreate,
    InternalEgressRuleUpdate,
    ProfileOfferCreate,
    ProfileOfferUpdate,
    ResourcePolicyUpdate,
    SpawnCheckRequest,
    SpawnConsumeRequest,
    UserProvisioningClaimRequest,
    UserProvisioningCompleteRequest,
    UserProvisioningFailRequest,
    WorkspaceCreate,
    WorkspaceDeletionClaimRequest,
    WorkspaceDeletionCompleteRequest,
    WorkspaceDeletionFailRequest,
)
from .security import SignedInternalRequest, TokenCipher, verify_internal_signature
from .serialization import (
    audit_dict,
    iso,
    operation_dict,
    user_dict,
    workspace_dict,
)
from .services.auth import AuthService, SessionContext
from .services.spawn import check_spawn_authorization, consume_spawn_authorization
from .services.deletions import (
    claim_deletion_job,
    complete_deletion_job,
    fail_deletion_job,
)
from .services.environment import (
    delete_environment_variable,
    environment_item_dict,
    list_environment_variables,
    put_environment_variable,
    workspace_restart_required,
)
from .services.internal_egress import (
    create_internal_egress_rule,
    delete_internal_egress_rule,
    get_internal_egress_policy,
    internal_egress_policy_dict,
    retry_internal_egress_policy,
    update_internal_egress_rule,
)
from .services.mutations import mutation_request, record_mutation, replay_mutation
from .services.profile_offers import (
    admin_profile_catalog,
    create_offer,
    disable_offer,
    offer_dict,
    public_offer_dict,
    update_offer,
)
from .services.resource_policy import (
    get_resource_policy,
    profile_is_allowed,
    resource_policy_dict,
    selected_resource_values,
    update_resource_policy,
)
from .services.provisioning import (
    claim_provisioning_job,
    complete_provisioning_job,
    fail_provisioning_job,
    provisioning_dict,
    provisioning_view,
    request_self_provisioning,
)
from .services.workspaces import (
    WorkspaceService,
    active_workspace,
    active_reservations,
    fresh_launch_url,
    owned_workspace,
    validate_launch_url,
)


EXPECTED_DATABASE_REVISION = "0009"


def create_app(
    settings: Settings | None = None,
    hub_provider: JupyterHubProvider | None = None,
) -> FastAPI:
    install_uvicorn_access_log_filter()
    settings = settings or Settings.from_env()
    settings.validate()
    engine = create_database_engine(settings)
    factory = create_session_factory(engine)
    cipher = TokenCipher(
        settings.token_encryption_key_id, settings.token_encryption_key
    )
    hub = hub_provider or HTTPJupyterHubProvider(settings)
    auth_service = AuthService(settings, factory, hub, cipher)
    workspace_service = WorkspaceService(settings, cipher)
    session_cookie_name = settings.session_cookie_name
    preauth_cookie_name = settings.preauth_cookie_name

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await hub.aclose()
        engine.dispose()

    app = FastAPI(
        title="Team Development Platform API",
        version="0.1.7",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = factory
    app.state.token_cipher = cipher
    app.state.hub = hub
    app.state.auth_service = auth_service
    app.state.workspace_service = workspace_service

    def get_db() -> Session:
        yield from session_dependency(factory)

    def serialize_workspace(db: Session, workspace: Workspace) -> dict[str, object]:
        profile = db.get(
            WorkspaceProfile, (workspace.profile_id, workspace.profile_version)
        )
        owner = db.get(User, workspace.owner_user_id)
        deletion_job = db.get(WorkspaceDeletionJob, workspace.id)
        latest_delete = db.scalar(
            select(Operation)
            .where(
                Operation.workspace_id == workspace.id,
                Operation.operation_type == "DELETE",
            )
            .order_by(Operation.requested_at.desc(), Operation.id.desc())
            .limit(1)
        )
        active_operation = db.scalar(
            select(Operation)
            .where(
                Operation.workspace_id == workspace.id,
                Operation.status.in_(["PENDING", "RUNNING", "WAITING_EXTERNAL"]),
            )
            .order_by(Operation.requested_at.desc(), Operation.id.desc())
            .limit(1)
        )
        return workspace_dict(
            workspace,
            profile,
            owner,
            deletion_job,
            latest_delete,
            active_operation,
            datetime.utcnow()
            - timedelta(seconds=settings.reconciliation_freshness_seconds),
            settings.reconciliation_freshness_seconds,
        )

    def current_context(
        request: Request,
        db: Annotated[Session, Depends(get_db)],
    ) -> SessionContext:
        return auth_service.authenticate(
            db,
            request.cookies.get(session_cookie_name),
            request_id=request.state.request_id,
        )

    def mutating_context(
        context: Annotated[SessionContext, Depends(current_context)],
        csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
        origin: Annotated[str | None, Header()] = None,
    ) -> SessionContext:
        auth_service.verify_csrf(context, csrf, origin)
        return context

    def has_admin_authority(context: SessionContext) -> bool:
        return bool(
            context.user.role == UserRole.ADMIN.value
            and context.user.hub_username in settings.admin_usernames
        )

    def admin_context(
        context: Annotated[SessionContext, Depends(current_context)],
    ) -> SessionContext:
        if not has_admin_authority(context):
            raise AppError(
                403, "ADMIN_REQUIRED", "Platform administrator role is required"
            )
        return context

    def admin_mutating_context(
        context: Annotated[SessionContext, Depends(mutating_context)],
    ) -> SessionContext:
        if not has_admin_authority(context):
            raise AppError(
                403, "ADMIN_REQUIRED", "Platform administrator role is required"
            )
        return context

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        supplied = request.headers.get("X-Request-ID", "")
        request_id = (
            supplied
            if 0 < len(supplied) <= 64 and supplied.isascii()
            else str(uuid.uuid4())
        )
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "request_id": getattr(request.state, "request_id", "unknown"),
                }
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, _exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "REQUEST_VALIDATION_FAILED",
                    "message": "Request did not match the API contract",
                    "request_id": getattr(request.state, "request_id", "unknown"),
                }
            },
        )

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    def readyz(db: Annotated[Session, Depends(get_db)]) -> dict[str, object]:
        try:
            db.execute(text("SELECT 1"))
        except Exception as exc:
            raise AppError(
                503, "DATABASE_UNAVAILABLE", "Platform database is unavailable"
            ) from exc
        try:
            revisions = db.scalars(
                text("SELECT version_num FROM alembic_version")
            ).all()
        except Exception as exc:
            raise AppError(
                503,
                "DATABASE_SCHEMA_NOT_READY",
                "Platform database schema is not ready",
            ) from exc
        if revisions != [EXPECTED_DATABASE_REVISION]:
            raise AppError(
                503,
                "DATABASE_SCHEMA_NOT_READY",
                "Platform database schema is not ready",
            )
        return {
            "status": "ready",
            "execution_host_healthy": settings.execution_host_healthy,
        }

    @app.get("/api/v1/auth/login")
    def login(redirect_path: str | None = Query(default="/")) -> RedirectResponse:
        started = auth_service.begin_login(redirect_path)
        response = RedirectResponse(started.authorization_url, status_code=302)
        response.set_cookie(
            preauth_cookie_name,
            started.preauth_cookie,
            max_age=settings.auth_transaction_seconds,
            secure=settings.cookie_secure,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return response

    @app.get("/api/v1/auth/callback")
    async def callback(
        request: Request,
        code: str,
        state: str,
    ) -> RedirectResponse:
        result = await auth_service.finish_login(
            preauth_cookie=request.cookies.get(preauth_cookie_name),
            state=state,
            code=code,
            request_id=request.state.request_id,
        )
        response = RedirectResponse(result.redirect_path, status_code=303)
        response.delete_cookie(
            preauth_cookie_name, secure=settings.cookie_secure, httponly=True, path="/"
        )
        response.set_cookie(
            session_cookie_name,
            result.session_cookie,
            max_age=settings.session_absolute_seconds,
            secure=settings.cookie_secure,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return response

    @app.post("/api/v1/auth/logout")
    def logout(
        request: Request,
        context: Annotated[SessionContext, Depends(mutating_context)],
        db: Annotated[Session, Depends(get_db)],
    ) -> Response:
        hub_logout_url = auth_service.logout(db, context, request.state.request_id)
        response = JSONResponse({"redirect_url": hub_logout_url})
        response.delete_cookie(
            session_cookie_name, secure=settings.cookie_secure, httponly=True, path="/"
        )
        return response

    @app.get("/api/v1/auth/change-password")
    def change_password(
        _context: Annotated[SessionContext, Depends(current_context)],
    ) -> RedirectResponse:
        return RedirectResponse(auth_service.password_change_url(), status_code=303)

    @app.get("/api/v1/me")
    def me(
        context: Annotated[SessionContext, Depends(current_context)],
        db: Annotated[Session, Depends(get_db)],
    ) -> dict[str, object]:
        job = db.get(UserProvisioningJob, context.user.id)
        return {
            "user": user_dict(context.user),
            "csrf_token": auth_service.csrf_for(context),
            "provisioning": provisioning_view(
                db=db,
                user=context.user,
                job=job,
                settings=settings,
            ),
        }

    @app.post("/api/v1/me/provisioning", status_code=202)
    def request_provisioning(
        request: Request,
        context: Annotated[SessionContext, Depends(mutating_context)],
        db: Annotated[Session, Depends(get_db)],
    ) -> dict[str, object]:
        job = request_self_provisioning(
            db,
            settings=settings,
            user=context.user,
            request_id=request.state.request_id,
        )
        return {
            "provisioning": provisioning_dict(
                job, max_attempts=settings.provisioning_max_attempts
            )
        }

    @app.get("/api/v1/workspace-profiles")
    def profiles(
        _context: Annotated[SessionContext, Depends(current_context)],
        db: Annotated[Session, Depends(get_db)],
    ) -> dict[str, object]:
        policy = get_resource_policy(db, settings)
        offers = db.scalars(
            select(WorkspaceProfileOffer)
            .where(WorkspaceProfileOffer.enabled.is_(True))
            .order_by(WorkspaceProfileOffer.created_at, WorkspaceProfileOffer.id)
        ).all()
        items: list[dict[str, object]] = []
        for offer in offers:
            runtime = db.get(
                WorkspaceProfile,
                (offer.runtime_profile_id, offer.runtime_profile_version),
            )
            if runtime is not None and profile_is_allowed(runtime, policy):
                items.append(public_offer_dict(offer, runtime))
        return {"items": items}

    @app.get("/api/v1/capacity")
    def capacity(
        context: Annotated[SessionContext, Depends(current_context)],
        db: Annotated[Session, Depends(get_db)],
    ) -> dict[str, object]:
        used = int(
            db.scalar(
                select(func.count(Workspace.id)).where(
                    Workspace.owner_user_id == context.user.id,
                    Workspace.archived_at.is_(None),
                )
            )
            or 0
        )
        reservations = active_reservations(db)
        policy = get_resource_policy(db, settings)
        used_slot_ids = select(Workspace.private_volume_slot_id).where(
            Workspace.owner_user_id == context.user.id,
            Workspace.archived_at.is_(None),
        )
        next_slot = db.scalar(
            select(WorkspaceVolumeSlot)
            .where(
                WorkspaceVolumeSlot.owner_user_id == context.user.id,
                WorkspaceVolumeSlot.provision_status == "PROVISIONED",
                ~WorkspaceVolumeSlot.id.in_(used_slot_ids),
            )
            .order_by(WorkspaceVolumeSlot.slot_no)
            .limit(1)
        )
        return {
            "user": {
                "used": used,
                "limit": settings.max_workspaces_per_user,
                "next_default_workspace_name": (
                    f"환경-{next_slot.slot_no}" if next_slot is not None else None
                ),
            },
            "global": {
                "active": reservations.count,
                "limit": settings.max_active_workspaces,
                "kernel_idle_timeout_seconds": policy.kernel_idle_timeout_seconds,
                "resources": {
                    "cpu_millicores": {
                        "reserved": reservations.cpu_millicores,
                        "limit": policy.cpu_budget_millicores,
                    },
                    "memory_mb": {
                        "reserved": reservations.memory_mb,
                        "limit": policy.memory_budget_mb,
                    },
                    "gpu_count": {
                        "reserved": reservations.gpu_count,
                        "limit": policy.gpu_budget_count,
                    },
                },
            },
            "execution_host_healthy": settings.execution_host_healthy,
        }

    @app.get("/api/v1/workspaces")
    def list_workspaces(
        context: Annotated[SessionContext, Depends(current_context)],
        db: Annotated[Session, Depends(get_db)],
    ) -> dict[str, object]:
        rows = db.scalars(
            select(Workspace).where(
                Workspace.owner_user_id == context.user.id,
                Workspace.archived_at.is_(None),
            )
            # Offset pagination must have a total order. Timestamps can tie
            # during migration/backfill or concurrent requests.
            .order_by(Workspace.created_at.desc(), Workspace.id.desc())
        ).all()
        return {"items": [serialize_workspace(db, row) for row in rows]}

    @app.post("/api/v1/workspaces", status_code=202)
    def create_workspace(
        payload: WorkspaceCreate,
        request: Request,
        context: Annotated[SessionContext, Depends(mutating_context)],
        db: Annotated[Session, Depends(get_db)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, object]:
        key = _idempotency_key(idempotency_key)
        result = workspace_service.create(
            db,
            user=context.user,
            portal_session=context.session,
            profile_id=payload.profile_id,
            profile_version=payload.profile_version,
            display_name=payload.name,
            idempotency_key=key,
            request_id=request.state.request_id,
        )
        return {
            "workspace": serialize_workspace(db, result.workspace),
            "operation": operation_dict(result.operation),
        }

    @app.get("/api/v1/workspaces/{workspace_id}")
    def get_workspace(
        workspace_id: str,
        context: Annotated[SessionContext, Depends(current_context)],
        db: Annotated[Session, Depends(get_db)],
    ) -> dict[str, object]:
        return {
            "workspace": serialize_workspace(
                db, owned_workspace(db, context.user.id, workspace_id)
            )
        }

    def _require_environment_owner(context: SessionContext) -> User:
        if context.user.status != "ACTIVE":
            raise AppError(403, "USER_NOT_ACTIVE", "The platform account is not active")
        return context.user

    def _owner_has_restart_required(db: Session, owner: User) -> bool:
        rows = db.scalars(
            select(Workspace).where(
                Workspace.owner_user_id == owner.id,
                Workspace.archived_at.is_(None),
            )
        ).all()
        return any(workspace_restart_required(row, owner) for row in rows)

    @app.get("/api/v1/me/environment-variables")
    def get_user_environment(
        context: Annotated[SessionContext, Depends(current_context)],
        db: Annotated[Session, Depends(get_db)],
    ) -> dict[str, object]:
        owner = _require_environment_owner(context)
        items = list_environment_variables(
            db, owner_user_id=owner.id, workspace_id=None
        )
        restart_count = sum(
            int(workspace_restart_required(row, owner))
            for row in db.scalars(
                select(Workspace).where(
                    Workspace.owner_user_id == owner.id,
                    Workspace.archived_at.is_(None),
                )
            ).all()
        )
        return {
            "items": [environment_item_dict(item) for item in items],
            "restart_required": restart_count > 0,
            "restart_required_workspace_count": restart_count,
        }

    @app.put("/api/v1/me/environment-variables/{name}")
    def put_user_environment(
        name: str,
        payload: EnvironmentVariablePut,
        request: Request,
        context: Annotated[SessionContext, Depends(mutating_context)],
        db: Annotated[Session, Depends(get_db)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, object]:
        owner = _require_environment_owner(context)
        key = _idempotency_key(idempotency_key)
        mutation = mutation_request(
            action="USER_ENVIRONMENT_PUT",
            target_key=f"user:{owner.id}:{name}",
            payload=payload.model_dump(),
            fingerprint_key=settings.internal_hmac_key,
        )
        begin_immediate(db)
        replay = replay_mutation(
            db,
            actor_user_id=owner.id,
            idempotency_key=key,
            request=mutation,
        )
        if replay is not None:
            db.commit()
            return replay
        item, changed = put_environment_variable(
            db,
            cipher=cipher,
            fingerprint_key=settings.internal_hmac_key,
            owner=owner,
            workspace=None,
            name=name,
            value=payload.value,
            is_secret=payload.is_secret,
            expected_version=payload.expected_version,
            actor_user_id=owner.id,
            request_id=request.state.request_id,
        )
        safe_item = environment_item_dict(item)
        safe_item["value"] = None
        response = {
            "item": safe_item,
            "changed": changed,
            "restart_required": _owner_has_restart_required(db, owner),
        }
        record_mutation(
            db,
            actor_user_id=owner.id,
            idempotency_key=key,
            request=mutation,
            response=response,
        )
        db.commit()
        return response

    @app.delete("/api/v1/me/environment-variables/{name}")
    def delete_user_environment(
        name: str,
        expected_version: int,
        request: Request,
        context: Annotated[SessionContext, Depends(mutating_context)],
        db: Annotated[Session, Depends(get_db)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, object]:
        owner = _require_environment_owner(context)
        key = _idempotency_key(idempotency_key)
        mutation = mutation_request(
            action="USER_ENVIRONMENT_DELETE",
            target_key=f"user:{owner.id}:{name}",
            payload={"expected_version": expected_version},
            fingerprint_key=settings.internal_hmac_key,
        )
        begin_immediate(db)
        replay = replay_mutation(
            db,
            actor_user_id=owner.id,
            idempotency_key=key,
            request=mutation,
        )
        if replay is not None:
            db.commit()
            return replay
        item = delete_environment_variable(
            db,
            cipher=cipher,
            hmac_key=settings.internal_hmac_key,
            owner=owner,
            workspace=None,
            name=name,
            expected_version=expected_version,
            actor_user_id=owner.id,
            request_id=request.state.request_id,
        )
        response = {
            "item": None,
            "changed": True,
            "deleted": True,
            "name": item.name,
            "scope": item.scope,
            "version": item.row_version,
            "restart_required": _owner_has_restart_required(db, owner),
        }
        record_mutation(
            db,
            actor_user_id=owner.id,
            idempotency_key=key,
            request=mutation,
            response=response,
        )
        db.commit()
        return response

    @app.get("/api/v1/workspaces/{workspace_id}/environment-variables")
    def get_workspace_environment(
        workspace_id: str,
        context: Annotated[SessionContext, Depends(current_context)],
        db: Annotated[Session, Depends(get_db)],
    ) -> dict[str, object]:
        owner = _require_environment_owner(context)
        workspace = owned_workspace(db, owner.id, workspace_id)
        items = list_environment_variables(
            db, owner_user_id=owner.id, workspace_id=workspace.id
        )
        return {
            "items": [environment_item_dict(item) for item in items],
            "restart_required": workspace_restart_required(workspace, owner),
        }

    @app.put("/api/v1/workspaces/{workspace_id}/environment-variables/{name}")
    def put_workspace_environment(
        workspace_id: str,
        name: str,
        payload: EnvironmentVariablePut,
        request: Request,
        context: Annotated[SessionContext, Depends(mutating_context)],
        db: Annotated[Session, Depends(get_db)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, object]:
        owner = _require_environment_owner(context)
        key = _idempotency_key(idempotency_key)
        mutation = mutation_request(
            action="WORKSPACE_ENVIRONMENT_PUT",
            target_key=f"workspace:{workspace_id}:{name}",
            payload=payload.model_dump(),
            fingerprint_key=settings.internal_hmac_key,
        )
        begin_immediate(db)
        replay = replay_mutation(
            db,
            actor_user_id=owner.id,
            idempotency_key=key,
            request=mutation,
        )
        if replay is not None:
            db.commit()
            return replay
        workspace = owned_workspace(db, owner.id, workspace_id)
        if workspace.deletion_started_at is not None:
            raise AppError(409, "WORKSPACE_DELETING", "Workspace deletion has started")
        item, changed = put_environment_variable(
            db,
            cipher=cipher,
            fingerprint_key=settings.internal_hmac_key,
            owner=owner,
            workspace=workspace,
            name=name,
            value=payload.value,
            is_secret=payload.is_secret,
            expected_version=payload.expected_version,
            actor_user_id=owner.id,
            request_id=request.state.request_id,
        )
        safe_item = environment_item_dict(item)
        safe_item["value"] = None
        response = {
            "item": safe_item,
            "changed": changed,
            "restart_required": workspace_restart_required(workspace, owner),
        }
        record_mutation(
            db,
            actor_user_id=owner.id,
            idempotency_key=key,
            request=mutation,
            response=response,
        )
        db.commit()
        return response

    @app.delete("/api/v1/workspaces/{workspace_id}/environment-variables/{name}")
    def delete_workspace_environment(
        workspace_id: str,
        name: str,
        expected_version: int,
        request: Request,
        context: Annotated[SessionContext, Depends(mutating_context)],
        db: Annotated[Session, Depends(get_db)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, object]:
        owner = _require_environment_owner(context)
        key = _idempotency_key(idempotency_key)
        mutation = mutation_request(
            action="WORKSPACE_ENVIRONMENT_DELETE",
            target_key=f"workspace:{workspace_id}:{name}",
            payload={"expected_version": expected_version},
            fingerprint_key=settings.internal_hmac_key,
        )
        begin_immediate(db)
        replay = replay_mutation(
            db,
            actor_user_id=owner.id,
            idempotency_key=key,
            request=mutation,
        )
        if replay is not None:
            db.commit()
            return replay
        workspace = owned_workspace(db, owner.id, workspace_id)
        if workspace.deletion_started_at is not None:
            raise AppError(409, "WORKSPACE_DELETING", "Workspace deletion has started")
        item = delete_environment_variable(
            db,
            cipher=cipher,
            hmac_key=settings.internal_hmac_key,
            owner=owner,
            workspace=workspace,
            name=name,
            expected_version=expected_version,
            actor_user_id=owner.id,
            request_id=request.state.request_id,
        )
        response = {
            "item": None,
            "changed": True,
            "deleted": True,
            "name": item.name,
            "scope": item.scope,
            "version": item.row_version,
            "restart_required": workspace_restart_required(workspace, owner),
        }
        record_mutation(
            db,
            actor_user_id=owner.id,
            idempotency_key=key,
            request=mutation,
            response=response,
        )
        db.commit()
        return response

    def _workspace_action(
        target: DesiredState,
        *,
        workspace_id: str,
        request: Request,
        context: SessionContext,
        db: Session,
        idempotency_key: str | None,
    ) -> dict[str, object]:
        result = workspace_service.action(
            db,
            owner=context.user,
            actor=context.user,
            portal_session=context.session,
            credential_mode="USER_DELEGATED",
            workspace_id=workspace_id,
            target=target,
            idempotency_key=_idempotency_key(idempotency_key),
            request_id=request.state.request_id,
        )
        return {
            "workspace": serialize_workspace(db, result.workspace),
            "operation": operation_dict(result.operation),
        }

    @app.post("/api/v1/workspaces/{workspace_id}/actions/start", status_code=202)
    def start_workspace(
        workspace_id: str,
        request: Request,
        context: Annotated[SessionContext, Depends(mutating_context)],
        db: Annotated[Session, Depends(get_db)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, object]:
        return _workspace_action(
            DesiredState.RUNNING,
            workspace_id=workspace_id,
            request=request,
            context=context,
            db=db,
            idempotency_key=idempotency_key,
        )

    @app.post("/api/v1/workspaces/{workspace_id}/actions/restart", status_code=202)
    def restart_workspace(
        workspace_id: str,
        request: Request,
        context: Annotated[SessionContext, Depends(mutating_context)],
        db: Annotated[Session, Depends(get_db)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, object]:
        result = workspace_service.restart(
            db,
            owner=context.user,
            actor=context.user,
            portal_session=context.session,
            credential_mode="USER_DELEGATED",
            workspace_id=workspace_id,
            idempotency_key=_idempotency_key(idempotency_key),
            request_id=request.state.request_id,
        )
        return {
            "workspace": serialize_workspace(db, result.workspace),
            "operation": operation_dict(result.operation),
        }

    @app.delete("/api/v1/workspaces/{workspace_id}", status_code=202)
    def delete_workspace(
        workspace_id: str,
        request: Request,
        context: Annotated[SessionContext, Depends(mutating_context)],
        db: Annotated[Session, Depends(get_db)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, object]:
        result = workspace_service.delete(
            db,
            owner=context.user,
            actor=context.user,
            portal_session=context.session,
            credential_mode="USER_DELEGATED",
            workspace_id=workspace_id,
            idempotency_key=_idempotency_key(idempotency_key),
            request_id=request.state.request_id,
        )
        return {
            "workspace": serialize_workspace(db, result.workspace),
            "operation": operation_dict(result.operation),
        }

    @app.post("/api/v1/workspaces/{workspace_id}/actions/stop", status_code=202)
    def stop_workspace(
        workspace_id: str,
        request: Request,
        context: Annotated[SessionContext, Depends(mutating_context)],
        db: Annotated[Session, Depends(get_db)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, object]:
        return _workspace_action(
            DesiredState.STOPPED,
            workspace_id=workspace_id,
            request=request,
            context=context,
            db=db,
            idempotency_key=idempotency_key,
        )

    @app.get("/api/v1/operations/{operation_id}")
    def get_operation(
        operation_id: str,
        context: Annotated[SessionContext, Depends(current_context)],
        db: Annotated[Session, Depends(get_db)],
    ) -> dict[str, object]:
        query = select(Operation).where(Operation.id == operation_id)
        if not has_admin_authority(context):
            query = query.where(Operation.requested_by_user_id == context.user.id)
        operation = db.scalar(query)
        if operation is None:
            raise AppError(404, "OPERATION_NOT_FOUND", "Operation was not found")
        return {"operation": operation_dict(operation)}

    @app.get("/api/v1/workspaces/{workspace_id}/launch")
    async def launch_workspace(
        workspace_id: str,
        context: Annotated[SessionContext, Depends(current_context)],
        db: Annotated[Session, Depends(get_db)],
    ) -> RedirectResponse:
        workspace = owned_workspace(db, context.user.id, workspace_id)
        if (
            workspace.observed_state != ObservedState.RUNNING.value
            or workspace.stale
            or workspace.deletion_started_at is not None
        ):
            raise AppError(
                409,
                "WORKSPACE_NOT_RUNNING",
                "Workspace is not in a fresh running state",
            )
        token = auth_service.decrypt_hub_token(context.session)
        url = await fresh_launch_url(
            db=db,
            workspace=workspace,
            user=context.user,
            portal_session=context.session,
            token=token,
            hub=hub,
            settings=settings,
        )
        return RedirectResponse(url, status_code=303)

    @app.get("/api/v1/admin/workspaces")
    def admin_workspaces(
        _context: Annotated[SessionContext, Depends(admin_context)],
        db: Annotated[Session, Depends(get_db)],
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        owner_user_id: str | None = Query(default=None),
        include_archived: bool = Query(default=False),
    ) -> dict[str, object]:
        conditions: list[object] = []
        if not include_archived:
            conditions.append(Workspace.archived_at.is_(None))
        if owner_user_id is not None:
            conditions.append(Workspace.owner_user_id == owner_user_id)
        rows = db.scalars(
            select(Workspace)
            .where(*conditions)
            # Offset pagination must have a total order. Timestamps can tie
            # during migration/backfill or concurrent requests.
            .order_by(Workspace.created_at.desc(), Workspace.id.desc())
            .offset(offset)
            .limit(limit)
        ).all()
        items: list[dict[str, object]] = []
        for row in rows:
            item = serialize_workspace(db, row)
            owner = db.get(User, row.owner_user_id)
            if owner is None:  # pragma: no cover - guarded by FK
                continue
            item["owner"] = {
                "id": owner.id,
                "username": owner.hub_username,
                "display_name": owner.display_name,
                "status": owner.status,
            }
            items.append(item)
        return {
            "items": items,
            "total": int(
                db.scalar(select(func.count(Workspace.id)).where(*conditions)) or 0
            ),
            "limit": limit,
            "offset": offset,
        }

    def _admin_lifecycle_result(
        action: str,
        *,
        workspace_id: str,
        request: Request,
        context: SessionContext,
        db: Session,
        idempotency_key: str | None,
    ) -> dict[str, object]:
        workspace = active_workspace(db, workspace_id)
        owner = db.get(User, workspace.owner_user_id)
        if owner is None:  # pragma: no cover - guarded by FK
            raise AppError(500, "INVARIANT_VIOLATION", "Workspace owner is missing")
        key = _idempotency_key(idempotency_key)
        if action == "start":
            result = workspace_service.action(
                db,
                owner=owner,
                actor=context.user,
                portal_session=None,
                credential_mode="ADMIN_SERVICE",
                workspace_id=workspace.id,
                target=DesiredState.RUNNING,
                idempotency_key=key,
                request_id=request.state.request_id,
            )
        elif action == "stop":
            result = workspace_service.action(
                db,
                owner=owner,
                actor=context.user,
                portal_session=None,
                credential_mode="ADMIN_SERVICE",
                workspace_id=workspace.id,
                target=DesiredState.STOPPED,
                idempotency_key=key,
                request_id=request.state.request_id,
            )
        elif action == "restart":
            result = workspace_service.restart(
                db,
                owner=owner,
                actor=context.user,
                portal_session=None,
                credential_mode="ADMIN_SERVICE",
                workspace_id=workspace.id,
                idempotency_key=key,
                request_id=request.state.request_id,
            )
        elif action == "delete":
            result = workspace_service.delete(
                db,
                owner=owner,
                actor=context.user,
                portal_session=None,
                credential_mode="ADMIN_SERVICE",
                workspace_id=workspace.id,
                idempotency_key=key,
                request_id=request.state.request_id,
            )
        else:  # pragma: no cover - closed call sites
            raise RuntimeError("unknown admin lifecycle action")
        return {
            "workspace": serialize_workspace(db, result.workspace),
            "operation": operation_dict(result.operation),
        }

    @app.post("/api/v1/admin/workspaces/{workspace_id}/actions/start", status_code=202)
    def admin_start_workspace(
        workspace_id: str,
        request: Request,
        context: Annotated[SessionContext, Depends(admin_mutating_context)],
        db: Annotated[Session, Depends(get_db)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, object]:
        return _admin_lifecycle_result(
            "start",
            workspace_id=workspace_id,
            request=request,
            context=context,
            db=db,
            idempotency_key=idempotency_key,
        )

    @app.post("/api/v1/admin/workspaces/{workspace_id}/actions/stop", status_code=202)
    def admin_stop_workspace(
        workspace_id: str,
        request: Request,
        context: Annotated[SessionContext, Depends(admin_mutating_context)],
        db: Annotated[Session, Depends(get_db)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, object]:
        return _admin_lifecycle_result(
            "stop",
            workspace_id=workspace_id,
            request=request,
            context=context,
            db=db,
            idempotency_key=idempotency_key,
        )

    @app.post(
        "/api/v1/admin/workspaces/{workspace_id}/actions/restart", status_code=202
    )
    def admin_restart_workspace(
        workspace_id: str,
        request: Request,
        context: Annotated[SessionContext, Depends(admin_mutating_context)],
        db: Annotated[Session, Depends(get_db)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, object]:
        return _admin_lifecycle_result(
            "restart",
            workspace_id=workspace_id,
            request=request,
            context=context,
            db=db,
            idempotency_key=idempotency_key,
        )

    @app.delete("/api/v1/admin/workspaces/{workspace_id}", status_code=202)
    def admin_delete_workspace(
        workspace_id: str,
        request: Request,
        context: Annotated[SessionContext, Depends(admin_mutating_context)],
        db: Annotated[Session, Depends(get_db)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, object]:
        return _admin_lifecycle_result(
            "delete",
            workspace_id=workspace_id,
            request=request,
            context=context,
            db=db,
            idempotency_key=idempotency_key,
        )

    @app.get("/api/v1/admin/workspaces/{workspace_id}/launch")
    def admin_launch_workspace(
        workspace_id: str,
        request: Request,
        context: Annotated[SessionContext, Depends(admin_context)],
        db: Annotated[Session, Depends(get_db)],
    ) -> RedirectResponse:
        workspace = active_workspace(db, workspace_id)
        owner = db.get(User, workspace.owner_user_id)
        if owner is None:  # pragma: no cover - guarded by FK
            raise AppError(500, "INVARIANT_VIOLATION", "Workspace owner is missing")
        usage_snapshot_at = datetime.utcnow()
        freshness_cutoff = usage_snapshot_at - timedelta(
            seconds=settings.reconciliation_freshness_seconds
        )
        if (
            workspace.observed_state != ObservedState.RUNNING.value
            or workspace.stale
            or workspace.deletion_started_at is not None
            or workspace.last_reconciled_at is None
            or workspace.last_reconciled_at < freshness_cutoff
        ):
            raise AppError(409, "WORKSPACE_NOT_RUNNING", "Workspace is not running")
        origin = urlsplit(settings.hub_public_url)
        authority = f"{owner.hub_username}.{settings.hub_user_domain}"
        if origin.port is not None:
            authority = f"{authority}:{origin.port}"
        constructed = (
            f"{origin.scheme}://{authority}/user/"
            f"{quote(owner.hub_username, safe='')}/"
            f"{quote(workspace.hub_server_name, safe='')}/"
        )
        url = validate_launch_url(
            constructed,
            username=owner.hub_username,
            server_name=workspace.hub_server_name,
            settings=settings,
        )
        # This GET intentionally records the sensitive cross-user launch request.
        # The audit contains only stable identities, never the URL or a credential.
        _admin_audit(
            db,
            actor_user_id=context.user.id,
            workspace_id=workspace.id,
            action="ADMIN_WORKSPACE_LAUNCH_REQUESTED",
            request_id=request.state.request_id,
            metadata={"target_owner_user_id": owner.id},
        )
        db.commit()
        return RedirectResponse(url, status_code=303)

    def _admin_audit(
        db: Session,
        *,
        actor_user_id: str,
        workspace_id: str | None = None,
        action: str,
        request_id: str,
        metadata: dict[str, object],
    ) -> None:
        from .security import json_dumps_safe

        db.add(
            AuditEvent(
                id=str(uuid.uuid4()),
                actor_user_id=actor_user_id,
                workspace_id=workspace_id,
                action=action,
                result="ACCEPTED",
                request_id=request_id,
                safe_metadata_json=json_dumps_safe(metadata),
            )
        )

    @app.get("/api/v1/admin/settings")
    def admin_settings(
        _context: Annotated[SessionContext, Depends(admin_context)],
        db: Annotated[Session, Depends(get_db)],
    ) -> dict[str, object]:
        # Administrators must be able to inspect and lower a persisted policy
        # after a deployment hard ceiling changes. All admission/capacity reads
        # keep the default fail-closed validation.
        policy = get_resource_policy(db, settings, enforce_hard_ceiling=False)
        return {"resource_policy": resource_policy_dict(db, settings, policy)}

    @app.patch("/api/v1/admin/settings")
    def patch_admin_settings(
        payload: ResourcePolicyUpdate,
        request: Request,
        context: Annotated[SessionContext, Depends(admin_mutating_context)],
        db: Annotated[Session, Depends(get_db)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, object]:
        key = _idempotency_key(idempotency_key)
        mutation = mutation_request(
            action="RESOURCE_POLICY_UPDATE",
            target_key="resource-policy:1",
            # Keep an older v1 client's canonical request shape when it omits
            # later policy fields. Durable receipts created by that client can
            # then still be replayed after the corresponding schema migrations.
            payload=payload.model_dump(exclude_unset=True),
            fingerprint_key=settings.internal_hmac_key,
        )
        begin_immediate(db)
        replay = replay_mutation(
            db,
            actor_user_id=context.user.id,
            idempotency_key=key,
            request=mutation,
        )
        if replay is not None:
            db.commit()
            return replay
        reservations = active_reservations(db)
        current_policy = get_resource_policy(db, settings, enforce_hard_ceiling=False)
        _current_cpu, _current_memory, current_gpu_counts = selected_resource_values(
            current_policy
        )
        previous_gpu_budget_count = current_policy.gpu_budget_count
        previous_selectable_gpu_counts = sorted(current_gpu_counts)
        gpu_budget_count = (
            previous_gpu_budget_count
            if payload.gpu_budget_count is None
            else payload.gpu_budget_count
        )
        selectable_gpu_counts = (
            previous_selectable_gpu_counts
            if payload.selectable_gpu_counts is None
            else payload.selectable_gpu_counts
        )
        previous_kernel_idle_timeout_seconds = (
            current_policy.kernel_idle_timeout_seconds
        )
        kernel_idle_timeout_seconds = (
            previous_kernel_idle_timeout_seconds
            if payload.kernel_idle_timeout_seconds is None
            else payload.kernel_idle_timeout_seconds
        )
        policy = update_resource_policy(
            db,
            settings=settings,
            actor_user_id=context.user.id,
            expected_version=payload.version,
            cpu_budget_millicores=payload.cpu_budget_millicores,
            memory_budget_mb=payload.memory_budget_mb,
            selectable_cpu_millicores=payload.selectable_cpu_millicores,
            selectable_memory_mb=payload.selectable_memory_mb,
            gpu_budget_count=gpu_budget_count,
            selectable_gpu_counts=selectable_gpu_counts,
            kernel_idle_timeout_seconds=kernel_idle_timeout_seconds,
            reserved_cpu_millicores=reservations.cpu_millicores,
            reserved_memory_mb=reservations.memory_mb,
            reserved_gpu_count=reservations.gpu_count,
        )
        response = {"resource_policy": resource_policy_dict(db, settings, policy)}
        _admin_audit(
            db,
            actor_user_id=context.user.id,
            action="RESOURCE_POLICY_UPDATED",
            request_id=request.state.request_id,
            metadata={
                "version": policy.version,
                "cpu_budget_millicores": policy.cpu_budget_millicores,
                "memory_budget_mb": policy.memory_budget_mb,
                "selectable_cpu_millicores": payload.selectable_cpu_millicores,
                "selectable_memory_mb": payload.selectable_memory_mb,
                "previous_gpu_budget_count": previous_gpu_budget_count,
                "previous_selectable_gpu_counts": previous_selectable_gpu_counts,
                "gpu_budget_count": policy.gpu_budget_count,
                "selectable_gpu_counts": selectable_gpu_counts,
                "previous_kernel_idle_timeout_seconds": (
                    previous_kernel_idle_timeout_seconds
                ),
                "kernel_idle_timeout_seconds": policy.kernel_idle_timeout_seconds,
            },
        )
        record_mutation(
            db,
            actor_user_id=context.user.id,
            idempotency_key=key,
            request=mutation,
            response=response,
        )
        db.commit()
        return response

    @app.get("/api/v1/admin/internal-egress-policy")
    def get_admin_internal_egress_policy(
        _context: Annotated[SessionContext, Depends(admin_context)],
        db: Annotated[Session, Depends(get_db)],
    ) -> dict[str, object]:
        policy = get_internal_egress_policy(db, create=True)
        response = internal_egress_policy_dict(db, policy)
        # Metadata-created development databases do not run Alembic's singleton
        # seed. Persist the same fail-closed empty policy on first inspection.
        db.commit()
        return response

    @app.post("/api/v1/admin/internal-egress-policy/rules", status_code=201)
    def post_admin_internal_egress_rule(
        payload: InternalEgressRuleCreate,
        request: Request,
        context: Annotated[SessionContext, Depends(admin_mutating_context)],
        db: Annotated[Session, Depends(get_db)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, object]:
        key = _idempotency_key(idempotency_key)
        mutation = mutation_request(
            action="INTERNAL_EGRESS_RULE_CREATE",
            target_key="internal-egress-rule:new",
            payload=payload.model_dump(),
            fingerprint_key=settings.internal_hmac_key,
        )
        begin_immediate(db)
        replay = replay_mutation(
            db,
            actor_user_id=context.user.id,
            idempotency_key=key,
            request=mutation,
        )
        if replay is not None:
            db.commit()
            return replay
        policy, rule = create_internal_egress_rule(
            db,
            actor_user_id=context.user.id,
            expected_revision=payload.expected_revision,
            destination_cidr=payload.destination_cidr,
            port=payload.port,
        )
        response = internal_egress_policy_dict(db, policy)
        _admin_audit(
            db,
            actor_user_id=context.user.id,
            action="INTERNAL_EGRESS_RULE_CREATED",
            request_id=request.state.request_id,
            metadata={
                "rule_id": rule.id,
                "destination_cidr": rule.destination_cidr,
                "port": rule.port,
                "desired_revision": policy.desired_revision,
            },
        )
        record_mutation(
            db,
            actor_user_id=context.user.id,
            idempotency_key=key,
            request=mutation,
            response=response,
        )
        db.commit()
        return response

    @app.patch("/api/v1/admin/internal-egress-policy/rules/{rule_id}")
    def patch_admin_internal_egress_rule(
        rule_id: str,
        payload: InternalEgressRuleUpdate,
        request: Request,
        context: Annotated[SessionContext, Depends(admin_mutating_context)],
        db: Annotated[Session, Depends(get_db)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, object]:
        key = _idempotency_key(idempotency_key)
        mutation = mutation_request(
            action="INTERNAL_EGRESS_RULE_UPDATE",
            target_key=f"internal-egress-rule:{rule_id}",
            payload=payload.model_dump(),
            fingerprint_key=settings.internal_hmac_key,
        )
        begin_immediate(db)
        replay = replay_mutation(
            db,
            actor_user_id=context.user.id,
            idempotency_key=key,
            request=mutation,
        )
        if replay is not None:
            db.commit()
            return replay
        policy, rule = update_internal_egress_rule(
            db,
            rule_id=rule_id,
            actor_user_id=context.user.id,
            expected_revision=payload.expected_revision,
            expected_version=payload.expected_version,
            destination_cidr=payload.destination_cidr,
            port=payload.port,
        )
        response = internal_egress_policy_dict(db, policy)
        _admin_audit(
            db,
            actor_user_id=context.user.id,
            action="INTERNAL_EGRESS_RULE_UPDATED",
            request_id=request.state.request_id,
            metadata={
                "rule_id": rule.id,
                "destination_cidr": rule.destination_cidr,
                "port": rule.port,
                "version": rule.row_version,
                "desired_revision": policy.desired_revision,
            },
        )
        record_mutation(
            db,
            actor_user_id=context.user.id,
            idempotency_key=key,
            request=mutation,
            response=response,
        )
        db.commit()
        return response

    @app.delete("/api/v1/admin/internal-egress-policy/rules/{rule_id}")
    def delete_admin_internal_egress_rule(
        rule_id: str,
        expected_revision: int,
        expected_version: int,
        request: Request,
        context: Annotated[SessionContext, Depends(admin_mutating_context)],
        db: Annotated[Session, Depends(get_db)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, object]:
        key = _idempotency_key(idempotency_key)
        mutation = mutation_request(
            action="INTERNAL_EGRESS_RULE_DELETE",
            target_key=f"internal-egress-rule:{rule_id}",
            payload={
                "expected_revision": expected_revision,
                "expected_version": expected_version,
            },
            fingerprint_key=settings.internal_hmac_key,
        )
        begin_immediate(db)
        replay = replay_mutation(
            db,
            actor_user_id=context.user.id,
            idempotency_key=key,
            request=mutation,
        )
        if replay is not None:
            db.commit()
            return replay
        policy, deleted = delete_internal_egress_rule(
            db,
            rule_id=rule_id,
            actor_user_id=context.user.id,
            expected_revision=expected_revision,
            expected_version=expected_version,
        )
        response = internal_egress_policy_dict(db, policy)
        _admin_audit(
            db,
            actor_user_id=context.user.id,
            action="INTERNAL_EGRESS_RULE_DELETED",
            request_id=request.state.request_id,
            metadata={**deleted, "desired_revision": policy.desired_revision},
        )
        record_mutation(
            db,
            actor_user_id=context.user.id,
            idempotency_key=key,
            request=mutation,
            response=response,
        )
        db.commit()
        return response

    @app.post("/api/v1/admin/internal-egress-policy/retry")
    def post_admin_internal_egress_policy_retry(
        payload: InternalEgressPolicyRetry,
        request: Request,
        context: Annotated[SessionContext, Depends(admin_mutating_context)],
        db: Annotated[Session, Depends(get_db)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, object]:
        key = _idempotency_key(idempotency_key)
        mutation = mutation_request(
            action="INTERNAL_EGRESS_POLICY_RETRY",
            target_key="internal-egress-policy:singleton",
            payload=payload.model_dump(),
            fingerprint_key=settings.internal_hmac_key,
        )
        begin_immediate(db)
        replay = replay_mutation(
            db,
            actor_user_id=context.user.id,
            idempotency_key=key,
            request=mutation,
        )
        if replay is not None:
            db.commit()
            return replay
        policy = retry_internal_egress_policy(
            db,
            actor_user_id=context.user.id,
            expected_revision=payload.expected_revision,
        )
        response = internal_egress_policy_dict(db, policy)
        _admin_audit(
            db,
            actor_user_id=context.user.id,
            action="INTERNAL_EGRESS_POLICY_RETRIED",
            request_id=request.state.request_id,
            metadata={
                "desired_revision": policy.desired_revision,
                "desired_digest": policy.desired_digest,
            },
        )
        record_mutation(
            db,
            actor_user_id=context.user.id,
            idempotency_key=key,
            request=mutation,
            response=response,
        )
        db.commit()
        return response

    @app.get("/api/v1/admin/profiles")
    def get_admin_profiles(
        _context: Annotated[SessionContext, Depends(admin_context)],
        db: Annotated[Session, Depends(get_db)],
    ) -> dict[str, object]:
        policy = get_resource_policy(db, settings)
        items, templates = admin_profile_catalog(db, resource_policy=policy)
        return {"items": items, "runtime_templates": templates}

    @app.post("/api/v1/admin/profiles", status_code=201)
    def post_admin_profile(
        payload: ProfileOfferCreate,
        request: Request,
        context: Annotated[SessionContext, Depends(admin_mutating_context)],
        db: Annotated[Session, Depends(get_db)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, object]:
        key = _idempotency_key(idempotency_key)
        mutation = mutation_request(
            action="PROFILE_OFFER_CREATE",
            target_key="profile-offer:new",
            payload=payload.model_dump(),
            fingerprint_key=settings.internal_hmac_key,
        )
        begin_immediate(db)
        replay = replay_mutation(
            db,
            actor_user_id=context.user.id,
            idempotency_key=key,
            request=mutation,
        )
        if replay is not None:
            db.commit()
            return replay
        offer, runtime = create_offer(
            db,
            actor_user_id=context.user.id,
            name=payload.name,
            description=payload.description,
            runtime_profile_id=payload.runtime_profile_id,
            runtime_profile_version=payload.runtime_profile_version,
            enabled=payload.enabled,
        )
        policy = get_resource_policy(db, settings)
        response = {"profile": offer_dict(offer, runtime, resource_policy=policy)}
        _admin_audit(
            db,
            actor_user_id=context.user.id,
            action="PROFILE_OFFER_CREATED",
            request_id=request.state.request_id,
            metadata={
                "offer_id": offer.id,
                "runtime_profile_id": runtime.id,
                "runtime_profile_version": runtime.version,
            },
        )
        record_mutation(
            db,
            actor_user_id=context.user.id,
            idempotency_key=key,
            request=mutation,
            response=response,
        )
        db.commit()
        return response

    @app.patch("/api/v1/admin/profiles/{offer_id}")
    def patch_admin_profile(
        offer_id: str,
        payload: ProfileOfferUpdate,
        request: Request,
        context: Annotated[SessionContext, Depends(admin_mutating_context)],
        db: Annotated[Session, Depends(get_db)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, object]:
        key = _idempotency_key(idempotency_key)
        mutation = mutation_request(
            action="PROFILE_OFFER_UPDATE",
            target_key=f"profile-offer:{offer_id}",
            payload=payload.model_dump(),
            fingerprint_key=settings.internal_hmac_key,
        )
        begin_immediate(db)
        replay = replay_mutation(
            db,
            actor_user_id=context.user.id,
            idempotency_key=key,
            request=mutation,
        )
        if replay is not None:
            db.commit()
            return replay
        offer, runtime = update_offer(
            db,
            offer_id=offer_id,
            expected_version=payload.version,
            name=payload.name,
            description=payload.description,
            enabled=payload.enabled,
        )
        policy = get_resource_policy(db, settings)
        response = {"profile": offer_dict(offer, runtime, resource_policy=policy)}
        _admin_audit(
            db,
            actor_user_id=context.user.id,
            action="PROFILE_OFFER_UPDATED",
            request_id=request.state.request_id,
            metadata={"offer_id": offer.id, "version": offer.row_version},
        )
        record_mutation(
            db,
            actor_user_id=context.user.id,
            idempotency_key=key,
            request=mutation,
            response=response,
        )
        db.commit()
        return response

    @app.delete("/api/v1/admin/profiles/{offer_id}")
    def delete_admin_profile(
        offer_id: str,
        expected_version: int,
        request: Request,
        context: Annotated[SessionContext, Depends(admin_mutating_context)],
        db: Annotated[Session, Depends(get_db)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, object]:
        key = _idempotency_key(idempotency_key)
        mutation = mutation_request(
            action="PROFILE_OFFER_DISABLE",
            target_key=f"profile-offer:{offer_id}",
            payload={"expected_version": expected_version},
            fingerprint_key=settings.internal_hmac_key,
        )
        begin_immediate(db)
        replay = replay_mutation(
            db,
            actor_user_id=context.user.id,
            idempotency_key=key,
            request=mutation,
        )
        if replay is not None:
            db.commit()
            return replay
        offer, runtime = disable_offer(
            db, offer_id=offer_id, expected_version=expected_version
        )
        policy = get_resource_policy(db, settings)
        response = {"profile": offer_dict(offer, runtime, resource_policy=policy)}
        _admin_audit(
            db,
            actor_user_id=context.user.id,
            action="PROFILE_OFFER_DISABLED",
            request_id=request.state.request_id,
            metadata={"offer_id": offer.id, "version": offer.row_version},
        )
        record_mutation(
            db,
            actor_user_id=context.user.id,
            idempotency_key=key,
            request=mutation,
            response=response,
        )
        db.commit()
        return response

    @app.get("/api/v1/admin/operations")
    def admin_operations(
        _context: Annotated[SessionContext, Depends(admin_context)],
        db: Annotated[Session, Depends(get_db)],
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, object]:
        rows = db.scalars(
            select(Operation)
            .order_by(Operation.requested_at.desc(), Operation.id.desc())
            .offset(offset)
            .limit(limit)
        ).all()
        return {
            "items": [operation_dict(row) for row in rows],
            "limit": limit,
            "offset": offset,
        }

    @app.get("/api/v1/admin/audit-events")
    def admin_audit_events(
        _context: Annotated[SessionContext, Depends(admin_context)],
        db: Annotated[Session, Depends(get_db)],
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, object]:
        rows = db.scalars(
            select(AuditEvent)
            .order_by(AuditEvent.created_at.desc())
            .offset(offset)
            .limit(limit)
        ).all()
        return {
            "items": [audit_dict(row) for row in rows],
            "limit": limit,
            "offset": offset,
        }

    @app.get("/api/v1/admin/capacity")
    def admin_capacity(
        _context: Annotated[SessionContext, Depends(admin_context)],
        db: Annotated[Session, Depends(get_db)],
    ) -> dict[str, object]:
        total_users = int(db.scalar(select(func.count(User.id))) or 0)
        total_workspaces = int(
            db.scalar(
                select(func.count(Workspace.id)).where(Workspace.archived_at.is_(None))
            )
            or 0
        )
        usage_snapshot_at = datetime.utcnow()
        freshness_cutoff = usage_snapshot_at - timedelta(
            seconds=settings.reconciliation_freshness_seconds
        )
        running_condition = (
            Workspace.archived_at.is_(None)
            & (Workspace.observed_state == ObservedState.RUNNING.value)
            & Workspace.stale.is_(False)
            & Workspace.deletion_started_at.is_(None)
            & Workspace.last_reconciled_at.is_not(None)
            & (Workspace.last_reconciled_at >= freshness_cutoff)
        )
        measured_condition = (
            running_condition
            & Workspace.resource_usage_observed_at.is_not(None)
            & (Workspace.resource_usage_observed_at >= freshness_cutoff)
            & Workspace.cpu_usage_millicores.is_not(None)
            & Workspace.memory_usage_bytes.is_not(None)
            & Workspace.memory_limit_bytes.is_not(None)
        )
        # Keep coverage and sums in one SQLite statement. Separate SELECTs in
        # sqlite3's legacy transaction mode do not guarantee one read snapshot,
        # allowing a reconciler commit between them to produce impossible
        # measured/running counts.
        usage_row = db.execute(
            select(
                func.coalesce(
                    func.sum(case((running_condition, 1), else_=0)), 0
                ),
                func.coalesce(
                    func.sum(case((measured_condition, 1), else_=0)), 0
                ),
                func.coalesce(
                    func.sum(
                        case(
                            (measured_condition, Workspace.cpu_usage_millicores),
                            else_=0,
                        )
                    ),
                    0,
                ),
                func.coalesce(
                    func.sum(
                        case(
                            (measured_condition, Workspace.memory_usage_bytes),
                            else_=0,
                        )
                    ),
                    0,
                ),
                func.min(
                    case(
                        (running_condition, Workspace.last_reconciled_at),
                        else_=None,
                    )
                ),
                func.min(
                    case(
                        (
                            measured_condition,
                            Workspace.resource_usage_observed_at,
                        ),
                        else_=None,
                    )
                ),
            ).select_from(Workspace)
        ).one()
        running_workspaces = int(usage_row[0])
        measured_usage = int(usage_row[1])
        usage_freshness_sources = [usage_snapshot_at]
        if usage_row[4] is not None:
            usage_freshness_sources.append(usage_row[4])
        if usage_row[5] is not None:
            usage_freshness_sources.append(usage_row[5])
        usage_expires_at = min(usage_freshness_sources) + timedelta(
            seconds=settings.reconciliation_freshness_seconds
        )
        reservations = active_reservations(db)
        # This administrative inventory remains available specifically so the
        # UI can lower a drifted persisted policy. User admission paths keep the
        # default strict hard-ceiling validation.
        policy = get_resource_policy(db, settings, enforce_hard_ceiling=False)
        return {
            "users": total_users,
            "workspaces": {
                "created": total_workspaces,
                "running": running_workspaces,
                "reserved": reservations.count,
                "limit": settings.max_active_workspaces,
            },
            "resources": {
                "cpu_millicores": {
                    "reserved": reservations.cpu_millicores,
                    "limit": policy.cpu_budget_millicores,
                },
                "memory_mb": {
                    "reserved": reservations.memory_mb,
                    "limit": policy.memory_budget_mb,
                },
                "gpu_count": {
                    "reserved": reservations.gpu_count,
                    "limit": policy.gpu_budget_count,
                },
            },
            "usage": {
                "running_total": running_workspaces,
                "measured": measured_usage,
                "unavailable": running_workspaces - measured_usage,
                "cpu_millicores": int(usage_row[2]),
                "memory_bytes": int(usage_row[3]),
                "expires_at": iso(usage_expires_at),
                "stale": False,
            },
        }

    async def _verified_internal_body(request: Request) -> bytes:
        body = await request.body()
        if len(body) > 64 * 1024:
            raise AppError(
                413,
                "INTERNAL_REQUEST_TOO_LARGE",
                "Internal request body is too large",
            )
        try:
            timestamp = int(request.headers.get("X-Platform-Timestamp", ""))
        except ValueError as exc:
            raise AppError(
                401,
                "INTERNAL_SIGNATURE_INVALID",
                "Internal request signature is invalid",
            ) from exc
        signed = SignedInternalRequest(
            version=request.headers.get("X-Platform-HMAC-Version", ""),
            timestamp=timestamp,
            nonce=request.headers.get("X-Platform-Nonce", ""),
            content_sha256=request.headers.get("X-Platform-Content-SHA256", ""),
            signature=request.headers.get("X-Platform-Signature", ""),
        )
        now = datetime.utcnow()
        if not verify_internal_signature(
            settings.internal_hmac_key,
            signed,
            method=request.method,
            path=request.url.path,
            body=body,
            now=now,
        ):
            raise AppError(
                401,
                "INTERNAL_SIGNATURE_INVALID",
                "Internal request signature is invalid",
            )
        _record_internal_nonce(factory, signed.nonce, now)
        return body

    async def _internal_spawn(request: Request, *, consume: bool) -> dict[str, object]:
        body = await _verified_internal_body(request)
        try:
            payload = (
                SpawnConsumeRequest.model_validate_json(body)
                if consume
                else SpawnCheckRequest.model_validate_json(body)
            )
        except Exception as exc:
            raise AppError(
                422, "REQUEST_VALIDATION_FAILED", "Spawn validation request is invalid"
            ) from exc
        with factory() as db:
            if consume:
                assert isinstance(payload, SpawnConsumeRequest)
                approval = consume_spawn_authorization(
                    db,
                    payload,
                    cipher=cipher,
                    environment_hmac_key=settings.internal_hmac_key,
                )
                return {
                    "schema_version": 2,
                    "authorized": True,
                    "authorization": approval.authorization.model_dump(),
                }
            assert isinstance(payload, SpawnCheckRequest)
            approval = check_spawn_authorization(db, payload)
            return {
                "schema_version": 2,
                "authorized": True,
                "spawn_authorization_id": approval.authorization.spawn_authorization_id,
            }

    @app.post("/internal/v1/spawn-authorizations/consume", include_in_schema=False)
    async def consume_spawn(request: Request) -> dict[str, object]:
        return await _internal_spawn(request, consume=True)

    @app.post("/internal/v1/spawn-authorizations/check", include_in_schema=False)
    async def check_spawn(request: Request) -> dict[str, object]:
        return await _internal_spawn(request, consume=False)

    @app.post(
        "/internal/v1/user-provisioning/claim",
        include_in_schema=False,
        response_model=None,
    )
    async def claim_user_provisioning(request: Request) -> Response | dict[str, object]:
        body = await _verified_internal_body(request)
        try:
            payload = UserProvisioningClaimRequest.model_validate_json(body)
        except Exception as exc:
            raise AppError(
                422,
                "REQUEST_VALIDATION_FAILED",
                "Provisioning claim request is invalid",
            ) from exc
        with factory() as db:
            claim = claim_provisioning_job(
                db,
                settings=settings,
                worker_id=payload.worker_id,
                request_id=request.state.request_id,
            )
        if claim is None:
            return Response(status_code=204)
        return {
            "schema_version": 1,
            "user_id": claim.user_id,
            "username": claim.username,
            "attempt_no": claim.attempt_no,
            "lease_expires_at": f"{claim.lease_expires_at.isoformat()}Z",
        }

    @app.post(
        "/internal/v1/user-provisioning/complete",
        status_code=204,
        include_in_schema=False,
    )
    async def complete_user_provisioning(request: Request) -> Response:
        body = await _verified_internal_body(request)
        try:
            payload = UserProvisioningCompleteRequest.model_validate_json(body)
        except Exception as exc:
            raise AppError(
                422,
                "REQUEST_VALIDATION_FAILED",
                "Provisioning completion request is invalid",
            ) from exc
        with factory() as db:
            complete_provisioning_job(
                db,
                settings=settings,
                worker_id=payload.worker_id,
                user_id=payload.user_id,
                attempt_no=payload.attempt_no,
                manifest=payload.manifest.model_dump(),
                request_id=request.state.request_id,
            )
        return Response(status_code=204)

    @app.post(
        "/internal/v1/user-provisioning/fail",
        status_code=204,
        include_in_schema=False,
    )
    async def fail_user_provisioning(request: Request) -> Response:
        body = await _verified_internal_body(request)
        try:
            payload = UserProvisioningFailRequest.model_validate_json(body)
        except Exception as exc:
            raise AppError(
                422,
                "REQUEST_VALIDATION_FAILED",
                "Provisioning failure request is invalid",
            ) from exc
        with factory() as db:
            fail_provisioning_job(
                db,
                settings=settings,
                worker_id=payload.worker_id,
                user_id=payload.user_id,
                attempt_no=payload.attempt_no,
                request_id=request.state.request_id,
            )
        return Response(status_code=204)

    @app.post(
        "/internal/v1/workspace-deletions/claim",
        include_in_schema=False,
        response_model=None,
    )
    async def claim_workspace_deletion(
        request: Request,
    ) -> Response | dict[str, object]:
        body = await _verified_internal_body(request)
        try:
            payload = WorkspaceDeletionClaimRequest.model_validate_json(body)
        except Exception as exc:
            raise AppError(
                422,
                "REQUEST_VALIDATION_FAILED",
                "Workspace deletion claim request is invalid",
            ) from exc
        with factory() as db:
            claim = claim_deletion_job(
                db, settings=settings, worker_id=payload.worker_id
            )
        if claim is None:
            return Response(status_code=204)
        return {
            "schema_version": 1,
            "deletion_id": claim.deletion_id,
            "workspace_id": claim.workspace_id,
            "operation_id": claim.operation_id,
            "owner_user_id": claim.owner_user_id,
            "username": claim.username,
            "server_name": claim.server_name,
            "workspace_spec_version": claim.workspace_spec_version,
            "private_volume_slot_id": claim.private_volume_slot_id,
            "private_volume_slot_number": claim.private_volume_slot_number,
            "private_volume_name": claim.private_volume_name,
            "attempt_no": claim.attempt_no,
        }

    @app.post(
        "/internal/v1/workspace-deletions/complete",
        status_code=204,
        include_in_schema=False,
    )
    async def complete_workspace_deletion(request: Request) -> Response:
        body = await _verified_internal_body(request)
        try:
            payload = WorkspaceDeletionCompleteRequest.model_validate_json(body)
        except Exception as exc:
            raise AppError(
                422,
                "REQUEST_VALIDATION_FAILED",
                "Workspace deletion completion request is invalid",
            ) from exc
        with factory() as db:
            complete_deletion_job(
                db,
                settings=settings,
                worker_id=payload.worker_id,
                workspace_id=payload.workspace_id,
                attempt_no=payload.attempt_no,
                manifest=payload.manifest.model_dump(),
                request_id=request.state.request_id,
            )
        return Response(status_code=204)

    @app.post(
        "/internal/v1/workspace-deletions/fail",
        status_code=204,
        include_in_schema=False,
    )
    async def fail_workspace_deletion(request: Request) -> Response:
        body = await _verified_internal_body(request)
        try:
            payload = WorkspaceDeletionFailRequest.model_validate_json(body)
        except Exception as exc:
            raise AppError(
                422,
                "REQUEST_VALIDATION_FAILED",
                "Workspace deletion failure request is invalid",
            ) from exc
        with factory() as db:
            fail_deletion_job(
                db,
                settings=settings,
                worker_id=payload.worker_id,
                workspace_id=payload.workspace_id,
                attempt_no=payload.attempt_no,
                request_id=request.state.request_id,
            )
        return Response(status_code=204)

    return app


def _idempotency_key(value: str | None) -> str:
    if value is None or not value.strip() or len(value) > 128:
        raise AppError(
            400,
            "IDEMPOTENCY_KEY_REQUIRED",
            "A valid Idempotency-Key header is required",
        )
    return value.strip()


def _record_internal_nonce(
    factory: sessionmaker[Session], nonce: str, now: datetime
) -> None:
    with factory() as db:
        try:
            db.execute(
                delete(InternalRequestNonce).where(
                    InternalRequestNonce.expires_at <= now
                )
            )
            db.add(
                InternalRequestNonce(
                    nonce=nonce,
                    seen_at=now,
                    expires_at=now + timedelta(minutes=2),
                )
            )
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise AppError(
                401,
                "INTERNAL_REPLAY_REJECTED",
                "Internal request nonce was already used",
            ) from exc
