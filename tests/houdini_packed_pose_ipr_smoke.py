# SPDX-License-Identifier: Apache-2.0
"""Artist edits packed leaf poses/properties/layout in one real Cycles viewer."""

import pass_fixtures as pass_fixture

from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import os
from pathlib import Path
import time
import traceback
import hou
import numpy as np
from PySide6 import QtCore, QtTest
import cycles_houdini as ch
from cycles_houdini import native_scene, object_properties, registry
from cycles_houdini.panel import RenderPanel

os.environ["CYCLES_HOUDINI_PROFILE_WORKER"] = "1"
path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
device = os.environ["CYCLES_HOUDINI_GUI_DEVICE"]
started = time.monotonic()
report = dict(result="running", device=device, checks=[], render_viewers=["cycles"], profiling=True)
geo = hou.node("/obj").createNode("geo", "pose_components", run_init_scripts=False)
object_properties.install(geo)
pig = geo.createNode("testgeometry_pighead")
curve = geo.createNode("line")
pass_fixture.parm(curve, "dist").set(0.8)
hair = geo.createNode("attribwrangle")
hair.setInput(0, curve)
pass_fixture.parm(hair, "snippet").set("f@width=.08; @P.x+=1;")
point = geo.createNode("add")
pass_fixture.set_parameters(point, dict(points=1, usept0=1, pt0x=-1))
radius = geo.createNode("attribwrangle")
radius.setInput(0, point)
pass_fixture.parm(radius, "snippet").set("f@pscale=.12;")
merge = geo.createNode("merge")
merge.setInput(0, pig)
merge.setInput(1, hair)
merge.setInput(2, radius)
prototype = geo.createNode("xform", "editable_prototype")
prototype.setInput(0, merge)
pass_fixture.parm(prototype, "scale").set(0.18)
targets = geo.createNode("grid", "placements")
pass_fixture.set_parameters(targets, dict(rows=5, cols=5, orient=0))
targets.parmTuple("size").set((4, 3))
move = geo.createNode("attribwrangle", "move_instances")
move.setInput(0, targets)
move.addSpareParmTuple(hou.FloatParmTemplate("offset", "Offset", 1, default_value=(0,)))
pass_fixture.parm(move, "snippet").set('@P.x+=chf("offset")*sin(@ptnum*.37); @P.y+=chf("offset")*cos(@ptnum*.19);')
copies = geo.createNode("copytopoints", "mixed_packed")
copies.setInput(0, prototype)
copies.setInput(1, move)
pass_fixture.parm(copies, "pack").set(1)
copies.setDisplayFlag(True)
copies.setRenderFlag(True)
camera = hou.node("/obj").createNode("cam", "pose_camera")
pass_fixture.set_parameters(camera, dict(tz=10, projection=1, orthowidth=6))
rop = hou.node("/out").createNode("cycles_ipr", "pose_render")
set_rop_devices(rop, device)
pass_fixture.set_parameters(
    rop,
    dict(
        source_mode="sop",
        soppath=copies.path(),
        camera=camera.path(),
        resx=320,
        resy=180,
        samples=131072,
        ipr_samples=131072,
        adaptive_sampling=0,
        ipr_adaptive_sampling=0,
        use_denoise=0,
        ipr_use_denoise=0,
        use_material_assignments=0,
        material_kind="emission",
        emission_strength=1,
        allowmotionblur=0,
        pass_depth=1,
        pass_object_id=1,
        crypto_object=1,
    ),
)
rop.parmTuple("basecolor").set((0.8, 0.2, 0.1))
panel = RenderPanel()
panel.resize(1000, 720)
panel.set_rop(rop)
panel.show()
panel.auto.setChecked(True)
timer = QtCore.QTimer(hou.qt.mainWindow())
timer.setInterval(10)
stage = "start"
before = 0
session = None
generations = {}
last_tick = time.monotonic()
gaps = []


def finish(error=None):
    timer.stop()
    panel.dispose()
    panel.close()
    registry.close_all()
    report.update(
        result="failed" if error else "passed",
        error=error,
        wall_seconds=time.monotonic() - started,
        maximum_qt_gap_seconds=max(gaps, default=0),
        stage=stage,
    )
    path.write_text(json.dumps(report, indent=2))
    hou.ui.postEventCallback(lambda: hou.exit(1 if error else 0, suppress_save_prompt=True))


def change(next_stage, status, action):
    global stage, before
    before = status["generation"]
    action()
    stage = next_stage


def pixels(frame, name):
    value = frame["planes"][name]
    return np.frombuffer(value["pixels"], np.float32).reshape(frame["height"], frame["width"], value["channels"])


def tick():
    global stage, session, base, components, pid, baseline_pixels, actual_pixels, diagnostics, last_tick
    now = time.monotonic()
    gaps.append(now - last_tick)
    last_tick = now
    try:
        if now - started > 135:
            raise TimeoutError("Packed pose workflow stalled at " + stage)
        if stage == "start":
            QtTest.QTest.mouseClick(panel.render_button, QtCore.Qt.LeftButton)
            stage = "initial"
            return
        if stage == "retire":
            if session.status()["state"] != "closed":
                return
            worker = Path(diagnostics["controller"]["worker_log"])
            profiles = {
                row["request"]: row
                for line in worker.read_text().splitlines()
                if line.startswith("CH_PROFILE ")
                for row in [json.loads(line[len("CH_PROFILE ") :])]
            }
            events = diagnostics["controller"]["events"]

            def requests(generation):
                return [
                    profiles[e["request_profile"]["request"]]
                    for e in events
                    if e["submission"] == generation
                    and e["accepted"]
                    and e.get("request_profile", {}).get("request") in profiles
                ]

            leaf = requests(generations["leaves"])
            prop = requests(generations["properties"])
            assert any(r["object_records"] == 0 and r["object_poses"] >= base["object_resources"] for r in leaf), leaf
            assert any(r["object_records"] >= base["object_resources"] for r in prop), prop
            report["pose_requests"] = leaf
            report["property_requests"] = prop
            # A fresh host capture, after the worker closes, is independent of
            # its incremental snapshots. No second viewer or engine overlaps it.
            with ch.Session(
                device=device, width=320, height=180, samples=16, interactive=False, execution="sync"
            ) as reference:
                expected = reference.wait(reference.apply(native_scene.capture(rop), at_time=hou.time()))
                difference = float(np.abs(actual_pixels - pixels(expected, "combined")).mean())
                assert difference < 0.002, difference
            report["fresh_reference_mean_error"] = difference
            report["checks"].append("Final mixed packed scene matches an independent fresh capture")
            finish()
            return
        entry = panel.entry
        if entry is None:
            return
        session = entry.session
        status = session.status()
        info = session.frame_info()
        if status["state"] == "failed" or entry.error:
            raise RuntimeError(entry.error or status["error"])
        if not info or info["generation"] != status["generation"] or panel.canvas.generation != status["generation"]:
            return
        if status.get("capture_pending") or status.get("pending_update") or panel._sequence < info["sequence"]:
            return
        if stage != "initial" and status["generation"] <= before:
            return
        if stage == "initial":
            base = dict(status)
            pid = status["renderer_pid"]
            components = status["object_resources"] // 25
            assert components >= 3 and status["object_resources"] == 25 * components
            baseline_pixels = pixels(session.frame(), "combined").copy()
            report["fixture"] = dict(
                instances=25, components_per_instance=components, source_geometry=status["geometry_resources"]
            )
            change("leaves", status, lambda: pass_fixture.parm(move, "offset").set(0.2))
            return
        assert status["renderer_pid"] == pid, "Artist edit replaced the persistent worker"
        if stage == "leaves":
            assert status["geometry_conversions"] == base["geometry_conversions"]
            assert float(np.abs(pixels(session.frame(), "combined") - baseline_pixels).mean()) > 0.001
            generations[stage] = status["generation"]
            report["checks"].append(
                "Packed mesh/hair/point leaf motion uses shared prototypes and changes the actual image"
            )
            change("properties", status, lambda: pass_fixture.parm(geo, "cycles_pass_index").set(7))
            panel.pass_menu.setCurrentIndex(panel.pass_menu.findData("object_id"))
            return
        if stage == "properties":
            if "object_id" not in info["passes"]:
                return
            assert np.any(pixels(session.frame(), "object_id") == 7)
            assert status["geometry_conversions"] == base["geometry_conversions"]
            generations[stage] = status["generation"]
            report["checks"].append("Object property edits use full records and reach the real object-index pass")
            panel.pass_menu.setCurrentIndex(panel.pass_menu.findData("combined"))
            change("grow", status, lambda: pass_fixture.parm(targets, "cols").set(6))
            return
        if stage in ("grow", "shrink"):
            count = 30 if stage == "grow" else 25
            assert status["object_resources"] == count * components
            assert status["geometry_conversions"] == base["geometry_conversions"]
            report["checks"].append("Packed layout " + stage + " updates membership without reconverting the prototype")
            if stage == "grow":
                change("shrink", status, lambda: pass_fixture.parm(targets, "cols").set(5))
            else:
                change("prototype", status, lambda: pass_fixture.parm(prototype, "scale").set(0.22))
            return
        if stage == "prototype":
            assert (
                status["object_resources"] == 25 * components
                and status["geometry_resources"] == base["geometry_resources"]
            )
            assert status["geometry_conversions"] == base["geometry_conversions"] + base["geometry_resources"]
            report["checks"].append("Changed prototype components convert once each and remain shared")
            change("complete", status, lambda: pass_fixture.set_parameters(rop, dict(samples=16, ipr_samples=16)))
            return
        if stage == "complete":
            if not info["complete"]:
                return
            actual_pixels = pixels(session.frame(), "combined").copy()
            diagnostics = session.diagnostics()
            assert diagnostics["session_cancel_calls"] == 0
            panel.grab().save(str(path.with_suffix(".panel.png")))
            report["final_status"] = status
            report["diagnostics"] = diagnostics
            panel.dispose()
            panel.close()
            registry.close_all()
            stage = "retire"
    except BaseException:
        finish(traceback.format_exc())


timer.timeout.connect(tick)
timer.start()
