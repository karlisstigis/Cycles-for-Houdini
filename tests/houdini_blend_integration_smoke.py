# SPDX-License-Identifier: Apache-2.0
"""Live check of the Blender assets integration (blend-import.md: Collect and relink, Asset nodes, Importer dialog) in
Houdini 22 (hython + Blender).

    hython tests/houdini_blend_integration_smoke.py [--only GROUP] [--work DIR] [--report FILE]
        [--device CPU] [--blender EXE]

Needs the staged development build (the Cycles Blender File and Blender Hierarchy assets) and
Blender. Builds small .blend files with Blender in a work folder (scenes, assets_blender and the
asset cache are all inside it) and checks, one group per --only:

  browser    asset-browser code path: Link -> Blender File node in Link mode with the chosen
             viewport shape; Convert & Append -> Converted (Save to Disk) and Stash in .hip (no
             geometry file); Link dropped into a SOP network creates the node there
  dialog     importer code path (converter_arguments -> prepare -> build): Without hierarchy,
             Hierarchy, Link + Hierarchy sent as packed; a frame range, then Update with fewer
             frames (stale frames deleted, a foreign file kept)
  shared     re-import from a second saved .hip in the same folder: shared_scenes names the
             first; on_shared="cancel" raises, "replace" proceeds
  materials  Update with a linked, an editable and a new material
  subdiv     a kept Subsurf: <item> + <item>_subdiv with the right Part, Update keeps both, the
             merged-geometry warning
  paths      Collect report with a Link node; Relink of a moved .blend (both node types) and of a
             missing converted file; Move into Project of a converted node made in an untitled scene
  render     hierarchy object moved and rotated vs the same hierarchy built in /obj with the same
             transform at the top; Converted Packed vs Unpacked; Link-mode node not black
  native     hide_native_reference; an old scene with a plain cycles::blender_reference loads and cooks

Prints a JSON summary and exits non-zero when a check failed.
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

GROUPS = ("browser", "dialog", "shared", "materials", "subdiv", "paths", "render", "native")

BLEND_SCRIPT = r"""
import sys
import bmesh
import bpy

variant, out = sys.argv[sys.argv.index("--") + 1 :][:2]
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.frame_start, scene.frame_end = 1, 5
props = bpy.data.collections.new("Props")
scene.collection.children.link(props)


def material(name, rgb):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    mat.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (*rgb, 1.0)
    return mat


def cube(name, location, mat, size=1.0, parent=None, smooth=False):
    mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=size)
    bm.to_mesh(mesh)
    bm.free()
    if smooth:
        try:
            mesh.shade_smooth()
        except AttributeError:
            mesh.polygons.foreach_set("use_smooth", [True] * len(mesh.polygons))
    mesh.materials.append(mat)
    obj = bpy.data.objects.new(name, mesh)
    obj.location = location
    props.objects.link(obj)
    if parent is not None:
        obj.parent = parent
    return obj


red, blue = material("Red", (0.8, 0.05, 0.05)), material("Blue", (0.05, 0.1, 0.8))
cube("Box", (0.0, 0.0, 0.0), red)
cube("Rock", (2.5, 0.0, 0.0), blue)
if variant == "m2":
    cube("Pebble", (-2.5, 0.0, 0.0), material("Green", (0.05, 0.8, 0.1)))
if variant == "sub":
    smooth = cube("Smooth", (0.0, 0.0, 2.5), red, smooth=True)
    modifier = smooth.modifiers.new("Subdivision", "SUBSURF")
    modifier.levels, modifier.render_levels = 1, 2
mover = cube("Mover", (0.0, -2.0, 0.0), red, 0.5)
mover.keyframe_insert("location", frame=1)
mover.location.z = 2.0
mover.keyframe_insert("location", frame=5)
parent = bpy.data.objects.new("Parent", None)
parent.location = (-1.5, 2.0, 0.5)
parent.rotation_euler = (0.0, 0.0, 0.6)
props.objects.link(parent)
cube("Child", (1.0, 0.0, 0.0), blue, 0.6, parent=parent)
bpy.ops.wm.save_as_mainfile(filepath=out)
"""


class Checks:
    def __init__(self):
        self.items, self.group = [], ""

    def __call__(self, name, ok, message=""):
        self.items.append(dict(group=self.group, check=name, ok=bool(ok), message=str(message)[:1500]))
        print(("PASS " if ok else "FAIL ") + f"[{self.group}] {name}: {str(message)[:300]}")
        return ok

    def error(self, name, text):
        self(name, False, text)


class Context:
    def __init__(self, args):
        self.args = args
        self.work = (args.work or Path(tempfile.mkdtemp(prefix="cycles_blend_integration_"))).resolve()
        self.work.mkdir(parents=True, exist_ok=True)
        # The user's asset cache is never touched: untitled-scene imports go here.
        os.environ["CYCLES_HOUDINI_ASSET_CACHE"] = str(self.work / "cache")
        hou.putenv("CYCLES_HOUDINI_ASSET_CACHE", str(self.work / "cache"))
        from cycles_houdini.blender import blend_file_node as fn

        self.blender = args.blender or fn.blender_executable()
        self.library = self.work / "library"
        self.library.mkdir(exist_ok=True)

    def blend(self, variant, name="props"):
        from cycles_houdini.blender.blend_worker import blender_environment

        path = self.library / variant / f"{name}.blend"
        path.parent.mkdir(parents=True, exist_ok=True)
        script = self.work / "make_blend.py"
        script.write_text(BLEND_SCRIPT, encoding="utf-8")
        subprocess.run(
            [self.blender, "-b", "--factory-startup", "--python", str(script), "--", variant, str(path)],
            check=True,
            timeout=300,
            stdout=subprocess.DEVNULL,
            env=blender_environment(hou.getenv("CYCLES_HOUDINI_BLENDER_OCIO") or None),
        )
        if not path.is_file():
            raise RuntimeError("Blender did not write " + str(path))
        return path

    def rewrite(self, path, variant):
        """Write another variant over an existing .blend (a new modification time)."""
        time.sleep(1.1)
        fresh = self.blend(variant, name="tmp_" + path.stem)
        shutil.copyfile(fresh, path)
        return path

    def scene(self, name):
        hou.hipFile.clear(suppress_save_prompt=True)
        folder = self.work / "scenes" / name
        folder.mkdir(parents=True, exist_ok=True)
        hou.hipFile.save(str(folder / "scene.hip"))
        return folder


def posix(path):
    return str(path).replace("\\", "/")


def say_into(lines):
    def report(text, error=False):
        lines.append((str(text), bool(error)))

    return report


def drain():
    from cycles_houdini.blender import blend_catalogue

    for _ in range(300):
        if blend_catalogue.drain_catalogues():
            return True
        time.sleep(0.05)
    return False


def cooked(node):
    node.cook(force=True)
    errors = node.errors()
    if errors:
        raise AssertionError(f"{node.path()} errors: {errors}")
    return node.geometry()


def prim_count(node):
    return len(cooked(node).prims())


def record_of(name, id_type, source):
    from cycles_houdini.assets.records import make_record

    return make_record(name, id_type, Path(source).name, source=posix(source), object_type="MESH")


def dialog_import(ctx, source, selection, options, lines=None, on_shared=None):
    """The importer dialog's code path: converter_arguments -> backend.prepare -> backend.build."""
    import threading

    from cycles_houdini.blender import blend_import_dialog as dialog

    src, chosen, kwargs = dialog.converter_arguments(posix(source), selection, options, ctx.blender)
    if on_shared:
        kwargs["on_shared"] = on_shared
    backend = dialog.HoudiniBackend()
    payload = backend.prepare(src, chosen, kwargs, threading.Event(), say_into(lines if lines is not None else []))
    return kwargs, backend.build(payload)


CONVERT = dict(mode="convert", save_location="files", structure="packed", materials="link", frames="current")


def files_in(folder):
    return sorted(p.name for p in Path(folder).iterdir()) if Path(folder).is_dir() else []


# ---------------------------------------------------------------------- groups


def group_browser(ctx, check):
    from cycles_houdini.assets import importers
    from cycles_houdini.blender import blend_file_node as fn

    folder = ctx.scene("browser")
    source = ctx.blend("m1")
    props = record_of("Props", "COLLECTION", source)
    rock = record_of("Rock", "OBJECT", source)
    lines = []
    nodes = importers.import_asset(props, mode="link", viewport_shape="bounds", report=say_into(lines))
    geo, node = nodes[0], nodes[1]
    check("link: geo object + Blender File node", geo.type().name() == "geo" and fn.is_file_node(node), nodes)
    check("link: mode link", node.evalParm("mode") == "link", node.evalParm("mode"))
    check("link: viewport shape bounds", node.parm("viewport_shape").evalAsString() == "bounds")
    check(
        "link: no plain reference SOP beside it",
        not [c for c in geo.children() if c.type().name().startswith("cycles::blender_reference")],
    )
    drain()
    check("link: cooks with geometry", prim_count(node) > 0)
    check("link: marked with the asset key", node.userData(importers.ASSET_KEY_USER_DATA) == props["ch"]["key"])
    check("link: status", fn.node_status(node) == fn.UP_TO_DATE, fn.node_status(node))
    nodes = importers.import_asset(props, mode="link", viewport_shape="hull")
    check("link: hull shape", nodes[1].parm("viewport_shape").evalAsString() == "hull")

    snapshot = importers.append_snapshot(props, storage="files")
    check("append files: no shared scenes on a first import", importers.append_shared_scenes(snapshot) == [])
    payload = importers.prepare_append(snapshot)
    lines = []
    nodes = importers.import_asset(props, mode="append", prepared=payload, report=say_into(lines))
    node = nodes[0].node("blender_file")
    check("append files: converted node", node is not None and node.evalParm("mode") == "converted", nodes)
    check("append files: storage files", fn.record(node).get("storage") == "files", fn.record(node))
    reader = node.node(fn.FILE_NODE).parm("file").unexpandedString()
    check("append files: $HIP/assets_blender path", reader.startswith("$HIP/assets_blender/props/geo/Props/"), reader)
    check("append files: file written", (folder / "assets_blender/props/geo/Props/Props.bgeo.sc").is_file())
    check("append files: cooks packed", prim_count(node) > 0)
    check("append files: Stash off", node.evalParm("stash") == 0)

    snapshot = importers.append_snapshot(rock, storage="embedded")
    payload = importers.prepare_append(snapshot)
    nodes = importers.import_asset(rock, mode="append", prepared=payload, report=say_into(lines))
    node = nodes[0].node("blender_file")
    check("append stash: Stash on", node.evalParm("stash") == 1)
    check("append stash: storage stash", fn.record(node).get("storage") == "stash", fn.record(node))
    check(
        "append stash: no geometry file",
        not (folder / "assets_blender/props/geo/Rock").exists(),
        files_in(folder / "assets_blender/props/geo"),
    )
    check("append stash: cooks", prim_count(node) > 0)

    target = hou.node("/obj").createNode("geo", "drop_target")
    created = importers.import_asset(rock, mode="link", network=target, position=(2, 2), viewport_shape="bounds")
    check(
        "drop into SOP network: node created there",
        len(created) == 1 and created[0].parent() == target and fn.is_file_node(created[0]),
        [n.path() for n in created],
    )
    drain()
    check("drop into SOP network: cooks", prim_count(created[0]) > 0)


def group_dialog(ctx, check):
    from cycles_houdini.blender import blend_convert
    from cycles_houdini.blender import blend_file_node as fn

    folder = ctx.scene("dialog")
    source = ctx.blend("m1")
    kwargs, nodes = dialog_import(ctx, source, [("collection", "Props")], CONVERT)
    node = nodes[0].node("blender_file")
    check("without hierarchy: Blender File node", fn.is_file_node(node) and kwargs["structure"] == "packed")
    check("without hierarchy: cooks", prim_count(node) > 0)
    kwargs, nodes = dialog_import(ctx, source, [("collection", "Props")], dict(CONVERT, structure="hierarchy"))
    check("hierarchy: one Blender Hierarchy object", len(nodes) == 1 and fn.is_hierarchy(nodes[0]), nodes)
    check("hierarchy: unlocked contents", not nodes[0].isLockedHDA())
    names = sorted(c.userData(fn.OBJECT_USER_DATA) or "" for c in nodes[0].children())
    check("hierarchy: objects inside", {"Box", "Rock", "Child", "Parent"} <= set(names), names)
    kwargs, nodes = dialog_import(ctx, source, [("object", "Box")], dict(CONVERT, mode="link", structure="hierarchy"))
    check("link + hierarchy is sent as packed", kwargs["structure"] == "packed" and kwargs["storage"] == "reference")
    check("link + hierarchy creates a Link node", fn.is_file_node(nodes[0].node("blender_file")))

    options = dict(CONVERT, frames="range", frame_start=1, frame_end=5, frame_step=1)
    kwargs, nodes = dialog_import(ctx, source, [("object", "Mover")], options)
    node = nodes[0].node("blender_file")
    asset = folder / "assets_blender/props/geo/Mover"
    before = files_in(asset)
    check("sequence: five frames written", before == [f"Mover.000{i}.bgeo.sc" for i in range(1, 6)], before)
    check("sequence: $F4 path", node.node(fn.FILE_NODE).parm("file").unexpandedString().endswith("Mover.$F4.bgeo.sc"))
    (asset / "notes.txt").write_text("not the importer's", encoding="utf-8")
    node.parmTuple("frame_range").set((1, 3, 1))
    check("sequence: options changed", fn.node_status(node) == fn.OPTIONS_CHANGED, fn.node_status(node))
    lines = []
    blend_convert.update_import([node], executable=ctx.blender, report=say_into(lines))
    after = files_in(asset)
    check(
        "fewer frames: stale frames deleted, foreign file kept",
        after == ["Mover.0001.bgeo.sc", "Mover.0002.bgeo.sc", "Mover.0003.bgeo.sc", "notes.txt"],
        after,
    )
    check("fewer frames: up to date", fn.node_status(node) == fn.UP_TO_DATE, fn.node_status(node))
    hou.setFrame(3)
    check("fewer frames: frame 3 cooks", prim_count(node) > 0)
    hou.setFrame(1)


def group_shared(ctx, check):
    from cycles_houdini.blender import blend_convert

    first = ctx.scene("shared")
    source = ctx.blend("m1")
    dialog_import(ctx, source, [("object", "Box")], CONVERT)
    hou.hipFile.save()
    hou.hipFile.clear(suppress_save_prompt=True)
    hou.hipFile.save(str(first / "second.hip"))
    shared = blend_convert.shared_scenes(posix(source), [("object", "Box")])
    scenes = [Path(s).name for entry in shared for s in entry["scenes"]]
    check("second scene: shared_scenes names the first", scenes == ["scene.hip"], shared)
    try:
        dialog_import(ctx, source, [("object", "Box")], CONVERT, on_shared="cancel")
        check("cancel raises", False, "no error")
    except blend_convert.BlendImportError as error:
        check("cancel raises", "scene.hip" in str(error), error)
    check("cancel: nothing created", hou.node("/obj").children() == (), hou.node("/obj").children())
    _, nodes = dialog_import(ctx, source, [("object", "Box")], CONVERT, on_shared="replace")
    check("replace proceeds", len(nodes) == 1 and nodes[0].node("blender_file") is not None, nodes)


def group_materials(ctx, check):
    from cycles_houdini.assets import importers
    from cycles_houdini.blender import blend_convert
    from cycles_houdini.blender import blend_file_node as fn
    from cycles_houdini.blender.blend_library_materials import link_info

    ctx.scene("materials")
    source = ctx.blend("m1", name="mats")
    _, nodes = dialog_import(ctx, source, [("collection", "Props")], CONVERT)
    node = nodes[0].node("blender_file")
    mats = {child.name(): child for child in hou.node("/mat").children()}
    check(
        "import: Red and Blue linked",
        {"Red", "Blue"} <= set(mats) and all(link_info(mats[n]) for n in ("Red", "Blue")),
        sorted(mats),
    )
    importers.append_linked(mats["Blue"])
    mats["Blue"].setComment("edited by the artist")
    check("Blue made editable", link_info(mats["Blue"]) is None)
    ctx.rewrite(source, "m2")
    check("status: source changed", fn.node_status(node) == fn.SOURCE_CHANGED, fn.node_status(node))
    lines = []
    blend_convert.update_import([node], executable=ctx.blender, report=say_into(lines))
    text = "\n".join(t for t, _ in lines)
    mats = {child.name(): child for child in hou.node("/mat").children()}
    check("update: Green imported, linked", "Green" in mats and link_info(mats["Green"]) is not None, sorted(mats))
    check("update: Red still linked", link_info(mats["Red"]) is not None)
    check(
        "update: Blue untouched", link_info(mats["Blue"]) is None and mats["Blue"].comment() == "edited by the artist"
    )
    check("update: Blue reported as editable", "Blue" in text and "editable" in text, text)
    check("update: up to date", fn.node_status(node) == fn.UP_TO_DATE, fn.node_status(node))
    geo = cooked(node)
    check("update: Pebble in the geometry", any("Pebble" in (p.attribValue("name") or "") for p in geo.prims()))


def group_subdiv(ctx, check):
    from cycles_houdini.blender import blend_convert
    from cycles_houdini.blender import blend_file_node as fn

    ctx.scene("subdiv")
    source = ctx.blend("sub", name="smooth")
    lines = []
    _, nodes = dialog_import(ctx, source, [("collection", "Props")], CONVERT, lines=lines)
    names = [n.name() for n in nodes]
    check("two objects: <item> and <item>_subdiv", names == ["Props", "Props_subdiv"], (names, lines))
    if len(nodes) != 2:
        return
    plain, smooth = (n.node("blender_file") for n in nodes)
    check("parts", (plain.evalParm("part"), smooth.evalParm("part")) == ("base", "subdiv"))
    check(
        "subdiv object subdivides",
        nodes[1].evalParm("cycles_subdivision_override") == 1
        and nodes[1].evalParm("cycles_subdivision_render_level") == 2,
    )
    check(
        "no warning on own objects",
        fn.subdivision_problem(smooth) == "" and fn.subdivision_problem(plain) == "",
        fn.subdivision_problem(smooth),
    )
    ctx.rewrite(source, "sub")
    lines = []
    blend_convert.update_import([smooth], executable=ctx.blender, report=say_into(lines))
    children = [c.name() for c in hou.node("/obj").children()]
    check("update keeps both", children == ["Props", "Props_subdiv"], children)
    check("update keeps parts", (plain.evalParm("part"), smooth.evalParm("part")) == ("base", "subdiv"))
    check(
        "update: both up to date",
        fn.node_status(plain) == fn.node_status(smooth) == fn.UP_TO_DATE,
        (fn.node_status(plain), fn.node_status(smooth)),
    )
    check("update: no warning line", not [t for t, e in lines if "Warning" in t], lines)
    box = nodes[1].createNode("box")
    merge = nodes[1].createNode("merge")
    merge.setInput(0, smooth)
    merge.setInput(1, box)
    merge.setDisplayFlag(True)
    merge.setRenderFlag(True)
    warning = fn.subdivision_problem(smooth)
    check(
        "merged: warning naming the fix",
        "merged with other geometry" in warning and "Apply Subdivision" in warning,
        warning,
    )
    check(
        "merged: in the Status label",
        "Warning" in smooth.parm("status").evalAsString(),
        smooth.parm("status").evalAsString(),
    )
    smooth.setDisplayFlag(True)
    smooth.setRenderFlag(True)
    nodes[1].parm("cycles_subdivision_render_level").set(1)
    check("mismatch: warning", "does not subdivide" in fn.subdivision_problem(smooth), fn.subdivision_problem(smooth))


def group_paths(ctx, check):
    from cycles_houdini.assets import collect
    from cycles_houdini.assets.move_project import move_imported_assets_into_project
    from cycles_houdini.assets.relink import relink_blender_assets
    from cycles_houdini.blender import blend_file_node as fn

    folder = ctx.scene("paths")
    library = ctx.work / "moving_library"
    library.mkdir(exist_ok=True)
    source = library / "props.blend"
    shutil.copyfile(ctx.blend("m1"), source)
    _, link = dialog_import(ctx, source, [("object", "Box")], dict(CONVERT, mode="link"))
    _, converted = dialog_import(ctx, source, [("object", "Rock")], CONVERT)
    _, hierarchy = dialog_import(ctx, source, [("object", "Box")], dict(CONVERT, structure="hierarchy"))
    link, converted, hierarchy = link[0].node("blender_file"), converted[0].node("blender_file"), hierarchy[0]
    hou.hipFile.save()
    report = collect.collect_textures(show=False)
    check("collect: link node counted once", report.linked_assets == 1, report.linked_assets)
    check(
        "collect: report says how to fix",
        "Switch them to Converted to make them portable." in report.text(),
        report.text(),
    )

    moved = ctx.work / "moved_library"
    shutil.move(str(library), str(moved))
    check("moved: status source missing", fn.node_status(converted) == fn.SOURCE_MISSING)
    plan = relink_blender_assets(folder=str(moved))
    new = posix(moved / "props.blend")
    check("relink: Blender File (link)", link.parm("file").eval() == new, link.parm("file").eval())
    check("relink: Blender File (converted)", converted.parm("file").eval() == new)
    check("relink: Blender Hierarchy", hierarchy.parm("file").eval() == new)
    check(
        "relink: inner reference follows",
        link.node(fn.REFERENCE_NODE).parm("file").eval() == new,
        link.node(fn.REFERENCE_NODE).parm("file").eval(),
    )
    check("relink: nothing left missing", not [e for e in plan.not_found if e.kind == "blend"], plan.text())
    check(
        "relink: status after a moved .blend",
        fn.node_status(converted) in (fn.UP_TO_DATE, fn.SOURCE_CHANGED),
        fn.node_status(converted),
    )

    reader = converted.node(fn.FILE_NODE)
    geo_file = folder / "assets_blender/props/geo/Rock/Rock.bgeo.sc"
    elsewhere = ctx.work / "found_geo"
    elsewhere.mkdir(exist_ok=True)
    shutil.move(str(geo_file), str(elsewhere / "Rock.bgeo.sc"))
    check(
        "missing converted file: status not converted",
        fn.node_status(converted) == fn.NOT_CONVERTED,
        fn.node_status(converted),
    )
    relink_blender_assets(folder=str(elsewhere))
    check(
        "relink converted file",
        reader.parm("file").eval() == posix(elsewhere / "Rock.bgeo.sc"),
        reader.parm("file").unexpandedString(),
    )
    check(
        "relink converted file: status consistent",
        fn.node_status(converted) in (fn.UP_TO_DATE, fn.SOURCE_CHANGED),
        fn.node_status(converted),
    )
    check("relink converted file: cooks", prim_count(converted) > 0)

    hou.hipFile.clear(suppress_save_prompt=True)
    _, nodes = dialog_import(ctx, new, [("object", "Rock")], CONVERT)
    node = nodes[0].node("blender_file")
    text = node.node(fn.FILE_NODE).parm("file").unexpandedString()
    cache = posix(ctx.work / "cache")
    check("untitled: converted file in the cache", text.startswith(cache), text)
    target = ctx.work / "scenes" / "moved_project"
    target.mkdir(parents=True, exist_ok=True)
    hou.hipFile.save(str(target / "scene.hip"))
    results, failures = move_imported_assets_into_project(show=False)
    text = node.node(fn.FILE_NODE).parm("file").unexpandedString()
    check("move: no failures", not failures, failures)
    check("move: inner File SOP rewritten", text == "$HIP/assets_blender/props/geo/Rock/Rock.bgeo.sc", text)
    check("move: file in the project", (target / "assets_blender/props/geo/Rock/Rock.bgeo.sc").is_file())
    check(
        "move: cooks and up to date",
        prim_count(node) > 0 and fn.node_status(node) == fn.UP_TO_DATE,
        fn.node_status(node),
    )


# ---------------------------------------------------------------------- render


def render(ctx, size=48, samples=32):
    import numpy as np

    from cycles_houdini import native_scene, registry
    from cycles_houdini.device_selection import set_devices

    obj = hou.node("/obj")
    cam = obj.node("j_cam") or obj.createNode("cam", "j_cam")
    cam.parmTuple("t").set((0.0, 2.0, 11.0))
    cam.parmTuple("r").set((-8.0, 0.0, 0.0))
    if obj.node("j_env") is None:
        from cycles_houdini.scene import light_properties

        env = obj.createNode("envlight", "j_env")
        light_properties.install(env)
        env.parm("light_contribprimary").set(1)
    rop = hou.node("/out").node("j_rop") or hou.node("/out").createNode("cycles_ipr", "j_rop")
    set_devices(rop, ctx.args.device)
    rop.setParms(
        dict(
            source_mode="objects",
            objectroot="/obj",
            vobject="*",
            camera=cam.path(),
            lights="*",
            world_source="scene",
            resx=size,
            resy=size,
            samples=samples,
            ipr_samples=samples,
            use_denoise=0,
            ipr_use_denoise=0,
            allowmotionblur=0,
            use_material_assignments=1,
        )
    )
    try:
        entry = registry.for_rop(rop, native_scene.configuration(rop))
        frame = entry.session.wait(entry.apply(native_scene.capture(rop), hou.time()))
        status = entry.session.status()
        if status["error"]:
            raise RuntimeError(status["error"])
        plane = frame["planes"]["combined"]
        return np.frombuffer(plane["pixels"], np.float32).reshape(size, size, plane["channels"])[..., :3].copy()
    finally:
        registry.close_all()


def save_image(ctx, name, pixels):
    import numpy as np

    path = ctx.work / "renders" / (name + ".npy")
    path.parent.mkdir(parents=True, exist_ok=True)
    np.save(path, pixels)
    # An 8-bit PPM beside it (sRGB-ish gamma) for a quick look; also copied next to the report.
    image = (np.clip(pixels, 0.0, 1.0) ** (1 / 2.2) * 255).astype(np.uint8)
    header = f"P6 {image.shape[1]} {image.shape[0]} 255\n".encode()
    for target in [path.with_suffix(".ppm")] + ([ctx.args.images / (name + ".ppm")] if ctx.args.images else []):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(header + image.tobytes())
    return path


def coloured(pixels):
    """Pixels clearly not grey: the red and blue objects (the environment is white)."""
    import numpy as np

    return int((np.ptp(pixels, axis=2) > 0.1).sum())


def group_render(ctx, check):
    import numpy as np

    from cycles_houdini.blender import blend_convert
    from cycles_houdini.blender import blend_file_node as fn

    transform_t, transform_r = (1.0, 0.5, -1.0), (0.0, 35.0, 10.0)
    source = ctx.blend("m1", name="render")
    ctx.scene("render_hierarchy")
    _, nodes = dialog_import(ctx, source, [("collection", "Props")], dict(CONVERT, structure="hierarchy"))
    hierarchy = nodes[0]
    hierarchy.parmTuple("t").set(transform_t)
    hierarchy.parmTuple("r").set(transform_r)
    first = render(ctx)
    save_image(ctx, "hierarchy_object", first)
    check("hierarchy render shows the objects", coloured(first) > 40, coloured(first))
    hierarchy.parmTuple("t").set((0.0, 0.0, 0.0))
    hierarchy.parmTuple("r").set((0.0, 0.0, 0.0))
    control = render(ctx)
    save_image(ctx, "hierarchy_object_identity", control)
    check(
        "control: the object's transform moves the render",
        float(np.abs(first - control).mean()) > 0.005,
        float(np.abs(first - control).mean()),
    )

    ctx.scene("render_direct")
    original = blend_convert.asset_types
    blend_convert.asset_types = lambda hou=None: (original(hou)[0], None)  # stock hierarchy in /obj
    try:
        _, nodes = dialog_import(ctx, source, [("collection", "Props")], dict(CONVERT, structure="hierarchy"))
    finally:
        blend_convert.asset_types = original
    root = [n for n in nodes if n.type().name() == "null" and n.userData(fn.COLLECTION_USER_DATA) == "Props"]
    check("direct: stock hierarchy with a collection null", len(root) == 1, [n.path() for n in nodes])
    if root:
        root[0].parmTuple("t").set(transform_t)
        root[0].parmTuple("r").set(transform_r)
    second = render(ctx)
    save_image(ctx, "direct", second)
    difference = float(np.abs(first - second).mean())
    level = float(first.mean())
    check("hierarchy render not black", level > 0.01, level)
    check(
        "hierarchy object == direct (mean abs difference < 4% of the mean)",
        difference < 0.04 * max(level, 1e-6),
        dict(difference=difference, mean=level, max_pixel=float(np.abs(first - second).max())),
    )

    ctx.scene("render_converted")
    _, nodes = dialog_import(ctx, source, [("collection", "Props")], CONVERT)
    node = nodes[0].node("blender_file")
    packed = render(ctx)
    node.parm("load_as").set("unpacked")
    unpacked = render(ctx)
    save_image(ctx, "converted_packed", packed)
    save_image(ctx, "converted_unpacked", unpacked)
    check("converted packed not black", float(packed.mean()) > 0.01, float(packed.mean()))
    check("converted render shows the objects", coloured(packed) > 40, coloured(packed))
    difference = float(np.abs(packed - unpacked).mean())
    check(
        "packed == unpacked",
        difference < 0.04 * max(float(packed.mean()), 1e-6),
        dict(difference=difference, mean=float(packed.mean())),
    )

    ctx.scene("render_link")
    _, nodes = dialog_import(ctx, source, [("collection", "Props")], dict(CONVERT, mode="link"))
    drain()
    prim_count(nodes[0].node("blender_file"))
    linked = render(ctx)
    save_image(ctx, "link", linked)
    check("link-mode render not black", float(linked.mean()) > 0.01, float(linked.mean()))
    check("link-mode render shows the objects", coloured(linked) > 40, coloured(linked))
    check(
        "link-mode render close to converted",
        float(np.abs(linked - packed).mean()) < 0.1 * float(packed.mean()),
        float(np.abs(linked - packed).mean()),
    )


def group_native(ctx, check):
    from cycles_houdini.blender import blend_catalogue
    from cycles_houdini.blender import blend_file_node as fn

    native = hou.sopNodeTypeCategory().nodeType(fn.NATIVE_REFERENCE_TYPE)
    import cycles_houdini_startup

    check("hide_native_reference", cycles_houdini_startup.hide_native_reference() and native.hidden())
    folder = ctx.scene("native")
    source = ctx.blend("m1")
    geo = hou.node("/obj").createNode("geo", "old_style")
    reference = geo.createNode(fn.NATIVE_REFERENCE_TYPE, "blender_reference")
    if not reference.evalParm("multiple_references"):
        reference.parm("multiple_references").set(1)
        reference.parm("references").set(1)
        reference.parm("references_initialized").set(1)
    reference.parm("file").set(posix(source))
    reference.parm("ref_enabled1").set(1)
    reference.parm("ref_target_kind1").set("object")
    reference.parm("ref_target1").set("Box")
    blend_catalogue.initialize_rows(reference)
    blend_catalogue.bind_targets(reference)
    reference.setDisplayFlag(True)
    reference.setRenderFlag(True)
    blend_catalogue.refresh_node(reference)
    drain()
    check("hidden type still creatable and cooks", prim_count(reference) > 0)
    hou.hipFile.save()
    hou.hipFile.load(str(folder / "scene.hip"), suppress_save_prompt=True, ignore_load_warnings=True)
    loaded = hou.node("/obj/old_style/blender_reference")
    blend_catalogue.refresh_node(loaded)
    drain()
    check("old scene loads and cooks", loaded is not None and prim_count(loaded) > 0)


# ---------------------------------------------------------------------- main


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=GROUPS, action="append")
    parser.add_argument("--work", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--device", default="CPU")
    parser.add_argument("--blender", default=None)
    parser.add_argument("--images", type=Path, help="also write the renders as .ppm here")
    args = parser.parse_args()
    check = Checks()
    from cycles_houdini.blender import blend_file_node as fn

    if None in fn.node_types():
        print("The Cycles Blender assets are not installed (stage the build).")
        return 1
    ctx = Context(args)
    print("Work folder:", ctx.work)
    for name in args.only or GROUPS:
        check.group = name
        started = time.time()
        try:
            globals()["group_" + name](ctx, check)
        except Exception:  # noqa: BLE001 - every group runs; a failure is a check
            check.error("group raised", traceback.format_exc()[-3000:])
        print(f"[{name}] {time.time() - started:.1f}s")
    failed = [item for item in check.items if not item["ok"]]
    summary = dict(
        result="failed" if failed else "passed",
        work=posix(ctx.work),
        houdini=hou.applicationVersionString(),
        passed=len(check.items) - len(failed),
        failed=len(failed),
        checks=check.items,
    )
    text = json.dumps(summary, indent=1, default=str)
    print("SUMMARY:" + text)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text, encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException:
        traceback.print_exc()
        code = 1
    try:  # stop Blender workers before the hard exit (os._exit skips atexit)
        from cycles_houdini.blender import blend_worker

        blend_worker.shutdown_all()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    sys.stdout.flush()
    # Houdini's own shutdown, as the parity smoke: os._exit crashed in exit handlers (exit code 139
    # after "passed"). os._exit stays as the fallback.
    hou.exit(code, suppress_save_prompt=True)
    os._exit(code)
