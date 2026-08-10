"""S55 — the structural gate: every registered event type must have an emitter.

This is the wave's "stop it regrowing" half. S49/S52/S53 de-registered fourteen
contracts whose emitters had gone, and they were not the first: S25 retired
``repo_bundle.delivered`` one commit BEHIND the S24 cut that deleted its writer,
and S44 retired six ``role_envelope.*`` / ``role_checklist.*`` types with the
store family. Each time the registration outlived the emitter, and each time it
was found by hand, one wave late. A registered type with no writer is a shape
the prompt contract advertises, the manifest publishes, ``contract_hash()``
stamps onto every live persona instance, and ``EventLog.append`` accepts — so a
test (or an agent) can mint an event no reader will ever see.

WHERE THIS FILE SITS. It is deliberately NOT folded into the S15 family. S15 and
its descendants are *records* — a frozen list of what a particular stage pruned,
plus one absolute count. This is a *rule*: it takes no list of names and stays
true as the registry changes. Mixing them would put a growing hand-maintained
list back into the one place the wave was trying to remove one from.

=============================================================================
WHAT THIS GATE PROVES, AND WHAT IT DOES NOT
=============================================================================

It proves: **every registered type has a literal emission site in production
code that reaches an ``Event(...)`` construction.** The resolver below is
AST-based and string-form aware — it follows four idioms this codebase actually
uses, which a naive ``Event(type="x")`` scan misses entirely:

1. keyword       ``Event(type="run.closed", ...)``
2. POSITIONAL    ``Event(now(), "self_test.recorded", task_id, ...)`` — ``type``
                 is field 1 of the dataclass, and roughly half the emitters in
                 the tree use this form.
3. wrapper       ``self._event("board.card.created", ...)`` where ``_event``
                 forwards its parameter into an ``Event(...)``. Resolved to a
                 fixed point, so wrappers-calling-wrappers are followed.
4. module const  ``Event(type=STATE_PATCHED_EVENT_TYPE)`` where the constant is
                 a module-level string literal.

It does NOT prove reachability. An emitter that is physically present but sits
inside a lane no production caller can reach still counts as an emitter here.
That is a deliberate boundary, not an oversight:

* Deciding "this method has no production caller" soundly needs receiver-typed
  call-graph analysis. Name-based reachability is worse than nothing on this
  tree — ``update``, ``save``, ``get`` and ``transition`` each collide with
  many unrelated definitions (a bare ``transition`` grep returns 268 hits), so
  a name-based graph would mark dead lanes reachable and quietly pass.
* The unreachable-but-present class is what the per-cut removal-contract tests
  own (S49/S50/S52/S53/S54), each of which records the receiver-aware
  verification for the specific lane it cut.
* The class this gate DOES catch is the one that actually recurred: an emitter
  physically deleted with its lane, registration left behind. That is exactly
  S24 -> S25 and S43 -> S44. Both would have gone red here in the same commit
  that deleted the writer, instead of one wave later.

RED-PROOF (run before this file landed, on the pre-wave tree ``3f9948eed``):
deleting the single ``self._event("repo_bundle.created", ...)`` call while
leaving its registration in place moved the resolver from 3 emitter-less types
to 4, naming ``repo_bundle.created``. That is the S24 regression reproduced, and
this gate fails on it. The sabotage was reverted; the check is reproducible by
removing any emitter without de-registering the type.

=============================================================================
THE ALLOWLIST
=============================================================================

Three types, one reason, and it is a real property of the code rather than a
convenience: ``progress.py`` dispatches by DATA, not by literal. Its
``ProgressEmitter.callback`` does ``self.emit(str(payload.get("type",
"run.progress")), payload)`` — the event type arrives inside a payload dict that
``profile_runner`` builds, so at the ``Event(...)`` construction the type is a
runtime value with no literal in scope. No static resolver can bind it without
tracing dict contents across module boundaries.

Rather than trust the allowlist, each entry carries a WITNESS assertion below
proving the literal really is on the progress dispatch path. An allowlist entry
that stops being true fails just like a missing emitter.

NOT allowlisted, though the wave expected them to be:

* ``state.patched`` — config-gated behind ``read_model.delta_patches`` (default
  off), but the emitter is a plain ``Event(type=STATE_PATCHED_EVENT_TYPE)`` and
  idiom 4 resolves it. Config gating is a RUNTIME condition; this gate is
  static, so a gated emitter is still an emitter. No entry needed.
* ``child.returned`` — gated behind ``supervision.child_events_enabled``, same
  reasoning; ``child_events.py`` emits it with a literal. No entry needed.
* **The ten ``worker_session.*`` types.** The wave's plan reserved a temporary
  "S51 pending" allowlist entry for them. **It is not needed and was not
  added.** ``worker_sessions.py`` still emits all ten through its live
  ``self._event`` wrapper, so idiom 3 resolves every one and this gate passes
  without an exemption. When the S51 follow-up cuts that write lane it must
  de-register the ten types in the same commit — and if it forgets, THIS GATE
  GOES RED, which is precisely the outcome the temporary entry would have
  suppressed. Adding it would have been a hole, so there is nothing here for
  the follow-up wave to remove.
"""

from __future__ import annotations

import ast
import functools
import inspect
import os
from pathlib import Path

import pytest

from agent_runtime.decision_contract_registry import event_catalog


#: Production packages scanned for emitters. Tests are excluded on purpose: an
#: event type kept alive only by a test append is exactly the debt S15 named.
PRODUCTION_PACKAGES = (
    "agent_runtime",
    "hermes_cli",
    "gateway",
    "agent",
    "acp_adapter",
    "tools",
    "cron",
    "mobile_core",
    "providers",
    "tui_gateway",
    "scripts",
    "apps",
)

#: Types whose emission type is a runtime value, not a literal, at the
#: ``Event(...)`` construction. Each is witnessed below.
DYNAMIC_DISPATCH_ALLOWLIST = {
    "run.progress": "progress.ProgressEmitter.callback dispatches on payload['type']",
    "run.tool.started": "profile_runner passes the label into _progress_adapter",
    "run.tool.finished": "profile_runner passes the label into _progress_adapter",
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@functools.lru_cache(maxsize=1)
def _production_trees_cached() -> tuple[tuple[str, ast.Module], ...]:
    return tuple(_production_trees_uncached().items())


def _production_trees() -> dict[str, ast.Module]:
    """Parsing the whole production tree is the expensive step; several cases
    need it, so it is parsed once per session."""

    return dict(_production_trees_cached())


def _production_trees_uncached() -> dict[str, ast.Module]:
    root = _repo_root()
    trees: dict[str, ast.Module] = {}
    paths: list[Path] = []
    for package in PRODUCTION_PACKAGES:
        directory = root / package
        if not directory.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(directory):
            dirnames[:] = [
                d for d in dirnames if d not in {"__pycache__", "node_modules"} and not d.startswith(".venv")
            ]
            paths += [Path(dirpath) / name for name in filenames if name.endswith(".py")]
    paths += [p for p in root.glob("*.py")]
    for path in paths:
        try:
            trees[str(path)] = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
    return trees


def _module_string_constants(trees: dict[str, ast.Module]) -> dict[str, str]:
    """Idiom 4: module-level ``NAME = "literal"``."""

    constants: dict[str, str] = {}
    for tree in trees.values():
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                if isinstance(node.value.value, str):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            constants[target.id] = node.value.value
    return constants


def _slot_value(call: ast.Call, index: int | None, keyword: str | None) -> ast.expr | None:
    if keyword:
        for kw in call.keywords:
            if kw.arg == keyword:
                return kw.value
    if index is not None and index < len(call.args):
        if not any(isinstance(arg, ast.Starred) for arg in call.args[: index + 1]):
            return call.args[index]
    return None


@functools.lru_cache(maxsize=1)
def _emitted_event_types_cached() -> tuple[tuple[str, tuple[str, ...]], ...]:
    return tuple((k, tuple(v)) for k, v in _emitted_event_types().items())


def emitted_event_types() -> dict[str, list[str]]:
    """Session-cached; the resolver walks every production AST."""

    return {k: list(v) for k, v in _emitted_event_types_cached()}


def _emitted_event_types() -> dict[str, list[str]]:
    """Every event-type literal that reaches an ``Event(...)`` construction.

    Returns ``{event_type: [\"path:line\", ...]}`` so a failure names the site.
    """

    trees = _production_trees()
    constants = _module_string_constants(trees)

    # A sink is (callee_name, positional_index, keyword_name). Seeded with the
    # Event dataclass itself: ``type`` is keyword ``type`` or positional 1.
    sinks: set[tuple[str, int | None, str | None]] = {("Event", 1, "type")}
    emitted: dict[str, list[str]] = {}

    for _round in range(8):
        discovered: set[tuple[str, int | None, str | None]] = set()
        for path, tree in trees.items():
            for func in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
                args = func.args
                positional = [a.arg for a in (*args.posonlyargs, *args.args)]
                keyword_only = [a.arg for a in args.kwonlyargs]
                is_method = bool(positional) and positional[0] in {"self", "cls"}
                for call in [n for n in ast.walk(func) if isinstance(n, ast.Call)]:
                    func_node = call.func
                    if isinstance(func_node, ast.Name):
                        name = func_node.id
                    elif isinstance(func_node, ast.Attribute):
                        name = func_node.attr
                    else:
                        continue
                    for sink_name, index, keyword in list(sinks):
                        if name != sink_name:
                            continue
                        value = _slot_value(call, index, keyword)
                        if value is None:
                            continue
                        if isinstance(value, ast.Constant) and isinstance(value.value, str):
                            emitted.setdefault(value.value, []).append(f"{path}:{call.lineno}")
                        elif isinstance(value, ast.Name) and value.id in constants:
                            emitted.setdefault(constants[value.id], []).append(f"{path}:{call.lineno}")
                        elif isinstance(value, ast.Name):
                            # This function forwards a parameter into a sink, so
                            # it IS a sink (idiom 3). Bound methods drop `self`
                            # from the call-site argument list.
                            if value.id in positional:
                                at = positional.index(value.id)
                                discovered.add((func.name, at - 1 if is_method else at, value.id))
                            elif value.id in keyword_only:
                                discovered.add((func.name, None, value.id))
        if discovered <= sinks:
            break
        sinks |= discovered
    return emitted


def test_every_registered_event_type_has_a_production_emitter():
    """THE GATE. A registered type with no writer is a shape the contract
    advertises and no reader will ever see."""

    emitted = emitted_event_types()
    missing = sorted(set(event_catalog()) - set(emitted) - set(DYNAMIC_DISPATCH_ALLOWLIST))
    assert missing == [], (
        "registered event types with no production emitter: "
        f"{missing}. Either de-register them in the same commit that removed "
        "the emitter (the S24->S25 / S43->S44 lesson), or add a reasoned "
        "allowlist entry WITH a witness assertion."
    )


def test_the_allowlist_is_not_a_dumping_ground():
    """Every allowlisted type must still be registered. An entry for a type that
    no longer exists is stale decoration, and an allowlist nobody prunes is how
    a gate quietly stops gating."""

    catalog = set(event_catalog())
    stale = sorted(name for name in DYNAMIC_DISPATCH_ALLOWLIST if name not in catalog)
    assert stale == [], f"allowlist names types that are no longer registered: {stale}"


def _module_tree(module) -> ast.Module:
    return ast.parse(inspect.getsource(module))


def _callee_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _calls_to(tree: ast.AST, name: str) -> list[ast.Call]:
    """Every call in ``tree`` whose callee spells ``name`` (bare or attribute)."""

    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _callee_name(node.func) == name
    ]


def _is_call(node: ast.expr, name: str) -> bool:
    return isinstance(node, ast.Call) and _callee_name(node.func) == name


def _const_args(call: ast.Call) -> list[object]:
    """Positional args, with non-literals rendered as ``None``."""

    return [a.value if isinstance(a, ast.Constant) else None for a in call.args]


def test_each_allowlisted_type_is_witnessed_on_the_dynamic_dispatch_path():
    """The allowlist claims these three arrive as DATA rather than as a literal
    at the Event construction. Asserted structurally, not trusted.

    WHY AST AND NOT SUBSTRING (converted 2026-08-09). This witness used to match
    raw source text, and it did what every text-matching gate eventually does: a
    purely cosmetic reformat of the ``_progress_adapter`` call sites broke it,
    and the author had to keep three calls on one line to appease a test that
    has no opinion about line breaks. That is a change-detector wearing a
    witness's clothes. The GUARANTEE is real and worth pinning — the runner
    supplies the label, the sink never writes it, which is exactly why the
    static resolver above cannot see these three types — but line layout was
    never part of it, and a witness that fails on formatting trains readers to
    edit the code to suit the test.

    Comments, string continuations, and reflowing all defeat substring matching,
    which is why structural gates in this repo are AST- or token-based. The
    resolver above always was; this witness now matches it.
    """

    from agent_runtime import profile_runner, progress

    # --- 1. The sink dispatches on payload DATA, not on a literal. -----------
    # Was: the exact text `self.emit(str(payload.get("type", "run.progress")),
    # payload)`. Now: an `emit` call whose type argument is a `payload.get(
    # "type", <default>)` (optionally wrapped in `str(...)`). Any spelling of
    # that shape survives; replacing it with a literal — which is the change
    # that would make this allowlist entry FALSE, because the resolver could
    # then see the type — fails.
    progress_tree = _module_tree(progress)
    dispatch_defaults: list[object] = []
    for call in _calls_to(progress_tree, "emit"):
        if not call.args:
            continue
        first = call.args[0]
        inner = first.args[0] if _is_call(first, "str") and first.args else first
        if not _is_call(inner, "get"):
            continue
        args = _const_args(inner)
        if args and args[0] == "type":
            dispatch_defaults.append(args[1] if len(args) > 1 else None)

    assert dispatch_defaults, (
        "ProgressEmitter no longer dispatches on payload['type']. If the event "
        "type became a literal at the sink, the static resolver can now see it "
        "and these three types must LEAVE the allowlist rather than keep an "
        "exemption that is no longer true."
    )
    assert "run.progress" in dispatch_defaults, (
        "the payload-driven dispatch no longer defaults to 'run.progress'; "
        f"defaults found: {dispatch_defaults}"
    )

    # --- 2. The trace gate is a VALUE, so compare the value. -----------------
    # Was: the exact text of the set literal, which made the SAME set written in
    # a different order a failure. Importing it compares what actually gates.
    assert progress._CHAT_TRACE_EVENT_TYPES == {
        "run.tool.started",
        "run.tool.finished",
        "run.progress",
    }

    # --- 3. The runner hands the two tool labels in at the CALL SITE. --------
    # This is the entire claim of the allowlist for these two: the label is an
    # ARGUMENT, so no literal is in scope where the Event is constructed. The
    # old form additionally required the first argument to be spelled
    # `request.progress_callback` on the same line — neither of which the
    # guarantee cares about.
    runner_tree = _module_tree(profile_runner)
    adapter_labels = {
        args[1]
        for args in (_const_args(c) for c in _calls_to(runner_tree, "_progress_adapter"))
        if len(args) > 1 and isinstance(args[1], str)
    }
    for label in ("run.tool.started", "run.tool.finished"):
        assert label in adapter_labels, (
            f"{label} is no longer passed into _progress_adapter by the runner. "
            f"Labels at the call sites: {sorted(adapter_labels)}"
        )

    # ...and `_progress_adapter` FORWARDS that parameter rather than writing its
    # own type. If it ever hardcoded one, the resolver would find it and the
    # exemption would be stale decoration.
    adapter_def = next(
        node
        for node in ast.walk(runner_tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_progress_adapter"
    )
    assert [
        call
        for call in _calls_to(adapter_def, "_progress_payload_from_callback")
        if any(isinstance(a, ast.Name) and a.id == "event_type" for a in call.args)
    ], (
        "_progress_adapter no longer forwards its `event_type` parameter into "
        "the payload builder — the label may now be written at the sink, which "
        "would make the allowlist entries for the two tool types wrong."
    )

    # --- 4. The runner builds the bare progress type as payload DATA. -------
    # Was: the substring `"type": "run.progress"`, which a docstring or a
    # comment mentioning it would have satisfied.
    assert [
        node
        for node in ast.walk(runner_tree)
        if isinstance(node, ast.Dict)
        and any(
            isinstance(k, ast.Constant)
            and k.value == "type"
            and isinstance(v, ast.Constant)
            and v.value == "run.progress"
            for k, v in zip(node.keys, node.values)
        )
    ], (
        "no `{'type': 'run.progress', ...}` payload is built in profile_runner; "
        "run.progress reaches the sink some other way now and its allowlist "
        "entry needs re-deriving."
    )


def test_the_witness_is_structural_and_survives_reformatting():
    """RED-PROOF for the conversion above, run permanently.

    The defect being retired is specific: a cosmetic reflow broke the witness.
    So the proof is equally specific — feed the four idioms to the same helpers
    the witness uses, in a REFORMATTED spelling of each, and require them to
    resolve. A witness that had quietly gone back to matching text would fail
    here while the code it guards was untouched.
    """

    reflowed = ast.parse(
        "\n".join(
            (
                "def sink(self, payload):",
                "    self.emit(",
                "        # the type arrives as data, on its own line now",
                "        str(",
                '            payload.get("type", "run.progress")',
                "        ),",
                "        payload,",
                "    )",
                "",
                "def runner(request, budget_guard):",
                "    return {",
                '        "tool_start_callback": _progress_adapter(',
                "            request.progress_callback,",
                '            "run.tool.started",',
                "            guard=budget_guard,",
                "        ),",
                '        "payload": {"type": "run.progress"},',
                "    }",
            )
        )
    )

    dispatch = [
        _const_args(c.args[0].args[0])
        for c in _calls_to(reflowed, "emit")
        if c.args and _is_call(c.args[0], "str") and _is_call(c.args[0].args[0], "get")
    ]
    assert dispatch == [["type", "run.progress"]], dispatch

    labels = {
        args[1]
        for args in (_const_args(c) for c in _calls_to(reflowed, "_progress_adapter"))
        if len(args) > 1
    }
    assert labels == {"run.tool.started"}, labels

    # And the negative direction: prose mentioning the label must NOT satisfy
    # the label witness. This is the substring gate's original hole.
    prose = ast.parse('"""calls _progress_adapter(cb, \\"run.tool.finished\\")."""')
    assert _calls_to(prose, "_progress_adapter") == []


def test_the_resolver_actually_resolves_each_emission_idiom():
    """A gate that silently resolved nothing would pass forever. Each of the
    four idioms is pinned to a type only that idiom can reach."""

    emitted = emitted_event_types()

    # Wrapper forwarding (self._event / self._emit / _append_realm_sync_event).
    assert "board.card.created" in emitted
    assert "persona_instance.created" in emitted
    assert "realm.sync.published" in emitted
    # 4. module-level string constant.
    assert "state.patched" in emitted
    assert "persona.profile_rebound" in emitted


def test_the_worker_session_family_needed_no_temporary_exemption():
    """INVERTED at S56, with its reason preserved.

    The S51 plan reserved an 'S51 pending' allowlist entry for these ten types.
    It was deliberately never added: they still had live emitters, so the gate
    passed without it — and the entry would have been a hole, because this gate
    going RED is exactly what should happen if the write lane is cut without
    de-registering the contracts in the SAME commit.

    S56 cut the lane and de-registered all ten together, so the family is now
    absent from BOTH sides. The assertion is inverted rather than deleted: it is
    what catches a re-registration with no emitter behind it, and it must never
    be satisfiable by an allowlist entry."""

    worker_types = sorted(name for name in event_catalog() if name.startswith("worker_session."))
    assert worker_types == []
    assert [name for name in DYNAMIC_DISPATCH_ALLOWLIST if name.startswith("worker_session.")] == []


def test_no_test_only_append_can_satisfy_the_gate():
    """The scan is production-scoped on purpose: a type kept alive only by a
    test append is exactly the debt S15 spent a stage clearing."""

    root = _repo_root()
    assert all(not package.startswith("tests") for package in PRODUCTION_PACKAGES)
    scanned = _production_trees()
    assert scanned, "resolver scanned no files — the gate would pass vacuously"
    assert not [path for path in scanned if (root / "tests") in Path(path).parents]


@pytest.mark.parametrize("event_type", sorted(DYNAMIC_DISPATCH_ALLOWLIST))
def test_allowlisted_types_are_still_appendable(event_type: str):
    """Negative gate: allowlisting is about STATIC resolution, not liveness.
    These types must still be registered and acceptable to ``append``."""

    from agent_runtime.events import ALLOWED_EVENT_TYPES

    assert event_type in ALLOWED_EVENT_TYPES
