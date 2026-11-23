# SPDX-License-Identifier: Apache-2.0
"""Measure dense material-motion acquisition with independent surface/height checks."""

import argparse
import json
import os
import time
import traceback

import hou
import numpy as np
import cycles_houdini as ch
from stylesheet_storage_fixture import Fixture


p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
os.environ["CYCLES_HOUDINI_PROFILE_PACKED_ASSEMBLY"] = "1"
f = Fixture(device)
general = os.environ.get("CYCLES_HOUDINI_MATERIAL_PROFILE_BINDINGS") == "1"
f.enable_material_motion(general_bindings=general)
destination = f.dest / ("material-motion-general-acquisition" if general else "material-motion-acquisition")
destination.mkdir(parents=True, exist_ok=True)
report = dict(result="running", device=device, checks=[], scope=__doc__)

try:
    with ch.Session(
        device=device,
        width=f.width,
        height=f.height,
        samples=1,
        threads=2,
        execution="worker",
        use_texture_cache=False,
        collect_diagnostics=True,
    ) as session:
        session.wait(session.apply(f.update()), timeout=180)
        cooks = f.prototype.cookCount()
        g = f.prototype.geometry()
        last = int(g.intrinsicValue("primitivecount")) // 2 - 1

        def center(prim):
            return np.mean([tuple(v.point().position()) for v in prim.vertices()], axis=0)

        start, opposite = center(g.prim(0)), center(g.prim(last + 1))
        axis = int(np.argmax(np.abs(start - opposite)))
        boundary = g.boundingBox().center()[axis]
        target = np.asarray(tuple(f.targets.geometry().points()[0].position() * f.obj.worldTransform()))
        normal = np.asarray(tuple(g.prim(0).normal()))
        for displaced in (False, True):
            if displaced:
                height = f.material.createNode("cycles::displacement::1", "profile_height")
                height.setParms(dict(height=0.02, scale=1, midlevel=0, space="object"))
                f.material.node("surface_output").setNamedInput("displacement", height, "displacement")
                f.material.parm("cycles_displacement_method").set("true")
            for index, offset in enumerate((0.0, 0.08)):
                rule = dict(
                    target=dict(group="0", subTarget=dict(group=f"0-{last}")),
                    overrides=dict(materialParameters=dict(tint=[0.9, 0.9, 0.2 + offset])),
                )
                f.obj.parm("shop_materialstylesheet").set(json.dumps(dict(styles=[rule])))
                began = time.perf_counter()
                update = f.update()
                capture_seconds = time.perf_counter() - began
                began = time.perf_counter()
                frame = session.wait(session.apply(update), timeout=240)
                status = session.status()
                image = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).reshape(f.height, f.width, 4)[
                    :, :, :3
                ]
                data = frame["planes"]["position"]
                position = np.frombuffer(data["pixels"], np.float32).reshape(f.height, f.width, data["channels"])[
                    :, :, :3
                ]
                assert frame["complete"] and np.isfinite(image).all() and not status["error"], status
                assert not status["source_diagnostics"] and not status["motion_warnings"], status
                assert f.prototype.cookCount() == cooks, (cooks, f.prototype.cookCount())
                inside = (np.abs(position[:, :, 0] - target[0]) < 0.8) & (np.abs(position[:, :, 1] - target[1]) < 0.7)
                local = position[:, :, axis] - target[axis] - boundary
                tagged = inside & (local < -0.03 if start[axis] < boundary else local > 0.03)
                plain = inside & (local > 0.03 if start[axis] < boundary else local < -0.03)
                assert tagged.sum() > 20 and plain.sum() > 20, (tagged.sum(), plain.sum())
                np.testing.assert_allclose(
                    image[tagged], np.broadcast_to((0.9, 0.9, 0.2 + offset), (tagged.sum(), 3)), atol=3e-4, rtol=0
                )
                np.testing.assert_allclose(
                    image[plain], np.broadcast_to(f.colors[0], (plain.sum(), 3)), atol=3e-4, rtol=0
                )
                expected_z = target[2] + (normal[2] * 0.02 if displaced else 0.0)
                np.testing.assert_allclose(position[:, :, 2][inside], expected_z, atol=3e-4, rtol=0)
                report["checks"].append(
                    dict(
                        name=("Displaced" if displaced else "Surface") + f" face style {index}",
                        capture_seconds=capture_seconds,
                        render_apply_seconds=time.perf_counter() - began,
                        status=status,
                        diagnostics=session.diagnostics(),
                    )
                )
                capture = report["checks"][-1]["diagnostics"]["capture"]["events"][-1]
                assert capture["preparation_yields"] >= 2, capture
                assert capture["maximum_preparation_seconds"] < capture["preparation_seconds"] * 0.5, capture
        session.close(wait=True)
        closed = session.status()
        assert (
            closed["state"] == "closed" and closed["owned_geometry_bytes"] == closed["device_allocation_bytes"] == 0
        ), closed
        report.update(
            result="passed", closed=closed, prototype_cooks=cooks, prototype_triangles=1048576, general_bindings=general
        )
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    (destination / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
