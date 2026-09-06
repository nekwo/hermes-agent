# Agent Runtime Harness — Master Index

> **What this system is.** The Hermes-native persona runtime behind Mission
> Control. **Chat is the only lane**: an operator (or another agent) messages an
> on-level persona instance's chat root; the runtime owns identity, chat
> continuity, the office scene, the board, realms/workspaces, and an
> enforcement-free agent graph. The goal/task mission lane (daemon, stage graph,
> proof gates, role gating) was removed 2026-07-30 — what remains is documented
> here, and only what remains.

Consolidated 2026-08-22 from 56 files. The rules that keep it consolidated are
at the bottom; read them before adding a document.

## The nine domains

Read in this order for a full picture; each stands alone for its own territory.

| Doc | Domain — the question it answers |
| --- | --- |
| [01 — System architecture](01-system-architecture.md) | What the entities ARE: persona template → durable instance → chat root → scene actor; the agent graph; personas and profiles as data; the realm-sync lane (canonical home since 2026-09-03); what the mission-lane removal deleted. |
| [02 — Runtime data and shapes](02-runtime-data-and-shapes.md) | Where state lives: the store layout, SessionDB, the snapshot core and its sections, the core cache and its fingerprint model, event-log offsets and rotation — and the honest status of the O(world) build. |
| [03 — Transport and wire](03-transport-and-wire.md) | How shapes leave the process: the serve, the stream and its fold negotiation, patch frames, the RPC lane, the PUSH-vs-RPC fork boundary, and the additive-only wire rule with its cross-repo byte pin. |
| [04 — Boot and lifecycle](04-boot-and-lifecycle.md) | What happens between spawn and authoritative, stage by stage, each with its receipt: boot timeline, prewarm, the first core build, the cache consult, shutdown. |
| [05 — Chat turn lane](05-chat-turn-lane.md) | One chat turn end to end: admission and its guards, the turn-phases contract, model selection cascade, tool-access posture, MCP admission, envelope grants, durability, budgets, the create path, provider dispatch. |
| [06 — Office and board](06-office-and-board.md) | The scene surface: office write verbs on the RPC lane, folds, optimistic rendering vs snapshot truth, drops and their timings, the board's (unmigrated) lane. |
| [07 — Observability](07-observability.md) | How the system is measured: the honesty contract (canonical home), the receipt census (emitter → format → consumer), the audit tools, the zero-scan lesson. |
| [08 — Performance and debt ledger](08-performance-and-debt-ledger.md) | The numbers: live baselines with dates and sources, landed optimizations with shas, the value-ordered burn-down, the open debt register. |
| [09 — Multi-device runtime](09-multi-device-runtime.md) | One account, many machines, one runtime per machine: the target picture, the three tiers, the entities a runtime holds (install, runtime, paired device, paired peer, peer directory), the credential doors and their membership sets, code-free pairing by account grant, dialable addresses, cross-install chat and reads, the runtime that outlives its launcher — and the ledger of what landed. The launcher half is `EterniaLauncher/docs/mission_control/10-multi-device-architecture.md`. Added 2026-09-06: the material was spread over 01/03/04 and eleven plans, and none of them said what the whole was for. |

## planned/ — designed, not implemented

Everything in [planned/](planned/) is a design that has NOT shipped. One file
per plan, each carrying its evidence and the gate that opens the work. The
domain docs link into this folder from their `## Open rows`; nothing in a
domain doc describes unbuilt behavior. When a plan ships, its file's content
moves into the owning domain doc as verified truth and the planned file leaves
`planned/` in the same commit — **deleted** when nothing in it outlives the
fold-in, **moved to `archive/`** under a sha-stamped header when it carries
evidence (measurements, falsified assumptions, rejected designs) that the domain
doc states the conclusion of rather than the proof. Field notes are always the
second kind. A plan whose header still says "not built" after it shipped is
corrected BEFORE it moves, so the archive never receives a document that lies
about the code it shipped (operator ruling, 2026-08-30).

## archive/ — history, not truth

[archive/2026-08-22-pre-consolidation/](archive/2026-08-22-pre-consolidation/)
holds all 56 pre-consolidation files unchanged (`git log --follow` preserves
their history). Each domain doc's `## Supersedes` names the archived files it
replaces. Archived docs are quotable as history only — several were verified
stale at consolidation time, and every claim worth keeping was re-verified
against code before it entered a domain doc. If an archived claim is not in a
domain doc, treat it as unverified or false until re-proven.

`archive/` also holds **shipped plans and their field notes**, moved out of
`planned/` at the landing that finished them (first wave 2026-08-30). The
sentence above governs them identically — the domain doc and the code are the
truth. Each one names its shipping shas in its header, and where a plan's own
findings were later measured false, the correction is stamped in the section
that carried the claim rather than only at the top.

## Not in this tree

- `harness-skills/` — live installed source (tests read it), not prose.
- `upstream-prs/` — PR material for the upstream boundary.
- The launcher's own docs — `EterniaLauncher/Launcher_Brain/` (its latency
  initiative docs are cited from 07/08 where their numbers are load-bearing).
- General Hermes CLI docs — the rest of `docs/` (gateway, billing, middleware,
  mcp-expansion) is not Mission Control canon.

## The rules that keep this consolidated

1. **A domain doc states implemented, verified truth only.** Every factual
   claim carries a code anchor (`path:line` or named symbol), a quoted receipt,
   or sits under an explicit `## Unverified carry-forward` naming its source.
2. **Unimplemented work goes in `planned/`, one file per plan** — evidence and
   opening gate included. Domain docs link; they do not inline.
3. **No new root files.** New knowledge folds into the owning domain doc (or a
   planned file). A dated plan/audit/scout file at the root is the sprawl this
   consolidation removed; if a document cannot be folded, the domain partition
   is wrong and should be re-argued instead of bypassed.
4. **Staleness is the enemy.** When code moves, the domain doc moves in the
   same change set, or the claim is deleted. An anchor that no longer supports
   its sentence is a defect, not a nuisance.
5. **The wire rule rides above all docs:** observability lands as log receipts,
   never as new keys on the parity envelope — byte-pinned goldens on both repos
   enforce it (see 03 and 07).
