"""Event-driven PM routing for blocked Kanban cards.

This module contains the pure/DB-local logic used by gateway event hooks to
turn a fresh ``task_events.kind == 'blocked'`` row into a PM routing card.  It
intentionally does **not** run inside ``kanban_db.block_task``'s write
transaction: the worker state transition stays fast and side-effect-free, then
the gateway event watcher reacts after commit.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
import sqlite3
from typing import Optional

from hermes_cli import kanban_db as kb


SERIOUS_PATTERNS = [
    r"credential", r"secret", r"token", r"password", r"auth(?!ored)", r"login", r"pkce",
    r"manual operator", r"manual action", r"human approval", r"tony approval", r"waiver",
    r"production", r"staging access", r"kubectl", r"kubernetes", r"cluster", r"external access",
    r"security", r"redaction", r"secret leak", r"jwt", r"bearer", r"authorization",
    r"destructive", r"delete", r"drop table", r"migration", r"payment", r"user data",
    r"product scope", r"architecture", r"api semantics", r"backend semantics",
    r"kill .*process", r"taskkill", r"cannot proceed without",
]

NON_SERIOUS_HINTS = [
    r"review-required", r"ready for review", r"implementation ready for review",
    r"needs[-_ ]?fix", r"protocol", r"routing", r"qa-routing",
    r"implementation complete", r"scoped implementation", r"commit", r"tests?.*(pass|green)", r"self-qa",
    r"process", r"stale lock", r"locked process", r"canonical .*exe .*locked", r"crashed", r"gave_up",
]


@dataclass(frozen=True)
class BlockedPmHookConfig:
    pm_assignee: str = "pm"
    workspace_kind: str = "scratch"
    workspace_path: Optional[str] = None
    priority: int = 100
    created_by: str = "kanban-blocked-hook"


@dataclass(frozen=True)
class BlockerClassification:
    actionable: bool
    serious: bool
    reason: str
    signal: str


@dataclass(frozen=True)
class BlockedPmHookResult:
    task_id: str
    action: str
    classification: BlockerClassification
    created_pm_task_id: Optional[str] = None


def _matches(patterns: list[str], text: str) -> bool:
    lower = text.lower()
    return any(re.search(pattern, lower) for pattern in patterns)


def _event_line(event: kb.Event) -> str:
    payload = event.payload or {}
    return f"event[{event.id}] {event.kind}: {payload}"


def _run_line(run: kb.Run) -> str:
    parts = [f"run[{run.id}] status={run.status}"]
    if run.outcome:
        parts.append(f"outcome={run.outcome}")
    if run.summary:
        parts.append(f"summary={run.summary}")
    if run.error:
        parts.append(f"error={run.error}")
    return " ".join(parts)


def blocked_signal_text(conn: sqlite3.Connection, task_id: str) -> str:
    """Return blocker-relevant signal without the full task body.

    Card bodies often include boilerplate about credentials, auth, redaction,
    taskkill, etc.  Classifying from the body falsely escalates ordinary
    review-required process blockers.  Use latest summaries/comments/events/runs
    instead: this mirrors Tony's PM rule and the existing cron watchdog.
    """
    chunks: list[str] = []
    latest = kb.latest_summary(conn, task_id)
    if latest:
        chunks.append(f"Latest summary: {latest}")
    for comment in kb.list_comments(conn, task_id)[-5:]:
        chunks.append(f"comment[{comment.author}]: {comment.body}")
    for event in kb.list_events(conn, task_id)[-10:]:
        chunks.append(_event_line(event))
    for run in kb.list_runs(conn, task_id)[-5:]:
        chunks.append(_run_line(run))
    return "\n".join(chunks)


def classify_blocked_card(conn: sqlite3.Connection, task_id: str) -> BlockerClassification:
    signal = blocked_signal_text(conn, task_id)
    non_serious = _matches(NON_SERIOUS_HINTS, signal)
    serious = _matches(SERIOUS_PATTERNS, signal)
    if non_serious:
        return BlockerClassification(True, False, "non-serious-process", signal)
    if serious:
        return BlockerClassification(True, True, "serious", signal)
    return BlockerClassification(False, False, "no-actionable-signal", signal)


def unseen_blocked_events(conn: sqlite3.Connection, *, after_event_id: int) -> list[kb.Event]:
    rows = conn.execute(
        "SELECT * FROM task_events WHERE id > ? AND kind = 'blocked' ORDER BY id ASC",
        (int(after_event_id),),
    ).fetchall()
    return [kb.Event(**dict(row)) if not isinstance(row["payload"], str) else _event_from_row(row) for row in rows]


def _event_from_row(row: sqlite3.Row) -> kb.Event:
    # Keep this parser local so callers don't need a new kanban_db API just for
    # the event hook.  ``Event`` payloads are stored as JSON text in SQLite.
    import json

    try:
        payload = json.loads(row["payload"]) if row["payload"] else None
    except Exception:
        payload = None
    return kb.Event(
        id=int(row["id"]),
        task_id=row["task_id"],
        kind=row["kind"],
        payload=payload,
        created_at=int(row["created_at"]),
        run_id=(int(row["run_id"]) if row["run_id"] is not None else None),
    )


def _pm_body(card_id: str, classification: BlockerClassification) -> str:
    verdict = (
        "serious blocker candidate; preserve block and escalate"
        if classification.serious
        else "non-serious process/routing blocker candidate; PM should route/recover without Alice/Tony intervention"
    )
    signal_excerpt = "\n".join(classification.signal.splitlines()[:80])
    return f"""Objective: PM must autonomously handle blocked card {card_id} according to Tony's rule: PM reviews every blocked card first, fixes/routes non-serious blockers, and keeps serious blockers visible/escalated.

Watchdog classification: {verdict}
Classifier reason: {classification.reason}
Classifier signal source: latest summary/comments/events/runs only (card body safety boilerplate intentionally ignored).

PM tasks:
1. Inspect `{card_id}` show/runs/log and current board blocked/running/ready state.
2. Classify the blocker as serious or non-serious from the actual block reason/latest handoff.
3. If non-serious process/routing blocker (review-required after commit/tests, protocol/routing bug, QA/review child missing, stale process hygiene): create/promote the correct immediate QA/review/recovery path yourself, complete/route parent only as implementation-complete when appropriate, and report the chain.
4. If serious (credentials/auth/manual operator action, production/staging access, security/redaction risk, destructive process/kill decision, product/API/architecture/data/payment/user-data risk, Tony waiver/approval): leave the original card blocked, add a PM comment with exact escalation need, and notify Tony/Alice only with the decision required.
5. Do not implement repo code, do not push, and do not ask Alice to manually fix the card.

Classifier signal excerpt:
```text
{signal_excerpt}
```
"""


def handle_blocked_event(
    conn: sqlite3.Connection,
    event: kb.Event,
    config: BlockedPmHookConfig | None = None,
) -> BlockedPmHookResult:
    config = config or BlockedPmHookConfig()
    task = kb.get_task(conn, event.task_id)
    if task is None:
        classification = BlockerClassification(False, False, "missing-task", "")
        return BlockedPmHookResult(event.task_id, "ignored", classification)
    if task.status != "blocked":
        classification = BlockerClassification(False, False, "task-no-longer-blocked", "")
        return BlockedPmHookResult(event.task_id, "ignored", classification)

    classification = classify_blocked_card(conn, event.task_id)
    if not classification.actionable:
        return BlockedPmHookResult(event.task_id, "ignored", classification)

    suffix = hashlib.sha1((event.task_id + classification.reason).encode()).hexdigest()[:10]
    idempotency_key = f"pm-blocked-hook-{event.task_id}-{suffix}"
    title_prefix = "PM: serious blocker triage " if classification.serious else "PM: auto-route blocked card "
    pm_task_id = kb.create_task(
        conn,
        title=title_prefix + event.task_id,
        body=_pm_body(event.task_id, classification),
        assignee=config.pm_assignee,
        created_by=config.created_by,
        workspace_kind=config.workspace_kind,
        workspace_path=config.workspace_path,
        priority=config.priority,
        idempotency_key=idempotency_key,
    )
    action = "serious-triage" if classification.serious else "auto-route"
    return BlockedPmHookResult(event.task_id, action, classification, pm_task_id)
