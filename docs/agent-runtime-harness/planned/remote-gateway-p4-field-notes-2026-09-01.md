# Field notes — P4, cross-install media (hermes half)

Written 2026-09-01 by the OPUS lane executing stage **P4** of the launcher plan
`docs/mission_control/planned/remote-parity-and-two-machine-proof.md`, consuming
ruling **R-P3**. Branch cut from hermes `0c84b3bd4f` (the dispatch pin
`c1de3b2d84` had been superseded on `origin/main` by two docs commits; the newer
tip was taken per the "or newer" clause). Launcher half rides the same landing
window on `p4-cross-install-media-fixtures` cut from launcher `ee1333eb0`.

Not a plan and not canon: what this session actually READ, where the brief and
the code disagreed, and what the build cost that the plan did not price.

---

## What the brief said, and where the code said otherwise

**§P4's hermes bullet 1 names the wrong file for the mint, and the correction is
the design.** The brief says:

> `tools/agent_chat_dispatch.py` (the remote leg, S7c's owner): after B's turn
> completes, scan the reply for `MEDIA:` references and mint handles ON B.

`_run_remote_dispatch` runs on **A**. It is A's supervisor thread dialling B; it
reads B's reply off B's per-request frame lane and writes A's completion row.
Install A cannot mint a single one of those handles — a handle is
`sha256(bytes)` and the bytes are on a disk A has no path to. So "mint here"
and "mint ON B" are the same sentence naming two different machines.

The mint had to move to the only code that runs on B *and* holds the reply:
**B's `harness mission-chat message` handler**, which is what
`peer.agent_chat.execute` spawns (`chat_turn.normalize_peer_chat_execute` builds
that argv). The map then rides home on the channel that already exists — the
child's JSON payload, streamed as `line` frames, parsed by A through
`parse_child_payload` — so R-P3's "the map rides the dispatch completion" is
honoured with **no second verb**, which the ruling's one-new-verb budget
required. `_run_remote_dispatch` still owns the *carry*: it reads
`payload["media"]` and passes it to `record_completion`. That is one line, and
it is the line the brief was reaching for.

**Consequence the plan did not price: the producer edit lands in
`hermes_cli/harness_parts/persona_commands.py`.** Five reply-carrying payload
sites, each already calling `_stamp_turn_visibility(data, reply_text)` — so the
stamp pattern existed and the new `_stamp_reply_media(data, reply_text, args)`
sits beside it at all five. The mint is **gated on the peer origin**
(`--requested-by peer:<install id>`, set by the normaliser from a connection
whose HMAC verified, and unforgeable from params). A local turn mints nothing
and its payload is byte-identical to before this stage: on this machine the
`MEDIA:` path already resolves, so hashing every local reply's images would
spend real I/O to write a field with no reader — the master prompt's
placeholder-architecture rule, applied to a field rather than a class.

**§P4's pin enumeration over-counts.** The brief says grepping
`runtime.persona.prewarm` "enumerates all 8 pin files across both repos". It
enumerates 8 hermes files and 3 launcher ones, but only **four** hermes files
carry a full method list that a new verb must join
(`test_serve_rpc_office.py`, `test_serve_rpc_office_subscribe.py`,
`test_serve_rpc_office_upsert.py`, and — as an integer/tier assertion —
`test_serve_rpc_method_tiers.py`). `agent_retire.py` and
`test_agent_retire_service.py` mention prewarm in PROSE about level-mutating
verbs; `peer.media.get` is not one, so neither moved.

**And the grep MISSES two pins that the suite caught.** Both are literal
`PEER_METHOD_ALLOWLIST` sets, and neither contains the word `prewarm`:

- `tests/agent_runtime/test_peer_authorization.py:147`
- `tests/agent_runtime/test_peer_chat_execute.py:83`

Two independent pins reddened on the widening, from opposite ends (the set
itself, and the set as the dispatcher's own authorize walk answers it). That is
the design working exactly as `PEER_METHOD_ALLOWLIST`'s comment promises — but
an executing lane that trusted the enumeration would have found them by running
the suite rather than by reading the brief. Recorded so the next cross-stack
landing greps `PEER_METHOD_ALLOWLIST` as well as `runtime.persona.prewarm`.

---

## What was built

**One new verb, as ruled.** `peer.media.get` (`agent_runtime/serve_rpc.py`),
`tier=console`, added to `PEER_METHOD_ALLOWLIST` with its reason. It is a
keyhole, not a door: it resolves the **local** half of the media scope only —
spelled as an argument, `build_media_scope(remote_completions=())`, rather than
left to a later check — so a handle this install holds only as a REMOTE row is
`unknown_handle` to a peer rather than a second proxy hop. There is no
`peer.media.index`: a peer can spend a handle it was given and enumerate
nothing, which is Stage 8's reference-out/handle-in asymmetry applied one
boundary further out.

**Two artifact kinds, not one with a nullable path.**
`media_handles.RemoteMediaArtifact` carries `{handle, reference,
peer_install_id, media_type, size_bytes}` and no `path`, because there is no
file on this disk. `read_artifact_bytes` therefore takes the local kind and
only the local kind — "read the bytes of a row that has none" does not
typecheck, rather than being a branch somebody has to remember.

**Every field of a peer's map is re-derived on arrival, never trusted.**
`remote_artifacts_from_completions` re-checks the handle grammar, re-checks that
the reference is absolute with an allowlisted image extension, and re-derives
`media_type` from that extension. Trusting the peer's `media_type` would let a
paired install put a credential into this install's namespace under
`image/png` — the exact exfiltration the extension allowlist exists to make
unrepresentable. The bound is enforced by the receiver too
(`dispatch_store.MEDIA_MAP_LIMIT`), because a bound only the sender applies is
not a bound.

**The proxy verifies and caches.** `agent_runtime/media_proxy.py`: cache first,
then one dial, then the returned bytes re-hashed against the handle before
anything is served or cached. Content addressing makes that verification free
and makes the cache need no invalidation protocol — the key IS the digest. A
peer that returns the wrong bytes gets `unknown_handle` and **nothing cached**,
so one lie cannot poison the namespace.

**The map is on the ROW; the event carries a COUNT.** `record_completion`'s
`media=` rides `result_json` beside `visibility` and `remote`; the
`dispatch.completed` event gains `media_count` and never the map. Sixteen
absolute Windows paths plus 71-character handles is kilobytes and the EventLog
payload cap is 4096 bytes, so this is the cap honoured by construction rather
than by hoping a map stays small (asserted, at the limit, in the new suite).

**`index` states `remote` on every row.** Local rows gained `"remote": false`
rather than remote rows being distinguishable by an absent key — `peer.ping`'s
rule that a client must never read a fact out of an absence. The launcher's
decoder (`mission_media_rpc.dart`) ignores unknown keys, so this is additive on
that side; hermes' own producer pin in `test_serve_rpc_media.py` moved
deliberately, which is what makes the shape change visible.

---

## Honest gaps

1. **Still one machine.** Stage 1's inherited gap, unchanged: both listeners in
   the acceptance bind loopback. "Install A reached install B across a LAN"
   stays unproven until the O2 session runs on two boxes. What IS proven is two
   isolated roots, two processes that cannot read each other's disks, real TLS
   with a real pin, and a real HMAC over a real paired credential.
2. **The dispatch that produces the map is synthesised in the acceptance.** A
   real cross-install reply needs a provider turn on B, which no test may
   depend on. The e2e writes onto A's store exactly the row
   `_run_remote_dispatch` writes; that supervisor's own write of it is pinned
   separately, on the real payload shape, by
   `test_cross_install_media.py::test_the_remote_leg_puts_the_far_installs_map_on_the_row`.
   The join between those two halves is the seam this stage did not prove
   end-to-end, and O2 step 4 is where it closes.
3. **The proxy dials INLINE on the serve reader loop**, because
   `runtime.media.get` does. A remote handle whose peer is switched off stalls
   that loop for `media_proxy.PEER_DIAL_TIMEOUT_SECONDS` (5 s, deliberately a
   third of the dispatch lane's, which can afford to wait on a supervisor
   thread) before answering `peer_unreachable`. A LOCAL handle is unaffected and
   the cache means any given picture is proxied at most once ever. Moving the
   media family off the reader loop is a real follow-up and is filed as one
   rather than half-done here.
4. **A→B→C is refused, not routed.** By design (see the keyhole above). An
   operator on A cannot reach an artifact B learned from C. Nothing has asked
   for it; recorded so it is a decision rather than an omission.
5. **`peer.media.get`'s scope is the far install's WHOLE local media scope**,
   not "the artifacts of the turn this peer asked for". That is the reachability
   rule the family already states, and it is wider than it strictly needs to be
   for this lane: B answers for anything its own chat mirror declares, to any
   paired install, given a handle. It is not enumerable and a handle cannot be
   guessed, so the practical reach is "what B already told A about" — but the
   narrower scope (per-dispatch) would need a second registry, which is the
   thing Stage 8 refused on purpose. Recorded, not fixed.
6. **No Stage C shot.** Unchanged from Stage 8's gap 6, and the launcher half
   of this landing is fixtures and pins only, so nothing here could have taken
   one.

---

## Environment findings (cost this session real time; recorded for the next)

- **The launcher's serve-frame generator needs `--python` pointed at the live
  venv in this environment.** Its default interpreter resolution lands on
  `C:\Python312\python.exe`, which imports `yaml` only from the per-user site —
  and the generator's sandbox redirects `APPDATA`/`HOME`/`USERPROFILE`, so the
  spawned child dies on `ModuleNotFoundError: No module named 'yaml'`. Its
  stderr is `DEVNULL`, so what the operator sees is
  `error: the serve child closed stdout; frames so far: []`, which names
  nothing. Working invocation:
  `python tool/hermes_serve_frames/generate.py --check --hermes-root <wt> --python X:/Eternia/.hermes/venvs/hermes-agent/Scripts/python.exe`.
- **pytest and serve want different interpreters here.** The live venv has no
  `pytest` (so suites run under `C:\Python312\python.exe`); the system
  interpreter cannot boot a sandboxed serve (above). Both are true at once and
  neither is a repo defect.
