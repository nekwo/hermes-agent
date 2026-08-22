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


# -- the spritesheetRevision producer (W2-H4) ---------------------------------


def test_gallery_row_for_an_installed_pet_carries_the_sprite_payload_revision(
    tmp_path, monkeypatch, capsys
):
    """The launcher's revision-keyed eviction finally has a writer.

    The key was implemented and gated launcher-side on 2026-08-21 against a
    hermes that stamped ``spritesheetRevision`` on the ``pets sprite`` payload
    only -- which the launcher reads AFTER it has already decided whether its
    resident decode is stale. So the gate is not "the row has a revision"; it is
    that the row carries the SAME value ``pets sprite`` reports for that slug.
    Two producers of one cache key would drift, and a drifting key evicts every
    sheet on every gallery read.
    """

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    _write_pet(tmp_path, slug="milo", display_name="Milo")

    gallery = parser().parse_args(
        ["harness", "pets", "gallery", "--local-only", "--json"]
    )
    assert gallery.func(gallery) == 0
    row = json.loads(capsys.readouterr().out)["pets"][0]

    sprite = parser().parse_args(["harness", "pets", "sprite", "milo", "--json"])
    assert sprite.func(sprite) == 0
    payload = json.loads(capsys.readouterr().out)["pet"]

    assert row["slug"] == payload["slug"] == "milo"
    assert row["spritesheetRevision"], "the installed gallery row is unstamped"
    assert row["spritesheetRevision"] == payload["spritesheetRevision"], (
        "the gallery row and the sprite payload disagree about the revision; "
        "the launcher would evict this sheet on every gallery read"
    )


def test_a_rewritten_spritesheet_moves_the_gallery_row_revision(
    tmp_path, monkeypatch, capsys
):
    """A key that never changes is a key that never evicts.

    The stamp is only worth producing if it MOVES when the sheet does. Asserted
    against a real rewrite of the file, not against a recomputation of the
    formula -- a producer keyed on something constant would satisfy the test
    above and fail here.
    """

    import time

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    pet_dir = _write_pet(tmp_path, slug="milo", display_name="Milo")

    def _revision() -> str:
        args = parser().parse_args(
            ["harness", "pets", "gallery", "--local-only", "--json"]
        )
        assert args.func(args) == 0
        return json.loads(capsys.readouterr().out)["pets"][0]["spritesheetRevision"]

    before = _revision()
    sheet = pet_dir / "spritesheet.png"
    # Size AND mtime both move, so the assertion does not rest on the clock's
    # resolution on whatever filesystem the suite runs against.
    time.sleep(0.01)
    sheet.write_bytes(sheet.read_bytes() + b"pad" * 64)
    after = _revision()

    assert before and after
    assert before != after, "the revision did not move when the sheet did"


def test_a_remote_manifest_row_carries_no_revision(monkeypatch, capsys):
    """Unstamped on purpose, and the reason is not laziness.

    A remote row's sheet is the one behind ``spritesheetUrl``; this process
    cannot stat it. Stamping it from anything local would mint a key that means
    nothing about the bytes the launcher would fetch, and the launcher -- which
    correctly reads an unstamped sheet as "no evidence of staleness" -- would
    begin evicting on it.
    """

    from agent.pet import manifest

    entry = manifest.ManifestEntry(
        slug="remote_pet",
        display_name="Remote Pet",
        kind="curated",
        submitted_by="somebody",
        spritesheet_url="https://example.invalid/curated/remote_pet.png",
        pet_json_url="https://example.invalid/curated/remote_pet.json",
        zip_url="https://example.invalid/curated/remote_pet.zip",
    )
    monkeypatch.setattr(manifest, "fetch_manifest", lambda: [entry])

    args = parser().parse_args(["harness", "pets", "gallery", "--json"])
    assert args.func(args) == 0
    rows = json.loads(capsys.readouterr().out)["pets"]

    remote = [row for row in rows if row["slug"] == "remote_pet"]
    assert len(remote) == 1, rows
    assert "spritesheetRevision" not in remote[0], (
        "a remote row was stamped with a revision this process cannot have "
        "measured"
    )
