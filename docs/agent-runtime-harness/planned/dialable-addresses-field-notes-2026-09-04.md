# Dialable addresses — D1 field notes (hermes), 2026-09-04

Running record for stage D1 of `dialable-addresses.md`, written as the work
happened. Worktree `X:/wt/d1-dialable`, branch `feat/d1-dialable-addresses`,
from main `e6dbcbb40c`. Nothing here was run against `X:/Eternia/.hermes`
except one read-only `gateway id --json`, which is what §4 reports.

## 1. What was built, item by item

| Plan item | Commit | What landed |
|---|---|---|
| 1 — probe + R-D2 ordering | `a3bd4a311a` | `_DEFAULT_ROUTE_PROBE = ("1.1.1.1", 53)`; `_address_rank` sorts default route → RFC1918 on its /24 → other RFC1918 → other v4 → global v6; `MAX_CANDIDATE_ENDPOINTS` applied AFTER the sort |
| 2 + 3 — `_dial_host`, payload shape | `9055b85ec1` | `_dial_host` / `_dial_target`; `NO_DIAL_HOST_SENTENCE`; all four writers take the dial host; `endpoints` on `join_payload` and `qr_payload` |
| 4 — ordered dial in `peers join` | `073e4d0f3e` | candidate loop, `ServeCertificatePinMismatch` terminal, refusal names every address tried, row records the candidate that ANSWERED |
| 5 — R-D5 supersede | `81e1226dc7` | `supersede_pending` in `gateway_pairing_codes`, called from both mints before the cap is counted |
| 6 — R-D6 + `dial_host` | `54d0f40a51` | `emit_harness_error(reason=…)`, `reason` on every error on this lane, `gateway id --json` → `dial_host` |
| 7 — these notes | this commit | — |

No tool schema changed (these are CLI verbs, not agent tools), so the
tool-inventory JSON was not regenerated — which is what item 7 expected.

## 2. THE PLAN GOT §0 WRONG, and it changes D3's expectation

> §0: "`10.97.7.100` is a 'Local Area Connection' with no gateway … The
> default-route probe in `_machine_addresses` connects a datagram socket to
> `10.255.255.255`, which on this machine selects the 10.x interface, not the
> internet default route — so the LAN address is listed THIRD."

The first clause is where it goes wrong, and the second conclusion follows from
it. Measured on this PC, 2026-09-04, read-only:

```
Get-NetRoute 0.0.0.0/0        → ifIndex 12 (Wi-Fi, 192.168.1.203) via 192.168.1.1   [the ONLY default route]
Get-NetRoute ifIndex 24       → 0.0.0.0/1 via 10.97.0.1, 128.0.0.0/1 via 10.97.0.1, 10.97.0.0/16 on-link
Get-NetAdapter ifIndex 24     → "Local Area Connection" = Private Internet Access Network Adapter
ipconfig, that adapter        → "Default Gateway . . . :"   (blank)
```

"Local Area Connection" is a **full-tunnel VPN adapter**. It has no default
gateway *line in ipconfig* — which is how §0 concluded "no gateway" — because
PIA does not install `0.0.0.0/0`. It installs the split-default pair
`0.0.0.0/1` + `128.0.0.0/1`, which together cover every IPv4 address and are
strictly more specific than `0.0.0.0/0`. So every internet-bound packet on this
machine genuinely leaves through `10.97.7.100`, and the kernel is right to say
so.

Which means the probe destination was never the cause. Measured:

```
connect(("10.255.255.255", 1))  → 10.97.7.100
connect(("1.1.1.1", 53))        → 10.97.7.100     ← R-D2's own probe
connect(("8.8.8.8", 53))        → 10.97.7.100
connect(("9.9.9.9", 53))        → 10.97.7.100
connect(("192.168.1.1", 53))    → 192.168.1.203
```

R-D2's premise — *"the datagram probe connects to a public unicast address so
the kernel names the interface that carries the default route"* — cannot
distinguish a LAN from a full-tunnel VPN, because with the VPN up there is no
destination that follows `0.0.0.0/0`: the two `/1`s cover the whole space. The
probe answers "which address carries internet traffic", correctly, and on this
machine that is a tunnel address no LAN peer can dial.

**So the D1 verification line does not hold on this PC while PIA is connected.**
`gateway id --json` prints `10.97.7.100` first, not `192.168.1.203`. §4 has the
real output. I did not bend the ordering to make it pass; see §3 for the two
ways I could have and why neither is a rule worth writing.

What the change DID buy, measured against §0's own numbers:

```
before  [10.97.7.100, 25.3.92.221, 192.168.1.203, 2620:9b::1903:5cdd]   LAN third
after   [10.97.7.100, 192.168.1.203, 25.3.92.221, 2620:9b::1903:5cdd]   LAN second
```

The LAN address moved up because rank 2 (RFC1918) beats rank 3 (everything else
v4 — the `25.3.92.221` Hamachi-class row). And with R-D3's list dial in
`peers join`, second is enough: the Mac now walks past `10.97.7.100`, fails it,
and lands on `192.168.1.203`. **The pair edge should close on D3 anyway.** What
will be wrong is R-D4's LABEL — the sheet will read `Windows PC (10.97.7.100)`
and `Listening on 10.97.7.100:8765 · all interfaces` until either PIA is
disconnected or a further ruling lands. The launcher builder and the operator
should both know that before D3 is read as a failure.

Two smaller §0 corrections while I was in there: `25.3.92.221` does have a
gateway (`2620:9b::1900:1`, v6), and the 4-address cap was itself a second,
unnamed defect — see §3.

## 3. Two heuristics I measured and did NOT ship

**(a) An mDNS-address probe.** `connect(("224.0.0.251", 5353))` answers
`192.168.1.203` on this machine, repeatably, where `224.0.0.1` and `224.0.0.2`
answer `10.97.7.100`. Tempting, and wrong to ship: there is no route entry that
explains it (`Get-NetRoute` shows one `224.0.0.0/4` per interface, all metric
256), so the selection is almost certainly a side effect of an mDNS responder
having joined that group on the Wi-Fi adapter. A rule whose correctness depends
on whether Bonjour is running is not a rule.

**(b) A subnet-size test.** The LAN interface is attached to a `/24`
(`192.168.1.0/24`) and the VPN to a `/16` (`10.97.0.0/16`), and that IS probeable
with stdlib: probe `A`'s own `/24` neighbour and its `/16` neighbour and see
whether the kernel still answers `A`. On this machine it discriminates cleanly.
But "the smaller subnet is the real LAN" is a heuristic dressed as a rule — it
ranks a corporate `10.0.0.0/16` LAN below a VPN that hands out `/24`s — and it
is a new ruling, not an implementation of an existing one. Recorded here as the
candidate for whoever writes the next ruling; not built.

**What I did fix beyond the letter of item 1**, because it is the same defect
and needs no ruling: `MAX_CANDIDATE_ENDPOINTS` (4, the peer row's cap) was
applied to the DISCOVERY order. On a machine with five virtual adapters
enumerated ahead of the LAN one, the only reachable address is truncated away
entirely — the list is capped, so a bad order is not merely a bad first guess,
it can delete the answer. The cap is now applied after the sort, with a test.

## 4. The read-only proof on this machine

Run from the worktree, against the operator's live store, read-only. `hermes`
on PATH resolves to the editable install rooted at `X:/Eternia/hermes-agent`
(the primary checkout), so I did **not** use it — the command below runs the
worktree's own code as a module with `PYTHONPATH` pinned to the worktree, which
`sys.path[0]` and the explicit env both guarantee:

```
cd X:/wt/d1-dialable
HERMES_HOME=X:/Eternia/.hermes/profiles/base \
HERMES_AGENT_RUNTIME_ROOT=X:/Eternia/.hermes/agent-runtime \
PYTHONPATH=X:/wt/d1-dialable \
  /c/Users/beast/.venvs/hermes-test/Scripts/python.exe -m hermes_cli.main harness gateway id --json
```

```json
"dial_host":        {"host": "10.97.7.100", "port": 8765},
"endpoints":        [10.97.7.100, 192.168.1.203, 25.3.92.221, 2620:9b::1903:5cdd]  (all :8765),
"endpoints_source": "live",
"listener":         {"host": "0.0.0.0", "port": 8765, "source": "live"},
"install_id":       "bbdb8120-575d-4890-85e7-ecdbd650cde0"
```

`dial_host` is present and is `endpoints[0]`; `listener` still reports the bind,
which is deliberate (§5). The expected first row is `192.168.1.203` and it is
second, for the reason §2 measures. Nothing was written: `gateway id` is
read-only by contract, and the run left `install.json` at its 2026-08-27 stamp.

## 5. Decisions inside the code worth naming

* **`listener` still reports the bind.** `0.0.0.0` is the honest answer to
  "what is this listener on"; it is never the answer to "what should another
  machine dial". Splitting them into `listener` and `dial_host` is what lets
  R-D4's sentence be true without hiding the wildcard from the operator who
  chose it.
* **The `unknown` endpoint source is not refused by `_dial_target`.** R-D1's
  refusal fires only for `live`/`config` with nothing enumerated. `gateway pair`
  and `peers pair` keep minting with `LISTENER_OFF_SENTENCE` as a NOTE, which
  is the pre-existing and correct behaviour — a code minted before the first
  boot is a legitimate thing to have — while `introduce` keeps refusing it,
  because its consumer is a machine. The plan's item 2 says exactly this; it is
  written down here because the two verbs disagreeing looks like a bug until
  you know it is a decision.
* **`reason` is omitted, not `null`, when a caller passes none.** An
  unconditional key would change the bytes of every error envelope this harness
  emits, and `tests/fixtures/response_envelopes/*.json` pins those bytes (with
  mirrors in the Launcher). The contract the launcher reads —
  `error.reason` beside `error.code` on this lane — is unaffected, and
  `test_response_contract_fixture.py` stayed green without regeneration.
* **`ServeCertificatePinMismatch` is a subclass**, so every existing
  `except ServeHelloProtocolError` arm and `pytest.raises(..., match="pinned
  fingerprint")` stays true. It names a condition that was already raised.
* **The stored row holds the candidate that ANSWERED**, not the payload's
  first. Recording an address the loop walked past would make every later dial
  from this install begin with a failure this run already proved.
* **Supersede runs before the cap is counted.** That ordering is R-D5, not an
  implementation detail: counting first would refuse the retry on the codes the
  first attempt minted, which is the whole defect.

## 6. Verification

```
bash scripts/run_tests.sh \
  tests/hermes_cli/test_gateway_introduce_verb.py \
  tests/agent_runtime/test_gateway_peers_store.py \
  tests/agent_runtime/test_gateway_peer_two_roots_e2e.py \
  tests/agent_runtime/test_serve_gateway_auth.py \
  tests/hermes_cli/test_gateway.py
```

plus the three files this lane also touches, which the plan's command does not
name and which hold most of the new assertions:
`tests/hermes_cli/test_gateway_peer_verbs.py`,
`tests/hermes_cli/test_gateway_pairing_verbs.py`,
`tests/agent_runtime/test_response_contract_fixture.py`.

Counts are in the final report. The two-roots e2e gained
`test_a_join_walks_past_an_unroutable_first_candidate_and_lands_on_the_second`,
which is the only place R-D3 is proved against two real serves: A's real
payload, doctored to advertise `192.0.2.1` (TEST-NET-1) first with `host`/`port`
pointing at it too, and the edge still lands on `127.0.0.1:<A's port>`.
