# SPDX-License-Identifier: Apache-2.0
"""Reproduce a 51,294-point, four-member Blender collection load in actual Houdini.

Run using scripts/run-hython.ps1 with a pinned 22.0.466 build/package and isolated
preferences. Supply --hip and --blend; private artist inputs are not in the repo.
The HIP must contain /obj/geo1/{scatter1,pointwrangle1} and /out/cycles_render.
Changes are in memory only. The original scatter seed, scene lighting, materials,
camera, and 0.2 instance scale are retained; its animated count is set explicitly.
Use --count 4 for native member-transform assertions before a dense trial.
One sample, 216x122, four CPU threads, no denoising, fixed frame and index 1.
--device OPTIX selects the RTX 4090. External callers should bound time and RSS.
"""

import argparse
import hashlib
import json
import sys
import time
import traceback
from pathlib import Path

import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene, instancing, _native
from cycles_houdini.device_selection import set_devices

ap = argparse.ArgumentParser()
ap.add_argument("--out", required=True, type=Path)
ap.add_argument("--fixture", type=Path)
ap.add_argument("--hip", type=Path)
ap.add_argument("--blend", type=Path)
ap.add_argument("--collection", default="wine_bottles_01")
ap.add_argument("--host", required=True, type=Path)
ap.add_argument("--count", type=int, default=51294)
ap.add_argument("--device", choices=("CPU", "OPTIX"), default="CPU")
ap.add_argument("--execution", choices=("worker", "sync"), default="worker")
ap.add_argument("--structural", action="store_true", help="Untimed depth/position/normal parity control")
a = ap.parse_args()
if a.fixture is None and (a.hip is None or a.blend is None):
    ap.error("Supply --hip and --blend (or --fixture for a copied fixture directory)")
a.out.mkdir(parents=True, exist_ok=True)
hip = a.hip or a.fixture / "Blender_Instancing.hiplc"
blend = a.blend or a.fixture / "assets/Instance_Source.blend"
assert Path(ch.__file__).resolve().is_relative_to(a.host.resolve()), ch.__file__
report = dict(
    host=ch.__file__,
    houdini=hou.applicationVersionString(),
    count=a.count,
    device=a.device,
    execution=a.execution,
    rows=[],
    prototypes=[],
    checks=[],
    inputs=dict(
        hip=str(hip.resolve()),
        blend=str(blend.resolve()),
        hip_sha256=hashlib.sha256(hip.read_bytes()).hexdigest(),
        blend_sha256=hashlib.sha256(blend.read_bytes()).hexdigest(),
        collection=a.collection,
        frame=1,
        render_seed=0,
    ),
)


def save():
    (a.out / "result.json").write_text(json.dumps(report, indent=2, default=str))


def phase(name, **values):
    print("PHASE", name, json.dumps(values, default=str), flush=True)
    report["last_phase"] = name
    report["memory"] = ch.process_memory()
    save()


start = time.perf_counter()
try:
    try:
        hou.hipFile.load(str(hip), suppress_save_prompt=True)
    except hou.LoadWarning:
        pass
    hou.setFrame(1)
    w = hou.node("/obj/geo1/pointwrangle1")
    rop = hou.node("/out/cycles_render")
    ref = blend.resolve().as_posix() + "#collection=" + a.collection
    w.parm("snippet").set("s@instance=" + json.dumps(ref) + "; f@index=1; matrix3 m=ident(); 3@transform=m*.2;")
    scatter = hou.node("/obj/geo1/scatter1")
    report["inputs"]["scatter_seed"] = scatter.parm("seed").eval()
    for name, value in (("forcetotal", 1), ("npts", a.count)):
        scatter.parm(name).deleteAllKeyframes()
        scatter.parm(name).set(value)
    sop = w
    sop.setDisplayFlag(True)
    sop.setRenderFlag(True)
    t = time.perf_counter()
    sop.cook(force=True)
    cook = time.perf_counter() - t
    points = len(sop.geometry().points())
    report.update(point_count=points, sop_cook_seconds=cook)
    report["inputs"]["point_positions_sha256"] = hashlib.sha256(
        np.asarray(sop.geometry().pointFloatAttribValues("P"), dtype=np.float32).tobytes()
    ).hexdigest()
    report["inputs"]["wrangle"] = w.parm("snippet").eval()
    assert points == a.count, (points, a.count)
    index = sop.geometry().pointFloatAttribValues("index")
    assert set(index) == {1.0}, set(index)
    device = (
        "CPU"
        if a.device == "CPU"
        else next((x["id"] for x in ch.devices() if x["backend"] == "OPTIX" and "4090" in x["name"]))
    )
    set_devices(rop, device)
    rop.setParms(
        dict(
            source_mode="objects",
            objectroot="/obj",
            vobject="/obj/geo1",
            forceobject="",
            samples=1,
            ipr_samples=1,
            use_denoise=0,
            ipr_use_denoise=0,
            resx=216,
            resy=122,
            adaptive_sampling=0,
            ipr_adaptive_sampling=0,
            allowmotionblur=0,
            render_threads=4,
            seed=0,
            animate_seed=0,
        )
    )
    if a.structural:
        from cycles_houdini.rop import passes

        for kind in ("depth", "position", "normal"):
            passes.add(rop, kind)
    phase("cooked", points=points, seconds=cook, memory=ch.process_memory())
    original_request = instancing.request

    def request(*args, **kwargs):
        t = time.perf_counter()
        v = original_request(*args, **kwargs)
        p = v.get("point_instancing", {}).get("prototypes", [])
        report["prototypes"].append(
            dict(
                object_key=v.get("object_key"),
                geometry_key=v.get("geometry_key"),
                seconds=time.perf_counter() - t,
                unique_prototype_frames=len(p),
                frames=[x.get("source_frame") for x in p],
                member_counts=[len(x.get("direct_instances", [])) for x in p],
                member_geometry_keys=[[m.get("geometry_key") for m in x.get("direct_instances", [])] for x in p],
            )
        )
        if a.count <= 4:
            table = _native.point_instance_table(v, hou.time())
            assert table["count"] == a.count * 4, table
            members = p[0]["direct_instances"]
            for i, point in enumerate(sop.geometry().points()):
                rows = [row for row in table["instances"] if row["point"] == i]
                assert len(rows) == len(members)
                point_matrix = np.diag([0.2, 0.2, 0.2, 1.0])
                point_matrix[3, :3] = point.position()
                for row, member in zip(rows, members):
                    expected = np.asarray(member["transform"]).reshape(4, 4).T @ point_matrix
                    assert np.allclose(np.asarray(row["matrix"]).reshape(4, 4), expected, rtol=1e-05, atol=1e-05), (
                        row,
                        expected.tolist(),
                    )
            report["tiny_table"] = table
        return v

    instancing.request = request
    with ch.Session(
        device=device,
        width=216,
        height=122,
        samples=1,
        interactive=False,
        execution=a.execution,
        threads=4,
        collect_diagnostics=True,
    ) as session:
        for label in ("cold", "unchanged"):
            t = time.perf_counter()
            update = native_scene.capture(rop)
            report["sampling"] = {
                key: update.get(key) for key in ("rendering", "camera_path", "camera_sampling", "crop")
            }
            captured = time.perf_counter()
            phase(label + "-captured", capture_seconds=captured - t, prototypes=report["prototypes"][-1:])
            generation = session.apply(update)
            applied = time.perf_counter()
            phase(label + "-applied", apply_seconds=applied - captured)
            first_seen = []
            original_frame = session.frame

            def observe_frame(*args, **kwargs):
                image = original_frame(*args, **kwargs)
                if image and image["generation"] == generation and image.get("current_scene", True) and not first_seen:
                    first_seen.append(time.perf_counter())
                return image

            session.frame = observe_frame
            try:
                frame = session.wait(generation, timeout=150)
            finally:
                session.frame = original_frame
            finish = time.perf_counter()
            st = session.status()
            diag = session.diagnostics()
            pixels = np.frombuffer(frame["planes"]["combined"]["pixels"], dtype=np.float32)
            assert frame["complete"] and np.isfinite(pixels).all() and (pixels.std() > 0.01)
            assert not st["error"], st
            assert st["object_resources"] == a.count * 4, st["object_resources"]
            assert st["geometry_resources"] == 4, st["geometry_resources"]
            assert st["geometry_conversions"] == 4, st["geometry_conversions"]
            assert not any(
                ("unavailable" in d["code"] or d.get("severity") == "error" for d in st.get("source_diagnostics", []))
            ), st.get("source_diagnostics")
            assert report["prototypes"][-1]["unique_prototype_frames"] == 1
            assert report["prototypes"][-1]["frames"] == [1.0]
            assert report["prototypes"][-1]["member_counts"] == [4]
            row = dict(
                label=label,
                capture_seconds=captured - t,
                apply_seconds=applied - captured,
                render_wait_seconds=finish - applied,
                first_complete_seconds=finish - t,
                first_delivered_image_seconds=first_seen[0] - t if first_seen else None,
                native_first_image_seconds=st.get("first_image_seconds"),
                object_flags_seconds=diag.get("engine_update_seconds", {}).get("object/device_update_flags"),
                status=st,
                diagnostics=diag,
                image_sha256=hashlib.sha256(pixels).hexdigest(),
                pixel_std=float(pixels.std()),
                memory=ch.process_memory(),
                width=frame.get("width"),
                height=frame.get("height"),
            )
            report["rows"].append(row)
            np.save(a.out / (label + ".npy"), pixels)
            if a.structural:
                for kind in ("depth", "position", "normal"):
                    plane = frame["planes"][kind]
                    np.save(a.out / (label + "-" + kind + ".npy"), np.frombuffer(plane["pixels"], dtype=np.float32))
            phase(
                label + "-complete",
                capture=row["capture_seconds"],
                apply=row["apply_seconds"],
                render=row["render_wait_seconds"],
                objects=st.get("object_resources"),
                geometries=st.get("geometry_resources"),
                memory=row["memory"],
            )
    first = np.load(a.out / "cold.npy")
    repeated = np.load(a.out / "unchanged.npy")
    report["repeat_image_max_error"] = float(np.max(np.abs(first - repeated)))
    report["repeat_image_rmse"] = float(np.sqrt(np.mean((first - repeated) ** 2)))
    assert np.allclose(first, repeated, rtol=1e-6, atol=1e-6), report["repeat_image_max_error"]
    report["result"] = "passed"
    report["total_seconds"] = time.perf_counter() - start
except BaseException:
    report["result"] = "error"
    report["error"] = traceback.format_exc()
    print(report["error"], flush=True)
finally:
    save()
print("RESULT", report["result"], flush=True)
sys.exit(0 if report["result"] == "passed" else 1)
