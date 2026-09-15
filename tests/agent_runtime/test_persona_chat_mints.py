from __future__ import annotations

import hashlib
import threading

from agent_runtime import paths
from agent_runtime.persona_chat_mints import reserve_persona_chat_mint


def test_persona_chat_mint_receipt_hashes_client_key(isolate_agent_runtime_root):
    raw_key = "launcher-new-chat-secret-shaped-retry-key"
    with reserve_persona_chat_mint(
        idempotency_key=raw_key,
        persona_id="dev",
        persona_instance_id="personainst_dev",
        session_id="persona_chat_personainst_dev_123456789abc",
    ) as mint:
        mint.mark_bound()

    receipts = list(paths.persona_chat_mint_receipts_dir().glob("*.json"))
    assert len(receipts) == 1
    persisted = receipts[0].read_text(encoding="utf-8")
    assert raw_key not in persisted
    assert hashlib.sha256(raw_key.encode("utf-8")).hexdigest() in persisted


def test_distinct_chat_mint_keys_serialize_one_instance(isolate_agent_runtime_root):
    first_acquired = threading.Event()
    release_first = threading.Event()
    second_acquired = threading.Event()
    failures: list[Exception] = []

    def reserve_first() -> None:
        try:
            with reserve_persona_chat_mint(
                idempotency_key="first-key",
                persona_id="dev",
                persona_instance_id="personainst_dev",
                session_id="persona_chat_personainst_dev_111111111111",
            ):
                first_acquired.set()
                if not release_first.wait(timeout=2):
                    raise AssertionError("test did not release the first mint")
        except Exception as exc:  # pragma: no cover - forwarded to main thread
            failures.append(exc)

    def reserve_second() -> None:
        try:
            with reserve_persona_chat_mint(
                idempotency_key="second-key",
                persona_id="dev",
                persona_instance_id="personainst_dev",
                session_id="persona_chat_personainst_dev_222222222222",
            ):
                second_acquired.set()
        except Exception as exc:  # pragma: no cover - forwarded to main thread
            failures.append(exc)

    first = threading.Thread(target=reserve_first)
    second = threading.Thread(target=reserve_second)
    first.start()
    assert first_acquired.wait(timeout=1)
    second.start()

    assert not second_acquired.wait(timeout=0.15)
    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert failures == []
    assert second_acquired.is_set()
