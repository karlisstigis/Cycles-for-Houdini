# SPDX-License-Identifier: Apache-2.0
"""Relink Blender Assets (blend-import.md: Collect and relink): pure matching of missing files under a chosen folder."""

from pathlib import Path

import pytest

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


def entry(node, path, kind="texture", parm="filename"):
    from cycles_houdini.assets.scene_paths import PathEntry

    return PathEntry(node, parm, str(path), str(path), None, kind)


def write(path, data=b"x"):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_unique_ambiguous_and_not_found(tmp_path):
    from cycles_houdini.assets.relink import is_missing, plan_relink

    old, new = tmp_path / "old", tmp_path / "new"
    write(new / "a" / "Wood.JPG")  # case-insensitive
    write(new / "a" / "bark.jpg")
    write(new / "b" / "bark.jpg")
    entries = [
        entry("/mat/m/t1", old / "wood.jpg"),
        entry("/mat/m/t2", old / "bark.jpg"),
        entry("/mat/m/t3", old / "rock.jpg"),
    ]
    assert all(is_missing(e) for e in entries)
    plan = plan_relink(entries, new)
    assert [(e.node, path) for e, path in plan.found] == [("/mat/m/t1", (new / "a" / "Wood.JPG").as_posix())]
    assert [(e.node, sorted(c)) for e, c in plan.ambiguous] == [
        ("/mat/m/t2", [(new / "a" / "bark.jpg").as_posix(), (new / "b" / "bark.jpg").as_posix()])
    ]
    assert [e.node for e in plan.not_found] == ["/mat/m/t3"]
    assert "Found 1, several candidates 1, not found 1." in plan.text() and "rock.jpg" in plan.details()


def test_patterns_match_as_patterns_and_keep_the_token(tmp_path):
    from cycles_houdini.assets.relink import plan_relink

    new = tmp_path / "new"
    write(new / "udim" / "wood.1003.exr")  # one tile is enough
    write(new / "seq" / "fire.0012.png")
    write(new / "other" / "fire.png")  # a plain file of the same stem is no frame
    entries = [
        entry("/mat/m/u", tmp_path / "old" / "wood.<UDIM>.exr"),
        entry("/mat/m/f", tmp_path / "old" / "fire.$F4.png"),
        entry("/mat/m/g", tmp_path / "old" / "smoke.$F4.png"),
    ]
    plan = plan_relink(entries, new)
    assert [path for _, path in plan.found] == [
        (new / "udim" / "wood.<UDIM>.exr").as_posix(),
        (new / "seq" / "fire.$F4.png").as_posix(),
    ]
    assert [e.node for e in plan.not_found] == ["/mat/m/g"]


def test_json_relative_path_is_tried_first_without_walking(tmp_path, monkeypatch):
    from cycles_houdini.assets import relink

    new = tmp_path / "moved_library"
    write(new / "textures" / "wood.jpg")
    write(new / "elsewhere" / "wood.jpg")  # would make a name search ambiguous

    def no_walk(*args, **kwargs):
        raise AssertionError("the folder must not be walked when every entry has a hint")

    monkeypatch.setattr(relink, "index_folder", no_walk)
    item = entry("/mat/m/t", tmp_path / "library" / "textures" / "wood.jpg")
    plan = relink.plan_relink([item], new, hints={("/mat/m/t", "filename"): ["textures/wood.jpg"]})
    assert [path for _, path in plan.found] == [(new / "textures" / "wood.jpg").as_posix()]


def test_capped_walk_is_reported(tmp_path):
    from cycles_houdini.assets.relink import index_folder, plan_relink

    for index in range(5):
        write(tmp_path / "new" / f"f{index}.jpg")
    index = index_folder(tmp_path / "new", limit=3)
    assert index.capped and index.count == 3
    plan = plan_relink([entry("/mat/m/t", tmp_path / "old" / "f4.jpg")], tmp_path / "new", limit=3)
    assert plan.capped and plan.searched == 3 and "stopped after 3 files" in plan.text()


def test_rewrites_use_hip_inside_the_scene_folder_and_absolute_paths_outside(tmp_path):
    from cycles_houdini.assets.relink import plan_relink, rewrites

    hip = tmp_path / "shot"
    write(hip / "assets_blender" / "metals" / "geo" / "tree" / "tree.bgeo.sc")
    write(tmp_path / "library" / "metals.blend")
    entries = [
        entry("/obj/geo/file1", tmp_path / "old" / "tree.bgeo.sc", "geometry", "file"),
        entry("/obj/geo/ref", tmp_path / "old" / "metals.blend", "blend", "file"),
    ]
    plan = plan_relink(entries, tmp_path)
    assert rewrites(plan, str(hip)) == [
        ("/obj/geo/file1", "file", "$HIP/assets_blender/metals/geo/tree/tree.bgeo.sc"),
        ("/obj/geo/ref", "file", (tmp_path / "library" / "metals.blend").as_posix()),
    ]


def test_existing_files_and_unsupported_patterns_are_not_missing(tmp_path):
    from cycles_houdini.assets.relink import is_missing

    present = write(tmp_path / "a.1001.exr")
    assert not is_missing(entry("/m", present))
    assert not is_missing(entry("/m", tmp_path / "a.<UDIM>.exr"))
    assert not is_missing(entry("/m", tmp_path / "$F4" / "a.<UDIM>.exr"))  # unsupported: left alone
