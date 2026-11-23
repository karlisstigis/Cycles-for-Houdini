# SPDX-License-Identifier: Apache-2.0
"""Move Imported Assets into Project (blend-import.md: Collect and relink): move, merge and path rewriting, pure."""

import json
from pathlib import Path

import pytest

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


def write(path, data=b"x"):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def blend_dir(root, source, files, images=(), assets=()):
    """A blend folder with files and a JSON naming ``source``, image and asset records."""
    from cycles_houdini.blender import blend_assets_layout as layout

    folder = Path(root) / layout.blend_folder_name(source)
    with layout.open_store(folder) as store:
        store.set_source(str(source), ["sig"])
        for key, file in images:
            store.image_name(key, file)
            store.record_image(key, original=key, packed=True, file=file, sha256="s")
        for original, folder_name in assets:
            store.asset_name(original, folder_name)
            store.record_asset(original, options={"o": 1}, frames=[1], files={folder_name + ".bgeo.sc": "d"})
        store.save()
    for rel, data in files.items():  # after allocation: files on disk count as owned by someone else
        write(folder / rel, data)
    return folder


def load(folder):
    return json.loads((Path(folder) / "cycles_import.json").read_text(encoding="utf-8"))


def test_plain_move_when_the_project_has_no_folder_for_the_blend(tmp_path):
    from cycles_houdini.assets.move_project import move_blend_folder, rewrite_path

    source = blend_dir(
        tmp_path / "cache", tmp_path / "lib/metals.blend", {"geo/tree/tree.0001.bgeo.sc": b"g", "tex/bark.jpg": b"b"}
    )
    result = move_blend_folder(source, tmp_path / "shot/assets_blender")
    target = tmp_path / "shot/assets_blender/metals"
    assert result.target == target and not result.merged and not source.exists()
    assert (target / "geo/tree/tree.0001.bgeo.sc").read_bytes() == b"g"
    assert (
        rewrite_path(str(source / "geo/tree/tree.$F4.bgeo.sc"), result)
        == (target / "geo/tree/tree.$F4.bgeo.sc").as_posix()
    )
    assert rewrite_path(str(tmp_path / "elsewhere.jpg"), result) is None


def test_move_uses_the_naming_rule_when_another_blend_owns_the_name(tmp_path):
    from cycles_houdini.assets.move_project import move_blend_folder

    blend_dir(tmp_path / "shot/assets_blender", tmp_path / "other/metals.blend", {"tex/a.jpg": b"a"})
    source = blend_dir(tmp_path / "cache", tmp_path / "lib/metals.blend", {"tex/a.jpg": b"a"})
    result = move_blend_folder(source, tmp_path / "shot/assets_blender")
    assert result.target.name == "metals__lib" and not result.merged


def test_merge_same_content_drops_the_cache_copy(tmp_path):
    from cycles_houdini.assets.move_project import move_blend_folder, rewrite_path

    blend = tmp_path / "lib/metals.blend"
    files = {"geo/tree/tree.bgeo.sc": b"g", "tex/bark.jpg": b"b"}
    target = blend_dir(tmp_path / "shot/assets_blender", blend, files)
    source = blend_dir(tmp_path / "cache", blend, files)
    result = move_blend_folder(source, tmp_path / "shot/assets_blender")
    assert result.merged and result.target == target and result.kept_both == []
    assert sorted(result.dropped) == ["geo/tree/tree.bgeo.sc", "tex/bark.jpg"] and not source.exists()
    assert rewrite_path(str(source / "tex/bark.jpg"), result) == (target / "tex/bark.jpg").as_posix()


def test_merge_different_content_keeps_both_with_the_naming_rule(tmp_path):
    from cycles_houdini.assets.move_project import move_blend_folder, rewrite_path

    blend = tmp_path / "lib/metals.blend"
    target = blend_dir(
        tmp_path / "shot/assets_blender",
        blend,
        {
            "geo/tree/tree.bgeo.sc": b"project",
            "tex/wood.1001.exr": b"p1",
            "tex/wood.1002.exr": b"p2",
            "tex/rock.jpg": b"r",
        },
    )
    source = blend_dir(
        tmp_path / "cache",
        blend,
        {
            "geo/tree/tree.bgeo.sc": b"cache",
            "tex/wood.1001.exr": b"p1",
            "tex/wood.1002.exr": b"c2",
            "tex/new.jpg": b"n",
        },
    )
    result = move_blend_folder(source, tmp_path / "shot/assets_blender")
    assert (target / "geo/tree/tree.bgeo.sc").read_bytes() == b"project"
    assert (target / "geo/tree_2/tree.bgeo.sc").read_bytes() == b"cache"
    assert (target / "tex/wood_2.1001.exr").read_bytes() == b"p1" and (
        target / "tex/wood_2.1002.exr"
    ).read_bytes() == b"c2"
    assert (target / "tex/wood.1002.exr").read_bytes() == b"p2" and (target / "tex/new.jpg").read_bytes() == b"n"
    assert ("geo/tree", "geo/tree_2") in result.kept_both
    assert (
        rewrite_path(str(source / "geo/tree/tree.bgeo.sc"), result) == (target / "geo/tree_2/tree.bgeo.sc").as_posix()
    )
    assert rewrite_path(str(source / "tex/wood.<UDIM>.exr"), result) == (target / "tex/wood_2.<UDIM>.exr").as_posix()
    assert rewrite_path(str(source / "tex/new.jpg"), result) == (target / "tex/new.jpg").as_posix()


def test_merge_moves_json_records_through_the_store(tmp_path):
    from cycles_houdini.assets.move_project import move_blend_folder

    blend = tmp_path / "lib/metals.blend"
    hip = tmp_path / "shot/scene.hip"
    write(hip, b"hip")
    target = blend_dir(tmp_path / "shot/assets_blender", blend, {"tex/a.jpg": b"a"}, images=[("A", "a.jpg")])
    source = blend_dir(
        tmp_path / "cache",
        blend,
        {"tex/b.jpg": b"b", "geo/tree/tree.bgeo.sc": b"g"},
        images=[("B", "b.jpg")],
        assets=[("Tree", "tree")],
    )
    move_blend_folder(source, tmp_path / "shot/assets_blender", hip=hip.as_posix())
    data = load(target)
    assert sorted(data["images"]) == ["A", "B"] and data["images"]["B"]["file"] == "b.jpg"
    assert data["names"]["assets"] == {"Tree": "tree"} and data["assets"]["tree"]["options"] == {"o": 1}
    assert data["assets"]["tree"]["scenes"] == [hip.as_posix()]
    assert data["source"]["path"] == blend.as_posix() and not source.exists()


def test_folder_without_json_is_moved_beside_an_existing_one(tmp_path):
    from cycles_houdini.assets.move_project import move_blend_folder

    write(tmp_path / "shot/assets_blender/metals/tex/a.jpg", b"project")
    source = tmp_path / "cache/metals"
    write(source / "tex/a.jpg", b"cache")
    result = move_blend_folder(source, tmp_path / "shot/assets_blender")
    assert result.target.name == "metals_2" and (result.target / "tex/a.jpg").read_bytes() == b"cache"
