# SPDX-License-Identifier: Apache-2.0
"""assets_blender layout: names, paths, path length, cycles_import.json, lock, stale files."""

import json
import os
import threading
import time

import pytest
from cycles_houdini.blender import blend_assets_layout as layout

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


# ------------------------------------------------------------------ safe_name


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("geo", "geo"),
        ("tex", "tex"),
        ("Tree", "Tree"),
        ("a/b", "a_b"),
        ("a\\b", "a_b"),
        ('x<>:"|?*y', "x_______y"),
        ("tab\there", "tab_here"),
        ("CON", "_CON"),
        ("con.txt", "_con.txt"),
        ("Lpt9", "_Lpt9"),
        ("COM1.tar.gz", "_COM1.tar.gz"),
        ("CONSOLE", "CONSOLE"),
        ("COM0", "COM0"),
        ("name. ", "name"),
        ("name...", "name"),
        ("  padded  ", "padded"),
        ("$HIP/tex", "_HIP_tex"),
        ("a`b`", "a_b_"),
        ("Baum Äste ø 木", "Baum Äste ø 木"),
        ("my tree", "my tree"),
        ("x" * 300, "x" * 300),
    ],
)
def test_safe_name(text, expected):
    assert layout.safe_name(text, "fallback") == expected


@pytest.mark.parametrize("text", ["", "   ", ". .", "..", None])
def test_safe_name_empty_gives_fallback(text):
    assert layout.safe_name(text, "asset") == "asset"


def test_safe_image_name_keeps_udim_token():
    assert layout.safe_image_name("C:\\lib\\wood:bark.<UDIM>.exr") == "wood_bark.<UDIM>.exr"
    assert layout.safe_image_name("//textures/wood.<udim>.exr") == "wood.<UDIM>.exr"
    assert layout.safe_image_name("CON.<UDIM>.png") == "_CON.<UDIM>.png"


# ------------------------------------------------------------------ unique names


def test_unique_name_case_insensitive_and_suffixes():
    assert layout.unique_name("tree", ["Tree"]) == "tree_2"
    assert layout.unique_name("tree", ["Tree", "TREE_2"]) == "tree_3"
    assert layout.unique_name("a_b", ["x"]) == "a_b"
    assert layout.unique_name("wood_diff.png", ["wood_diff.png"], file=True) == "wood_diff_2.png"
    assert layout.unique_name("wood.<UDIM>.exr", ["WOOD.1001.exr"], file=True) == "wood_2.<UDIM>.exr"
    assert layout.unique_name("wood.1001.exr", ["wood.<UDIM>.exr"], file=True) == "wood_2.1001.exr"
    assert layout.unique_name("noext", ["noext"], file=True) == "noext_2"


def test_image_tile_name():
    assert layout.image_tile_name("wood_2.<UDIM>.exr", 1003) == "wood_2.1003.exr"
    assert layout.image_tile_name("wood_2.1001.exr", 1002) == "wood_2.1002.exr"
    assert layout.image_tile_name("shot_0001.png", 12) == "shot_0012.png"
    assert layout.image_tile_name("plain.png", 1001) == "plain.png"
    assert layout.image_tile_name("plain.png", None) == "plain.png"


# ------------------------------------------------------------------ blend folders


def _make_blend_folder(root, name, source):
    folder = root / name
    folder.mkdir(parents=True)
    store = layout.ImportStore.load(folder)
    store.set_source(source, None)
    store.save()
    return folder


def test_blend_folder_named_after_stem(tmp_path):
    root = tmp_path / "assets_blender"
    assert layout.blend_folder(root, "D:\\Poly Haven\\materials.blend") == root / "materials"
    assert layout.blend_folder(root, "/lib/geo.blend") == root / "geo"
    assert layout.blend_folder(root, "C:/lib/CON.blend") == root / "_CON"


def test_same_stem_blends_from_different_parents(tmp_path):
    root = tmp_path / "assets_blender"
    first = "D:/Poly Haven/materials.blend"
    second = "E:\\Other Lib\\Materials.blend"
    third = "F:/x/Other Lib/materials.blend"
    assert layout.blend_folder(root, first) == root / "materials"
    _make_blend_folder(root, "materials", first)
    assert layout.blend_folder(root, second) == root / "Materials__Other Lib"
    _make_blend_folder(root, "Materials__Other Lib", second)
    assert layout.blend_folder(root, third) == root / "materials__Other Lib_2"
    _make_blend_folder(root, "materials__Other Lib_2", third)
    # Stable: each source finds its own folder again, whatever its spelling.
    assert layout.blend_folder(root, "d:\\poly haven\\MATERIALS.blend") == root / "materials"
    assert layout.blend_folder(root, second) == root / "Materials__Other Lib"
    assert layout.blend_folder(root, third) == root / "materials__Other Lib_2"


def test_existing_blend_folder_without_json_is_owned_by_unknown_source(tmp_path):
    """Pinned: with no readable JSON there is no evidence of ownership, so the folder is
    never reused, even if the same .blend wrote it (its JSON was lost)."""
    root = tmp_path / "assets_blender"
    (root / "tree" / "geo").mkdir(parents=True)
    assert layout.blend_folder(root, "C:/lib/tree.blend") == root / "tree__lib"
    (root / "other").mkdir()
    (root / "other" / layout.JSON_NAME).write_text("{broken", encoding="utf-8")
    assert layout.blend_folder(root, "C:/lib/other.blend") == root / "other__lib"


# ------------------------------------------------------------------ store names


def test_asset_names_unique_case_insensitive_and_stable(tmp_path):
    folder = tmp_path / "blend"
    store = layout.ImportStore.load(folder)
    assert store.asset_name("OB:Tree", "Tree") == "Tree"
    assert store.asset_name("OB:tree", "tree") == "tree_2"
    assert store.asset_name("OB:a/b", "a/b") == "a_b"
    assert store.asset_name("OB:a_b", "a_b") == "a_b_2"
    assert store.asset_name("OB:geo", "geo") == "geo"
    assert store.asset_name("OB:Tree", "Tree") == "Tree"
    store.save()
    again = layout.ImportStore.load(folder)
    assert again.state == "ok"
    assert again.asset_name("OB:tree", "tree") == "tree_2"
    assert again.asset_name("OB:a_b", "a_b") == "a_b_2"
    # A renamed Blender object keeps its folder: the original is the identity.
    assert again.asset_name("OB:Tree", "Renamed") == "Tree"


def test_unrecorded_asset_folder_on_disk_counts_as_taken(tmp_path):
    folder = tmp_path / "blend"
    (folder / "geo" / "rock").mkdir(parents=True)
    store = layout.ImportStore.load(folder)
    assert store.asset_name("OB:rock", "rock") == "rock_2"


def test_object_and_shape_names_have_their_own_scope(tmp_path):
    store = layout.ImportStore.load(tmp_path)
    store.asset_name("CO:set", "set")
    assert store.object_name("set", "OB:Leaf", "Leaf") == "Leaf"
    assert store.object_name("set", "OB:leaf", "leaf") == "leaf_2"
    assert store.shape_name("set", "ME:Leaf", "Leaf") == "Leaf"
    assert store.object_name("other", "OB:leaf", "leaf") == "leaf"
    assert store.object_name("set", "OB:Leaf", "Leaf") == "Leaf"
    assert layout.hierarchy_file("leaf_2") == "hierarchy/leaf_2.bgeo.sc"
    assert layout.shape_file("Leaf") == "hierarchy/shapes/Leaf.bgeo.sc"


def test_udim_set_allocated_on_one_stem(tmp_path):
    store = layout.ImportStore.load(tmp_path)
    assert store.image_name("IM:wood", "//tex/wood.<UDIM>.exr") == "wood.<UDIM>.exr"
    other = store.image_name("IM:wood_other", "D:\\b\\Wood.<UDIM>.exr")
    assert other == "Wood_2.<UDIM>.exr"
    tiles = [layout.image_tile_name(other, n) for n in (1001, 1002, 1011)]
    assert tiles == ["Wood_2.1001.exr", "Wood_2.1002.exr", "Wood_2.1011.exr"]
    # One tile of the same family from a third image clashes as a whole set.
    assert store.image_name("IM:single", "wood.1001.exr") == "wood_3.1001.exr"
    assert store.image_name("IM:diff", "wood_diff.png") == "wood_diff.png"
    assert store.image_name("IM:diff2", "WOOD_DIFF.png") == "WOOD_DIFF_2.png"
    assert store.image_name("IM:wood", "anything.png") == "wood.<UDIM>.exr"


def test_unrecorded_texture_on_disk_counts_as_taken(tmp_path):
    (tmp_path / "tex").mkdir()
    (tmp_path / "tex" / "bark.1001.jpg").write_bytes(b"x")
    store = layout.ImportStore.load(tmp_path)
    assert store.image_name("IM:bark", "bark.<UDIM>.jpg") == "bark_2.<UDIM>.jpg"


# ------------------------------------------------------------------ paths


def test_paths(tmp_path):
    blend = tmp_path / "assets_blender" / "tree"
    assert layout.asset_root(tmp_path) == tmp_path / "assets_blender"
    assert layout.asset_folder(blend, "Tree") == blend / "geo" / "Tree"
    assert layout.tex_folder(blend) == blend / "tex"
    assert layout.json_path(blend) == blend / "cycles_import.json"
    assert layout.geometry_file("Tree") == "Tree.bgeo.sc"
    assert layout.geometry_file("Tree", 1) == "Tree.0001.bgeo.sc"
    assert layout.geometry_pattern("Tree") == "Tree.bgeo.sc"
    assert layout.geometry_pattern("Tree", sequence=True) == "Tree.$F4.bgeo.sc"


def test_path_length_check():
    short = "C:/p/" + "a" * 235  # 240 characters: allowed
    assert layout.check_path_lengths([short, "C:/x"]) == 240
    assert layout.check_path_lengths([]) == 0
    long = short + "b"
    with pytest.raises(layout.PathTooLongError) as caught:
        layout.check_path_lengths(["C:/x", long, short])
    assert isinstance(caught.value, ValueError)
    assert caught.value.path == long
    assert long in str(caught.value) and "240" in str(caught.value)


def test_same_path_windows_style():
    assert layout.same_path("C:\\Shots\\A.hip", "c:/shots/a.HIP")
    assert layout.same_path("C:/shots//x/../a.hip", "C:\\shots\\a.hip")
    assert not layout.same_path("C:/shots/a.hip", "C:/shots/b.hip")
    assert not layout.same_path(None, "C:/a")


@pytest.mark.parametrize("bad", ["../x", "a/../../x", "/abs", "C:/x", "C:x", "", "a//b", "./a"])
def test_relative_path_rejects_escapes(bad):
    with pytest.raises(layout.UnsafePathError):
        layout.relative_path(bad)


def test_relative_path_is_stored_posix():
    assert layout.relative_path("hierarchy\\shapes\\a.bgeo.sc") == "hierarchy/shapes/a.bgeo.sc"


# ------------------------------------------------------------------ cycles_import.json


def test_missing_json_is_new_and_deletes_nothing(tmp_path):
    store = layout.ImportStore.load(tmp_path)
    assert store.state == "new" and store.writable and not store.can_delete
    store.asset_name("OB:a", "a")
    assert store.stale_files("OB:a", []) == []


def test_corrupt_json_renamed_to_bad(tmp_path):
    path = tmp_path / layout.JSON_NAME
    (tmp_path / layout.JSON_BAD_NAME).write_text("older bad", encoding="utf-8")
    path.write_text("{not json", encoding="utf-8")
    store = layout.ImportStore.load(tmp_path)
    assert store.state == "recovered" and store.writable and not store.can_delete
    assert not path.exists()
    assert (tmp_path / layout.JSON_BAD_NAME).read_text(encoding="utf-8") == "{not json"
    assert store.notes and layout.JSON_BAD_NAME in store.notes[0]
    store.asset_name("OB:a", "a")
    store.record_asset("OB:a", options={}, frames=[1], files={"a.bgeo.sc": "d"})
    assert store.stale_files("OB:a", []) == []  # still nothing deleted this run
    store.save()
    assert layout.ImportStore.load(tmp_path).state == "ok"


@pytest.mark.parametrize("text", ["[]", '{"schema": "1"}', '{"schema": 1}', '{"schema": 0, "names": {}}'])
def test_wrong_shape_json_is_recovered(tmp_path, text):
    (tmp_path / layout.JSON_NAME).write_text(text, encoding="utf-8")
    assert layout.ImportStore.load(tmp_path).state == "recovered"


def test_newer_schema_is_foreign_and_untouched(tmp_path):
    path = tmp_path / layout.JSON_NAME
    text = json.dumps({"schema": layout.SCHEMA + 1, "whatever": True})
    path.write_text(text, encoding="utf-8")
    store = layout.ImportStore.load(tmp_path)
    assert store.state == "foreign" and not store.writable and not store.can_delete
    with pytest.raises(layout.ForeignStoreError, match="newer version"):
        store.save()
    for call in (
        lambda: store.asset_name("OB:a", "a"),
        lambda: store.image_name("IM:a", "a.png"),
        lambda: store.add_scene("OB:a", "C:/a.hip"),
        lambda: store.record_image("IM:a", original="a.png", packed=False),
    ):
        with pytest.raises(layout.ForeignStoreError):
            call()
    assert store.asset_record("OB:a") is None and store.image_record("IM:a") is None
    assert store.other_scenes("OB:a", "C:/a.hip") == [] and store.asset_files("OB:a") == {}
    assert path.read_text(encoding="utf-8") == text
    assert not (tmp_path / layout.JSON_BAD_NAME).exists()


def test_save_is_atomic_and_leaves_no_tmp(tmp_path):
    folder = tmp_path / "new" / "blend"
    store = layout.ImportStore.load(folder)
    store.set_source("C:\\lib\\tree.blend", "1:2:3")
    path = store.save()
    assert path == folder / layout.JSON_NAME
    assert sorted(p.name for p in folder.iterdir()) == [layout.JSON_NAME]
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema"] == 1
    assert data["source"] == {"path": "C:/lib/tree.blend", "signature": ["1", "2", "3"]}
    again = layout.ImportStore.load(folder)
    assert again.source_path == "C:/lib/tree.blend"
    assert again.same_signature("1:2:3") and not again.same_signature("1:2:4")
    assert not again.same_signature(None)


def test_save_failure_removes_tmp(tmp_path, monkeypatch):
    store = layout.ImportStore.load(tmp_path)

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(layout.os, "replace", fail)
    with pytest.raises(OSError):
        store.save()
    assert not (tmp_path / layout.JSON_TMP_NAME).exists()


def test_asset_record_round_trip(tmp_path):
    store = layout.ImportStore.load(tmp_path)
    store.asset_name("OB:Tree", "Tree")
    store.record_asset(
        "OB:Tree",
        options={"apply_subdivision": True},
        frames=[1, 2],
        files={"Tree.0002.bgeo.sc": "d2", "Tree.0001.bgeo.sc": "d1", "hierarchy\\Leaf.bgeo.sc": "d3"},
    )
    store.save()
    again = layout.ImportStore.load(tmp_path)
    record = again.asset_record("OB:Tree")
    assert record["options"] == {"apply_subdivision": True} and record["frames"] == [1, 2]
    assert again.asset_files("OB:Tree") == {
        "Tree.0001.bgeo.sc": "d1",
        "Tree.0002.bgeo.sc": "d2",
        "hierarchy/Leaf.bgeo.sc": "d3",
    }
    with pytest.raises(KeyError):
        again.record_asset("OB:unknown", options={}, frames=[], files={})
    with pytest.raises(layout.UnsafePathError):
        again.record_asset("OB:Tree", options={}, frames=[], files={"../x": "d"})


def test_image_record(tmp_path):
    store = layout.ImportStore.load(tmp_path)
    name = store.image_name("IM:bark", "D:\\lib\\bark.jpg")
    store.record_image(
        "IM:bark",
        original="D:\\lib\\bark.jpg",
        packed=False,
        sha256="abc",
        library_root="D:\\lib",
        relative_path="bark.jpg",
    )
    store.record_image("IM:packed", original=None, packed=True, file="p.png", sha256="def")
    store.save()
    again = layout.ImportStore.load(tmp_path)
    assert again.image_record("IM:bark") == {
        "original": "D:/lib/bark.jpg",
        "packed": False,
        "file": name,
        "sha256": "abc",
        "library_root": "D:/lib",
        "relative_path": "bark.jpg",
    }
    assert again.image_record("IM:packed") == {"original": None, "packed": True, "file": "p.png", "sha256": "def"}
    assert again.image_record("IM:none") is None


# ------------------------------------------------------------------ lock


def test_lock_is_exclusive_and_removed(tmp_path):
    folder = tmp_path / "blend"
    with layout.import_lock(folder) as lock:
        assert lock.exists()
        with pytest.raises(layout.ImportLockError, match="Another import"):
            with layout.import_lock(folder, timeout=0.05, poll=0.01):
                pass
    assert not (folder / layout.LOCK_NAME).exists()


def test_lock_removed_on_error(tmp_path):
    with pytest.raises(RuntimeError):
        with layout.import_lock(tmp_path):
            raise RuntimeError("boom")
    assert not (tmp_path / layout.LOCK_NAME).exists()


def test_stale_lock_is_broken(tmp_path):
    lock = tmp_path / layout.LOCK_NAME
    lock.write_text("old", encoding="utf-8")
    old = time.time() - 700
    os.utime(lock, (old, old))
    with layout.import_lock(tmp_path, timeout=0.05, poll=0.01):
        assert lock.read_text(encoding="utf-8") != "old"
    assert not lock.exists()


def test_fresh_lock_waits_then_succeeds(tmp_path):
    lock = tmp_path / layout.LOCK_NAME
    lock.write_text("other", encoding="utf-8")
    threading.Timer(0.1, lock.unlink).start()
    with layout.import_lock(tmp_path, timeout=5, poll=0.01):
        pass


def test_lock_not_removed_when_taken_over(tmp_path):
    lock = tmp_path / layout.LOCK_NAME
    with layout.import_lock(tmp_path):
        lock.write_text("someone else", encoding="utf-8")  # broke ours as stale and took it
    assert lock.read_text(encoding="utf-8") == "someone else"


def test_open_store_locks_and_loads(tmp_path):
    with layout.open_store(tmp_path, timeout=0.05, poll=0.01) as store:
        assert (tmp_path / layout.LOCK_NAME).exists()
        store.asset_name("OB:a", "a")
        store.save()
    assert not (tmp_path / layout.LOCK_NAME).exists()
    assert layout.ImportStore.load(tmp_path).asset_name("OB:a") == "a"


# ------------------------------------------------------------------ replace on re-import


def _import(folder, files, frames=(1,)):
    with layout.open_store(folder) as store:
        asset = store.asset_name("OB:Tree", "Tree")
        stale = store.stale_files("OB:Tree", files)
        target = layout.asset_folder(folder, asset)
        for name in files:
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name, encoding="utf-8")
        store.record_asset("OB:Tree", options={}, frames=list(frames), files=files)
        store.save()
    return target, stale


def test_reimport_with_fewer_frames_deletes_only_listed_files(tmp_path):
    first = {layout.geometry_file("Tree", f): "d" for f in (1, 2, 3)}
    first[layout.hierarchy_file("Leaf")] = "h"
    first[layout.shape_file("Leaf")] = "s"
    target, stale = _import(tmp_path, first, frames=(1, 2, 3))
    assert stale == []
    (target / "notes.txt").write_text("artist file", encoding="utf-8")
    second = {"tree.0001.bgeo.sc": "d"}  # differs only in case from Tree.0001: same file on Windows
    target, stale = _import(tmp_path, second)
    assert stale == [
        "Tree.0002.bgeo.sc",
        "Tree.0003.bgeo.sc",
        "hierarchy/Leaf.bgeo.sc",
        "hierarchy/shapes/Leaf.bgeo.sc",
    ]
    (target / "Tree.0003.bgeo.sc").unlink()  # already missing: ignored
    result = layout.delete_stale(target, stale)
    assert result.removed == ["Tree.0002.bgeo.sc", "hierarchy/Leaf.bgeo.sc", "hierarchy/shapes/Leaf.bgeo.sc"]
    assert result.failed == []
    # A case-insensitive file system (Windows) kept one file under its first name.
    same_file = (target / "TREE.0001.bgeo.sc").exists()
    expected = ["Tree.0001.bgeo.sc", "notes.txt"] + ([] if same_file else ["tree.0001.bgeo.sc"])
    assert sorted(p.name for p in target.iterdir()) == expected


def test_delete_stale_keeps_non_empty_hierarchy(tmp_path):
    (tmp_path / "hierarchy" / "shapes").mkdir(parents=True)
    (tmp_path / "hierarchy" / "shapes" / "a.bgeo.sc").write_text("a", encoding="utf-8")
    (tmp_path / "hierarchy" / "keep.bgeo.sc").write_text("k", encoding="utf-8")
    layout.delete_stale(tmp_path, ["hierarchy/shapes/a.bgeo.sc"])
    assert not (tmp_path / "hierarchy" / "shapes").exists()
    assert (tmp_path / "hierarchy" / "keep.bgeo.sc").exists()


@pytest.mark.parametrize("escape", ["../outside.txt", "/etc/passwd", "C:/x.bgeo.sc", "a/../../outside.txt"])
def test_delete_stale_refuses_escape_and_deletes_nothing(tmp_path, escape):
    asset = tmp_path / "asset"
    asset.mkdir()
    (asset / "a.bgeo.sc").write_text("a", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("o", encoding="utf-8")
    with pytest.raises(layout.UnsafePathError):
        layout.delete_stale(asset, ["a.bgeo.sc", escape])
    assert (asset / "a.bgeo.sc").exists() and (tmp_path / "outside.txt").exists()


def test_delete_stale_refuses_symlink_escape(tmp_path):
    asset = tmp_path / "asset"
    asset.mkdir()
    (tmp_path / "outside").mkdir()
    try:
        (asset / "link").symlink_to(tmp_path / "outside", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    (tmp_path / "outside" / "f.bgeo.sc").write_text("o", encoding="utf-8")
    with pytest.raises(layout.UnsafePathError):
        layout.delete_stale(asset, ["link/f.bgeo.sc"])
    assert (tmp_path / "outside" / "f.bgeo.sc").exists()


def test_no_stale_files_after_recovered_or_foreign(tmp_path):
    _import(tmp_path, {"Tree.0001.bgeo.sc": "d", "Tree.0002.bgeo.sc": "d"})
    path = tmp_path / layout.JSON_NAME
    data = json.loads(path.read_text(encoding="utf-8"))
    data["schema"] = 99
    path.write_text(json.dumps(data), encoding="utf-8")
    assert layout.ImportStore.load(tmp_path).stale_files("OB:Tree", []) == []
    path.write_text("garbage", encoding="utf-8")
    assert layout.ImportStore.load(tmp_path).stale_files("OB:Tree", []) == []


# ------------------------------------------------------------------ scenes


def test_scenes_add_drop_and_shared_query(tmp_path):
    existing = {"c:/shots/shot_v01.hip", "c:/shots/shot_v02.hip", "c:/shots/old.hip"}

    def exists(path):
        return path.replace("\\", "/").lower() in existing

    store = layout.ImportStore.load(tmp_path)
    store.asset_name("OB:Tree", "Tree")
    store.add_scene("OB:Tree", "C:\\Shots\\old.hip", exists=exists)
    store.add_scene("OB:Tree", "C:\\Shots\\shot_v01.hip", exists=exists)
    store.add_scene("OB:Tree", "c:/shots/SHOT_V01.hip", exists=exists)  # same scene: not repeated
    assert store.asset_record("OB:Tree")["scenes"] == ["C:/Shots/old.hip", "c:/shots/SHOT_V01.hip"]
    # The shared-scene query from shot_v02 names the others that still exist.
    assert store.other_scenes("OB:Tree", "C:\\Shots\\shot_v02.hip", exists=exists) == [
        "C:/Shots/old.hip",
        "c:/shots/SHOT_V01.hip",
    ]
    existing.discard("c:/shots/old.hip")
    assert store.other_scenes("OB:Tree", "C:\\Shots\\shot_v02.hip", exists=exists) == ["c:/shots/SHOT_V01.hip"]
    # Recording from shot_v02 drops the deleted scene and adds the current one.
    store.record_asset("OB:Tree", options={}, frames=[1], files={}, scene="C:\\Shots\\shot_v02.hip", exists=exists)
    assert store.asset_record("OB:Tree")["scenes"] == ["c:/shots/SHOT_V01.hip", "C:/Shots/shot_v02.hip"]
    assert store.other_scenes("OB:Tree", "C:/SHOTS/shot_v02.hip", exists=exists) == ["c:/shots/SHOT_V01.hip"]
    assert store.other_scenes("OB:Tree", "C:/Shots/shot_v01.hip", exists=exists) == ["C:/Shots/shot_v02.hip"]
    assert store.other_scenes("OB:Unknown", "C:/Shots/shot_v01.hip", exists=exists) == []


def test_blend_folder_name_from_blender_relative_and_drive_root(tmp_path):
    assert layout.blend_folder_name("//libs/Tree.BLEND") == "Tree"
    assert layout.blend_folder_name("tree.blend1") == "tree.blend1"
    root = tmp_path / "assets_blender"
    (root / "x").mkdir(parents=True)
    assert layout.blend_folder(root, "C:\\x.blend") == root / "x__root"
