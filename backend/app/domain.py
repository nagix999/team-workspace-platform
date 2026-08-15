from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class UserRole(StrEnum):
    USER = "USER"
    ADMIN = "ADMIN"


class UserStatus(StrEnum):
    PROVISIONING = "PROVISIONING"
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class ProvisionStatus(StrEnum):
    PROVISIONED = "PROVISIONED"
    WIPING = "WIPING"
    ERROR = "ERROR"


class UserProvisioningStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class DesiredState(StrEnum):
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    DELETED = "DELETED"


class ObservedState(StrEnum):
    UNKNOWN = "UNKNOWN"
    NOT_FOUND = "NOT_FOUND"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class OperationType(StrEnum):
    CREATE = "CREATE"
    START = "START"
    STOP = "STOP"
    RESTART = "RESTART"
    DELETE = "DELETE"


class OperationStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    WAITING_EXTERNAL = "WAITING_EXTERNAL"
    CANCELLED = "CANCELLED"

    @property
    def terminal(self) -> bool:
        return self in {
            self.SUCCEEDED,
            self.FAILED,
            self.AUTH_REQUIRED,
            self.CANCELLED,
        }


class HubServerState(StrEnum):
    NOT_FOUND = "NOT_FOUND"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"
