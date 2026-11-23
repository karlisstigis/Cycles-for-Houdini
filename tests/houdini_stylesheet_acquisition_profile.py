# SPDX-License-Identifier: Apache-2.0
"""Measure native nonuniform stylesheet acquisition separately from rendering."""

import argparse, json, os, time, traceback
from pathlib import Path
import hou, numpy as np
import cycles_houdini as ch
from stylesheet_storage_fixture import Fixture

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
os.environ["CYCLES_HOUDINI_PROFILE_PACKED_ASSEMBLY"] = "1"
f = Fixture(device)
destination = f.dest / "acquisition"
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
        first = session.wait(session.apply(f.update()), timeout=180)
        cooks = f.prototype.cookCount()
        last = int(f.prototype.geometry().intrinsicValue("primitivecount")) // 2 - 1
        g = f.prototype.geometry()
        center = lambda prim: np.mean([tuple(v.point().position()) for v in prim.vertices()], axis=0)
        start, opposite = center(g.prim(0)), center(g.prim(last + 1))
        axis = int(np.argmax(np.abs(start - opposite)))
        boundary = g.boundingBox().center()[axis]
        target = np.asarray(tuple(f.targets.geometry().points()[0].position() * f.obj.worldTransform()))
        for index, offset in enumerate((0.0, 0.08, 0.0)):
            rule = dict(
                target=dict(group="0", subTarget=dict(group=f"0-{last}")),
                overrides=dict(materialParameters=dict(tint=[0.9, 0.9, 0.2 + offset])),
            )
            f.obj.parm("shop_materialstylesheet").set(json.dumps(dict(styles=[rule])))
            began = time.perf_counter()
            update = f.update()
            host = time.perf_counter() - began
            applied = time.perf_counter()
            frame = session.wait(session.apply(update), timeout=180)
            status = session.status()
            pixels = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32)
            assert frame["complete"] and np.isfinite(pixels).all() and not status["error"], status
            assert f.prototype.cookCount() == cooks, (cooks, f.prototype.cookCount())
            rgb = pixels.reshape(f.height, f.width, 4)[:, :, :3].reshape(-1, 3)
            assert np.max(np.abs(rgb - np.asarray((0.9, 0.9, 0.2 + offset))), axis=1).min() < 3e-4
            data = frame["planes"]["position"]
            position = np.frombuffer(data["pixels"], np.float32).reshape(f.height, f.width, data["channels"])[:, :, :3]
            image = rgb.reshape(f.height, f.width, 3)
            inside = (np.abs(position[:, :, 0] - target[0]) < 0.8) & (np.abs(position[:, :, 1] - target[1]) < 0.7)
            local = position[:, :, axis] - target[axis] - boundary
            tagged = inside & (local < -0.03 if start[axis] < boundary else local > 0.03)
            plain = inside & (local > 0.03 if start[axis] < boundary else local < -0.03)
            assert tagged.sum() > 20 and plain.sum() > 20, (tagged.sum(), plain.sum())
            np.testing.assert_allclose(
                image[tagged], np.broadcast_to((0.9, 0.9, 0.2 + offset), (tagged.sum(), 3)), atol=3e-4, rtol=0
            )
            np.testing.assert_allclose(image[plain], np.broadcast_to(f.colors[0], (plain.sum(), 3)), atol=3e-4, rtol=0)
            report["checks"].append(
                dict(
                    name="Nonuniform face style " + str(index),
                    capture_seconds=host,
                    render_apply_seconds=time.perf_counter() - applied,
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
        report.update(result="passed", closed=closed, prototype_cooks=cooks, prototype_triangles=1048576)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    (destination / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
