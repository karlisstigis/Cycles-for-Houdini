# SPDX-License-Identifier: Apache-2.0
"""Headless: parameter edits inside a rendered material network take the materials-only update (the edited
material is resolved alone, no geometry is read) and render exactly what a complete read renders on a second
ROP. Structure changes (a new node wired in) and edits that change what the geometry carries (an attribute
read) take the complete read. Also: keyed parameters, emission switched on and off, an instanced mesh with the
edited material, CYCLES_HOUDINI_MATERIAL_PATH=0 (complete read) and the worker rule switch
CYCLES_HOUDINI_MATERIAL_VALUES_ONLY=0 (same images). Scene in memory, never saved.
Usage: hython houdini_ipr_material_edit_smoke.py --device CPU [--blur]
"""

import json
import os
import sys
import time

import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene, registry
from cycles_houdini.core import device_selection
from cycles_houdini.scene import camera_properties, light_properties, object_properties

device = sys.argv[sys.argv.index("--device") + 1] if "--device" in sys.argv else "CPU"
devices = json.loads(device) if device.startswith("[") else [device]  # probes/E4/run_both.py passes a JSON list
# CPU renders of two sessions are bit-identical. On GPU two sessions are not always bit-identical after a scene
# layout change (BVH build order, kernel reductions): other tests here accept up to about 6e-8 there.
tolerance = 0.0 if devices == ["CPU"] else 1e-6
blur = "--blur" in sys.argv


def menu_pick(parm, *words):
    for item, label in zip(parm.parmTemplate().menuItems(), parm.parmTemplate().menuLabels()):
        if any(w in item.lower() or w in label.lower() for w in words):
            return item
    raise ValueError(words)


def find_parm(node, *words, tuple_=False):
    names = [t.name() for t in node.parmTuples()]
    for word in words:
        for name in names:
            if name == word:
                return node.parmTuple(name) if tuple_ else node.parm(name) or node.parmTuple(name)[0]
    for word in words:
        for name in names:
            if word in name:
                return node.parmTuple(name) if tuple_ else node.parm(name) or node.parmTuple(name)[0]
    raise ValueError("%s: none of %s in %s" % (node.path(), words, names))


obj = hou.node("/obj")
cam = obj.createNode("cam", "me_cam")
cam.setParms(dict(ty=2, tz=9, rx=-10, resx=64, resy=36))
camera_properties.install(cam)
light = obj.createNode("hlight::2.0", "me_light")
light.parm("light_type").set(menu_pick(light.parm("light_type"), "point"))
light.setParms(dict(tx=-2, ty=4, tz=3, light_intensity=20.0))
light_properties.install(light)
mat_ground = ch.create_material(name="me_ground")
mat_ball = ch.create_material(name="me_ball")
ground = obj.createNode("geo", "me_ground")
object_properties.install(ground)
ground.createNode("grid").setRenderFlag(True)
ground.parm("shop_materialpath").set(mat_ground.path())
ball = obj.createNode("geo", "me_ball")
object_properties.install(ball)
sphere = ball.createNode("sphere")
sphere.parm("type").set(menu_pick(sphere.parm("type"), "polygon"))
sphere.setRenderFlag(True)
ball.parm("ty").set(1)
ball.parm("shop_materialpath").set(mat_ball.path())
# Packed instances sharing the ball's material (many owners of one mesh).
packed = obj.createNode("geo", "me_packed")
object_properties.install(packed)
grid = packed.createNode("grid")
grid.parmTuple("size").set((4, 4))
grid.parm("rows").set(6)
grid.parm("cols").set(6)
box = packed.createNode("box")
box.parmTuple("size").set((0.2, 0.2, 0.2))
copy = packed.createNode("copytopoints::2.0")
copy.setInput(0, box)
copy.setInput(1, grid)
copy.parm("pack").set(1)
copy.setRenderFlag(True)
packed.setParms(dict(tx=2.5, ty=0.1))
packed.parm("shop_materialpath").set(mat_ball.path())
rops = []
for name in ("me_rop_a", "me_rop_b"):
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
    device_selection.set_devices(rop, devices)
    rops.append(rop)
rop_a, rop_b = rops
if blur:
    for frame, value in ((1, 0.0), (24, 0.5)):
        key = hou.Keyframe()
        key.setFrame(frame)
        key.setValue(value)
        ball.parm("tz").setKeyframe(key)
    hou.setFrame(5)
shader = mat_ball.node("surface_shader")
roughness = find_parm(shader, "roughness")
base_color = find_parm(shader, "base_color", "color", tuple_=True)
emission_strength = find_parm(shader, "emission_strength")
emission_color = find_parm(shader, "emission_color", tuple_=True)


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
    if entry.observer:
        # What the viewers' poll does after an accepted read: observe the materials the renderer bound.
        entry.observer.refresh_material_bindings(entry.session)
    return entry


report = dict(device=device, blur=blur, tolerance=tolerance, cases=[])


def case(name, edit, expected, env=None, undo=None):
    os.environ.update(env or {})
    try:
        registry.close_all()
        entry = start(rop_a)
        watched = sorted(getattr(entry.observer, "_material_wiring", {}))
        edit()
        pending = entry.material_values_pending
        now = hou.time()
        motion = native_scene.motion_configuration(rop_a, now)
        started = time.perf_counter()
        narrow = entry.narrow_update(now, motion, native_scene.capture(rop_a, at_time=now, camera_only=True))
        if narrow is not None:
            generation = entry.apply(narrow, now, reuse_if_unchanged=True, change_serial=entry.change_serial)
            path = entry.narrow_reason
        else:
            generation, path = full(entry, rop_a), entry.narrow_reason or "full read"
        host_ms = 1000 * (time.perf_counter() - started)
        image = wait_complete(entry, generation)
    finally:
        for key in env or {}:
            os.environ.pop(key, None)
    registry.close_all()
    reference = start(rop_b)
    image_full = wait_complete(reference, full(reference, rop_b))
    registry.close_all()
    row = dict(
        case=name,
        path=path,
        pending=pending,
        watched=watched,
        host_ms=round(host_ms, 2),
        equal=bool(np.array_equal(image, image_full)),
        max_difference=float(np.abs(image - image_full).max()),
    )
    report["cases"].append(row)
    print("MATERIAL_EDIT " + json.dumps(row), flush=True)
    if undo:
        undo()
    assert row["equal"] or row["max_difference"] <= tolerance, row
    assert path == expected or (expected == "full" and path.startswith("full read")), row


start_roughness = roughness.eval()
case("roughness", lambda: roughness.set(0.15), "materials", undo=lambda: roughness.set(start_roughness))
start_color = base_color.eval()
case("base colour", lambda: base_color.set((0.9, 0.2, 0.1)), "materials", undo=lambda: base_color.set(start_color))
start_strength = emission_strength.eval()
start_emission = emission_color.eval()


def emission_on():
    emission_color.set((1.0, 0.6, 0.2))
    emission_strength.set(4.0)


case("emission switched on", emission_on, "materials")
case(
    "emission switched off",
    lambda: emission_strength.set(0.0),
    "materials",
    undo=lambda: (emission_strength.set(start_strength), emission_color.set(start_emission)),
)
# Keyed parameter (animated values take the same path).
for frame, value in ((1, 0.1), (24, 0.9)):
    key = hou.Keyframe()
    key.setFrame(frame)
    key.setValue(value)
    roughness.setKeyframe(key)
hou.setFrame(5 if not blur else 5)
case(
    "keyed roughness, key edited",
    lambda: roughness.setKeyframe((lambda k: (k.setFrame(24), k.setValue(0.4), k)[-1])(hou.Keyframe())),
    "materials",
)
roughness.deleteAllKeyframes()
roughness.set(start_roughness)
case(
    "roughness, materials path off",
    lambda: roughness.set(0.3),
    "full",
    env={"CYCLES_HOUDINI_MATERIAL_PATH": "0"},
    undo=lambda: roughness.set(start_roughness),
)
case(
    "roughness, worker value rule off",
    lambda: roughness.set(0.35),
    "materials",
    env={"CYCLES_HOUDINI_MATERIAL_VALUES_ONLY": "0"},
    undo=lambda: roughness.set(start_roughness),
)
# Structure: a new node wired in. The attribute read also changes what the geometry carries.
attribute_nodes = []


def wire_attribute():
    node = mat_ball.createNode("cycles::attribute::1", "me_attribute")
    find_parm(node, "attribute").set("Cd")
    shader.setNamedInput(base_color.name(), node, "color")
    attribute_nodes.append(node)


case("attribute node wired in", wire_attribute, "full")
# A value edit that changes what the geometry carries: the attribute name. prepare_material_edit refuses it.
case(
    "attribute name edited",
    lambda: find_parm(attribute_nodes[0], "attribute").set("me_weight"),
    "full",
    undo=lambda: [n.destroy() for n in attribute_nodes],
)
assert report["cases"][-1]["path"].startswith("full read: attributes the geometry carries changed"), report["cases"][-1]
# Ground material (not instanced) still works after the network went back to its old structure.
ground_roughness = find_parm(mat_ground.node("surface_shader"), "roughness")
case("ground roughness", lambda: ground_roughness.set(0.8), "materials")
print("MATERIAL_EDIT_RESULT " + json.dumps(report), flush=True)
print("PASS", flush=True)
hou.exit(0, suppress_save_prompt=True)
