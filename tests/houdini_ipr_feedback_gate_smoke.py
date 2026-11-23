# SPDX-License-Identifier: Apache-2.0
"""Headless: the feedback gate of a live viewer. A viewer that registered for feedback holds the next update
until the current revision's first picture is out. With the default gate the picture's ARRIVAL opens it (the
viewer converts and paints while the next edit renders); with CYCLES_HOUDINI_FEEDBACK_GATE=paint only the
viewer's notify_presented opens it (or a 1 s deadline). The test makes 20 light edits while a viewer is registered
that never paints: the arrival gate is not slower than the paint gate, and in both modes the final picture
equals a complete read on a second ROP.
Usage: hython houdini_ipr_feedback_gate_smoke.py --device CPU
"""

import json
import os
import sys
import time

import hou
import numpy as np
from cycles_houdini import native_scene, registry
from cycles_houdini.core import device_selection
from cycles_houdini.scene import camera_properties, light_properties, object_properties

device = sys.argv[sys.argv.index("--device") + 1] if "--device" in sys.argv else "CPU"
devices = json.loads(device) if device.startswith("[") else [device]
tolerance = 0.0 if devices == ["CPU"] else 1e-6


def menu_pick(parm, *words):
    for item, label in zip(parm.parmTemplate().menuItems(), parm.parmTemplate().menuLabels()):
        if any(w in item.lower() or w in label.lower() for w in words):
            return item
    raise ValueError(words)


obj = hou.node("/obj")
cam = obj.createNode("cam", "fg_cam")
cam.setParms(dict(ty=2, tz=9, rx=-10, resx=64, resy=36))
camera_properties.install(cam)
light = obj.createNode("hlight::2.0", "fg_light")
light.parm("light_type").set(menu_pick(light.parm("light_type"), "point"))
light.setParms(dict(tx=-2, ty=4, tz=3, light_intensity=20.0))
light_properties.install(light)
ground = obj.createNode("geo", "fg_ground")
object_properties.install(ground)
ground.createNode("grid").setRenderFlag(True)
rops = []
for name in ("fg_rop_a", "fg_rop_b"):
    rop = hou.node("/out").createNode("cycles_ipr", name)
    rop.setParms(
        dict(camera=cam.path(), resx=64, resy=36, ipr_samples=1024, ipr_adaptive_sampling=0, ipr_use_denoise=0)
    )
    device_selection.set_devices(rop, devices)
    rops.append(rop)
rop_a, rop_b = rops


def wait_complete(entry, generation, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        status = entry.session.status()
        frame = entry.session.frame_info()
        if status["state"] == "failed":
            raise RuntimeError(status["error"])
        if status["state"] == "complete" and frame and frame["complete"] and frame["generation"] >= generation:
            if not status.get("capture_pending") and not status.get("pending_update"):
                return np.frombuffer(entry.session.frame()["planes"]["combined"]["pixels"], np.float32).copy()
        time.sleep(0.005)
    raise TimeoutError("render did not complete")


def full(entry, rop):
    now = hou.time()
    return entry.apply(native_scene.capture(rop, now), now, reuse_if_unchanged=True)


def start(rop):
    entry = registry.for_rop(rop, native_scene.configuration(rop))
    wait_complete(entry, full(entry, rop))
    return entry


report = dict(device=device, cases=[])


def edits(name, env=None):
    base = light.evalParm("light_intensity")
    os.environ.update(env or {})
    try:
        registry.close_all()
        entry = start(rop_a)
        entry.session.set_feedback("native-display", True)  # a viewer that never paints
        started = time.monotonic()
        for n in range(20):
            light.parm("light_intensity").set(base * (1.0 + 0.04 * (n + 1)))
            now = hou.time()
            narrow = entry.narrow_update(
                now,
                native_scene.motion_configuration(rop_a, now),
                native_scene.capture(rop_a, at_time=now, camera_only=True),
            )
            assert narrow is not None, entry.narrow_reason
            entry.apply(narrow, now, reuse_if_unchanged=True, change_serial=entry.change_serial)
            for _ in range(10):
                entry.session.status()
                time.sleep(0.003)
        generation = entry.session.status()["generation"]
        entry.session.set_feedback("native-display", False)
        image = wait_complete(entry, generation)
        seconds = time.monotonic() - started
        physical = entry.session.diagnostics().get("controller", {}).get("events", [])
        applied = sum(1 for e in physical if e.get("accepted"))
    finally:
        for key in env or {}:
            os.environ.pop(key, None)
    registry.close_all()
    reference = start(rop_b)
    image_full = wait_complete(reference, full(reference, rop_b))
    registry.close_all()
    light.parm("light_intensity").set(base)
    row = dict(
        case=name,
        seconds=round(seconds, 2),
        updates_applied=applied,
        equal=bool(np.array_equal(image, image_full)),
        max_difference=float(np.abs(image - image_full).max()),
    )
    report["cases"].append(row)
    print("FEEDBACK_GATE " + json.dumps(row), flush=True)
    assert row["equal"] or row["max_difference"] <= tolerance, row
    return row


arrival = edits("gate opens when the picture arrives")
paint = edits("gate opens on paint only", {"CYCLES_HOUDINI_FEEDBACK_GATE": "paint"})
# Without paints the paint gate can only open by its 1 s deadline; the arrival gate never waits for a paint.
assert arrival["seconds"] <= paint["seconds"] + 0.25, (arrival, paint)
print("FEEDBACK_GATE_RESULT " + json.dumps(report), flush=True)
print("PASS", flush=True)
hou.exit(0, suppress_save_prompt=True)
