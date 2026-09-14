import struct

import pytest

from agent_runtime.local_llama.catalog import metadata, scan, validate_model
from agent_runtime.local_llama.config import default_config, LocalLlamaError


def gguf(path, **extra):
    fields = {"general.architecture": "qwen3", "general.name": "Test model",
              "qwen3.context_length": 8192, **extra}
    def string(value):
        data = value.encode()
        return struct.pack("<Q", len(data)) + data
    data = b"GGUF" + struct.pack("<IQQ", 3, 1, len(fields))
    for key, value in fields.items():
        data += string(key)
        data += struct.pack("<I", 8 if isinstance(value, str) else 4)
        data += string(value) if isinstance(value, str) else struct.pack("<I", value)
    path.write_bytes(data)
    return path


def test_scan_preserves_identity_and_ignores_projection_weights(tmp_path):
    gguf(tmp_path / "model.gguf")
    gguf(tmp_path / "mmproj.gguf")
    config = default_config()
    config["model_roots"] = [str(tmp_path)]
    found, report = scan(config)
    assert len(found["presets"]) == 1
    assert found["presets"][0]["load"]["context_size"] == 8192
    again, _ = scan(found)
    assert again == found
    assert config["presets"] == []
    assert not report["errors"]


def test_split_requires_complete_matching_shards(tmp_path):
    first = gguf(tmp_path / "model-00001-of-00002.gguf", **{"split.count": 2, "split.no": 0})
    with pytest.raises(LocalLlamaError, match="missing"):
        validate_model(first)
    second = gguf(tmp_path / "model-00002-of-00002.gguf", **{"split.count": 2, "split.no": 1})
    assert validate_model(first)["split.count"] == 2
    with pytest.raises(LocalLlamaError, match="first file"):
        validate_model(second)
    gguf(second, **{"split.count": 2, "split.no": 1, "general.name": "Different"})
    with pytest.raises(LocalLlamaError, match="same model"):
        validate_model(first)


def test_truncated_and_oversized_metadata_refused(tmp_path):
    path = gguf(tmp_path / "model.gguf")
    path.write_bytes(path.read_bytes()[:-1])
    with pytest.raises(LocalLlamaError, match="truncated"):
        metadata(path)
    path.write_bytes(b"GGUF" + struct.pack("<IQQQ", 3, 1, 1, 2**40))
    with pytest.raises(LocalLlamaError, match="bounded"):
        metadata(path)


def test_scan_candidate_budget_reports_truncation(tmp_path):
    for index in range(3):
        gguf(tmp_path / f"{index}.gguf")
    config = default_config()
    config["model_roots"] = [str(tmp_path)]
    found, report = scan(config, max_candidates=1)
    assert report["truncated"]
    assert report["candidates"] == len(found["presets"]) == 1


def test_missing_model_has_actionable_error(tmp_path):
    with pytest.raises(LocalLlamaError) as caught:
        validate_model(tmp_path / "missing.gguf")
    assert caught.value.reason == "missing_file"
