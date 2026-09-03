from __future__ import annotations

from typing import Literal

import unicodedata

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .accelerators import NVIDIA_GPU_DEVICE_ID_RE
from .policy_values import validate_kernel_idle_timeout
from .services.internal_egress import (
    normalize_destination_cidr,
    validate_internal_service_port,
)


def _normalized_text(value: str, *, allow_blank: bool, maximum: int) -> str | None:
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized and allow_blank:
        return None
    if not normalized or len(normalized) > maximum:
        raise ValueError("text length is outside the allowed range")
    if any(unicodedata.category(char).startswith("C") for char in normalized):
        raise ValueError("control and format characters are not allowed")
    return normalized


class WorkspaceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9-]*$")
    profile_version: int = Field(strict=True, gt=0)
    name: str | None = Field(default=None, max_length=80)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        return (
            None
            if value is None
            else _normalized_text(value, allow_blank=True, maximum=80)
        )


class EnvironmentVariablePut(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    value: str = Field(max_length=16_384)
    is_secret: bool
    expected_version: int | None = Field(default=None, gt=0)


class ResourcePolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: int = Field(gt=0)
    cpu_budget_millicores: int = Field(gt=0)
    memory_budget_mb: int = Field(gt=0)
    selectable_cpu_millicores: list[int] = Field(min_length=1, max_length=32)
    selectable_memory_mb: list[int] = Field(min_length=1, max_length=32)
    # Optional additions preserve the current values for older v1 clients;
    # responses always contain the resolved policy values.
    gpu_budget_count: int | None = Field(default=None, ge=0, le=1)
    selectable_gpu_counts: list[int] | None = Field(
        default=None, min_length=1, max_length=2
    )
    kernel_idle_timeout_seconds: int | None = Field(default=None, ge=0)

    @field_validator("kernel_idle_timeout_seconds")
    @classmethod
    def kernel_idle_timeout_is_supported(cls, value: int | None) -> int | None:
        return None if value is None else validate_kernel_idle_timeout(value)


class InternalEgressRuleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_revision: int = Field(gt=0)
    destination_cidr: str = Field(min_length=1, max_length=18)
    port: int = Field(ge=1024, le=65535)

    @field_validator("destination_cidr")
    @classmethod
    def destination_is_exact_private_host(cls, value: str) -> str:
        return normalize_destination_cidr(value)

    @field_validator("port")
    @classmethod
    def port_is_not_control_plane(cls, value: int) -> int:
        return validate_internal_service_port(value)


class InternalEgressRuleUpdate(InternalEgressRuleCreate):
    expected_version: int = Field(gt=0)


class InternalEgressPolicyRetry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_revision: int = Field(gt=0)


class ProfileOfferCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=512)
    runtime_profile_id: str = Field(min_length=1, max_length=64)
    runtime_profile_version: int = Field(gt=0)
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        result = _normalized_text(value, allow_blank=False, maximum=80)
        assert result is not None
        return result

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str | None) -> str | None:
        return (
            None
            if value is None
            else _normalized_text(value, allow_blank=True, maximum=512)
        )


class ProfileOfferUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=512)
    enabled: bool

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        result = _normalized_text(value, allow_blank=False, maximum=80)
        assert result is not None
        return result

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str | None) -> str | None:
        return (
            None
            if value is None
            else _normalized_text(value, allow_blank=True, maximum=512)
        )


class UserProvisioningClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1]
    worker_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )

    @field_validator("schema_version", mode="before")
    @classmethod
    def schema_version_is_exact_integer(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value


class UserProvisioningSlotManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    slot_id: str = Field(
        min_length=36,
        max_length=36,
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    )
    slot_number: int = Field(ge=1, le=5)
    volume_name: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9_.-]*$",
    )
    hard_limit_bytes: int = Field(gt=0)
    project_id: int = Field(gt=0, le=2**31 - 1)


class UserProvisioningManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1]
    unsafe_local_dev: Literal[True]
    user_id: str = Field(
        min_length=36,
        max_length=36,
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    )
    username: str = Field(
        min_length=1,
        max_length=32,
        pattern=r"^[a-z](?:[a-z0-9-]{0,30}[a-z0-9])?$",
    )
    uid: int = Field(gt=0)
    gid: int = Field(gt=0)
    slots: list[UserProvisioningSlotManifest] = Field(min_length=5, max_length=5)

    @field_validator("schema_version", mode="before")
    @classmethod
    def schema_version_is_exact_integer(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value

    @field_validator("unsafe_local_dev", mode="before")
    @classmethod
    def unsafe_marker_is_exact_boolean(cls, value: object) -> object:
        if type(value) is not bool or value is not True:
            raise ValueError("unsafe_local_dev must be the boolean true")
        return value

    @field_validator("username")
    @classmethod
    def username_has_no_double_hyphen(cls, value: str) -> str:
        if "--" in value:
            raise ValueError("username must not contain consecutive hyphens")
        return value


class ProductionUserProvisioningSlotManifest(UserProvisioningSlotManifest):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str = Field(
        min_length=1,
        max_length=4096,
        pattern=r"^/(?:[^/\x00]+/)*[^/\x00]+$",
    )


class ProductionUserProvisioningManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1]
    inventory_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    user_id: str = Field(
        min_length=36,
        max_length=36,
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    )
    username: str = Field(
        min_length=1,
        max_length=32,
        pattern=r"^[a-z](?:[a-z0-9-]{0,30}[a-z0-9])?$",
    )
    uid: int = Field(gt=0)
    gid: int = Field(gt=0)
    slots: list[ProductionUserProvisioningSlotManifest] = Field(
        min_length=5, max_length=5
    )

    @field_validator("schema_version", mode="before")
    @classmethod
    def schema_version_is_exact_integer(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value

    @field_validator("username")
    @classmethod
    def username_has_no_double_hyphen(cls, value: str) -> str:
        if "--" in value:
            raise ValueError("username must not contain consecutive hyphens")
        return value


class UserProvisioningCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1]
    worker_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )
    user_id: str = Field(
        min_length=36,
        max_length=36,
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    )
    attempt_no: int = Field(gt=0)
    manifest: UserProvisioningManifest | ProductionUserProvisioningManifest

    @field_validator("schema_version", mode="before")
    @classmethod
    def schema_version_is_exact_integer(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value


class UserProvisioningFailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1]
    worker_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )
    user_id: str = Field(
        min_length=36,
        max_length=36,
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    )
    attempt_no: int = Field(gt=0)

    @field_validator("schema_version", mode="before")
    @classmethod
    def schema_version_is_exact_integer(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value


class WorkspaceDeletionClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1]
    worker_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )


class WorkspaceDeletionManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1]
    workspace_id: str = Field(min_length=36, max_length=36)
    owner_user_id: str = Field(min_length=36, max_length=36)
    username: str = Field(min_length=1, max_length=64)
    workspace_spec_version: int = Field(gt=0)
    private_volume_slot_id: str = Field(min_length=36, max_length=36)
    private_volume_slot_number: int = Field(ge=1, le=5)
    private_volume_name: str = Field(min_length=1, max_length=128)
    hard_limit_bytes: int = Field(gt=0)
    project_id: int = Field(gt=0)
    uid: int = Field(gt=0)
    gid: int = Field(gt=0)
    mode: Literal["0700"]
    volume_recreated: Literal[True]


class WorkspaceDeletionCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1]
    worker_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )
    workspace_id: str = Field(min_length=36, max_length=36)
    attempt_no: int = Field(gt=0)
    manifest: WorkspaceDeletionManifest


class WorkspaceDeletionFailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1]
    worker_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )
    workspace_id: str = Field(min_length=36, max_length=36)
    attempt_no: int = Field(gt=0)


class SpawnConsumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(ge=1, le=1)
    spawn_ticket: str = Field(min_length=32, max_length=256)
    username: str = Field(min_length=1, max_length=64)
    server_name: str = Field(min_length=1, max_length=64)
    profile_id: str = Field(min_length=1, max_length=64)
    profile_version: int = Field(ge=1)


class SpawnAuthorizationBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spawn_authorization_id: str = Field(min_length=8, max_length=128)
    workspace_id: str = Field(min_length=8, max_length=128)
    operation_id: str = Field(min_length=8, max_length=128)
    attempt_no: int = Field(gt=0)
    workspace_spec_version: int = Field(gt=0)
    username: str = Field(min_length=1, max_length=64)
    server_name: str = Field(min_length=1, max_length=64)
    profile_id: str = Field(min_length=1, max_length=64)
    profile_version: int = Field(gt=0)
    profile_config_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    runtime_base_profile_id: str = Field(min_length=1, max_length=64)
    runtime_base_profile_version: int = Field(gt=0)
    runtime_base_profile_config_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    cpu_limit_millicores: int = Field(gt=0)
    memory_limit_bytes: int = Field(gt=0)
    private_volume_slot_id: str = Field(min_length=8, max_length=128)
    private_volume_slot_number: int = Field(ge=1, le=5)
    private_volume_name: str = Field(min_length=1, max_length=128)
    private_disk_hard_limit_bytes: int = Field(gt=0)
    uid: int = Field(gt=0)
    gid: int = Field(gt=0)
    environment_digest: str = Field(pattern=r"^hmac-sha256:[0-9a-f]{64}$")
    user_environment_generation: int = Field(gt=0)
    workspace_environment_generation: int = Field(gt=0)
    kernel_idle_timeout_seconds: int = Field(strict=True, ge=0)
    gpu_count: int = Field(strict=True, ge=0, le=1)
    gpu_device_id: str | None = Field(pattern=NVIDIA_GPU_DEVICE_ID_RE.pattern)
    gpu_inventory_digest: str | None = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    valid_until_unix: int = Field(gt=0)

    @field_validator("kernel_idle_timeout_seconds")
    @classmethod
    def kernel_idle_timeout_is_supported(cls, value: int) -> int:
        return validate_kernel_idle_timeout(value)

    @model_validator(mode="after")
    def gpu_assignment_is_exact(self) -> "SpawnAuthorizationBinding":
        if self.gpu_count == 0:
            if self.gpu_device_id is not None or self.gpu_inventory_digest is not None:
                raise ValueError("CPU authorization cannot carry a GPU assignment")
        elif self.gpu_device_id is None or self.gpu_inventory_digest is None:
            raise ValueError("GPU authorization requires an exact inventory binding")
        return self


class SpawnAuthorizationPayload(SpawnAuthorizationBinding):
    environment: dict[str, str] = Field(max_length=128)


class SpawnCheckRequest(SpawnAuthorizationBinding):
    schema_version: int = Field(ge=1, le=1)


class ErrorEnvelope(BaseModel):
    code: str
    message: str
    request_id: str
