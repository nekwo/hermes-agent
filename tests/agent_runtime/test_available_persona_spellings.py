"""The persona spellings ``_available_persona_summary`` may advertise.

The summary mints one row per profile template with
``persona_id = "profile:<name>"``, and the launcher's Presets lane carries that
id verbatim into ``runtime.agent.create``. But ``profile:<name>`` is not
uniformly accepted: ``agent_create.accepted_persona_spellings`` — the ONE
authority for "how may an operator name this row" — withholds it for a profile
that two personas declare, because the CLI resolver's synthesis lane inherits
nothing from an ambiguously-owned profile and would mint a defaults-less agent
under a name that reads like the persona beside it.

So each row carries ``persona_spellings``: the spellings ``--persona`` actually
accepts, routed through that authority rather than re-derived here.
"""

from agent_runtime import snapshot as snapshot_mod


class _Template:
    def __init__(self, name, description=""):
        self.name = name
        self.description = description


class _Agent:
    def __init__(self, persona_id, profile):
        self.id = persona_id
        self.hermes_profile = profile


def _rows(monkeypatch, templates, agents):
    monkeypatch.setattr(
        snapshot_mod, "available_profile_templates", lambda: list(templates)
    )
    return {row["persona_id"]: row for row in snapshot_mod._available_persona_summary(agents)}


def test_an_ambiguously_owned_profile_withholds_the_profile_spelling(monkeypatch):
    """Two personas declare one profile: ``profile:<name>`` parses but inherits
    nothing, so it is NOT among the spellings the row advertises. The owner ids
    are, because each of them names the agent it actually mints."""
    rows = _rows(
        monkeypatch,
        [_Template("shared")],
        [_Agent("alice", "shared"), _Agent("bob", "shared")],
    )

    row = rows["profile:shared"]
    assert "profile:shared" not in row["persona_spellings"]
    assert row["persona_spellings"] == ["alice", "bob"]


def test_a_uniquely_owned_profile_advertises_both_spellings(monkeypatch):
    """One owner: the bare id and the profile spelling both resolve to the same
    persona's defaults, and the bare id comes first (the authority's order)."""
    rows = _rows(monkeypatch, [_Template("solo")], [_Agent("alice", "solo")])

    assert rows["profile:solo"]["persona_spellings"] == ["alice", "profile:solo"]


def test_an_unowned_profile_advertises_the_synthesis_spelling(monkeypatch):
    """Decision D-U1: a profile no persona declares is the template-only
    placement lane, and ``profile:<name>`` is the ONLY spelling for it. The
    per-persona authority cannot answer this case — it has no persona to key on
    — so the row supplies it and the list is never empty."""
    rows = _rows(monkeypatch, [_Template("orphan")], [_Agent("alice", "solo")])

    assert rows["profile:orphan"]["persona_spellings"] == ["profile:orphan"]


def test_the_backing_persona_is_named_only_when_ownership_is_unique(monkeypatch):
    """``backs_persona_id`` answers "which persona's defaults does this row
    place"; for two owners there is no such persona, and naming an arbitrary
    one of them is the same silent lie the spellings list exists to stop."""
    rows = _rows(
        monkeypatch,
        [_Template("shared"), _Template("solo")],
        [_Agent("alice", "shared"), _Agent("bob", "shared"), _Agent("carol", "solo")],
    )

    assert "backs_persona_id" not in rows["profile:shared"]
    assert rows["profile:solo"]["backs_persona_id"] == "carol"
