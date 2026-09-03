"""The OUTBOUND half of the push edge (S2c, R-S2-15).

``gateway_announce`` is best-effort by design and this file is mostly about
what that means precisely, because "best effort" is the kind of phrase a reader
can take as "unfinished" instead of as a decision. Everything an announce
carries is also discoverable the slow way — the next hello refreshes a cache,
the next call to a revoked edge is refused — so the push buys latency and
nothing else depends on it landing. What it must NOT do is block the work that
triggered it, or reach a peer the rest of the runtime has written off.

The one ORDERING that is load-bearing gets its own test with a recording fake:
``peers revoke`` announces before it writes, because a peer we have already
revoked would be refused at our own door on the way back.
"""

from __future__ import annotations

import argparse
import json

import pytest

from agent_runtime import paths
from agent_runtime.gateway_announce import (
    ANNOUNCE_ATTEMPTS,
    ANNOUNCE_METHOD,
    AnnounceReceipt,
    announce_to_peers,
)
from agent_runtime.gateway_peers import (
    apply_peer_announce,
    read_peer_cache,
    record_peer,
    revoke_peer,
)

PEER_A = "inst_aaaaaaaaaaaa"
PEER_B = "inst_bbbbbbbbbbbb"


def _pair(root, peer_install_id=PEER_A, name="workstation"):
    record_peer(
        root,
        peer_install_id=peer_install_id,
        secret="f" * 64,
        display_name=name,
        endpoints=[{"host": "10.0.0.4", "port": 9000}],
        cert_fingerprint="ab" * 32,
    )


class _Recorder:
    """A ``call_peer_method`` stand-in that records every call in order."""

    def __init__(self, *, fail_times: int = 0, slow: bool = False) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.fail_times = fail_times
        self.slow = slow

    def __call__(self, root, peer_install_id, method, params, **kwargs):
        if self.slow:
            import time

            time.sleep(0.4)
        self.calls.append((peer_install_id, method, dict(params)))
        if self.fail_times > 0:
            self.fail_times -= 1
            return {"refusal": {"reason": "peer_unreachable", "message": "no answer"}}
        return {"result": {"accepted": True}}


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr("tools.agent_chat_remote.call_peer_method", rec)
    return rec


def test_outbound_announce_reaches_every_usable_peer_and_records_the_receipt(
    tmp_path, recorder
):
    _pair(tmp_path, PEER_A, "workstation")
    _pair(tmp_path, PEER_B, "laptop")

    receipts = announce_to_peers(tmp_path, {"display_name": "renamed"})

    assert sorted(r.peer_install_id for r in receipts) == sorted([PEER_A, PEER_B])
    assert all(r.ok for r in receipts)
    assert {call[1] for call in recorder.calls} == {ANNOUNCE_METHOD}
    assert all(call[2] == {"display_name": "renamed"} for call in recorder.calls)


def test_it_never_dials_a_peer_the_rest_of_the_runtime_has_written_off(
    tmp_path, recorder
):
    """It iterates ``usable_peers`` — the SAME predicate the resolver and the HUD
    read. A fan-out with its own definition of "reachable" would eventually dial
    an edge every other surface had already given up on, and an operator would
    see one machine described two ways."""

    _pair(tmp_path, PEER_A, "workstation")
    _pair(tmp_path, PEER_B, "laptop")
    revoke_peer(tmp_path, PEER_A)
    apply_peer_announce(tmp_path, PEER_B, {"revoked_you": True})

    receipts = announce_to_peers(tmp_path, {"display_name": "renamed"})

    assert receipts == []
    assert recorder.calls == []


def test_only_names_one_peer_so_a_revocation_is_not_broadcast(tmp_path, recorder):
    """Announcing "you are revoked" to every peer would tell four installs a
    fact about a fifth."""

    _pair(tmp_path, PEER_A, "workstation")
    _pair(tmp_path, PEER_B, "laptop")

    announce_to_peers(tmp_path, {"revoked_you": True}, only=[PEER_A])

    assert [call[0] for call in recorder.calls] == [PEER_A]


def test_a_failure_is_retried_once_and_then_recorded_rather_than_raised(
    tmp_path, monkeypatch
):
    """Two attempts, not one and not five: one is a single dropped packet away
    from silence, and more than two turns a courtesy into a retry storm against
    an install that is simply switched off."""

    _pair(tmp_path, PEER_A)
    rec = _Recorder(fail_times=1)
    monkeypatch.setattr("tools.agent_chat_remote.call_peer_method", rec)

    receipts = announce_to_peers(tmp_path, {"display_name": "x"})

    assert len(rec.calls) == 2
    assert receipts[0].ok is True

    rec = _Recorder(fail_times=99)
    monkeypatch.setattr("tools.agent_chat_remote.call_peer_method", rec)
    receipts = announce_to_peers(tmp_path, {"display_name": "x"})

    assert len(rec.calls) == ANNOUNCE_ATTEMPTS
    assert receipts[0].ok is False
    assert receipts[0].error
    # …and it landed in the cache through the SAME door a chat dial records
    # through, so "unreachable" means one thing across the runtime.
    assert read_peer_cache(tmp_path)[PEER_A].reachability == "unreachable"
    assert read_peer_cache(tmp_path)[PEER_A].unreachable_since


def test_a_revokes_own_announce_does_not_mark_the_peer_unreachable(
    tmp_path, monkeypatch
):
    """A peer we are cutting is about to be unusable anyway, and marking it
    unreachable on the way out would put a misleading word on the row an
    operator then reads: "we could not reach it" when what happened is "we threw
    it out"."""

    _pair(tmp_path, PEER_A)
    monkeypatch.setattr(
        "tools.agent_chat_remote.call_peer_method", _Recorder(fail_times=99)
    )

    announce_to_peers(tmp_path, {"revoked_you": True}, only=[PEER_A])

    cached = read_peer_cache(tmp_path).get(PEER_A)
    assert cached is None or cached.reachability == "unknown"


def test_a_slow_peer_never_delays_the_caller(tmp_path, monkeypatch):
    """``announce_in_background`` returns immediately and deliberately hands
    back no handle: a caller that could wait on this would eventually wait on
    it, and the whole point of pushing is that it costs the pusher nothing."""

    import time

    from agent_runtime.gateway_announce import announce_in_background

    _pair(tmp_path, PEER_A)
    monkeypatch.setattr(
        "tools.agent_chat_remote.call_peer_method", _Recorder(slow=True)
    )

    started = time.monotonic()
    assert announce_in_background(tmp_path, {"display_name": "x"}) is None
    assert time.monotonic() - started < 0.2


def test_an_unreadable_store_answers_with_no_receipts_rather_than_raising(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "agent_runtime.gateway_peers.usable_peers",
        lambda root: (_ for _ in ()).throw(OSError("gone")),
    )

    assert announce_to_peers(tmp_path, {"display_name": "x"}) == []


def test_the_receipt_renders_without_a_secret_in_it():
    receipt = AnnounceReceipt(peer_install_id=PEER_A, ok=False, error="no answer")

    assert receipt.payload() == {
        "peer_install_id": PEER_A,
        "ok": False,
        "error": "no answer",
    }


# ── the ordering (R-S2-15) ───────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def hermetic_runtime_root(tmp_path, monkeypatch):
    root = tmp_path / "agent-runtime"
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(root))
    resolved = paths.store_root().resolve()
    assert resolved == root.resolve() or root.resolve() in resolved.parents
    return root


def _dispatch(argv: list[str]) -> int:
    from hermes_cli import harness

    parser = argparse.ArgumentParser(prog="hermes")
    harness.build_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(argv)
    return args.func(args)


def test_revoke_announces_revoked_you_before_the_local_trust_write(
    capsys, monkeypatch
):
    """The order is load-bearing rather than tidy. The announce is a CALL to the
    peer, and a peer we have already revoked would be refused at our own door on
    the way back — so announcing first means the far install learns it was cut
    while the edge still works.

    Pinned with a fake that reads the STORE at call time: asserting "announce
    ran" would pass just as well if it ran second, which is the version of this
    feature that does not work.
    """

    from agent_runtime.gateway_peers import lookup_peer

    root = paths.store_root()
    _pair(root, PEER_A)
    seen: list[bool] = []

    def _observe(store_root, peer_install_id, method, params, **kwargs):
        record = lookup_peer(paths.store_root(), peer_install_id)
        seen.append(bool(record and record.revoked))
        return {"result": {"accepted": True}}

    monkeypatch.setattr("tools.agent_chat_remote.call_peer_method", _observe)

    code = _dispatch(["harness", "gateway", "peers", "revoke", PEER_A, "--json"])
    out = capsys.readouterr().out
    payload = json.loads(out[out.find("{") :])

    assert code == 0, out
    # The row was NOT yet revoked when the announce went out.
    assert seen == [False]
    assert payload["revoked"] is True
    assert payload["announced"] is True


def test_a_failed_announce_never_blocks_the_revoke_and_the_ack_says_so(
    capsys, monkeypatch
):
    """The posture, stated on the ack: an operator who believes a revocation was
    heard when it was not is the gap ``announced`` exists to close. The far side
    still learns at its next dial's refusal, exactly as it did before this edge
    existed."""

    root = paths.store_root()
    _pair(root, PEER_A)
    monkeypatch.setattr(
        "tools.agent_chat_remote.call_peer_method", _Recorder(fail_times=99)
    )

    code = _dispatch(["harness", "gateway", "peers", "revoke", PEER_A, "--json"])
    out = capsys.readouterr().out
    payload = json.loads(out[out.find("{") :])

    assert code == 0, out
    assert payload["revoked"] is True
    assert payload["announced"] is False
    assert "NOT reachable to be told" in payload["note"]


def test_no_announce_skips_the_call_entirely(capsys, monkeypatch):
    """For the offline case, and for an operator who must not contact that
    machine at all."""

    root = paths.store_root()
    _pair(root, PEER_A)
    rec = _Recorder()
    monkeypatch.setattr("tools.agent_chat_remote.call_peer_method", rec)

    code = _dispatch(
        ["harness", "gateway", "peers", "revoke", PEER_A, "--no-announce", "--json"]
    )
    out = capsys.readouterr().out
    payload = json.loads(out[out.find("{") :])

    assert code == 0, out
    assert rec.calls == []
    assert payload["announced"] is False
    assert payload["revoked"] is True


def test_a_dry_run_announces_nothing(capsys, monkeypatch):
    """A preview that announced would have told the far install about a
    revocation that never happened."""

    root = paths.store_root()
    _pair(root, PEER_A)
    rec = _Recorder()
    monkeypatch.setattr("tools.agent_chat_remote.call_peer_method", rec)

    code = _dispatch(
        ["harness", "gateway", "peers", "revoke", PEER_A, "--dry-run", "--json"]
    )
    out = capsys.readouterr().out
    payload = json.loads(out[out.find("{") :])

    assert code == 0, out
    assert rec.calls == []
    assert payload["announced"] is False
    from agent_runtime.gateway_peers import lookup_peer

    assert lookup_peer(root, PEER_A).revoked is False
