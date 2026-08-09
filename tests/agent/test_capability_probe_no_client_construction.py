"""A capability check must not construct a provider client.

``check_vision_requirements`` is a registry ``check_fn``: the Mission Control
snapshot build runs it once per gated toolset per persona (~1,050 invocations
in one cold build) and it used to answer by resolving a REAL client — lazy
openai SDK import, keepalive httpx client, TLS context, Codex OAuth token —
1.5s of provider-client construction inside a read-only projection (measured
2026-08-09, PERF_STARTUP_ANALYSIS §3). That is the same disease the
2026-07-08 chat-lane fix retired, recurring one layer down.

The guarantee pinned here is STRUCTURAL — zero client constructions across the
check — not a wall-clock budget, which would rot on the first slow CI box.
The fixtures below deliberately route the resolution into the branch that DOES
build a client (Codex credentials present), so a regression is visible as a
construction, not merely as a slower test.
"""

import pytest

from agent import auxiliary_client as ac


@pytest.fixture(autouse=True)
def isolated_client_cache(monkeypatch):
    """Each test starts with an empty shared client cache.

    The cache is a legitimate second way to answer "available" without
    constructing (a memoized client IS the runtime's client), so leaving it
    shared would let one test's real resolution satisfy the next test's probe
    and hide whether the probe path itself constructs anything.
    """

    monkeypatch.setattr(ac, "_client_cache", {})
    yield


@pytest.fixture
def codex_vision_route(monkeypatch):
    """Force the vision resolution down the Codex branch with a credential.

    Without this the answer depends on the developer's own config, and a
    machine with no provider at all would make broken and correct behaviour
    identical (both construct nothing).
    """

    monkeypatch.setattr(
        ac,
        "_resolve_task_provider_model",
        lambda *args, **kwargs: ("openai-codex", "gpt-5.4", None, None, None),
    )
    monkeypatch.setattr(ac, "_read_codex_access_token", lambda: "codex-token")
    monkeypatch.setattr(ac, "_select_pool_entry", lambda _provider: (False, None))
    yield


def test_the_fixture_route_really_would_construct_a_client(codex_vision_route):
    """Guard on the guard: prove this route constructs when NOT probing, so a
    zero-construction assertion below is evidence rather than a tautology."""

    before = ac.client_construction_count()
    provider, client, _model = ac.resolve_vision_provider_client()

    assert provider == "openai-codex"
    assert client is not None
    assert ac.client_construction_count() == before + 1


def test_vision_capability_check_constructs_no_client(codex_vision_route):
    from tools.vision_tools import check_vision_requirements

    before = ac.client_construction_count()
    available = check_vision_requirements()

    # The ANSWER is unchanged — this is the half that makes the optimisation
    # honest. A check that skipped the work by reporting "unavailable" would
    # silently drop the vision tool from every persona's toolset (#31179).
    assert available is True
    assert ac.client_construction_count() == before


def test_the_probe_answer_matches_the_real_resolution(codex_vision_route):
    """Same resolver, same fallback chain, same verdict — the probe replaces
    only the terminal construction."""

    _provider, real_client, _model = ac.resolve_vision_provider_client()
    assert not ac.is_capability_probe_client(real_client)
    after_real = ac.client_construction_count()

    with ac.capability_probe_scope():
        _provider, probe_client, _model = ac.resolve_vision_provider_client()

    assert (real_client is not None) == (probe_client is not None)
    # Once a real client is memoized the probe reuses it — an availability
    # answer from the client the runtime will actually use, and still zero
    # construction. That is the same guarantee by a different route.
    assert ac.client_construction_count() == after_real


def test_a_probe_with_a_cold_cache_answers_from_a_stand_in(codex_vision_route):
    """With nothing memoized, the probe still answers — and what it resolved
    is the inert stand-in (wrapped by the resolver), not a live client."""

    with ac.capability_probe_scope():
        _provider, probe_client, _model = ac.resolve_vision_provider_client()

    assert probe_client is not None
    assert ac.is_capability_probe_client(probe_client)


def test_an_unavailable_backend_still_answers_unavailable(monkeypatch):
    """The stand-in is returned in place of a client that WOULD have been
    built — never in place of one that could not be."""

    monkeypatch.setattr(
        ac,
        "_resolve_task_provider_model",
        lambda *args, **kwargs: ("openai-codex", "gpt-5.4", None, None, None),
    )
    monkeypatch.setattr(ac, "_read_codex_access_token", lambda: None)
    monkeypatch.setattr(ac, "_select_pool_entry", lambda _provider: (False, None))

    with ac.capability_probe_scope():
        client, _model = ac.resolve_provider_client("openai-codex", "gpt-5.4")

    assert client is None


def test_wrappers_can_still_be_constructed_around_the_stand_in(codex_vision_route):
    """``CodexAuxiliaryClient`` reads ``.api_key`` / ``.base_url`` while it
    constructs. A stand-in that raised on attribute access would make the probe
    answer "unavailable" for a backend that is available — the exact false
    negative issue #31179 fixed."""

    with ac.capability_probe_scope():
        _provider, client, _model = ac.resolve_vision_provider_client()

    assert client is not None
    assert client.api_key is not None


def test_using_the_stand_in_raises_instead_of_reaching_a_wire():
    probe = ac._CAPABILITY_PROBE_CLIENT

    with pytest.raises(RuntimeError) as excinfo:
        probe.chat.completions.create(model="gpt-5.4", messages=[])

    assert "capability-probe" in str(excinfo.value)


def test_the_stand_in_never_enters_the_shared_client_cache(monkeypatch):
    """If one were cached, the next REAL caller would be handed something that
    cannot make a request."""

    monkeypatch.setattr(ac, "_client_cache", {})
    monkeypatch.setattr(ac, "_read_codex_access_token", lambda: "codex-token")
    monkeypatch.setattr(ac, "_select_pool_entry", lambda _provider: (False, None))

    with ac.capability_probe_scope():
        client, _model = ac._get_cached_client("openai-codex", "gpt-5.4")

    assert ac.is_capability_probe_client(client)
    assert ac._client_cache == {}


def test_the_probe_scope_does_not_leak_out_of_its_block():
    assert ac.capability_probe_active() is False
    with ac.capability_probe_scope():
        assert ac.capability_probe_active() is True
    assert ac.capability_probe_active() is False
