"""Regression tests for #4707 — cron must be per-profile.

Design intent (Teknium, June 2026): a profile's cron jobs both LIVE in that
profile's HERMES_HOME and EXECUTE under it.

- Storage: a job created under profile ``coder`` writes to
  ``~/.hermes/profiles/coder/cron/jobs.json`` — NOT the shared default root.
- Execution: the profile-scoped gateway's in-process ticker resolves the
  active HERMES_HOME (profile home) at call time, so jobs run with that
  profile's ``.env`` / ``config.yaml`` / scripts / skills.

This is the opposite direction from the (reverted) #50112/#32091 "anchor at the
shared root" approach. Anchoring at the root funnels every profile's jobs into
one store and runs them under whatever HERMES_HOME the ticker happens to have —
leaking config/credentials/skills across profiles, the security boundary #4707
was filed for. These tests pin per-profile isolation so a stale-branch merge or
a re-anchor "fix" can't silently flip it back.
"""
import importlib
from pathlib import Path

import pytest


def _set_profile_env(patcher: pytest.MonkeyPatch, root: Path, profile_home: Path) -> None:
    """Pretend the platform default root is ``root`` and the active
    HERMES_HOME is a profile under it (``<root>/profiles/<name>``)."""
    import hermes_constants

    patcher.setattr(
        hermes_constants, "_get_platform_default_hermes_home", lambda: root
    )
    patcher.setenv("HERMES_HOME", str(profile_home))


def test_cron_storage_anchors_at_profile_home(tmp_path):
    """Under a profile HERMES_HOME (<root>/profiles/<name>), the cron store
    resolves to <profile>/cron, NOT the shared <root>/cron."""
    root = tmp_path / "hermes_home"
    profile_home = root / "profiles" / "coder"
    profile_home.mkdir(parents=True)

    import cron.jobs as jobs
    import hermes_constants

    try:
        # SCOPED (EG-0.1 / ML-4). The env pin has to be DOWN for the restoring
        # reload in the ``finally``, and this test used to reach that state
        # with a mid-test ``monkeypatch.undo()`` — which unwinds the entire
        # shared per-test instance, every fixture's pins included, not just
        # these two. A context unwinds exactly this block.
        with pytest.MonkeyPatch.context() as patched:
            _set_profile_env(patched, root, profile_home)

            # Sanity: the override is wired the way the gateway sees it.
            assert hermes_constants.get_hermes_home().resolve() == profile_home.resolve()
            assert hermes_constants.get_default_hermes_root().resolve() == root.resolve()

            # cron/jobs.py computes HERMES_DIR from get_hermes_home() at import,
            # so a fresh import under this env anchors the store at
            # <profile>/cron. (The module body only computes paths — the mkdirs
            # live inside its functions — so the reloads here touch no disk.)
            importlib.reload(jobs)

            assert jobs.HERMES_DIR.resolve() == profile_home.resolve()
            assert (
                jobs.JOBS_FILE.resolve()
                == (profile_home / "cron" / "jobs.json").resolve()
            )
            # The shared-root path must NOT be the store — that would re-break
            # per-profile isolation (#4707).
            assert (
                jobs.JOBS_FILE.resolve() != (root / "cron" / "jobs.json").resolve()
            )
    finally:
        # Re-anchor the module at the REAL root for any later test that imports
        # it. In the ``finally`` and not merely after the block, because the
        # ``undo()`` this replaced also ran on the assertion-failure path.
        importlib.reload(jobs)


