"""The delta-patch lane must survive a reimage.

The whole S7-A patch lane hung on ONE key — ``agent_runtime.read_model.
delta_patches`` — that existed in exactly one untracked file on one machine. The
flag defaulted ``False``, so a fresh clone against a fresh runtime root got the
lane DARK and said nothing: every field change re-shipped a full snapshot core
(measured 2026-08-13: 822,671 bytes where the patch frame is 486). Nothing in any
repo recorded that the flag existed or why it mattered.

Two mechanisms were rejected and one was chosen; this file pins the chosen one.

* REJECTED — scaffold it into a generated config. That mechanism already exists
  and already lost: the Launcher installer's ``kMissionControlBaseSeedConfigYaml``
  stamps ``delta_patches: true`` into the fresh ``base`` PROFILE, and the key is
  ROOT-ONLY, so every fresh install reproduced the exact defect. A value that must
  land in one specific file is a value a writer can put in the wrong file.
* REJECTED — report the absence from ``harness doctor``. A doctor row helps only
  an operator who runs the doctor, and the lane being dark costs bytes silently
  rather than failing anything that would send one there.
* CHOSEN — SHIP the default. ``runtime_config.SHIPPED_DELTA_PATCHES`` is the
  dataclass default, applied by ``config._read_model_config`` for an ABSENT key,
  so it reaches a fresh install and an existing one identically, cannot be
  written to the wrong file, and needs no init/upgrade step to have run.

What that buys is only real if silence resolves LIVE through the REAL path
resolution, if an operator's explicit ``false`` still wins, if a PROFILE copy
still cannot decide the lane, and if the two remaining off-paths (an unloadable
config, a config object carrying no such field) are LOUD. Each is asserted below.
"""

from __future__ import annotations

import logging

import pytest

from agent_runtime.config import (
    AgentRuntimeConfig,
    _read_model_config,
    harness_root_config_path,
    load_root_runtime_config,
)
from agent_runtime.runtime_config import (
    FALLBACK_DELTA_PATCHES,
    SHIPPED_DELTA_PATCHES,
    ReadModelConfig,
)
from agent_runtime.state_patches import delta_patches_enabled

ROOT_NO_READ_MODEL = "agent_runtime:\n  redaction_mode: observe\n"
ROOT_EXPLICIT_OFF = "agent_runtime:\n  read_model:\n    delta_patches: false\n"
ROOT_EXPLICIT_ON = "agent_runtime:\n  read_model:\n    delta_patches: true\n"


@pytest.fixture
def hermes_root(tmp_path, monkeypatch):
    """Point the REAL root resolution at a synthetic runtime root.

    Deliberately drives ``HERMES_HOME`` rather than patching
    ``harness_root_config_path``: the bug being retired lived in WHICH file the
    reader resolves, so a test that stubs the resolver would assert the shipped
    default while proving nothing about the resolution it has to survive.
    """

    root = tmp_path / "runtime-root"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    assert harness_root_config_path() == root / "config.yaml", (
        "the synthetic root did not take — the rest of this file would be "
        "asserting against the operator's live config"
    )
    return root


def _write_root(root, text: str):
    (root / "config.yaml").write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------- #
# The headline: a fresh clone + a fresh runtime root ends up with the lane ON
# --------------------------------------------------------------------------- #
def test_absent_root_config_resolves_the_lane_on(hermes_root):
    """No ``config.yaml`` at all — the shape a reimaged machine boots in."""

    assert not (hermes_root / "config.yaml").exists()
    assert delta_patches_enabled() is True


def test_root_config_without_a_read_model_block_resolves_the_lane_on(hermes_root):
    """A real config that simply never heard of the flag.

    This is the shape the operator's root was in for the lane's whole life, and
    the one the 822 KB deltas were measured against.
    """

    _write_root(hermes_root, ROOT_NO_READ_MODEL)

    assert delta_patches_enabled() is True


def test_operator_explicit_false_still_wins(hermes_root):
    """A shipped default must not become an unspellable one.

    Absence resolves live; ``false`` written by an operator is an instruction and
    outranks the default. If it did not, the only way back to full-core deltas
    would be a code edit.
    """

    _write_root(hermes_root, ROOT_EXPLICIT_OFF)

    assert delta_patches_enabled() is False


def test_operator_explicit_true_is_still_honoured(hermes_root):
    """The live operator config's own spelling keeps working unchanged."""

    _write_root(hermes_root, ROOT_EXPLICIT_ON)

    assert delta_patches_enabled() is True


# --------------------------------------------------------------------------- #
# The default is SHIPPED, not scaffolded: absence vs explicit at the loader
# --------------------------------------------------------------------------- #
def test_loader_distinguishes_absent_from_explicit_false():
    """``_read_model_config`` is where "silence" becomes "the shipped default".

    An ``or``-style fallback would read an explicit ``false`` as absence and make
    the flag one-way; a hardcoded literal here would let the dataclass and the
    loader drift apart.
    """

    assert _read_model_config({}).delta_patches is True
    assert _read_model_config({"delta_patches": False}).delta_patches is False
    assert _read_model_config({"delta_patches": True}).delta_patches is True


def test_dataclass_default_is_the_shipped_constant():
    """The constant is the single authority, so the two cannot diverge silently."""

    assert SHIPPED_DELTA_PATCHES is True
    assert ReadModelConfig().delta_patches is SHIPPED_DELTA_PATCHES


def test_fallback_is_not_the_shipped_default():
    """The fault landing zone is deliberately different from the shipped value.

    Mirrors ``permission_modes.FALLBACK_DEFAULT_PERMISSION_MODE``: a config the
    runtime could not read has told it nothing, and "start emitting a new event
    class from every store chokepoint" is not what silence-it-could-not-parse
    should mean. Collapsing the two constants would erase that distinction.
    """

    assert FALLBACK_DELTA_PATCHES is False
    assert FALLBACK_DELTA_PATCHES is not SHIPPED_DELTA_PATCHES


# --------------------------------------------------------------------------- #
# Root-only: a PROFILE copy still cannot decide the lane, in EITHER direction
# --------------------------------------------------------------------------- #
def test_a_profile_cannot_switch_the_shipped_default_off(tmp_path, monkeypatch):
    """The sticky-active profile is not an authority for this key.

    The reader resolves the ROOT no matter which profile the CLI bootstrap
    redirected ``HERMES_HOME`` into. A profile that says ``false`` must therefore
    be inert — the same inertness that cost the lane its life when a profile said
    ``true``, asserted in the direction that would now silently disable it.
    """

    root = tmp_path / "runtime-root"
    profile = root / "profiles" / "p1"
    profile.mkdir(parents=True)
    (root / "config.yaml").write_text(ROOT_NO_READ_MODEL, encoding="utf-8")
    (profile / "config.yaml").write_text(ROOT_EXPLICIT_OFF, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(profile))

    assert harness_root_config_path() == root / "config.yaml"
    assert load_root_runtime_config().read_model.delta_patches is True
    assert delta_patches_enabled() is True


# --------------------------------------------------------------------------- #
# The remaining off-paths are accounted, never silent
# --------------------------------------------------------------------------- #
def test_unparseable_root_config_is_off_and_warns(hermes_root, caplog):
    """The REALISTIC config fault: a root ``config.yaml`` full of broken YAML.

    This does not raise out of the loader and never did. Every agent_runtime read
    goes through ``parse_cache.cached_yaml_file(path, default=None)``, which
    returns that default for a PARSE failure exactly as it does for a missing
    file — so a broken config silently produces an empty one and every key in it
    resolves to its shipped default.

    That was harmless while this default was ``False`` (absent and broken agreed).
    The moment it ships ``True`` they stop agreeing, and the loader answers the
    wrong one: a config the runtime just failed to read would silently ACTIVATE
    the lane — possibly the very file in which the operator wrote ``false``.
    Measured before the probe existed: this exact file resolved ``True``, in
    silence.

    The first version of this test monkeypatched the loader into raising and
    passed against a code path production never takes. It is written against the
    real file now.
    """

    (hermes_root / "config.yaml").write_text(
        "agent_runtime:\n  read_model:\n   - [oops\n", encoding="utf-8"
    )

    with caplog.at_level(logging.WARNING, logger="agent_runtime.state_patches"):
        assert delta_patches_enabled() is False

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "the lane went dark on a config fault and said nothing"
    assert "delta_patches" in warnings[0].getMessage()


def test_non_mapping_root_config_is_off_and_warns(hermes_root, caplog):
    """A ``config.yaml`` that parses but is not a mapping is equally unread.

    ``load_agent_runtime_config`` coerces a non-dict document to ``{}`` without a
    word, so this lands in the same place as broken YAML: an operator's whole
    file ignored, every key on its shipped default.
    """

    (hermes_root / "config.yaml").write_text("- just\n- a list\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="agent_runtime.state_patches"):
        assert delta_patches_enabled() is False

    assert [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_an_empty_root_config_is_not_a_fault(hermes_root, caplog):
    """Anti-false-positive: an empty file is silence, not damage.

    ``yaml.safe_load("")`` is ``None`` — a legitimately empty config, which is
    absence by another spelling and must resolve ON without a warning. Grading it
    as a fault would put a permanent warning in front of an ordinary install and
    take the lane dark for the shape closest to a fresh root.
    """

    (hermes_root / "config.yaml").write_text("", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="agent_runtime.state_patches"):
        assert delta_patches_enabled() is True

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_a_raising_loader_is_off_and_warns(hermes_root, monkeypatch, caplog):
    """The other fault arm: the load raises outright rather than degrading.

    Kept beside the parse-fault test because they are different code paths with
    the same contract, and the parse path is the one that was wrong.
    """

    def _boom():
        raise OSError("config.yaml is unreadable")

    monkeypatch.setattr("agent_runtime.state_patches.load_root_runtime_config", _boom)

    with caplog.at_level(logging.WARNING, logger="agent_runtime.state_patches"):
        assert delta_patches_enabled() is False

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "the lane went dark on a config fault and said nothing"
    text = warnings[0].getMessage()
    assert "delta_patches" in text
    assert "OSError" in text


def test_config_object_without_the_field_is_off_and_warns(caplog):
    """A cfg that carries no ``read_model.delta_patches`` observed nothing.

    A real ``AgentRuntimeConfig`` always carries it, so this is a stub or a
    half-built object — "the caller told us nothing", which must not silently
    read as the shipped default NOR as an operator's ``false``.
    """

    class _Stub:
        read_model = None

    with caplog.at_level(logging.WARNING, logger="agent_runtime.state_patches"):
        assert delta_patches_enabled(_Stub()) is False

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "an unresolvable config object silently disabled the lane"
    assert "delta_patches" in warnings[0].getMessage()


def test_explicit_false_config_object_does_not_warn(caplog):
    """An operator's ``false`` is an ANSWER, not a fault — it must stay quiet.

    Warning here would put a permanent line in the log of every install that
    deliberately runs full-core deltas, which is how a warning stops being read.
    """

    cfg = AgentRuntimeConfig(read_model=ReadModelConfig(delta_patches=False))

    with caplog.at_level(logging.WARNING, logger="agent_runtime.state_patches"):
        assert delta_patches_enabled(cfg) is False

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
