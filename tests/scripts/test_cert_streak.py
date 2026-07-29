from __future__ import annotations

import importlib.util


def test_cert_streak_script_is_removed_with_burn_in_machinery():
    assert importlib.util.find_spec("scripts.cert_streak") is None
