"""S29 removed ``context_requests.fulfilled_context_bundles``; S45 removed the module.

RETARGETED 2026-07-31. This file is kept, not deleted, because it is the record
of a prediction that came true — and of the deferral that made it take two waves.

**What S29 did.** ``fulfilled_context_bundles``' ONE consumer was
``context_builder.build_context``, which packed the last three fulfilled bundles
into ``AgentContext.context_bundles`` for a tick. S27 (``539bf5813``) removed
that builder; the reader kept returning a list nobody asked for, so S29 cut it.

**What S29 recorded but did not act on**, verbatim from the original docstring:

    CONTRADICTION WORTH RECORDING, because a future pass will hit it: the
    premise that "the context-request store/projection is live" does not survive
    contact with the current tree. NOTHING in production imports
    ``agent_runtime.context_requests`` at all — not ``agent_runtime``, not
    ``hermes_cli``, not ``tools``, not ``agent``, not ``scripts``. The three
    public functions [...] are reached only from ``test_context_requests.py``.

S29 then kept that surface anyway and reported it for an operator ruling, on the
explicit grounds that "whole-module removal is a bigger call than the one this
sweep was scoped to". Deferring was correct; what it cost is worth naming, since
this wave hit the same shape twice: a module that is *reported* dead but left
importable reads as live to the next reachability pass, and this one sat that way
for two more waves.

**What S45 did.** The operator ruled CUT on 2026-07-31 (deferred-debt ledger
item 2). The module and ``test_context_requests.py`` — its only importer, and the
other half of the closed loop — were deleted together.

**One premise moved between the two waves, which is why S45 re-derived instead of
inheriting.** S29 named ``observability.py`` as the nearest-miss, doing
``getattr(task, "context_requests", [])`` on a duck-typed row: an ATTRIBUTE read,
never an import. That read is *also* gone now — it went with S28's removal of
``build_observability``'s ``tasks`` parameter (``026bc7b30``). At the time of the
cut the string ``context_requests`` appeared in exactly one production file, the
one being deleted.

The absence assertions below replace the liveness assertions this file used to
make. Deleting the file instead would have erased the prediction along with its
subject.
"""

from __future__ import annotations

import pkgutil
from importlib.util import find_spec

import agent_runtime


#: The public surface this file used to pin as KEEP, awaiting a ruling.
S29_KEPT_PENDING_RULING = (
    "add_context_request",
    "has_unresolved_context_request",
)

#: The private helpers it pinned alongside them.
S29_KEPT_PRIVATE_HELPERS = (
    "_fulfill_request",
    "_allowed_roots",
    "_resolve_allowed_path",
    "_mask_secret_lines",
)


def test_the_bundle_reader_is_gone():
    """S29's own cut, now subsumed: the whole module went at S45."""

    assert find_spec("agent_runtime.context_requests") is None
    assert not hasattr(agent_runtime, "context_requests")


def test_the_surface_s29_deferred_went_with_the_module():
    """The KEEP was explicitly provisional — "equally consumer-free today [...]
    but removing them is a whole-module decision, not this cut". S45 was that
    decision, so every deferred name went, not just the one S29 could reach."""

    import importlib

    import pytest

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("agent_runtime.context_requests")

    for name in S29_KEPT_PENDING_RULING + S29_KEPT_PRIVATE_HELPERS:
        assert not hasattr(agent_runtime, name), name


def test_no_module_in_the_package_still_defines_or_calls_the_reader():
    """Text gate, path-scoped to the package — never a bare word.

    Gates on the CODE forms (definition, call, import), not on any mention: the
    removal rationale is recorded in prose above and in the S45 contract test,
    and naming the retired function there is the point. Widened at S45 from the
    single reader to the module import itself."""

    offenders = []
    for module_info in pkgutil.iter_modules(agent_runtime.__path__):
        path = f"{agent_runtime.__path__[0]}/{module_info.name}.py"
        try:
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
        except OSError:
            continue
        for form in (
            "def fulfilled_context_bundles",
            "fulfilled_context_bundles(",
            "import fulfilled_context_bundles",
            "from .context_requests import",
            "from agent_runtime.context_requests import",
        ):
            if form in source:
                offenders.append(f"{module_info.name}:{form}")
    assert offenders == []


def test_the_nearest_miss_s29_named_is_itself_retired():
    """S29's named false positive was ``observability``'s duck-typed attribute
    read. S28 removed the ``tasks`` parameter that fed it, so the module is live
    and no longer mentions the name at all — re-derived at S45, not inherited."""

    import inspect

    from agent_runtime import observability

    assert callable(observability.build_observability)
    assert "context_requests" not in inspect.getsource(observability)
