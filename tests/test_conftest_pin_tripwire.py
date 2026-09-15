"""THE BEHAVIOURAL witness for the shared-MonkeyPatch unwind (ML-14 / C21).

``monkeypatch`` is ONE ``MonkeyPatch`` instance per test function, shared by
every fixture that requests it and by the test body, and its ``undo()`` takes no
argument — it unwinds the ENTIRE stack. Every autouse guard in
``tests/conftest.py`` installs itself through that instance, so a body that
calls ``undo()`` to drop its own stub also drops the hermetic HERMES_HOME, the
credential blanking, the kanban write guard, the live-system guard and the audio
guard, and runs the rest of itself against the OPERATOR's live root. That is the
2026-08-17 leak (EG-0.1): ``ws_office_patch_test`` reached revision 67 in
X:/Eternia/.hermes and a persona-chat root lease was taken out there.

Two witnesses fence it, and they are NOT redundant:

* the **structural** one — ``tests/agent_runtime/test_no_midtest_monkeypatch_undo.py``
  AST-walks all of ``tests/`` and reddens in review, naming file and line. It can
  only see what a walker can resolve;
* the **behavioural** one — ``tests/conftest.py``'s autouse
  ``_shared_monkeypatch_pin_tripwire``, which this file gates. It watches a
  sentinel minted per test and never handed to the body, so it reddens the exact
  test whatever spelling the unwind used.

WHY THE SPELLINGS BELOW ARE THE POINT. Each inner body reaches ``undo`` in a way
the AST walker cannot flag: through an alias, through ``getattr`` with a
computed name, through a callback invoked by something else. If the tripwire
worked only for the literal ``monkeypatch.undo()`` the structural gate already
catches, it would be paying a teardown assertion on every test in the tree to
duplicate a gate that runs once. It does not, and these prove it.

The inner runs happen under ``pytester`` because the claim is about a TEARDOWN
assertion reddening the test that caused it — an outcome only a real pytest run
produces. The throwaway conftest imports the real fixture out of
``tests.conftest`` rather than restating it, so a tripwire that was weakened in
the real file cannot pass here.
"""

from __future__ import annotations

import pytest

pytest_plugins = ["pytester"]


#: The throwaway package's conftest: the REAL fixture, imported. Not a copy —
#: a copy would let this file stay green while the shipped tripwire rotted.
_INNER_CONFTEST = (
    "from tests.conftest import _shared_monkeypatch_pin_tripwire  # noqa: F401\n"
)


#: One inner test body per spelling of the unwind. Every one of them is
#: invisible to an AST walker that flags ``<expr>.undo()`` calls, which is
#: exactly the residue this witness exists for.
_UNWIND_SPELLINGS = {
    "alias": (
        "def test_body(monkeypatch):\n"
        "    handle = monkeypatch\n"
        "    handle.setattr('os.sep', '/', raising=False)\n"
        "    handle.undo()\n"
    ),
    "getattr_computed": (
        "def test_body(monkeypatch):\n"
        "    monkeypatch.setattr('os.sep', '/', raising=False)\n"
        "    getattr(monkeypatch, 'un' + 'do')()\n"
    ),
    "callback": (
        "def test_body(monkeypatch):\n"
        "    def drop(handle):\n"
        "        handle.undo()\n"
        "    monkeypatch.setattr('os.sep', '/', raising=False)\n"
        "    drop(monkeypatch)\n"
    ),
    "fixture_value": (
        "def test_body(request):\n"
        "    handle = request.getfixturevalue('monkeypatch')\n"
        "    handle.setattr('os.sep', '/', raising=False)\n"
        "    handle.undo()\n"
    ),
}


@pytest.mark.parametrize("spelling", sorted(_UNWIND_SPELLINGS))
def test_an_unwind_reddens_the_exact_test_whatever_its_spelling(pytester, spelling):
    """THE GATE. The tripwire must redden the test that performed the unwind."""

    pytester.makeconftest(_INNER_CONFTEST)
    pytester.makepyfile(test_unwinds=_UNWIND_SPELLINGS[spelling])

    result = pytester.runpytest_inprocess("-p", "no:cacheprovider")

    # The BODY passes — the defect is invisible to the test's own assertions,
    # which is the whole reason a teardown tripwire is the instrument. The
    # teardown is what reddens.
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(
        ["*THE SHARED MONKEYPATCH WAS UNWOUND FROM INSIDE THIS TEST*"]
    )


def test_a_body_that_does_not_unwind_is_left_alone(pytester):
    """ANTI-VACUITY. A tripwire that reddened everything would pass the gate
    above forever while making the suite unusable — and the pressure would then
    be to delete it, not to fix the tests it caught."""

    pytester.makeconftest(_INNER_CONFTEST)
    pytester.makepyfile(
        test_clean=(
            "def test_body(monkeypatch):\n"
            "    monkeypatch.setattr('os.sep', '/', raising=False)\n"
            "    assert True\n"
        )
    )

    result = pytester.runpytest_inprocess("-p", "no:cacheprovider")

    result.assert_outcomes(passed=1, errors=0, failed=0)


def test_a_scoped_context_is_the_supported_way_to_drop_one_patch(pytester):
    """The instrument the failure message recommends must actually be legal.

    A fence whose prescribed fix also reds is a fence people route around. The
    scoped context unwinds precisely one block and touches nothing the shared
    instance holds, so it passes — which is what makes the message's advice
    followable rather than aspirational.
    """

    pytester.makeconftest(_INNER_CONFTEST)
    pytester.makepyfile(
        test_scoped=(
            "import pytest\n"
            "\n"
            "def test_body():\n"
            "    with pytest.MonkeyPatch.context() as patched:\n"
            "        patched.setattr('os.sep', '/', raising=False)\n"
            "    assert True\n"
        )
    )

    result = pytester.runpytest_inprocess("-p", "no:cacheprovider")

    result.assert_outcomes(passed=1, errors=0, failed=0)


def test_the_tripwire_is_armed_for_this_very_test():
    """The wiring, not the mechanism: proof the fixture is AUTOUSE in the real
    suite.

    Everything above runs the tripwire inside a throwaway package whose conftest
    imports it by name. That proves the mechanism and nothing about whether the
    shipped suite is actually carrying it — a fixture demoted to opt-in would
    keep every test above green while covering zero real tests. This test never
    requests the fixture, and the witness is minted only by the fixture, so a
    non-``None`` token here means it ran for a test that did not ask.
    """

    from tests import conftest as root_conftest

    assert root_conftest._SHARED_MONKEYPATCH_WITNESS.token is not None, (
        "the per-test witness is unset while a test is running: "
        "_shared_monkeypatch_pin_tripwire did not fire for this test, so it is "
        "no longer autouse and the tree is unwatched"
    )
