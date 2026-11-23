# SPDX-License-Identifier: Apache-2.0
"""Render native imported controls against Blender-evaluated geometry bundles."""

from support.paths import artifact_label

import argparse
import json
import os
from pathlib import Path
import sys
import hou
import numpy as np

# Exercise current production Python with the existing compiled runtime.
root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "houdini/python3.13libs"))
import cycles_houdini as ch
from cycles_houdini import native_scene, object_properties
from cycles_houdini.device_selection import set_devices

parser = argparse.ArgumentParser()
parser.add_argument("--bundle-root", type=Path, required=True)
parser.add_argument("--device", default="CPU")
parser.add_argument("--source", type=Path, help="Override the source path recorded in the reference bundles")
args = parser.parse_args()
device = args.device
if device == "OPTIX":
    devices = [entry for entry in ch.devices() if entry["backend"] == "OPTIX"]
    device = next((entry["id"] for entry in devices if "4090" in entry["name"]), devices[0]["id"])
out = args.bundle_root.resolve()
manifests = {mode: json.loads((out / mode / "manifest.json").read_text()) for mode in ("native", "evaluated")}
native_mesh = manifests["native"]["meshes"][0]
assert native_mesh.get("subdivision"), native_mesh
assert not manifests["evaluated"]["meshes"][0].get("subdivision")
with np.load(out / "native/geometry.npz", allow_pickle=False) as data:
    positions = data[native_mesh["arrays"]["positions"]]
    matrix = np.asarray(manifests["native"]["instances"][0]["transform"]).reshape(4, 4)
    world = np.column_stack((positions, np.ones(len(positions)))) @ matrix.T
centre = (world[:, :3].min(axis=0) + world[:, :3].max(axis=0)) * 0.5
size = float(np.max(np.ptp(world[:, :3], axis=0)))
camera = hou.node("/obj").createNode("cam")
camera.setParms(
    dict(tx=float(centre[0]), ty=float(centre[1]), tz=float(centre[2] + size * 3), projection=1, orthowidth=size * 1.4)
)
receiver = hou.node("/obj").createNode("geo", "subdivision_import_receiver", run_init_scripts=False)
object_properties.install(receiver)
sop = receiver.createNode("attribwrangle")
sop.parm("class").set(0)
sop.setDisplayFlag(True)
sop.setRenderFlag(True)
rop = hou.node("/out").createNode("cycles_ipr")
set_devices(rop, device)
rop.setParms(
    dict(
        source_mode="objects",
        objectroot="/obj",
        vobject=receiver.name(),
        camera=camera.path(),
        lights="",
        resx=160,
        resy=160,
        samples=64,
        ipr_samples=64,
        adaptive_sampling=0,
        ipr_adaptive_sampling=0,
        use_denoise=0,
        ipr_use_denoise=0,
        use_material_assignments=0,
        material_kind="emission",
        transparent_background=1,
        pixel_filter="box",
        filter_width=1,
        allowmotionblur=0,
        render_threads=4,
    )
)
rop.parmTuple("background").set((0, 0, 0))
rop.parmTuple("basecolor").set((0.2, 0.4, 0.8))
frames, rows = {}, []


def plane(frame, name):
    field = frame["planes"][name]
    return (
        np.frombuffer(field["pixels"], dtype=np.float32)
        .reshape(frame["height"], frame["width"], field["channels"])
        .copy()
    )


with ch.Session(
    device=device,
    width=160,
    height=160,
    samples=64,
    execution="worker",
    threads=4,
    collect_diagnostics=True,
    passes=["combined", "position", "normal", "uv"],
) as session:
    for mode in ("native", "evaluated"):
        receiver.parm("cycles_blender_subdivision").set(mode == "native")
        reference = (
            (args.source or Path(manifests[mode]["source"])).resolve().as_posix()
            + "#object="
            + manifests[mode]["selector"]
        )
        sop.parm("snippet").set(
            'int p=addpoint(0,{0,0,0}); setpointattrib(0,"instance",p,'
            + json.dumps(reference)
            + '); setpointattrib(0,"index",p,'
            + repr(float(manifests[mode]["frame"]))
            + ");"
        )
        captured = native_scene.capture(rop)
        captured["passes"] = ["combined", "position", "normal", "uv"]
        frame = session.wait(session.apply(captured))
        status = session.status()
        assert not status["error"], status
        if mode == "native":
            assert status["subdivision_control_faces"] == native_mesh["polygon_count"], status
            assert status["subdivision_triangles"] > native_mesh["polygon_count"] * 2, status
        else:
            assert status["subdivision_control_faces"] == 0, status
        session.write_exr(out / (mode + "-" + artifact_label(args.device) + ".exr"))
        frames[mode] = {name: plane(frame, name) for name in ("position", "normal", "uv")}
        rows.append(
            dict(
                mode=mode,
                subdivision_control_faces=status["subdivision_control_faces"],
                subdivision_triangles=status["subdivision_triangles"],
                exported_vertices=manifests[mode]["meshes"][0]["vertex_count"],
                exported_polygons=manifests[mode]["meshes"][0]["polygon_count"],
            )
        )
    # Compare interior pixels with geometry present in both renderings. The
    # engines triangulate their parameter grids differently near silhouettes.
    a, b = frames["native"], frames["evaluated"]
    mask = (np.linalg.norm(a["normal"], axis=2) > 0.5) & (np.linalg.norm(b["normal"], axis=2) > 0.5)
    mask[1:] &= mask[:-1].copy()
    mask[:-1] &= mask[1:].copy()
    mask[:, 1:] &= mask[:, :-1].copy()
    mask[:, :-1] &= mask[:, 1:].copy()
    assert int(mask.sum()) > 1000, int(mask.sum())
    errors = {}
    for name in ("position", "normal", "uv"):
        error = np.linalg.norm(a[name][mask] - b[name][mask], axis=1)
        errors[name] = dict(
            mean=float(np.mean(error)), p99=float(np.percentile(error, 99)), maximum=float(np.max(error))
        )
    assert errors["position"]["mean"] < size * 0.02, errors
    assert errors["normal"]["mean"] < 0.15, errors
    assert errors["uv"]["mean"] < 0.02, errors
report = dict(
    result="passed",
    device=args.device,
    rows=rows,
    interior_pixels=int(mask.sum()),
    errors=errors,
    image_iris=ch.capabilities()["image_iris"],
)
(out / ("render-report-" + artifact_label(args.device) + ".json")).write_text(json.dumps(report, indent=2))
print(json.dumps(report), flush=True)
