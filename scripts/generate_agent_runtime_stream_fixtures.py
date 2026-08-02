"""Regenerate deterministic Agent Runtime stream contract fixtures.

The builder runs against a fresh isolated Hermes/runtime root and calls the
current production frame constructors. Volatile values are normalized only
after construction, so the fixture bytes stay reviewable.

Reproducibility, stated precisely
---------------------------------
Rerunning this script on ANY machine reproduces the committed bytes of the four
frames :func:`main` writes. That claim did **not** hold before the
``_MACHINE_PROBED_FLAGS`` normalization below, and the correction is recorded
here so a future reader does not have to re-derive it.

``core.repo_scopes`` is built by ``snapshot._repo_scope_entry``, whose
``resolved`` flag is ``resolve_affected_repo_workdir(alias) is not None``. That
resolver PROBES HARDCODED ABSOLUTE PATHS on the operator's disk — see
``agent_runtime.repo_context._REPO_ALIAS_PATHS``, e.g.
``X:/Unreal Engine/Engine/Launcher/EterniaLauncher`` and its EterniaBackend
siblings. On a box carrying those checkouts ``frontend`` and ``backend`` resolve
true; on CI, on a fresh clone, or on macOS/Linux they resolve false. The emitted
bytes therefore depended on WHO RAN THE SCRIPT, and a regeneration anywhere else
would have silently rewritten a byte-pinned cross-repo golden. (``harness`` was
never machine-dependent: it resolves through ``Path(__file__).parents[1]``,
i.e. this repo, which always exists.)

``resolved`` is now pinned to a fixture constant. That constant asserts NOTHING
about any machine's checkout layout — it is a sentinel of the same kind as
``FIXED_TIME`` and ``<isolated-root>``, and no code in either repo reads the
field (hermes pins only the three ``label`` values, in
``tests/agent_runtime/test_specialist_agents_red.py``; the launcher has no Dart
reader at all). It is pinned to the value the committed goldens already carry,
so retiring the machine-dependence cost no cross-repo byte churn; changing it
later is a cross-stack change under the fixture README's update rule. The
``label`` values are contractual and are deliberately NOT normalized.

What this script writes, and what it only pins
----------------------------------------------
:func:`main` regenerates the four frames in :data:`GENERATED_FRAME_FILES`. The
four files in :data:`PINNED_ONLY_FILES` are hand-authored and are only HASHED
into ``MANIFEST.sha256``; see that tuple's comment for why they cannot be
generated from the current production builders.
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
#: The frames :func:`main` builds and writes. Regenerating these reproduces the
#: committed bytes on any machine (see the module docstring).
GENERATED_FRAME_FILES = (
    "hydrate.json",
    "delta.json",
    "heartbeat.json",
    "delta_batch.json",
)

#: Hand-authored goldens this script only PINS: it hashes them into the manifest
#: and never rewrites them. They are not regenerable from the current production
#: builders, and the reason is structural rather than effort:
#:
#: * ``patch.json`` / ``patch_upsert_profile.json`` / ``patch_remove.json`` are
#:   S6 v2 field-patch frames carrying REAL wall-clock stamps
#:   (``2026-07-17T04:22:55.149761Z``, not :data:`FIXED_TIME`) and hand-chosen
#:   ``base_offset`` / ``seq`` pairs that demonstrate specific fold semantics
#:   over entities this script's seeded root does not contain.
#:   ``patch_remove.json`` is moreover UN-EMITTABLE today: it is the
#:   ``incident.closed`` remove fold, and S65 de-registered that event with its
#:   last writer. ``agent_runtime.patch_coverage`` keeps it in
#:   ``HISTORICAL_COVERED_DOMAIN_EVENT_TYPES`` precisely so a cross-stack fixture
#:   replaying an old batch still classifies the way the launcher folded it when
#:   the event was live — regenerating that frame would mean resurrecting a
#:   retired lane.
#: * ``patch_coverage_manifest.json`` is not a frame at all: it is the S7-A
#:   coverage TABLE.
#:
#: They are maintained by hand and validated by SHAPE plus live-classifier
#: agreement in ``tests/agent_runtime/test_stream_patch.py``
#: (``test_patch_fixtures_manifest_and_shape``,
#: ``test_coverage_manifest_agrees_with_classifier``), not by byte-regeneration.
PINNED_ONLY_FILES = (
    "patch.json",
    "patch_upsert_profile.json",
    "patch_remove.json",
    "patch_coverage_manifest.json",
)

#: Everything ``MANIFEST.sha256`` pins, in manifest order.
MANIFEST_FILES = GENERATED_FRAME_FILES + PINNED_ONLY_FILES
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
_MACHINE_PROBED_FLAGS = {
    # container key -> (per-entry flag key, pinned value)
    #
    # The ONE value in the frame that answers a question about the operator's
    # DISK rather than about the isolated runtime root. See the module docstring
    # for the full derivation: `repo_scopes[*].resolved` is a probe of hardcoded
    # absolute paths in `agent_runtime.repo_context._REPO_ALIAS_PATHS`, so
    # without this pin the script emitted different bytes on a machine that
    # lacks those checkouts (CI, a fresh clone, macOS/Linux).
    #
    # Only the flag is pinned. `label` is contractual and survives untouched, so
    # this stays a normalization rather than a rewrite of the section.
    "repo_scopes": ("resolved", True),
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
        if key in _MACHINE_PROBED_FLAGS:
            flag_key, pinned = _MACHINE_PROBED_FLAGS[key]
            for entry in normalized.values():
                if isinstance(entry, dict) and flag_key in entry:
                    entry[flag_key] = pinned
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
    for name in MANIFEST_FILES:
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
        # A frame that silently drops out of the built set while staying in
        # MANIFEST_FILES would become hand-maintained without anyone saying so —
        # exactly the undocumented split this constant pair exists to retire.
        assert tuple(frames) == GENERATED_FRAME_FILES, (
            "main() must build exactly GENERATED_FRAME_FILES; anything else "
            "belongs in PINNED_ONLY_FILES with a recorded reason"
        )
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
