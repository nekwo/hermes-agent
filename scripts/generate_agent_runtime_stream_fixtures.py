"""Regenerate deterministic Agent Runtime stream contract fixtures.

The builder runs against a fresh isolated Hermes/runtime root and calls the
current production frame constructors. Volatile timestamps, timings, and the
temporary root spelling are normalized only after construction so fixture
bytes stay reviewable and reproducible across machines.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "stream_frames"
FIXED_TIME = "2026-07-16T12:00:00.000000Z"
FRAME_FILES = (
    "hydrate.json",
    "delta.json",
    "heartbeat.json",
    "delta_batch.json",
    "patch.json",
    "patch_upsert_profile.json",
    "patch_remove.json",
    "patch_coverage_manifest.json",
)
_TIME_KEYS = {
    "generated_at",
    "captured_at",
    "created_at",
    "updated_at",
    "started_at",
    "finished_at",
    "last_heartbeat_at",
}
_VOLATILE_METRICS = {
    "build_ms",
    "snapshot_bytes",
    "event_log_bytes",
    "projection_age_ms",
}
_VOLATILE_METRIC_MAPS = {
    # Each section is timed independently while the parity snapshot is built.
    # Normalizing only the container key keeps configured/contractual durations
    # elsewhere in the frame intact while removing scheduler and filesystem
    # jitter from every current and future section name.
    "sections_ms",
}


def _normalize(value: Any, *, isolated_root: Path, key: str = "") -> Any:
    if isinstance(value, dict):
        if key in _VOLATILE_METRIC_MAPS:
            return {str(item_key): 0 for item_key in value}
        normalized = {
            str(item_key): _normalize(item, isolated_root=isolated_root, key=str(item_key))
            for item_key, item in value.items()
        }
        if key == "runtime_root" and "fingerprint" in normalized:
            normalized["fingerprint"] = "isolated-runtime"
        return normalized
    if isinstance(value, list):
        return [_normalize(item, isolated_root=isolated_root) for item in value]
    if key in _TIME_KEYS and value is not None:
        return FIXED_TIME
    if key in _VOLATILE_METRICS and value is not None:
        return 0
    if isinstance(value, str):
        root = str(isolated_root)
        return value.replace(root, "<isolated-root>").replace(root.replace("\\", "/"), "<isolated-root>")
    return value


def _write_json(name: str, value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
    (FIXTURE_ROOT / name).write_text(payload, encoding="utf-8", newline="\n")


def _write_manifest() -> None:
    lines = []
    for name in FRAME_FILES:
        digest = hashlib.sha256((FIXTURE_ROOT / name).read_bytes()).hexdigest()
        lines.append(f"{digest}  {name}")
    (FIXTURE_ROOT / "MANIFEST.sha256").write_text(
        "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
    )


def main() -> int:
    # SessionDB keeps a process-lifetime SQLite handle on Windows.  Ignoring a
    # cleanup race here lets the interpreter release that handle at exit; the
    # fixture bytes themselves never contain or depend on the temporary path.
    with tempfile.TemporaryDirectory(
        prefix="hermes-stream-fixtures-", ignore_cleanup_errors=True
    ) as temp:
        isolated_root = Path(temp)
        hermes_home = isolated_root / "hermes"
        runtime_root = isolated_root / "runtime"
        hermes_home.mkdir()
        runtime_root.mkdir()
        os.environ["HERMES_HOME"] = str(hermes_home)
        os.environ["HERMES_HEAD_HOME"] = str(hermes_home)
        os.environ["HERMES_AGENT_RUNTIME_ROOT"] = str(runtime_root)
        os.environ["LOCALAPPDATA"] = str(isolated_root / "local")

        from datetime import datetime, timedelta, timezone

        from agent_runtime.events import EventLog
        from agent_runtime.models import Event
        from agent_runtime.serde import to_jsonable
        from agent_runtime.stream import (
            delta_batch_frame,
            delta_frame,
            heartbeat_frame,
            hydrate_frame,
        )

        hydrate = hydrate_frame()
        core = hydrate["core"]
        log = EventLog()
        first = Event(
            ts=datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc),
            type="state.reconciled",
            task_id="task_shape",
            run_id=None,
            persona_id="custom_agent",
            payload={"fingerprint": "shape-fp"},
        )
        second = Event(
            ts=first.ts + timedelta(seconds=1),
            type="state.reconciled",
            task_id="task_shape",
            run_id=None,
            persona_id="custom_agent",
            payload={"fingerprint": "shape-fp-2"},
        )
        log.append(first)
        log.append(second)
        batch = list(log.iter_from_offset(0))

        frames = {
            "hydrate.json": hydrate,
            "delta.json": delta_frame(first, offset=batch[0][0], snapshot=core),
            "heartbeat.json": heartbeat_frame(offset=7),
            "delta_batch.json": delta_batch_frame(batch, snapshot=core),
        }
        prompt_observability = core.get("prompt_observability") or {}
        assert "default_flow" not in prompt_observability
        for name in ("delta.json", "delta_batch.json"):
            assert frames[name]["core"] is core
            assert frames[name]["core"]["parity"]["capabilities"] == core["parity"][
                "capabilities"
            ]
            assert frames[name]["core"]["parity"]["completeness"] == core["parity"][
                "completeness"
            ]
        for name, frame in frames.items():
            _write_json(
                name,
                _normalize(to_jsonable(frame), isolated_root=isolated_root),
            )
        _write_manifest()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
