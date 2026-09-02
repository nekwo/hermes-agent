"""The parent-binding half of this directory's sys.modules identity guard.

Two contracts, and the second one is a performance contract with teeth: the
walk this class replaced cost 29.9 ms per test at the aged tail of a full
``tests/hermes_cli`` run (~65% of the measured aging floor). Both are asserted
by running the class, never by reading its source.
"""

from __future__ import annotations

import sys
import types

import pytest

from tests.hermes_cli._module_identity import ParentBindingRepair


def _package(name: str) -> types.ModuleType:
    pkg = types.ModuleType(name)
    pkg.__path__ = []  # a package, not a plain module
    return pkg


@pytest.fixture
def repair() -> ParentBindingRepair:
    return ParentBindingRepair()


def test_a_child_its_parent_never_bound_is_repaired(repair):
    """The pollution class the guard exists for: a hand-rolled loader's child."""
    parent = _package("fakepkg")
    child = types.ModuleType("fakepkg.honcho")
    modules = {"fakepkg": parent, "fakepkg.honcho": child}

    assert repair.run(modules) == 1
    assert vars(parent)["honcho"] is child


def test_the_probe_never_fires_the_parents_module_getattr(repair):
    """``vars()``, not ``hasattr`` -- the 24 ms half of the aging cost.

    ``hasattr`` on a package fires PEP 562 module ``__getattr__`` and whatever
    lazy-import machinery sits behind it. The verdict is the same; the bill is
    not.
    """
    calls: list[str] = []
    parent = _package("lazypkg")

    def _lazy(attr: str):
        calls.append(attr)
        raise AttributeError(attr)

    parent.__getattr__ = _lazy
    child = types.ModuleType("lazypkg.sub")
    modules = {"lazypkg": parent, "lazypkg.sub": child}

    assert repair.run(modules) == 1
    assert vars(parent)["sub"] is child
    assert calls == []


def test_an_existing_binding_is_never_overwritten(repair):
    """The guard ADDS the binding a real import would have made. Nothing else."""
    parent = _package("fakepkg")
    real_child = types.ModuleType("fakepkg.sub")
    decoy = object()
    parent.sub = decoy
    modules = {"fakepkg": parent, "fakepkg.sub": real_child}

    repair.run(modules)
    assert vars(parent)["sub"] is decoy


def test_an_unchanged_module_count_skips_the_walk_entirely(repair):
    """The 5 ms half: nothing imported means nothing to repair.

    Sabotaging the binding WITHOUT touching ``len(modules)`` is not a case the
    mapping can reach on its own -- binding a child changes no row -- so the
    skip is asserted here by doing the impossible thing on purpose.
    """
    parent = _package("fakepkg")
    child = types.ModuleType("fakepkg.sub")
    modules = {"fakepkg": parent, "fakepkg.sub": child}
    assert repair.run(modules) == 1

    del vars(parent)["sub"]
    assert repair.run(modules) == 0
    assert "sub" not in vars(parent)


def test_a_grown_mapping_re_arms_the_walk(repair):
    """Any import re-arms it, and only the not-yet-bound names are examined."""
    parent = _package("fakepkg")
    first = types.ModuleType("fakepkg.one")
    modules = {"fakepkg": parent, "fakepkg.one": first}
    assert repair.run(modules) == 1

    second = types.ModuleType("fakepkg.two")
    modules["fakepkg.two"] = second
    assert repair.run(modules) == 1  # only the new name, not both
    assert vars(parent)["two"] is second


def test_a_child_whose_parent_is_not_imported_yet_is_reconsidered(repair):
    """The name must NOT be remembered as done while its parent is absent."""
    child = types.ModuleType("fakepkg.sub")
    modules = {"fakepkg.sub": child}
    assert repair.run(modules) == 0

    parent = _package("fakepkg")
    modules["fakepkg"] = parent
    assert repair.run(modules) == 1
    assert vars(parent)["sub"] is child


def test_a_non_string_key_is_stepped_over(repair):
    """A mocked ``spec.name`` lands a MagicMock key in sys.modules."""

    class _NotAName:
        pass

    parent = _package("fakepkg")
    child = types.ModuleType("fakepkg.sub")
    modules = {_NotAName(): object(), "fakepkg": parent, "fakepkg.sub": child}

    assert repair.run(modules) == 1
    assert vars(parent)["sub"] is child


def test_the_autouse_guard_leaves_this_session_fully_bound():
    """End to end: the real fixture ran before this test, on real sys.modules.

    Every dotted module whose parent package is itself imported must carry its
    binding in the parent's ``__dict__`` -- the state ``importlib`` would have
    left, and the state ``monkeypatch.setattr("a.b.c", ...)`` needs to resolve.
    """
    unbound = []
    for name, module in list(sys.modules.items()):
        if not isinstance(name, str) or "." not in name:
            continue
        if not isinstance(module, types.ModuleType):
            continue
        parent_name, _, child = name.rpartition(".")
        parent = sys.modules.get(parent_name)
        if not isinstance(parent, types.ModuleType):
            continue
        if child not in vars(parent):
            unbound.append(name)
    assert unbound == []
