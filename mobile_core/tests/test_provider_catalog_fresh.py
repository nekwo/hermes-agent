from __future__ import annotations

from pathlib import Path

from mobile_core.tools.generate_provider_catalog import generate


def test_committed_provider_catalog_is_fresh(tmp_path: Path) -> None:
    generated = tmp_path / "provider_catalog.json"
    generate(generated)
    committed = (
        Path(__file__).resolve().parents[1]
        / "src" / "hermes_mobile_core" / "provider_catalog.json"
    )
    assert generated.read_bytes() == committed.read_bytes()
