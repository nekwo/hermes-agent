"""S45 removes the four whole modules whose only importers were their own tests.

Ledger item 2 (docs/agent-runtime-harness/archive/2026-08-22-pre-consolidation/19-deferred-debt-ledger.md) asked for a
ruling on whether test-only-anchored counts as KEEP. The operator ruled CUT on
2026-07-31. The rule this settles, stated once so future waves do not re-derive
it: **a module whose entire importer set is the test file written to exercise it
is not covered code — it is a closed loop.** Deleting the module and its test in
the same commit removes exactly the loop and nothing else.

Each cut was re-verified with a whole-repo search immediately before it, not
inherited from the audit's list:

===================== ============================================ ============
module                surviving importers before the cut           lines
===================== ============================================ ============
budget_approval       tests/agent_runtime/test_budget_approval.py    90
context_requests      tests/agent_runtime/test_context_requests.py  371
role_contracts        tests/agent_runtime/test_stage53_contracts.py 184
stage_intent          tests/agent_runtime/test_stage_intent.py      257
===================== ============================================ ============

Zero production importers in ``agent_runtime``, ``hermes_cli``, ``tools``,
``agent``, or ``scripts`` for any of the four. Three were already partly swept:
S43 cut ``budget_approval.eligible_budget_approval_incidents`` and five
``stage_intent`` symbols; S29 cut ``context_requests.fulfilled_context_bundles``
and recorded the contradiction verbatim — *"NOTHING in production imports
``agent_runtime.context_requests`` at all"* — while explicitly deferring the
whole-module call to an operator. This is that call, executed.

**S29's nearest-miss no longer even exists** — re-checked rather than inherited,
and the answer moved. S29 recorded that *"the nearest thing to a live reader is
``observability.py``, which does ``getattr(task, "context_requests", [])`` on a
duck-typed object"*. That read went with S28's removal of
``build_observability``'s ``tasks`` parameter (``026bc7b30``). As of this cut the
string ``context_requests`` appears in exactly ONE production file — the module
being deleted — so even the attribute-shaped false positive is gone.

Every S-witness that named these modules is RETARGETED to assert absence, never
deleted — the s41-s43 precedent. A witness that quietly loses its subject hides
the reversal; a witness that asserts the subject is gone records it.

=============================================================================
MIGRATED to ``tests/agent_runtime/test_tombstone_registry.py`` (2026-08-01)
=============================================================================

The two absence forms this file asserted are now registry rows:

* the four modules — ``Form.MODULE`` rows (``find_spec`` is ``None``). Both the
  ``find_spec`` pin AND the ``pytest.raises(ModuleNotFoundError)`` pin went:
  they are one fact stated twice, since a module with no spec cannot be
  imported.
* the four closed-loop test files that went with their subjects, all four
  **deleted** — ``Form.PATH`` rows (``tests/agent_runtime/test_budget_approval.py``,
  ``test_context_requests.py``, ``test_stage_intent.py``,
  ``test_stage53_contracts.py``). ``DELETED_TEST_FILES`` existed only to feed
  that pin and went with it.

``REMOVED_MODULES`` STAYS, because one test still needs it and that test is NOT
a registry row. ``test_no_surviving_module_imports_any_of_the_four`` gates the
IMPORT-STATEMENT forms (``from .x import`` / ``from agent_runtime.x import`` /
``import x``) rather than a bare word, and the registry deliberately carries no
``Form.CODE`` row for ``budget_approval`` / ``context_requests`` /
``role_contracts`` / ``stage_intent``. Banning those four bare names repo-wide
would be a different, wider claim than "no surviving module imports them", and
this file makes the narrow one.

WHY THE OTHER SURVIVORS STAYED: ``test_the_lookalike_keep_set_survives`` is a
KEEP pin over the live neighbours one bare-word grep away from the cut (plus a
``inspect.getsource`` characterization that ``observability`` no longer even
mentions ``context_requests``, which the registry's docstring-stripping scanner
cannot state); ``test_the_package_still_imports_end_to_end`` is a negative gate
that four module deletions did not strand a package import.
"""

from __future__ import annotations


REMOVED_MODULES = (
    "agent_runtime.budget_approval",
    "agent_runtime.context_requests",
    "agent_runtime.role_contracts",
    "agent_runtime.stage_intent",
)


def test_no_surviving_module_imports_any_of_the_four():
    """Text gate, path-scoped to the package — never a bare word. Gates on the
    CODE forms (import statement), not on any mention: several witnesses name
    these modules in prose and that is the point of a witness."""

    import pkgutil

    import agent_runtime

    names = tuple(dotted.rsplit(".", 1)[1] for dotted in REMOVED_MODULES)
    offenders = []
    for module_info in pkgutil.iter_modules(agent_runtime.__path__):
        path = f"{agent_runtime.__path__[0]}/{module_info.name}.py"
        try:
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
        except OSError:
            continue
        for name in names:
            for form in (f"from .{name} import", f"from agent_runtime.{name} import", f"import {name}\n"):
                if form in source:
                    offenders.append(f"{module_info.name}:{form.strip()}")
    assert offenders == []


def test_the_lookalike_keep_set_survives():
    """Names one bare-word grep away from the four — each still has a real
    production caller, and every one was checked, not assumed."""

    import importlib

    # ``observability`` was S29's named nearest-miss. Its ``context_requests``
    # attribute read went with S28's ``tasks`` parameter, so the module is live
    # and no longer mentions the name at all — the false positive is retired,
    # not merely explained.
    import inspect

    from agent_runtime import observability

    assert callable(observability.build_observability)
    assert "context_requests" not in inspect.getsource(observability)

    # The shared registry survives as the event-contract authority; the
    # structured decision/scope companions retired at S64.
    for dotted in (
        "agent_runtime.decision_contract_registry",
        "agent_runtime.incidents",
    ):
        assert importlib.import_module(dotted) is not None
    for dotted in (
        "agent_runtime.decision_contracts",
        "agent_runtime.decision_schema",
        "agent_runtime.scope_control",
        "agent_runtime.simplified_contract",
    ):
        assert importlib.util.find_spec(dotted) is None

    # ``states`` keeps the four live enums S23 pinned when ``StageStatus`` went;
    # ``stage_intent`` was its last named consumer and outlived it by two waves.
    from agent_runtime import states

    assert states.RunState.WAITING_ON_APPROVAL == "waiting_on_approval"
    assert not hasattr(states, "StageStatus")


def test_the_package_still_imports_end_to_end():
    """Negative gate: four module deletions must not strand a package import."""

    import importlib

    import agent_runtime

    importlib.reload(agent_runtime)
    for dotted in (
        "agent_runtime.snapshot",
        "agent_runtime.status",
        "agent_runtime.observability",
        "agent_runtime.checkpoint",
        "agent_runtime.projector",
    ):
        assert importlib.import_module(dotted) is not None
