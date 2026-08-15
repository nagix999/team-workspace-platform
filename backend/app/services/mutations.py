from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import AppError
from ..models import MutationReceipt
from ..security import keyed_hash


@dataclass(frozen=True)
class MutationRequest:
    action: str
    target_key: str
    fingerprint: str


def mutation_request(
    *, action: str, target_key: str, payload: object, fingerprint_key: str
) -> MutationRequest:
    canonical = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    fingerprint = keyed_hash(
        f"platform-api-mutation-v1\0{action}\0{target_key}\0{canonical}",
        fingerprint_key,
    )
    return MutationRequest(action, target_key, fingerprint)


def replay_mutation(
    db: Session,
    *,
    actor_user_id: str,
    idempotency_key: str,
    request: MutationRequest,
) -> dict[str, object] | None:
    receipt = db.scalar(
        select(MutationReceipt).where(
            MutationReceipt.actor_user_id == actor_user_id,
            MutationReceipt.idempotency_key == idempotency_key,
        )
    )
    if receipt is None:
        return None
    if (
        receipt.action != request.action
        or receipt.target_key != request.target_key
        or receipt.request_fingerprint != request.fingerprint
    ):
        raise AppError(
            409,
            "IDEMPOTENCY_KEY_REUSED",
            "Idempotency-Key was already used for a different request",
        )
    value = json.loads(receipt.response_json)
    if not isinstance(value, dict):  # pragma: no cover - database corruption
        raise AppError(500, "MUTATION_RECEIPT_INVALID", "Mutation receipt is invalid")
    return value


def record_mutation(
    db: Session,
    *,
    actor_user_id: str,
    idempotency_key: str,
    request: MutationRequest,
    response: dict[str, object],
) -> None:
    db.add(
        MutationReceipt(
            id=str(uuid.uuid4()),
            actor_user_id=actor_user_id,
            idempotency_key=idempotency_key,
            action=request.action,
            target_key=request.target_key,
            request_fingerprint=request.fingerprint,
            response_json=json.dumps(
                response, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            ),
        )
    )
