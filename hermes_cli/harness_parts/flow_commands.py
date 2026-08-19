# Exec'd command part (see harness._load_command_parts), with its own explicit
# import header — exactly as in persona_commands.py / runtime_commands.py.
#
# The flow lane (2026-07-16): the Launcher's authored agent map travels as ONE
# JSON document. `flow set` stores it and reconciles steering for the EXISTING
# instances it references, in-process — one spawn for the whole chart instead
# of one `persona instance steer` spawn per agent. Ingest never creates an
# instance and never touches goal membership (clear_parents, not
# detach_parents). Steering is OWNER-SCOPED (2026-07-18): the graph id names the
# owner instance, and the map asserts only that owner's `owner -> child` edges;
# non-owner edges are reported (ignored_non_owner_edges), never applied.

# Explicit import header — its rationale lives ONCE, in
# ``hermes_cli/harness_support.py``'s module docstring, and the pair of
# guarantees it rests on are checked by
# tests/hermes_cli/test_harness_parts_namespace.py.

from __future__ import annotations

from pathlib import Path

from agent_runtime.cli_format import emit_json


def _cmd_flow_set(args) -> int:
    from agent_runtime.flow_graph import FlowGraphDocError, ingest_flow_graph

    inline = getattr(args, "graph", None)
    file_path = getattr(args, "graph_file", None)
    if bool(inline) == bool(file_path):
        data = {"ok": False, "error": "provide exactly one of --graph / --graph-file"}
        print(emit_json(data) if args.json else data["error"])
        return 2
    if file_path:
        try:
            payload_text = Path(file_path).read_text(encoding="utf-8")
        except OSError as exc:
            data = {"ok": False, "error": f"could not read --graph-file: {exc}"}
            print(emit_json(data) if args.json else data["error"])
            return 2
    else:
        payload_text = inline
    try:
        report = ingest_flow_graph(
            payload_text,
            requested_by=getattr(args, "requested_by", None),
        )
    except FlowGraphDocError as exc:
        data = {"ok": False, "error": str(exc)}
        print(emit_json(data) if args.json else data["error"])
        return 2
    if args.json:
        print(emit_json(report))
    else:
        line = (
            f"flow {report['graph_id']} (owner {report['owner_instance_id']}): "
            f"stored; {report['changed_count']} changed, "
            f"{report['failed_count']} failed of {report['bound_agent_count']} "
            f"bound agents"
        )
        ignored = report.get("ignored_non_owner_edge_count", 0)
        if ignored:
            line += f"; {ignored} non-owner edge(s) ignored (layout only)"
        print(line)
    return 0


def _cmd_flow_show(args) -> int:
    from agent_runtime.flow_graph import FlowGraphStore

    stored = FlowGraphStore().get(args.graph_id)
    if stored is None:
        data = {"ok": False, "error": f"no stored flow graph: {args.graph_id}"}
        print(emit_json(data) if args.json else data["error"])
        return 2
    data = {"ok": True, **stored}
    print(emit_json(data) if args.json else f"flow {args.graph_id}: updated_at {stored.get('updated_at')}")
    return 0


def _cmd_flow_list(args) -> int:
    from agent_runtime.flow_graph import FlowGraphStore

    ids = FlowGraphStore().list_ids()
    data = {"ok": True, "graph_ids": ids, "count": len(ids)}
    print(emit_json(data) if args.json else "\n".join(ids))
    return 0
