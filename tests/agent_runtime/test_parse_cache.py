import hashlib

from agent_runtime.parse_cache import (
    cached_by_mtime,
    cached_file_sha256,
    cached_yaml_file,
    clear_parse_cache,
)


def test_cached_yaml_file_parses_and_memoizes_by_mtime(tmp_path):
    clear_parse_cache()
    path = tmp_path / "config.yaml"
    path.write_text("agent_runtime:\n  store_root: /some/where\n", encoding="utf-8")

    calls = {"n": 0}

    def loader(p):
        calls["n"] += 1
        import yaml

        return yaml.safe_load(p.read_text(encoding="utf-8"))

    first = cached_by_mtime(path, loader)
    second = cached_by_mtime(path, loader)
    assert first == {"agent_runtime": {"store_root": "/some/where"}}
    assert second is first  # same cached object
    assert calls["n"] == 1  # parsed once

    # A content change (new mtime) invalidates the entry.
    path.write_text("agent_runtime:\n  store_root: /elsewhere\n", encoding="utf-8")
    third = cached_by_mtime(path, loader)
    assert third == {"agent_runtime": {"store_root": "/elsewhere"}}
    assert calls["n"] == 2


def test_cached_yaml_file_missing_returns_default(tmp_path):
    clear_parse_cache()
    assert cached_yaml_file(tmp_path / "absent.yaml", default={}) == {}


def test_cached_file_sha256_matches_hashlib_and_caches(tmp_path):
    clear_parse_cache()
    path = tmp_path / "blob.bin"
    path.write_bytes(b"hello world" * 1000)
    expected = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

    assert cached_file_sha256(path) == expected
    assert cached_file_sha256(path) == expected  # cache hit, same value
