"""Point ``~`` at a directory the test controls, on POSIX *and* on Windows.

``monkeypatch.setenv("HOME", tmp_path)`` is the reflex, and on POSIX it works:
``posixpath.expanduser`` reads ``$HOME`` first. ``ntpath.expanduser`` does not
— it prefers ``USERPROFILE``, then ``HOMEDRIVE`` + ``HOMEPATH``, and only then
falls back. So the reflex silently expands ``~`` to the developer's REAL
profile on Windows, and a test that thought it was hermetic quietly reads and
writes outside its tmpdir.

That divergence produced a cluster of registry rows in the per-directory
environment-gap fences, every one of them filed as "Windows ignores a
monkeypatched HOME". It is not an environment gap: the behaviour under test is
reproducible on both platforms once the fixture patches the variables the
platform's ``expanduser`` actually consults.

Note this patches the *environment*, so it also covers production code that
reads ``$HOME`` directly (``gateway/platforms/base.py`` does, deliberately, in
``_media_delivery_denied_paths``).
"""

from __future__ import annotations

import os
from pathlib import Path


def point_home_at(monkeypatch, path: str | os.PathLike[str]) -> Path:
    """Make ``os.path.expanduser("~")`` resolve to ``path`` on every platform.

    Returns the path as a :class:`~pathlib.Path` for convenience.
    """
    home = Path(path)
    monkeypatch.setenv("HOME", str(home))
    # ntpath.expanduser consults these, in this order, before anything else.
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)
    return home
