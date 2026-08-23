"""Root-only config keys set in a PROFILE, where no reader ever looks.

Four keys resolve through ``config.harness_root_config_path`` and never consult
the active profile. YAML accepts them anywhere and profile-aware surfaces report
them back, so a value written one layer below its reader looks applied and does
nothing. This defect class has landed twice in three weeks, in both directions:

* 2026-07-23 — ruling in the ROOT, reader on the profile (fixed by moving the
  reader; that is why ``harness_root_config_path`` exists).
* 2026-08-13 — the mirror image. ``read_model.delta_patches: true`` sat in
  ``profiles/base`` and ``profiles/alice`` while the reader correctly used the
  root, which carried no ``read_model`` block. The S7-A patch producer stayed
  dark for its whole life: one field change shipped an 822,671-byte delta
  carrying an 864,241-byte full core, where the patch frame is 486 bytes.
  ``harness status`` reported ``delta_patches: true`` the entire time.

The severity split is load-bearing and is asserted here: profile-only is a
DEFECT (the instruction is inert), profile-and-root is a NOTICE (the live value
is correct, the copy is a leftover). Grading a redundant copy as a defect would
leave ``harness doctor`` permanently red, which is the "a gate that is always
red is not a gate" failure this repo has already shipped once.
"""

from __future__ import annotations

import agent_runtime.config as cfgmod
from agent_runtime.harness_doctor import (
    HEALTH_DEFECT,
    HEALTH_NOTICE,
    HEALTH_OK,
    _root_config_misplacement_report,
)

ROOT_WITH_KEY = "agent_runtime:\n  read_model:\n    delta_patches: true\n"
ROOT_WITHOUT_KEY = "agent_runtime:\n  redaction_mode: observe\n"
PROFILE_WITH_KEY = "agent_runtime:\n  read_model:\n    delta_patches: true\n"
PROFILE_CLEAN = "agent_runtime:\n  redaction_mode: observe\n"


def _arrange(tmp_path, monkeypatch, root_text: str, profile_text: str):
    """Point the readers at a synthetic root + one profile, and report."""

    root_config = tmp_path / "config.yaml"
    root_config.write_text(root_text, encoding="utf-8")
    profile_config = tmp_path / "profiles" / "p1" / "config.yaml"
    profile_config.parent.mkdir(parents=True)
    profile_config.write_text(profile_text, encoding="utf-8")

    monkeypatch.setattr(cfgmod, "harness_root_config_path", lambda: root_config)
    monkeypatch.setattr(cfgmod, "_profile_config_paths", lambda: [profile_config])
    return _root_config_misplacement_report()


def test_profile_only_key_is_a_defect(tmp_path, monkeypatch):
    """The operator's value is INERT — the reader never sees it."""

    report = _arrange(tmp_path, monkeypatch, ROOT_WITHOUT_KEY, PROFILE_WITH_KEY)

    assert report["health"] == HEALTH_DEFECT
    assert len(report["inert"]) == 1
    assert report["redundant"] == []
    row = report["inert"][0]
    assert row["key"] == "agent_runtime.read_model.delta_patches"
    assert row["profile"] == "p1"
    assert row["set_in_root"] is False
    # The finding must name the reader, so a fix does not require re-deriving
    # which code path ignored the value.
    assert "delta_patches_enabled" in row["read_only_by"]


def test_key_in_both_is_only_a_notice(tmp_path, monkeypatch):
    """Live value is correct; the profile copy is a leftover, not a failure."""

    report = _arrange(tmp_path, monkeypatch, ROOT_WITH_KEY, PROFILE_WITH_KEY)

    assert report["health"] == HEALTH_NOTICE
    assert report["inert"] == []
    assert len(report["redundant"]) == 1
    assert report["redundant"][0]["set_in_root"] is True


def test_clean_profile_reports_ok(tmp_path, monkeypatch):
    """Anti-false-positive: a profile that sets none of the keys is clean."""

    report = _arrange(tmp_path, monkeypatch, ROOT_WITH_KEY, PROFILE_CLEAN)

    assert report["health"] == HEALTH_OK
    assert report["misplaced"] == []


def test_read_model_enabled_is_not_flagged(tmp_path, monkeypatch):
    """The rule keys on LEAVES, never on blocks, and this is the proof.

    ``read_model`` WAS split across both loaders: ``read_model.enabled`` was read
    profile-aware (``snapshot.py`` consulted the passed cfg) while
    ``read_model.delta_patches`` is root-only, so a block-level rule would have
    raised a false positive on every profile that set ``enabled`` — which was
    most of them — and trained operators to ignore the finding.

    Stage 6 (2026-08-22) removed the profile-aware half: ``read_model.enabled``
    has no reader at all now. This test is KEPT and re-aimed rather than deleted,
    because what it actually gates is the leaf-vs-block shape of the rule, and
    that shape is still what stops the next split block from false-positiving.
    Note the finding it asserts absent is also still the RIGHT one: a profile
    setting a reader-less key is inert, but it is not MISPLACED — nothing would
    have read it at the root either — and reporting it here would be answering a
    different question than this doctor asks.
    """

    report = _arrange(
        tmp_path,
        monkeypatch,
        ROOT_WITH_KEY,
        "agent_runtime:\n  read_model:\n    enabled: true\n",
    )

    assert report["health"] == HEALTH_OK
    assert report["misplaced"] == []


def test_per_persona_key_matches_the_concrete_path(tmp_path, monkeypatch):
    """A root pin for ONE persona does not make another persona's copy live.

    ``personas.*.workdir`` is per-persona, so redundancy must be judged on the
    concrete path. Judging on the pattern would report ``personas.qa.workdir``
    as a harmless duplicate because the root happens to pin ``personas.neko``,
    hiding a genuinely inert value behind a notice.
    """

    report = _arrange(
        tmp_path,
        monkeypatch,
        "agent_runtime:\n  personas:\n    neko:\n      workdir: /root/neko\n",
        "agent_runtime:\n  personas:\n    qa:\n      workdir: /profile/qa\n",
    )

    assert report["health"] == HEALTH_DEFECT
    assert len(report["inert"]) == 1
    assert report["inert"][0]["key"] == "agent_runtime.personas.qa.workdir"


def test_every_declared_key_names_a_real_reader():
    """The key table must not outlive the readers it protects.

    Each entry pairs a key with the dotted path of the function that consumes
    it. A renamed or deleted reader leaves a table entry describing a rule
    nobody enforces — the same "classifier naming a producer nobody has" shape
    ``patch_coverage`` split its live/historical sets to prevent.
    """

    import importlib

    assert cfgmod.ROOT_ONLY_CONFIG_KEYS, "the table must not be empty"
    for key_path, reader in cfgmod.ROOT_ONLY_CONFIG_KEYS:
        assert key_path, "every entry needs a key path"
        module_name, _, attr = reader.rpartition(".")
        module = importlib.import_module(module_name)
        assert hasattr(module, attr), f"{reader} does not exist"
