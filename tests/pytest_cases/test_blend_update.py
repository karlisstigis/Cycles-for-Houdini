# SPDX-License-Identifier: Apache-2.0
"""Update on Cycles Blender File nodes and Blender Hierarchy objects (fake `hou`; blend-import.md: Asset nodes).

blend_convert.update_import = update_snapshot (host) + prepare_update (no HOM) + sync_import
(host, one undo group). Covers: unchanged source (nothing rewritten), changed geometry, fewer
frames, new and removed subdivision parts, Stash in .hip on and off, several nodes of one
asset, linked materials reloaded through the hook, editable ones left alone, new ones
imported, the shared-scene cancel, Link nodes and missing sources, and the hierarchy object's
new, removed and hand-moved objects, artist-added nodes and Material SOP entries.
"""

import sys
import threading
from types import ModuleType

import numpy as np
import pytest

import test_blend_convert as cases
import test_blend_file_node as nodes_cases

pytestmark = [pytest.mark.pure, pytest.mark.cpu]
fake = cases.fake
assets = nodes_cases.assets


def update(fake, nodes, manifest_for=None, hook=None, calls=None, **options):
    from cycles_houdini.blender import blend_convert

    messages, reloaded = [], []

    def default_hook(materials, report=None):
        reloaded.append(sorted(node.path() for node in materials))

    results = blend_convert.update_import(
        nodes,
        report=lambda text, error=False: messages.append((text, error)),
        evaluate=cases.fake_evaluate(manifest_for or (lambda request: cases.scene_manifest()), calls),
        executable=sys.executable,
        reload_materials=hook or default_hook,
        **options,
    )
    return results, messages, reloaded


def file_node(container):
    return container.node("blender_file")


def text(path):
    return path.read_text()


def test_unchanged_source_rewrites_nothing(assets):
    from cycles_houdini.blender import blend_file_node as fn

    nodes, _, _ = cases.run(assets, "packed")
    node = file_node(nodes[0])
    path = assets.assets / "geo" / "Props" / "Props.bgeo.sc"
    before, mtime = text(path), path.stat().st_mtime_ns
    built = len(assets.built)
    log = len(assets.hou.log)
    [result], messages, reloaded = update(assets, [node])
    assert result["nodes"] == [node] and result["created"] == []
    assert text(path) == before and path.stat().st_mtime_ns == mtime
    assert any("Reused identical geometry" in t for t, _ in messages)
    assert len(assets.built) == built  # materials reused, not rebuilt
    assert reloaded == [["/mat/Blue", "/mat/Red_Paint"]]  # linked materials: Reload from Library
    assert ("undo", "Update Blender assets") in assets.hou.log[log:]
    assert fn.node_status(node) == fn.UP_TO_DATE


def test_changed_geometry_replaces_the_file_and_reloads(assets):
    nodes, _, _ = cases.run(assets, "packed")
    node = file_node(nodes[0])
    path = assets.assets / "geo" / "Props" / "Props.bgeo.sc"
    before = text(path)
    log = len(assets.hou.log)
    _, messages, _ = update(assets, [node], manifest_for=lambda request: cases.scene_manifest(offset=1.0))
    assert text(path) != before and any("Replaced the previous import" in t for t, _ in messages)
    reader = node.node("converted_file")
    assert ("press", reader.path(), "reload", 1) in assets.hou.log[log:]
    assert reader.evalParm("file") == "$HIP/assets_blender/props/geo/Props/Props.bgeo.sc"


def test_fewer_frames(assets):
    from cycles_houdini.blender import blend_file_node as fn

    def manifest_for(request):
        return cases.scene_manifest(frame=request["frame"], offset=request["frame"])

    nodes, _, _ = cases.run(assets, "packed", frames=(1, 3, 1), manifest_for=manifest_for)
    node = file_node(nodes[0])
    node.parm("frame_range2").set(2)
    assert fn.node_status(node) == fn.OPTIONS_CHANGED
    calls = []
    _, messages, _ = update(assets, [node], manifest_for=manifest_for, calls=calls)
    assert [r["frame"] for r in calls[0]] == [1.0, 2.0]
    folder = assets.assets / "geo" / "Props"
    assert sorted(p.name for p in folder.glob("*.bgeo.sc")) == ["Props.0001.bgeo.sc", "Props.0002.bgeo.sc"]
    assert any("1 old file(s) deleted" in t for t, _ in messages)
    assert node.node("converted_file").evalParm("file").endswith("/Props.$F4.bgeo.sc")
    assert fn.node_status(node) == fn.UP_TO_DATE


def test_new_subdivision_part_creates_a_sibling_object(assets):
    from cycles_houdini.blender import blend_file_node as fn

    nodes, _, _ = cases.run(assets, "packed")
    node = file_node(nodes[0])
    node.parm("load_as").set("unpacked")
    [result], messages, _ = update(assets, [node], manifest_for=lambda request: cases.subdivided_scene())
    [new] = result["created"]
    assert new.parent().name() == "Props_subdiv" and new.parent().parent() is assets.obj
    assert new.type_name == fn.FILE_TYPE and new.evalParm("part") == "subdiv" and node.evalParm("part") == "base"
    assert new.evalParm("load_as") == "unpacked"  # the sibling loads like the node it came from
    assert new.node("converted_file").evalParm("file").endswith("/Props_subdiv.bgeo.sc")
    assert new.parent().evalParm("cycles_subdivision_render_level") == 2
    assert any("new subdivision part" in t for t, _ in messages)
    assert fn.record(new)["storage"] == "files"


def test_removed_part_keeps_the_node_and_reports_it(assets):
    nodes, _, _ = cases.run(assets, "packed", manifest_for=lambda request: cases.subdivided_scene())
    plain, smooth = (file_node(n) for n in nodes)
    [result], messages, _ = update(assets, [plain])  # the plain scene has one part
    assert smooth in result["nodes"] or smooth.parent() is not None
    assert plain.evalParm("part") == ""
    assert any(smooth.path() in t and "no longer exists" in t for t, _ in messages)
    assert smooth.parent().parent() is assets.obj  # kept


def test_stash_on_and_off(assets):
    from cycles_houdini.blender import blend_file_node as fn

    nodes, _, _ = cases.run(assets, "packed")
    node = file_node(nodes[0])
    path = assets.assets / "geo" / "Props" / "Props.bgeo.sc"
    node.parm("stash").set(1)
    assert fn.node_status(node) == fn.OPTIONS_CHANGED and fn.source_index(node) == 1  # nothing written yet
    update(assets, [node])
    stashed = node.evalParm(fn.DATA_PARM)
    assert isinstance(stashed, cases.FakeGeometry) and len(stashed.prims) == 4
    assert fn.record(node)["storage"] == "stash" and fn.source_index(node) == 2
    assert path.is_file()  # the file stays on disk
    assert fn.node_status(node) == fn.UP_TO_DATE
    node.parm("stash").set(0)
    update(assets, [node])
    assert fn.record(node)["storage"] == "files" and fn.source_index(node) == 1
    assert node.evalParm(fn.DATA_PARM).prims == []  # the stash is cleared


def test_update_after_link_refreshes_the_output_without_a_parameter_toggle(assets, monkeypatch):
    """Defect found live: Link import, Mode Converted, Update wrote the files and the record, but
    the node kept showing the Link result until another parameter changed. Every Update ends by
    setting Mode away and back (the inner switches re-evaluate) and reloading the File SOP after
    the files are committed; a stash Update reloads no file."""
    from cycles_houdini.blender import blend_convert
    from cycles_houdini.blender import blend_file_node as fn

    blend_convert.build_import(blend_convert.prepare_import(cases.snapshot(assets, storage="reference")))
    [node] = nodes_cases.blender_files(assets)
    reader = node.node(fn.FILE_NODE)
    path = assets.assets / "geo" / "Props" / "Props.bgeo.sc"
    pressed = []
    original = cases.Parm.pressButton

    def press(self):
        if self.node is reader and self.name == "reload":
            pressed.append(path.is_file())
        return original(self)

    monkeypatch.setattr(cases.Parm, "pressButton", press)
    node.parm("mode").set("converted")
    assert fn.source_index(node) == 0 and fn.node_status(node) == fn.NOT_CONVERTED
    start = len(assets.hou.log)
    update(assets, [node])
    assert fn.record(node)["storage"] == "files" and fn.source_index(node) == 1
    assert node.evalParm("mode") == "converted" and fn.node_status(node) == fn.UP_TO_DATE
    mine = [e[:3] for e in assets.hou.log[start:] if e[1] in (node.path(), reader.path())]
    assert mine[-3:] == [("set", node.path(), "mode"), ("set", node.path(), "mode"), ("press", reader.path(), "reload")]
    assert pressed and pressed[-1] is True  # the last reload sees the committed file

    node.parm("stash").set(1)
    del pressed[:]
    start = len(assets.hou.log)
    update(assets, [node])
    assert fn.record(node)["storage"] == "stash" and fn.source_index(node) == 2
    mine = [e[:3] for e in assets.hou.log[start:] if e[1] in (node.path(), reader.path())]
    assert mine[-2:] == [("set", node.path(), "mode"), ("set", node.path(), "mode")] and pressed == []

    node.parm("mode").set("link")  # Link: nothing to refresh, Mode is left alone
    start = len(assets.hou.log)
    assert fn.refresh_output(node) is None and assets.hou.log[start:] == []


def test_nodes_reading_the_same_files_are_one_import(assets):
    from cycles_houdini.blender import blend_file_node as fn

    nodes, _, _ = cases.run(assets, "packed")
    first = file_node(nodes[0])
    second = nodes[0].createNode(fn.FILE_TYPE, "blender_file")  # several nodes in one object
    fn.configure_file_node(second, source=str(assets.blend), kind="collection", name="Props", mode="converted")
    fn.set_converted(second, path_text=first.node("converted_file").evalParm("file"))
    other, _, _ = cases.run(assets, "packed", storage="embedded")  # its own stash: not part of it
    [result], _, _ = update(assets, [first], manifest_for=lambda request: cases.scene_manifest(offset=1.0))
    assert [n.path() for n in result["nodes"]] == [first.path(), second.path()]
    assert fn.record(second)["storage"] == "files"
    assert fn.record(file_node(other[0]))["storage"] == "stash"


def test_materials_editable_left_alone_new_imported(assets):
    def with_green(request):
        manifest, arrays = cases.scene_manifest()
        manifest["materials"] = cases.MATERIALS + [
            dict(key="kGreen", name="Green", local_name="Green", library="", nodes=[], outputs={}, default=False)
        ]
        manifest["instances"][0]["material_keys"] = ["kGreen", "kDefault", "kBlue"]
        return manifest, arrays

    nodes, _, _ = cases.run(assets, "packed", materials="append")
    node = file_node(nodes[0])
    _, messages, reloaded = update(assets, [node], manifest_for=with_green)
    assert reloaded == []  # nothing linked
    assert any("/mat/Blue not updated: editable" in t for t, _ in messages)
    assert [name for name, *_ in assets.built].count("Green") == 1  # new: imported, as append
    assert assets.hou.node("/mat/Green").userData("cycles_asset_link") is None


def test_linked_material_reload_degrades_to_a_report(assets, monkeypatch):
    from cycles_houdini.assets import importers
    from cycles_houdini.blender import blend_convert

    def broken(nodes, report=None):
        raise RuntimeError("no Blender")

    monkeypatch.setattr(importers, "reload_linked_async", broken)
    nodes, _, _ = cases.run(assets, "packed")
    messages = []
    blend_convert.update_import(
        [file_node(nodes[0])],
        report=lambda t, e=False: messages.append((t, e)),
        evaluate=cases.fake_evaluate(lambda request: cases.scene_manifest()),
        executable=sys.executable,
    )
    assert any("were not reloaded (no Blender)" in t and e for t, e in messages)


def test_shared_scene_cancel(assets):
    from cycles_houdini.blender import blend_convert

    nodes, _, _ = cases.run(assets, "packed")
    path = assets.assets / "geo" / "Props" / "Props.bgeo.sc"
    before = text(path)
    other = assets.hip / "shot_b.hip"
    other.write_bytes(b"hip")
    assets.hou.scene.path = str(other)
    with pytest.raises(blend_convert.BlendImportError, match=r"also used by: scene\.hip"):
        update(
            assets, [file_node(nodes[0])], manifest_for=lambda r: cases.scene_manifest(offset=1.0), on_shared="cancel"
        )
    assert text(path) == before


def test_link_nodes_are_skipped_and_missing_sources_refused(assets):
    from cycles_houdini.blender import blend_convert

    nodes, _, _ = cases.run(assets, "packed")
    node = file_node(nodes[0])
    node.parm("mode").set("link")
    results, messages, _ = update(assets, [node])
    assert results == [] and any("Link mode" in t for t, _ in messages)
    node.parm("mode").set("converted")
    node.parm("file").set(str(assets.tmp / "gone.blend"))
    with pytest.raises(blend_convert.BlendImportError, match="Source missing"):
        update(assets, [node])


def test_prepare_update_runs_without_hom(assets, monkeypatch):
    from cycles_houdini.blender import blend_convert

    nodes, _, _ = cases.run(assets, "packed")
    snapshot = blend_convert.update_snapshot([file_node(nodes[0])], executable=sys.executable)

    class NoHom(ModuleType):
        def __getattr__(self, name):
            raise AssertionError("HOM used on the background thread: hou." + name)

    host, result = sys.modules["hou"], {}

    def work():
        try:
            result["payload"] = blend_convert.prepare_update(
                snapshot, evaluate=cases.fake_evaluate(lambda r: cases.scene_manifest(offset=2.0))
            )
        except BaseException as error:
            result["error"] = error

    monkeypatch.setitem(sys.modules, "hou", NoHom("hou"))
    thread = threading.Thread(target=work)
    thread.start()
    thread.join(30)
    monkeypatch.setitem(sys.modules, "hou", host)
    assert "error" not in result, result.get("error")
    [entry] = result["payload"]["imports"]
    staging = entry["payload"]["staging"]
    blend_convert.sync_import(result["payload"], [file_node(nodes[0])], reload_materials=lambda n, report=None: None)
    assert not __import__("os").path.exists(staging)


# ---------------------------------------------------------------- hierarchy


def edited_scene(request):
    """Child removed, Rock added, Parent moved (offset); Tree unchanged."""
    manifest, arrays = cases.scene_manifest(offset=1.0)
    rock_world = cases.C @ cases.translate(-3, 0, 0)
    manifest["objects"] = [o for o in manifest["objects"] if o["name"] != "Child"] + [
        dict(name="Rock", type="MESH", parent=None, collection_path=["Props"], transform=list(rock_world.reshape(-1)))
    ]
    manifest["instances"] = [i for i in manifest["instances"] if i["owner"] != "Child"] + [
        cases.instance("i9", rock_world, "Rock", "Rock", 1)
    ]
    return manifest, arrays


def test_hierarchy_update_new_removed_moved_and_artist_nodes(assets):
    from cycles_houdini.blender import blend_file_node as fn

    nodes, _, _ = cases.run(assets, "hierarchy", materials="none")
    [hierarchy] = nodes
    children = {c.name(): c for c in hierarchy.children()}
    moved = assets.hou.Matrix4([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [9, 9, 9, 1]])
    children["Tree"].setParmTransform(moved)
    artist_object = hierarchy.createNode("geo", "artist_geo")
    artist_sop = children["Tree"].createNode("null", "artist_null")
    parent_before = children["Parent"].transform
    [result], messages, _ = update(assets, [hierarchy], manifest_for=edited_scene)
    after = {c.name(): c for c in hierarchy.children()}
    # New Blender object: created inside, parented under the collection null.
    rock = after["Rock"]
    assert result["created"] == [rock] and rock.userData(fn.OBJECT_USER_DATA) == "Rock"
    assert rock.inputs == [after["Props"]] and rock.node("file1").evalParm("file").endswith("/hierarchy/Rock.bgeo.sc")
    # Gone from the .blend: kept and reported.
    assert "Child" in after and any(after["Child"].path() in t and "no longer exists" in t for t, _ in messages)
    # Moved by hand: kept and reported; untouched objects follow Blender.
    assert children["Tree"].transform == moved
    assert any(children["Tree"].path() in t and "moved by hand" in t for t, _ in messages)
    assert children["Parent"].transform != parent_before and children["Parent"].transform[3][1] == 3.0
    # Artist nodes are never touched.
    assert after["artist_geo"] is artist_object and artist_object._parms == {}
    assert children["Tree"].node("artist_null") is artist_sop and artist_sop._parms == {}
    transforms = fn.record(hierarchy)["transforms"]
    assert set(transforms) == {"Parent", "Tree", "Rock"} and transforms["Parent"][13] == 3.0
    assert transforms["Tree"][12:15] != [9.0, 9.0, 9.0]  # the written value, not the artist's
    assert fn.node_status(hierarchy) == fn.UP_TO_DATE


def test_hierarchy_stash_switches_the_source_sop(assets):
    from cycles_houdini.blender import blend_file_node as fn

    nodes, _, _ = cases.run(assets, "hierarchy", materials="none")
    [hierarchy] = nodes
    tree = hierarchy.node("Tree")
    hierarchy.parm("stash").set(1)
    assert fn.node_status(hierarchy) == fn.OPTIONS_CHANGED
    update(assets, [hierarchy])
    stash = tree.node("stash1")
    assert stash is not None and tree.node("file1") is None
    assert tree.node("OUT").inputs == [stash] and isinstance(stash.evalParm("stash"), cases.FakeGeometry)
    assert stash.userData(fn.SOURCE_USER_DATA) == "1" and fn.record(hierarchy)["storage"] == "stash"


def test_hierarchy_material_sop_entries(assets):
    def green(request):
        manifest, arrays = cases.distinct_scene_manifest()
        manifest["materials"] = cases.MATERIALS + [
            dict(key="kGreen", name="Green", local_name="Green", library="", nodes=[], outputs={}, default=False)
        ]
        manifest["instances"][1]["material_keys"] = ["kGreen", "kDefault", "kBlue"]  # Tree's own mesh
        return manifest, arrays

    nodes, _, _ = cases.run(
        assets, "hierarchy", materials="append", manifest_for=lambda r: cases.distinct_scene_manifest()
    )
    [hierarchy] = nodes
    material = hierarchy.node("Tree").node("material1")

    def entries():
        return [
            (material.evalParm(f"group{i}"), material.evalParm(f"shop_materialpath{i}"))
            for i in range(1, material.evalParm("num_materials") + 1)
        ]

    assert entries() == [("mat_Blue", "/mat/Blue"), ("mat_Red_Paint", "/mat/Red_Paint")]
    material.parm("shop_materialpath1").set("/mat/custom")  # the artist's choice
    material.parm("num_materials").set(3)
    material.parm("group3").set("@name=Tree")
    material.parm("shop_materialpath3").set("/mat/other")
    _, messages, _ = update(assets, [hierarchy], manifest_for=green)
    assert entries() == [("mat_Blue", "/mat/custom"), ("@name=Tree", "/mat/other"), ("mat_Green", "/mat/Green")]
    assert any("/mat/Blue not updated: editable" in t for t, _ in messages)


def test_hierarchy_transform_unchanged_is_not_rewritten(assets):
    nodes, _, _ = cases.run(assets, "hierarchy", materials="none")
    [hierarchy] = nodes
    tree = hierarchy.node("Tree")
    log = len(assets.hou.log)
    update(assets, [hierarchy])
    assert np.allclose(np.asarray(tree.transform, dtype=float)[3, :3], [5, 0, 0])
    assert not [e for e in assets.hou.log[log:] if e[0] == "create" and e[1] in ("geo", "null")]


def test_a_linked_material_made_editable_is_kept_and_reported(assets):
    """Defect found live: after Make Editable on a linked material, Update created a second,
    linked copy (Blue_1) and rebound the geometry to it. The editable material is the
    material now: it is reused, left alone and reported; the others are reloaded."""
    from cycles_houdini.blender import blend_library_materials

    nodes, _, _ = cases.run(assets, "packed")
    node = file_node(nodes[0])
    blue = assets.hou.node("/mat/Blue")
    assert blend_library_materials.link_info(blue) is not None
    blue.setUserData("cycles_asset_link", "")  # what Make Editable leaves: no link data
    blue.user.pop("cycles_asset_link", None)
    assert blend_library_materials.link_info(blue) is None
    built = len(assets.built)
    _, messages, reloaded = update(assets, [node])
    assert any("/mat/Blue not updated: editable" in t for t, _ in messages), messages
    assert reloaded == [["/mat/Red_Paint"]]
    assert len(assets.built) == built and assets.hou.node("/mat/Blue2") is None
    assert not [c for c in assets.mat.children() if c.name().startswith("Blue") and c is not blue]
