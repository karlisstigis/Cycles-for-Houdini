# SPDX-License-Identifier: Apache-2.0
"""Light manager reuse of the light object list (engine patch 0112) under CYCLES_VERIFY_LIGHT_LIST.

A live session runs a chain of light move, strength, colour, type, enable/disable, add/remove, object and
material edits, frame changes and light-tree on/off with the engine's verification on: every light manager
update compares the light objects it uses (collected by the object update) with walking every object of the
scene and reports any difference as a session error. The test asserts that no error was reported, that the
verification ran on every light update, and that the frame after the edits equals a fresh render. Scene in
memory, never saved.
"""

import argparse, json, os, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
import hou
import numpy as np

os.environ["CYCLES_VERIFY_LIGHT_LIST"] = "1"
os.environ.pop("CYCLES_FULL_LIGHT_WALK", None)
import cycles_houdini as ch
from cycles_houdini import device_selection, light_properties, materials, native_scene, object_properties, registry
from cycles_houdini.rop import passes

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
device = json.loads(device) if device.startswith("[") else device
report = dict(result="running", device=device, scope=__doc__, states=[])

obj = hou.node("/obj")
cam = obj.createNode("cam", "ll_cam")
cam.setParms(dict(tx=0, ty=4, tz=9, rx=-22, resx=320, resy=180))
ground = obj.createNode("geo", "ll_ground", run_init_scripts=False)
object_properties.install(ground)
ground.createNode("grid").setParms(dict(sizex=14, sizey=14, rows=10, cols=10))
sphere = obj.createNode("geo", "ll_sphere", run_init_scripts=False)
object_properties.install(sphere)
sphere.createNode("sphere").setParms(dict(type=2, freq=12, ty=1))
inst = obj.createNode("geo", "ll_instances", run_init_scripts=False)
object_properties.install(inst)
isph = inst.createNode("sphere")
isph.setParms(dict(type=2, freq=3, scale=0.06))
ipts = inst.createNode("grid")
ipts.setParms(dict(sizex=6, sizey=6, rows=30, cols=30, tz=-3, ty=0.1))
icp = inst.createNode("copytopoints::2.0")
icp.setInput(0, isph)
icp.setInput(1, ipts)
icp.parm("pack").set(1)
icp.setRenderFlag(True)
icp.setDisplayFlag(True)
key = obj.createNode(light_properties.LIGHT_TYPE, "ll_key")
key.setParms(dict(cycles_light_type="point", tx=-2, ty=4, tz=3, light_intensity=60))
fill = obj.createNode(light_properties.LIGHT_TYPE, "ll_fill")
fill.setParms(dict(cycles_light_type="grid", tx=2, ty=3, tz=4, light_intensity=20))
extra = obj.createNode(light_properties.LIGHT_TYPE, "ll_extra")
extra.setParms(dict(cycles_light_type="spot", tx=0, ty=5, tz=1, light_intensity=40))
sun = obj.createNode(light_properties.LIGHT_TYPE, "ll_sun")
sun.setParms(dict(cycles_light_type="sun", light_intensity=1))
other = materials.create_material(name="ll_other", shader="diffuse_bsdf")
rop = hou.node("/out").createNode("cycles_ipr", "ll_rop")
rop.setParms(
    dict(
        camera=cam.path(),
        resx=320,
        resy=180,
        ipr_samples=8,
        samples=8,
        ipr_adaptive_sampling=0,
        adaptive_sampling=0,
        ipr_use_denoise=0,
        use_denoise=0,
        allowmotionblur=0,
        world_source="color",
    )
)
for name, value in (("seed", 1), ("animate_seed", 0), ("use_animated_seed", 0)):
    if rop.parm(name) is not None:
        rop.parm(name).set(value)
device_selection.set_devices(rop, device)
_orig = registry.Session
registry.Session = lambda **kw: _orig(collect_diagnostics=True, **kw)


def lights(nodes):
    rop.parm("lights").set(" ".join(n.path() for n in nodes))


def setp(node, name, value):
    if node.parm(name) is not None:
        node.parm(name).set(value)


STATES = [
    ("init", lambda: lights([key, fill])),
    ("light_move", lambda: key.parm("tx").set(-1.5)),
    ("light_strength", lambda: key.parm("light_intensity").set(80)),
    ("light_color", lambda: key.parmTuple("cycles_light_color").set((1.0, 0.6, 0.4))),
    ("light_type", lambda: key.setParms(dict(cycles_light_type="grid", cycles_light_area_shape="disk"))),
    ("light_type_back", lambda: key.parm("cycles_light_type").set("point")),
    ("light_zero", lambda: fill.parm("light_intensity").set(0)),
    ("light_restore", lambda: fill.parm("light_intensity").set(20)),
    ("light_added", lambda: lights([key, fill, extra])),
    ("light_move_added", lambda: extra.parm("tz").set(1.5)),
    ("sun_added", lambda: lights([key, fill, extra, sun])),
    ("sun_strength", lambda: sun.parm("light_intensity").set(2)),
    ("light_removed", lambda: lights([key, extra, sun])),
    ("light_move_after_remove", lambda: key.parm("ty").set(3.5)),
    ("object_move", lambda: sphere.parm("tx").set(0.5)),
    ("material_change", lambda: sphere.parm("shop_materialpath").set(other.path())),
    ("instances_move", lambda: inst.parm("ty").set(0.2)),
    ("light_move_after_objects", lambda: extra.parm("tx").set(0.5)),
    ("light_tree_off", lambda: setp(rop, "light_tree", 0)),
    ("light_move_no_tree", lambda: key.parm("tx").set(-1.0)),
    ("light_tree_on", lambda: setp(rop, "light_tree", 1)),
    ("frame_change", lambda: hou.setFrame(5)),
    ("light_strength_after_frame", lambda: extra.parm("light_intensity").set(55)),
]


def render(entry):
    cap = native_scene.capture(rop)
    if entry is None:
        entry = registry.for_rop(rop, native_scene.configuration(rop))
    frame = entry.session.wait(entry.apply(cap, hou.time()))
    status = entry.session.status()
    stats = entry.session.diagnostics().get("engine_update_seconds", {})
    return entry, frame, status, stats


entry = None
try:
    registry.close_all()
    hou.setFrame(1)
    for name, edit in STATES:
        edit()
        # The light tree toggle is part of the session configuration: a new session.
        entry, frame, status, stats = render(entry if not name.startswith("light_tree") else None)
        error = status.get("error") or ""
        light_update = "light/device_update" in stats
        verified = int(round(stats.get("light/light list verifications", 0)))
        row = dict(state=name, light_update=light_update, verified=verified, error=error)
        report["states"].append(row)
        print("LLSTATE " + json.dumps(row), flush=True)
        assert "verification" not in error and not error, row
        assert verified == (1 if light_update else 0), row
    assert sum(r["verified"] for r in report["states"]) >= 15, report["states"]
    last = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).copy()
    registry.close_all()
    entry, fresh_frame, status, _ = render(None)
    fresh = np.frombuffer(fresh_frame["planes"]["combined"]["pixels"], np.float32)
    report["frame_after_edits_max_error"] = float(np.abs(last - fresh).max())
    assert report["frame_after_edits_max_error"] < 1e-5, report["frame_after_edits_max_error"]
    report["result"] = "passed"
finally:
    from support.paths import artifact_path

    artifact_path(
        "engine-contracts",
        "houdini_light_list_smoke-" + ("cpu" if device.upper().startswith("CPU") else "gpu") + ".json",
    ).write_text(json.dumps(report, indent=2) + "\n")
    registry.close_all()
    print(
        json.dumps(
            dict(
                result=report["result"],
                states=len(report["states"]),
                verified=sum(r["verified"] for r in report["states"]),
                frame_after_edits_max_error=report.get("frame_after_edits_max_error"),
            )
        ),
        flush=True,
    )
