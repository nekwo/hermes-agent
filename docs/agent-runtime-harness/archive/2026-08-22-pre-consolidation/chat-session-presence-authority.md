# ONE chat-session presence authority — design (2026-07-26)

Status: **P1 SHIPPED 2026-07-27** (`agent_runtime/chat_session_scope.py`);
P2–P6 remain as written. Owner: fork (`agent_runtime/`,
`hermes_cli/harness_parts/`, `hermes_state.py`).
Trigger: the 2026-07-25 binding massacre (below). Stopgap guard already
shipped as `8c3942a21`; this document retires the class the guard patched.

## 2026-08-12 addendum — the INSTANCE_RECORDED rung + the ambient-read refusal

Two changes landed on top of P1 and the root-observability wave (machine root
anchor `root_anchor.py`, per-verb `resolution`/`chat_scope` blocks), closing
the "silent empty answer from the wrong root" class the 2026-08-12 ambient
chat-history incident exposed (`ok: true, count: 0` from the platform-default
shadow runtime; a full operator day lost to two wrong root causes because
every probe that resolved its own root confirmed health):

1. **The chat head is now recorded PER CONVERSATION.**
   `PersonaInstance.chat_head_home` stamps the head home the conversation was
   bound against — written by `PersonaInstanceStore.open_chat` under any
   AUTHORITATIVE scope (never from an ambient guess), re-affirmed every turn
   because the send path re-enters `open_chat` per turn, and audited on
   change (`persona_instance.chat_opened` carries `chat_head_home` +
   `previous_chat_head_home`). `resolve_chat_session_scope(session_id=…)`
   consults the record as a new ladder rung **above `SHARED_ROOT_POINTER`**,
   below env/relay. The rung resolves AND verifies: a disagreeing AUTHORITY
   (explicit head above, pointer below) is a typed `ChatScopeMismatch`, never
   a silent preference; absence is typed (`UNRECORDED` / `NO_INSTANCE` /
   `UNRESOLVABLE` / `RECORDED_HOME_MISSING`) and falls through exactly as
   before. Newly viable because the anchor made `store_root` — and therefore
   the instance store — resolvable from ambient processes. The field stays
   OFF the snapshot/patch wire (both are explicit allowlists).

2. **`ambient_home` stopped being an answer for chat READS.**
   `persona_chat_session_messages`, when it self-resolves, refuses a typed
   `chat_scope_unresolved` instead of reading the ambient guess, and a typed
   `chat_scope_mismatch` when two authorities disagree — each refusal carries
   the resolved `chat_scope` block, and every envelope (success included) now
   states its frame of reference. Escape hatch for a deliberately
   single-root, serve-less setup: `HERMES_ALLOW_AMBIENT_CHAT_READS=1` (plus a
   WARNING log per allowed ambient read). Consumer enumeration before the
   flip: the CLI verb prints the failure and exits 2; `agent_chat_open`
   propagates a typed refusal; the live-log backfill stops (mirror stays
   pending); the Launcher fetch lane maps exit 2 to a retryable-error tile
   and always names an explicit head anyway. Callers passing their own
   `session_db` own the acquisition and are untouched.

Pins: `tests/agent_runtime/test_chat_scope_instance_rung.py` (19, every one
red-proven by sabotage), plus the inverted
`tests/agent_runtime/test_snapshot_history_eviction.py::test_persona_chat_history_fetch_refuses_an_ambient_self_resolve`
(`:404`; formerly asserted the silent-empty success). **[Corrected 2026-08-20 —
MCF-78: that function lives in the eviction suite, not the instance-rung suite,
and this line named no file at all.]** This lands part of what P2/P3 planned
(typed refusals on the read lane) without SessionDB identity (P4/P5), which
remains open — the stamp records a head PATH, not a DB identity, so a
restored/moved `state.db` is still out of scope until P4.

## What shipped in P1 — and the THIRD defect it had to fix

The 2026-07-27 cockpit read-lane gap (Chat History listing six stale sessions
and none of the day's CLI-lane sessions, confirmed twice live) is **not** D1 or
D2. Under the Launcher's own environment
(`mission_control_settings.dart`: `HERMES_HOME=<root>/profiles/<profile>`,
`HERMES_HEAD_HOME=<root>/profiles/base`, default profile `base`) the two roots
are the SAME path, so both latent defects were dormant exactly as this document
predicted. The live defect is a third member of the class:

**D3 — the persona-instance store collapses per-profile homes onto the shared
runtime root; the chat SessionDB does not.** `resolution._default_hermes_root`
maps `<root>/profiles/<x>` → `<root>`, so every profile shares ONE
`persona_instances/` directory and therefore one `default_chat_session_id` per
instance. `get_hermes_head_home()` performs no such collapse: with no head
named it degrades to `get_hermes_home()`. Only the Launcher names a head, so a
CLI-lane turn under a profile home minted its transcript into
`<root>/profiles/<active>/state.db` while writing the binding into the SHARED
store, and the cockpit — reading the head database — dropped the row as
`session_not_in_db`. Live evidence: the three most recently updated bindings in
`X:\Eternia\.hermes\agent-runtime` pointed at sessions present only in
`profiles/alice/state.db`.

Shipped:

- **`agent_runtime/chat_session_scope.py`** — the single acquisition, with a
  typed ladder (`ChatHeadSource.RELAY_CONTEXT` > `ENV_HEAD_HOME` >
  `SHARED_ROOT_POINTER` > `AMBIENT_HOME`) and two postures on the resolved
  scope: `authoritative` (enough to read or mint) and `explicitly_named`
  (required by the destructive lane, so `8c3942a21`'s behavior is unchanged).
- **The head pointer (`<store_root>/chat_head_home.json`)** — the D3 fix. The
  process that legitimately knows the operator head (`harness serve`, always
  started by the Launcher with an explicit `HERMES_HEAD_HOME`) publishes it once
  into the shared runtime root; any later process that names no head reads it
  instead of degrading to its own profile database. One writer, at serve boot.
  Env and relay context always win, so this can only narrow the ambient
  fallback.
- **Seven delegating sites**, each preserving its own failure posture:
  `persona_chat_history._default_session_db`,
  `snapshot._default_persona_session_db`, `stream._scope_fingerprint`,
  `persona_assignments._session_presence_probe` (still requires an explicitly
  named head), `persona_commands._default_persona_session_db` (still raises
  `PersonaChatPersistenceError`; now fails closed only on an AMBIENT scope),
  `continuity._session_db` (**D2**), `serve._runtime_state_fingerprint`
  (**D1**, and it no longer opens a database just to read its own path).
- **`tests/agent_runtime/test_chat_session_scope.py`** — the ladder table, the
  pointer lifecycle, D1, D2, and the check that failed twice live: mint through
  the real CLI-lane acquisition under a different profile home, then assert the
  serve projection lists it and records no `session_not_in_db` drop. The
  companion test removes the pointer and pins the gap reproducing.

Not shipped (unchanged from the plan below): the CI AST chokepoint guard (R1),
typed `PresenceVerdict`/`PresenceRefused` (P2), the advisory-lane
reclassification (P3), SessionDB identity (P4/R4), and P5/P6.

Sibling docs: `12-read-path-freshness-hardening.md` (the "emission is
convention, not enforcement" ruling this design applies to presence),
`13-write-path-intent-integrity.md` (the store-chokepoint precedent),
`serve-runtime-truth.md` (operator forensics for the same runtime roots).

---

## The incident (2026-07-25 ~17:49Z)

`995d98ec4` added `PersonaInstanceStore.repair_missing_chat_session_bindings`
as **Phase 4** of `hermes harness persona-instance reconcile` — a self-heal
for persona instances still pointing at deleted chat sessions (the
`session_not_in_db` anomaly the Mission Control parity pill had been
reporting ×10).

A reconcile then ran with `HERMES_HOME=<root>/profiles/alice` and **no**
`HERMES_HEAD_HOME`. Consequences, in order:

1. `_session_presence_probe` self-resolved its SessionDB through
   `persona_chat_history._default_session_db()` →
   `get_hermes_head_home()/state.db`. With no head authority present,
   `get_hermes_head_home()` degrades to `get_hermes_home()`
   (`hermes_constants.py:111`) — so it opened **alice's** `state.db`.
2. Alice's DB holds ~1486 sessions. The probe's fail-closed guard is an
   **empty-database** check (`session_db_empty`), so a populated-but-wrong
   database sails straight through it.
3. The live operator bindings belonged to **base** (~72 sessions). Every one
   of them was absent from alice's DB, so the probe returned `"absent"`.
4. Phase 4 cleared **10 live chat bindings** through
   `clear_chat_session_binding`, each emitting
   `persona_instance.chat_binding_cleared` with
   `reason: session_missing_from_session_db`.
5. Operator symptom: the Mission Control console rendered the
   **"Console Standing By"** card
   (`EterniaLauncher lib/features/mission_control/page_parts/secondary_drawers.dart:1355`,
   widget key `mission_operator_channel_standby`), because a cleared
   `default_chat_session_id` produces no history row → no operator channel →
   `channelsProjected == 0`. QA agents forked fresh sessions on their next
   send.
6. Recovery: all 10 bindings were replayed out of the
   `chat_binding_cleared` events through
   `hermes harness persona instance open-chat`.

`8c3942a21` then added a third precondition to the probe: refuse with
`head_home_not_authoritative` when the head home is self-resolved rather than
explicitly named (`persona_assignments.py:84-85`, predicate
`hermes_constants.hermes_head_home_is_authoritative()` at
`hermes_constants.py:114-131`).

**That guard is correct and should stay. It is also not the fix.** It checks
that *an* authority was named, not that the *right* one was. An operator who
exports `HERMES_HEAD_HOME=<root>/profiles/alice` — wrong, but explicit —
passes the guard and gets the identical massacre.

---

## Established codebase facts (verified 2026-07-26, do not re-derive)

### The two roots

| State | Resolver | Root |
| --- | --- | --- |
| Persona-instance rows (`default_chat_session_id`, `session_id`) | `agent_runtime/resolution.py:144` `_hermes_home(env)` via `paths.store_root()` | **`HERMES_HOME`** |
| Chat sessions (SessionDB `state.db`) | `hermes_constants.py:95-111` `get_hermes_head_home()` | **`HERMES_HEAD_HOME`**, falling back to `HERMES_HOME` |

`agent_runtime/resolution.py` never reads `HERMES_HEAD_HOME`. Presence is
therefore a **join across two independently-resolved roots**, and nothing
asserts the two halves belong together.

`HERMES_HEAD_HOME` appears in **zero** Markdown files repo-wide
(`grep -rln HERMES_HEAD_HOME --include=*.md .` → empty, 2026-07-26). It is an
undocumented environment variable that decides which database ten live
bindings are judged against.

### Every fork-owned SessionDB acquisition (7 sites, 4 policies, 2 roots)

| # | Site | Root resolved | Authority guard | On failure |
| --- | --- | --- | --- | --- |
| 1 | `agent_runtime/persona_chat_history.py:644-655` `_default_session_db()` | head | **none** | returns `None` silently |
| 2 | `agent_runtime/snapshot.py:1673-1681` `_default_persona_session_db()` | head | **none** | returns `None` silently |
| 3 | `hermes_cli/harness_parts/persona_commands.py:4020-4048` `_default_persona_session_db()` | head | only when a profile override is active (`:4042`) | raises `PersonaChatPersistenceError("session_db_acquire")` |
| 4 | `agent_runtime/persona_assignments.py:50-105` `_session_presence_probe()` | head (via #1) | **always** (`:84`) | returns `(None, "head_home_not_authoritative")` |
| 5 | `agent_runtime/stream.py:539-551` `_scope_fingerprint()` | head | **none** | appends `"session_db:unresolved"` |
| 6 | `hermes_cli/harness_parts/serve.py:220-227` read-cache key | **`HERMES_HOME` (wrong root)** | **none** | appends `("session_db", -1, -1)` |
| 7 | `agent_runtime/continuity.py:120-123` `_session_db()` | **`HERMES_HOME` (wrong root)** | **none** | raises out |

Sites 1–5 are correct about the root and disagree about the policy. Sites 6
and 7 are wrong about the root (see "Two latent defects" below).

### Every answerer of "does this chat session exist" (3, with 3 consequences)

| Lane | Site | Consequence of a false "absent" |
| --- | --- | --- |
| Projection / parity | `persona_chat_history.py:195-209` — `_get_session_row(db, session_id) is None` → `accountant.drop("session_not_in_db", …)` | cosmetic: the MC parity pill counts a phantom anomaly |
| Gating | `persona_commands.py:419-430` — `session_db.get_session(args.session_id) is None` → `{"error_kind": "unknown_chat_session"}` | recoverable: an `open-chat` is refused |
| **Repair (destructive)** | `persona_assignments.py:853` — `probe(session_id) == "absent"` → `clear_chat_session_binding` | **irreversible-by-default: a live binding is nulled** |

Three code paths, three independent database acquisitions, three fail
postures, one question. Until `8c3942a21` the destructive lane had the
weakest guard of the three.

### The presence probe as shipped (`persona_assignments.py:50-105`)

```python
    if db is None:
        try:
            from hermes_constants import hermes_head_home_is_authoritative
            from .persona_chat_history import _default_session_db

            if not hermes_head_home_is_authoritative():
                return None, "head_home_not_authoritative"
            db = _default_session_db()
        except Exception:
            db = None
    if db is None:
        return None, "session_db_unavailable"
    try:
        sample = db.list_sessions_rich(limit=1, include_archived=True)
    except Exception:
        return None, "session_db_unavailable"
    if not sample:
        return None, "session_db_empty"
```

The closure returns the bare strings `"present" | "absent" | "unknown"`; a
raising `get_session` yields `"unknown"`, never `"absent"` — the tri-state is
already right, it is just untyped.

### Other load-bearing facts

- Unbind chokepoint: `PersonaInstanceStore.clear_chat_session_binding`
  (`persona_assignments.py:745-795`) — sole emitter of
  `persona_instance.chat_binding_cleared` (`:794`); contract registered at
  `decision_contract_registry.py:1225`. Reasons:
  `_BINDING_REPAIR_REASON = "session_missing_from_session_db"` (`:46`) and
  `CHAT_BINDING_CLEARED_REASON_DELETED = "chat_deleted"` (`:47`).
- Bind chokepoint: `PersonaInstanceStore.open_chat`
  (`persona_assignments.py:1525-1676`); writes `mode`,
  `default_chat_session_id` (`:1654`) and the legacy `session_id` mirror
  (`:1657`); idempotent no-op short-circuit at `:1667-1673`.
- Reconcile: `agent_runtime/persona_instance_identity.py`
  `reconcile_persona_instances`; Phase 4 at `:478-484`; report keys
  `session_binding_repairs` / `_repaired_count` / `_held` / `_skipped`
  (`:498-501`). CLI: `hermes harness persona-instance reconcile`
  (`hermes_cli/harness.py:1439-1458` →
  `hermes_cli/harness_parts/runtime_commands.py:18-57`).
- `SessionDB` — `hermes_state.py:905`; `get_session` `:2620-2627`;
  `list_sessions_rich` `:3010`; `SCHEMA_VERSION = 19` (`:159`);
  `repair_state_db_schema` `:590`; default path resolution
  `_resolve_default_db_path()` `:134-156`.
- `PersonaInstance.default_chat_session_id` — `agent_runtime/models.py:531`;
  legacy `session_id` mirror `:534`; `__post_init__` v1 migration `:562-567`
  (the additive-field precedent).
- Parity classification is emitted at the drop site since `995d98ec4`:
  `ProjectionAccountant.drop(code, by_design=…)` in `agent_runtime/parity.py`;
  `PARITY_ENVELOPE_VERSION` deliberately unchanged (additive evolution).

### Two latent defects found while writing this doc (not live-proven)

**D1 — `serve.py:222` fingerprints the wrong database.** The serve read-model
cache key calls bare `SessionDB()`, which resolves `HERMES_HOME/state.db`,
while every chat write goes to `HERMES_HEAD_HOME/state.db`. Whenever the
launcher's selected `hermesProfile` is not the head profile, the cache is
keyed on a file nothing writes — so a cached snapshot can serve a frozen Chat
History for the life of the serve process. This is the *same symptom class*
`639242901` just fixed on the stream lane by fingerprinting the head-home
`state.db`; the serve lane was missed. Dormant today only because the
launcher's default profile is `base`.

**D2 — `continuity.py:54` mints and writes the parent session in the wrong
database.** `return_summary_to_parent_session` calls
`session_db.ensure_session(parent_session_id, …)` then `append_message` on a
bare `SessionDB()` (`HERMES_HOME`). A child returning a distilled summary to
its operator parent runs **precisely under a persona profile-home override** —
which is the exact condition `_HERMES_HEAD_HOME` was introduced to defend
(the 2026-07-18 relay-SessionDB-persistence incident, `hermes_constants.py:37-45`).
Under an override this writes the summary into the child's profile DB where
the projection never reads it, **and mints a phantom session row there**. A
phantom row is worse than a missing one: a misrouted presence probe reading
that DB would answer `"present"` for a session the operator cannot see.

Both are in scope for stage **P1** below; neither requires the rest of the
design.

---

## 1. What is weak, and why it will recur

**W1 — Seven acquisitions, four policies, two roots.** See the table above.
There is no single place to change how the runtime decides which chat
database is authoritative, so every new consumer re-decides — and three of
the seven decided "don't check".

**W2 — Three answerers of one question, with consequences from cosmetic to
destructive.** The diagnosis lane (which produces the `session_not_in_db`
count that *motivates* running the repair) and the repair lane (which acts on
it) do not share a verdict, a database acquisition, or a notion of
trustworthiness. On 2026-07-25 they agreed — both were wrong — and that
agreement is what made the repair look justified.

**W3 — The guard is a bolt-on on one lane, with two different predicates.**
`hermes_head_home_is_authoritative()` is consulted at exactly 2 of 7
acquisition sites, and the two spellings differ:

- `persona_commands.py:4042` — `override is not None and not authoritative` → raise
- `persona_assignments.py:84` — `not authoritative` → skip

The **projection** lanes have no guard at all. Today, a snapshot built under
`HERMES_HOME=profiles/alice` with no head still produces the same false
verdict — it just *renders* it as a phantom parity anomaly instead of
*executing* it. The wrong answer was never retired; only one of its three
consumers was disarmed.

**W4 — The guard validates that an authority was named, not that it is the
right one.** `hermes_head_home_is_authoritative()` is a proxy for
provenance. Nothing records, at bind time, **which SessionDB a binding was
minted against**, so at repair time there is no fact to check against — only
whether an environment variable happens to be non-empty. An explicit but
wrong `HERMES_HEAD_HOME` reproduces the incident exactly. **This is the
reason the class recurs: the destructive lane trusts a proxy.**

**W5 — Presence is inferred by scanning, never recorded as a fact.**
Deleting a chat emits `persona_chat.deleted` + `chat_binding_cleared`. Every
*other* way a session can stop being visible — profile switch, DB swap,
restore from backup, test-fixture leak — is discoverable only by opening
whatever database ambient process resolution hands you. SessionDB mints emit
no EventLog event at all, which is why `639242901` needed a filesystem
fingerprint to keep Chat History unfrozen, and why `serve.py` needs one too.

**W6 — The authority is undocumented.** Zero Markdown references. An
operator running `persona-instance reconcile` by hand has no way to learn
that they must first export `HERMES_HEAD_HOME`, and the command prints
nothing about which database it is about to judge.

**Why it will recur — the recurrence is already the finding.** This exact
class has produced two live incidents in twelve days, both "a presence
verdict computed against whichever SessionDB the ambient process resolution
returned":

- **2026-07-13** — `persona_chat_history/no_instance_match` ×7: test-fixture
  rows leaked into the live `state.db` through an import-frozen
  `DEFAULT_DB_PATH` (fixed `e24bbe526`, ratchet guard `e8a5842fa`).
- **2026-07-25** — `session_not_in_db` ×10: a misrouted home (fixed
  `995d98ec4` for the write path, guarded `8c3942a21` for the probe).

Both were fixed at the call site. Stage 12 already ruled on this shape:
*make the violation impossible (CI) or self-announcing (runtime), never "be
more careful"* (`12-read-path-freshness-hardening.md` §Design). A third
call-site patch is the wrong move.

---

## 2. Target shape

### One module, one acquisition, one verdict

**NEW `agent_runtime/chat_session_presence.py`** — the sole authority for
"which chat database is authoritative here" and "does this session exist".

```python
class PresenceVerdict(Enum):
    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"      # never destructive


class HomeSource(Enum):
    RELAY_CONTEXT = "relay_context"    # _HERMES_HEAD_HOME ContextVar
    ENV_HEAD_HOME = "env_head_home"    # HERMES_HEAD_HOME
    AMBIENT_HOME = "ambient_home"      # degraded: get_hermes_home() fallback


class Posture(Enum):
    ADVISORY = "advisory"          # projection, parity, fingerprints, cache keys
    GATING = "gating"              # open-chat precondition, send validation
    DESTRUCTIVE = "destructive"    # reconcile Phase 4, clear_chat_session_binding


@dataclass(frozen=True, slots=True)
class PresenceScope:
    db_path: Path
    home_source: HomeSource
    authoritative: bool           # RELAY_CONTEXT or ENV_HEAD_HOME
    db_identity: str | None       # stable SessionDB identity (stage P4)
    unusable_reason: str | None   # typed; None when usable


class ChatSessionPresence:
    @classmethod
    def resolve(cls, *, posture: Posture, session_db=None) -> "ChatSessionPresence": ...
    @property
    def scope(self) -> PresenceScope: ...
    def verdict(self, session_id: str, *, minted_against: str | None = None) -> PresenceVerdict: ...
    def require_usable(self) -> None:  # raises PresenceRefused(reason, scope)
```

Typed refusal reasons (promoted from today's bare strings, plus two new):
`head_home_not_authoritative`, `session_db_unavailable`, `session_db_empty`,
`session_db_identity_mismatch`, `presence_provenance_unknown`.

### The five rules

**R1 — One acquisition.** No fork-owned module constructs
`SessionDB(db_path=get_hermes_head_home() / "state.db")` (or bare
`SessionDB()` for chat purposes) again. All seven sites delegate. Enforced by
a CI AST guard in the `test_store_event_invariant.py` tradition (Stage 12
slice B2): walk `agent_runtime/` and `hermes_cli/harness_parts/` for
`SessionDB(` constructions and fail unless the site is in a curated
allowlist with a written justification. A new consumer fails CI until it is
consciously classified. **Upstream (non-fork) `SessionDB()` sites are out of
scope** — a plain `hermes chat` session legitimately belongs to the active
profile; the fork boundary (`project_hermes_fork_boundary`) holds.

**R2 — One policy, three declared postures.** The caller declares intent; it
does not re-derive the guard:

| Posture | Requires | On an unusable scope |
| --- | --- | --- |
| `ADVISORY` | nothing | **never refuses**; stamps confidence into what it emits (see R3) |
| `GATING` | `authoritative` | raise `PersonaChatPersistenceError` / return `error_kind` — today's behavior, unchanged |
| `DESTRUCTIVE` | `authoritative` **and** identity match | refuse with a typed reason; hold the row, never clear |

**R3 — Advisory lanes stop crying wolf.** A projection reading a
non-authoritative scope emits `session_presence_unverified` (declared
`by_design=True`, i.e. non-anomalous) instead of `session_not_in_db`. This
rides the `995d98ec4` mechanism exactly — classification at the emission
site, additive envelope, no `PARITY_ENVELOPE_VERSION` bump. The operator
stops seeing a phantom anomaly that invites a destructive repair; they see an
honest "presence could not be verified from this process".

**R4 — Provenance, not env-presence (the root fix).** `open_chat` records
the **identity of the SessionDB the binding was minted against** on the
persona-instance row: `chat_session_db_id`. Identity is a stable UUID minted
once into the SessionDB itself (a `meta` row — `SCHEMA_VERSION 19 → 20`,
additive; the store already runs `repair_state_db_schema` at
`hermes_state.py:590`), **not** the path, which changes across machines and
realm sync.

`verdict()` under `DESTRUCTIVE` then becomes two checks:

1. Is the scope authoritative? (today's guard, kept)
2. Does the resolved DB's identity equal the identity recorded on the
   binding? If not → `UNKNOWN` with `session_db_identity_mismatch` — **never
   `ABSENT`**.

This kills the 2026-07-25 incident at the root: probing alice's DB for a
base-minted binding returns `UNKNOWN`, **even under an explicit-but-wrong
`HERMES_HEAD_HOME=profiles/alice`**, which is precisely the case the shipped
guard still permits. Bindings minted before the migration carry no identity →
`UNKNOWN` → held, with `presence_provenance_unknown` so the operator can see
how many rows the repair can no longer judge. Backfill is automatic: every
`open_chat` stamps it.

**R5 — The scope is printed before anything destructive happens.**
`persona-instance reconcile` gains `--head-home` and prints the resolved
scope (path, `home_source`, `authoritative`, `db_identity`) in both human and
`--json` output **before** Phase 4 runs. The 2026-07-25 run would have
printed `home_source=ambient_home authoritative=false` in its own output.
`HERMES_HEAD_HOME` becomes a documented runtime contract (P0).

### Deliberate non-goals

- **Not** event-sourcing SessionDB. R5's documentation and R4's identity make
  the cross-root scan *safe*; making it unnecessary is P6, separately
  scheduled and gated.
- **No** launcher (Dart) change is required. The standby card at
  `secondary_drawers.dart:1355` is driven by `channelsProjected`, and nothing
  here changes the operator-channel projection shape. Surfacing a "presence
  unverified" chip is an optional later slice (open question 3).
- **No** change to `operator_channels.py`, the read model, the stream frame
  schema, or any MCP/delivery surface.
- **No** upstream (non-fork) file is touched.

---

## 3. Blast radius

### Files

| File | Change |
| --- | --- |
| `agent_runtime/chat_session_presence.py` | **NEW** — the authority |
| `agent_runtime/persona_assignments.py` | delete `_session_presence_probe` (`:50-105`); `repair_missing_chat_session_bindings` (`:797-900`) consumes `Posture.DESTRUCTIVE`; `open_chat` (`:1525-1676`) stamps `chat_session_db_id`; `clear_chat_session_binding` (`:745-795`) asserts a destructive-usable scope |
| `agent_runtime/models.py` | `PersonaInstance` gains `chat_session_db_id` beside `:531` (additive; `__post_init__` migration precedent at `:562-567`) |
| `agent_runtime/persona_chat_history.py` | `_default_session_db` (`:644-655`) → delegate; the `session_not_in_db` drop (`:195-209`) gains the unverified classification |
| `agent_runtime/snapshot.py` | `_default_persona_session_db` (`:1673-1681`) → delegate |
| `agent_runtime/stream.py` | `_scope_fingerprint` (`:539-551`) → delegate (same bytes fingerprinted, one resolver) |
| `agent_runtime/continuity.py` | `_session_db` (`:120-123`) → delegate — **fixes D2** |
| `agent_runtime/parity.py` | register `session_presence_unverified` as by-design |
| `agent_runtime/decision_contract_registry.py` | `persona_instance.chat_binding_cleared` (`:1225`) gains optional `session_db_id`; `persona_chat.session_minted` (P6 only) |
| `hermes_cli/harness_parts/persona_commands.py` | `_default_persona_session_db` (`:4020-4048`) → delegate, keeps raising `PersonaChatPersistenceError`; open-chat precondition (`:419-430`) → `Posture.GATING` |
| `hermes_cli/harness_parts/serve.py` | cache key (`:220-227`) → scope `db_path` — **fixes D1** |
| `hermes_cli/harness_parts/runtime_commands.py` | `_cmd_persona_instance_reconcile` (`:18-57`) prints the scope; honors `--head-home` |
| `hermes_cli/harness.py` | `--head-home` on the reconcile parser (`:1439-1458`) |
| `hermes_state.py` | `meta` identity row + `SessionDB.identity()`; `SCHEMA_VERSION 19 → 20` |
| `docs/agent-runtime-harness/serve-runtime-truth.md` | new §: the `HERMES_HOME` / `HERMES_HEAD_HOME` contract |

### Contracts

- `persona_instance.chat_binding_cleared` payload — **additive**
  (`session_db_id`); existing required summary fields unchanged.
- Parity envelope — **additive** (`session_presence_unverified` code,
  declared by-design). `PARITY_ENVELOPE_VERSION` stays `1`, per the
  `995d98ec4` precedent. The launcher's `f0e5b5b4` consumer prefers the
  row-declared `by_design` list, so **no launcher change is required** for
  the new code to read as benign.
- `PersonaInstance` JSON — **additive** field; an older hermes reading a
  newer row ignores it, a newer hermes reading an older row treats it as
  `None` → `presence_provenance_unknown` → held. Safe both directions.
- SessionDB schema `19 → 20` — one `meta` row. An older hermes opening a
  newer DB is unaffected (it never selects the key); `repair_state_db_schema`
  already handles forward migration.

### Not touched

`operator_channels.py`, `read_model.py`, the stream frame schema, the
delivery directive, the MCP lanes, the launcher.

---

## 4. Migration plan (ordered; each step independently shippable)

### P0 — Document the contract *(docs only, ships immediately)*

This document, plus a `HERMES_HOME` vs `HERMES_HEAD_HOME` section in
`serve-runtime-truth.md` (which already owns "runtime facts that keep reading
as bugs"). Zero code risk; retires **W6** the day it lands, and gives the
next operator running a bare reconcile the missing precondition.

### P1 — One acquisition, zero policy change

Introduce `chat_session_presence.py` with `resolve()` + `PresenceScope`.
Convert all seven sites to delegate, each passing an explicit `Posture` that
**preserves its current behavior verbatim**. Add the CI AST chokepoint guard.

Two intentional behavior changes ride this step because they are pure
root-correctness fixes with no policy component:

- **D1** — `serve.py` fingerprints the head-home DB (matching `639242901`'s
  stream fix).
- **D2** — `continuity.py` writes parent-session summaries to the head-home
  DB and stops minting phantom rows in profile DBs.

Everything else is byte-identical. **This is the step that makes the guard
structural rather than a bolt-on**: after P1 there is exactly one place where
presence policy can be changed, and CI fails any attempt to add an eighth.

### P2 — Typed verdicts, typed refusals, visible scope

Replace `"present" | "absent" | "unknown"` with `PresenceVerdict`; replace
`report["skipped"] = "head_home_not_authoritative"` with a typed
`PresenceRefused` carrying reason **and** the resolved scope. Reconcile and
every `--json` envelope gain a `presence_scope` block. Add `--head-home`.
Behavior unchanged; diagnosability transformed — the 2026-07-25 run would
have named its own fault in its own output.

### P3 — Advisory lanes stop crying wolf

Projection and parity emit `session_presence_unverified` (by-design) instead
of `session_not_in_db` when the scope is not authoritative. Retires the
phantom-pill half of the failure mode and removes the false motive for
running a destructive repair. Rides `995d98ec4`; no envelope version bump; no
launcher change.

### P4 — SessionDB identity *(additive, soak-friendly)*

`hermes_state.py` mints a stable identity into the DB (`meta` row,
`SCHEMA_VERSION 19 → 20`); `SessionDB.identity()`. `open_chat` stamps
`chat_session_db_id` on the instance row. **No consumer yet** — this stage is
pure additive plumbing and is designed to sit in production through a soak
while real bindings accumulate provenance.

### P5 — `DESTRUCTIVE` requires an identity match *(the class retirement)*

Phase 4 clears a binding only when the recorded `chat_session_db_id` equals
the resolved DB's identity. Legacy rows without one are **held** with
`presence_provenance_unknown` and counted in the report, so the operator sees
exactly how much of the fleet the repair can no longer judge (and can decide
whether to force a backfill).

Ship behind `HERMES_PRESENCE_REQUIRE_IDENTITY` (**default on**) so rollback
is an environment change on the live serve, not a redeploy.

After P5 the 2026-07-25 incident is **unrepresentable**, including under an
explicitly-wrong `HERMES_HEAD_HOME`.

### P6 — Event the presence transitions *(strategic; separately scheduled)*

Emit `persona_chat.session_minted` into the `HERMES_HOME` EventLog beside the
persona-instance rows it will be joined against, and keep the existing
`persona_chat.deleted`. Then:

- the reconcile can prefer the **same-root event log** over a cross-root DB
  scan — the join stops crossing roots at all;
- `stream._scope_fingerprint`'s `state.db` stat and `serve.py`'s cache key
  demote from correctness to optimization.

**Gate:** only after P5 has soaked and the parity pill has read zero
anomalous drops for a sustained window. Do not bundle with P1–P5.

---

## 5. Test plan

**P1 — chokepoint — LANDED as `tests/agent_runtime/test_chat_session_scope.py`**
(the name below was the proposal; `test_chat_session_presence.py` was never
created — MCF-78, 2026-08-20)
- `resolve()` returns `RELAY_CONTEXT` when the ContextVar is recorded,
  `ENV_HEAD_HOME` when only the env var is set, `AMBIENT_HOME` (and
  `authoritative=False`) when neither is — one table test replacing four
  scattered assumptions.
- An authoritative head that **equals** the active profile override is
  `authoritative=True` (the legitimate same-DB relay case documented at
  `persona_commands.py:4036-4040`; the old path-equality check killed it live
  on 2026-07-23 — pin the regression here).
- **AST guard** `test_chat_session_presence_chokepoint.py` — **NOT BUILT**
  (UNCOVERED SEAM as of MCF-78, 2026-08-20): a synthetic
  unclassified `SessionDB(` construction inside `agent_runtime/` fails the
  test. Modeled on `test_store_event_invariant.py`.
- D1: `serve._fingerprint` includes the head-home `state.db` path when
  `HERMES_HEAD_HOME != HERMES_HOME`; a write to the head DB moves the
  fingerprint, a write to the profile DB does not.
- D2: `return_summary_to_parent_session` under an active profile override
  appends to the **head-home** DB and mints **no** row in the profile DB.

**P2 — typed surface**
- Every refusal reason round-trips through the reconcile `--json` envelope
  with a populated `presence_scope`.
- `--head-home` overrides ambient resolution and reports `ENV_HEAD_HOME`.
- Migrate the existing coverage in
  `tests/agent_runtime/test_persona_instance_identity.py:591-789` — the
  Phase-4 block, including `test_repair_skips_when_head_home_is_not_authoritative`
  (`:744-766`) and `..._refuses_on_blind_database` (`:720`) — onto the typed
  reasons. **These must keep passing unmodified in behavior**; only the
  assertion spelling changes.

**P3 — classification**
- Non-authoritative scope → the projection emits
  `session_presence_unverified` with `by_design=True`; the anomalous count is
  **0**. Extends `tests/agent_runtime/test_persona_assignments.py:1609-1639`
  and `tests/agent_runtime/test_parity.py:39-46`.
- Authoritative scope with a genuinely deleted session still emits
  `session_not_in_db` as anomalous (no regression in real detection).

**P4 — identity**
- Identity is minted once and stable across reopen; two distinct DB files
  have distinct identities; a **copied** DB file preserves its identity (see
  open question 2 — pin whichever answer the operator chooses).
- `open_chat` stamps `chat_session_db_id`; the idempotent no-op path
  (`persona_assignments.py:1667-1673`) still writes no event when nothing
  else changed.
- Schema `19 → 20` upgrade on an existing DB preserves every session row.

**P5 — the incident, as a regression test**
- **The 2026-07-25 scenario, verbatim:** two SessionDBs, one populated with
  ~1486 unrelated sessions ("alice") and one with the bindings' real sessions
  ("base"); bindings stamped with base's identity; reconcile run against
  alice with `HERMES_HEAD_HOME` **explicitly set to alice**. Assert:
  `repaired_count == 0`, every row held with
  `session_db_identity_mismatch`, and **zero**
  `persona_instance.chat_binding_cleared` events appended. *This is the case
  the shipped `8c3942a21` guard still permits — it is the acceptance test for
  the whole design.*
- Legacy rows (no `chat_session_db_id`) → held with
  `presence_provenance_unknown`, never cleared.
- A genuinely deleted session on a matching, authoritative DB **is** still
  repaired (the feature must keep working).
- `HERMES_PRESENCE_REQUIRE_IDENTITY=0` restores P4-era behavior exactly.

**Gates.** Full `tests/agent_runtime` + `tests/hermes_cli/test_harness_cli.py`
green at every stage (the Stage 12/13 precedent). Known pre-existing failures
are not to be masked or adopted.

**Live proof (P5, operator-run).** Back up
`agent-runtime/persona_instances/*.json` first (the 2026-07-25 runbook
precedent), then: `hermes harness persona-instance reconcile --dry-run --json`
against a deliberately misrouted `HERMES_HEAD_HOME` must report **0** repairs
and print `session_db_identity_mismatch` — with no `--dry-run` run needed to
prove safety, because the dry-run path (`persona_assignments.py:867-892`)
writes nothing.

---

## 6. Rollback

Stage-by-stage, and every stage is a **strict narrowing** of the destructive
lane — no step can clear a binding that today's code would hold. The failure
mode of a bad rollout is "reconcile repairs nothing", which is exactly the
fail-closed posture `8c3942a21` already chose.

| Stage | Rollback | Data left behind |
| --- | --- | --- |
| P0 | revert the docs | none |
| P1 | revert the module + the seven delegates (one commit) | none |
| P2 | revert; consumers read the old string reasons | none |
| P3 | revert; parity re-emits `session_not_in_db` | none (envelope was additive) |
| P4 | revert code; **leave the schema** | an unread `meta` row + an unread instance field, both ignored by older code — no data migration either direction |
| P5 | `HERMES_PRESENCE_REQUIRE_IDENTITY=0` (**env change on the live serve, no redeploy**), or revert | none |
| P6 | revert; the fingerprint lanes are still in place as the correctness path | orphaned `persona_chat.session_minted` events, harmless |

Emergency recovery for a *future* false clear is unchanged and already
proven: `persona_instance.chat_binding_cleared` events carry
`persona_instance_id` + `session_id`, and
`hermes harness persona instance open-chat` replays them (10/10 restored on
2026-07-25). P2's `presence_scope` in the event payload would make that
replay auditable rather than archaeological.

---

## 7. Open questions for the operator

1. **Hard-require `HERMES_HEAD_HOME` for destructive verbs?** Should
   `persona-instance reconcile` **error** without an explicit head (no
   ambient fallback at all), or keep today's soft refuse-and-report? Hard
   error is the more honest contract but breaks any existing muscle memory or
   script that runs the verb bare. *Recommendation: soft refuse through P4,
   hard error at P5 — by then the report tells the operator exactly what to
   set.*

2. **Should SessionDB identity survive a file copy?** Realm sync, profile
   clones, and backup restores all duplicate `state.db`. If identity is
   copied, a restored backup still matches its bindings (good) but two
   diverged copies claim the same identity (bad). If identity is re-minted on
   copy, a restore looks like a foreign DB and holds every row.
   *Recommendation: survive the copy* — the restore case is real and a copied
   DB genuinely contains the same sessions; the diverged-copy case is already
   an operator error the realm-sync lane owns. **Needs your call; it decides
   a test assertion in P4.**

3. **Surface "presence unverified" in Mission Control?** Costs one snapshot
   field plus a small MC chip. Benefit: the operator sees the 2026-07-25
   condition *before* running anything, rather than discovering it from a
   standby card afterwards. Currently scoped out (no launcher change).

4. **Is `hermesProfile != base` a supported launcher configuration?** D1's
   stale-cache bug is dormant only because the launcher defaults to `base`.
   If non-base profiles are supported, P1 is urgent; if they are
   dormant-by-policy, P1 is still correct but drops in priority.

5. **Schedule P6, or accept fingerprinting permanently?** P6 is the only path
   that stops the presence join from crossing two independently-resolved
   roots. Everything before it makes the cross-root read *safe*; only P6
   makes it *unnecessary*. It is deliberately unscheduled here.

6. **Backfill provenance for the ~10 restored bindings?** They were replayed
   through `open-chat` on 2026-07-25 and will be stamped automatically at
   P4 the next time they are opened — but until then they sit in the
   `presence_provenance_unknown` bucket and Phase 4 will not judge them. A
   one-shot `persona-instance stamp-provenance` verb is possible; it is also
   exactly the kind of ad-hoc write path this design exists to prevent.
   *Recommendation: no new verb — let `open_chat` backfill naturally.*

---

## Log

- **2026-07-26** — design written against `main @ f58d1be81`. Not
  implemented. Two latent defects (D1 `serve.py:222`, D2
  `continuity.py:54`) identified by code reading during the audit; neither
  live-proven; both folded into P1.
