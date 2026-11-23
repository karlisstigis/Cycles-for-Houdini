# SPDX-License-Identifier: Apache-2.0
"""Converter files under assets_blender/<blend>/geo/<asset> with one cycles_import.json (blend-import.md: Folder
layout, Import record).

Uses the fake `hou` and synthetic exporter results of test_blend_convert. Covers the layout of
every structure, blend folder and asset name clashes, the shared JSON, replace-on-reimport
deleting only listed files of the same structure, recovered/foreign/missing JSON, the
shared-scene rule, the path-length limit, the untitled-scene cache, the refusal inside the
library and the textures of converted materials (packed images in tex/, the rest in place).
"""

import hashlib
import json
from pathlib import Path

import pytest

import test_blend_convert as cases

pytestmark = [pytest.mark.pure, pytest.mark.cpu]
fake = cases.fake  # fake hou network, scene and .blend (fixture)
run = cases.run
import_json = cases.import_json


def tree(folder):
    return sorted(p.relative_to(folder).as_posix() for p in Path(folder).rglob("*") if p.is_file())


def object_manifest(request):
    """The Tree object alone (as test_object_selection_hierarchy_has_no_collection_null)."""
    manifest, arrays = cases.scene_manifest(frame=request["frame"] if request["frame"] != "scene" else 1.0)
    manifest.update(
        selector_type="OBJECT",
        selector="Tree",
        objects=[o for o in manifest["objects"] if o["name"] == "Tree"],
        instances=[dict(i, depth=0 if i["depth"] == 1 else 2) for i in manifest["instances"][1:]],
    )
    return manifest, arrays


def shared_shapes(request):
    manifest, arrays = cases.scene_manifest()
    manifest["instances"] = manifest["instances"][:2]  # Child and Tree share one mesh
    return manifest, arrays


def blend_from(fake, *parts):
    path = fake.tmp.joinpath("library", *parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"BLENDER")
    return path


# ---------------------------------------------------------------- layout and names


def test_every_structure_shares_one_blend_folder_and_one_json(fake):
    run(fake, "packed")
    run(fake, "hierarchy", materials="none", manifest_for=shared_shapes)
    nodes, _, _ = run(fake, "packed", selection=[("object", "Tree")], frames=(1, 2, 1), manifest_for=object_manifest)
    assert tree(fake.hip / "assets_blender") == [
        "props/cycles_import.json",
        "props/geo/Props/Props.bgeo.sc",
        "props/geo/Props/hierarchy/shapes/Child.bgeo.sc",
        "props/geo/Tree/Tree.0001.bgeo.sc",
        "props/geo/Tree/Tree.0002.bgeo.sc",
    ]  # no per-structure manifests, no lock left behind, no tex/ without packed images
    data = import_json(fake)
    assert data["schema"] == 1 and data["names"]["assets"] == {"collection:Props": "Props", "object:Tree": "Tree"}
    assert data["assets"]["Props"]["options"]["structures"] == {
        "hierarchy": ["hierarchy/shapes/Child.bgeo.sc"],
        "packed": ["Props.bgeo.sc"],
    }
    assert data["assets"]["Tree"]["frames"] == [1, 2, 1]
    assert nodes[0].node("file1").evalParm("file") == "$HIP/assets_blender/props/geo/Tree/Tree.$F4.bgeo.sc"


def test_blend_folders_of_files_with_the_same_stem(fake):
    run(fake, "packed")
    run(fake, "packed", source=blend_from(fake, "alt", "props.blend"))
    run(fake, "packed", source=blend_from(fake, "x", "alt", "props.blend"))
    folders = sorted(p.name for p in (fake.hip / "assets_blender").iterdir())
    assert folders == ["props", "props__alt", "props__alt_2"]
    # A re-import finds its own folder through the JSON's source, whatever its name.
    run(fake, "packed", source=fake.tmp / "library" / "x" / "alt" / "props.blend")
    assert sorted(p.name for p in (fake.hip / "assets_blender").iterdir()) == folders


def test_asset_name_clashes_get_readable_suffixes(fake):
    nodes, _, _ = run(fake, "packed", selection=[("collection", "Props"), ("object", "Props")])
    assert import_json(fake)["names"]["assets"] == {"collection:Props": "Props", "object:Props": "Props_2"}
    assert [n.node("file1").evalParm("file").rsplit("/", 2)[1] for n in nodes] == ["Props", "Props_2"]
    # A folder nobody recorded counts as someone else's: never written into.
    (fake.assets / "geo" / "Tree").mkdir()
    (fake.assets / "geo" / "Tree" / "mine.txt").write_text("not from Blender")
    run(fake, "packed", selection=[("object", "Tree")], manifest_for=object_manifest)
    assert import_json(fake)["names"]["assets"]["object:Tree"] == "Tree_2"
    assert tree(fake.assets / "geo" / "Tree") == ["mine.txt"]
    # Once given, a name never changes.
    run(fake, "packed", selection=[("object", "Props")])
    assert import_json(fake)["names"]["assets"]["object:Props"] == "Props_2"


# ---------------------------------------------------------------- replace on re-import


def test_reimport_deletes_only_listed_files_of_the_same_asset(fake):
    run(fake, "packed")
    folder = fake.assets / "geo" / "Props"
    (folder / "Props.0009.bgeo.sc").write_text("not listed")
    (folder / "readme.txt").write_text("artist notes")
    _, _, messages = run(fake, "packed", frames=(1, 2, 1))
    assert tree(folder) == ["Props.0001.bgeo.sc", "Props.0002.bgeo.sc", "Props.0009.bgeo.sc", "readme.txt"]
    assert any("1 old file(s) deleted" in text for text, _ in messages)


def test_recovered_json_deletes_nothing(fake):
    run(fake, "packed", frames=(1, 3, 1))
    path = fake.assets / "cycles_import.json"
    data = json.loads(path.read_text())
    data["names"] = "broken"  # the source is still readable, so the folder is this .blend's
    path.write_text(json.dumps(data))
    _, _, messages = run(fake, "packed", frames=(1, 2, 1))
    assert (fake.assets / "cycles_import.json.bad").is_file()
    assert any("unreadable" in text and error for text, error in messages)
    # Nothing of the earlier import is deleted; its folder now counts as unknown.
    assert tree(fake.assets / "geo" / "Props") == [f"Props.000{i}.bgeo.sc" for i in (1, 2, 3)]
    assert tree(fake.assets / "geo" / "Props_2") == ["Props_2.0001.bgeo.sc", "Props_2.0002.bgeo.sc"]
    assert import_json(fake)["names"]["assets"] == {"collection:Props": "Props_2"}


def test_missing_json_deletes_nothing(fake):
    run(fake, "packed", frames=(1, 3, 1))
    (fake.assets / "cycles_import.json").unlink()
    run(fake, "packed", frames=(1, 2, 1))
    # The folder has no owner now: the import goes to a new blend folder and deletes nothing.
    assert tree(fake.assets / "geo" / "Props") == [f"Props.000{i}.bgeo.sc" for i in (1, 2, 3)]
    assert (fake.hip / "assets_blender" / "props__props" / "cycles_import.json").is_file()


def test_foreign_json_fails_cleanly(fake):
    from cycles_houdini.blender import blend_convert

    run(fake, "packed")
    path = fake.assets / "cycles_import.json"
    data = json.loads(path.read_text())
    data["schema"] = 99
    path.write_text(json.dumps(data))
    before = path.read_text(), tree(fake.assets), len(fake.obj.children())
    with pytest.raises(blend_convert.BlendImportError, match="newer version"):
        run(fake, "packed", manifest_for=lambda request: cases.scene_manifest(offset=1.0))
    assert (path.read_text(), tree(fake.assets), len(fake.obj.children())) == before


# ---------------------------------------------------------------- scenes sharing the asset


def test_shared_scene_cancel_and_replace(fake):
    from cycles_houdini.blender import blend_convert

    run(fake, "packed")
    first = (fake.hip / "scene.hip").as_posix()
    other = fake.hip / "shot_b.hip"
    other.write_bytes(b"hip")
    fake.hou.scene.path = str(other)
    found = blend_convert.shared_scenes(str(fake.blend), [("collection", "Props")])
    assert found == [dict(kind="collection", name="Props", asset="Props", scenes=[first])]
    assert blend_convert.shared_scenes(str(fake.blend), [("collection", "Props")], structure="hierarchy") == []
    folder = fake.assets / "geo" / "Props"
    before = (folder / "Props.bgeo.sc").read_text()
    changed = lambda request: cases.scene_manifest(offset=1.0)  # noqa: E731
    with pytest.raises(blend_convert.BlendImportError, match=r'"Props" is also used by: scene\.hip'):
        run(fake, "packed", on_shared="cancel", manifest_for=changed)
    assert (folder / "Props.bgeo.sc").read_text() == before
    assert import_json(fake)["assets"]["Props"]["scenes"] == [first]
    _, _, messages = run(fake, "packed", manifest_for=changed)  # default: replace, and say so
    assert any("also used by: scene.hip" in text and "changes that scene too" in text for text, _ in messages)
    assert (folder / "Props.bgeo.sc").read_text() != before
    assert import_json(fake)["assets"]["Props"]["scenes"] == [first, other.as_posix()]
    # Scenes that no longer exist are not counted.
    (fake.hip / "scene.hip").unlink()
    assert blend_convert.shared_scenes(str(fake.blend), [("collection", "Props")]) == []


# ---------------------------------------------------------------- folders


def test_path_too_long_stops_before_writing(fake, monkeypatch):
    from cycles_houdini.blender import blend_assets_layout, blend_convert

    monkeypatch.setattr(blend_assets_layout, "MAX_PATH", len(str(fake.assets)) + 20)
    with pytest.raises(blend_convert.BlendImportError, match=r"over the \d+-character limit: .*Props\.bgeo\.sc"):
        run(fake, "packed")
    assert not (fake.hip / "assets_blender" / "props").exists() and not fake.built


def test_untitled_scene_writes_to_the_cache(fake):
    fake.hou.scene.new = True
    nodes, _, messages = run(fake, "packed")
    cache = fake.tmp / "cache" / "assets_blender" / "props"
    assert nodes[0].node("file1").evalParm("file") == (cache / "geo" / "Props" / "Props.bgeo.sc").as_posix()
    assert "scenes" not in json.loads((cache / "cycles_import.json").read_text())["assets"]["Props"]
    assert not (fake.hip / "assets_blender").exists()
    assert any("not saved yet" in text for text, _ in messages)


def test_refuses_folders_inside_the_library(fake):
    from cycles_houdini.blender import blend_convert

    inside = fake.blend.parent / "out"
    with pytest.raises(blend_convert.BlendImportError, match="Save the scene") as error:
        cases.snapshot(fake, asset_root=str(inside))
    assert inside.as_posix() in str(error.value) and fake.blend.parent.as_posix() in str(error.value)
    with pytest.raises(blend_convert.BlendImportError, match="inside"):
        cases.snapshot(fake, asset_root=str(fake.tmp / "library" / "x"), library_root=str(fake.tmp / "library"))
    assert not inside.exists()


# ---------------------------------------------------------------- textures


def _png(fake, name, data):
    path = fake.tmp / "library" / "tex" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path.as_posix()


def test_material_textures_packed_in_tex_and_others_in_place(fake):
    wood = b"packed wood"
    bark = _png(fake, "bark.jpg", b"bark")
    gone = (fake.tmp / "library" / "tex" / "gone.png").as_posix()

    def packed():
        dependency = dict(filename="textures/wood.png", sha256=hashlib.sha256(wood).hexdigest(), packed=True)
        return dict(filename="textures/wood.png", tiles=[], packed=True, textures="in_place", dependencies=[dependency])

    def in_place(path, missing=False):
        dependency = dict(filename=path, source_path=path, packed=False, missing=missing)
        return dict(
            filename=path, tiles=[], packed=False, textures="in_place", missing=missing, dependencies=[dependency]
        )

    def manifest_for(request):
        manifest, arrays = cases.scene_manifest()
        red, blue = dict(cases.MATERIALS[0]), dict(cases.MATERIALS[1])
        red["nodes"] = [dict(image=packed()), dict(image=in_place(bark))]
        blue["nodes"] = [dict(image=packed()), dict(image=in_place(gone, missing=True))]
        manifest["materials"] = [red, blue, cases.MATERIALS[2]]
        return manifest, arrays

    def evaluate(source, executable, requests, texture_directory, timeout, source_config=None, consume=None):
        staged = Path(texture_directory, "request-0", "textures")
        staged.mkdir(parents=True, exist_ok=True)
        (staged / "wood.png").write_bytes(wood)
        return cases.fake_evaluate(manifest_for)(
            source, executable, requests, texture_directory, timeout, source_config, consume
        )

    from cycles_houdini.blender import blend_convert

    snap = cases.snapshot(fake)
    payload = blend_convert.prepare_import(snap, evaluate=evaluate)
    messages = []
    blend_convert.build_import(payload, report=lambda text, error=False: messages.append((text, error)))
    # The packed image is extracted once, under its real name, and shared by both materials.
    assert tree(fake.assets / "tex") == ["wood.png"]
    built = {name: prepared for name, prepared, _ in fake.built}
    for prepared in built.values():
        assert prepared["root"] == str(fake.assets / "tex")
        assert prepared["record"]["nodes"][0]["image"]["filename"] == "wood.png"
        assert prepared["record"]["nodes"][0]["image"]["packed"] is True  # still checked by sha256
    # In place: untouched (the path the .blend points to) and recorded for Collect Textures.
    assert built["Red Paint"]["record"]["nodes"][1]["image"]["filename"] == bark
    images = import_json(fake)["images"]
    assert images[bark] == {"original": bark, "packed": False}
    assert images["packed:" + hashlib.sha256(wood).hexdigest()]["file"] == "wood.png"
    assert not (fake.assets / "tex" / "bark.jpg").exists()
    # A missing texture is a warning, not a failure; the report says textures stay in place.
    assert any("gone.png" in text and not error for text, error in messages)
    assert any(text.startswith("Textures stay in place") for text, _ in messages)
    assert sorted(built) == ["Blue", "Red Paint"]
