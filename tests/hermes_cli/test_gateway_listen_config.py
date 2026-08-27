"""The config read that opens the second door — and the key that never existed.

``gateway_listen_config`` is the seam every other gateway test patches, so its
own behaviour has to be pinned somewhere, and this is that somewhere. Three
things are worth pinning and one of them is a regression:

* **off is the default and every unreadable answer is off.** The failure
  direction for a config this runtime cannot parse is "do not bind", never "bind
  something reasonable".
* **``listen`` is a HOST STRING, and boolean ``true`` is refused.** An operator
  opening a port onto a LAN should have to say which interface; "guessed one for
  you" is not a sentence this runtime should be able to say about a listener
  that executes agents with tools.
* **the key is ``remote_gateway``.** Stage 0a declared it as ``gateway``, which
  is already a top-level key in ``config_defaults``' single dict literal — so
  Python kept the later entry and dropped Stage 0a's at parse time. Its receipts
  say the keys were "declared, read by nothing", and the second half is exactly
  what hid the first: a key nobody reads is indistinguishable from a key that is
  not there.
"""

from __future__ import annotations

import pytest

from hermes_cli.harness_parts import serve as serve_module
from hermes_cli.harness_parts.serve import gateway_listen_config


@pytest.fixture
def config(monkeypatch):
    """Drive the read without writing an operator's real ``config.yaml``."""

    state: dict = {}

    def _install(block):
        state["block"] = block
        monkeypatch.setattr(
            serve_module,
            "load_config_readonly",
            lambda: {"remote_gateway": block},
            raising=False,
        )
        import hermes_cli.config as config_module

        monkeypatch.setattr(
            config_module, "load_config_readonly", lambda: {"remote_gateway": block}
        )

    return _install


def test_the_shipped_default_is_off(config):
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    config(DEFAULT_CONFIG["remote_gateway"])

    assert gateway_listen_config() == (None, 0)


def test_a_host_string_turns_it_on_and_the_port_comes_with_it(config):
    config({"listen": "0.0.0.0", "port": 8765})

    assert gateway_listen_config() == ("0.0.0.0", 8765)


def test_a_specific_interface_is_carried_through_verbatim(config):
    config({"listen": "192.168.1.40", "port": 0})

    assert gateway_listen_config() == ("192.168.1.40", 0)


@pytest.mark.parametrize(
    "block",
    [
        {"listen": True, "port": 8765},
        {"listen": "true", "port": 8765},
        {"listen": False},
        {"listen": "", "port": 8765},
        {"listen": "   "},
        {"listen": None},
        {"listen": 1},
        {"listen": ["0.0.0.0"]},
        {},
    ],
)
def test_everything_that_is_not_a_host_is_off(config, block):
    """Boolean ``true`` is in this list on purpose, and it is the interesting
    one: it is the value an operator is most likely to write, and resolving it
    to a default interface would open a LAN port nobody named."""

    config(block)

    assert gateway_listen_config()[0] is None


def test_an_unparseable_port_falls_back_to_ephemeral_rather_than_refusing(config):
    """A port is a preference; a host is a decision. Refusing to bind because a
    port was mistyped would turn a typo into an outage on a lane the operator
    explicitly asked for, and an ephemeral port is still reachable — it rides
    the ready frame and the registry."""

    config({"listen": "0.0.0.0", "port": "eight thousand"})

    assert gateway_listen_config() == ("0.0.0.0", 0)


def test_a_port_outside_the_range_is_clamped_rather_than_bound(config):
    config({"listen": "0.0.0.0", "port": 999999})

    assert gateway_listen_config() == ("0.0.0.0", 65535)


def test_a_config_that_raises_is_off(monkeypatch):
    """The read runs on the boot path. A config layer that throws must cost the
    gateway lane and nothing else — never the runtime."""

    import hermes_cli.config as config_module

    def _boom():
        raise RuntimeError("config is unreadable")

    monkeypatch.setattr(config_module, "load_config_readonly", _boom)

    assert gateway_listen_config() == (None, 0)


def test_the_messaging_gateway_block_cannot_turn_this_lane_on(monkeypatch):
    """The duplicate-key defect's other half. ``gateway`` belongs to the
    chat-platform gateway, and this lane must not read it — the two share a word
    and must never share a key."""

    import hermes_cli.config as config_module

    monkeypatch.setattr(
        config_module,
        "load_config_readonly",
        lambda: {"gateway": {"listen": "0.0.0.0", "port": 8765}},
    )

    assert gateway_listen_config() == (None, 0)
