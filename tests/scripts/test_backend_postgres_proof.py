from pathlib import Path

from scripts import backend_postgres_proof as proof


def _fake_backend(tmp_path: Path) -> Path:
    root = tmp_path / "backend"
    (root / "scripts").mkdir(parents=True)
    (root / "docs" / "testing").mkdir(parents=True)
    (root / "scripts" / "test.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (root / "docs" / "testing" / "README.md").write_text("Postgres tier", encoding="utf-8")
    return root


def test_dry_run_builds_default_postgres_command(tmp_path, capsys):
    root = _fake_backend(tmp_path)

    rc = proof.run(["--backend-root", str(root), "--dry-run", "--", "posts.tests.HomeFeedTests"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "scripts/test.sh" in out
    assert "Docker/PostgreSQL" in out
    assert "scripts/test.sh --sqlite" not in out
    assert "posts.tests.HomeFeedTests" in out


def test_rejects_sqlite_escape_hatch(tmp_path, capsys):
    root = _fake_backend(tmp_path)

    rc = proof.run(["--backend-root", str(root), "--dry-run", "--", "--sqlite"])

    assert rc == 2
    err = capsys.readouterr().err
    assert "Refusing non-release backend proof marker" in err


def test_missing_backend_root_is_loud(tmp_path, capsys):
    rc = proof.run(["--backend-root", str(tmp_path / "missing"), "--dry-run"])

    assert rc == 2
    assert "not an EterniaBackend checkout" in capsys.readouterr().err
