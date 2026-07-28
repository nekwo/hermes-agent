import argparse
import json

from hermes_cli.harness import build_parser


def _parser():
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="command"))
    return parser


def test_worktree_reap_dry_run_reaches_backend(monkeypatch, capsys):
    observed = {}

    def fake_reap_orphan_worktrees(*, min_age_seconds, dry_run):
        observed.update(min_age_seconds=min_age_seconds, dry_run=dry_run)
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
    assert observed == {"min_age_seconds": 7200, "dry_run": True}
    assert json.loads(capsys.readouterr().out)["dry_run"] is True


def test_worktree_reap_defaults_to_destructive_backend_mode(monkeypatch, capsys):
    observed = {}

    def fake_reap_orphan_worktrees(*, min_age_seconds, dry_run):
        observed.update(min_age_seconds=min_age_seconds, dry_run=dry_run)
        return {"reaped": [], "kept": [], "dry_run": dry_run}

    monkeypatch.setattr(
        "agent_runtime.delivery_directive.reap_orphan_worktrees",
        fake_reap_orphan_worktrees,
    )
    args = _parser().parse_args(["harness", "worktree", "reap", "--json"])

    assert args.func(args) == 0
    assert observed == {"min_age_seconds": 3600, "dry_run": False}
    assert json.loads(capsys.readouterr().out)["dry_run"] is False
