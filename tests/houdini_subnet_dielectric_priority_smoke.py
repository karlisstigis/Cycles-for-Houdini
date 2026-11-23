# SPDX-License-Identifier: Apache-2.0
"""A subnet instance keeps member priorities, packed overrides and live edits.

The reference is the same three independent OBJ nodes rendered directly. The
bubble is itself a packed primitive. No artist scene is modified by this test.
"""

from support.paths import artifact_label

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path

import hou
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pass_fixtures as pf
import cycles_houdini as ch
from cycles_houdini import materials, native_scene, object_properties, registry
from cycles_houdini.device_selection import set_devices

p = argparse.ArgumentParser()
p.add_argument("--device", default="cpu")
p.add_argument("--out", required=True)
p.add_argument("--samples", type=int, default=64)
p.add_argument("--ipr-samples", type=int, default=1024)
p.add_argument("--expected-host", default="")
a = p.parse_args()
out = Path(a.out)
out.mkdir(parents=True, exist_ok=True)
report = {"device": a.device, "checks": []}


def check(name, **values):
    report["checks"].append({"name": name, **values})
    print("PASS", name, json.dumps(values, default=str), flush=True)


def status_priority(status):
    return (
        status.get("dielectric_priority_objects"),
        status.get("dielectric_priority_min"),
        status.get("dielectric_priority_max"),
    )


def expect_priority(status, expected):
    actual = status_priority(status)
    assert actual == expected, (actual, expected, status.get("source_diagnostics"))
    return actual


try:
    if a.expected_host:
        actual = Path(os.environ.get("CYCLES_HOUDINI_HOST_ROOT", "")).resolve()
        assert actual == Path(a.expected_host).resolve(), (str(actual), a.expected_host)
        check("Loaded the expected installed host", host=str(actual))
    want = a.device.lower()
    devs = [
        d
        for d in ch.devices()
        if d["id"] == a.device
        or (
            d["backend"].upper() == "CPU"
            if want == "cpu"
            else d["backend"].upper() == "OPTIX" and want in d["name"].lower()
        )
    ]
    assert len(devs) == 1, devs
    device = devs[0]
    root = hou.node("/obj")
    assembly = root.createNode("subnet", "glass_assembly")

    def material(name, ior, color):
        mat = materials.create_material(name=name, shader="glass_bsdf")
        # The default this test was written for (new nodes now take Blender's).
        mat.node("surface_shader").setParms(dict(color=(0.8, 0.8, 0.8), distribution="ggx"))
        surface = mat.node("surface_shader")
        next(p for p in surface.parms() if p.name().lower() == "ior").set(ior)
        next(p for p in surface.parmTuples() if p.name().lower() == "color").set(color)
        return mat

    glass_mat = material("test_glass", 1.52, (0.95, 1, 0.95))
    liquid_mat = material("test_liquid", 1.33, (0.9, 0.96, 1))
    bubble_mat = material("test_air", 1.0, (1, 1, 1))

    def member(name, priority, mat):
        node = assembly.createNode("geo", name, run_init_scripts=False)
        assert object_properties.install(node)
        node.parm("cycles_dielectric_priority").set(priority)
        node.parm("shop_materialpath").set(mat.path())
        return node

    def sphere(node, radius):
        s = node.createNode("sphere")
        s.parm("type").set(1)
        s.parmTuple("rad").set((radius, radius, radius))
        s.setRenderFlag(True)
        s.setDisplayFlag(True)
        return s

    glass = member("glass", 3, glass_mat)
    sphere(glass, 1.0)
    liquid = member("liquid", 1, liquid_mat)
    sphere(liquid, 0.78)
    bubbles = member("bubbles", 2, bubble_mat)
    bubble = sphere(bubbles, 0.2)
    points = bubbles.createNode("add")
    points.parm("points").set(1)
    points.parm("usept0").set(1)
    points.parmTuple("pt0").set((0.18, 0.1, 0.4))
    copies = bubbles.createNode("copytopoints::2.0")
    copies.setInput(0, bubble)
    copies.setInput(1, points)
    copies.parm("pack").set(1)
    copies.setRenderFlag(True)
    copies.setDisplayFlag(True)

    def instancer(kind, name, snippet):
        node = root.createNode(kind, name, run_init_scripts=False)
        w = node.createNode("attribwrangle", "points")
        w.parm("class").set(0)
        w.parm("snippet").set(snippet)
        w.setRenderFlag(True)
        w.setDisplayFlag(True)
        return node, w

    one = "int p=addpoint(0,{0,0,0});"
    inst, inst_points = instancer("instance", "glass_instance", one)
    inst.parm("ptinstance").set("fast")
    inst.parm("instancepath").set("../glass_assembly")
    inst.parm("subnet_filter").set("all")
    scatter, scatter_points = instancer(
        "geo", "glass_scatter", one + 'setpointattrib(0,"instance",p,"../glass_assembly");'
    )
    camera = root.createNode("cam", "test_camera")
    camera.parm("tz").set(4)
    rop = hou.node("/out").createNode("cycles_ipr", "subnet_priority_test")
    set_devices(rop, device["id"])
    pf.set_parameters(
        rop,
        dict(
            source_mode="objects",
            objectroot="/obj",
            camera=camera.path(),
            resx=128,
            resy=96,
            samples=a.samples,
            ipr_samples=a.samples,
            adaptive_sampling=0,
            ipr_adaptive_sampling=0,
            use_denoise=0,
            ipr_use_denoise=0,
            allowmotionblur=0,
            use_material_assignments=1,
            nested_dielectrics=1,
            seed=3,
            animate_seed=0,
            max_bounce=16,
            transmission_bounce=16,
        ),
    )
    rop.parmTuple("background").set((0.8, 0.8, 0.8))

    def select(path, objects):
        rop.parm("objectroot").set(path)
        rop.parm("vobject").set(objects)

    def image(session, label):
        frame = session.wait(session.apply(native_scene.capture(rop)))
        status = session.status()
        plane = frame["planes"]["combined"]
        pixels = np.frombuffer(plane["pixels"], np.float32).reshape(96, 128, plane["channels"]).copy()
        np.save(out / (label + ".npy"), pixels)
        return pixels, status

    with ch.Session(
        device=device["id"], width=128, height=96, samples=a.samples, collect_diagnostics=True, use_texture_cache=False
    ) as session:
        select(assembly.path(), "*")
        reference, st = image(session, "direct")
        expect_priority(st, (3, 1, 3))
        check("Direct glass, liquid and packed bubble priorities", priority=status_priority(st))

        select("/obj", "glass_instance")
        instanced, st = image(session, "instance")
        expect_priority(st, (3, 1, 3))
        delta = np.abs(reference[..., :3] - instanced[..., :3])
        rmse = float(np.sqrt(np.mean(delta * delta)))
        assert rmse < 0.08, rmse
        check(
            "Instance OBJ preserves member priorities and matches direct rendering",
            priority=status_priority(st),
            image_rmse=rmse,
        )

        select("/obj", "glass_scatter")
        _, st = image(session, "point_instance")
        expect_priority(st, (3, 1, 3))
        check("s@instance also preserves member priorities", priority=status_priority(st))

        select("/obj", "glass_instance")
        inst_points.parm("snippet").set(one + 'setpointattrib(0,"cycles_dielectric_priority",p,6);')
        _, st = image(session, "point_override")
        expect_priority(st, (3, 6, 6))
        check("Explicit instance-point priority overrides member defaults", priority=status_priority(st))

        field = bubbles.createNode("attribwrangle", "packed_priority")
        field.setInput(0, copies)
        field.parm("class").set(1)
        field.parm("snippet").set("i@cycles_dielectric_priority=8;")
        field.setRenderFlag(True)
        field.setDisplayFlag(True)
        _, st = image(session, "nested_packed_override")
        expect_priority(st, (3, 6, 8))
        check("A nearer packed bubble override remains effective", priority=status_priority(st))

        copies.setRenderFlag(True)
        copies.setDisplayFlag(True)
        inst_points.parm("snippet").set(one)
        bubbles.parm("cycles_dielectric_priority").set(7)
        _, st = image(session, "member_edit")
        expect_priority(st, (3, 1, 7))
        check("Editing a member priority updates the instance", priority=status_priority(st))

        rop.parm("allowmotionblur").set(1)
        _, st = image(session, "motion")
        expect_priority(st, (3, 1, 7))
        check("Member priorities survive motion-enabled capture", priority=status_priority(st))
        rop.parm("allowmotionblur").set(0)

    # A real worker IPR session: the edit must arrive during accumulation and
    # the next accepted revision must contain the repaired member priority.
    # Viewer mode may use either the final or the interactive sample budget.
    rop.parm("samples").set(a.ipr_samples)
    rop.parm("ipr_samples").set(a.ipr_samples)
    config = native_scene.configuration(rop)
    # The normal viewer disables optional engine counters; enable them in this
    # otherwise identical worker session so the effective priorities are observable.
    entry = registry.Entry(
        key="subnet-priority-test",
        config=config,
        session=registry.session_from_configuration(config, collect_diagnostics=True),
        rop=rop,
    )
    observer_calls = []
    native_scene.capture(rop)
    observer = native_scene.SceneObserver(rop, lambda: observer_calls.append("scene"))
    try:
        generation = entry.apply(native_scene.capture(rop), hou.time())
        print("IPR submitted", generation, flush=True)
        deadline = time.monotonic() + 90
        last_progress = 0.0
        while time.monotonic() < deadline:
            st = entry.session.status()
            if st["state"] == "failed":
                raise RuntimeError(st["error"])
            n = st.get("current_sample", 0)
            if st["state"] == "rendering" and 0 < n < a.ipr_samples and not st.get("pending_update"):
                break
            if time.monotonic() - last_progress > 10:
                print(
                    "IPR waiting:",
                    {
                        k: st.get(k)
                        for k in (
                            "state",
                            "current_sample",
                            "pending_update",
                            "capture_pending",
                            "render_revision",
                            "generation",
                        )
                    },
                    flush=True,
                )
                last_progress = time.monotonic()
            time.sleep(0.005)
        else:
            raise TimeoutError("IPR did not reach partial accumulation")
        before = st.get("render_revision", st["generation"])
        observer_calls.clear()
        bubbles.parm("cycles_dielectric_priority").set(9)
        assert observer_calls, "Member priority edit did not notify the scene observer"
        print("IPR edit at sample", n, "revision", before, flush=True)
        generation = entry.apply(native_scene.capture(rop), hou.time())
        deadline = time.monotonic() + 90
        last_progress = 0.0
        while time.monotonic() < deadline:
            st = entry.session.status()
            if st["state"] == "failed":
                raise RuntimeError(st["error"])
            rev = st.get("render_revision", st["generation"])
            if rev > before and status_priority(st) == (3, 1, 9):
                break
            if time.monotonic() - last_progress > 10:
                print(
                    "IPR edit waiting:",
                    {
                        k: st.get(k)
                        for k in (
                            "state",
                            "current_sample",
                            "pending_update",
                            "render_revision",
                            "generation",
                            "dielectric_priority_max",
                        )
                    },
                    flush=True,
                )
                last_progress = time.monotonic()
            time.sleep(0.005)
        else:
            raise TimeoutError("Edited priority was not applied to the IPR engine")
        check(
            "Member priority edit applied during IPR accumulation",
            sample_at_edit=n,
            revision_before=before,
            revision_after=rev,
            priority=status_priority(st),
        )
    finally:
        observer.close()
        entry.session.close(wait=True)
    report["result"] = "passed"
except Exception:
    report["result"] = "failed"
    report["error"] = traceback.format_exc()
    print(report["error"], flush=True)
finally:
    registry.close_all()
    (out / ("subnet-priority-" + artifact_label(a.device) + ".json")).write_text(json.dumps(report, indent=2))
print("RESULT", report["result"], flush=True)
hou.exit(0 if report["result"] == "passed" else 1)
