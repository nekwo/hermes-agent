"""Generate the mobile provider manifest from Hermes desktop authorities."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def generate(destination: Path) -> None:
    from hermes_cli.provider_catalog import provider_catalog
    from hermes_cli.providers import get_provider

    rows = []
    for descriptor in provider_catalog():
        resolved = get_provider(descriptor.slug)
        rows.append({
            "id": descriptor.slug,
            "display_name": descriptor.label,
            "description": descriptor.description,
            "auth_type": descriptor.auth_type,
            "transport": getattr(resolved, "transport", "") if resolved else "",
            "default_base_url": getattr(resolved, "base_url", "") if resolved else "",
            "api_key_env_vars": list(descriptor.api_key_env_vars),
        })
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps({"schema_version": 1, "providers": rows}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    target = root / "src" / "hermes_mobile_core" / "provider_catalog.json"
    generate(target)
    print(f"Generated {target}")
