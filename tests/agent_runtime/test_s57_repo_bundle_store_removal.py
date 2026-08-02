"""S57 deletes ``RepoBundleStore`` WHOLE -- module, model, path helper, wire
count, serve fingerprint entry.

Operator-ruled CUT on 2026-08-01. This is the third and final pass over one
lane, and the sequence is the point:

* **S52** deleted the WRITE side (10 mutators, 2 lock mutators, 7 contracts) and
  explicitly declared "this is a write-lane cut, NOT a store removal", because
  ``status.py`` still projected operator bundle rows off ``list_all``.
* **S56** deleted those four status projections (``repo_bundles`` /
  ``repo_bundle_closeout`` / ``bundle_queue`` / ``repo_locks``) -- each empty by
  construction once the writers went -- and retired the checkpoint EntityClass
  row with them. That left the store with **zero production importers**, which
  doc 19 recorded as newly-opened debt rather than sweeping in the same wave.
* **S57** (here) takes the module. Re-verified before the cut: a repo-wide scan
  of ``agent_runtime`` / ``hermes_cli`` / ``tools`` finds ``RepoBundleStore``
  named ONLY in comments recording its own retirement, and ``RepoBundle`` named
  only inside ``repo_bundles.py`` itself.

**A store nobody imports is not a seam; it is a shape.** Keeping the read side
after its last customer left is the same defect S52 named on the write side --
"a store that can only be written by its own tests is a closed loop, not covered
code" -- with the verbs reversed.

**What went WITH it, and why each is a binding rather than a bonus.**

* ``models.RepoBundle`` (31 fields). Constructed and decoded nowhere else.
* ``paths.repo_bundle_path``. Its only caller was ``RepoBundleStore.get``.
* ``migration_status()``'s ``counts.repo_bundles`` row -- see below.
* ``serve``'s ``_FINGERPRINT_STORE_DIRS`` entry: a per-poll ``stat`` of a
  directory no code path can write. S56 removed ``worker_sessions`` from the
  same tuple for the same reason.

**The migration count, handled honestly rather than quietly.** It did NOT read
through the store -- it was a direct ``_count_nested_json(root / "repo_bundles")``
filesystem read, so a shallow "does it call the store?" check would have left it
standing. But it counts a class the runtime can no longer name, the last writer
went at S52, and the live root has no ``repo_bundles/`` directory at all, so the
row reported ``0`` by construction on the wire an operator reads. It is RETIRED,
not re-homed: there is nothing left to re-home it onto.

**What deliberately did NOT go**, each named so absence is not read as oversight:

* The promotion-record reader and its two directory helpers were a separate
  ruled lane. Round 2 removed them after receiver-aware re-verification.
* ``PersonaAssignment.repo_bundle_id`` and its ``assignment_signal_hash``
  component -- a live identity field of a DIFFERENT store. Its identity test was
  re-homed into ``test_persona_assignments.py`` rather than deleted with
  ``test_repo_bundles.py``.
* ``RuntimeInstance.repo_bundle_locks`` and its ``runtime_instances`` projection
  -- the ``runtime_instances`` rows still ship on the status wire (S56 kept that
  checkpoint row deliberately for exactly this reason).

RED-FIRST: written against the pre-cut tree, where ``test_the_module_is_gone``
and ``test_the_model_is_gone`` both fail on a successful import.

-----------------------------------------------------------------------------
MIGRATED TO ``test_tombstone_registry.py`` (2026-08-01)
-----------------------------------------------------------------------------

Three pure-ABSENCE cases left this file — the three the RED-FIRST note above
names, which is exactly the point: they were the mechanical half:

* ``test_the_module_is_gone`` -> ``MODULE`` row ``agent_runtime.repo_bundles``
  AND ``PATH`` row ``agent_runtime/repo_bundles.py`` (this case asserted both;
  the registry has a form for each).
* ``test_the_model_is_gone`` -> ``ATTR`` row ``RepoBundle``
  (``agent_runtime.models``).
* ``test_the_path_helper_is_gone`` -> ``ATTR`` row ``repo_bundle_path``
  (``agent_runtime.paths``).

The registry also carries a repo-wide ``CODE`` row for ``RepoBundleStore``, so
the class name is now banned across seven production packages rather than the
three scanned here.

WHY THE SURVIVORS STAYED:

* ``test_no_production_module_names_the_store_in_code`` — its subject is not the
  NAME, it is the IMPORT FORMS (``from .repo_bundles``,
  ``from agent_runtime.repo_bundles``, ``import repo_bundles``). ``find_spec``
  returning ``None`` says a module cannot be found; it says nothing about a
  production file that still writes the import and would fail at runtime. Same
  reasoning as the S49/S50 import-graph gates. ``test_the_scan_above_is_not_vacuous``
  is that scan's own anti-vacuity proof and stays with it.
* Everything under "The wire" — ``migration_status()["counts"]``, the frame's
  two ``counts`` maps and its contract version, the EXACT surviving key-set, the
  ``serve._FINGERPRINT_STORE_DIRS`` tuple membership (both the removal and the
  four KEEPs), and the ``checkpoint.ENTITY_CLASS_NAMES`` row plus its deliberate
  ``runtime_instances`` counter-example. Produced dicts and exact key sets are
  named in the registry's docstring as deliberately NOT rows.
* The former directory-helper KEEP is deliberately inverted by the Round 2
  tombstone registry after its sole caller was removed.
"""

from __future__ import annotations

import inspect
import pathlib

from agent_runtime import checkpoint, migrations, paths, snapshot
from hermes_cli.harness_parts import serve


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

PRODUCTION_PACKAGES = ("agent_runtime", "hermes_cli", "tools")


def _production_source() -> str:
    chunks = []
    for package in PRODUCTION_PACKAGES:
        for path in (REPO_ROOT / package).rglob("*.py"):
            chunks.append(path.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(chunks)


def test_no_production_module_names_the_store_in_code():
    """A comment recording the retirement is fine and expected; an executable
    reference is not. Checked by declaration/usage form rather than raw text so
    the retirement notes this wave writes do not self-flag."""
    blob = _production_source()
    for form in (
        "import RepoBundleStore",
        "RepoBundleStore(",
        "class RepoBundleStore",
        "from .repo_bundles",
        "from agent_runtime.repo_bundles",
        "import repo_bundles\n",
    ):
        assert form not in blob, form


def test_the_scan_above_is_not_vacuous():
    """The blob really does contain this package's source: a scan that read
    nothing would pass every assertion in the previous case."""
    blob = _production_source()
    assert len(blob) > 500_000
    assert "class PersonaAssignmentStore" in blob


# --------------------------------------------------------------------------
# The wire
# --------------------------------------------------------------------------


def test_the_migration_count_row_is_retired(isolate_agent_runtime_root):
    status = migrations.migration_status()
    assert "repo_bundles" not in status["counts"]


def test_the_frame_no_longer_carries_the_count(isolate_agent_runtime_root):
    frame = snapshot.build_snapshot()
    assert "repo_bundles" not in frame["migration"]["counts"]
    assert "migration" not in frame["runtime_config"]
    assert frame["parity"]["contract_version"] == 51


def test_the_surviving_counts_are_untouched(isolate_agent_runtime_root):
    """Negative gate: this cut takes ONE row off the counts map."""
    counts = migrations.migration_status()["counts"]
    assert set(counts) == {
        "runs",
        "agents",
        "incidents",
        "archive_batches",
    }


def test_the_serve_fingerprint_no_longer_stats_the_tree():
    assert "repo_bundles" not in serve._FINGERPRINT_STORE_DIRS
    # ...and the tuple did not become empty or lose an unrelated store.
    assert {"runs", "agents", "persona_instances", "realms"} <= set(
        serve._FINGERPRINT_STORE_DIRS
    )


def test_the_checkpoint_class_stays_retired():
    """S56 retired the row; S57 removed the writer AND reader that made it
    describable at all. It must not come back."""
    assert "repo_bundles" not in checkpoint.ENTITY_CLASS_NAMES
    # `runtime_instances` is the deliberate counter-example: also writer-less,
    # KEPT because its rows still ship on the status wire.
    assert "runtime_instances" in checkpoint.ENTITY_CLASS_NAMES
