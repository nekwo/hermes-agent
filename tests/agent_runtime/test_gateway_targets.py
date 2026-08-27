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
