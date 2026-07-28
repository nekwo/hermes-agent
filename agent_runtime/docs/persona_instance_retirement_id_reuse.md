# Retirement tombstones are not persona-namespaced — DEFERRED BY DECISION (2026-07-27)

## The shape

`PersonaInstanceStore.retire()` archives a placement-backed row to
`persona_instances_archive/<ts>_retire/<instance_id>.json`. The read-only
retirement predicate `retired_instance_archive_path()` composes retirement out
of two facts: **the absence of a live row** PLUS **the presence of a `*_retire`
archive for that id**.

The archive is keyed by **instance id alone**. A placement id is derived from
the placement (`persona_instance_id_for_placement`), not from the persona, so
two different personas placed at the same placement id would resolve to the
same tombstone. More importantly, in the shape that actually occurs: once a
placement id has been retired, **that id is permanently unusable**. Re-creating
a placement with the same id resolves the old tombstone and every bind through
`assert_bindable` / `open_chat` refuses it as `retired_persona_instance`.

## Decision: deferred, deliberately

Placement ids are **permanently single-use** until an un-retire / re-placement
verb ships. That verb is where the fix belongs — namespacing tombstones is only
meaningful once something can legitimately claim a retired id again, and
choosing the namespace (persona? realm? workspace? generation counter?) is a
decision that verb's semantics make, not one this predicate can make on its own.
Adding a namespace now would be speculative infrastructure with no consumer, and
a wrong namespace is worse than none: it would silently make a real tombstone
un-findable and let a retired placement come back as a live roster row.

Nothing today needs it. Retirement is an end-of-life transition for a deliberate
placement; the operator creating a replacement creates a **new** placement, which
gets a new id.

## The armor that makes deferral safe

`retired_instance_archive_path()` answers **`None` whenever a live row exists**:

```python
try:
    self.get(instance_id)
except Exception:
    pass
else:
    # A live row always wins: the archive is history, and an id carried by a
    # live placement is live — never a tombstone.
    return None
```

That live-row-wins branch is not an optimization, it is the guard rail for this
deferral. It means a legitimately re-created row is never refused by its own
history — whatever path creates it — so if an un-retire/re-placement verb ever
lands and writes a live row first, the tombstone stops being load-bearing on its
own. Pinned by
`tests/agent_runtime/test_persona_assignments.py` (the live-row-wins case
asserts both facts hold at once and that the LIVE one decides).

## If you are the one shipping the un-retire verb

- Namespace (or generation-stamp) the archive key **in that change**, together
  with the verb, so the two decisions are made once by the same author.
- Keep the composition inside `retired_instance_archive_path()`. Every caller
  (the mint pre-flight, `assert_bindable`, `open_chat`, the open-chat CLI
  pre-flight) asks that one method precisely so a second, subtly different
  retirement rule cannot be born at a call site.
- Do not weaken the live-row-wins branch to "compensate" for the namespace —
  they answer different questions.
