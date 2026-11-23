# SPDX-License-Identifier: Apache-2.0
"""Run under Blender's Python: the File > Import > Blender (.blend) scene listing.

blender --background --factory-startup --python-exit-code 1 --python tests/blend_list_contents_smoke.py -- --output <dir>
or, with the bpy module: python tests/blend_list_contents_smoke.py --output <dir> [--source file.blend ...]

Builds a fixture (nested and multi-parent collections, a view-layer-excluded
child, a geometry-nodes object, parenting, render-hidden data), checks the
in-Blender listing directly and over the real framed worker protocol, and
proves the listed files are never written. Extra --source files are listed
from a private copy and summarised.
"""

import argparse
import glob
import hashlib
import json
import os
import shutil
import sys
import tempfile
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

from cycles_blender import blend_list_contents as listing
from cycles_houdini.blender import blend_worker

argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else sys.argv[1:]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--output", type=Path, default=None)
parser.add_argument("--source", type=Path, action="append", default=[])
args = parser.parse_args(argv)
output = (args.output or Path(tempfile.mkdtemp(prefix="blend_list_contents_"))).resolve()
output.mkdir(parents=True, exist_ok=True)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build_fixture():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = 5, 48
    scene.render.fps, scene.render.fps_base = 30, 1.001
    mesh = bpy.data.meshes.new("Tri")
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    red, blue = bpy.data.materials.new("Red"), bpy.data.materials.new("Blue")
    props = bpy.data.collections.new("Props")
    shared = bpy.data.collections.new("Shared")
    disabled = bpy.data.collections.new("Disabled Child")
    hidden = bpy.data.collections.new("Render Hidden")
    hidden.hide_render = True
    scene.collection.children.link(props)
    props.children.link(shared)
    props.children.link(disabled)
    props.children.link(hidden)
    scene.collection.children.link(shared)  # Shared has two parents.
    rock = bpy.data.objects.new("Rock", mesh)
    rock.data.materials.append(red)
    rock.data.materials.append(None)
    rock.data.materials.append(blue)
    props.objects.link(rock)
    pebble = bpy.data.objects.new("Pebble", mesh)
    pebble.parent = rock
    pebble.hide_render = True
    shared.objects.link(pebble)
    scatter = bpy.data.objects.new("Scatter", mesh)
    scatter.modifiers.new("GeometryNodes", "NODES")
    disabled.objects.link(scatter)
    empty = bpy.data.objects.new("Root Empty", None)
    scene.collection.objects.link(empty)
    instancer = bpy.data.objects.new("Shared Instance", None)
    instancer.instance_type = "COLLECTION"
    instancer.instance_collection = shared
    hidden.objects.link(instancer)
    orphan = bpy.data.collections.new("Unlinked")
    orphan.use_fake_user = True
    layer = bpy.context.view_layer
    layer.layer_collection.children["Props"].children["Disabled Child"].exclude = True
    scene.view_layers.new("Second")
    path = output / "fixture.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(path))
    bpy.ops.wm.read_factory_settings(use_empty=True)
    return path


def check(result):
    collections = {entry["name"]: entry for entry in result["collections"]}
    objects = {entry["name"]: entry for entry in result["objects"]}
    assert set(collections) == {"Props", "Shared", "Disabled Child", "Render Hidden", "Unlinked"}, sorted(collections)
    assert collections["Props"]["children"] == ["Shared", "Disabled Child", "Render Hidden"]
    assert collections["Props"]["parent"] is None and collections["Props"]["objects"] == ["Rock"]
    assert sorted(collections["Shared"]["parents"]) == ["Props"], collections["Shared"]
    assert collections["Disabled Child"]["excluded"] is True
    assert collections["Shared"]["excluded"] is False and collections["Props"]["excluded"] is False
    assert collections["Render Hidden"]["hide_render"] is True
    assert collections["Unlinked"]["in_scene"] is False and collections["Unlinked"]["excluded"] is False
    assert objects["Rock"]["material_slots"] == ["Red", "", "Blue"], objects["Rock"]
    assert objects["Rock"]["type"] == "MESH" and objects["Rock"]["has_geometry_nodes"] is False
    assert objects["Pebble"]["parent"] == "Rock" and objects["Pebble"]["hide_render"] is True
    assert objects["Scatter"]["has_geometry_nodes"] is True and objects["Scatter"]["collections"] == ["Disabled Child"]
    assert objects["Root Empty"]["type"] == "EMPTY" and objects["Root Empty"]["collections"] == []
    assert objects["Shared Instance"]["instance_collection"] == "Shared"
    [scene] = result["scenes"]
    assert scene["view_layers"] == ["ViewLayer", "Second"] and scene["active_view_layer"] == "ViewLayer"
    assert scene["collections"] == ["Props", "Shared"] and scene["objects"] == ["Root Empty"]
    assert result["active_scene"] == scene["name"]
    assert result["frame_range"] == [5, 48] and abs(result["fps"] - 30 / 1.001) < 1e-4, result


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


def summary(result):
    return {
        "collections": len(result["collections"]),
        "excluded": sorted(c["name"] for c in result["collections"] if c["excluded"]),
        "objects": len(result["objects"]),
        "types": sorted({o["type"] for o in result["objects"]}),
        "geometry_nodes": sorted(o["name"] for o in result["objects"] if o["has_geometry_nodes"]),
        "materials": sorted({slot for o in result["objects"] for slot in o["material_slots"] if slot}),
        "top": next(s["collections"] for s in result["scenes"] if s["name"] == result["active_scene"]),
        "frame_range": result["frame_range"],
        "fps": result["fps"],
    }


fixture = build_fixture()
before = digest(fixture)
direct = blend_worker.checked_listing(listing.list_file(fixture))
check(direct)
executable = blender_executable()
report = {"blender": bpy.app.version_string, "fixture": summary(direct), "host_protocol": "skipped", "sources": {}}
if executable:
    hosted = blend_worker.list_blend_contents(fixture, executable, timeout=120)
    check(hosted)
    assert hosted["source"] == str(fixture) and len(hosted["signature"]) == 4
    report["host_protocol"] = "ok"
    try:
        blend_worker.list_blend_contents(output / "missing.blend", executable, timeout=60)
        raise AssertionError("missing file listed")
    except ValueError:
        pass
for source in args.source:
    copy = output / "sources" / source.name
    copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, copy)
    copied = digest(copy)
    result = (
        blend_worker.list_blend_contents(copy, executable, timeout=120)
        if executable
        else blend_worker.checked_listing(listing.list_file(copy))
    )
    assert digest(copy) == copied, "listing wrote the source file"
    report["sources"][source.name] = summary(result)
assert digest(fixture) == before, "listing wrote the fixture"
print(json.dumps(report, indent=1))
print("blend_list_contents_smoke: OK")
