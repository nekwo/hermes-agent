"""Stage 7's addressing grammar: ``@install/target`` (R4).

The property under test that matters more than any individual case is the first
one: an unqualified target is LOCAL, and the parser is where that is true.
"""

from __future__ import annotations

import pytest

from agent_runtime import gateway_targets as gt
from agent_runtime.gateway_peers import record_peer, revoke_peer


# ── the grammar ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    [
        "dev",
        "neko_supervisor",
        "@personainst_dev_agent_2",
        "",
        None,
        "  ",
        # A slash with no leading sigil is not a qualifier either — a target
        # that grows a slash for some other reason must not become remote.
        "team/dev",
    ],
)
def test_unqualified_targets_never_reach_the_peer_lane(value):
    assert gt.is_install_qualified(value) is False
    assert gt.parse_install_target(value) is None


def test_a_qualified_target_splits_into_its_two_halves():
    parsed = gt.parse_install_target("@workstation/dev")
    assert isinstance(parsed, gt.InstallTarget)
    assert parsed.install_ref == "workstation"
    assert parsed.target == "dev"
    assert parsed.spelling() == "@workstation/dev"


def test_an_install_name_may_contain_spaces():
    """`clean_display_name` collapses whitespace but keeps it, so the grammar
    has to survive a name an operator actually typed."""

    parsed = gt.parse_install_target("@tony laptop/qa")
    assert parsed.install_ref == "tony laptop"
    assert parsed.target == "qa"


def test_the_instance_sigil_is_accepted_both_ways_and_normalised():
    with_sigil = gt.parse_install_target("@workstation/@personainst_dev_agent_2")
    without = gt.parse_install_target("@workstation/personainst_dev_agent_2")
    assert with_sigil == without
    assert without.target == "personainst_dev_agent_2"


def test_only_the_first_slash_splits():
    parsed = gt.parse_install_target("@workstation/a/b")
    assert parsed.install_ref == "workstation"
    # Passed through WHOLE to the door that owns targets, rather than silently
    # truncated here by a greedy split.
    assert parsed.target == "a/b"


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("@/dev", gt.REASON_EMPTY_INSTALL),
        ("@workstation/", gt.REASON_EMPTY_TARGET),
        ("@workstation/@", gt.REASON_EMPTY_TARGET),
        ("@" + "x" * 300 + "/dev", gt.REASON_EMPTY_INSTALL),
        ("@workstation/" + "y" * 300, gt.REASON_EMPTY_TARGET),
    ],
)
def test_a_malformed_qualifier_refuses_rather_than_falling_back_to_local(value, reason):
    refusal = gt.parse_install_target(value)
    assert isinstance(refusal, gt.TargetRefusal)
    assert refusal.reason == reason
    assert refusal.ok is False


# ── resolution against peers.json ────────────────────────────────────────────


def _pair(root, *, install_id: str, name: str) -> None:
    outcome = record_peer(
        root,
        peer_install_id=install_id,
        secret="a" * 64,
        display_name=name,
        endpoints=[{"host": "127.0.0.1", "port": 9000}],
    )
    assert getattr(outcome, "peer_install_id", None) == install_id


def test_an_unknown_install_refuses_and_names_what_is_paired(tmp_path):
    _pair(tmp_path, install_id="install-b", name="workstation")
    parsed = gt.parse_install_target("@laptop/dev")
    refusal = gt.resolve_install_target(tmp_path, parsed)
    assert isinstance(refusal, gt.TargetRefusal)
    assert refusal.reason == gt.REASON_UNKNOWN_INSTALL
    assert "workstation" in refusal.message


def test_no_peers_at_all_points_at_the_ceremony(tmp_path):
    parsed = gt.parse_install_target("@laptop/dev")
    refusal = gt.resolve_install_target(tmp_path, parsed)
    assert refusal.reason == gt.REASON_UNKNOWN_INSTALL
    assert "gateway peers pair" in refusal.message


def test_a_display_name_resolves_to_its_row(tmp_path):
    _pair(tmp_path, install_id="install-b", name="workstation")
    parsed = gt.parse_install_target("@WorkStation/dev")
    resolved = gt.resolve_install_target(tmp_path, parsed)
    assert isinstance(resolved, gt.ResolvedInstallTarget)
    assert resolved.install_id == "install-b"
    assert resolved.display_name == "workstation"
    assert resolved.target == "dev"
    assert resolved.target_instance_id == ""


def test_an_install_id_resolves_too_and_outranks_a_colliding_name(tmp_path):
    """The id is the discriminator. A row whose DISPLAY NAME happens to equal
    another row's id must not be able to shadow it."""

    _pair(tmp_path, install_id="install-b", name="workstation")
    _pair(tmp_path, install_id="install-c", name="install-b")
    parsed = gt.parse_install_target("@install-b/dev")
    resolved = gt.resolve_install_target(tmp_path, parsed)
    assert isinstance(resolved, gt.ResolvedInstallTarget)
    assert resolved.install_id == "install-b"


def test_two_installs_with_one_name_refuse_with_both_ids(tmp_path):
    """Stage 6's field note #4 as a test: `display_name` defaults to the
    hostname, so two roots on one machine really do collide."""

    _pair(tmp_path, install_id="install-b", name="DESKTOP-QJ7DDV2")
    _pair(tmp_path, install_id="install-c", name="DESKTOP-QJ7DDV2")
    parsed = gt.parse_install_target("@DESKTOP-QJ7DDV2/dev")
    refusal = gt.resolve_install_target(tmp_path, parsed)
    assert isinstance(refusal, gt.TargetRefusal)
    assert refusal.reason == gt.REASON_AMBIGUOUS_INSTALL
    assert refusal.candidates == ("install-b", "install-c")


def test_a_revoked_edge_refuses_with_its_own_reason(tmp_path):
    _pair(tmp_path, install_id="install-b", name="workstation")
    assert revoke_peer(tmp_path, "install-b").revoked is True
    parsed = gt.parse_install_target("@workstation/dev")
    refusal = gt.resolve_install_target(tmp_path, parsed)
    assert isinstance(refusal, gt.TargetRefusal)
    # NOT `unknown_peer_install`: an operator acts differently on the two.
    assert refusal.reason == gt.REASON_PEER_REVOKED
    assert "install-b" in refusal.message


def test_an_instance_handle_travels_as_an_instance_id(tmp_path):
    _pair(tmp_path, install_id="install-b", name="workstation")
    parsed = gt.parse_install_target("@workstation/@personainst_dev_agent_2")
    resolved = gt.resolve_install_target(tmp_path, parsed)
    assert resolved.target == "personainst_dev_agent_2"
    assert resolved.target_instance_id == "personainst_dev_agent_2"


def test_a_revoked_row_is_not_offered_as_a_hint(tmp_path):
    _pair(tmp_path, install_id="install-b", name="workstation")
    revoke_peer(tmp_path, "install-b")
    parsed = gt.parse_install_target("@laptop/dev")
    refusal = gt.resolve_install_target(tmp_path, parsed)
    assert refusal.reason == gt.REASON_UNKNOWN_INSTALL
    assert "workstation" not in refusal.message


# ── S2 / S2c: the three ways an edge is dead, and one predicate ──────────────


def test_an_expired_row_refuses_with_peer_expired_and_a_revoked_you_row_with_its_own_reason(
    tmp_path,
):
    """Three reasons where there used to be one, because an operator acts
    differently on each: a revoked edge is a ceremony to re-run at both
    machines, an expired one is a credential to renew, and ``peer_revoked_you``
    is the FAR operator's decision — which no amount of work at this machine
    will fix. Before the announce edge existed, the third was indistinguishable
    from the far install being down."""

    from agent_runtime.gateway_peers import apply_peer_announce, record_peer, revoke_peer
    from agent_runtime.gateway_targets import (
        REASON_PEER_EXPIRED,
        REASON_PEER_REVOKED,
        REASON_PEER_REVOKED_YOU,
        TargetRefusal,
        parse_install_target,
        resolve_install_target,
    )

    record_peer(tmp_path, peer_install_id="inst_gone", secret="a" * 64, display_name="gone")
    record_peer(
        tmp_path,
        peer_install_id="inst_old",
        secret="b" * 64,
        display_name="old",
        expires_at="2000-01-01T00:00:00+00:00",
    )
    record_peer(tmp_path, peer_install_id="inst_cut", secret="c" * 64, display_name="cut")
    revoke_peer(tmp_path, "inst_gone")
    apply_peer_announce(tmp_path, "inst_cut", {"revoked_you": True})

    for name, reason in (
        ("gone", REASON_PEER_REVOKED),
        ("old", REASON_PEER_EXPIRED),
        ("cut", REASON_PEER_REVOKED_YOU),
    ):
        refusal = resolve_install_target(
            tmp_path, parse_install_target(f"@{name}/dev")
        )
        assert isinstance(refusal, TargetRefusal), name
        assert refusal.reason == reason, name


def test_the_hint_lists_usable_refs_only(tmp_path):
    """A suggestion the runtime prints must be one it would then accept. Listing
    a revoked name — or an ambiguous one — would hand an operator a spelling
    that fails for a second reason after they retype it."""

    from agent_runtime.gateway_peers import record_peer, revoke_peer
    from agent_runtime.gateway_targets import parse_install_target, resolve_install_target

    record_peer(tmp_path, peer_install_id="inst_live", secret="a" * 64, display_name="mac")
    record_peer(tmp_path, peer_install_id="inst_dead", secret="b" * 64, display_name="old")
    record_peer(tmp_path, peer_install_id="inst_dup1", secret="c" * 64, display_name="twin")
    record_peer(tmp_path, peer_install_id="inst_dup2", secret="d" * 64, display_name="twin")
    revoke_peer(tmp_path, "inst_dead")

    refusal = resolve_install_target(tmp_path, parse_install_target("@nobody/dev"))

    assert "mac" in refusal.message
    assert "old" not in refusal.message
    # The duplicated NAME is not offered; the two ids that would actually
    # resolve are.
    assert "twin" not in refusal.message
    assert "inst_dup1" in refusal.message and "inst_dup2" in refusal.message


def test_the_predicate_is_byte_identical_between_the_resolver_and_the_directory(
    tmp_path,
):
    """ONE predicate, three readers (R-S2-16). Before it, the resolver checked
    ``revoked``, the HUD listed everything and a tool would have invented a
    third rule — so an operator could see a peer in one place and be refused it
    in another with no way to tell which was right.

    Asserted both ways round: every usable id resolves, and every paired id that
    is NOT usable refuses.
    """

    from agent_runtime.gateway_peers import (
        apply_peer_announce,
        list_peers,
        record_peer,
        revoke_peer,
        usable_peers,
    )
    from agent_runtime.gateway_targets import (
        TargetRefusal,
        parse_install_target,
        resolve_install_target,
    )

    record_peer(tmp_path, peer_install_id="inst_live", secret="a" * 64, display_name="mac")
    record_peer(tmp_path, peer_install_id="inst_dead", secret="b" * 64, display_name="old")
    record_peer(
        tmp_path,
        peer_install_id="inst_old",
        secret="c" * 64,
        display_name="lapsed",
        expires_at="2000-01-01T00:00:00+00:00",
    )
    record_peer(tmp_path, peer_install_id="inst_cut", secret="d" * 64, display_name="cut")
    revoke_peer(tmp_path, "inst_dead")
    apply_peer_announce(tmp_path, "inst_cut", {"revoked_you": True})

    usable_ids = [peer.record.peer_install_id for peer in usable_peers(tmp_path)]
    assert usable_ids == ["inst_live"]

    for record in list_peers(tmp_path):
        outcome = resolve_install_target(
            tmp_path, parse_install_target(f"@{record.peer_install_id}/dev")
        )
        if record.peer_install_id in usable_ids:
            assert outcome.install_id == record.peer_install_id
        else:
            assert isinstance(outcome, TargetRefusal), record.peer_install_id


def test_resolve_install_ref_is_the_matcher_both_doors_share(tmp_path):
    """Two matchers would be two answers to "which machine is @mac", and the
    second would be discovered by an operator whose message went somewhere the
    roster said it would not."""

    from agent_runtime.gateway_peers import record_peer
    from agent_runtime.gateway_targets import (
        parse_install_target,
        resolve_install_ref,
        resolve_install_target,
    )

    record_peer(tmp_path, peer_install_id="inst_live", secret="a" * 64, display_name="mac")

    by_ref = resolve_install_ref(tmp_path, "mac")
    by_target = resolve_install_target(tmp_path, parse_install_target("@mac/dev"))

    assert by_ref.peer_install_id == by_target.install_id == "inst_live"
    # …and the id spelling reaches the same row, which is the ordering rule the
    # resolver's docstring states: an id always wins over a name.
    assert resolve_install_ref(tmp_path, "inst_live").peer_install_id == "inst_live"
