import argparse
import base64
import json

from hermes_cli.harness import build_parser


def parser():
    p = argparse.ArgumentParser()
    subs = p.add_subparsers(dest="command")
    build_parser(subs)
    return p


def _write_pet(tmp_path, slug="milo", display_name="Milo"):
    from PIL import Image, ImageDraw

    from agent.pet import constants

    pets = tmp_path / "home" / "pets" / slug
    pets.mkdir(parents=True)
    sheet = Image.new(
        "RGBA",
        (constants.FRAME_W * constants.FRAMES_PER_STATE, constants.FRAME_H * 2),
        (0, 0, 0, 0),
    )
    draw = ImageDraw.Draw(sheet)
    for col in range(3):
        x = col * constants.FRAME_W
        draw.rectangle((x + 20, 20, x + 120, 150), fill=(30 + col * 40, 90, 220, 255))
    run_y = constants.FRAME_H
    for col in range(2):
        x = col * constants.FRAME_W
        draw.ellipse((x + 30, run_y + 30, x + 130, run_y + 150), fill=(220, 90 + col * 40, 30, 255))
    sheet.save(pets / "spritesheet.png")
    (pets / "pet.json").write_text(
        json.dumps(
            {
                "id": slug,
                "displayName": display_name,
                "description": "A test pet",
                "spritesheetPath": "spritesheet.png",
            }
        ),
        encoding="utf-8",
    )
    return pets


def test_harness_parser_exposes_petdex_bridge_commands():
    args = parser().parse_args(["harness", "pets", "gallery", "--local-only", "--json"])
    assert args.command == "harness"
    assert args.harness_command == "pets"
    assert args.pets_command == "gallery"
    assert args.local_only is True
    assert args.json is True


def test_harness_pets_gallery_local_only_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    _write_pet(tmp_path, slug="milo", display_name="Milo")

    args = parser().parse_args(["harness", "pets", "gallery", "--local-only", "--json"])

    assert args.func(args) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert data["localOnly"] is True
    assert data["pets"][0]["slug"] == "milo"
    assert data["pets"][0]["displayName"] == "Milo"
    assert data["pets"][0]["installed"] is True


def test_harness_pets_gallery_manifest_error_stays_top_level(monkeypatch, capsys):
    from agent.pet import manifest

    monkeypatch.setattr(manifest, "fetch_manifest", lambda: (_ for _ in ()).throw(RuntimeError("offline")))

    args = parser().parse_args(["harness", "pets", "gallery", "--json"])

    assert args.func(args) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert data["pets"] == []
    assert "offline" in data["manifestError"]


def test_harness_pets_sprite_json_shape(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    _write_pet(tmp_path, slug="milo", display_name="Milo")

    args = parser().parse_args(["harness", "pets", "sprite", "milo", "--json"])

    assert args.func(args) == 0
    data = json.loads(capsys.readouterr().out)
    pet = data["pet"]
    assert data["ok"] is True
    assert pet["slug"] == "milo"
    assert pet["mime"] == "image/png"
    assert pet["frameW"] == 192
    assert pet["frameH"] == 208
    assert pet["stateRows"][:2] == ["idle", "wave"]
    assert pet["framesByRow"]["idle"] == 3
    assert pet["framesByRow"]["wave"] == 2
    assert base64.standard_b64decode(pet["spritesheetBase64"]).startswith(b"\x89PNG")


def test_harness_pets_thumb_json_returns_data_uri(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    _write_pet(tmp_path, slug="milo", display_name="Milo")

    args = parser().parse_args(["harness", "pets", "thumb", "milo", "--json"])

    assert args.func(args) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert data["slug"] == "milo"
    assert data["dataUri"].startswith("data:image/png;base64,")


def test_harness_pets_install_json_uses_profile_safe_store(monkeypatch, capsys):
    from agent.pet import store

    installed = store.InstalledPet(
        slug="milo",
        display_name="Milo",
        description="Installed by test",
        directory=__import__("pathlib").Path("."),
        spritesheet=__import__("pathlib").Path("spritesheet.png"),
    )
    calls = []
    monkeypatch.setattr(store, "install_pet", lambda slug, force=False: calls.append((slug, force)) or installed)

    args = parser().parse_args(["harness", "pets", "install", "milo", "--force", "--json"])

    assert args.func(args) == 0
    data = json.loads(capsys.readouterr().out)
    assert calls == [("milo", True)]
    assert data["ok"] is True
    assert data["pet"]["slug"] == "milo"
