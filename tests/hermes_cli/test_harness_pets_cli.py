import argparse
import base64
import hashlib
import io
import json
import os

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


def test_harness_pets_sprite_no_sheet_is_metadata_only(tmp_path, monkeypatch, capsys):
    """Mirrors `characters sprite --no-sheet` (row 33): metadata only, no bytes.

    The row this closes: pets had no metadata-only mode while characters did,
    a deliberate divergence the Mission Control queue flagged (row 33). Same
    relief, same shape — drop `spritesheetBase64`, carry `sheet` (the absolute
    path) in its place, leave every other key untouched.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    pet_dir = _write_pet(tmp_path, slug="milo", display_name="Milo")

    args = parser().parse_args(["harness", "pets", "sprite", "milo", "--no-sheet", "--json"])

    assert args.func(args) == 0
    data = json.loads(capsys.readouterr().out)
    pet = data["pet"]
    assert data["ok"] is True
    assert "spritesheetBase64" not in pet
    assert pet["sheet"] == str(pet_dir / "spritesheet.png")
    assert pet["spritesheetRevision"]
    assert pet["frameW"] == 192
    assert pet["frameH"] == 208
    assert pet["framesByRow"]["idle"] == 3


# ── the sprite byte-baseline (the standing sha check, now enforced by a test) ──
#
# `harness characters` lives in the same argparse tree and the same emitter as
# `harness pets`, so every charsheet slice's done-when list has carried "pets
# sprite byte-baseline re-verified after a harness.py touch". Until now that sha
# lived only in an agent memory note — `grep -rn f378cc37 tests/ docs/` was
# empty — which made the gate a thing an agent had to REMEMBER to read. It is a
# test now.
#
# Split in two on purpose. The payload's `spritesheetBase64` is Pillow-encoded
# PNG bytes and `spritesheetRevision` embeds that encoding's byte SIZE, so a
# Pillow upgrade would move both for no regression at all. What the baseline
# actually guards is the payload's FIELD SET and values — so the sha is taken
# over the payload with those two values removed (the KEYS are still required:
# popping without a default raises if either is renamed away), and the bytes get
# their own decode assertion below.
#
# The pre-split, whole-stdout sha the memory note carried is
# f378cc37fb8033445823409d843b1c44e5ebb23448ce5462216af13f0d076c33, re-verified
# unchanged at the commit that added these tests. It is not asserted here, for
# the encoder reason above.
_PETS_SPRITE_MTIME = 1700000000


def _sprite_payload_for_baseline(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    pet_dir = _write_pet(tmp_path, slug="milo", display_name="Milo")
    sheet = pet_dir / "spritesheet.png"
    # A stat-derived field in a byte-baseline needs a fixed clock.
    os.utime(sheet, (_PETS_SPRITE_MTIME, _PETS_SPRITE_MTIME))

    args = parser().parse_args(["harness", "pets", "sprite", "milo", "--json"])
    assert args.func(args) == 0
    return sheet, json.loads(capsys.readouterr().out)


def test_the_pets_sprite_payload_shape_is_byte_stable(tmp_path, monkeypatch, capsys):
    sheet, payload = _sprite_payload_for_baseline(tmp_path, monkeypatch, capsys)

    # Both are byte-derived, and both must still be PRESENT: `pop` without a
    # default is the field's existence check.
    payload["pet"].pop("spritesheetBase64")
    revision = payload["pet"].pop("spritesheetRevision")

    assert revision == f"{_PETS_SPRITE_MTIME * 10**9}:{sheet.stat().st_size}"
    digest = hashlib.sha256(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert digest == (
        "1c429da01b012d326e389c072bc2c3cebd9bef5a6135b662f1ba0ca278d8272a"
    ), (
        "the pets sprite payload's fields, values or ordering moved; a shipped "
        "Dart client parses this shape, so re-record the sha only once the "
        "change is understood and intended"
    )


def test_the_pets_sprite_bytes_are_a_png_of_the_sheets_own_size(
    tmp_path, monkeypatch, capsys
):
    """The half the sha deliberately does not cover, asserted for what it is."""
    from PIL import Image

    from agent.pet import constants

    sheet, payload = _sprite_payload_for_baseline(tmp_path, monkeypatch, capsys)
    raw = base64.standard_b64decode(payload["pet"]["spritesheetBase64"])

    assert raw.startswith(b"\x89PNG")
    assert raw == sheet.read_bytes(), "the payload re-encoded the sheet"
    with Image.open(io.BytesIO(raw)) as decoded:
        assert decoded.size == (
            constants.FRAME_W * constants.FRAMES_PER_STATE,
            constants.FRAME_H * 2,
        )


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
