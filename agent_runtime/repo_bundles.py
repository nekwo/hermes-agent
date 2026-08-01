from __future__ import annotations

import json

from . import paths
from .events import EventLog
from .models import RepoBundle
from .serde import from_jsonable

# S52 removed the write lane, and with it every binding only a writer read:
# ``hashlib`` / ``dataclasses.replace`` / ``hermes_time.now`` /
# ``utils.atomic_json_write`` / ``models.Event`` / ``serde.to_jsonable``, plus
# the ``REPO_BUNDLE_STATES`` / ``TERMINAL_*`` / ``DELIVERED_*`` /
# ``REPO_LOCK_MODES`` / ``WAKE_DEPENDENCY_DELIVERED`` vocabularies and the
# ``_REPO_OWNER_RULES`` table. A dead import is not free (the S41 rule): it keeps
# a retired symbol reachable by name, so the next reachability pass counts it as
# used. ``EventLog`` stays because the constructor still accepts one.
# S56 removed ``REPO_BUNDLE_DELIVERY_CONTRACT`` / ``REPO_BUNDLE_CHECKOUT_STATUS``
# and the six promotion/closeout label helpers with the four status projections
# that were their only readers.


class RepoBundleStore:
    def __init__(self, event_log: EventLog | None = None):
        self.event_log = event_log or EventLog()

    # S52 (2026-08-01) removed this store's WRITE lane whole:
    # ``create_or_update_from_task``, ``update``, ``attach_assignment``,
    # ``mark_running``, ``mark_verified``, ``mark_rejected``,
    # ``wake_ready_dependencies``, the hollow ``cancel_superseded`` seam
    # (``return []``), and the two private writers ``_write`` / ``_event`` that
    # only they reached. Not one had a production caller: the last one went with
    # the mission/dispatch lane, and what remained was a store that could only
    # ever be written by its own tests. The seven ``repo_bundle.*`` contracts
    # they emitted are de-registered with them. The READ side below is LIVE --
    # ``status.py`` builds every operator bundle row off ``list_all`` -- so this
    # is a write-lane cut, not a store removal. See
    # tests/agent_runtime/test_s52_repo_bundle_write_lane_removal.py.

    def get(self, task_id: str, bundle_id: str) -> RepoBundle:
        raw = json.loads(paths.repo_bundle_path(task_id, bundle_id).read_text(encoding="utf-8"))
        return from_jsonable(RepoBundle, raw)

    def list_for_task(self, task_id: str) -> list[RepoBundle]:
        directory = paths.repo_bundles_task_dir(task_id)
        if not directory.exists():
            return []
        bundles: list[RepoBundle] = []
        for path in sorted(directory.glob("*.json")):
            try:
                bundles.append(from_jsonable(RepoBundle, json.loads(path.read_text(encoding="utf-8"))))
            except Exception:
                continue
        return sorted(bundles, key=lambda item: (item.repo.lower(), item.id))

    def list_all(self) -> list[RepoBundle]:
        root = paths.repo_bundles_dir()
        if not root.exists():
            return []
        bundles: list[RepoBundle] = []
        for path in sorted(root.glob("*/*.json")):
            try:
                bundles.append(from_jsonable(RepoBundle, json.loads(path.read_text(encoding="utf-8"))))
            except Exception:
                continue
        return sorted(bundles, key=lambda item: (item.task_id, item.repo.lower(), item.id))

    def find_for_assignment(self, assignment) -> RepoBundle | None:
        bundle_id = getattr(assignment, "repo_bundle_id", None)
        task_id = getattr(assignment, "task_id", None)
        if not bundle_id or not task_id:
            return None
        try:
            return self.get(str(task_id), str(bundle_id))
        except Exception:
            return None


# S52 also removed the two module-level repo-lock MUTATORS,
# ``acquire_repo_bundle_locks`` / ``release_repo_bundle_locks``, plus the two
# helpers only they reached (``_repo_lock_conflicts``, ``_write_repo_locks``),
# and ``desired_bundles_for_task`` / ``merge_desired_bundle`` / ``qa_waiting_on``
# — the first two reachable only from the deleted
# ``create_or_update_from_task``, the third with no caller at all.
#
# KNOWN CONSEQUENCE, deliberately left for the follow-up wave rather than taken
# here: with both writers gone, nothing can create a row in
# ``repo_bundle_locks.json``, so ``repo_lock_summary()`` below can now only ever
# report ``{"lock_count": 0, "locks": []}`` — and ``status.py`` publishes it as
# ``repo_locks``. That is the S47 item-5 defect class (a wire whose value no
# code path can move), but retiring it means editing the emitted status frame,
# which belongs with the read-side/contract work this wave was scoped out of.
# Recorded in docs/agent-runtime-harness/19-deferred-debt-ledger.md.


# S52: ``bundle_id_for`` / ``safe_bundle_state`` / ``normalize_repo`` /
# ``safe_token`` / ``safe_text`` / ``_dedupe_preserve_order`` and the
# ``owner_for_repo`` + ``_REPO_OWNER_RULES`` pair went with the write lane. Every
# one was reachable ONLY from a deleted writer (id minting, state coercion, and
# payload sanitising are write-time concerns), and none had an importer outside
# this module. Leaving them would be the residue S25 named when it retired
# ``events._safe_int`` with the formatter arm that was its only caller: a private
# helper outliving its only branch.


# S56 removed the four status projections this module existed to feed, plus
# the repo-lock reader behind them: ``repo_bundle_summary``,
# ``repo_bundle_delivery_summary``, ``bundle_queue_summary``,
# ``repo_lock_summary`` and its ``_repo_locks_path`` / ``_read_repo_locks``
# helpers. ``status.py`` was the sole caller of all four, and S52 had already
# deleted every writer that could create a bundle row or a lock entry — doc 19
# recorded ``repo_lock_summary`` as a wire that could only report
# ``{"lock_count": 0, "locks": []}``. The STORE READ side above stays: it is
# still the typed reader for on-disk bundle rows.
