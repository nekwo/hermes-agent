"""``scan_registered_tools`` reads a registrar module's tools without importing it.

The question this exists for
----------------------------
"Which toolset is this tool in" and "what tools does this toolset hold" are
answered today by importing ``model_tools``, whose module scope imports all 38
registrar modules under ``tools/``. Measured on this checkout with the AST verdict
cache warm, that costs **3.16 s**, and it is paid on every cold
``perform_agent_create`` and on every single-test run — the memo that would
otherwise absorb it lives under ``get_hermes_home()``, which the test runner
points at a fresh temp directory per file, so it is cold by construction.

Nothing about the QUESTION needs the imports. Both identifying arguments of a
``registry.register()`` call are string literals in the source.

What is asserted here, and why each case is in
----------------------------------------------
The four cases are the four ways this reader can be wrong, and three of them
were found by reading the real tree rather than imagined:

* **a plain literal registration** — the 79-of-90 majority;
* **a module-level ``_TOOLSET`` constant** — the other 11 (``flux3_video_tool``,
  ``yuanbao_tools``). A reader that handled only literals would ship a manifest
  that was 88% of the truth, which is worse than no manifest at all;
* **a registration inside a function body** — ``_module_registers_tools``'
  top-level-only rule exists because helper modules call ``registry.register``
  from inside functions, and a scan that swept those in would name tools no
  import produces;
* **a name this reader cannot resolve** — it must land in ``unresolved`` and NOT
  be silently dropped. A scan that drops what it cannot parse is indistinguishable
  from one that found nothing, and the artifact built from it is quietly short.
  That is the MCF-53 census disease one layer down, and the reason ``ToolScan``
  has a third field at all.

The whole-tree assertion at the end is the anti-vacuity floor: a synthetic
fixture proves the reader's shape, and only the real tree proves it is pointed at
anything.
"""

from __future__ import annotations

from pathlib import Path

from tools.registry import scan_registered_tools


def _write(tmp_path: Path, name: str, body: str) -> None:
    (tmp_path / name).write_text(body, encoding="utf-8")


def test_a_plain_literal_registration_is_read(tmp_path):
    _write(
        tmp_path,
        "alpha_tool.py",
        "from tools.registry import registry\n"
        "def handle(**kw):\n    return None\n"
        "registry.register('alpha', 'file', {}, handle)\n",
    )

    scan = scan_registered_tools(tmp_path)

    assert scan.tools == {"alpha": "file"}
    assert scan.modules == {"alpha_tool": ["alpha"]}
    assert scan.unresolved == []


def test_a_keyword_registration_is_read_the_same_way(tmp_path):
    """Positional and keyword forms both appear in the real tree, and a reader
    that only handled one would drop whole modules without saying so."""

    _write(
        tmp_path,
        "beta_tool.py",
        "from tools.registry import registry\n"
        "def handle(**kw):\n    return None\n"
        "registry.register(name='beta', toolset='web', schema={}, handler=handle)\n",
    )

    assert scan_registered_tools(tmp_path).tools == {"beta": "web"}


def test_a_module_level_toolset_constant_is_folded(tmp_path):
    """The 11 registrations that name ``_TOOLSET`` instead of repeating the
    literal. Folding these is what takes the manifest from 79/90 to 90/90."""

    _write(
        tmp_path,
        "gamma_tool.py",
        "from tools.registry import registry\n"
        "_TOOLSET = 'video_gen'\n"
        "def handle(**kw):\n    return None\n"
        "registry.register('gamma_one', _TOOLSET, {}, handle)\n"
        "registry.register('gamma_two', _TOOLSET, {}, handle)\n",
    )

    scan = scan_registered_tools(tmp_path)

    assert scan.tools == {"gamma_one": "video_gen", "gamma_two": "video_gen"}
    assert scan.modules == {"gamma_tool": ["gamma_one", "gamma_two"]}
    assert scan.unresolved == []


def test_a_constant_written_twice_is_refused_rather_than_guessed(tmp_path):
    """A name whose value depends on WHERE the call sits is not a constant.

    Picking either side would be a coin flip recorded as fact; the reader
    reports it and the generator refuses to write."""

    _write(
        tmp_path,
        "delta_tool.py",
        "from tools.registry import registry\n"
        "_TOOLSET = 'web'\n"
        "_TOOLSET = 'file'\n"
        "def handle(**kw):\n    return None\n"
        "registry.register('delta', _TOOLSET, {}, handle)\n",
    )

    scan = scan_registered_tools(tmp_path)

    assert scan.tools == {}
    assert len(scan.unresolved) == 1
    assert "delta_tool" in scan.unresolved[0]
    assert "toolset" in scan.unresolved[0]


def test_a_registration_inside_a_function_is_invisible(tmp_path):
    """The top-level-only rule ``_module_registers_tools`` already holds, held
    here too: importing this module registers nothing, so a scan that named
    ``helper`` would claim a tool that does not exist."""

    _write(
        tmp_path,
        "epsilon_tool.py",
        "from tools.registry import registry\n"
        "def handle(**kw):\n    return None\n"
        "def install():\n"
        "    registry.register('helper', 'file', {}, handle)\n",
    )

    scan = scan_registered_tools(tmp_path)

    assert scan.tools == {}
    assert scan.modules == {}
    # Not an ERROR either: a helper is not a broken registrar.
    assert scan.unresolved == []


def test_a_computed_name_lands_in_unresolved_and_is_not_dropped(tmp_path):
    _write(
        tmp_path,
        "zeta_tool.py",
        "from tools.registry import registry\n"
        "_PREFIX = 'zeta'\n"
        "def handle(**kw):\n    return None\n"
        "registry.register(_PREFIX + '_one', 'file', {}, handle)\n",
    )

    scan = scan_registered_tools(tmp_path)

    assert scan.tools == {}
    assert len(scan.unresolved) == 1
    assert "zeta_tool" in scan.unresolved[0]
    assert "name" in scan.unresolved[0]


def test_the_registry_and_the_mcp_shim_are_skipped_exactly_as_discovery_skips_them(
    tmp_path,
):
    """``__init__.py``, ``registry.py`` and ``mcp_tool.py`` are excluded by
    ``discover_builtin_tools`` and must be excluded here, or the artifact and the
    imports would disagree about the corpus before they disagreed about a name.
    """

    for name in ("__init__.py", "registry.py", "mcp_tool.py"):
        _write(
            tmp_path,
            name,
            "from tools.registry import registry\n"
            "def handle(**kw):\n    return None\n"
            "registry.register('ghost', 'file', {}, handle)\n",
        )

    assert scan_registered_tools(tmp_path).tools == {}


def test_the_scan_is_pointed_at_a_real_corpus():
    """MCF-53 discipline: a census that returns zero forever is the disease.

    Floors, not exact numbers — a new tool file must not red this — except for
    ``unresolved``, which is a ceiling of zero: the day a registration stops
    being statically readable is the day the manifest may not be trusted, and
    this is where that is said.
    """

    scan = scan_registered_tools()

    assert len(scan.modules) >= 30
    assert len(scan.tools) >= 80
    assert len(set(scan.tools.values())) >= 25
    assert scan.unresolved == [], scan.unresolved
    # Two names from opposite ends of the corpus, one of each registration form,
    # so a reader that regressed to literals-only reds here as well.
    assert scan.tools["read_terminal"] == "terminal"
    assert scan.tools["bfl_flux3_get_result"] == "bfl"
