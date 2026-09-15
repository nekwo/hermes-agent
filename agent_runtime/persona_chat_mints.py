from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Iterator

from hermes_time import now
from utils import atomic_json_write

from . import paths
from .locks import (
    HarnessLockUnavailable,
    persona_chat_instance_lock,
    persona_chat_mint_lock,
)


_SCHEMA_VERSION = 1
_VALID_STATES = frozenset({"reserved", "bound"})


class PersonaChatMintError(RuntimeError):
    """A typed, fail-closed chat-root mint receipt failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PersonaChatMintReceipt:
    key_digest: str
    persona_id: str
    persona_instance_id: str
    session_id: str
    state: str
    created_at: str
    updated_at: str
    idempotent_replay: bool = False

    @property
    def bound(self) -> bool:
        return self.state == "bound"


class PersonaChatMintTransaction:
    """One receipt reservation held under its cross-process lock."""

    def __init__(self, receipt: PersonaChatMintReceipt):
        self.receipt = receipt

    def mark_bound(self) -> PersonaChatMintReceipt:
        if self.receipt.bound:
            return self.receipt
        timestamp = _timestamp()
        self.receipt = replace(self.receipt, state="bound", updated_at=timestamp)
        _write_receipt(self.receipt)
        return self.receipt


@contextmanager
def reserve_persona_chat_mint(
    *,
    idempotency_key: str,
    persona_id: str,
    persona_instance_id: str,
    session_id: str,
) -> Iterator[PersonaChatMintTransaction]:
    """Reserve or replay one server-minted chat root.

    The ``reserved`` receipt is atomically durable before SessionDB creation or
    persona-instance binding begins. A process crash or transient persistence
    failure can therefore retry with the same key and recover the same root
    instead of creating a duplicate conversation.
    """
    key = _validated_key(idempotency_key)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    try:
        # Instance first, key second is the global lock order. The instance
        # lock makes distinct-key concurrent creates deterministic instead of
        # racing the selected-session pointer; the key lock deduplicates retries
        # across CLI and serve processes.
        with persona_chat_instance_lock(persona_instance_id):
            with persona_chat_mint_lock(digest):
                path = paths.persona_chat_mint_receipt_path(digest)
                if path.exists():
                    receipt = _read_receipt(path, digest=digest)
                    _validate_scope(
                        receipt,
                        persona_id=persona_id,
                        persona_instance_id=persona_instance_id,
                    )
                    receipt = replace(receipt, idempotent_replay=True)
                else:
                    timestamp = _timestamp()
                    receipt = PersonaChatMintReceipt(
                        key_digest=digest,
                        persona_id=persona_id,
                        persona_instance_id=persona_instance_id,
                        session_id=session_id,
                        state="reserved",
                        created_at=timestamp,
                        updated_at=timestamp,
                    )
                    _write_receipt(receipt)
                yield PersonaChatMintTransaction(receipt)
    except HarnessLockUnavailable as exc:
        raise PersonaChatMintError(
            "mint_lock_unavailable",
            "another chat-root change is still active; retry with the same idempotency key",
        ) from exc


def _validated_key(value: str) -> str:
    key = str(value or "").strip()
    if not key:
        raise PersonaChatMintError(
            "idempotency_key_required",
            "idempotency_key is required when new_session is true",
        )
    if len(key) > 240:
        raise PersonaChatMintError(
            "idempotency_key_invalid",
            "idempotency_key must be 240 characters or fewer",
        )
    return key


def _read_receipt(path, *, digest: str) -> PersonaChatMintReceipt:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if int(raw.get("schema_version") or 0) != _SCHEMA_VERSION:
            raise ValueError("unsupported schema_version")
        state = str(raw.get("state") or "")
        if state not in _VALID_STATES:
            raise ValueError("invalid state")
        receipt = PersonaChatMintReceipt(
            key_digest=str(raw["idempotency_key_sha256"]),
            persona_id=str(raw["persona_id"]),
            persona_instance_id=str(raw["persona_instance_id"]),
            session_id=str(raw["session_id"]),
            state=state,
            created_at=str(raw["created_at"]),
            updated_at=str(raw["updated_at"]),
        )
        if not all(
            (
                receipt.key_digest,
                receipt.persona_id,
                receipt.persona_instance_id,
                receipt.session_id,
                receipt.created_at,
                receipt.updated_at,
            )
        ):
            raise ValueError("required field is blank")
        if receipt.key_digest != digest:
            raise ValueError("key digest does not match receipt path")
        return receipt
    except PersonaChatMintError:
        raise
    except Exception as exc:
        raise PersonaChatMintError(
            "mint_receipt_corrupt",
            f"chat mint receipt is unreadable: {exc}",
        ) from exc


def _validate_scope(
    receipt: PersonaChatMintReceipt,
    *,
    persona_id: str,
    persona_instance_id: str,
) -> None:
    if (
        receipt.persona_id == persona_id
        and receipt.persona_instance_id == persona_instance_id
    ):
        return
    raise PersonaChatMintError(
        "idempotency_conflict",
        "idempotency_key was already used for a different persona chat target",
    )


def _write_receipt(receipt: PersonaChatMintReceipt) -> None:
    atomic_json_write(
        paths.persona_chat_mint_receipt_path(receipt.key_digest),
        {
            "schema_version": _SCHEMA_VERSION,
            "idempotency_key_sha256": receipt.key_digest,
            "persona_id": receipt.persona_id,
            "persona_instance_id": receipt.persona_instance_id,
            "session_id": receipt.session_id,
            "state": receipt.state,
            "created_at": receipt.created_at,
            "updated_at": receipt.updated_at,
        },
        indent=2,
        sort_keys=True,
    )


def _timestamp() -> str:
    value = now()
    return value.isoformat().replace("+00:00", "Z")
