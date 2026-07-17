import pytest


@pytest.fixture(autouse=True)
def isolate_agent_runtime_root(tmp_path, monkeypatch):
    root = tmp_path / "agent-runtime"
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(root))
    yield root


@pytest.fixture
def history_in_frame_config(monkeypatch):
    """Opt a snapshot test into the pre-S2 full-in-frame history shape.

    S2 evicts ``archived_tasks`` / closed-incident bulk / persona-chat tails from
    the steady-state frame by default (read_model.history_in_frame=False). Tests
    that assert the FULL in-frame projection content flip the kill-switch on via
    this fixture; the eviction itself is exercised by the S2 goldens.
    """

    from agent_runtime import snapshot as snapshot_module
    from agent_runtime.config import load_agent_runtime_config as _real_load

    def _loader(*args, **kwargs):
        cfg = _real_load(*args, **kwargs)
        cfg.read_model.history_in_frame = True
        return cfg

    monkeypatch.setattr(snapshot_module, "load_agent_runtime_config", _loader)
    return _loader
