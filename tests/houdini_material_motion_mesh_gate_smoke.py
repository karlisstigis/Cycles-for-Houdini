# SPDX-License-Identifier: Apache-2.0
"""Packed curve-only displacement probe gate, rewires and nominal material edits."""

import argparse, json, traceback
from pathlib import Path
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene
from stylesheet_storage_fixture import Fixture

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
args = p.parse_args()
f = Fixture(args.device, heavy=False)
f.enable_material_motion(displacement=True)
line = f.obj.createNode("line", "curve_prototype")
line.parmTuple("origin").set((0, -0.8, 0))
line.parm("dist").set(1.6)
mixed = f.obj.createNode("merge", "mesh_and_curve")
mixed.setInput(0, line)
mixed.setInput(1, f.prototype)
switch = f.obj.createNode("switch", "prototype_kind")
for i, node in enumerate((line, f.prototype, mixed)):
    switch.setInput(i, node)
animated = f.obj.createNode("attribwrangle", "animated_prototype")
animated.setInput(0, switch)
animated.parm("snippet").set("@P.x+=0.001*sin(@Time);")
f.copies.setInput(0, animated)
nested = f.obj.createNode("pack", "nested_copies")
nested.setInput(0, f.copies)
nested.setDisplayFlag(True)
nested.setRenderFlag(True)
f.copies.setDisplayFlag(True)
f.copies.setRenderFlag(True)
shader = f.material.node("surface_shader")
shader.parm("strength").setExpression("$FF/10", hou.exprLanguage.Hscript)
report = dict(result="running", device=args.device, rows=[])
destination = f.dest / "material-motion-mesh-gate"
destination.mkdir(parents=True, exist_ok=True)

try:
    with ch.Session(
        device=args.device,
        width=f.width,
        height=f.height,
        samples=1,
        threads=2,
        execution="worker",
        use_texture_cache=False,
        collect_diagnostics=True,
    ) as session:

        def capture(label, frame, mesh, colors):
            hou.setFrame(frame)
            update = f.update()
            for request in update["geometry"]:
                request["curve_width"] = 0.2
            image = session.wait(session.apply(update, at_time=hou.time()), timeout=90)
            status = session.status()
            assert image["complete"] and not status["error"], status
            # Assigning the displaced test material to curves preserves the
            # existing, independent unsupported-binding diagnostic.
            expected_codes = {"material.geometry_binding"} if not mesh or "Mixed" in label else set()
            assert {issue["code"] for issue in status["source_diagnostics"]} == expected_codes, status
            assert not status["motion_warnings"], status
            assert status["material_motion_samples"] == (3 if mesh else 0), status
            if not mesh:
                assert status["material_motion_cooks"] == status["material_motion_cache_hits"] == 0, status
            plane = image["planes"]["combined"]
            rgb = np.frombuffer(plane["pixels"], np.float32).reshape(f.height, f.width, 4)[:, :, :3]
            assert np.isfinite(rgb).all()
            for color in colors:
                expected = np.asarray(color) * frame / 10
                count = int((np.max(np.abs(rgb - expected), axis=2) < 3e-4).sum())
                assert count >= 4, (label, expected.tolist(), count, rgb.max(axis=(0, 1)).tolist())
            report["rows"].append(dict(label=label, frame=frame, mesh=mesh, status=status))

        f.styles("colors")
        capture("Animated curves with per-instance styles", 1, False, f.colors)
        capture("Repeated curve capture", 1, False, f.colors)
        capture("Time dependent surface", 2, False, f.colors)
        f.styles("colors", offset=0.03)
        capture("Style edit on curves", 2, False, [tuple(np.asarray(c) + (0.03, 0, 0)) for c in f.colors])
        switch.parm("input").set(1)
        f.styles("uniform")
        nested.setDisplayFlag(True)
        nested.setRenderFlag(True)
        capture("Rewire to packed mesh descendants", 2, True, [f.colors[0]])
        switch.parm("input").set(2)
        capture("Mixed mesh and curve prototype", 2, True, [f.colors[0]])
        switch.parm("input").setExpression("floor($FF+0.5)%2", hou.exprLanguage.Hscript)
        capture("Animated kind becomes curve", 4, False, [f.colors[0]])
        capture("Animated kind becomes mesh", 5, True, [f.colors[0]])
        switch.parm("input").deleteAllKeyframes()
        switch.parm("input").set(0)
        # Color is promoted; edit the exported parameter used by the graph.
        f.material.parmTuple("tint").set((0.3, 0.5, 0.7))
        capture("Return to curves and edit material", 5, False, [(0.3, 0.5, 0.7)])
        f.camera.parm("tx").set(0.1)
        header = native_scene.capture(f.rop, camera_only=True)
        assert "geometry" not in header
        image = session.wait(session.apply(header, at_time=hou.time()), timeout=90)
        status = session.status()
        assert image["complete"] and not status["error"], status
        assert status["packed_assembly"]["seconds"].get("host_material_motion", 0) == 0, status
        report["rows"].append(dict(label="Real camera-only header bypasses material acquisition", status=status))
        session.close(wait=True)
        report["closed"] = session.status()
        assert report["closed"]["owned_geometry_bytes"] == report["closed"]["device_allocation_bytes"] == 0
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    (destination / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result=report["result"], checks=len(report["rows"]))))
