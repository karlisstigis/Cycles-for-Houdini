# SPDX-License-Identifier: Apache-2.0
"""Headless: a light intensity/colour edit reads only the lights (no scene scan),
gives the same image as a fresh session, and every uncertain case falls back to
the complete read. The debug checker (CYCLES_HOUDINI_NARROW_CHECK=1) finds no
difference. Scene built in memory, never saved.
"""

import json
import os
import sys
import time

os.environ["CYCLES_HOUDINI_NARROW_CHECK"] = "1"
import hou
import numpy as np
from cycles_houdini import native_scene, registry
from cycles_houdini.core import device_selection
from cycles_houdini.scene import camera_properties, light_properties, object_properties

device = sys.argv[sys.argv.index("--device") + 1] if "--device" in sys.argv else "CPU"


def menu_pick(parm, *words):
    for item, label in zip(parm.parmTemplate().menuItems(), parm.parmTemplate().menuLabels()):
        if any(w in item.lower() or w in label.lower() for w in words):
            return item
    raise ValueError(words)


obj = hou.node("/obj")
cam = obj.createNode("cam", "value_cam")
cam.setParms(dict(ty=2, tz=8, rx=-10, resx=128, resy=72))
camera_properties.install(cam)
light = obj.createNode("hlight::2.0", "value_light")
light.parm("light_type").set(menu_pick(light.parm("light_type"), "grid", "area"))
light.setParms(dict(tx=-2, ty=4, tz=3, rx=-50, light_intensity=20.0))
light_properties.install(light)
other = obj.createNode("hlight::2.0", "value_other")
other.parm("light_type").set(menu_pick(other.parm("light_type"), "point"))
other.setParms(dict(tx=2, ty=3, tz=2, light_intensity=5.0))
light_properties.install(other)
geo = obj.createNode("geo", "value_ball")
object_properties.install(geo)
sphere = geo.createNode("sphere")
sphere.parm("type").set(menu_pick(sphere.parm("type"), "polygon"))
sphere.setRenderFlag(True)
rop = hou.node("/out").createNode("cycles_ipr", "value_rop")
rop.setParms(dict(camera=cam.path(), resx=128, resy=72, ipr_samples=8, ipr_adaptive_sampling=0, ipr_use_denoise=0))
device_selection.set_devices(rop, [device])
report = dict(device=device, cases=[])


def wait_complete(entry, generation, timeout=90):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        status = entry.session.status()
        frame = entry.session.frame_info()
        if status["state"] == "failed":
            raise RuntimeError(status["error"])
        if status["state"] == "complete" and frame and frame["complete"] and frame["generation"] >= generation:
            if not status.get("capture_pending"):
                return np.frombuffer(entry.session.frame()["planes"]["combined"]["pixels"], np.float32).copy()
        time.sleep(0.01)
    raise TimeoutError("render did not complete")


def soho_update(entry):
    """soho_adapter._update in update mode, without SOHO's camera object (as the panel path)."""
    now = hou.time()
    motion = native_scene.motion_configuration(rop, now)
    lights = entry.light_value_capture(now, motion)
    if lights is not None:
        update = dict(native_scene.capture(rop, now, camera_only=True), **lights)
        kind = "lights"
    else:
        update = native_scene.capture(rop, now)
        kind = "full"
    scans = len(entry.session._capture_events)
    generation = entry.apply(
        update, now, reuse_if_unchanged=True, change_serial=entry.change_serial if "geometry" not in update else None
    )
    return kind, generation, scans


def fresh_image():
    registry.close_all()
    entry = registry.for_rop(rop, native_scene.configuration(rop))
    now = hou.time()
    image = wait_complete(entry, entry.apply(native_scene.capture(rop, now), now))
    registry.close_all()
    return image


def start():
    entry = registry.for_rop(rop, native_scene.configuration(rop))
    now = hou.time()
    wait_complete(entry, entry.apply(native_scene.capture(rop, now), now))
    return entry


def case(name, edit, expected_kind, undo=None):
    entry = start()
    edit()
    kind, generation, _ = soho_update(entry)
    image = wait_complete(entry, generation)
    events = [e for e in entry.session._capture_events if not e.get("navigation_only")]
    scanned = bool(events) and events[-1].get("scan_batches", 0) > 0
    assert entry.applied_serial == entry.change_serial
    check = None
    if kind == "lights":
        entry.changed_at -= 1.0
        check = entry.run_narrow_check()
    expected = fresh_image()
    row = dict(
        case=name,
        kind=kind,
        last_capture_scanned=scanned,
        checker=check and check["differences"],
        max_abs_difference=float(np.abs(image - expected).max()),
    )
    report["cases"].append(row)
    print("LIGHT_VALUE " + json.dumps(row), flush=True)
    assert kind == expected_kind, row
    assert np.array_equal(image, expected), row
    if kind == "lights":
        assert not scanned, row
        assert check is not None and not check["differences"], row
    if undo:
        undo()


case("intensity", lambda: light.parm("light_intensity").set(35.0), "lights")
case("colour", lambda: light.parmTuple("light_color").set((1.0, 0.5, 0.25)), "lights")
case("exposure", lambda: other.parm("light_exposure").set(1.0), "lights")
case(
    "temperature",
    lambda: (light.parm("cycles_light_use_temperature").set(1), light.parm("cycles_light_temperature").set(3200)),
    "lights",
)
case("two lights", lambda: (light.parm("light_intensity").set(10.0), other.parm("light_intensity").set(9.0)), "lights")
# Animation and time/self expressions are local to the light (owner decision, round 4).
case(
    "intensity with a transform", lambda: (light.parm("light_intensity").set(12.0), light.parm("ty").set(4.5)), "lights"
)
case("intensity and a SOP edit", lambda: (light.parm("light_intensity").set(14.0), sphere.parm("freq").set(5)), "full")
case("size", lambda: light.parmTuple("areasize").set((1.5, 1.5)), "lights")
keyed = hou.Keyframe()
keyed.setFrame(1)
keyed.setValue(16.0)
case(
    "keyframed intensity",
    lambda: light.parm("light_intensity").setKeyframe(keyed),
    "lights",
    lambda: light.parm("light_intensity").deleteAllKeyframes(),
)
case(
    "expression intensity",
    lambda: light.parm("light_intensity").setExpression("17"),
    "lights",
    lambda: light.parm("light_intensity").deleteAllKeyframes(),
)
linker = geo.createNode("null", "reads_light")
linker.addSpareParmTuple(hou.FloatParmTemplate("watch", "Watch", 1))
linker.parm("watch").setExpression('ch("/obj/value_light/light_intensity")')
linker.parm("watch").eval()
case("referenced by another node", lambda: light.parm("light_intensity").set(18.0), "full", linker.destroy)
wrangle = geo.createNode("attribwrangle", "scale_by_light")
wrangle.setInput(0, sphere)
wrangle.parm("snippet").set('@P *= 1 + 0.01 * ch("/obj/value_light/light_intensity");')
wrangle.setRenderFlag(True)
wrangle.geometry()
case("read by a VEX wrangle", lambda: light.parm("light_intensity").set(19.0), "full")
registry.close_all()
print("LIGHT_VALUE_RESULT " + json.dumps(report), flush=True)
print("PASS", flush=True)
hou.exit(0, suppress_save_prompt=True)
