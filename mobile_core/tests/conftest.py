from __future__ import annotations

import sys
from pathlib import Path

MOBILE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = MOBILE_ROOT.parent
SRC_ROOT = MOBILE_ROOT / "src"

for path in (str(SRC_ROOT), str(REPO_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)
