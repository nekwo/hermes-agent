"""The memory-provider loader must finish an import, not half of one.

``plugins/memory/__init__.py`` discovers providers by hand —
``spec_from_file_location`` + ``sys.modules[full_name] = mod`` — and until
2026-09-02 stopped one step short of what real import machinery does: binding
the child module on its PARENT package object. The two spellings of the same
import then answer differently, permanently:

* ``import plugins.memory.honcho`` / ``importlib.import_module(...)`` read
  ``sys.modules`` and succeed — including on every later call, because
  ``import_module`` short-circuits on the row it finds and never repairs the
  missing attribute;
* ``from plugins.memory import honcho``, and every attribute walk built on it —
  ``getattr``, ``unittest.mock.patch("plugins.memory.honcho.client…")``,
  pytest's ``monkeypatch.setattr("<dotted>")`` resolver — raise
  ``AttributeError: 'module' object at plugins.memory has no attribute
  'honcho'``.

So ``memory.<name>`` resolved or failed depending on which spelling ran first.
That is a PRODUCT shape, not a test shape: a plugin doing ``from plugins.memory
import <sibling>``, or any caller patching into a live provider, hits it exactly
as a test does. ``tests/hermes_cli/conftest.py`` carries a setup-half repair
loop for the same defect; that loop makes the SUITE order-independent and is not
what makes the product correct.

Every test here builds a throwaway provider on disk and drives the real loader.
Nothing reads a shipped provider's source, and nothing asserts about the set of
providers this repo happens to bundle.
"""

from __future__ import annotations

import importlib
import sys

import pytest

import plugins.memory as memory

PROBE = "loader_identity_probe"


def _write_provider(root, name: str = PROBE, *, init_body: str | None = None):
    """A minimal provider directory with ONE submodule.

    The submodule is not decoration: it is the second binding site (the loader
    pre-registers every ``*.py`` beside ``__init__.py`` so relative imports
    resolve), and it is the one a plugin's own ``from .store import …`` runs
    through.
    """

    directory = root / name
    directory.mkdir(parents=True)
    (directory / "store.py").write_text("VALUE = 42\n", encoding="utf-8")
    (directory / "__init__.py").write_text(
        init_body
        if init_body is not None
        # ``MemoryProvider`` in the source is what ``_is_memory_provider_dir``
        # looks for; the relative import is what proves the pre-registered
        # submodule is reachable the way a real plugin reaches it.
        else "from .store import VALUE\n\nMemoryProvider = None\nLOADED = VALUE\n",
        encoding="utf-8",
    )
    return directory


@pytest.fixture
def loader_sandbox(tmp_path, monkeypatch):
    """Point the loader's BUNDLED root at a temp dir and undo every module row.

    Treating the temp dir as the bundled root is what puts the probe on the
    ``plugins.memory.<name>`` namespace — the exact namespace the field failure
    was reported on — without writing a directory into the repo's real provider
    root.
    """

    monkeypatch.setattr(memory, "_MEMORY_PLUGINS_DIR", tmp_path)
    before = dict(sys.modules)
    parents = [sys.modules.get("plugins.memory"), sys.modules.get(memory._USER_NAMESPACE)]
    try:
        yield tmp_path
    finally:
        for name in [n for n in sys.modules if n not in before]:
            sys.modules.pop(name, None)
        for parent in parents:
            if parent is not None:
                for attr in (PROBE, f"{PROBE}_broken"):
                    if hasattr(parent, attr):
                        delattr(parent, attr)


def test_loaded_provider_is_bound_on_its_parent_package(loader_sandbox):
    """RED-FIRST: ``from plugins.memory import <name>`` raised ``AttributeError``
    after a successful load, while ``import_module`` of the same name succeeded.
    """

    directory = _write_provider(loader_sandbox)

    memory._load_provider_from_dir(directory)

    full = f"plugins.memory.{PROBE}"
    assert full in sys.modules
    # The half that was missing. ``is``, not truthiness: two module objects for
    # one name is the same defect wearing a different mask.
    assert getattr(sys.modules["plugins.memory"], PROBE) is sys.modules[full]
    # And therefore the spelling that used to raise now resolves — driven
    # through the real ``IMPORT_FROM`` machinery rather than a getattr this test
    # performed itself, because a getattr is the thing under test.
    namespace: dict = {}
    exec(f"from plugins.memory import {PROBE} as loaded", namespace)  # noqa: S102
    assert namespace["loaded"] is sys.modules[full]
    assert namespace["loaded"].LOADED == 42
    # The other spelling still answers, and answers with the SAME object.
    assert importlib.import_module(full) is sys.modules[full]


def test_submodules_are_bound_on_the_provider_package_too(loader_sandbox):
    """The loader pre-registers every sibling ``*.py`` so ``from .store import …``
    resolves. Same one-step-short bug, one level down: ``provider.store`` was a
    ``sys.modules`` row with no attribute behind it, so
    ``patch("plugins.memory.<name>.store.VALUE")`` could not address it."""

    directory = _write_provider(loader_sandbox)

    memory._load_provider_from_dir(directory)

    provider = sys.modules[f"plugins.memory.{PROBE}"]
    assert getattr(provider, "store") is sys.modules[f"plugins.memory.{PROBE}.store"]
    assert provider.store.VALUE == 42


def test_a_provider_that_fails_to_exec_leaves_neither_half_behind(loader_sandbox):
    """The rollback has to be symmetric.

    ``exec_module`` failing used to pop the ``sys.modules`` row and leave
    nothing; binding the parent attribute without undoing it would trade one
    two-answers-for-one-name defect for another, aimed at the failure path.
    """

    name = f"{PROBE}_broken"
    directory = _write_provider(
        loader_sandbox,
        name,
        init_body="MemoryProvider = None\nraise RuntimeError('provider is broken')\n",
    )

    assert memory._load_provider_from_dir(directory) is None

    assert f"plugins.memory.{name}" not in sys.modules
    assert not hasattr(sys.modules["plugins.memory"], name)


def test_a_user_installed_provider_binds_on_its_synthetic_parent(tmp_path):
    """The user namespace has the same two halves.

    ``_hermes_user_memory`` exists nowhere on disk — the loader mints it as a
    package shell — so a child registered in ``sys.modules`` and not bound on it
    is unreachable by ``from _hermes_user_memory import <name>`` exactly as the
    bundled case was.
    """

    # NOT under ``_MEMORY_PLUGINS_DIR``, which is all ``_is_bundled`` asks — so
    # this lands on the synthetic ``_hermes_user_memory`` namespace.
    directory = _write_provider(tmp_path / "user_plugins")
    before = dict(sys.modules)
    try:
        memory._load_provider_from_dir(directory)

        namespace = sys.modules[memory._USER_NAMESPACE]
        full = f"{memory._USER_NAMESPACE}.{PROBE}"
        assert getattr(namespace, PROBE) is sys.modules[full]
        assert getattr(sys.modules[full], "store") is sys.modules[f"{full}.store"]
    finally:
        for name in [n for n in sys.modules if n not in before]:
            sys.modules.pop(name, None)
        sys.modules.pop(memory._USER_NAMESPACE, None)
