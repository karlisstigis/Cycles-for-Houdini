# SPDX-License-Identifier: Apache-2.0
"""Render an over-budget live Blender asset and verify its visible diagnostic.

Run with CYCLES_HOUDINI_BLEND_CACHE_MB=1 and a dense Blender mesh named Dense.
Also renders an ordinary Houdini box through the same scene path first.
"""

import argparse
import json
from pathlib import Path

import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import blender_assets, native_scene
from cycles_houdini.device_selection import set_devices


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--source", type=Path, required=True)
parser.add_argument("--out", type=Path, required=True)
args = parser.parse_args()
args.out.mkdir(parents=True, exist_ok=True)
source = args.source.resolve(strict=True)
root = hou.node("/obj")
obj = root.createNode("geo", "memory_points", run_init_scripts=False)
box = obj.createNode("box")
box.setDisplayFlag(True)
box.setRenderFlag(True)
camera = root.createNode("cam")
camera.parm("tz").set(5)
rop = hou.node("/out").createNode("cycles_ipr")
set_devices(rop, "CPU")
rop.setParms(
    dict(
        source_mode="objects",
        objectroot="/obj",
        vobject=obj.name(),
        lights="",
        camera=camera.path(),
        resx=32,
        resy=32,
        samples=4,
        ipr_samples=4,
        use_denoise=0,
        ipr_use_denoise=0,
        adaptive_sampling=0,
        ipr_adaptive_sampling=0,
        transparent_background=1,
        allowmotionblur=0,
        use_material_assignments=1,
        render_threads=2,
    )
)
rop.parmTuple("background").set((0.3, 0.3, 0.3))
report = {}
try:
    with ch.Session(
        device="CPU", width=32, height=32, samples=4, execution="sync", threads=2, collect_diagnostics=True
    ) as session:
        for label in ("ordinary_obj", "over_budget_blender"):
            if label == "over_budget_blender":
                points = obj.createNode("attribwrangle")
                points.parm("class").set(0)
                points.parm("snippet").set(
                    'int p=addpoint(0, {0,0,0}); setpointattrib(0,"instance",p,'
                    + json.dumps(source.as_posix() + "#object=Dense")
                    + ");"
                )
                points.setDisplayFlag(True)
                points.setRenderFlag(True)
            update = native_scene.capture(rop)
            diagnostics = update["source_diagnostics"]
            assert not any("unavailable" in issue["code"] for issue in diagnostics), diagnostics
            session.wait(session.apply(update))
            status = session.status()
            assert not status["error"], status
            frame = session.frame()
            plane = frame["planes"]["combined"]
            pixels = np.frombuffer(plane["pixels"], np.float32).reshape(
                frame["height"], frame["width"], plane["channels"]
            )
            assert np.isfinite(pixels).all() and pixels[:, :, 3].max() > 0.5, dict(
                label=label,
                status=status,
                shape=pixels.shape,
                maximum=pixels.max(axis=(0, 1)).tolist(),
                source_diagnostics=diagnostics,
            )
            session.write_exr(args.out / (label + ".exr"))
            report[label] = dict(diagnostics=diagnostics, foreground_pixels=int((pixels[:, :, 3] > 0.5).sum()))
            if label == "over_budget_blender":
                state = blender_assets.cache_diagnostics()["prototypes"]
                assert state["active_over_budget_bytes"] > 0, state
                assert state["active_bytes"] == state["resident_bytes"], state
                assert state["largest_assets"] and state["largest_assets"][0]["active"], state
                assert any(issue["code"] == "blend.active_cache_over_budget" for issue in diagnostics), diagnostics
                assert len(blender_assets._bundles) == 1
                report["cache"] = state
        report["result"] = "passed"
        (args.out / "report.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report), flush=True)
finally:
    from cycles_houdini import blend_worker

    blend_worker.shutdown_all()
