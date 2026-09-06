# D12 — the address the far side actually reached, carried instead of thrown away

**Status: hermes half BUILT 2026-09-05 (w17/ha). The launcher half — write it
first on the install's row, re-publish through R-D7 — is NOT built and is
deliberately gated on the D3 proof, so the two are never proven in the same
run.**

Successor to the launcher's `docs/mission_control/archive/dialable-addresses.md`
(D1–D11) on the hermes side. Filed by the operator 2026-09-04 ("definitely add
it in case we forget"); RULED 2026-09-05, approved as staged.

## 1. What was wrong with every address this install offers

Every candidate `harness gateway id` publishes today is an **inference**:

* **R-D8** reads the routing table for the `0.0.0.0/0` owner. Correct on the
  operator's PC, and correct only because someone parsed three platforms'
  command output by hand.
* **R-D2** connects a datagram socket to `1.1.1.1:53` and asks the kernel which
  interface it chose. Measured wrong once already — with Private Internet
  Access up, PIA installs `0.0.0.0/1` + `128.0.0.0/1` and never a default
  route, so *every* probe destination answered `10.97.7.100` and no probe could
  find the LAN.
* The launcher's dialler then re-orders the published list by shared subnet.
  A third inference on top of the first two.

Meanwhile the one **measured** answer exists on every successful pairing and is
discarded: when a far install's hello lands on this listener, the accepting
socket's `getsockname()` is the local address the kernel bound *that* connection
to. On a wildcard bind that is not the bind — it is the single interface the
packet actually arrived on. It is not a guess about which address works; it is
the address that just worked.

## 2. The hermes half, as built

One source, `agent_runtime/serve_socket.py::_reached_at(sock)`, read **once at
accept** (not lazily at greeting time — a TLS wrap or an admission refusal can
close the socket before the frame is built) and parked on
`SocketConnection.reached_at`. It never raises, and it refuses to answer rather
than guess: a socket that cannot answer, a non-IP sockaddr, and a wildcard or
empty host all yield `None`. That last exclusion is R-D1 held at a new door — a
bind address is not something anyone can dial, and passing `0.0.0.0` along here
would hand the far side exactly what D1 spent a wave removing from every
payload.

Three emit surfaces, all additive, all absent when there is nothing to say:

| surface | key | who reads it |
|---|---|---|
| `hello_ok` (`harness_parts/serve.py::_hello_ok_frame`) | `reached_at: {host, port}` | the far install, on the one frame it already reads |
| `socket_connections` rows (`SocketConnection.payload`) | `reached_at` | **this** install's own launcher |
| `gateway peers join` ack (`gateway_commands.py`) | `reached_at` | the joining operator, and the launcher's redeemer |

The connections row is the surface the row's own consumer needs and the row did
not name. R-D7 makes **this** install's launcher the party that writes the
address first and re-publishes it — and that launcher reads the `connections`
block, never a greeting addressed to somebody else. One fact, one source, three
readers.

The `peers join` ack copies the value off the `hello_ok` and never re-derives
it. `endpoints` on that row is what this side *dialled* — a candidate off a
list, correct only as far as the list was; `reached_at` is what the accepting
kernel says. Where they differ (a wildcard bind behind several interfaces, an
alias, a port-forward) the measurement is the one worth publishing. Absent when
the far side predates the field: never backfilled from `dialled`, because a
fabricated measurement is worse than none — it would look like proof and be an
echo of the guess.

Nothing here dials, probes, or announces. The sender-side self-probe the row
names as a cheaper sibling is **not** ruled and stays optional. mDNS/Bonjour
stays REJECTED (recorded 2026-09-04: a multicast responder in the agent
process, fails across VLANs, and the account already is the rendezvous).

## 3. What is loopback, and why it is still emitted

On the local socket lane `reached_at` is `127.0.0.1` — honest, and useless as a
published endpoint. It is emitted anyway, because the alternative is a producer
that decides for its consumers which measurements they are allowed to see. The
frame already carries `transport`, so the consumer's rule is one field away:
**promote a `reached_at` only from the `gateway` transport.** Stated here so
the launcher half does not have to re-derive it, and asserted in the loopback
test so the honest-`127.0.0.1` behaviour is pinned rather than incidental.

## 4. The launcher half — NOT built here

Gated on the D3 proof (run #7 is owed) so the measurement and its promotion are
never proven in the same run. Handed back verbatim by w17/ha; the launcher row
is the authority. In one paragraph: on a `peers join` ack (and on the
`connections` block for this install), when `reached_at` is present and came
off the gateway transport, write it as the **first** endpoint on that install's
row, dedupe the rest of the list behind it, and re-publish through the existing
`gateway_publisher` so R-D7's "the account's published list is the same list"
still holds — no new backend field, no second list. The far side's next dial
then starts at an address that has already carried a packet.

## 5. Tests

* `tests/agent_runtime/test_serve_socket_lane.py` —
  `test_the_greeting_reports_the_address_this_connection_actually_reached` and
  `test_the_connections_row_carries_the_same_reached_address`. Both red before
  the field existed (`KeyError: 'reached_at'`).
* `tests/agent_runtime/test_gateway_peer_two_roots_e2e.py` — the join ack's
  `reached_at`, asserted against the accepting root's real gateway port. Red
  first, on the real two-install ceremony over real sockets.
* `tests/agent_runtime/test_serve_gateway_lane.py` — the loopback-vs-gateway
  greeting equality test now compares `reached_at`'s **host** and drops its
  port. The host is the claim that test makes (bringing the gateway lane up
  must not change which address a loopback client reaches); the port is this
  boot's ephemeral listener, as boot-dependent as `socket` and `connection`
  already in that test's volatile set. Nothing was waived — the comparison got
  narrower where it was comparing two boots' port numbers to each other.
