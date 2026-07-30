"""S35 — migrate retired assignment task bindings, then drop the spec field.

``0b67024af`` kept ``PersonaAssignmentSpec.task_id`` only because historical
mission-lane assignment rows could still carry a real value.  The 2026-07-30
follow-up ruling resolves that blocker explicitly: archive every live row whose
raw ``task_id`` is non-null, then make ``evidence_kind`` the sole authority for
free-floating discrimination and assignment archive scope.
"""

from __future__ import annotations

import dataclasses
import inspect
import json

from agent_runtime import paths, persona_assignments
from utils import atomic_json_write


def test_persona_assignment_spec_no_longer_declares_task_id():
    fields = {
        field.name
        for field in dataclasses.fields(persona_assignments.PersonaAssignmentSpec)
    }
    assert "task_id" not in fields


def test_create_or_resume_and_archive_scope_use_evidence_kind_not_spec_task_id():
    source = inspect.getsource(persona_assignments.PersonaAssignmentStore.create_or_resume)
    assert "spec.task_id" not in source

    signature = inspect.signature(persona_assignments.assignment_archive_scope)
    assert list(signature.parameters) == ["kind"]
    assert persona_assignments.assignment_archive_scope("free_floating_message") == "assignment"
    assert persona_assignments.assignment_archive_scope("diagnostic") == "task"


def test_retired_task_id_migration_is_dry_run_safe_archival_and_idempotent(
    isolate_agent_runtime_root,
):
    live = paths.persona_assignments_dir()
    legacy = live / "assign_legacy.json"
    current = live / "assign_current.json"
    missing = live / "assign_missing.json"
    atomic_json_write(legacy, {"id": "assign_legacy", "task_id": "task_old"})
    atomic_json_write(current, {"id": "assign_current", "task_id": None})
    atomic_json_write(missing, {"id": "assign_missing"})

    migrate = getattr(
        persona_assignments,
        "migrate_retired_persona_assignment_task_ids",
        None,
    )
    assert callable(migrate)

    preview = migrate(dry_run=True)
    assert preview["dry_run"] is True
    assert preview["scanned"] == 3
    assert preview["eligible"] == 1
    assert preview["archived"] == 0
    assert preview["assignment_ids"] == ["assign_legacy"]
    assert legacy.is_file()
    assert not paths.persona_assignments_archive_dir().exists()

    applied = migrate(dry_run=False)
    assert applied["dry_run"] is False
    assert applied["eligible"] == 1
    assert applied["archived"] == 1
    assert not legacy.exists()
    archived = paths.persona_assignments_archive_dir() / "retired_task_id" / legacy.name
    assert archived.is_file()
    assert json.loads(archived.read_text(encoding="utf-8"))["task_id"] == "task_old"
    assert current.is_file()
    assert missing.is_file()

    repeated = migrate(dry_run=False)
    assert repeated["eligible"] == 0
    assert repeated["archived"] == 0
    assert repeated["assignment_ids"] == []


def test_free_floating_cli_discriminators_use_evidence_kind_alone():
    from hermes_cli import harness

    delete_source = inspect.getsource(harness._cmd_persona_chat_delete)
    close_source = inspect.getsource(harness._close_free_floating_assignments)
    queue_source = inspect.getsource(harness._queue_free_floating_assignment)

    assert ".task_id is None" not in delete_source
    assert ".task_id is None" not in close_source
    assert "task_id=None" not in queue_source
