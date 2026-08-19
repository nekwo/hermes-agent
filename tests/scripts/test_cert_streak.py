"""The burn-in DRIVERS are gone — plural, and that is the point.

`scripts/cert_streak.py` went at S6 with the rest of the burn-in machinery.
`scripts/certification_ladder.py` did not: it drove the same retired
`harness burn-in` verb, sat beside its deleted twin for two months, and was
missed by every sweep because this test pinned ONE name. A single-name pin
over a family is a gate that reports on whichever member the author happened
to think of.
"""

from __future__ import annotations

import importlib.util

import pytest

#: Both drivers of the retired `harness burn-in` verb. Any future one belongs
#: here on the day it is written, not on the day someone notices it survived.
BURN_IN_DRIVERS = (
    "scripts.cert_streak",
    "scripts.certification_ladder",
)


@pytest.mark.parametrize("module", BURN_IN_DRIVERS)
def test_burn_in_drivers_are_removed_with_their_machinery(module: str):
    assert importlib.util.find_spec(module) is None, (
        f"{module} drives `harness burn-in`, a verb that no longer exists"
    )
