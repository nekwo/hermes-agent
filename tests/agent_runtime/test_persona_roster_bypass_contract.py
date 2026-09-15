"""RD-H6 item 3 — the roster-check BYPASS is fenced by enumeration.

``agent_create._persona_is_unknown`` opens with::

    if persona is not None:
        return False

That is deliberate and documented (D-U1): the caller's already-resolved persona
object comes from a resolver that is a strict SUPERSET of
``agent_create.resolve_persona`` — it also handles ``profile:`` synthesis and
persona-INSTANCE id spellings — so "the caller found one" settles the question
without a second, narrower lookup contradicting it.

It is also an unconditional bypass of the only roster check that runs before a
durable write. It is safe TODAY because every caller resolves through the strict
path; nothing fences the seam, so a future caller that synthesises a persona
object (or reads one off a client payload) silently turns UC-H2's refusal back
into the fail-open it replaced — minting a roster row, a chat root and a
placement for an agent class nobody declared.

WHY THE FENCE IS A TEST AND NOT RUNTIME CODE. Making ``_persona_is_unknown``
re-verify the passed object against the roster would re-introduce exactly the
contradiction the parameter exists to avoid: the strict CLI resolver legitimately
answers for ids the narrow roster lookup does not (``profile:<token>`` owns no
persona row at all — see D-U1's carve-out and its own witness test). The risk is
"a NEW caller arrives with a persona from somewhere else", which is a change to
the caller set; a test over the caller set is the instrument that matches it, and
it costs nothing at runtime. RD-H6 says so in as many words.

WHAT THIS FILE PINS, in two independent halves:

1. the bypass BEHAVIOUR, asserted positively — so the enumeration below is
   understood to be load-bearing rather than decorative, and so a future reader
   cannot mistake the invariant for something the runtime already enforces;
2. the CALLER SET, by AST enumeration, with each caller's RESOLVER resolved
   through its local binding rather than read as a variable name. Pinning
   ``persona`` (the name) would be satisfied by any mutant that kept the
   spelling and changed the right-hand side, which is the whole defect shape.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from agent_runtime import agent_create
from tests.agent_runtime import _tree_index

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Every production entry point that accepts a caller-supplied persona object,
#: paired with WHERE that object sits in the call. ``_persona_is_unknown`` is
#: included as its own row: it is the resolver whose invariant this is, and a
#: caller reaching it directly would bypass the two wrappers' arms as well.
PERSONA_TAKING_ENTRY_POINTS = {
    "perform_agent_create": ("persona", None),
    "normalize_agent_create": ("persona", None),
    "require_known_persona": ("persona", 1),
    "_persona_is_unknown": ("persona", 1),
}

#: The resolvers a caller may hand to those entry points. Each reads the roster
#: through ``config.ensure_persisted_personas`` and answers ``None`` — never a
#: synthesised stand-in — for a persona the roster does not hold, EXCEPT for the
#: sanctioned ``profile:`` synthesis D-U1 carves out by id shape rather than by
#: object.
#:
#: ``_cli_create_persona`` is ``_persona_by_id`` plus RD-H6 item 2's typed
#: roster-fault refusal; it resolves identically and refuses rather than
#: degrading, so it is strictly stronger than the bare call.
STRICT_RESOLVERS = {
    "_persona_by_id(cfg, persona_id)",
    "_cli_create_persona(persona_id)",
}

#: The caller set, as of RD-H6. ``(file, enclosing function, resolver)``.
#:
#: ``absent`` means the call passes NO persona, so ``_persona_is_unknown`` does
#: the strict roster read itself — the RPC lane, and the strongest of the three
#: shapes. ``param:<name>`` means the enclosing function FORWARDS its own
#: parameter, so the contract on it is the contract on ITS callers, which are
#: themselves rows here.
PERSONA_ARGUMENT_CONTRACT = {
    ("agent_runtime/agent_create.py", "normalize_agent_create", "param:persona"),
    ("agent_runtime/agent_create.py", "perform_agent_create", "param:persona"),
    ("agent_runtime/agent_create.py", "require_known_persona", "param:persona"),
    ("agent_runtime/serve_rpc.py", "_runtime_agent_create", "absent"),
    (
        "hermes_cli/harness_parts/persona_commands.py",
        "_cmd_agent_create",
        "_cli_create_persona(persona_id)",
    ),
    (
        "hermes_cli/harness_parts/persona_commands.py",
        "_cmd_persona_instance_create",
        "_persona_by_id(cfg, persona_id)",
    ),
    (
        "hermes_cli/harness_parts/persona_commands.py",
        "_cmd_persona_instance_open_chat",
        "_persona_by_id(cfg, persona_id)",
    ),
}

SCANNED_PACKAGES = ("agent_runtime", "hermes_cli")


# ── half 1: the bypass is real ───────────────────────────────────────────────


def test_any_non_none_persona_short_circuits_the_roster_check(monkeypatch):
    """The invariant, stated positively. This is the thing being fenced.

    ANTI-VACUITY: the roster is patched EMPTY, so ``persona_not_found`` is the
    answer for every id — and the synthesised object still walks through. A test
    that seeded the persona would pass whether the bypass existed or not.
    """

    from agent_runtime.models import AgentPersona

    monkeypatch.setattr(agent_create, "persona_roster", lambda: [])

    synthesized = AgentPersona(
        id="not_in_any_roster",
        display_name="Fabricated",
        role="qa",
        model=None,
        provider=None,
        api_mode=None,
        toolsets=[],
        system_prompt_path="",
    )

    # The bare id is refused, and the SAME id carrying an object is not.
    assert agent_create._persona_is_unknown("not_in_any_roster") is True
    assert agent_create._persona_is_unknown("not_in_any_roster", synthesized) is False
    # It is the object's presence, not its contents: anything non-``None`` wins.
    assert agent_create._persona_is_unknown("not_in_any_roster", object()) is False

    # And the wrapper inherits it all the way to the refusal envelope, which is
    # what makes the bypass reach a durable write rather than stopping at a
    # private helper.
    assert agent_create.require_known_persona("not_in_any_roster", synthesized) is None
    assert (
        agent_create.require_known_persona("not_in_any_roster")["reason"]
        == agent_create.PERSONA_NOT_FOUND_REASON
    )


def test_the_bypass_is_the_documented_first_statement_not_an_accident():
    """Read off the parser: the ``persona is not None`` short-circuit is the
    FIRST thing the resolver does, before any roster read.

    Order is the property. A mutant that moved the roster read above it would
    make every other test in this file pass while turning the D-U1 carve-out
    (``profile:`` ids own no persona row) into a refusal for the launcher's
    template browser — and it would ALSO turn a roster fault into a raise on a
    path that had already been answered.
    """

    source = Path(agent_create.__file__).read_text(encoding="utf-8")
    func = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == "_persona_is_unknown"
    )
    body = [stmt for stmt in func.body if not isinstance(stmt, ast.Expr)]
    assert ast.unparse(body[0]) == "if persona is not None:\n    return False"
    # The roster read exists, and it is BELOW the short-circuit.
    roster_lines = [
        node.lineno
        for node in ast.walk(func)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "persona_roster"
    ]
    assert roster_lines and min(roster_lines) > body[0].lineno


# ── half 2: the caller set, by enumeration ───────────────────────────────────


def _owners(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    owners: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                owners.setdefault(child, node)
    return owners


def _persona_argument(call: ast.Call, keyword: str, position: int | None) -> ast.AST | None:
    for kw in call.keywords:
        if kw.arg == keyword:
            return kw.value
    if position is not None and len(call.args) > position:
        return call.args[position]
    return None


def _resolver_for(argument: ast.AST | None, owner: ast.AST | None) -> str:
    """The RESOLVER behind a call's persona argument, not the variable name.

    A bare name is followed to its binding inside the enclosing function: a
    parameter is reported as a forward (``param:<name>``), a local assignment as
    the right-hand side that produced it. Anything else is reported as its own
    source text, which is what makes an inline synthesis loud.
    """

    if argument is None:
        return "absent"
    if isinstance(argument, ast.Constant) and argument.value is None:
        return "absent"
    if isinstance(argument, ast.Name) and owner is not None:
        params = {arg.arg for arg in owner.args.args + owner.args.kwonlyargs}
        assigned = [
            ast.unparse(stmt.value)
            for stmt in ast.walk(owner)
            if isinstance(stmt, ast.Assign)
            for target in stmt.targets
            if isinstance(target, ast.Name) and target.id == argument.id
        ]
        if assigned:
            # A name bound more than once is reported as every binding, joined —
            # so a second, weaker assignment cannot hide behind the first.
            return " | ".join(sorted(set(assigned)))
        if argument.id in params:
            return f"param:{argument.id}"
    return ast.unparse(argument)


def _enumerate_persona_callers() -> set[tuple[str, str, str]]:
    found: set[tuple[str, str, str]] = set()
    for package in SCANNED_PACKAGES:
        for path in sorted((REPO_ROOT / package).rglob("*.py")):
            tree = _tree_index.parsed(str(path))
            owners = _owners(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                spec = PERSONA_TAKING_ENTRY_POINTS.get(name)
                if spec is None:
                    continue
                owner = owners.get(node)
                found.add(
                    (
                        path.relative_to(REPO_ROOT).as_posix(),
                        owner.name if owner is not None else "<module>",
                        _resolver_for(_persona_argument(node, *spec), owner),
                    )
                )
    return found


def test_every_persona_carrying_call_site_is_declared():
    found = _enumerate_persona_callers()

    assert found == PERSONA_ARGUMENT_CONTRACT, (
        "the set of call sites that hand a persona object across the roster "
        "check changed.\n"
        f"  found:    {sorted(found)}\n"
        f"  declared: {sorted(PERSONA_ARGUMENT_CONTRACT)}\n"
        "``_persona_is_unknown`` returns False for ANY non-None persona, so a "
        "call site added here bypasses UC-H2's roster refusal — the only check "
        "that runs before a durable roster row, chat root and placement are "
        "minted. If the new caller resolves through the strict path, add its row "
        "AND its resolver to STRICT_RESOLVERS in this file, deliberately."
    )


@pytest.mark.parametrize(
    "site", sorted(PERSONA_ARGUMENT_CONTRACT), ids=lambda site: f"{site[1]}"
)
def test_each_declared_resolver_is_a_strict_one(site):
    """The declared table is not self-certifying: each resolver must be strict.

    Without this leg the enumeration above could be kept green by editing the
    table — the row would change, the test would pass, and a synthesised persona
    would be in production with a green suite. Here the resolver itself has to be
    on the allowlist, so widening it is a separate, visible edit.
    """

    _, function, resolver = site
    if resolver == "absent" or resolver.startswith("param:"):
        # No object, or a forward whose own callers are rows in the same table.
        return
    assert resolver in STRICT_RESOLVERS, (
        f"{function} resolves its persona with {resolver!r}, which is not a "
        "strict roster resolver. ``_persona_is_unknown`` will return False for "
        "whatever it produced, so a synthesised or client-supplied object walks "
        "straight past the roster refusal into a durable write."
    )


def test_the_forwards_terminate_at_a_real_caller():
    """A ``param:`` row is only honest if something eventually resolves.

    Three of the seven rows are forwards inside ``agent_create`` itself. If the
    table were ALL forwards, the second leg above would pass vacuously — every
    row exempt, nothing checked. Assert the table contains at least one real
    resolver and at least one ``absent``, so the exemption can never be the
    whole story.
    """

    resolvers = {resolver for _, _, resolver in PERSONA_ARGUMENT_CONTRACT}
    assert "absent" in resolvers
    assert resolvers & STRICT_RESOLVERS
    # And every declared strict resolver is actually USED — a stale allowlist
    # entry is a widening nobody is paying for.
    assert STRICT_RESOLVERS <= resolvers
