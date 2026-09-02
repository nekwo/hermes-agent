"""A hung image provider is bounded by hermes, not only by the launcher.

The defect: ``agent/charsheet/pipeline.py`` set no timeout on its one provider
seam, and :func:`agent.pet.generate.imagegen.generate` has no ``timeout``
parameter to pass one through — so the only bound was whatever the resolved
backend set for ITSELF, and they disagree (300 s on openrouter, a 180 s poll
ceiling on krea, and a bare ``openai.OpenAI()`` whose SDK default is 600 s with
retries). A wedged call therefore held one of the serve child's four pool
workers for as long as its client allowed, with the launcher's own ceiling the
only thing that would ever notice.

Nothing here touches Pillow or a network: the seam under test is
``imagegen.generate``, replaced per test.
"""

from __future__ import annotations

import threading
import time

import pytest

from agent.charsheet import pipeline
from agent.charsheet.errors import CharsheetRefusal, ProviderTimeout


def test_a_provider_that_never_answers_is_refused_within_the_deadline(monkeypatch):
    """The row: a hung call yields a typed refusal, and it yields it on time."""

    entered = threading.Event()
    release = threading.Event()

    def _never_returns(prompt, **kwargs):
        entered.set()
        release.wait(30.0)
        return []

    monkeypatch.setattr(pipeline.imagegen, "generate", _never_returns)
    monkeypatch.setattr(pipeline, "provider_timeout_seconds", lambda: 0.3)

    started = time.monotonic()
    try:
        with pytest.raises(ProviderTimeout) as caught:
            pipeline._generate_image(
                "draw a knight",
                reference_images=None,
                aspect_ratio="square",
                prefix="charsheet_row_walk-e",
                provider=None,
            )
        elapsed = time.monotonic() - started
    finally:
        # Never leave the worker blocked on a suite-wide event.
        release.set()

    assert entered.is_set(), "the deadline fired before the provider was called"
    # Bounded, and loosely — a shared runner is not a quiet one.
    assert elapsed < 10.0
    assert caught.value.code == "provider_timeout"
    assert caught.value.safe_details == {
        "prefix": "charsheet_row_walk-e",
        "seconds": 0.3,
    }
    # It is a refusal the character verbs already catch (`_CHARACTERS_EXPECTED`
    # takes the base class), not a bug that keeps its traceback.
    assert isinstance(caught.value, CharsheetRefusal)


def test_the_providers_own_failure_still_reaches_the_caller(monkeypatch):
    """Losing a real fault behind a timeout wrapper would be the worse bug."""

    class _Boom(RuntimeError):
        pass

    def _explodes(prompt, **kwargs):
        raise _Boom("no image backend configured")

    monkeypatch.setattr(pipeline.imagegen, "generate", _explodes)
    monkeypatch.setattr(pipeline, "provider_timeout_seconds", lambda: 5.0)

    with pytest.raises(_Boom, match="no image backend configured"):
        pipeline._generate_image(
            "draw a knight",
            reference_images=None,
            aspect_ratio="square",
            prefix="charsheet_turnaround",
            provider=None,
        )


def test_a_non_positive_budget_runs_the_call_inline_on_this_thread(monkeypatch):
    """`charsheet.provider_timeout_seconds: 0` restores the old behaviour exactly."""

    caller = threading.get_ident()
    ran_on: list[int] = []

    def _records(prompt, **kwargs):
        ran_on.append(threading.get_ident())
        return ["/tmp/whatever.png"]

    monkeypatch.setattr(pipeline.imagegen, "generate", _records)
    monkeypatch.setattr(pipeline, "provider_timeout_seconds", lambda: 0.0)

    result = pipeline._generate_image(
        "draw a knight",
        reference_images=None,
        aspect_ratio="square",
        prefix="charsheet_turnaround",
        provider=None,
    )
    assert ran_on == [caller]
    assert result.name == "whatever.png"


def test_an_empty_answer_is_still_the_no_image_refusal_it_always_was(monkeypatch):
    monkeypatch.setattr(pipeline.imagegen, "generate", lambda prompt, **kwargs: [])
    monkeypatch.setattr(pipeline, "provider_timeout_seconds", lambda: 5.0)

    with pytest.raises(ValueError, match="returned no image"):
        pipeline._generate_image(
            "draw a knight",
            reference_images=None,
            aspect_ratio="square",
            prefix="charsheet_turnaround",
            provider=None,
        )


def test_the_deadline_is_read_from_config_yaml_and_falls_back_to_the_default(monkeypatch):
    """`config.yaml`, never an env var — timeouts are behavioural settings."""

    import hermes_cli.config as config_module

    monkeypatch.setattr(
        config_module, "load_config_readonly", lambda: {"charsheet": {"provider_timeout_seconds": 42}}
    )
    assert pipeline.provider_timeout_seconds() == 42.0

    # Absent section, absent key, and a value that will not coerce all land on
    # the default rather than on an unbounded call or a crash mid-draft.
    monkeypatch.setattr(config_module, "load_config_readonly", lambda: {})
    assert pipeline.provider_timeout_seconds() == pipeline.PROVIDER_TIMEOUT_SECONDS
    monkeypatch.setattr(
        config_module, "load_config_readonly", lambda: {"charsheet": {"provider_timeout_seconds": "soon"}}
    )
    assert pipeline.provider_timeout_seconds() == pipeline.PROVIDER_TIMEOUT_SECONDS
    monkeypatch.setattr(
        config_module,
        "load_config_readonly",
        lambda: (_ for _ in ()).throw(OSError("config.yaml is unreadable")),
    )
    assert pipeline.provider_timeout_seconds() == pipeline.PROVIDER_TIMEOUT_SECONDS


def test_the_shipped_default_sits_inside_the_launchers_long_run_ceiling():
    """A relationship between two bounds, not a snapshot of one number.

    The launcher gives a whole `characters auto` 30 minutes
    (`kHarnessLongRunCeiling`). One wedged provider call must not be able to
    spend that ticket on its own, which is the property the default has to keep
    however it is retuned.
    """

    launcher_long_run_ceiling_seconds = 30.0 * 60.0
    assert pipeline.PROVIDER_TIMEOUT_SECONDS > 0
    assert pipeline.PROVIDER_TIMEOUT_SECONDS < launcher_long_run_ceiling_seconds


def test_the_config_default_and_the_module_default_are_one_number():
    """Two places state the ceiling; a reader must not have to pick one."""

    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert (
        float(DEFAULT_CONFIG["charsheet"]["provider_timeout_seconds"])
        == pipeline.PROVIDER_TIMEOUT_SECONDS
    )
