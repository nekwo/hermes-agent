"""The parent-package binding half of this directory's sys.modules guard.

``plugins/memory/__init__.py`` discovers providers by hand -- ``spec_from_file
_location`` + ``sys.modules[full_name] = mod`` -- and never performs the last
step real import machinery does, which is binding the child on its parent
package. So once any test triggers provider discovery,
``sys.modules["plugins.memory.honcho"]`` exists while ``plugins.memory`` has no
``honcho`` attribute, and ``importlib.import_module`` will not repair it: it
short-circuits on the sys.modules row it finds. Every later
``monkeypatch.setattr("plugins.memory.honcho.client...")`` then dies in pytest's
own path resolver with ``'module' object at plugins.memory.honcho has no
attribute 'honcho'`` -- a message that names neither the loader nor the test that
ran it. Measured 2026-08-31: ``test_dashboard_admin_endpoints.py`` alone reds
``test_doctor.py::TestHonchoDoctorConfigDetection``.

This module owns the repair. It used to be an inline loop in ``conftest.py``
that ran, per test, over every entry in ``sys.modules`` and asked
``hasattr(parent, child)``. That was the single largest term in the suite's
measured AGING floor: 0.42 ms/test in a fresh process, **29.9 ms/test** at the
aged tail, ~65% of the growth, as ``sys.modules`` filled from 626 to 14,843
entries during a full ``tests/hermes_cli`` run (field notes
``hermes-suite-perf-field-notes-2026-09-01.md`` section 15). Two mechanisms, in
the proportion the profile measured:

* **~24 ms of it was ``hasattr`` itself**, not the walk. ``hasattr`` on a
  package fires PEP 562 module ``__getattr__`` and, behind it, whatever
  lazy-import machinery a package installs there -- a *failed* import costs
  2.04 ms aged against 0.73 fresh, and the no-op probe tests were stat-ing 120
  files each through ``importlib._path_stat``. :meth:`ParentBindingRepair.run`
  asks ``child in vars(parent)`` instead. That reads the parent's ``__dict__``
  directly, so no ``__getattr__`` can fire, and it is the *right* question
  anyway: the binding a real import makes is a ``__dict__`` entry, and the only
  names we consider are ones whose module object is already in ``sys.modules``,
  so nothing is left to lazily load.
* **~5 ms of it was the walk**, and the walk is nearly always pointless:
  binding a child changes no ``sys.modules`` row, so a test that imports
  nothing new leaves the mapping in exactly the state the last repair already
  fixed. :meth:`run` returns immediately when ``len(modules)`` is unchanged
  since the last pass, and otherwise skips every name it has already bound.

The guarantee is unchanged -- for every ``a.b`` in ``sys.modules`` whose parent
``a`` is also in ``sys.modules``, ``a.b`` is bound on ``a`` -- because the two
skips are only ever taken where the answer cannot have changed:

* A name is remembered as done **only after its parent was resolved and the
  binding confirmed present**. A child whose parent is not imported yet stays
  unmarked, so the pass that follows the parent's import reconsiders it.
* Importing anything changes ``len(sys.modules)``, and that is what re-arms the
  walk. The gap is a test that adds one module and drops another *within the
  same test*, leaving the count equal -- the dropped module is restored by the
  identity guard's own teardown, and the added one is the loader class above,
  which only ever grows the mapping.
"""

from __future__ import annotations

import sys
import types


class ParentBindingRepair:
    """Bind child modules onto their parent packages, at most once per name.

    Stateful on purpose: the state IS the optimisation. One instance per
    process (the ``conftest`` singleton) sees the whole session; tests of this
    class make their own.
    """

    def __init__(self) -> None:
        #: Names whose parent was resolved and whose binding is known present.
        self._bound: set[str] = set()
        #: ``len(modules)`` as of the last completed pass; ``-1`` == never run.
        self._last_count: int = -1

    def reset(self) -> None:
        """Forget every pass, as if this instance had never run."""
        self._bound.clear()
        self._last_count = -1

    def run(self, modules: dict | None = None) -> int:
        """Repair missing parent -> child bindings in *modules*.

        Returns the number of names actually EXAMINED -- i.e. dotted names with
        a resolvable parent that this pass had to look at. ``0`` means the pass
        was skipped or had nothing left to do, and is what a caller asserts on
        to show the walk did not run.
        """
        modules = sys.modules if modules is None else modules
        count = len(modules)
        if count == self._last_count:
            return 0

        examined = 0
        for name in list(modules):
            if not isinstance(name, str) or "." not in name:
                # sys.modules can hold a non-str key: production does
                # ``sys.modules[spec.name] = module`` and a test that mocks
                # ``spec_from_file_location`` hands it a MagicMock whose
                # ``.name`` is another MagicMock (measured:
                # test_setup_openclaw_migration.py). Such a key has no package
                # to repair; policing junk keys is not this guard's question.
                continue
            if name in self._bound:
                continue
            module = modules.get(name)
            if not isinstance(module, types.ModuleType):
                continue
            parent_name, _, child = name.rpartition(".")
            parent = modules.get(parent_name)
            if not isinstance(parent, types.ModuleType):
                # Parent not imported yet. Leave the name UNMARKED so the pass
                # that follows the parent's own import reconsiders it.
                continue
            examined += 1
            try:
                already_bound = child in vars(parent)
            except Exception:
                # A module object without a readable ``__dict__`` is not
                # something this guard can or should repair.
                continue
            if not already_bound:
                try:
                    setattr(parent, child, module)
                except Exception:
                    # Leave it unmarked: a later pass may find the parent
                    # writable. Overwriting nothing is the invariant here --
                    # we only ever ADD the binding a real import would have
                    # made.
                    continue
            self._bound.add(name)

        self._last_count = len(modules)
        return examined


#: The session-wide instance the directory conftest drives.
PARENT_BINDING_REPAIR = ParentBindingRepair()
