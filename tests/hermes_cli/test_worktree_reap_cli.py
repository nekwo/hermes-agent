import argparse
import json

from hermes_cli.harness import build_parser


def _parser():
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="command"))
    return parser


def test_worktree_reap_dry_run_reaches_backend(monkeypatch, capsys):
    observed = {}

    def fake_reap_orphan_worktrees(*, min_age_seconds, dry_run, include_legacy_temp):
        observed.update(
            min_age_seconds=min_age_seconds,
            dry_run=dry_run,
            include_legacy_temp=include_legacy_temp,
        )
        return {"reaped": [], "kept": [], "dry_run": dry_run}

    monkeypatch.setattr(
        "agent_runtime.delivery_directive.reap_orphan_worktrees",
        fake_reap_orphan_worktrees,
    )
    args = _parser().parse_args(
        [
            "harness",
            "worktree",
            "reap",
            "--dry-run",
            "--min-age-seconds",
            "7200",
            "--json",
        ]
    )

    assert args.func(args) == 0
    assert observed == {
        "min_age_seconds": 7200,
        "dry_run": True,
        "include_legacy_temp": False,
    }
    assert json.loads(capsys.readouterr().out)["dry_run"] is True


def test_worktree_reap_defaults_to_destructive_backend_mode(monkeypatch, capsys):
    observed = {}

    def fake_reap_orphan_worktrees(*, min_age_seconds, dry_run, include_legacy_temp):
        observed.update(
            min_age_seconds=min_age_seconds,
            dry_run=dry_run,
            include_legacy_temp=include_legacy_temp,
        )
        return {"reaped": [], "kept": [], "dry_run": dry_run}

    monkeypatch.setattr(
        "agent_runtime.delivery_directive.reap_orphan_worktrees",
        fake_reap_orphan_worktrees,
    )
    args = _parser().parse_args(["harness", "worktree", "reap", "--json"])

    assert args.func(args) == 0
    assert observed == {
        "min_age_seconds": 3600,
        "dry_run": False,
        "include_legacy_temp": False,
    }
    assert json.loads(capsys.readouterr().out)["dry_run"] is False


def test_worktree_reap_dry_run_human_output_is_explicit_preview(monkeypatch, capsys):
    monkeypatch.setattr(
        "agent_runtime.delivery_directive.reap_orphan_worktrees",
        lambda **_: {
            "reaped": [{"worktree": "launcher_candidate", "dry_run": True}],
            "kept": [{"worktree": "owned", "reason": "owned_by_open_task_run"}],
            "dry_run": True,
        },
    )
    args = _parser().parse_args(["harness", "worktree", "reap", "--dry-run"])

    assert args.func(args) == 0
    output = capsys.readouterr().out
    assert "preview: would reap 1 worktree(s); kept 1" in output
    assert "would reap: launcher_candidate" in output
    assert not output.startswith("reaped ")


def test_worktree_reap_legacy_temp_is_explicit_opt_in(monkeypatch, capsys):
    observed = {}

    def fake_reap_orphan_worktrees(**kwargs):
        observed.update(kwargs)
        return {"reaped": [], "kept": [], "dry_run": True}

    monkeypatch.setattr(
        "agent_runtime.delivery_directive.reap_orphan_worktrees",
        fake_reap_orphan_worktrees,
    )
    args = _parser().parse_args(
        [
            "harness",
            "worktree",
            "reap",
            "--dry-run",
            "--include-legacy-temp",
            "--json",
        ]
    )

    assert args.func(args) == 0
    assert observed["include_legacy_temp"] is True
    capsys.readouterr()
