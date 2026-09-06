# Field notes — the realm-switch field defect, hermes half, 2026-09-01

Running record. The launcher half — where the defect and both fixes live — is at
`EterniaLauncher/docs/mission_control/archive/iws-fix-realm-switch-field-notes-2026-09-01.md`
(two files, split by repo, not by subject). Read that one for the defect; this
one records what hermes was asked, what it answered, and the two tests that
turned the answer into a fact.

Baseline: hermes `a6274094d5`, branch `fix/ws2-realm-switch-scope`, worktree
only — the primary checkout shares an index with concurrent sessions and was
never written.

---

## 1. The question put to hermes

The operator reported that a realm switch landed the office on the wrong scene.
Two of the four suspects were hermes-side:

* **(c)** the RPC lane threads `issued_at` into `activate_realm`, which performs
  TWO `set_active` writes under one basis — the realm pointer's, then
  `reconcile_active_workspace_to_realm`'s on the workspace pointer. If the
  compare-and-set arms declined the second as `superseded`/`duplicate`, the
  active workspace would be left pointing into the realm the operator just left.
* **(d)** the realm rows might not carry `default_workspace_id`, leaving the
  launcher's fallback chain null-null.

---

## 2. The answer, from the live store

`X:/Eternia/.hermes/agent-runtime`, read before touching anything:

```
active_realm.json      {"realm_id": "realm_default",  "intent_issued_at": "2026-09-01T12:45:44.052220Z"}
active_workspace.json  {"workspace_id": "ws_default", "intent_issued_at": "2026-09-01T12:45:44.052220Z"}
```

Both pointers moved, under ONE basis, into the same realm. The reconcile ran and
was not declined. **(c) is not real.**

The launcher's diag log agrees from the other side: three
`[MissionScopeUse] lane=rpc capability=realm.use applied=true reason=none`, each
followed within ~250 ms by `[MissionFold] fold_applied 2 of 2 rows (scope x2)`.
Two scope rows per realm switch is correct — one per `set_active`, emitted at the
store chokepoint, and the SECOND is the settled pair.

(d) is factually true and harmless: `realms/cf6d244d-….json` (the operator's
"test realm") genuinely has `default_workspace_id: null`, while
`realms/realm_default.json` has `ws_default`. That is a real asymmetry the
launcher's `resolve` handles by design — the pointer is what refills the
selection — and it only became visible because the launcher was refusing the
frame the pointer arrived on. Nothing to change here.

**hermes needed no production change.**

---

## 3. Two tests added, both closing a gap between covered and fielded

`tests/agent_runtime/test_scope_use_methods.py`:

1. `test_a_STAMPED_realm_switch_still_reconciles_the_workspace_under_it` — the
   existing reconcile test calls the verb with NO `issued_at`, and the fielded
   lane always stamps (`mission_scope_use_client`'s `_scopeIntentIssuedAt`). So
   the shape that actually runs — one basis threaded into two pointer files —
   had no coverage. It passes, which is what makes "(c) is not real" a fact
   rather than a reading of the code.
2. `test_a_realm_switch_emits_a_scope_patch_whose_LAST_row_carries_both_new_pointers`
   — the wire shape the launcher's fold consumes, pinned at the producer. The
   first of the two rows is mid-reconcile and names a workspace in the realm the
   operator just LEFT; only the second is settled. The launcher folds them in
   order and the last one wins, so the claim is about ORDER and about the final
   row's contents. A producer that emitted them the other way round, or dropped
   the second, would leave every fielded client holding a straddled scope with no
   receipt anywhere. Asserted non-vacuously: the two rows are checked to differ.

No mutation claims were added — no new hermes logic was written, only tests over
logic that already exists and is already claimed.

---

## 4. Filed, not fixed: a latent supersede hole in `activate_realm`

`activate_realm` writes the realm pointer, then hands the SAME basis to
`reconcile_active_workspace_to_realm`. If the workspace pointer already carries a
NEWER basis — an explicit `workspace use` that landed between the realm gesture
being stamped and being delivered — the reconcile's `set_active` declines as
`superseded`, the realm pointer has already moved, and the two pointers straddle
realms. `activate_realm` does not check, and its row still answers
`applied: true`.

This is NOT the field defect (the live store shows both pointers landed
together), and closing it means deciding what a realm switch should do when a
newer workspace intent contradicts it — keep the newer workspace and refuse the
realm? move the realm and report the straddle? Both are rulings about supersede
semantics, not a field fix. Left open here rather than guessed at.

---

## 5. What was run

| suite | result |
|---|---|
| `test_scope_use_methods` | **30 passed** (28 before, +2 added) |
| `test_scope_use_methods` + `test_scope_patch_coverage` + `test_scope_consistency` + `test_workspace_scope` + `test_scope_use_serve_acceptance` | **81 passed** |
| `pytest tests/agent_runtime -k "store or activation or realm or workspace"` | **867 passed**, 6433 deselected, 8:52 |

System `python -m pytest` throughout. The mutation gate was not run — it shares
no tree with pytest by rule, and there is no new hermes logic for it to claim.
