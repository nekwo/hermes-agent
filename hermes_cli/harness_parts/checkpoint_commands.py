# Exec'd command part (see harness._load_command_parts), with its own explicit
# import header — exactly as in flow_commands.py / persona_commands.py /
# runtime_commands.py.
#
# The checkpoint lane (Stage S5, 2026-07-16): the read model's recovery/hydrate
# substrate. Operator ruling — "entity classes, persisted per-actor, like
# Unreal: the store IS the checkpoint; wire bundles are transport envelopes,
# never a second persistence format." These verbs are READ-ONLY: they bundle
# the EXISTING on-disk per-actor store files into one keyed transport envelope
# (keyed by entity class → actor id), reading every row verbatim. Nothing is
# written anywhere.

# Explicit import header. Still exec'd into harness.py's globals by
# _load_command_parts — that mechanism is unchanged — but no longer dependent
# on it: these names used to arrive implicitly from whatever harness.py
# imported, so a wrong one surfaced as a NameError only when an operator ran
# the one verb that touched it. Re-importing a name harness.py also imports
# rebinds it to the identical object; both halves are checked by
# tests/hermes_cli/test_harness_parts_namespace.py.

from __future__ import annotations

from agent_runtime.cli_format import emit_json


def _checkpoint_split_classes(value):
    """Parse a ``--classes a,b,c`` filter into a name list (None = all)."""

    if not value:
        return None
    names = [part.strip() for part in str(value).split(",")]
    names = [name for name in names if name]
    return names or None


def _cmd_checkpoint_fetch(args) -> int:
    from agent_runtime.checkpoint import build_checkpoint

    classes = _checkpoint_split_classes(getattr(args, "classes", None))
    row_cap = getattr(args, "max_rows", None)
    envelope = build_checkpoint(classes, row_cap=row_cap)
    if args.json:
        print(emit_json(envelope))
        return 0
    counts = envelope.get("counts") or {}
    total_actors = sum(int(value or 0) for value in counts.values())
    lines = [
        f"checkpoint v{envelope['checkpoint_version']}: "
        f"{len(envelope.get('classes') or {})} classes, {total_actors} actors, "
        f"~{envelope.get('bytes_estimate', 0)} bytes"
    ]
    for name in sorted(counts):
        suffix = ""
        truncation = (envelope.get("truncations") or {}).get(name)
        if truncation:
            suffix = f" (truncated: {truncation['returned']} of {truncation['total']})"
        lines.append(f"  {name}: {counts[name]}{suffix}")
    for absent in envelope.get("requested_absent") or []:
        lines.append(f"  {absent}: (absent — no store on this root)")
    print("\n".join(lines))
    return 0


def _cmd_checkpoint_classes(args) -> int:
    from agent_runtime.checkpoint import class_manifest

    manifest = class_manifest()
    if args.json:
        print(emit_json(manifest))
        return 0
    entries = manifest.get("classes") or []
    if not entries:
        print("no entity-class stores discovered on this runtime root")
        return 0
    for entry in entries:
        print(f"{entry['class']}: {entry['count']} actors, {entry['bytes']} bytes")
    return 0
