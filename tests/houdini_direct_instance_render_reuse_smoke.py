# SPDX-License-Identifier: Apache-2.0
"""Render retained public two-root instances after no-op and camera captures."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import time
import traceback

import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini.scene import native_scene
from blend_direct_fixture import mesh, prototype


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--out", type=Path, required=True)
parser.add_argument("--device", required=True)
args = parser.parse_args()
if args.device == "OPTIX":
    from support.devices import resolve_device_argument

    arguments, _ = resolve_device_argument(
        __file__, ["--device", args.device], ch.devices(), os.environ.get("CYCLES_HOUDINI_TEST_OPTIX_DEVICE")
    )
    args.device = arguments[1]
args.out = args.out.resolve()
args.out.mkdir(parents=True, exist_ok=True)
width, height, samples = 96, 64, 8
obj = hou.node("/obj").createNode("geo", "retained_render", run_init_scripts=False)
points = obj.createNode("attribwrangle")
points.parm("class").set(0)
points.addSpareParmTuple(hou.FloatParmTemplate("pose_shift", "Placement", 1))
reference = "fixture.blend#collection=Rendered"
points.parm("snippet").set(
    'for(int i=0;i<4;i++){int p=addpoint(0,set((i%2-.5)*1.2+chf("pose_shift"),(i/2-.5)*1.2,0));'
    'setpointattrib(0,"instance",p,' + json.dumps(reference) + ");"
    'setpointattrib(0,"index",p,1.0);setpointattrib(0,"id",p,i);}'
)
material = ch.create_material(name="retained_native_emission", shader="emission")
# The default this test was written for (new nodes now take Blender's).
material.node("surface_shader").setParms(dict(strength=10.0))
material.node("surface_shader").parmTuple("color").set((0.2, 0.7, 0.4))
members = [
    prototype(
        "render/member:" + str(i),
        mesh("render/mesh:" + str(i), size=(0.4, 0.4, 0.4), material=material.path()),
        translate=((i - 0.5) * 0.5, 0, 0),
    )["instances"][0]
    for i in range(2)
]
camera = hou.node("/obj").createNode("cam", "retained_render_camera")
camera.setParms(dict(tz=6, projection=1, orthowidth=4))
report = dict(result="running", runtime=str(ch.__file__), device=args.device, samples=samples, checks=[])


def handle():
    return ch._native.create(args.device, width, height, samples, True, False, False, False, None, True)


def update():
    base = ch.geometry_request(obj, points, material="fallback", material_mode="native")
    ordinary = dict(base, render_types=dict(mesh=True, points=False, curves=True, volume=True, packed=True))
    direct = dict(
        base,
        object_key=base["object_key"] + "/point-instances",
        geometry_key=base["geometry_key"] + "/point-instances",
        render_types=dict(mesh=False, points=False, curves=False, volume=False, packed=True),
        point_instancing=dict(
            mode="fast",
            instancer_path=obj.path(),
            fallback=-1,
            frame=hou.frame(),
            prototypes=[
                dict(path=reference, node_id=0, source_reference=reference, source_frame=1.0, direct_instances=members)
            ],
        ),
    )
    return dict(
        geometry=[ordinary, direct],
        camera_path=camera.path(),
        background=(0, 0, 0),
        materials=[dict(key="fallback", kind="emission", color=(1, 0, 0))],
        material_resolver=native_scene.resolve_materials,
        motion=dict(enabled=False, fps=24, shutter_frames=0.5, offset=0, samples=3),
    )


def capture(target, executor):
    data = update()
    job = ch._native.acquire_apply(target, data, hou.time())
    try:
        while True:
            executor.submit(ch._native.prewarm_apply, job).result(timeout=30)
            if not ch._native.expand_apply(job):
                break
        while not ch._native.prepare_apply(target, data, hou.time(), True, job):
            pass
        executor.submit(ch._native.convert_apply, job).result(timeout=30)
        return ch._native.finish_apply(job)
    except BaseException:
        executor.submit(ch._native.discard_apply, job).result(timeout=30)
        raise


def rendered(engine, snapshot, previous=None):
    generation = (
        ch._native.apply_snapshot(engine, snapshot)
        if previous is None
        else ch._native.apply_snapshot(engine, snapshot, previous)
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        status = ch._native.status(engine)
        assert status["state"] != "failed" and not status["error"], status
        frame = ch._native.frame(engine)
        if frame is not None and frame["generation"] == generation and frame["complete"]:
            plane = frame["planes"]["combined"]
            pixels = np.frombuffer(plane["pixels"], np.float32).reshape(height, width, plane["channels"]).copy()
            assert np.isfinite(pixels).all()
            support = np.max(pixels[:, :, :3], axis=2) > 0.02
            assert support.sum() > 100, (int(support.sum()), pixels.max((0, 1)).tolist(), status)
            assert status["object_resources"] == 8 and status["geometry_resources"] == 2, status
            assert status["render_device_ids"] == [args.device], status
            return pixels, generation, status
        time.sleep(0.01)
    raise TimeoutError(status)


capture_handle = handle()
engine = ch._native.engine_create(capture_handle)
previous = None
initial = None
try:
    with ThreadPoolExecutor(max_workers=1) as executor:
        for label, frame_number, camera_x, shift in (
            ("cold", 1, 0, 0),
            ("no-op", 2, 0, 0),
            ("camera", 3, 0.35, 0),
            ("camera-no-op", 4, 0.35, 0),
            ("point-placement", 5, 0.35, 0.2),
            ("placement-no-op", 6, 0.35, 0.2),
        ):
            hou.setFrame(frame_number)
            camera.parm("tx").set(camera_x)
            if points.evalParm("pose_shift") != shift:
                points.parm("pose_shift").set(shift)
            snapshot = capture(capture_handle, executor)
            stats = ch._native.native_stats(capture_handle)
            if previous is not None:
                delta = ch._native.snapshot_info(snapshot, previous)["difference"]
                if label == "point-placement":
                    assert stats["direct_population_reuses"] == 0 and stats["direct_instances_expanded"] == 8, stats
                    assert delta["object_poses"] == 8 and delta["object_upserts"] == 0, delta
                else:
                    assert stats["direct_population_reuses"] == 1 and stats["direct_instances_expanded"] == 0, stats
                    assert delta["object_poses"] == 0, delta
                assert not any(
                    delta[k]
                    for k in (
                        "mesh_upserts",
                        "object_upserts",
                        "object_deletions",
                        "geometry_deletions",
                    )
                ), delta
            actual, generation, status = rendered(engine, snapshot, previous)
            fresh = handle()
            reference_engine = ch._native.engine_create(fresh)
            try:
                reference_snapshot = capture(fresh, executor)
                reference_pixels, _, _ = rendered(reference_engine, reference_snapshot)
            finally:
                ch._native.close(reference_engine)
                ch._native.close(fresh)
            error = float(np.sqrt(np.mean((actual - reference_pixels) ** 2)))
            assert error < 0.0001, (label, error)
            if label in ("no-op", "camera-no-op", "placement-no-op"):
                assert generation == report["checks"][-1]["generation"], (label, generation, report["checks"][-1])
            if label in ("camera", "point-placement"):
                assert generation > report["checks"][-1]["generation"]
                assert initial is not None
                assert float(np.abs(actual - initial).mean()) > 0.005
            if label == "cold":
                initial = actual.copy()
            np.save(args.out / (label + ".npy"), actual)
            report["checks"].append(
                dict(
                    label=label,
                    generation=generation,
                    fresh_reference_rmse=error,
                    support_pixels=int((np.max(actual[:, :, :3], axis=2) > 0.02).sum()),
                    native_stats=stats,
                    renderer_status=status,
                )
            )
            previous = snapshot
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    ch._native.close(engine)
    ch._native.close(capture_handle)
    (args.out / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
