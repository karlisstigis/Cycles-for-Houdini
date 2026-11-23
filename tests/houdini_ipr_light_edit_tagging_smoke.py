# SPDX-License-Identifier: Apache-2.0
"""Headless: each kind of light edit gives the same image as a fresh session, and an
intensity or colour edit does not run the engine's geometry/object update.

Scene built in memory, never saved. Counter: the engine update statistics of the
edited generation (collect_diagnostics) contain no geometry/ or object/ stage.
"""

import json
import sys
import time

import hou
import numpy as np
from cycles_houdini import native_scene, registry
from cycles_houdini.core import device_selection
from cycles_houdini.scene import camera_properties, light_properties, object_properties

device = sys.argv[sys.argv.index("--device") + 1] if "--device" in sys.argv else "CPU"
_session = registry.Session
registry.Session = lambda **kw: _session(collect_diagnostics=True, **kw)


def menu_pick(parm, *words):
    for item, label in zip(parm.parmTemplate().menuItems(), parm.parmTemplate().menuLabels()):
        if any(w in item.lower() or w in label.lower() for w in words):
            return item
    raise ValueError(words)


obj = hou.node("/obj")
cam = obj.createNode("cam", "tag_cam")
cam.setParms(dict(ty=2, tz=8, rx=-10, resx=128, resy=72))
camera_properties.install(cam)
area = obj.createNode("hlight::2.0", "tag_area")
area.parm("light_type").set(menu_pick(area.parm("light_type"), "grid", "area"))
area.setParms(dict(tx=-2, ty=4, tz=3, rx=-50, light_intensity=20.0))
light_properties.install(area)
spot = obj.createNode("hlight::2.0", "tag_spot")
spot.parm("light_type").set(menu_pick(spot.parm("light_type"), "point"))
spot.setParms(dict(tx=2, ty=4, tz=3, rx=-50, light_intensity=30.0, coneenable=1))
light_properties.install(spot)
for name, kind in (("tag_ground", "grid"), ("tag_ball", "sphere")):
    geo = obj.createNode("geo", name)
    object_properties.install(geo)
    sop = geo.createNode(kind)
    if kind == "sphere":
        sop.parm("type").set(menu_pick(sop.parm("type"), "polygon"))
        geo.parm("ty").set(1)
    sop.setRenderFlag(True)
rop = hou.node("/out").createNode("cycles_ipr", "tag_rop")
rop.setParms(dict(camera=cam.path(), resx=128, resy=72, ipr_samples=8, ipr_adaptive_sampling=0, ipr_use_denoise=0))
device_selection.set_devices(rop, [device])


def wait_complete(entry, generation, timeout=90):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        status = entry.session.status()
        frame = entry.session.frame_info()
        if status["state"] == "failed":
            raise RuntimeError(status["error"])
        if (
            status["state"] == "complete"
            and frame
            and frame["complete"]
            and frame["generation"] >= generation
            and not status.get("capture_pending")
        ):
            diagnostics = entry.session.diagnostics()
            if not diagnostics.get("engine_statistics_pending"):
                image = np.frombuffer(entry.session.frame()["planes"]["combined"]["pixels"], np.float32).copy()
                return image, dict(diagnostics.get("engine_update_seconds", {}))
        time.sleep(0.01)
    raise TimeoutError("render did not complete")


def apply(entry):
    now = hou.time()
    return entry.apply(native_scene.capture(rop, now), now, reuse_if_unchanged=True)


def fresh_image():
    registry.close_all()
    entry = registry.for_rop(rop, native_scene.configuration(rop))
    image, _ = wait_complete(entry, apply(entry))
    registry.close_all()
    return image


edits = [
    ("intensity", lambda: area.parm("light_intensity").set(35.0), False),
    ("colour", lambda: area.parmTuple("light_color").set((1.0, 0.6, 0.3)), False),
    ("exposure", lambda: spot.parm("light_exposure").set(0.5), False),
    ("size", lambda: area.parmTuple("areasize").set((2.0, 0.5)), None),
    ("transform", lambda: area.parm("tx").set(-1.0), True),
    ("visibility", lambda: area.parm("light_contribprimary").set(1), True),
    ("lightgroup", lambda: area.parm("cycles_lightgroup").set("key"), True),
    ("spread", lambda: area.parm("cycles_light_spread").set(60.0), None),
    ("cone", lambda: spot.parm("coneangle").set(25.0), None),
]
report = dict(device=device, edits=[])
entry = registry.for_rop(rop, native_scene.configuration(rop))
wait_complete(entry, apply(entry))
for name, edit, object_update in edits:
    edit()
    image, stages = wait_complete(entry, apply(entry))
    geometry = sorted(k for k in stages if k.startswith(("geometry/", "object/")))
    expected = fresh_image()
    row = dict(edit=name, geometry_object_stages=geometry, max_abs_difference=float(np.abs(image - expected).max()))
    report["edits"].append(row)
    print("TAGGING " + json.dumps(row), flush=True)
    assert np.array_equal(image, expected), row
    if object_update is False:
        assert not geometry, row  # a pure value edit must not run the geometry/object update
    entry = registry.for_rop(rop, native_scene.configuration(rop))
    wait_complete(entry, apply(entry))
registry.close_all()
print("LIGHT_TAGGING_RESULT " + json.dumps(report), flush=True)
print("PASS", flush=True)
hou.exit(0, suppress_save_prompt=True)
