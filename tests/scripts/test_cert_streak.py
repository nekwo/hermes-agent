from __future__ import annotations

from pathlib import Path

from scripts.cert_streak import append_row, validate_doc


def _manifest(*, case_id: str, green: bool = True):
    return {
        "case_id": case_id,
        "task_id": f"task_{case_id}",
        "status": "passed" if green else "blocked",
        "started_at": "2026-07-06T16:00:00+00:00",
        "finished_at": "2026-07-06T16:03:05+00:00",
        "actual_persona_sequence": ["neko_supervisor", "dev"],
        "proof_ids": ["proof_1"],
        "archive_dir": r"C:\archive\batch",
        "unattended": {
            "green": green,
            "failure_class": None if green else "manual_intervention",
            "manual_intervention_counts": {
                "manual_ticks": 0 if green else 1,
                "task_unblocks": 0,
                "manual_incident_closes": 0,
                "process_kills": 0,
            },
        },
    }


def test_append_row_records_custom_blueprint_metadata(tmp_path):
    doc = tmp_path / "cert_streak.md"

    row = append_row(doc, _manifest(case_id="custom-launcher-proof"))

    text = doc.read_text(encoding="utf-8")
    assert row["custom"] is True
    assert row["blueprint"] == "custom_launcher_proof"
    assert "custom-launcher-proof" in text
    assert "proof_1" in text


def test_validate_doc_requires_ten_green_and_three_custom(tmp_path):
    doc = tmp_path / "cert_streak.md"
    for index in range(7):
        append_row(doc, _manifest(case_id="noop-orchestration"))
    for case_id in ["custom-backend-proof", "custom-launcher-proof", "custom-cross-stack-proof"]:
        append_row(doc, _manifest(case_id=case_id))

    result = validate_doc(doc)

    assert result["ok"] is True
    assert result["green_count"] == 10
    assert result["custom_green_count"] == 3


def test_validate_doc_fails_when_manual_row_is_not_green(tmp_path):
    doc = tmp_path / "cert_streak.md"
    append_row(doc, _manifest(case_id="custom-launcher-proof", green=False))

    result = validate_doc(doc)

    assert result["ok"] is False
    assert result["green_count"] == 0
