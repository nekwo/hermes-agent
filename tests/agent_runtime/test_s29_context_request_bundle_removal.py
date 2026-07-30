"""S29 removes ``context_requests.fulfilled_context_bundles``.

Its ONE consumer was ``context_builder.build_context``, which packed the last
three fulfilled bundles into ``AgentContext.context_bundles`` for a tick.
S27 (``539bf5813``) removed that builder; the reader kept returning a list
nobody asked for. It has no private helpers of its own, so nothing goes with it.

CONTRADICTION WORTH RECORDING, because a future pass will hit it: the premise
that "the context-request store/projection is live" does not survive contact
with the current tree. NOTHING in production imports
``agent_runtime.context_requests`` at all — not ``agent_runtime``, not
``hermes_cli``, not ``tools``, not ``agent``, not ``scripts``. The three public
functions (``add_context_request``, ``has_unresolved_context_request``, and the
one removed here) are reached only from ``test_context_requests.py``. The
nearest thing to a live reader is ``observability.py``, which does
``getattr(task, "context_requests", [])`` on a duck-typed object — it reads the
ATTRIBUTE, never this module. The remaining surface is therefore kept in place
and reported for an operator ruling rather than swept on this agent's own
initiative: whole-module removal is a bigger call than the one this sweep was
scoped to, and ``observability.py`` is owned by a concurrent session.

What IS proven here is narrower and sufficient for the cut: the removed function
had a real consumer, that consumer is gone, and no other one appeared.
"""

from __future__ import annotations

import inspect
import pkgutil

import agent_runtime
from agent_runtime import context_requests


def test_the_bundle_reader_is_gone():
    assert not hasattr(context_requests, "fulfilled_context_bundles")


def test_no_module_in_the_package_still_defines_or_calls_the_reader():
    """Text gate, path-scoped to the package — never a bare word.

    Gates on the CODE forms (definition, call, import), not on any mention: the
    removal rationale is recorded in ``context_requests``' own comment and naming
    the retired function there is the point."""

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
        ):
            if form in source:
                offenders.append(f"{module_info.name}:{form}")
    assert offenders == []


def test_the_request_lifecycle_surface_is_untouched():
    """KEEP: the create/fulfil/dedupe path and the unresolved predicate. They are
    equally consumer-free today (see the module docstring) but are the module's
    reason to exist, and removing them is a whole-module decision, not this
    cut."""

    assert callable(context_requests.add_context_request)
    assert callable(context_requests.has_unresolved_context_request)
    for name in ("_fulfill_request", "_allowed_roots", "_resolve_allowed_path", "_mask_secret_lines"):
        assert callable(getattr(context_requests, name)), name


def test_the_bundle_is_still_written_onto_the_request():
    """Negative gate: this cut removes a READER, not the projection. The bundle
    the reader used to collect is still built by ``_fulfill_request`` and still
    lands on the request row — which is where the surviving test asserts it."""

    source = inspect.getsource(context_requests._fulfill_request)
    assert 'req["bundle"] = {' in source
    assert '"bundle_id": bundle_id' in source
