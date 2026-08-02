"""S33 — retire the caller-free repo-baseline capture lane.

S32 (``a54e802cd``) proved that ``persona_runtime``'s three
``RepoExecutionContext``-typed helpers have had zero callers since S5.  The
2026-07-30 follow-up ruling clears their contested test pins and retires the
whole baseline lane: once ``_attach_repo_baseline`` is gone,
``repo_context.capture_repo_baseline`` has no production caller either.

The worktree-creator lane is a separate, live regression seam and stays whole.
"""

from __future__ import annotations

import inspect

from agent_runtime import repo_context


REMOVED_PERSONA_RUNTIME_SYMBOLS = (
    "_attach_repo_baseline",
    "_repo_context_for_render",
    "_repo_context_progress_payload",
)

REMOVED_PERSONA_RUNTIME_IMPORTS = (
    "RepoExecutionContext",
    "RunStore",
    "capture_repo_baseline",
)








def test_the_worktree_creator_trio_stays_live_as_regression_infrastructure():
    for name in (
        "isolated_repo_context_for_run",
        "_worktree_token",
        "_ensure_isolated_worktree",
    ):
        assert callable(getattr(repo_context, name)), name

    source = inspect.getsource(repo_context)
    assert "isolated_repo_context_for_run(" in source
    assert "_ensure_isolated_worktree(" in source
