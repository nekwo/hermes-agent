from __future__ import annotations

from .read_model import ReadModel
from .snapshot import build_snapshot


class Projector:
    """Rebuild the surviving snapshot into the read model as one whole frame.

    S46 retired the INCREMENTAL lane (ledger item 9, operator-ruled RETIRE
    2026-08-01). ``apply_pending`` — with the ``meta.projector_lease`` it took,
    the watermark diff it did, the pending-event count it made, and the
    ``ProjectorResult`` offsets/timings only it produced — had five test callers
    and zero production ones. The RD3 "ticker chokepoint" that was supposed to
    drive it (doc 05:348) was never wired, so its SLO tests were timing a lane
    nothing ran.

    What survives is the operator-invoked cache warmer: ``full_rebuild`` behind
    ``hermes harness rebuild-read-model``. It never took the lease, so nothing
    about single-writer safety changed with the removal; the projection unit was
    already the whole compact snapshot rather than row-level deltas.
    """

    def __init__(self, read_model: ReadModel, *, config):
        self.read_model = read_model
        self.config = config

    def full_rebuild(self) -> None:
        self.read_model.apply_full_rebuild(build_snapshot())
