from __future__ import annotations

import threading

from agent_runtime import paths
from agent_runtime.queued_skills import (
    consume_skills_for_next_turn,
    pending_skills_for_next_turn,
    queue_skills_for_next_turn,
)


def test_queue_skills_batch_is_deduped_and_consumed_once(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "store_root", lambda: tmp_path)

    payload = queue_skills_for_next_turn(
        persona_id="dev",
        persona_instance_id="personainst_dev",
        session_id="persona_chat_dev",
        skills=["harness-dev-delivery", "harness-dev-delivery", "launcher-analyze-proof"],
    )

    assert payload["skills"] == ["harness-dev-delivery", "launcher-analyze-proof"]
    assert consume_skills_for_next_turn(
        persona_id="dev", session_id="persona_chat_dev"
    ) == payload["skills"]
    assert pending_skills_for_next_turn(
        persona_id="dev", session_id="persona_chat_dev"
    ) == []


def test_concurrent_batches_merge_without_lost_update(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "store_root", lambda: tmp_path)
    barrier = threading.Barrier(3)

    def queue(skill: str) -> None:
        barrier.wait()
        queue_skills_for_next_turn(
            persona_id="dev",
            session_id="persona_chat_dev",
            skills=[skill],
        )

    threads = [
        threading.Thread(target=queue, args=("harness-dev-delivery",)),
        threading.Thread(target=queue, args=("launcher-analyze-proof",)),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert set(
        pending_skills_for_next_turn(
            persona_id="dev", session_id="persona_chat_dev"
        )
    ) == {"harness-dev-delivery", "launcher-analyze-proof"}
