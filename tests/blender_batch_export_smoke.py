# SPDX-License-Identifier: Apache-2.0
"""Run in Blender: batched RENDER evaluation, selectors and failure cleanup."""

import argparse
from support.package_loader import import_blender
import json
import math
from pathlib import Path
import sys

import bpy
import numpy as np

parser = argparse.ArgumentParser()
parser.add_argument("--output", required=True, type=Path)
args = parser.parse_args(sys.argv[sys.argv.index("--") + 1 :])
output = args.output.resolve()
output.mkdir(parents=True, exist_ok=True)
root = Path(__file__).resolve().parents[1]
exporter = import_blender("blend_export")
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
collection = bpy.data.collections.new("Detached")
collection.use_fake_user = True
mesh = bpy.data.meshes.new("Quad")
mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [(0, 1, 2, 3)])
obj = bpy.data.objects.new("Animated", mesh)
collection.objects.link(obj)
modifier = obj.modifiers.new("Render only thickness", "SOLIDIFY")
modifier.show_viewport = False
modifier.show_render = True
modifier.thickness = 0.2
obj.location.x = 1
obj.keyframe_insert(data_path="location", frame=1)
obj.location.x = 3
obj.keyframe_insert(data_path="location", frame=2)
for layer in obj.animation_data.action.layers:
    for strip in layer.strips:
        for bag in strip.channelbags:
            for curve in bag.fcurves:
                for point in curve.keyframe_points:
                    point.interpolation = "LINEAR"
scene.frame_set(9, subframe=0.5)
source = output / "batch.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(source))


def snapshot():
    return dict(
        objects=sorted(o.name for o in bpy.data.objects),
        cameras=sorted(c.name for c in bpy.data.cameras),
        scenes=[
            (
                s.name,
                s.camera.name if s.camera else None,
                s.frame_current,
                s.frame_subframe,
                s.render.engine,
                sorted(o.name for o in s.collection.objects),
            )
            for s in bpy.data.scenes
        ],
        window=bpy.context.window.scene.name if bpy.context.window else None,
        modifier=modifier.show_render,
    )


def request(kind="OBJECT", name="Animated", frame=1):
    return dict(source=str(source), selector_type=kind, selector=name, frame=frame)


before = snapshot()
requests = [
    request(frame=1),
    request(frame=1.25),
    request(name="Missing"),
    request(frame=math.nan),
    request("COLLECTION", "Detached", 2),
    request(frame=2),
]
registrations = []
register = bpy.utils.register_class


def counted(cls):
    registrations.append(cls.bl_idname)
    return register(cls)


bpy.utils.register_class = counted
try:
    results = exporter.evaluate_many(requests, [output / ("textures-" + str(i)) for i in range(len(requests))])
finally:
    bpy.utils.register_class = register
assert registrations == ["CH_PROTOTYPE_BUNDLE_EXPORT"], registrations
assert snapshot() == before, (before, snapshot())
assert isinstance(results[2], ValueError) and isinstance(results[3], ValueError), results
checks = []
for index in (0, 1, 4, 5):
    manifest, arrays = results[index]
    assert manifest["depsgraph_mode"] == "RENDER", manifest
    assert len(manifest["instances"]) == 1, manifest["instances"]
    assert manifest["meshes"][0]["vertex_count"] == 8, manifest["meshes"]
    expected = {0: 1.0, 1: 1.5, 4: 3.0, 5: 3.0}[index]
    assert np.isclose(manifest["instances"][0]["transform"][3], expected), manifest["instances"]
    scalar_manifest, scalar_arrays = exporter.evaluate(requests[index], output / ("scalar-" + str(index)))
    assert scalar_manifest == manifest, (scalar_manifest, manifest)
    for name in arrays:
        assert np.array_equal(arrays[name], scalar_arrays[name]), name
    checks.append(dict(index=index, mode=manifest["depsgraph_mode"], x=expected, vertices=8))

# Render callback failures restore modifiers/scene state and allow later frames.
original = exporter._export_depsgraph


def fail_one(bpy_, depsgraph, req, *rest):
    if req["frame"] == 1.25:
        raise RuntimeError("injected export failure")
    return original(bpy_, depsgraph, req, *rest)


exporter._export_depsgraph = fail_one
try:
    failures = exporter.evaluate_many([request(frame=1.25), request(frame=2)], output / "failure-textures")
finally:
    exporter._export_depsgraph = original
assert isinstance(failures[0], RuntimeError), failures
assert failures[1][0]["depsgraph_mode"] == "RENDER", failures[1]
assert snapshot() == before, (before, snapshot())
stream = exporter.iter_evaluate_many([request(frame=1.25), request(frame=2)], output / "stream-textures")
assert next(stream)[0]["depsgraph_mode"] == "RENDER"
stream.close()
assert snapshot() == before, (before, snapshot())
(output / "report.json").write_text(
    json.dumps(
        dict(
            result="passed",
            engine_registrations=len(registrations),
            checks=checks,
            failures_isolated=3,
            scene_restored=True,
            abandoned_stream_restored=True,
        ),
        indent=2,
    )
)
print("BATCH_EXPORT_QA_PASSED", json.dumps(checks))
