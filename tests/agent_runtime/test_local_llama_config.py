from copy import deepcopy

import pytest

from agent_runtime.local_llama.config import (
    ConfigStore, GENERATION_DEFAULTS, LOAD_DEFAULTS, LocalLlamaError,
    default_config, validate_config, validate_parameters,
)


@pytest.mark.parametrize("field,value", [("context_size", True), ("context_size", 4097),
    ("gpu_layers", -1), ("flash_attention", "maybe"), ("cache_type_v", "q4_0")])
def test_bad_load_parameters_refused(field, value):
    load = deepcopy(LOAD_DEFAULTS)
    load[field] = value
    with pytest.raises(LocalLlamaError):
        validate_parameters(load, GENERATION_DEFAULTS)


@pytest.mark.parametrize("field,value", [("temperature", float("nan")), ("top_p", 0),
    ("top_k", True), ("max_output_tokens", 32768), ("thinking", "invented")])
def test_bad_generation_parameters_refused(field, value):
    generation = deepcopy(GENERATION_DEFAULTS)
    generation[field] = value
    with pytest.raises(LocalLlamaError):
        validate_parameters(LOAD_DEFAULTS, generation)


def test_unknown_parameter_does_not_silently_drop():
    with pytest.raises(LocalLlamaError):
        validate_parameters({**LOAD_DEFAULTS, "gpu_layer": 8}, GENERATION_DEFAULTS)


def test_config_preserves_other_settings_and_root_comments(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("# keep me\nmodel:\n  default: unrelated\n")
    store = ConfigStore(path)
    config = default_config()
    config["port"] = 8181
    store.write(validate_config(config))
    assert store.read() == config
    assert "# keep me" in path.read_text()
    assert "default: unrelated" in path.read_text()


def test_missing_config_has_no_fs_mutation(tmp_path):
    path = tmp_path / "config.yaml"
    assert ConfigStore(path).read() == default_config()
    assert not path.exists()


def test_missing_saved_server_paths_remain_repairable(tmp_path):
    config = default_config()
    config["executable_path"] = str(tmp_path / "removed.exe")
    config["model_roots"] = [str(tmp_path / "removed-models")]
    store = ConfigStore(tmp_path / "config.yaml")
    store.write(config)
    assert store.read() == config
    with pytest.raises(LocalLlamaError) as caught:
        validate_config(config)
    assert caught.value.reason == "missing_file"


def test_invalid_saved_config_has_typed_error(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("local_llama: [invalid\n")
    with pytest.raises(LocalLlamaError) as caught:
        ConfigStore(path).read()
    assert caught.value.reason == "invalid_config"
