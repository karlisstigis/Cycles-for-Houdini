# SPDX-License-Identifier: Apache-2.0
"""Live check of the Cycles Blender File and Blender Hierarchy nodes (hython + Blender).

    hython tests/houdini_blend_file_node_smoke.py [OUT_DIR] [--assets DIR] [--blender EXE]

Needs the staged development build (cycles::blender_reference, cycles_houdini) and Blender.
The two assets come from the staged host's otls, or from --assets (a folder written by
scripts/create-blender-assets.py). Builds a small .blend with Blender, creates each node
through blend_convert (the creation API) and checks: Link cooks; Converted Packed carries
name / path; Unpacked carries name / path / shop_materialpath; Unpack Iterations 1 vs 2 on
nested collection instances; Stash in .hip (no file, survives deleting assets_blender, a
copied node keeps its own data, save and reload); Update after editing the .blend (status
Source changed -> Up to date); Link -> Converted -> Update shows the converted
mesh without any other parameter change, and a second Update after editing the .blend
shows the edit (inner switch input, cook counts); Hierarchy Update (new object created, removed one kept,
hand-moved one kept). Prints and writes a JSON summary; exits 1 when a check failed.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

import hou

from cycles_houdini.blender import blend_convert
from cycles_houdini.blender import blend_file_node as fn

BLEND_SCRIPT = r"""
import sys
import bmesh
import bpy

variant, out = sys.argv[sys.argv.index("--") + 1 :][:2]
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
props = bpy.data.collections.new("Props")
scene.collection.children.link(props)
red = bpy.data.materials.new("Red")
red.use_nodes = True
red.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0.8, 0.05, 0.05, 1.0)


def cube(name, location, collection, size=1.0):
    mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=size)
    bm.to_mesh(mesh)
    bm.free()
    mesh.materials.append(red)
    obj = bpy.data.objects.new(name, mesh)
    obj.location = location
    collection.objects.link(obj)
    return obj


def instancer(name, collection, target, location):
    empty = bpy.data.objects.new(name, None)
    empty.instance_type = "COLLECTION"
    empty.instance_collection = target
    empty.location = location
    collection.objects.link(empty)
    return empty


cube("Box", (0.0, 0.0, 0.0) if variant == "a" else (2.0, 0.0, 0.0), props)
cube("Gone" if variant == "a" else "Rock", (0.0, 3.0, 0.0) if variant == "a" else (-3.0, 0.0, 0.0), props)
# Nested instances: Props > InnerInstance (collection Inner) > LeafInstance x2 (collection Leaf) > LeafCube.
leaf = bpy.data.collections.new("Leaf")
cube("LeafCube", (0.0, 0.0, 0.0), leaf, 0.3)
inner = bpy.data.collections.new("Inner")
instancer("LeafInstance", inner, leaf, (0.0, 0.0, 0.0))
instancer("LeafInstance2", inner, leaf, (1.0, 0.0, 0.0))
instancer("InnerInstance", props, inner, (0.0, 0.0, 2.0))
# Nested geometry nodes instances (exported as an instance tree): 2 x 2 branches of 3 cubes each.
forest_mesh = bpy.data.meshes.new("ForestBase")
forest_mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
forest = bpy.data.objects.new("Forest", forest_mesh)
forest.location = (6.0, 0.0, 0.0)
props.objects.link(forest)
nested = bpy.data.node_groups.new("Nested", "GeometryNodeTree")
nested.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
nested.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
N, L = nested.nodes.new, nested.links.new
gin, gout = N("NodeGroupInput"), N("NodeGroupOutput")
grid = N("GeometryNodeMeshGrid")
grid.inputs["Vertices X"].default_value = 2
grid.inputs["Vertices Y"].default_value = 2
grid_points = N("GeometryNodeMeshToPoints")
L(grid.outputs[0], grid_points.inputs[0])
line = N("GeometryNodeMeshLine")
line.inputs["Count"].default_value = 3
line_points = N("GeometryNodeMeshToPoints")
L(line.outputs[0], line_points.inputs[0])
leaf_cube = N("GeometryNodeMeshCube")
branch = N("GeometryNodeInstanceOnPoints")
L(line_points.outputs[0], branch.inputs["Points"])
L(leaf_cube.outputs[0], branch.inputs["Instance"])
trees = N("GeometryNodeInstanceOnPoints")
L(grid_points.outputs[0], trees.inputs["Points"])
L(branch.outputs[0], trees.inputs["Instance"])
join_all = N("GeometryNodeJoinGeometry")
L(trees.outputs[0], join_all.inputs[0])
L(gin.outputs[0], join_all.inputs[0])
L(join_all.outputs[0], gout.inputs[0])
forest.modifiers.new("Nested", "NODES").node_group = nested
bpy.ops.wm.save_as_mainfile(filepath=out)
"""

CHECKS = {}


def check(name):
    def wrap(function):
        started = time.time()
        try:
            value = function()
            CHECKS[name] = dict(ok=True, value=value)
        except Exception as error:  # noqa: BLE001 - every failure goes into the summary
            CHECKS[name] = dict(ok=False, error=repr(error), trace=traceback.format_exc()[-3000:])
        CHECKS[name]["seconds"] = round(time.time() - started, 2)
        return function

    return wrap


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def make_blend(blender, path, variant):
    script = Path(tempfile.mkdtemp(prefix="cycles_blend_script_")) / "make.py"
    script.write_text(BLEND_SCRIPT, encoding="utf-8")
    # Blender's own environment, as the bridge starts it: not Houdini's OCIO or Python.
    from cycles_houdini.blender.blend_worker import blender_environment

    subprocess.run(
        [blender, "-b", "--factory-startup", "--python", str(script), "--", variant, str(path)],
        check=True,
        timeout=300,
        stdout=subprocess.DEVNULL,
        env=blender_environment(hou.getenv("CYCLES_HOUDINI_BLENDER_OCIO") or None),
    )
    require(Path(path).is_file(), "Blender did not write " + str(path))


def geometry(node):
    node.cook(force=True)
    errors = node.errors()
    require(not errors, f"{node.path()} errors: {errors}")
    return node.geometry()


def counts(geo):
    packed = sum(
        1 for prim in geo.prims() if prim.type() == hou.primType.PackedGeometry or "Packed" in prim.type().name()
    )
    attribs = sorted(attrib.name() for attrib in geo.primAttribs())
    return dict(prims=len(geo.prims()), packed=packed, points=len(geo.points()), prim_attribs=attribs)


def say(text, error=False):
    print(("ERROR " if error else "") + text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    # Positional: scripts/run-hython.ps1 (PowerShell) takes "--out" for one of its own parameters.
    parser.add_argument(
        "out", type=Path, nargs="?", default=Path(tempfile.mkdtemp(prefix="cycles_blender_file_smoke_"))
    )
    parser.add_argument(
        "--assets", type=Path, help="Folder with cycles_blender_file.hda and cycles_blender_hierarchy.hda"
    )
    parser.add_argument("--blender", help="Blender executable (default: the Cycles bridge discovery)")
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    if args.assets:
        for name in ("cycles_blender_file.hda", "cycles_blender_hierarchy.hda"):
            hou.hda.installFile(str(args.assets / name))
    file_type, hierarchy_type = fn.node_types()
    summary = dict(
        houdini=hou.applicationVersionString(), out=str(out), installed=[bool(file_type), bool(hierarchy_type)]
    )
    if file_type is None or hierarchy_type is None:
        summary["result"] = "failed: the Cycles Blender assets are not installed (stage the build or pass --assets)"
        print(json.dumps(summary, indent=1))
        return 1
    blender = args.blender or fn.blender_executable()
    hip = out / "scene"
    hip.mkdir(exist_ok=True)
    hou.hipFile.clear(suppress_save_prompt=True)
    hou.hipFile.save(str(hip / "scene.hip"))
    library = out / "library"
    library.mkdir(exist_ok=True)
    source = library / "props.blend"
    make_blend(blender, source, "a")
    selection = [("collection", "Props")]
    common = dict(executable=blender, report=say)
    state = {}

    @check("hide_native_reference")
    def _():
        hidden = fn.hide_native_reference()
        return dict(hidden=hidden, native_hidden=hou.sopNodeTypeCategory().nodeType(fn.NATIVE_REFERENCE_TYPE).hidden())

    @check("link")
    def _():
        [container] = blend_convert.import_blend(str(source), selection, storage="reference", **common)
        node = container.node("blender_file")
        require(node.type().name() == fn.FILE_TYPE, "not a Blender File node")
        require(node.evalParm("mode") == "link", "not Link mode")
        reference = node.node(fn.REFERENCE_NODE)
        # The catalogue and bounds are read asynchronously; in hython drain them before cooking.
        from cycles_houdini.blender import blend_catalogue

        for _ in range(200):
            if blend_catalogue.drain_catalogues():
                break
            time.sleep(0.05)
        geo = geometry(node)
        state["link"] = node
        return dict(
            counts=counts(geo),
            ref_target=reference.evalParm("ref_target1"),
            status=node.parm("status").evalAsString(),
            status_text=fn.status_text(node),
            target_menu=fn.target_menu(dict(node=node))[:6],
        )

    @check("converted_packed")
    def _():
        [container] = blend_convert.import_blend(str(source), selection, storage="files", structure="packed", **common)
        node = container.node("blender_file")
        state["converted"] = node
        geo = geometry(node)
        result = counts(geo)
        require(result["prims"] == result["packed"] and result["packed"] > 0, "packed output expected")
        require({"name", "path"} <= set(result["prim_attribs"]), "name/path missing on packed prims")
        reader = node.node(fn.FILE_NODE).parm("file").unexpandedString()
        require(reader.startswith("$HIP/assets_blender/props/geo/Props/"), reader)
        return dict(
            counts=result, file=reader, status=fn.node_status(node), files_label=node.parm("files").evalAsString()
        )

    @check("converted_unpacked")
    def _():
        node = state["converted"]
        node.parm("load_as").set("unpacked")
        result = {}
        for iterations in (1, 2):
            node.parm("unpack_iterations").set(iterations)
            geo = geometry(node)
            result[iterations] = counts(geo)
            require(
                {"name", "path", "shop_materialpath"} <= set(result[iterations]["prim_attribs"]),
                f"iterations {iterations}: attributes {result[iterations]['prim_attribs']}",
            )
            values = {prim.attribValue("shop_materialpath") for prim in geo.prims()}
            result[iterations]["materials"] = sorted(values)
        # Forest's branches are nested packed primitives: Iterations 1 leaves the inner level packed.
        require(result[1]["packed"] > 0, "Iterations 1 should leave nested instances packed")
        require(result[2]["packed"] < result[1]["packed"], "Iterations 2 must unpack the nested level")
        node.parm("load_as").set("packed")
        return result

    @check("status_label")
    def _():
        node = state["converted"]
        label = node.parm("status").evalAsString()
        return dict(label=label, expanded=label in fn.STATUSES, status=fn.node_status(node))

    @check("update_after_edit")
    def _():
        node = state["converted"]
        before = counts(geometry(node))
        time.sleep(1.1)  # a new modification time
        make_blend(blender, source, "b")
        status_before = fn.node_status(node)
        require(status_before == fn.SOURCE_CHANGED, "status after editing the .blend: " + status_before)
        bbox_before = node.geometry().boundingBox().sizevec()
        blend_convert.update_import([node], **common)
        after = counts(geometry(node))
        status_after = fn.node_status(node)
        require(status_after == fn.UP_TO_DATE, "status after Update: " + status_after)
        return dict(
            before=before,
            after=after,
            bbox_before=list(bbox_before),
            bbox_after=list(node.geometry().boundingBox().sizevec()),
        )

    @check("stash")
    def _():
        node = state["converted"]
        path = node.path()
        node.parm("stash").set(1)
        require(fn.node_status(node) == fn.OPTIONS_CHANGED, "stash toggled: " + fn.node_status(node))
        blend_convert.update_import([node], **common)
        stashed = counts(geometry(node))
        require(fn.record(node)["storage"] == "stash" and stashed["prims"] > 0, "stash not used")
        copy = hou.copyNodesTo([node], node.parent())[0]
        copy_counts = counts(geometry(copy))
        copy.parm(fn.DATA_PARM).set(hou.Geometry())
        require(counts(geometry(copy))["prims"] == 0, "copy's own data was not changed")
        require(counts(geometry(node))["prims"] == stashed["prims"], "changing the copy changed the original")
        assets = Path(hou.text.expandString("$HIP/assets_blender"))
        moved = assets.with_name("assets_blender_moved")
        shutil.move(str(assets), str(moved))
        try:
            without_files = counts(geometry(node))
            link_path = state["link"].path()
            hou.hipFile.save()
            hou.hipFile.load(hou.hipFile.path(), suppress_save_prompt=True, ignore_load_warnings=True)
            state["link"] = hou.node(link_path)
            reloaded = counts(geometry(hou.node(path)))
            require(reloaded["prims"] == stashed["prims"], "stash lost on reload")
        finally:
            shutil.move(str(moved), str(assets))
        state["converted"] = hou.node(path)
        return dict(
            stashed=stashed,
            copy=copy_counts,
            without_files=without_files,
            reloaded=reloaded,
            locked=state["converted"].isLockedHDA(),
        )

    @check("link_to_converted_without_data")
    def _():
        node = state.get("link")
        require(node is not None, "no link node")
        node.parm("mode").set("converted")
        status = fn.node_status(node)
        geo = counts(geometry(node))
        node.parm("mode").set("link")
        require(status == fn.NOT_CONVERTED and fn.source_index(node) == 0, status)
        return dict(status=status, counts=geo)

    @check("link_converted_update_refreshes")
    def _():
        # Defect found live: after Link -> Converted, Update wrote the files but the node kept
        # showing the Link result until another parameter changed. Plain geometry() calls here
        # (no forced cook), plus the inner switch's input and cook counts, so a node that is not
        # dirtied, or a switch expression that is not re-evaluated, fails.
        node = state.get("link")
        require(node is not None, "no link node")
        switch, reader = node.node(fn.SOURCE_SWITCH), node.node(fn.FILE_NODE)
        link = counts(geometry(node))
        node.parm("mode").set("converted")
        shown = counts(node.geometry())
        require(switch.parm("input").eval() == 0, "Converted without data must show the Link result")
        # Diagnostic only (the cause): a record written to user data alone, no parameter change.
        fn.write_record(node, storage="files")
        user_data_only = switch.parm("input").eval()
        fn.write_record(node, storage=None)
        dirtied = {}
        for name, change in (
            ("reader_reload", lambda: reader.parm("reload").pressButton()),
            ("reader_file", lambda: reader.parm("file").set("$HIP/missing_probe.bgeo.sc")),
            ("same_empty_data", lambda: node.parm(fn.DATA_PARM).set(hou.Geometry())),
            ("user_data", lambda: fn.write_record(node, storage=None)),
        ):
            node.geometry()
            change()
            dirtied[name] = dict(switch=switch.needsToCook(), node=node.needsToCook())
        reader.parm("file").set("")
        node.geometry()
        cooks = (switch.cookCount(), reader.cookCount())
        blend_convert.update_import([node], **common)
        index = switch.parm("input").eval()
        require(switch.needsToCook() and node.needsToCook(), "Update did not dirty the node's inner switch")
        geo = node.geometry()
        first = counts(geo)
        paths = sorted({prim.attribValue("path") for prim in geo.prims()})
        require(index == 1, f"inner source switch still at {index} after Update")
        require(switch.cookCount() > cooks[0], "the inner source switch did not cook after Update")
        require(reader.cookCount() > cooks[1], "the converted File SOP did not cook after Update")
        require(first["prims"] > 0 and first["prims"] == first["packed"], f"converted packed mesh expected: {first}")
        require({"name", "path"} <= set(first["prim_attribs"]), first["prim_attribs"])
        require(node.parm("mode").evalAsString() == "converted", "Mode changed")
        # Second Update after editing the .blend (the source is variant "b" here).
        time.sleep(1.1)
        make_blend(blender, source, "a")
        cooks = (switch.cookCount(), reader.cookCount())
        blend_convert.update_import([node], **common)
        geo = node.geometry()
        second = counts(geo)
        edited = sorted({prim.attribValue("path") for prim in geo.prims()})
        require(reader.cookCount() > cooks[1], "the converted File SOP did not cook after the second Update")
        require(edited != paths, f"output unchanged after editing the .blend: {edited}")
        require(fn.node_status(node) == fn.UP_TO_DATE, fn.node_status(node))
        return dict(
            link=link,
            converted_without_data=shown,
            switch_after_user_data_only=user_data_only,
            dirtied_without_cook=dirtied,
            first=first,
            second=second,
            paths=[paths, edited],
        )

    @check("hierarchy")
    def _():
        make_blend(blender, source, "a")
        [hierarchy] = blend_convert.import_blend(
            str(source), selection, storage="files", structure="hierarchy", **common
        )
        require(hierarchy.type().name() == fn.HIERARCHY_TYPE and not hierarchy.isLockedHDA(), "hierarchy object")
        children = {
            child.userData(fn.OBJECT_USER_DATA): child
            for child in hierarchy.children()
            if child.userData(fn.OBJECT_USER_DATA)
        }
        require({"Box", "Gone"} <= set(children), sorted(children))
        moved = children["Box"]
        moved.parm("tx").set(moved.evalParm("tx") + 5.0)
        artist = hierarchy.createNode("geo", "artist_geo")
        hierarchy.parmTuple("t").set((1.0, 0.0, 0.0))
        time.sleep(1.1)
        make_blend(blender, source, "b")
        require(fn.node_status(hierarchy) == fn.SOURCE_CHANGED, fn.node_status(hierarchy))
        lines = []
        blend_convert.update_import([hierarchy], executable=blender, report=lambda t, e=False: lines.append(t))
        after = {
            child.userData(fn.OBJECT_USER_DATA): child
            for child in hierarchy.children()
            if child.userData(fn.OBJECT_USER_DATA)
        }
        require("Rock" in after and "Gone" in after, sorted(after))
        require(hou.node(artist.path()) is not None, "artist node removed")
        require(any("moved by hand" in line for line in lines), lines)
        box_world = list(after["Box"].worldTransform().extractTranslates())
        return dict(objects=sorted(after), report=lines, box_world=box_world, status=fn.node_status(hierarchy))

    failed = [name for name, value in CHECKS.items() if not value["ok"]]
    summary.update(checks=CHECKS, failed=failed, result="passed" if not failed else "failed")
    text = json.dumps(summary, indent=1, default=repr)
    (out / "blend_file_node_smoke.json").write_text(text, encoding="utf-8")
    print(text)
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException:
        traceback.print_exc()
        code = 1
    sys.stdout.flush()
    os._exit(code)
