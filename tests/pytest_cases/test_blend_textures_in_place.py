# SPDX-License-Identifier: Apache-2.0
"""Textures stay in place (blend-import.md: Textures in place): the exporter's textures="in_place" mode and the builder.

Images on disk are recorded by their resolved absolute path and never read or copied; packed
images are still extracted with their sha256; a missing file is recorded, not fatal, and the
Houdini builder warns with material.missing_texture. textures="copy" stays the default.
"""

import hashlib
import importlib.util
import json
import pathlib
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from cycles_blender import blend_material_export as exporter
from cycles_houdini.blender import blend_materials

import test_blend_material_axes as axes

# The real-bpy cases add the blender mark and skip without bpy.
pytestmark = [pytest.mark.pure, pytest.mark.cpu]
_Images = exporter._Images
world = axes.world  # fake hou network (fixture)


class _Image:
    """The parts of bpy.types.Image that _Images.export reads."""

    _pointer = 5000

    def __init__(self, name, filepath="", *, packed=None, tiles=(), source=None, packed_tiles=(), library=None):
        _Image._pointer += 1
        self.pointer = _Image._pointer
        self.name = self.name_full = name
        self.filepath = filepath
        self.library = library
        self.source = source or ("TILED" if tiles else "FILE")
        self.use_multiview = False
        self.tiles = [SimpleNamespace(number=number) for number in tiles]
        self.file_format = "PNG"
        self.is_float = False
        self.packed_file = SimpleNamespace(data=packed) if packed is not None else None
        self.packed_files = [
            SimpleNamespace(filepath=path, tile_number=number, packed_file=SimpleNamespace(data=data))
            for number, path, data in packed_tiles
        ] or ([SimpleNamespace(filepath=filepath, tile_number=None, packed_file=self.packed_file)] if packed else [])
        self.colorspace_settings = SimpleNamespace(name="sRGB", is_data=False)
        self.alpha_mode = "STRAIGHT"

    def as_pointer(self):
        return self.pointer


@pytest.fixture
def blender(monkeypatch, tmp_path):
    """Fake bpy/OCIO. `//` resolves below tmp_path/blend, or below a library's own folder."""
    base = tmp_path / "blend"

    def abspath(path, library=None):
        if not path.startswith("//"):
            return path
        folder = Path(library.filepath[2:]).parent if library is not None else base
        return str(base / folder / path[2:]) if library is not None else str(base / path[2:])

    bpy = ModuleType("bpy")
    bpy.path = SimpleNamespace(abspath=abspath)
    ocio = ModuleType("PyOpenColorIO")
    ocio.GetCurrentConfig = lambda: SimpleNamespace(getColorSpace=lambda name: None)
    monkeypatch.setitem(sys.modules, "bpy", bpy)
    monkeypatch.setitem(sys.modules, "PyOpenColorIO", ocio)
    return base


@pytest.fixture
def no_reads(monkeypatch):
    """In-place exports must never read texture bytes."""

    def refuse(self, *args, **kwargs):
        raise AssertionError("texture bytes read: " + str(self))

    monkeypatch.setattr(pathlib.Path, "read_bytes", refuse)


def _file(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _posix(path):
    return str(Path(path).resolve()).replace("\\", "/")


# ---------------------------------------------------------------- exporter (fake bpy)


def test_file_on_disk_is_recorded_in_place_without_copy(blender, tmp_path, no_reads):
    source = _file(blender / "tex/aerial_beach_01_diff_1k.jpg", b"diffuse")
    images = _Images(tmp_path / "out", textures="in_place")
    result = images.export(_Image("beach", "//tex/../tex/aerial_beach_01_diff_1k.jpg"))
    assert result["filename"] == _posix(source) and Path(result["filename"]).is_absolute()
    assert "\\" not in result["filename"] and ".." not in result["filename"].split("/")
    assert (result["textures"], result["packed"], result["missing"]) == ("in_place", False, False)
    assert result["source_kind"] == "FILE" and result["tiles"] == []
    assert result["colorspace"] == "sRGB" and result["alpha_mode"] == "STRAIGHT"
    (dependency,) = result["dependencies"]
    assert dependency == {
        "filename": _posix(source),
        "source_path": str(source.resolve()),
        "packed": False,
        "missing": False,
    }
    assert images.dependencies == [dependency]
    assert not (tmp_path / "out").exists()  # nothing staged, not even textures/


def test_relative_paths_resolve_like_blender_also_inside_a_library(blender, tmp_path, no_reads):
    _file(blender / "tex/wood.png", b"wood")
    _file(blender / "lib/tex/wood.png", b"library wood")
    images = _Images(tmp_path / "out", textures="in_place")
    local = images.export(_Image("wood", "//tex/wood.png"))
    linked = images.export(_Image("wood", "//tex/wood.png", library=SimpleNamespace(filepath="//lib/props.blend")))
    assert local["filename"] == _posix(blender / "tex/wood.png")
    assert linked["filename"] == _posix(blender / "lib/tex/wood.png")
    # An absolute path is kept as is.
    absolute = images.export(_Image("abs", str(blender / "tex/wood.png")))
    assert absolute["filename"] == _posix(blender / "tex/wood.png") and not absolute["missing"]


def test_packed_images_are_still_extracted_with_sha256(blender, tmp_path):
    images = _Images(tmp_path / "out", textures="in_place")
    result = images.export(_Image("Packed color", "", packed=b"red"))
    assert result["filename"] == "textures/Packed color.png"
    assert (result["textures"], result["packed"], result["missing"]) == ("in_place", True, False)
    (dependency,) = result["dependencies"]
    assert dependency == {
        "filename": "textures/Packed color.png",
        "source_path": dependency["source_path"],
        "sha256": _sha(b"red"),
        "packed": True,
        "missing": False,
    }
    assert (tmp_path / "out/textures/Packed color.png").read_bytes() == b"red"
    # A packed image with a (missing) file path uses its file name, as in copy mode.
    wood = images.export(_Image("Wood", "//gone/wood_basecolor.png", packed=b"wood"))
    assert wood["filename"] == "textures/wood_basecolor.png" and wood["packed"] is True
    # Packed UDIM tiles: every tile extracted, path with the token.
    tiles = images.export(
        _Image(
            "Baked",
            "//baked.<UDIM>.png",
            tiles=(1001, 1002),
            packed_tiles=((1001, "baked.1001.png", b"t1"), (1002, "baked.1002.png", b"t2")),
        )
    )
    assert tiles["filename"] == "textures/baked.<UDIM>.png" and tiles["packed"] is True
    assert [(d["filename"], d["sha256"]) for d in tiles["dependencies"]] == [
        ("textures/baked.1001.png", _sha(b"t1")),
        ("textures/baked.1002.png", _sha(b"t2")),
    ]
    assert (tmp_path / "out/textures/baked.1002.png").read_bytes() == b"t2"


def test_udim_set_keeps_the_token_and_lists_existing_tiles(blender, tmp_path, no_reads):
    for number in (1001, 1002):
        _file(blender / f"scans_1024/wall.{number}.exr", b"tile")
    images = _Images(tmp_path / "out", textures="in_place")
    token = images.export(_Image("Wall", "//scans_1024/wall.<UDIM>.exr", tiles=(1001, 1002, 1003)))
    assert token["filename"] == _posix(blender / "scans_1024") + "/wall.<UDIM>.exr"
    assert token["tiles"] == [1001, 1002] and token["missing"] is False and token["packed"] is False
    assert [(Path(d["filename"]).name, d["missing"]) for d in token["dependencies"]] == [
        ("wall.1001.exr", False),
        ("wall.1002.exr", False),
        ("wall.1003.exr", True),
    ]
    assert all("sha256" not in d for d in token["dependencies"])
    # A path naming one tile: its last tile number (not the folder's 1024) becomes the token.
    named = images.export(_Image("Wall named", "//scans_1024/wall.1001.exr", tiles=(1001, 1002)))
    assert named["filename"] == token["filename"] and named["tiles"] == [1001, 1002]
    assert not (tmp_path / "out").exists()


def test_missing_files_are_recorded_not_fatal(blender, tmp_path, no_reads):
    images = _Images(tmp_path / "out", textures="in_place")
    gone = images.export(_Image("gone", "//tex/gone.png"))
    assert gone["filename"] == _posix(blender / "tex/gone.png")
    assert (gone["packed"], gone["missing"]) == (False, True)
    assert gone["dependencies"][0]["missing"] is True
    tiles = images.export(_Image("gone tiles", "//tex/gone.<UDIM>.png", tiles=(1001, 1002)))
    assert tiles["missing"] is True and tiles["tiles"] == [1001, 1002]  # none exists: Blender's tiles
    assert tiles["filename"].endswith("/tex/gone.<UDIM>.png")
    empty = images.export(_Image("no path", ""))
    assert (empty["filename"], empty["missing"], empty["dependencies"][0]["source_path"]) == ("", True, "")
    assert not (tmp_path / "out").exists()


def test_copy_mode_keeps_failing_on_a_missing_file(blender, tmp_path):
    with pytest.raises(FileNotFoundError):
        _Images(tmp_path / "copy").export(_Image("gone", "//tex/gone.png"))


@pytest.mark.parametrize("textures", ["copy", "in_place"])
@pytest.mark.parametrize("source", ["SEQUENCE", "MOVIE", "GENERATED", "VIEWER"])
def test_refused_sources_are_unchanged(blender, tmp_path, textures, source):
    _file(blender / "tex/a.png", b"a")
    image = _Image("seq", "//tex/a.png", source=source)
    with pytest.raises(exporter.BlendMaterialExportError, match="Image source " + source + " requires an explicit"):
        _Images(tmp_path / "out", textures=textures).export(image)
    multiview = _Image("stereo", "//tex/a.png")
    multiview.use_multiview = True
    with pytest.raises(exporter.BlendMaterialExportError, match="Multiview images require an explicit view"):
        _Images(tmp_path / "out", textures=textures).export(multiview)


def test_copy_mode_is_the_default_and_unchanged(blender, tmp_path):
    _file(blender / "tex/a.png", b"a")
    result = _Images(tmp_path / "out").export(_Image("a", "//tex/a.png"))
    assert set(result) == {
        "filename",
        "source_path",
        "source_kind",
        "colorspace",
        "is_data",
        "alpha_mode",
        "tiles",
        "dependencies",
        "color_interop_id",
    }
    assert result["filename"] == "textures/a.png"
    assert set(result["dependencies"][0]) == {"filename", "source_path", "sha256", "packed"}
    with pytest.raises(exporter.BlendMaterialExportError, match="Unknown texture mode"):
        _Images(tmp_path / "out", textures="link")


def test_unknown_colour_space_of_a_missing_file_falls_back_to_the_default_role(blender, monkeypatch, tmp_path):
    import PyOpenColorIO as ocio

    monkeypatch.delitem(sys.modules, "PyOpenColorIO")
    import PyOpenColorIO as real_ocio  # noqa: F811 - the real module, not the fixture's fake

    assert real_ocio is not ocio
    config = real_ocio.Config.CreateRaw()
    config.addColorSpace(real_ocio.ColorSpace(name="Byte Test"))
    config.setRole("default_byte", "Byte Test")
    monkeypatch.setattr(real_ocio, "GetCurrentConfig", lambda: config)
    image = _Image("gone", "//tex/gone.png")
    image.colorspace_settings.name = ""  # another OCIO config's name: unknown until loaded
    image.size = (0, 0)  # Blender cannot load a missing file
    result = _Images(tmp_path / "out", textures="in_place").export(image)
    assert result["missing"] is True and result["colorspace"] == "Byte Test"


# ---------------------------------------------------------------- builder (fake hou)


def _bare_builder(root):
    """A _Builder with only what image() uses; property() calls are recorded."""
    builder = object.__new__(blend_materials._Builder)
    builder.root, builder.record, builder.warnings = Path(root).resolve(), {"name": "Mat"}, []
    builder.set = {}
    builder.property = lambda item, key, value: builder.set.__setitem__(key, value)
    builder.parameter = lambda host, key, value: builder.set.__setitem__(key, value)
    builder.nodes = {"n0": None}
    return builder


def _image(**values):
    return dict(dict(is_data=True, alpha_mode="STRAIGHT", tiles=[], dependencies=[]), **values)


def test_builder_uses_absolute_in_place_paths_as_is(tmp_path):
    builder = _bare_builder(tmp_path / "root")
    texture = _file(tmp_path / "library/tex/wood.png", b"wood")
    item = dict(id="n0", name="Image Texture", image=_image(filename=_posix(texture), packed=False, missing=False))
    item["image"]["dependencies"] = [dict(filename=_posix(texture), packed=False, missing=False)]
    builder.image(item)
    assert builder.set["filename"] == _posix(texture) and builder.warnings == []
    # A Windows path is absolute too, separators normalised.
    item["image"]["filename"] = "C:\\Library\\tex\\wood.<UDIM>.png"
    item["image"]["tiles"] = [1001, 1002]
    builder.image(item)
    assert builder.set["filename"] == "C:/Library/tex/wood.<UDIM>.png" and builder.set["tiles"] == "1001 1002"
    item["image"]["filename"] = "tex/wood.png"
    with pytest.raises(blend_materials.BlendMaterialError, match="not absolute"):
        builder.image(item)


def test_builder_warns_for_a_missing_in_place_texture(tmp_path):
    builder = _bare_builder(tmp_path / "root")
    path = _posix(tmp_path / "library/tex/gone.png")
    builder.image(dict(id="n0", name="Base Color", image=_image(filename=path, packed=False, missing=True)))
    assert builder.set["filename"] == path
    (warning,) = builder.warnings
    assert warning["code"] == "material.missing_texture" and warning["severity"] == "warning"
    assert (warning["node"], warning["path"]) == ("Base Color", path)
    assert "Base Color" in warning["message"] and path in warning["message"]


@pytest.mark.parametrize("packed", [True, None])  # in_place packed record, copy-mode record
def test_builder_keeps_ownership_checks_for_owned_textures(tmp_path, packed):
    root = tmp_path / "root"
    _file(root / "tex/red.png", b"red")
    extra = {} if packed is None else dict(textures="in_place", packed=True, missing=False)
    dependency = dict(filename="tex/red.png", sha256=_sha(b"red"), packed=True)
    item = dict(id="n0", name="Image", image=_image(filename="tex/red.png", dependencies=[dependency], **extra))
    builder = _bare_builder(root)
    builder.image(item)
    assert builder.set["filename"] == str(root.resolve() / "tex/red.png")
    dependency["sha256"] = _sha(b"blue")
    with pytest.raises(blend_materials.BlendMaterialError, match="Texture changed"):
        builder.image(item)
    dependency["filename"] = "../outside.png"
    _file(tmp_path / "outside.png", b"x")
    with pytest.raises(blend_materials.BlendMaterialError, match="Missing or escaping owned texture"):
        builder.image(item)
    # An absolute path in an owned record is never trusted.
    item["image"]["filename"] = dependency["filename"] = _posix(tmp_path / "outside.png")
    with pytest.raises(blend_materials.BlendMaterialError, match="Missing or escaping owned texture"):
        builder.image(item)


def test_import_materials_reports_missing_textures_as_diagnostics(world, monkeypatch, tmp_path):
    monkeypatch.setitem(axes.INPUTS, "image_texture", ["vector"])
    monkeypatch.setitem(axes.OUTPUTS, "image_texture", ["color", "alpha"])
    definition = dict(
        engine_type="image_texture",
        surface_graph_adapter=True,
        inputs=[
            axes.socket("filename", "string"),
            axes.socket("colorspace", "string"),
            axes.socket("alpha_type", "enum", enum_values=axes.enum("unassociated", "associated", "ignore")),
            axes.socket("vector", "point"),
        ],
        outputs=[axes.socket("color", "color"), axes.socket("alpha")],
    )
    monkeypatch.setattr(axes, "DEFINITIONS", axes.DEFINITIONS + [definition])
    path = _posix(tmp_path / "library/tex/gone.png")
    image_node = dict(
        id="i",
        name="Leaf Color",
        type="ShaderNodeTexImage",
        properties={},
        inputs=[],
        outputs=[axes.sock(0, "Color", "RGBA")],
        image=dict(
            filename=path,
            source_path=path,
            source_kind="FILE",
            colorspace="Non-Color",
            is_data=True,
            alpha_mode="STRAIGHT",
            tiles=[],
            dependencies=[dict(filename=path, source_path=path, packed=False, missing=True)],
            textures="in_place",
            packed=False,
            missing=True,
            color_interop_id="",
        ),
    )
    shader = axes.diffuse(normal=None)
    shader["inputs"][0] = axes.sock(0, "Color", "RGBA", **axes.src("i", 0, "Color"))
    record = axes.material([image_node, shader])
    diagnostics = []
    host, nodes = axes.build(world, tmp_path / "root", record, diagnostics)
    assert nodes["Leaf_Color"].parm("filename").value == path
    (warning,) = [d for d in diagnostics if d["code"] == "material.missing_texture"]
    assert warning["path"] == path and warning["node"] == "Leaf Color"
    assert path in json.loads(host.user["cycles_blender_material_warnings"])[0]


# ---------------------------------------------------------------- exporter (real bpy)

SCRIPT = r"""
import json, os, sys, traceback
# argv[1]: <checkout>/houdini/python3.13libs; the Blender component is <checkout>/blender.
sys.path[:0] = [sys.argv[1], sys.argv[1] + "/../../blender"]
import bpy
from cycles_blender import blend_material_export as exporter

out = sys.argv[2]
bpy.ops.wm.read_factory_settings(use_empty=True)
os.makedirs(out + "/library/tex", exist_ok=True)
bpy.ops.wm.save_as_mainfile(filepath=out + "/library/scene.blend")


def write_png(path, rgba):
    image = bpy.data.images.new("tmp", 2, 2)
    image.pixels[:] = rgba * 4
    image.filepath_raw = path
    image.file_format = "PNG"
    image.save()
    bpy.data.images.remove(image)


for name, rgba in (("disk", (1, 0, 0, 1)), ("packme", (0, 1, 0, 1)), ("udim.1001", (0, 0, 1, 1)), ("udim.1002", (1, 1, 0, 1))):
    write_png(out + "/library/tex/" + name + ".png", list(rgba))

disk = bpy.data.images.load(out + "/library/tex/disk.png")
disk.filepath = "//tex/disk.png"
disk.colorspace_settings.name = "Non-Color"
packed = bpy.data.images.load(out + "/library/tex/packme.png")
packed.pack()
missing = bpy.data.images.load(out + "/library/tex/disk.png", check_existing=False)
missing.filepath = "//tex/gone.png"
udim = bpy.data.images.load(out + "/library/tex/udim.1001.png")
udim.source = "TILED"
udim.filepath = "//tex/udim.<UDIM>.png"
udim.tiles.new(tile_number=1002)
udim.tiles.new(tile_number=1003)
generated = bpy.data.images.new("generated", 2, 2)


def material(name, image):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    tree = mat.node_tree
    node = tree.nodes.new("ShaderNodeTexImage")
    node.image = image
    principled = next(n for n in tree.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled")
    tree.links.new(node.outputs["Color"], principled.inputs["Base Color"])
    return mat


materials = {n: material(n, i) for n, i in (("Disk", disk), ("Packed", packed), ("Missing", missing), ("Udim", udim), ("Generated", generated))}
result = {}


def run(key, names, directory, **options):
    try:
        exported = exporter.export_materials([materials[n] for n in names], out + "/" + directory, **options)
        images = [node["image"] for record in exported["materials"] for node in record["nodes"] if "image" in node]
        result[key] = dict(images=images, dependencies=exported["dependencies"])
    except Exception as error:
        result[key] = dict(error=type(error).__name__ + ": " + str(error))
    folder = out + "/" + directory
    result[key]["staged"] = sorted(
        os.path.relpath(os.path.join(p, f), folder).replace(os.sep, "/") for p, _, fs in os.walk(folder) for f in fs
    )


run("in_place_disk", ["Disk", "Missing", "Udim"], "stage_disk", textures="in_place")
run("in_place_packed", ["Packed"], "stage_packed", textures="in_place")
run("in_place_generated", ["Generated"], "stage_generated", textures="in_place")
run("copy", ["Disk", "Packed"], "stage_copy")
run("copy_missing", ["Missing"], "stage_copy_missing")
print("RESULT:" + json.dumps(dict(result, library=out + "/library")))
"""


@pytest.fixture(scope="module")
def real(tmp_path_factory):
    if importlib.util.find_spec("bpy") is None:
        pytest.skip("bpy (Blender as a Python module) is not installed")
    folder = tmp_path_factory.mktemp("blend_textures_in_place")
    script = folder / "make_and_export.py"
    script.write_text(SCRIPT, encoding="utf-8")
    root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [sys.executable, "-I", str(script), str(root / "houdini/python3.13libs"), str(folder)],
        capture_output=True,
        text=True,
        timeout=600,
        cwd=folder,
    )
    lines = [line for line in completed.stdout.splitlines() if line.startswith("RESULT:")]
    assert lines, completed.stdout[-4000:] + completed.stderr[-4000:]
    return json.loads(lines[-1][len("RESULT:") :])


@pytest.mark.blender
def test_real_bpy_in_place_records_paths_and_copies_nothing(real):
    library = _posix(real["library"])
    exported = real["in_place_disk"]
    assert "error" not in exported, exported
    assert exported["staged"] == []  # no file read or written
    disk, gone, udim = exported["images"]
    assert (disk["filename"], disk["packed"], disk["missing"]) == (library + "/tex/disk.png", False, False)
    assert disk["is_data"] is True and disk["colorspace"] == "Non-Color"
    assert (gone["filename"], gone["packed"], gone["missing"]) == (library + "/tex/gone.png", False, True)
    assert gone["colorspace"]  # falls back like an unreadable image
    assert udim["filename"] == library + "/tex/udim.<UDIM>.png"
    assert (udim["tiles"], udim["missing"]) == ([1001, 1002], False)
    assert [d["missing"] for d in udim["dependencies"]] == [False, False, True]
    assert all("sha256" not in d for image in exported["images"] for d in image["dependencies"])


@pytest.mark.blender
def test_real_bpy_in_place_extracts_packed_images(real):
    exported = real["in_place_packed"]
    assert "error" not in exported, exported
    (image,) = exported["images"]
    assert (image["filename"], image["packed"], image["missing"]) == ("textures/packme.png", True, False)
    assert exported["staged"] == ["textures/packme.png"]
    assert len(image["dependencies"][0]["sha256"]) == 64


@pytest.mark.blender
def test_real_bpy_refusals_and_copy_mode_unchanged(real):
    assert real["in_place_generated"]["error"].startswith("BlendMaterialExportError: Image source GENERATED")
    copied = real["copy"]
    assert "error" not in copied, copied
    assert copied["staged"] == ["textures/disk.png", "textures/packme.png"]
    assert all("packed" not in image and "textures" not in image for image in copied["images"])
    assert real["copy_missing"]["error"].startswith("FileNotFoundError")


# ---------------------------------------------------------------- worker pass-through


@pytest.mark.parametrize("textures", ["copy", "in_place"])
def test_worker_passes_the_texture_mode_through(monkeypatch, tmp_path, textures):
    from cycles_blender import blend_asset_scan, worker_server
    from cycles_houdini.blender import blend_worker

    source = _file(tmp_path / "lib.blend", b"BLENDER")
    sent, received = [], []

    def disposable(command, *args, **kwargs):
        sent.append(command)
        return worker_server._asset_command(command)

    def export_named_materials(path, names, output_dir, **options):
        received.append(options)
        return {"records": [], "errors": {}}

    monkeypatch.setattr(blend_worker, "_disposable_request", disposable)
    monkeypatch.setattr(blend_asset_scan, "export_named_materials", export_named_materials)
    options = {} if textures == "copy" else {"textures": textures}
    blend_worker.export_library_materials(source, ["Mat"], tmp_path / "out", "blender", **options)
    # Copy mode: the command and the call are exactly as before.
    assert ("textures" in sent[0], received) == (textures != "copy", [options])
    with pytest.raises(ValueError, match="texture mode"):
        blend_worker.export_library_materials(source, ["Mat"], tmp_path / "out", "blender", textures="link")
