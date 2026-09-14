import socket
import threading

from agent_runtime.local_llama.config import LocalLlamaError
from agent_runtime.local_llama.router_client import RouterClient


def test_shutdown_during_binary_probe_cannot_spawn_after_close(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def probe(executable):
        entered.set()
        assert release.wait(5)
    monkeypatch.setattr(RouterClient, "probe_binary", staticmethod(probe))
    router = RouterClient(tmp_path)
    with socket.socket() as port:
        port.bind(("127.0.0.1", 0))
        config = {"executable_path": "must-never-launch", "port": port.getsockname()[1]}
    errors = []
    def start():
        try:
            router.start(config)
        except Exception as exc:
            errors.append(exc)
    worker = threading.Thread(target=start)
    worker.start()
    assert entered.wait(5)
    router.close()
    release.set()
    worker.join(5)
    assert not worker.is_alive()
    assert router.process is None
    assert len(errors) == 1 and isinstance(errors[0], LocalLlamaError)
    assert errors[0].reason == "interrupted"
