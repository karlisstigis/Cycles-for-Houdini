# SPDX-License-Identifier: Apache-2.0
"""Headless: a viewport handle drag. Houdini's handles write the transform through setParmTransform-like
calls, which (measured in hython) emit ParmTupleChanged for t/r AND one ParmTupleChanged without a parm
tuple. The observer classifies the tuple-less event by what changed on the node, so a light drag stays on
the pose path and ends with the lights-only read, an object drag ends with the pose commit; both images
equal a complete read on a second ROP. With CYCLES_HOUDINI_UNNAMED_TUPLE_RULE=0 the same drag must fall
back to the complete read (the test reproduces the on-screen problem). Scene in memory, never saved.
Usage: hython houdini_ipr_handle_drag_smoke.py --device CPU [--blur]
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
blur = "--blur" in sys.argv


def menu_pick(parm, *words):
    for item, label in zip(parm.parmTemplate().menuItems(), parm.parmTemplate().menuLabels()):
        if any(w in item.lower() or w in label.lower() for w in words):
            return item
    raise ValueError(words)


obj = hou.node("/obj")
cam = obj.createNode("cam", "hd_cam")
cam.setParms(dict(ty=2, tz=9, rx=-10, resx=64, resy=36))
camera_properties.install(cam)
light = obj.createNode("hlight::2.0", "hd_light")
light.parm("light_type").set(menu_pick(light.parm("light_type"), "point"))
light.setParms(dict(tx=-2, ty=4, tz=3, light_intensity=20.0))
light_properties.install(light)
ground = obj.createNode("geo", "hd_ground")
object_properties.install(ground)
ground.createNode("grid").setRenderFlag(True)
ball = obj.createNode("geo", "hd_ball")
object_properties.install(ball)
sphere = ball.createNode("sphere")
sphere.parm("type").set(menu_pick(sphere.parm("type"), "polygon"))
sphere.setRenderFlag(True)
ball.parm("ty").set(1)
rops = []
for name in ("hd_rop_a", "hd_rop_b"):
    rop = hou.node("/out").createNode("cycles_ipr", name)
    rop.setParms(
        dict(
            camera=cam.path(),
            resx=64,
            resy=36,
            ipr_samples=2,
            ipr_adaptive_sampling=0,
            ipr_use_denoise=0,
            allowmotionblur=1 if blur else 0,
        )
    )
    device_selection.set_devices(rop, [device])
    rops.append(rop)
rop_a, rop_b = rops
if blur:
    for frame, value in ((1, 0.0), (24, 0.5)):
        key = hou.Keyframe()
        key.setFrame(frame)
        key.setValue(value)
        ground.parm("tz").setKeyframe(key)
    hou.setFrame(5)


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


report = dict(device=device, blur=blur, cases=[])


def drag(name, node, expected, env=None):
    start_matrix = node.parmTransform()
    os.environ.update(env or {})
    try:
        registry.close_all()
        entry = start(rop_a)
        during = []
        for n in range(4):  # what a handle does per mouse move: setParmTransform (t, r and a tuple-less event)
            node.setParmTransform(
                start_matrix * hou.hmath.buildTranslate(0.25 * (n + 1), 0.1 * n, 0) * hou.hmath.buildRotate(0, 5 * n, 0)
            )
            during.append(dict(transform_only=entry.transform_only_pending, lights=entry.light_values_pending))
            entry.preview_object_poses()
            time.sleep(0.03)
        time.sleep(0.2)
        now = hou.time()
        motion = native_scene.motion_configuration(rop_a, now)
        narrow = entry.narrow_update(now, motion, native_scene.capture(rop_a, at_time=now, camera_only=True))
        if narrow is not None:
            generation = entry.apply(narrow, now, reuse_if_unchanged=True, change_serial=entry.change_serial)
            path = entry.narrow_reason
        else:
            generation, path = full(entry, rop_a), entry.narrow_reason or "full read"
        image = wait_complete(entry, generation)
        check = entry.run_narrow_check() if narrow is not None else None
    finally:
        for key in env or {}:
            os.environ.pop(key, None)
    registry.close_all()
    reference = start(rop_b)
    image_full = wait_complete(reference, full(reference, rop_b))
    row = dict(
        case=name,
        path=path,
        during=during,
        equal=bool(np.array_equal(image, image_full)),
        max_difference=float(np.abs(image - image_full).max()),
        checker=None if check is None else check["differences"],
    )
    report["cases"].append(row)
    print("HANDLE_DRAG " + json.dumps(row), flush=True)
    node.setParmTransform(start_matrix)
    assert row["equal"], row
    assert path == expected or (expected == "full" and path.startswith("full read")), row
    assert not row["checker"], row


drag("light handle drag", light, "lights")
drag("object handle drag", ball, "poses")
# Animated transform (normal use: change a keyed move to see how it looks): keys on the ball's tx/ry.
for frame, value in ((1, 0.0), (24, 0.6)):
    key = hou.Keyframe()
    key.setFrame(frame)
    key.setValue(value)
    ball.parm("tx").setKeyframe(key)
    key = hou.Keyframe()
    key.setFrame(frame)
    key.setValue(10 * value)
    ball.parm("ry").setKeyframe(key)
hou.setFrame(5)
drag("keyed object handle drag", ball, "poses")
for name in ("tx", "ry"):
    ball.parm(name).deleteAllKeyframes()
# Tuple kinds of the fingerprint (decided once per baseline): plain values, expressions (compared as text,
# never evaluated in a node event) and keyed channels (compared by value).
template = hou.FloatParmTemplate("hd_expression", "Expression", 1)
ball.addSpareParmTuple(template)
ball.parm("hd_expression").setExpression('ch("/obj/hd_ground/ty") + $F * 0.01')
key = hou.Keyframe()
key.setFrame(1)
key.setValue(0.0)
ball.parm("rz").setKeyframe(key)
key = hou.Keyframe()
key.setFrame(24)
key.setValue(3.0)
ball.parm("rz").setKeyframe(key)
kinds = {
    name: native_scene._tuple_kind(ball.parmTuple(name)) for name in ("t", "hd_expression", "r", "shop_materialpath")
}
print("HANDLE_DRAG_KINDS " + json.dumps(kinds), flush=True)
assert kinds == dict(t="plain", hd_expression="expression", r="keyed", shop_materialpath="plain"), kinds
ball.parm("rz").deleteAllKeyframes()
drag("object handle drag, expression parameter on the object", ball, "poses")
drag("object handle drag, per-parameter fingerprint", ball, "poses", {"CYCLES_HOUDINI_TUPLE_KINDS": "0"})
ball.removeSpareParmTuple(template)
drag("light handle drag, tuple-less rule off", light, "full", {"CYCLES_HOUDINI_UNNAMED_TUPLE_RULE": "0"})
# Tuple-less event that also changed another light parameter (intensity set beside the transform):
# the light value route, still the lights-only read.
start_intensity = light.parm("light_intensity").eval()
registry.close_all()
entry = start(rop_a)
light.parm("light_intensity").set(30.0)  # named event first: lights route
with hou.undos.group("handle"):
    light.setParmTransform(light.parmTransform() * hou.hmath.buildTranslate(0.3, 0, 0))
now = hou.time()
narrow = entry.narrow_update(
    now, native_scene.motion_configuration(rop_a, now), native_scene.capture(rop_a, at_time=now, camera_only=True)
)
row = dict(case="light value then handle move", path=entry.narrow_reason)
assert narrow is not None and entry.narrow_reason == "lights", row
image = wait_complete(entry, entry.apply(narrow, now, reuse_if_unchanged=True, change_serial=entry.change_serial))
registry.close_all()
reference = start(rop_b)
row["equal"] = bool(np.array_equal(image, wait_complete(reference, full(reference, rop_b))))
report["cases"].append(row)
print("HANDLE_DRAG " + json.dumps(row), flush=True)
assert row["equal"], row
light.parm("light_intensity").set(start_intensity)
registry.close_all()

# A literal Houdini Light Linker mask registers a node dependency even though
# moving or recoloring the emitter cannot change the mask or the receiver.
named = obj.createNode(light_properties.LIGHT_TYPE, "hd_named")
named.setParms(dict(tx=2, ty=4, tz=3, rx=-45, light_intensity=30))
ball.parm("lightmask").set("* ^hd_named")
drag("named light drag with literal receiver mask", named, "lights")

entry = start(rop_a)
assert named.sessionId() in entry.observer.light_value_nodes
conversions = entry.session.status()["geometry_conversions"]
for name, change, route in (
    ("native tint", lambda: named.parmTuple("cycles_light_color").set((0.35, 0.8, 1)), "lights"),
    ("native type", lambda: named.parm("cycles_light_type").set("spot"), "lights"),
    ("native shadow mask", lambda: named.parm("shadowmask").set("* ^hd_ball"), "lights+linking"),
):
    now = hou.time()
    change()
    narrow = entry.narrow_update(
        now, native_scene.motion_configuration(rop_a, now), native_scene.capture(rop_a, now, camera_only=True)
    )
    assert narrow is not None and entry.narrow_reason == route and "geometry" not in narrow, name
    generation = entry.apply(narrow, now, reuse_if_unchanged=True, change_serial=entry.change_serial)
    image = wait_complete(entry, generation)
    assert not entry.run_narrow_check()["differences"], name
    assert entry.session.status()["geometry_conversions"] == conversions, name
    reference = start(rop_b)
    assert np.array_equal(image, wait_complete(reference, reference.session.status()["generation"])), name
    registry.retire(reference.key)
    report["cases"].append(dict(case=name, path=route, equal=True))
registry.close_all()
drag("geometry drag with named light shadow mask", ball, "poses")

for parm, expression in (
    (ball.parm("tx"), 'ch("../hd_named/light_intensity")'),
    (sphere.parm("radx"), 'ch("../../hd_named/light_intensity") / 30'),
    (sphere.parm("radx"), '1 + abs(origin("/obj/hd_named", "", "TX"))'),
):
    parm.setExpression(expression)
    sphere.geometry()
    observer = native_scene.SceneObserver(rop_a, lambda: None, light_value_callback=lambda node: None)
    try:
        assert named.sessionId() not in observer.light_value_nodes, expression
    finally:
        observer.close()
        parm.deleteAllKeyframes()
        parm.set(1 if parm == sphere.parm("radx") else 0)

# Scene geometry can explicitly import the viewport guide. Even though the
# locked internal guide is not watched directly, the external read is real.
merge = ball.createNode("object_merge")
merge.parm("objpath1").set(named.renderNode().path())
merge.setRenderFlag(True)
merge.geometry()
observer = native_scene.SceneObserver(rop_a, lambda: None, light_value_callback=lambda node: None)
try:
    assert named.sessionId() not in observer.light_value_nodes
finally:
    observer.close()

print("HANDLE_DRAG_RESULT " + json.dumps(report), flush=True)
print("PASS", flush=True)
hou.exit(0, suppress_save_prompt=True)
