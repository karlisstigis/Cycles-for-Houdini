# SPDX-License-Identifier: Apache-2.0
"""Run under Blender's Python: asset listing, previews, cleanup and named material export.

blender --background --factory-startup --python-exit-code 1 --python tests/blend_asset_scan_smoke.py -- --output <dir>
or, with the bpy module: python tests/blend_asset_scan_smoke.py --output <dir>
"""

import argparse
import glob
import json
import os
import struct
import sys
import tempfile
import zlib
from pathlib import Path

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "houdini/python3.13libs"))
sys.path.insert(0, str(ROOT / "blender"))
try:
    import PyOpenColorIO  # noqa: F401
except ImportError:
    # The pip bpy module ships OCIO bindings in its private site-packages.
    version = "{}.{}".format(*bpy.app.version[:2])
    for parent in Path(bpy.__file__).parents:
        if parent.name == version:
            sys.path.extend(glob.glob(str(parent / "python/lib/python3*/site-packages")))
            break

from cycles_blender import blend_asset_scan as scan

argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else sys.argv[1:]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--output", type=Path, default=None)
args = parser.parse_args(argv)
output = (args.output or Path(tempfile.mkdtemp(prefix="blend_asset_scan_"))).resolve()
library = output / "library"
thumbnails = output / "thumbnails"
exported = output / "export"
for folder in (library, thumbnails, exported):
    folder.mkdir(parents=True, exist_ok=True)
CATALOG = "11111111-2222-3333-4444-555555555555"


def decode_png(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    position, idat, header = 8, b"", None
    while position < len(data):
        length, kind = struct.unpack("!I4s", data[position : position + 8])
        body = data[position + 8 : position + 8 + length]
        if kind == b"IHDR":
            header = struct.unpack("!IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
        position += 12 + length
    width, height = header[:2]
    raw = zlib.decompress(idat)
    stride = width * 4 + 1
    assert all(raw[row * stride] == 0 for row in range(height))
    return width, height, b"".join(raw[row * stride + 1 : (row + 1) * stride] for row in range(height))


def build_library():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    inner_mesh = bpy.data.meshes.new("InnerMesh")
    inner_mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    inner_mesh.use_fake_user = True
    bpy.ops.wm.save_as_mainfile(filepath=str(library / "inner.blend"))

    bpy.ops.wm.read_factory_settings(use_empty=True)
    image = bpy.data.images.new("Swatch", width=2, height=2)
    image.pixels = [0.8, 0.1, 0.1, 1.0] * 4
    (library / "textures").mkdir(exist_ok=True)
    image.filepath_raw = str(library / "textures" / "swatch.png")
    image.file_format = "PNG"
    image.save()
    image.source = "FILE"

    marked = bpy.data.materials.new("Metal - Galvanized")
    nodes = marked.node_tree.nodes
    texture = nodes.new("ShaderNodeTexImage")
    texture.image = image
    marked.node_tree.links.new(texture.outputs["Color"], nodes["Principled BSDF"].inputs["Base Color"])
    marked.asset_mark()
    marked.asset_data.tags.new("metal")
    marked.asset_data.tags.new("worn")
    marked.asset_data.description = "Galvanized steel"
    marked.asset_data.author = "Fixture"
    marked.asset_data.license = "CC0"
    marked.asset_data.copyright = "None"
    marked.asset_data.catalog_id = CATALOG
    preview = marked.preview_ensure()
    preview.image_size = (4, 2)
    pixels = []
    for index in range(8):
        value = index * 20 | 100 << 8 | 50 << 16 | 255 << 24
        pixels.append(value - (1 << 32) if value >= 1 << 31 else value)
    preview.image_pixels = pixels

    plain = bpy.data.materials.new("Plain Marked")
    plain.asset_mark()

    broken = bpy.data.materials.new("Broken Output")
    for link in list(broken.node_tree.links):
        broken.node_tree.links.remove(link)
    broken.asset_mark()

    unmarked = bpy.data.materials.new("Unmarked")
    unmarked.use_fake_user = True

    with bpy.data.libraries.load(str(library / "inner.blend"), link=True) as (_available, selected):
        selected.meshes = ["InnerMesh"]
    obj = bpy.data.objects.new("Linked Mesh Object", selected.meshes[0])
    obj.asset_mark()
    collection = bpy.data.collections.new("Prop Collection")
    collection.objects.link(obj)
    collection.asset_mark()
    world = bpy.data.worlds.new("Sky")
    world.asset_mark()
    group = bpy.data.node_groups.new("Geo Tool", "GeometryNodeTree")
    group.asset_mark()
    bpy.ops.wm.save_as_mainfile(filepath=str(library / "assets.blend"), relative_remap=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)


def check_scan():
    source = library / "assets.blend"
    before = {item: os.stat(item).st_mtime_ns for item in library.rglob("*")}
    result = scan.scan_file(source, thumbnails)
    assert result["source"] == str(source)
    assert result["signature"] == scan.signature_of(source)
    by_key = {(asset["id_type"], asset["name"]): asset for asset in result["assets"]}
    assert set(by_key) == {
        ("MATERIAL", "Metal - Galvanized"),
        ("MATERIAL", "Plain Marked"),
        ("MATERIAL", "Broken Output"),
        ("OBJECT", "Linked Mesh Object"),
        ("COLLECTION", "Prop Collection"),
        ("WORLD", "Sky"),
        ("NODETREE", "Geo Tool"),
    }, sorted(by_key)
    metal = by_key["MATERIAL", "Metal - Galvanized"]
    assert metal["meta"] == {
        "catalog_id": CATALOG,
        "tags": ["metal", "worn"],
        "author": "Fixture",
        "description": "Galvanized steel",
        "license": "CC0",
        "copyright": "None",
    }, metal["meta"]
    assert metal["object_type"] is None
    assert metal["preview_size"] == [4, 2]
    assert metal["preview"] == scan.thumbnail_name(source, "MATERIAL", "Metal - Galvanized", result["signature"])
    width, height, rgba = decode_png((thumbnails / metal["preview"]).read_bytes())
    assert (width, height) == (4, 2)
    # Bottom-up Blender rows become top-down PNG rows.
    bottom_row = bytes(v for i in range(4) for v in (i * 20, 100, 50, 255))
    top_row = bytes(v for i in range(4, 8) for v in (i * 20, 100, 50, 255))
    assert rgba == top_row + bottom_row, list(rgba)
    assert by_key["MATERIAL", "Plain Marked"]["meta"]["catalog_id"] == ""
    assert by_key["OBJECT", "Linked Mesh Object"]["object_type"] == "MESH"
    assert by_key["COLLECTION", "Prop Collection"]["preview"] is None
    # Every linked ID and Library datablock was removed again.
    assert len(bpy.data.libraries) == 0, list(bpy.data.libraries)
    for name in ("materials", "objects", "collections", "worlds", "node_groups", "meshes", "images"):
        assert len(getattr(bpy.data, name)) == 0, (name, list(getattr(bpy.data, name)))
    # Repeated scans stay bounded and deterministic.
    for _ in range(20):
        again = scan.scan_file(source, thumbnails)
    assert again["assets"] == result["assets"]
    assert len(bpy.data.libraries) == 0 and len(bpy.data.materials) == 0
    after = {item: os.stat(item).st_mtime_ns for item in library.rglob("*")}
    assert after == before, "the library folder was written"
    return result


def check_export():
    source = library / "assets.blend"
    requested = ["Metal - Galvanized", "Broken Output", "Plain Marked", "Missing"]
    result = scan.export_named_materials(source, requested, exported)
    names = [record["name"] for record in result["records"]]
    assert names == ["Metal - Galvanized", "Plain Marked"], result["errors"]
    # One unsupported material is reported without failing the others.
    assert set(result["errors"]) == {"Missing", "Broken Output"}, result["errors"]
    assert "unconnected" in result["errors"]["Broken Output"], result["errors"]
    metal = result["records"][0]
    images = [node["image"] for node in metal["nodes"] if "image" in node]
    assert len(images) == 1 and len(images[0]["dependencies"]) == 1
    texture = exported / images[0]["dependencies"][0]["filename"]
    assert texture.is_file() and texture.is_relative_to(exported)
    assert len(bpy.data.libraries) == 0 and len(bpy.data.materials) == 0 and len(bpy.data.images) == 0
    return result


def check_preferences():
    libraries = scan.dump_asset_libraries()
    assert isinstance(libraries, list)
    for entry in libraries:
        assert set(entry) == {"name", "path", "import_method", "enabled", "remote"}, entry


def blender_executable():
    """Real Blender, or (bpy module) a wrapper that runs the --python script with this interpreter."""
    if bpy.app.binary_path:
        return bpy.app.binary_path
    if os.name == "nt":
        return None
    wrapper = output / "fake-blender.sh"
    extra = os.pathsep.join(path for path in sys.path if path.endswith("site-packages") and "bpy" in path)
    wrapper.write_text(
        "#!/bin/sh\n"
        'while [ "$#" -gt 0 ] && [ "$1" != "--python" ]; do shift; done\n'
        "shift\n"
        f'PYTHONPATH="{extra}" exec "{sys.executable}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return str(wrapper)


def check_host_protocol(executable):
    """Host functions over the real framed worker protocol and the preferences script."""
    from cycles_houdini.blender import blend_worker

    source = library / "assets.blend"
    missing = library / "missing.blend"
    host_thumbnails = output / "host-thumbnails"
    entries = blend_worker.assets_many([source, missing], executable, host_thumbnails, timeout=120)
    assert [entry["source"] for entry in entries] == [str(source), str(missing.resolve())]
    assert "error" in entries[1] and "assets" not in entries[1], entries[1]
    assets = {(a["id_type"], a["name"]): a for a in entries[0]["assets"]}
    assert len(assets) == 7 and (host_thumbnails / assets["MATERIAL", "Metal - Galvanized"]["preview"]).is_file()
    export_dir = output / "host-export"
    exported = blend_worker.export_library_materials(
        source, ["Metal - Galvanized", "Broken Output"], export_dir, executable, timeout=120
    )
    assert [record["name"] for record in exported["records"]] == ["Metal - Galvanized"], exported["errors"]
    assert list(exported["errors"]) == ["Broken Output"]
    libraries = blend_worker.dump_asset_library_preferences(executable, timeout=60)
    assert isinstance(libraries, list)
    return len(entries)


build_library()
scanned = check_scan()
export = check_export()
check_preferences()
executable = blender_executable()
host_entries = check_host_protocol(executable) if executable else "skipped"
print(
    json.dumps(
        {
            "blender": bpy.app.version_string,
            "assets": len(scanned["assets"]),
            "exported": [r["name"] for r in export["records"]],
            "errors": export["errors"],
            "host_protocol": host_entries,
            "output": str(output),
        }
    )
)
print("blend_asset_scan_smoke: OK")
