"""Typed downstream credential-selection state; credentials and priorities are never rotation cursors."""
from __future__ import annotations
import logging
import random
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, TYPE_CHECKING
from agent_runtime.auth_extensions import read_pool_rotation_state, write_pool_rotation_state
if TYPE_CHECKING:
    from agent.credential_pool import PooledCredential
logger = logging.getLogger(__name__)
_rotation_persist_warned = set()

@dataclass(frozen=True)
class PoolRotationCursor:
    """Where a provider's selection stands — as TYPED FIELDS, not as row order.

    MCF-44. The round-robin position used to have no field of its own: it WAS
    the order of the persisted credential rows, so "advance by one" meant
    renumbering every entry's ``priority`` and rewriting the whole credential
    store. That made a READ impossible without a write, moved ``auth.json``'s
    bytes on a quiescent store (MCF-16), and left ``priority`` meaning two
    different things at once.

    MCF-45. ``least_used``'s position is its USAGE VECTOR — the counts are
    exactly what decides its next pick, the same role ``last_selected_id`` plays
    for round-robin — and it had the opposite defect: it was incremented in
    memory and persisted NOWHERE, while ``load_pool`` rebuilds from disk on
    every call, so across processes the strategy always returned the same entry.
    One record holds both because they are one question ("where is selection?")
    asked by two strategies; a second record would be a second authority over
    the same answer.

    Persisted in its own sidecar beside ``auth.json``
    (:func:`hermes_cli.auth.write_pool_rotation_state`). Selecting writes this
    record and NOTHING else — no credential row moves, and no token material is
    ever stored here: only ids the pool itself minted, and integers.
    """

    last_selected_id: Optional[str] = None
    #: ``credential id -> requests this pool has handed out``. Monotonic, and
    #: merged across processes by MAX rather than by last-writer-wins: two
    #: processes incrementing concurrently must never let the slower writer
    #: erase the faster one's usage, which would re-pin the strategy on the
    #: busiest key.
    request_counts: Dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_state(cls, state: Any) -> "PoolRotationCursor":
        if not isinstance(state, dict):
            return cls()
        raw = state.get("last_selected_id")
        raw_counts = state.get("request_counts")
        counts: Dict[str, int] = {}
        if isinstance(raw_counts, dict):
            for key, value in raw_counts.items():
                # A hand-edited or partially-written sidecar must degrade to
                # "no count for that id", never poison a min() with a string.
                if isinstance(key, str) and isinstance(value, int) and value > 0:
                    counts[key] = value
        return cls(
            last_selected_id=raw if isinstance(raw, str) and raw else None,
            request_counts=counts,
        )

    def to_state(self) -> Dict[str, Any]:
        state: Dict[str, Any] = {}
        if self.last_selected_id:
            state["last_selected_id"] = self.last_selected_id
        if self.request_counts:
            state["request_counts"] = dict(self.request_counts)
        return state

def read_pool_rotation_cursor(provider: str) -> PoolRotationCursor:
    """Load one provider's cursor. A missing/damaged sidecar starts at the top."""
    try:
        return PoolRotationCursor.from_state(read_pool_rotation_state(provider))
    except Exception:  # pragma: no cover - defensive; the reader never raises
        logger.debug(
            "credential pool: could not read rotation cursor for %s",
            provider, exc_info=True,
        )
        return PoolRotationCursor()

def write_pool_rotation_cursor(provider: str, cursor: PoolRotationCursor) -> None:
    """Persist one provider's cursor.

    A failure here does NOT fail the selection: the caller already holds a
    usable credential, and refusing to hand it over because the rotation
    bookkeeping did not land would turn housekeeping into an outage. It is not
    silent either — the degradation (this process stops distributing across
    processes) is named once per provider, because a silent drop with no
    accounting is the class this whole line exists to retire.
    """
    try:
        write_pool_rotation_state(provider, cursor.to_state())
    except Exception as exc:
        if provider not in _rotation_persist_warned:
            _rotation_persist_warned.add(provider)
            logger.warning(
                "credential pool: could not persist the %s rotation cursor "
                "(%s) — selection still succeeded, but rotation will not "
                "distribute across processes until this store is writable",
                provider, exc,
            )

class PoolRotationMixin:
    def _usage_counts(self) -> Dict[str, int]:
        """The usage vector as it stands in memory, pruned to live entries.

        Built from ``self._entries`` rather than accumulated separately, so an
        entry removed from the pool takes its counter with it and the sidecar
        cannot grow without bound.
        """
        return {
            entry.id: entry.request_count
            for entry in self._entries
            if entry.request_count
        }

    def _sync_usage_counts_from_store(self) -> None:
        """Re-read the durable counts and merge them into memory by MAX (MCF-45).

        Re-read on every ``least_used`` selection, not once per pool, because
        the agent runtime holds ONE pool object per agent for the life of the
        process (``agent._credential_pool``). A pool loaded once and never
        refreshed would distribute perfectly within that process and be blind to
        every sibling — which is the defect this is fixing, one layer in. The
        cost is one read of a small JSON file per selection, against a selection
        that is about to make a network inference call.

        MAX rather than assignment in both directions: disk never lowers a live
        in-memory count (this process's own increments may not have landed yet),
        and memory never hides a higher count another process wrote.
        """
        durable = read_pool_rotation_cursor(self.provider)
        base = self._rotation_cursor if self._rotation_cursor is not None else durable
        for idx, entry in enumerate(self._entries):
            seen = durable.request_counts.get(entry.id, 0)
            if seen > entry.request_count:
                self._entries[idx] = replace(entry, request_count=seen)
        self._rotation_cursor = replace(base, request_counts=self._usage_counts())

    def _rotation_cursor_unlocked(self) -> PoolRotationCursor:
        if self._rotation_cursor is None:
            self._rotation_cursor = read_pool_rotation_cursor(self.provider)
        return self._rotation_cursor

    def _next_after_cursor(self, available: List[PooledCredential]) -> PooledCredential:
        """The available entry that FOLLOWS the cursor in priority order.

        Order comes from ``self._entries`` (operator priority); position comes
        from the typed cursor. Neither is derived from the other any more,
        which is the whole of MCF-44: rotating reorders nothing and rewrites
        nothing. Entries in cooldown are skipped without consuming a slot, and
        a cursor naming an id that has since left the pool restarts at the top
        rather than pinning the first entry forever.
        """
        order = [entry.id for entry in self._entries]
        by_id = {entry.id: entry for entry in available}
        last = self._rotation_cursor_unlocked().last_selected_id
        start = order.index(last) + 1 if last in order else 0
        for offset in range(len(order)):
            picked = by_id.get(order[(start + offset) % len(order)])
            if picked is not None:
                return picked
        return available[0]

    def _select_with_rotation(self, available, *, count=True, persist_rotation=True):
        from agent.credential_pool import STRATEGY_RANDOM, STRATEGY_LEAST_USED, STRATEGY_ROUND_ROBIN
        if self._strategy == STRATEGY_RANDOM:
            entry = random.choice(available)
        elif self._strategy == STRATEGY_LEAST_USED and len(available) > 1:
            self._sync_usage_counts_from_store()
            synced = {e.id: e for e in self._entries}
            available = [synced.get(e.id, e) for e in available]
            entry = min(available, key=lambda e: e.request_count)
        elif self._strategy == STRATEGY_ROUND_ROBIN and len(available) > 1:
            entry = self._next_after_cursor(available)
        else:
            entry = available[0]
        if count:
            entry = self._adopt(entry, persist=False, request_count=entry.request_count + 1)
            self._rotation_cursor = replace(self._rotation_cursor_unlocked(),
                last_selected_id=entry.id, request_counts=self._usage_counts())
            if persist_rotation:
                write_pool_rotation_cursor(self.provider, self._rotation_cursor)
        return entry

    def select_without_persisting_rotation(self):
        """Select and rotate in memory only; real token refresh still runs outside the pool lock."""
        with self._lock:
            entry, pending = self._select_unlocked(persist_rotation=False)
        if pending:
            self._refresh_pending_entries(pending)
            if entry is None:
                with self._lock:
                    entry, _ = self._select_unlocked(persist_rotation=False)
        if entry is not None:
            self._unmatched_rotation_streak = 0
        return entry
