"""Contract pins for the path-identity authority.

Every guard that used to re-derive "is this the same file" now delegates here,
so the guarantees they depend on have to be pinned at THIS level rather than
re-proved once per caller — pinning a guarantee at the boundary that is easiest
to reach is how both halves of a seam end up correct and the join untested.

Two rules run through the whole file:

* **Every permissive verdict is paired with a near miss.** ``denotes_same_file``
  saying True is only meaningful next to a proof that a sibling, a
  prefix-extended name, or a symlink pointing elsewhere still says False.
  Otherwise a predicate that answered True unconditionally would pass.
* **The narrowness is pinned, not just the capability.** Everything this module
  deliberately does NOT equate — MSYS spellings, Cygwin/WSL drive mounts, an
  empty operand — has a test, so widening any of it has to be a conscious edit
  to a red pin rather than a silent side effect.
"""

import os
from pathlib import Path
from unittest.mock import patch as mock_patch

import pytest

from tools import path_identity
from tools.path_identity import (
    denotes_same_file,
    posix_match_forms,
    windows_spelling_of_msys_path,
)


_WINDOWS_ONLY = pytest.mark.skipif(
    os.name != "nt", reason="pins a Windows-specific spelling or case-folding rule"
)
_POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt", reason="pins POSIX-native symlink/case behaviour"
)


class TestPosixMatchForms:
    """The spelling set a POSIX-rooted blocklist must be matched against."""

    def test_posix_literal_survives_normalization(self):
        """The security bypass, stated as a guarantee.

        ``os.path.normpath("/etc/hosts")`` is ``"\\etc\\hosts"`` where ``os.sep``
        is a backslash. A guard comparing only that form matched none of the
        POSIX literals it was built from and answered ALLOW.
        """
        assert "/etc/hosts" in posix_match_forms("/etc/hosts")
        assert "/dev/zero" in posix_match_forms("/dev/zero")

    def test_normalized_form_comes_first(self):
        """Callers index ``forms[0]`` for the host-native spelling."""
        assert posix_match_forms("/etc/hosts")[0] == os.path.normpath("/etc/hosts")

    def test_traversal_is_collapsed_in_every_form(self):
        """Adding a spelling must not create a way to smuggle ``..`` past a
        prefix test: normalization happens BEFORE the spellings fan out."""
        for form in posix_match_forms("/etc/../etc/hosts"):
            assert ".." not in form
        assert "/etc/hosts" in posix_match_forms("/etc/../etc/hosts")

    @_WINDOWS_ONLY
    def test_windows_yields_both_spellings(self):
        forms = posix_match_forms("/etc/hosts")
        assert forms == ("\\etc\\hosts", "/etc/hosts")

    @_WINDOWS_ONLY
    def test_native_windows_path_gains_no_posix_root(self):
        """The added spelling cannot create a false positive.

        A native Windows path is always drive- or UNC-anchored, so no form it
        produces can match an ``/etc/``-style root — which is what makes adding
        the POSIX spelling safe for a *prefix* guard rather than only an
        exact-set one.
        """
        for form in posix_match_forms(r"C:\Users\x\etc\hosts"):
            assert not form.startswith("/etc")
            assert not form.startswith("/dev")

    def test_posix_host_returns_exactly_one_form(self):
        if os.sep != "/":
            pytest.skip("statement is about POSIX hosts")
        assert posix_match_forms("/etc/hosts") == ("/etc/hosts",)

    def test_total_on_non_string_input(self):
        """Total, per the module's purity contract: no raise, ever."""
        assert posix_match_forms(Path("/etc/hosts"))  # PathLike is coerced
        assert posix_match_forms(None) == ()
        assert posix_match_forms(3) == ()


class TestWindowsSpellingOfMsysPath:
    """The ONE translation a guard is allowed to apply, and its exact edges.

    Driven with ``_IS_WINDOWS`` forced on so the rejection table is pinned on
    every host — the historical failure mode here was a Windows-only lane whose
    guarantees nobody could see from CI.
    """

    def test_translates_the_git_bash_spelling(self):
        with mock_patch.object(path_identity, "_IS_WINDOWS", True):
            assert windows_spelling_of_msys_path("/c/Users/x/hermes-verify-a.py") == (
                r"C:\Users\x\hermes-verify-a.py"
            )

    @pytest.mark.parametrize(
        "operand,why",
        [
            ("/tmp/hermes-verify-a.py", "multi-character first component is a real POSIX dir"),
            ("c/Users/x", "relative — names no absolute file"),
            ("/c/", "bare drive root — names no file to act on"),
            ("/c", "bare drive letter — names no file to act on"),
            ("/c/Users\\x", "already carries a native separator; not a clean MSYS spelling"),
            ("/cygdrive/c/Users/x", "Cygwin spelling: deliberately NOT accepted here"),
            ("/mnt/c/Users/x", "WSL mount spelling: deliberately NOT accepted here"),
            ("", "empty"),
            (None, "not a string"),
        ],
    )
    def test_rejection_table(self, operand, why):
        """Everything this refuses to translate, and why.

        The last two rows are the interesting ones. ``tools/environments/local.py``
        ``_msys_to_windows_path`` DOES accept ``/cygdrive/c/...`` and
        ``/mnt/c/...``; this one must not, because its output feeds a security
        exemption and widening it would widen every guard downstream at once.
        Pinned so that widening is a deliberate edit to a red test.
        """
        with mock_patch.object(path_identity, "_IS_WINDOWS", True):
            assert windows_spelling_of_msys_path(operand) is None, why

    def test_never_translates_off_windows(self):
        """On POSIX ``/c/...`` is a real path; reinterpreting it would be a lie."""
        with mock_patch.object(path_identity, "_IS_WINDOWS", False):
            assert windows_spelling_of_msys_path("/c/Users/x/hermes-verify-a.py") is None


class TestDenotesSameFile:
    def test_same_file_through_a_traversal_spelling(self, tmp_path):
        target = tmp_path / "a.txt"
        target.write_text("x", encoding="utf-8")
        spelled = tmp_path / "sub" / ".." / "a.txt"
        (tmp_path / "sub").mkdir()
        assert denotes_same_file(str(spelled), str(target)) is True

    def test_sibling_is_not_the_same_file(self, tmp_path):
        """The near miss for every True above."""
        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        a.write_text("x", encoding="utf-8")
        b.write_text("x", encoding="utf-8")
        assert denotes_same_file(str(a), str(b)) is False

    def test_prefix_extension_is_not_the_same_file(self, tmp_path):
        """Identity is not a prefix test.

        ``/tmp/session`` and ``/tmp/session-evil`` share a prefix and must never
        compare equal — the failure mode of every ``startswith`` guard this
        module replaces.
        """
        root = tmp_path / "session"
        evil = tmp_path / "session-evil"
        root.mkdir()
        evil.mkdir()
        assert denotes_same_file(str(root), str(evil)) is False

    def test_symlink_denotes_its_target(self, tmp_path):
        target = tmp_path / "real.txt"
        target.write_text("x", encoding="utf-8")
        link = tmp_path / "link.txt"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation unavailable for this user")
        # The capability...
        assert denotes_same_file(str(link), str(target)) is True
        # ...and the near miss: a symlink pointing somewhere ELSE does not
        # borrow identity from the directory it happens to sit in.
        other = tmp_path / "other.txt"
        other.write_text("y", encoding="utf-8")
        elsewhere = tmp_path / "elsewhere.txt"
        elsewhere.symlink_to(other)
        assert denotes_same_file(str(elsewhere), str(target)) is False

    def test_nonexistent_paths_still_compare_by_spelling(self, tmp_path):
        """Write targets do not exist yet; identity must still be decidable."""
        missing = tmp_path / "not-created-yet.txt"
        assert denotes_same_file(str(missing), str(missing)) is True
        assert denotes_same_file(str(missing), str(tmp_path / "other.txt")) is False

    @_WINDOWS_ONLY
    def test_windows_folds_case_even_for_a_path_that_does_not_exist(self, tmp_path):
        """``realpath`` recovers on-disk case only for paths that EXIST.

        The write guards run before the file exists, which is precisely where
        ``normpath(a) == normpath(b)`` answered "different file" for one file.
        ``normcase`` is what closes it.
        """
        missing = tmp_path / "Config.yaml"
        flipped = Path(str(missing).upper())
        assert denotes_same_file(str(missing), str(flipped)) is True
        # Near miss: case folding must not fold anything but case.
        assert denotes_same_file(str(missing), str(tmp_path / "Configs.yaml")) is False

    @_WINDOWS_ONLY
    def test_windows_unifies_the_separator(self, tmp_path):
        assert denotes_same_file(str(tmp_path), str(tmp_path).replace("\\", "/")) is True

    @_POSIX_ONLY
    def test_posix_case_is_significant(self, tmp_path):
        """``normcase`` is the identity on POSIX, and must stay that way: two
        files differing only in case are two files there."""
        lower = tmp_path / "a.txt"
        upper = tmp_path / "A.txt"
        lower.write_text("x", encoding="utf-8")
        upper.write_text("y", encoding="utf-8")
        assert denotes_same_file(str(lower), str(upper)) is False

    @_WINDOWS_ONLY
    def test_msys_spelling_is_deliberately_not_equated(self):
        """A guard that wants MSYS equivalence must ask for it explicitly.

        Folding it in here would silently widen every caller at once. The
        explicit route is ``windows_spelling_of_msys_path`` at the call site,
        where the widening is visible and separately testable.
        """
        native = r"C:\Windows"
        assert denotes_same_file("/c/Windows", native) is False
        # ...and the explicit route does reach it, so the refusal above is a
        # policy choice rather than a broken translation.
        translated = windows_spelling_of_msys_path("/c/Windows")
        assert denotes_same_file(translated, native) is True

    @pytest.mark.parametrize("empty", ["", None])
    def test_an_empty_operand_denotes_nothing(self, empty, tmp_path):
        """``realpath("")`` is the process cwd, which would make two empty
        operands "the same file" and hand a guard a match it never meant."""
        assert denotes_same_file(empty, empty) is False
        assert denotes_same_file(empty, str(tmp_path)) is False
        assert denotes_same_file(str(tmp_path), empty) is False

    def test_total_on_hostile_input(self):
        """Purity contract: total for any input, no exception escapes."""
        for a, b in [
            (3, 3),
            (object(), "/tmp"),
            ("/tmp", b"/tmp"),
            ("\x00/tmp", "/tmp"),
            ("/tmp", "\x00/tmp"),
        ]:
            assert denotes_same_file(a, b) in (True, False)

    def test_pathlike_operands_are_accepted(self, tmp_path):
        assert denotes_same_file(tmp_path, str(tmp_path)) is True
