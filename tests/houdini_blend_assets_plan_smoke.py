# SPDX-License-Identifier: Apache-2.0
"""Live check of the Blender asset import rules in Houdini 22 (hython).

Builds its own small asset library with real Blender in a temporary folder (two
.blend files named materials.blend in different folders, a worlds.blend, textures on
disk, a packed image, a missing texture, a two-tile UDIM set, HDRIs), then imports
through the asset browser's code path and checks the rules in docs/dev/blend-import.md:

  layout_material       textures in place, packed images in <blend>/tex,
                        cycles_import.json, missing texture warning, same-stem folders,
                        untitled scene -> cache; colour spaces under Houdini's OCIO
  collect               Collect Textures
  reload_after_collect  Reload from Library after a collect
  relink                Relink (non-UI functions)
  move_project          Move into Project (non-UI function)
  menus                 host menu files found and their expressions resolve
  worlds                Environment Light / World Material, replace / keep the existing one,
                        link reuse, reload, Make Editable, packed / missing HDRI, collect
  render_sanity         a tiny CPU render with an imported material and world

Never writes into the user's libraries, scenes or asset cache: the library, every
saved scene ($HIP) and the user cache (CYCLES_HOUDINI_ASSET_CACHE) are in the work
folder. Usage:

    hython tests/houdini_blend_assets_plan_smoke.py [--only SECTION] [--report FILE]
        [--work DIR] [--device CPU] [--blender EXE]

Runs every section even when one fails, prints a JSON summary (per check: section,
name, ok, message) and exits non-zero when any check failed. A failure whose
traceback is inside blend_convert.py or blend_file_node.py is marked "blocked_by".

The same file is the Blender half: Blender runs it with --build-library <folder>.
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

SECTIONS = (
    "layout_material",
    "collect",
    "reload_after_collect",
    "relink",
    "move_project",
    "menus",
    "worlds",
    "render_sanity",
)
HDRI_ROTATION_DEGREES = 30.0
HDRI_STRENGTH = 2.0
DIFFUSE_RGB = (0.8, 0.35, 0.05)  # orange: the rendered sphere must look like it


# ====================================================================== Blender half


def _blender_build_library(out):
    """Runs inside Blender (factory settings). Writes:

    libA/materials.blend  Mat_Textured (wood_diff sRGB + wood_rough Non-Color), Mat_Shared
                          (wood_diff), Mat_Packed (packed_logo.png, packed), Mat_Missing
                          (textures/missing_tex.png, absent), Mat_UDIM (tiles.<UDIM>.png,
                          tiles 1001 and 1002), object Crate (Mat_Textured)
    libA/worlds.blend     World_HDRI (Generated -> Mapping Z 30 deg -> sky.exr -> Background
                          strength 2), World_Data (sky_data.exr Non-Color), World_SRGB (sky_ldr.png
                          sRGB), World_Sky (Sky
                          Texture), World_Packed (packed_sky.exr, packed), World_Missing
    libB/materials.blend  Mat_Textured (libB/textures/wood_diff.png)
    library.json          names and paths
    Image paths are saved relative (//textures/...), so a copied library is complete.
    """
    import math

    import bpy

    out = Path(out).resolve()
    lib_a, lib_b = out / "libA", out / "libB"
    tex_a, tex_b = lib_a / "textures", lib_b / "textures"
    scratch = out / "_scratch"
    for folder in (tex_a, tex_b, scratch):
        folder.mkdir(parents=True, exist_ok=True)

    def write_image(path, rgb, width=4, height=4, float_buffer=False):
        image = bpy.data.images.new(Path(path).stem, width, height, float_buffer=float_buffer)
        image.pixels[:] = [*rgb, 1.0] * (width * height)
        image.filepath_raw = str(path)
        image.file_format = "OPEN_EXR" if str(path).endswith(".exr") else "PNG"
        image.save()
        bpy.data.images.remove(image)

    def load(path, colorspace=None):
        image = bpy.data.images.load(str(path))
        if colorspace:
            image.colorspace_settings.name = colorspace
        return image

    def material(name, image, socket="Base Color", second=None):
        mat = bpy.data.materials.new(name)
        mat.use_fake_user = True
        tree = mat.node_tree
        shader = tree.nodes["Principled BSDF"]
        node = tree.nodes.new("ShaderNodeTexImage")
        node.image = image
        tree.links.new(node.outputs["Color"], shader.inputs[socket])
        if second is not None:
            rough = tree.nodes.new("ShaderNodeTexImage")
            rough.image = second
            tree.links.new(rough.outputs["Color"], shader.inputs["Roughness"])
        return mat

    def save(path):
        bpy.context.preferences.filepaths.save_version = 0  # no .blend1 files
        bpy.ops.wm.save_as_mainfile(filepath=str(path))
        bpy.ops.file.make_paths_relative()
        bpy.ops.wm.save_mainfile()

    # Textures on disk.
    write_image(tex_a / "wood_diff.png", DIFFUSE_RGB)
    write_image(tex_a / "wood_rough.png", (0.5, 0.5, 0.5))
    write_image(tex_a / "tiles.1001.png", (0.9, 0.1, 0.1))
    write_image(tex_a / "tiles.1002.png", (0.1, 0.9, 0.1))
    write_image(tex_a / "sky.exr", (0.6, 0.7, 1.0), 16, 8, True)
    write_image(tex_a / "sky_data.exr", (0.5, 0.5, 0.5), 16, 8, True)
    write_image(tex_a / "sky_ldr.png", (0.5, 0.6, 0.8), 16, 8)
    write_image(tex_b / "wood_diff.png", (0.2, 0.6, 0.2))
    write_image(scratch / "packed_logo.png", (0.1, 0.2, 0.9))
    write_image(scratch / "packed_sky.exr", (0.3, 0.3, 0.3), 16, 8, True)

    # libA/materials.blend
    bpy.ops.wm.read_factory_settings(use_empty=True)
    diff = load(tex_a / "wood_diff.png", "sRGB")
    rough = load(tex_a / "wood_rough.png", "Non-Color")
    textured = material("Mat_Textured", diff, second=rough)
    material("Mat_Shared", diff)
    packed = load(scratch / "packed_logo.png", "sRGB")
    packed.pack()
    material("Mat_Packed", packed)
    missing = bpy.data.images.new("missing_tex", 4, 4)
    missing.source = "FILE"
    missing.filepath = str(tex_a / "missing_tex.png")
    material("Mat_Missing", missing)
    tiles = load(tex_a / "tiles.1001.png", "sRGB")
    tiles.name = "tiles"
    tiles.source = "TILED"
    tiles.filepath = str(tex_a / "tiles.<UDIM>.png")
    if not any(tile.number == 1002 for tile in tiles.tiles):
        tiles.tiles.new(tile_number=1002)
    tiles.reload()
    material("Mat_UDIM", tiles)
    mesh = bpy.data.meshes.new("Crate")
    mesh.from_pydata(
        [(-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1), (-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1)],
        [],
        [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)],
    )
    mesh.materials.append(textured)
    crate = bpy.data.objects.new("Crate", mesh)
    bpy.context.scene.collection.objects.link(crate)
    save(lib_a / "materials.blend")
    udim = [tile.number for tile in bpy.data.images["tiles"].tiles]

    # libA/worlds.blend
    bpy.ops.wm.read_factory_settings(use_empty=True)

    def world(name, image=None, mapping_z=None, strength=1.0, sky=False):
        item = bpy.data.worlds.new(name)
        item.use_fake_user = True
        tree = item.node_tree
        background = tree.nodes["Background"]
        background.inputs["Strength"].default_value = strength
        if sky:
            texture = tree.nodes.new("ShaderNodeTexSky")
        else:
            texture = tree.nodes.new("ShaderNodeTexEnvironment")
            texture.image = image
        tree.links.new(texture.outputs["Color"], background.inputs["Color"])
        if mapping_z is not None:
            mapping = tree.nodes.new("ShaderNodeMapping")
            mapping.inputs["Rotation"].default_value = (0.0, 0.0, math.radians(mapping_z))
            coordinate = tree.nodes.new("ShaderNodeTexCoord")
            tree.links.new(coordinate.outputs["Generated"], mapping.inputs["Vector"])
            tree.links.new(mapping.outputs["Vector"], texture.inputs["Vector"])
        return item

    # Blender 5.3 in background mode loads an EXR as "sRGB" (checked); an HDRI is linear.
    world("World_HDRI", load(tex_a / "sky.exr", "Linear Rec.709"), HDRI_ROTATION_DEGREES, HDRI_STRENGTH)
    world("World_Data", load(tex_a / "sky_data.exr", "Non-Color"))
    world("World_SRGB", load(tex_a / "sky_ldr.png", "sRGB"))
    world("World_Sky", sky=True)
    packed_sky = load(scratch / "packed_sky.exr", "Linear Rec.709")
    packed_sky.pack()
    world("World_Packed", packed_sky)
    missing_sky = bpy.data.images.new("missing_sky", 4, 4, float_buffer=True)
    missing_sky.source = "FILE"
    missing_sky.filepath = str(tex_a / "missing_sky.exr")
    world("World_Missing", missing_sky)
    save(lib_a / "worlds.blend")

    # libB/materials.blend: same stem, other folder.
    bpy.ops.wm.read_factory_settings(use_empty=True)
    material("Mat_Textured", load(tex_b / "wood_diff.png", "sRGB"))
    save(lib_b / "materials.blend")

    shutil.rmtree(scratch, ignore_errors=True)
    info = {
        "libA": lib_a.as_posix(),
        "libB": lib_b.as_posix(),
        "udim_tiles": udim,
        "blender": bpy.app.version_string,
        "colorspaces": {"sRGB": "sRGB", "data": "Non-Color"},
    }
    (out / "library.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    print("LIBRARY:" + json.dumps(info))


if "--build-library" in sys.argv:
    _blender_build_library(sys.argv[sys.argv.index("--build-library") + 1])
    sys.exit(0)


# ====================================================================== Houdini half

import hou  # noqa: E402

POSIX = lambda path: str(path).replace("\\", "/")  # noqa: E731


class Checks:
    def __init__(self):
        self.items = []
        self.section = ""

    def __call__(self, name, ok, message=""):
        item = {"section": self.section, "check": name, "ok": bool(ok), "message": str(message)[:2000]}
        self.items.append(item)
        print(("PASS " if ok else "FAIL ") + f"[{self.section}] {name}" + ("" if ok else f": {item['message']}"))
        return bool(ok)

    def error(self, name, error_text):
        blocked = next((f for f in ("blend_convert.py", "blend_file_node.py") if f in error_text), None)
        item = {"section": self.section, "check": name, "ok": False, "message": error_text[-3000:]}
        if blocked:
            item["blocked_by"] = "task H (" + blocked + ")"
        self.items.append(item)
        print(f"FAIL [{self.section}] {name}: exception\n{error_text}")


class Context:
    def __init__(self, args):
        self.args = args
        self.work = Path(args.work or tempfile.mkdtemp(prefix="ch_assets_plan_")).resolve()
        self.work.mkdir(parents=True, exist_ok=True)
        self.cache = self.work / "user_cache"
        os.environ["CYCLES_HOUDINI_ASSET_CACHE"] = str(self.cache)
        os.environ.setdefault(
            "CYCLES_HOUDINI_ASSET_BROWSER_SETTINGS_FILE", str(self.work / "asset_browser_settings.json")
        )
        from cycles_houdini.core import blender_bridge

        self.blender = args.blender or str(blender_bridge.require_executable())
        self.library = self.work / "library"
        self._built = False

    def build_library(self):
        if self._built:
            return
        from cycles_houdini.blender import blend_worker

        completed = __import__("subprocess").run(
            [
                self.blender,
                "-b",
                "--factory-startup",
                "--python-exit-code",
                "1",
                "--python",
                str(Path(__file__).resolve()),
                "--",
                "--build-library",
                str(self.library),
            ],
            capture_output=True,
            text=True,
            timeout=600,
            env=blend_worker.blender_environment(hou.getenv("CYCLES_HOUDINI_BLENDER_OCIO") or None),
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "Building the sample library failed:\n" + completed.stdout[-3000:] + completed.stderr[-3000:]
            )
        self.info = json.loads((self.library / "library.json").read_text(encoding="utf-8"))
        self._built = True

    def copy_library(self, name):
        """A private copy of the sample library (sections that move or change files)."""
        self.build_library()
        target = self.work / name / "library"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(self.library, target)
        return target

    def scene(self, name, saved=True):
        """A fresh scene; saved as <work>/<name>/project/<name>.hip ($HIP) unless saved=False."""
        hou.hipFile.clear(suppress_save_prompt=True)
        if not saved:
            return None
        project = self.work / name / "project"
        project.mkdir(parents=True, exist_ok=True)
        hip = project / (name + ".hip")
        hou.hipFile.save(POSIX(hip))
        return project


def record(name, id_type, blend):
    from cycles_houdini.assets.records import make_record

    return make_record(name, id_type, Path(blend).name, source=str(blend))


def texture_parms(node):
    """{image file name (pattern): (parm, unexpanded text)} of the texture nodes inside node."""
    found = {}
    for child in [node, *node.allSubChildren()]:
        parm = child.parm("filename") if "texture" in child.type().name() else None
        if parm is None:
            continue
        text = parm.unexpandedString()
        found[os.path.basename(text)] = (parm, text)
    return found


def colorspace_of(node, file_name):
    for child in node.allSubChildren():
        parm = child.parm("filename")
        if parm is not None and os.path.basename(parm.unexpandedString()) == file_name:
            space = child.parm("colorspace")
            return space.evalAsString() if space is not None else None
    return None


def decode_half(space):
    """Houdini OCIO: 0.5 in `space` -> scene_linear (green channel); None if not resolvable."""
    import PyOpenColorIO as ocio

    config = ocio.GetCurrentConfig()
    try:
        processor = config.getProcessor(space, ocio.ROLE_SCENE_LINEAR).getDefaultCPUProcessor()
    except Exception as error:  # noqa: BLE001
        return f"error: {error}"
    return processor.applyRGB([0.5, 0.5, 0.5])[1]


def tex_files(folder):
    folder = Path(folder)
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


def import_material(ctx, name, mode, blend, lines=None):
    from cycles_houdini.assets import importers

    report = (lambda text, warning: lines.append((text, warning))) if lines is not None else (lambda *a: None)
    [node] = importers.import_asset(record(name, "MATERIAL", blend), mode=mode, executable=ctx.blender, report=report)
    return node


def json_of(blend_dir):
    return json.loads((Path(blend_dir) / "cycles_import.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------- 1. layout_material


def section_layout_material(ctx, check):
    from cycles_houdini.blender import blend_library_materials

    ctx.build_library()
    lib_a = Path(ctx.info["libA"])
    blend_a, blend_b = lib_a / "materials.blend", Path(ctx.info["libB"]) / "materials.blend"
    project = ctx.scene("layout")
    root = project / "assets_blender"
    expected = {
        "wood_diff.png": POSIX(lib_a / "textures/wood_diff.png"),
        "wood_rough.png": POSIX(lib_a / "textures/wood_rough.png"),
    }
    for mode in ("link", "append"):
        lines = []
        node = import_material(ctx, "Mat_Textured", mode, blend_a, lines)
        parms = texture_parms(node)
        values = {name: text for name, (_, text) in parms.items()}
        check(f"{mode}: texture parms are the absolute library paths", values == expected, values)
        check(f"{mode}: forward slashes", all("\\" not in text for text in values.values()), values)
        check(
            f"{mode}: report says textures stay in the library",
            any("Collect" in text for text, _ in lines),
            lines,
        )
        if mode == "link":
            check("link: node is linked", blend_library_materials.link_info(node) is not None)
        # Colour spaces under Houdini's OCIO (the library was authored under Blender's).
        rough_space = colorspace_of(node, "wood_rough.png")
        diff_space = colorspace_of(node, "wood_diff.png")
        check(f"{mode}: Non-Color image -> data", rough_space == "data", rough_space)
        decoded = decode_half(diff_space) if diff_space and diff_space != "data" else None
        check(
            f"{mode}: sRGB image -> an sRGB-encoded Houdini space (0.5 decodes to ~0.214)",
            isinstance(decoded, float) and abs(decoded - 0.214) < 0.02,
            f"space={diff_space!r} decoded={decoded!r} OCIO={os.environ.get('OCIO')}",
        )
    blend_dir = root / "materials"
    check("nothing copied for in-place textures", tex_files(blend_dir / "tex") == [], tex_files(blend_dir / "tex"))
    try:
        data = json_of(blend_dir)
        originals = {POSIX(image.get("original")) for image in (data.get("images") or {}).values()}
        check("cycles_import.json parses and records the images", set(expected.values()) <= originals, originals)
        check(
            "cycles_import.json names its source .blend",
            POSIX(blend_a).casefold() in json.dumps(data).casefold(),
            data.get("source"),
        )
        located = {
            POSIX(image.get("original")): (POSIX(image.get("library_root")), image.get("relative_path"))
            for image in (data.get("images") or {}).values()
        }
        check(
            "cycles_import.json locates in-place images (library_root, relative_path) for Relink",
            located.get(expected["wood_diff.png"]) == (POSIX(lib_a), "textures/wood_diff.png"),
            located,
        )
    except Exception as error:  # noqa: BLE001
        check("cycles_import.json exists and parses", False, error)

    # Packed image: once in <blend>/tex under its real name, $HIP path.
    packed = import_material(ctx, "Mat_Packed", "append", blend_a)
    parms = texture_parms(packed)
    text = next(iter(parms.values()))[1] if parms else ""
    check(
        "packed image -> $HIP/assets_blender/materials/tex/packed_logo.png",
        text == "$HIP/assets_blender/materials/tex/packed_logo.png",
        text,
    )
    import_material(ctx, "Mat_Packed", "link", blend_a)
    check("packed image stored once", tex_files(blend_dir / "tex") == ["packed_logo.png"], tex_files(blend_dir / "tex"))
    try:
        images = json_of(blend_dir).get("images") or {}
        check(
            "cycles_import.json records the packed image",
            any(image.get("packed") and image.get("file") == "packed_logo.png" for image in images.values()),
            images,
        )
    except Exception as error:  # noqa: BLE001
        check("cycles_import.json records the packed image", False, error)

    # Missing texture: builds, warns.
    lines = []
    missing = import_material(ctx, "Mat_Missing", "append", blend_a, lines)
    check("missing texture: material builds", missing is not None and missing.path().startswith("/mat/"))
    check(
        "missing texture: warning in the report",
        any(warning and "missing_tex.png" in text for text, warning in lines),
        lines,
    )
    diagnostics = []
    blend_library_materials.import_library_material(
        str(blend_a), "Mat_Missing", executable=ctx.blender, diagnostics=diagnostics
    )
    check(
        "missing texture: material.missing_texture diagnostic",
        any(d.get("code") == "material.missing_texture" for d in diagnostics),
        diagnostics,
    )

    # Same stem from another folder.
    import_material(ctx, "Mat_Textured", "append", blend_b)
    second = root / "materials__libB"
    check("same stem: second .blend gets materials__libB", second.is_dir(), sorted(p.name for p in root.iterdir()))
    try:
        check(
            "same stem: each folder records its own source",
            "libb/materials.blend" in json.dumps(json_of(second)).casefold()
            and "liba/materials.blend" in json.dumps(json_of(blend_dir)).casefold(),
        )
    except Exception as error:  # noqa: BLE001
        check("same stem: each folder records its own source", False, error)

    # Unsaved scene: the cache.
    ctx.scene("layout_untitled", saved=False)
    node = import_material(ctx, "Mat_Packed", "append", blend_a)
    text = next(iter(texture_parms(node).values()))[1]
    cache_file = ctx.cache / "assets_blender" / "materials" / "tex" / "packed_logo.png"
    check("untitled scene: packed image in <cache>/assets_blender", cache_file.is_file(), text)
    check(
        "untitled scene: parm names the cache file",
        Path(hou.text.expandString(text)).resolve() == cache_file.resolve(),
        text,
    )


# ---------------------------------------------------------------------- 2. collect


def section_collect(ctx, check):
    from cycles_houdini.assets import collect
    from cycles_houdini.blender import blend_library_materials

    ctx.build_library()
    blend_a = Path(ctx.info["libA"]) / "materials.blend"
    project = ctx.scene("collect")
    tex = project / "assets_blender" / "materials" / "tex"
    first = import_material(ctx, "Mat_Textured", "link", blend_a)
    shared = import_material(ctx, "Mat_Shared", "link", blend_a)
    before = {name: text for name, (_, text) in texture_parms(first).items()}
    report = collect.collect_textures([first], show=False)
    after = {name: text for name, (_, text) in texture_parms(first).items()}
    check("files copied with real names", tex_files(tex) == ["wood_diff.png", "wood_rough.png"], tex_files(tex))
    check(
        "parms become $HIP paths",
        after
        == {
            "wood_diff.png": "$HIP/assets_blender/materials/tex/wood_diff.png",
            "wood_rough.png": "$HIP/assets_blender/materials/tex/wood_rough.png",
        },
        after,
    )
    check("still linked", blend_library_materials.link_info(first) is not None)
    check(
        "still locked",
        all(parm.isLocked() for parm, _ in texture_parms(first).values())
        and all(parm.isLocked() for parm in first.parms()),
    )
    check(
        "report counts (2 copied, 0 reused, 2 rewritten)",
        (len(report.copied), len(report.reused), report.rewritten) == (2, 0, 2),
        report.text(),
    )
    check("report notes nothing failed", not report.failed and not report.missing, report.text())
    hou.undos.performUndo()
    undone = {name: text for name, (_, text) in texture_parms(first).items()}
    check("one undo restores the library paths", undone == before, f"undos enabled={hou.undos.areEnabled()} {undone}")
    hou.undos.performRedo()
    redone = {name: text for name, (_, text) in texture_parms(first).items()}
    check("redo restores the collected paths", redone == after, redone)

    report = collect.collect_textures([shared], show=False)
    value = next(iter(texture_parms(shared).values()))[1]
    check("second material lands on the same file", value == "$HIP/assets_blender/materials/tex/wood_diff.png", value)
    check(
        "second material: 0 copied, 1 reused",
        (len(report.copied), len(report.reused)) == (0, 1),
        report.text(),
    )
    check("no duplicate copy", tex_files(tex) == ["wood_diff.png", "wood_rough.png"], tex_files(tex))
    report = collect.collect_textures([first], show=False)
    check("collecting again skips files already in the project", len(report.skipped) == 2, report.text())

    udim = import_material(ctx, "Mat_UDIM", "link", blend_a)
    report = collect.collect_textures([udim], show=False)
    value = next(iter(texture_parms(udim).values()))[1]
    check("UDIM: parm keeps the token", value == "$HIP/assets_blender/materials/tex/tiles.<UDIM>.png", value)
    check(
        "UDIM: every tile copied",
        {"tiles.1001.png", "tiles.1002.png"} <= set(tex_files(tex)),
        f"{tex_files(tex)}; tiles in Blender: {ctx.info.get('udim_tiles')}; {report.text()}",
    )

    ctx.scene("collect_untitled", saved=False)
    node = import_material(ctx, "Mat_Textured", "link", blend_a)
    try:
        collect.collect_textures([node], show=False)
        check("unsaved scene refuses", False, "no CollectError")
    except collect.CollectError as error:
        check("unsaved scene refuses with 'Save the scene first'", "Save the scene first" in str(error), error)


# ---------------------------------------------------------------------- 3. reload_after_collect


def section_reload_after_collect(ctx, check):
    from cycles_houdini.assets import collect, importers

    library = ctx.copy_library("reload")
    blend_a = library / "libA" / "materials.blend"
    project = ctx.scene("reload")
    copy = project / "assets_blender" / "materials" / "tex" / "wood_diff.png"
    node = import_material(ctx, "Mat_Textured", "link", blend_a)
    collect.collect_textures([node], show=False)
    collected = {name: text for name, (_, text) in texture_parms(node).items()}
    original = library / "libA" / "textures" / "wood_diff.png"

    def reload():
        lines = []
        new = importers.reload_linked_material(node.path(), report=lambda text, warning: lines.append((text, warning)))
        return new, lines

    data = copy.read_bytes()
    node, lines = reload()
    values = {name: text for name, (_, text) in texture_parms(node).items()}
    check("unchanged: collected paths kept", values == collected, values)
    check("unchanged: copy untouched", copy.read_bytes() == data)
    check("unchanged: no warnings", not any(w for _, w in lines), lines)

    original.write_bytes((library / "libA" / "textures" / "wood_rough.png").read_bytes())
    node, lines = reload()
    values = {name: text for name, (_, text) in texture_parms(node).items()}
    check("changed: collected paths kept", values == collected, values)
    check("changed: copy refreshed", copy.read_bytes() == original.read_bytes())

    original.unlink()
    node, lines = reload()
    values = {name: text for name, (_, text) in texture_parms(node).items()}
    check("deleted: collected paths kept", values == collected, values)
    check("deleted: copy kept", copy.is_file())
    check(
        "deleted: material.library_texture_missing warning",
        any(w and "library texture not found" in t for t, w in lines),
        lines,
    )
    check(
        "deleted: still linked",
        __import__("cycles_houdini.blender.blend_library_materials", fromlist=["x"]).link_info(node) is not None,
    )


# ---------------------------------------------------------------------- 4. relink


def section_relink(ctx, check):
    from cycles_houdini.assets import importers, relink, scene_paths

    library = ctx.copy_library("relink")
    blend_a = library / "libA" / "materials.blend"
    project = ctx.scene("relink")
    linked = import_material(ctx, "Mat_Textured", "link", blend_a)
    udim = import_material(ctx, "Mat_UDIM", "append", blend_a)
    nodes = importers.import_asset(record("Crate", "OBJECT", blend_a), mode="link")
    reference = next(
        (n for top in nodes for n in [top, *top.allSubChildren()] if n.type().name() == importers.REFERENCE_SOP),
        None,
    )
    check("object linked as a Blender Reference SOP", reference is not None, [n.path() for n in nodes])
    moved = library.parent / "libA_moved"
    shutil.move(str(library / "libA"), str(moved))
    # A second wood_rough.png elsewhere under the new folder: ambiguous by name.
    (moved / "extra").mkdir()
    shutil.copy2(moved / "textures" / "wood_rough.png", moved / "extra" / "wood_rough.png")
    hip = scene_paths.hip_folder()

    def plan_now():
        entries = relink.missing_entries()
        plan = relink.plan_relink(entries, POSIX(moved), hints=relink.hints_for(entries, hip))
        found = {os.path.basename(e.path): new for e, new in plan.found}
        ambiguous = {os.path.basename(e.path): c for e, c in plan.ambiguous}
        return entries, plan, found, ambiguous

    entries, plan, found, ambiguous = plan_now()
    names = sorted(os.path.basename(e.path) for e in entries)
    check(
        "missing entries found (textures, UDIM, .blend)",
        {"wood_diff.png", "wood_rough.png", "tiles.<UDIM>.png", "materials.blend"} <= set(names),
        names,
    )
    expected = {
        "wood_diff.png": POSIX(moved / "textures/wood_diff.png"),
        "tiles.<UDIM>.png": POSIX(moved / "textures/tiles.<UDIM>.png"),
        "materials.blend": POSIX(moved / "materials.blend"),
    }
    check(
        "with JSON: textures, UDIM pattern and .blend found", all(found.get(k) == v for k, v in expected.items()), found
    )
    check(
        "with JSON: wood_rough found through its recorded relative_path, not by guessing",
        found.get("wood_rough.png") == POSIX(moved / "textures/wood_rough.png") and not ambiguous,
        plan.details(),
    )

    for json_file in (project / "assets_blender").glob("*/cycles_import.json"):
        json_file.unlink()
    entries, plan, found, ambiguous = plan_now()
    check(
        "without JSON: textures, UDIM pattern and .blend found by name",
        all(found.get(k) == v for k, v in expected.items()),
        found,
    )
    check(
        "without JSON: ambiguous wood_rough listed with both candidates, not guessed",
        "wood_rough.png" not in found and len(ambiguous.get("wood_rough.png", ())) == 2,
        plan.details(),
    )
    failed = scene_paths.apply_rewrites(relink.rewrites(plan, hip), "Relink Blender assets")
    check("rewrites applied", not failed, failed)
    values = {name: text for name, (_, text) in texture_parms(linked).items()}
    check("linked material repaired", values.get("wood_diff.png") == expected["wood_diff.png"], values)
    check(
        "ambiguous parm left unchanged",
        values.get("wood_rough.png") == POSIX(library / "libA/textures/wood_rough.png"),
        values,
    )
    check(
        "linked material still linked and locked",
        __import__("cycles_houdini.blender.blend_library_materials", fromlist=["x"]).link_info(linked) is not None
        and all(p.isLocked() for p, _ in texture_parms(linked).values()),
    )
    value = next(iter(texture_parms(udim).values()))[1]
    check("UDIM parm repaired with its token", value == expected["tiles.<UDIM>.png"], value)
    if reference is not None:
        check(
            "reference .blend path repaired",
            POSIX(reference.evalParm("file")) == expected["materials.blend"],
            reference.evalParm("file"),
        )
    # The menu action without a UI, given the folder: only the ambiguous file stays missing.
    plan = relink.relink_blender_assets(folder=POSIX(moved))
    remaining = sorted(os.path.basename(e.path) for e in relink.missing_entries())
    check(
        "menu action (no UI, folder given) leaves only the ambiguous file",
        remaining == ["wood_rough.png"],
        f"{remaining} {plan.text() if plan else ''}",
    )


# ---------------------------------------------------------------------- 5. move_project


def section_move_project(ctx, check):
    from cycles_houdini.assets import move_project

    ctx.build_library()
    blend_a = Path(ctx.info["libA"]) / "materials.blend"
    cache_root = ctx.cache / "assets_blender"
    shutil.rmtree(cache_root / "materials", ignore_errors=True)
    ctx.scene("move_untitled", saved=False)
    packed = import_material(ctx, "Mat_Packed", "append", blend_a)
    import_material(ctx, "Mat_Textured", "link", blend_a)
    text = next(iter(texture_parms(packed).values()))[1]
    check(
        "untitled import reads the cache",
        POSIX(cache_root).casefold() in POSIX(hou.text.expandString(text)).casefold(),
        text,
    )
    project = ctx.work / "move" / "project"
    project.mkdir(parents=True, exist_ok=True)
    hou.hipFile.save(POSIX(project / "move.hip"))
    results, failures = move_project.move_imported_assets_into_project(show=False)
    check("moved without failures", results and not failures, failures)
    target = project / "assets_blender" / "materials"
    check(
        "blend folder moved into $HIP/assets_blender",
        (target / "tex" / "packed_logo.png").is_file(),
        tex_files(target / "tex"),
    )
    check("JSON moved", (target / "cycles_import.json").is_file())
    check("cache folder gone", not (cache_root / "materials").exists(), tex_files(cache_root))
    text = next(iter(texture_parms(packed).values()))[1]
    check("packed parm rewritten to $HIP", text == "$HIP/assets_blender/materials/tex/packed_logo.png", text)

    # Again into the same project: merge.
    ctx.scene("move_untitled2", saved=False)
    packed = import_material(ctx, "Mat_Packed", "append", blend_a)
    hou.hipFile.save(POSIX(project / "move2.hip"))
    results, failures = move_project.move_imported_assets_into_project(show=False)
    check("merge: no failures", results and not failures, failures)
    check("merge: reported as merged", bool(results) and results[0].merged, results)
    check(
        "merge: same content stored once", tex_files(target / "tex") == ["packed_logo.png"], tex_files(target / "tex")
    )
    text = next(iter(texture_parms(packed).values()))[1]
    check("merge: parm rewritten to $HIP", text == "$HIP/assets_blender/materials/tex/packed_logo.png", text)
    check("merge: cache folder gone", not (cache_root / "materials").exists())


# ---------------------------------------------------------------------- 6. menus


def section_menus(ctx, check):
    import importlib
    import re
    import xml.etree.ElementTree as ET

    for file_name in ("OPmenu.xml", "NetworkViewMenu.xml"):
        found = [POSIX(path) for path in hou.findFiles(file_name)]
        ours = [path for path in found if "cycles_houdini" in Path(path).read_text(encoding="utf-8", errors="replace")]
        if not check(f"{file_name} found on HOUDINI_PATH", len(ours) == 1, found):
            continue
        root = ET.parse(ours[0]).getroot()
        codes = [(e.tag, (e.text or "").strip()) for e in root.iter() if e.tag in ("expression", "scriptCode")]
        problems, calls = [], 0
        for tag, code in codes:
            try:
                compile(code, file_name, "eval" if tag == "expression" else "exec")
            except SyntaxError as error:
                problems.append(f"{tag}: {error}")
                continue
            for module, function in re.findall(r'__import__\("([\w.]+)"[^)]*\)\.(\w+)\(', code) + [
                (f"{a}.{b}", f) for a, b, f in re.findall(r"from ([\w.]+) import (\w+)\s*\n\s*\2\.(\w+)\(", code)
            ]:
                calls += 1
                try:
                    if not callable(getattr(importlib.import_module(module), function)):
                        problems.append(f"{module}.{function} is not callable")
                except Exception as error:  # noqa: BLE001
                    problems.append(f"{module}.{function}: {error}")
            if tag == "expression":
                try:
                    value = eval(code, {"__builtins__": __builtins__}, {"kwargs": {}})  # noqa: S307
                    if not isinstance(value, bool):
                        problems.append(f"expression returned {value!r}: {code}")
                except Exception as error:  # noqa: BLE001
                    problems.append(f"expression raised {error!r}: {code}")
        check(
            f"{file_name}: {calls} referenced functions resolve, expressions evaluate", calls and not problems, problems
        )


# ---------------------------------------------------------------------- 7. worlds


def _rop(name="cycles_rop"):
    rop = hou.node("/out").createNode("cycles_ipr", name)
    return rop


def import_world(ctx, name, mode, blend, existing="replace", lines=None):
    from cycles_houdini.assets import importers

    report = (lambda text, warning: lines.append((text, warning))) if lines is not None else (lambda *a: None)
    [node] = importers.import_asset(
        record(name, "WORLD", blend), mode=mode, executable=ctx.blender, report=report, existing_environment=existing
    )
    return node


def section_worlds(ctx, check):
    from cycles_houdini.assets import collect, importers
    from cycles_houdini.blender import blend_library_materials, blend_worlds

    ctx.build_library()
    lib_a = Path(ctx.info["libA"])
    worlds = lib_a / "worlds.blend"
    project = ctx.scene("worlds")
    rop = _rop()
    light = import_world(ctx, "World_HDRI", "append", worlds)
    check(
        "simple world -> envlight in /obj",
        light.type().name() == "envlight" and light.parent().path() == "/obj",
        light.path(),
    )
    check(
        "env_map = library path",
        light.parm("env_map").unexpandedString() == POSIX(lib_a / "textures/sky.exr"),
        light.parm("env_map").unexpandedString(),
    )
    check(
        "light_intensity = Strength",
        abs(light.evalParm("light_intensity") - HDRI_STRENGTH) < 1e-6,
        light.evalParm("light_intensity"),
    )
    space = light.parm("cycles_environment_colorspace").evalAsString()
    decoded = decode_half(space) if space not in ("data", "auto") else None
    check(
        "HDRI colour space: linear Houdini space (0.5 stays 0.5)",
        isinstance(decoded, float) and abs(decoded - 0.5) < 0.01,
        f"space={space!r} decoded={decoded!r}",
    )
    rotation = tuple(round(v, 4) for v in light.parmTuple("r").eval())
    check("rotation r = (0, -90 - z, 0)", rotation == (0.0, -90.0 - HDRI_ROTATION_DEGREES, 0.0), rotation)
    check("ROP world_source scene", rop.parm("world_source").evalAsString() == "scene")
    data_light = import_world(ctx, "World_Data", "append", worlds)
    check(
        "Non-Color HDRI -> data",
        data_light.parm("cycles_environment_colorspace").evalAsString() == "data",
        data_light.parm("cycles_environment_colorspace").evalAsString(),
    )
    check(
        "replace: previous light disabled", not light.evalParm("light_enable") and data_light.evalParm("light_enable")
    )
    srgb_light = import_world(ctx, "World_SRGB", "append", worlds)
    space = srgb_light.parm("cycles_environment_colorspace").evalAsString()
    decoded = decode_half(space) if space not in ("data", "auto") else None
    check(
        "sRGB environment image -> an sRGB-encoded Houdini space (0.5 decodes to ~0.214)",
        isinstance(decoded, float) and abs(decoded - 0.214) < 0.02,
        f"space={space!r} decoded={decoded!r}",
    )

    lines = []
    sky = import_world(ctx, "World_Sky", "append", worlds, lines=lines)
    check(
        "non-simple world -> World Material in /mat",
        "world_material" in sky.type().name() and sky.parent().path() == "/mat",
        sky.type().name(),
    )
    check("ROP world_source = material", rop.parm("world_source").evalAsString() == "material")
    check(
        "ROP world_material = the new node",
        rop.node(rop.parm("world_material").evalAsString()) == sky,
        rop.parm("world_material").evalAsString(),
    )

    # Replace / Keep existing on a scene with an enabled envlight and a ROP in material mode.
    for choice in ("keep", "replace"):
        ctx.scene("worlds_" + choice)
        rop = _rop()
        existing = hou.node("/mat").createNode("cycles::world_material::1", "existing_sky")
        rop.parm("world_source").set("material")
        rop.parm("world_material").set(existing.path())
        envlight = hou.node("/obj").createNode("envlight", "envlight1")
        from cycles_houdini.scene import light_properties

        light_properties.install(envlight)
        asked = importers.existing_environment(record("World_HDRI", "WORLD", worlds), mode="append")
        check(f"{choice}: question names the active environment", existing.path() in asked, asked)
        new = import_world(ctx, "World_HDRI", "append", worlds, existing=choice)
        if choice == "keep":
            check("keep: new light disabled", not new.evalParm("light_enable"))
            check("keep: existing light untouched", bool(envlight.evalParm("light_enable")))
            check(
                "keep: ROP stays in material mode",
                rop.parm("world_source").evalAsString() == "material"
                and rop.parm("world_material").evalAsString() == existing.path(),
            )
        else:
            check("replace: new light enabled", bool(new.evalParm("light_enable")))
            check(
                "replace: existing light disabled, not deleted",
                hou.node(envlight.path()) is not None and not envlight.evalParm("light_enable"),
            )
            check("replace: ROP switched to scene", rop.parm("world_source").evalAsString() == "scene")

    # Link, reuse, reload, Make Editable, packed, missing, collect.
    project = ctx.scene("worlds_link")
    _rop()
    linked = import_world(ctx, "World_HDRI", "link", worlds)
    again_lines = []
    again = import_world(ctx, "World_HDRI", "link", worlds, lines=again_lines)
    check("link: reused", again == linked, again_lines)
    check(
        "link: parameters locked except Enable",
        linked.parm("env_map").isLocked() and not linked.parm("light_enable").isLocked(),
    )
    reloaded, report = blend_worlds.reload_linked_world(linked, executable=ctx.blender)
    check(
        "reload_linked_world keeps the node",
        reloaded.path() == linked.path() and blend_library_materials.link_info(reloaded) is not None,
        report,
    )
    check("reload keeps env_map", reloaded.parm("env_map").unexpandedString() == POSIX(lib_a / "textures/sky.exr"))

    report = collect.collect_textures([linked], show=False)
    value = linked.parm("env_map").unexpandedString()
    check(
        "collect on the light: env_map -> $HIP/assets_blender/worlds/tex/sky.exr",
        value == "$HIP/assets_blender/worlds/tex/sky.exr",
        f"{value} {report.text()}",
    )
    check("collect on the light: file copied", (project / "assets_blender/worlds/tex/sky.exr").is_file())
    check("collect on the light: still linked", blend_library_materials.link_info(linked) is not None)
    # Reload from Library through the menu's path (importers): the collected copy stays (5.2).
    lines = []
    linked = importers.reload_linked_material(linked.path(), report=lambda text, warning: lines.append((text, warning)))
    value = linked.parm("env_map").unexpandedString()
    check(
        "menu reload of the collected linked world keeps $HIP/assets_blender/worlds/tex/sky.exr",
        value == "$HIP/assets_blender/worlds/tex/sky.exr" and blend_library_materials.link_info(linked) is not None,
        f"{value} {lines}",
    )

    # A linked world with a packed HDRI: reloading keeps the $HIP form of the extracted file.
    packed_link = import_world(ctx, "World_Packed", "link", worlds)
    before = packed_link.parm("env_map").unexpandedString()
    check("linked packed HDRI -> $HIP path", before == "$HIP/assets_blender/worlds/tex/packed_sky.exr", before)
    node, report = blend_worlds.reload_linked_world(packed_link, executable=ctx.blender)
    value = node.parm("env_map").unexpandedString()
    check("blend_worlds.reload_linked_world keeps the $HIP form of a packed HDRI", value == before, f"{value} {report}")
    node = importers.reload_linked_material(packed_link.path())
    value = node.parm("env_map").unexpandedString()
    check("menu reload keeps the $HIP form of a packed HDRI", value == before, value)

    importers.append_linked(linked)
    check("Make Editable: link data gone", blend_library_materials.link_info(linked) is None)
    check("Make Editable: parameters unlocked", not any(p.isLocked() for p in linked.parms()))

    packed = import_world(ctx, "World_Packed", "append", worlds)
    value = packed.parm("env_map").unexpandedString()
    check(
        "packed HDRI -> $HIP/assets_blender/worlds/tex/packed_sky.exr",
        value == "$HIP/assets_blender/worlds/tex/packed_sky.exr",
        value,
    )
    check(
        "packed HDRI file placed",
        (project / "assets_blender/worlds/tex/packed_sky.exr").is_file(),
        tex_files(project / "assets_blender/worlds/tex"),
    )

    lines = []
    missing = import_world(ctx, "World_Missing", "append", worlds, lines=lines)
    check("missing HDRI: light still created", missing is not None and missing.type().name() == "envlight")
    check("missing HDRI: warning", any(w and "missing_sky.exr" in t for t, w in lines), lines)


# ---------------------------------------------------------------------- 8. render_sanity


def section_render_sanity(ctx, check):
    import numpy as np

    from cycles_houdini import native_scene, registry
    from cycles_houdini.device_selection import set_devices

    ctx.build_library()
    lib_a = Path(ctx.info["libA"])
    ctx.scene("render")
    material = import_material(ctx, "Mat_Textured", "link", lib_a / "materials.blend")
    light = import_world(ctx, "World_HDRI", "append", lib_a / "worlds.blend")
    geo = hou.node("/obj").createNode("geo", "sanity_ball")
    sphere = geo.createNode("sphere")
    sphere.parm("type").set("polymesh")
    uv = None
    for type_name in ("uvproject", "texture"):
        try:
            uv = geo.createNode(type_name)
            break
        except hou.OperationFailed:
            continue
    if uv is not None:
        uv.setFirstInput(sphere)
        uv.setDisplayFlag(True)
        uv.setRenderFlag(True)
    geo.parm("shop_materialpath").set(material.path())
    cam = hou.node("/obj").createNode("cam", "sanity_cam")
    cam.parmTuple("t").set((0, 0, 4))
    rop = hou.node("/out").createNode("cycles_ipr", "sanity")
    set_devices(rop, ctx.args.device)
    size = 24
    rop.setParms(
        dict(
            source_mode="objects",
            vobject=geo.path(),
            camera=cam.path(),
            lights=light.path(),
            world_source="scene",
            resx=size,
            resy=size,
            samples=8,
            ipr_samples=8,
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
        check("render: no error", not status["error"], status["error"])
        check("render: no source diagnostics", not status["source_diagnostics"], status["source_diagnostics"])
        plane = frame["planes"]["combined"]
        pixels = np.frombuffer(plane["pixels"], np.float32).reshape(size, size, plane["channels"])[..., :3]
        centre = pixels[size // 2 - 3 : size // 2 + 3, size // 2 - 3 : size // 2 + 3].reshape(-1, 3).mean(0)
        corner = pixels[:3, :3].reshape(-1, 3).mean(0)
        r, g, b = (float(v) for v in centre)
        check("render: not black", max(r, g, b) > 0.02, centre.tolist())
        check("render: not magenta", not (r > 0.5 and b > 0.5 and g < 0.2), centre.tolist())
        check("render: sphere shows the orange texture (r > 1.5 b)", r > 1.5 * b, centre.tolist())
        check("render: background shows the HDRI (bluish)", float(corner[2]) > float(corner[0]) > 0.0, corner.tolist())
    finally:
        registry.close_all()


# ---------------------------------------------------------------------- main


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=SECTIONS, action="append")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--work", type=Path, help="work folder (default: a new temporary folder)")
    parser.add_argument("--device", default="CPU")
    parser.add_argument("--blender", default=None, help="Blender executable (default: the bridge's)")
    args = parser.parse_args()
    check = Checks()
    ctx = Context(args)
    print("Work folder:", ctx.work)
    for name in args.only or SECTIONS:
        check.section = name
        try:
            globals()["section_" + name](ctx, check)
        except Exception:  # noqa: BLE001 - every section runs; the failure is a check
            check.error("section raised", traceback.format_exc())
    failed = [item for item in check.items if not item["ok"]]
    summary = {
        "result": "failed" if failed else "passed",
        "work": POSIX(ctx.work),
        "houdini": hou.applicationVersionString(),
        "ocio": os.environ.get("OCIO", ""),
        "library": getattr(ctx, "info", None),
        "passed": len(check.items) - len(failed),
        "failed": len(failed),
        "checks": check.items,
    }
    text = json.dumps(summary, indent=2)
    print("SUMMARY:" + text)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text, encoding="utf-8")
    hou.hipFile.clear(suppress_save_prompt=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
