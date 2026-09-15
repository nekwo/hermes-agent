"""``load_config_readonly()`` is read-only toward the FILESYSTEM too.

The defect this pins against (eager-tool-discovery audit, 2026-08-09):
``_load_config_impl`` called ``ensure_hermes_home()`` unconditionally, so a
function named *readonly* scaffolded eleven directories and wrote ``SOUL.md``
into whatever home the environment resolved. Because the config read sits on
the tool tree's import chain, that made pytest COLLECTION with an exported
``HERMES_HOME`` materialize a home skeleton before any fixture ran, and made
every "look at a setting" hot path a potential writer.

The contract now: reading config must leave a nonexistent home nonexistent.
``load_config()`` — the mutate-then-``save_config`` path — still ensures the
home, as do the explicit write paths.
"""

from __future__ import annotations


def test_load_config_readonly_does_not_materialize_the_home(tmp_path, monkeypatch):
    home = tmp_path / "never_materialized"
    assert not home.exists()

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_HEAD_HOME", raising=False)

    from hermes_cli.config import load_config_readonly

    config = load_config_readonly()

    assert isinstance(config, dict) and config, "readonly load must still answer"
    assert not home.exists(), (
        "load_config_readonly() materialized the home directory — a reader "
        "scaffolding the filesystem is the eager-tool-discovery defect class; "
        f"created: {sorted(p.name for p in home.iterdir()) if home.exists() else []}"
    )


def test_load_config_still_ensures_the_home(tmp_path, monkeypatch):
    """The mutable path keeps its guarantee — the split must not overshoot.

    Callers of ``load_config()`` mutate the result and hand it to
    ``save_config``; the home existing afterward is part of that path's
    long-standing contract and every write path relies on it.
    """

    home = tmp_path / "materialized"
    assert not home.exists()

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_HEAD_HOME", raising=False)

    from hermes_cli.config import load_config

    config = load_config()

    assert isinstance(config, dict) and config
    assert home.is_dir(), "load_config() must keep ensuring the home"
