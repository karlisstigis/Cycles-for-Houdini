# SPDX-License-Identifier: Apache-2.0
"""Partial object flags updates (engine patch 0113) under CYCLES_VERIFY_OBJECT_FLAGS.

A live session runs a chain of light, object, instance, visibility, holdout, shadow catcher, material,
volume add/move/remove, motion blur and frame edits with the engine's verification on: after every partial
flags pass the engine compares the device copies of the object flags on every device with the host and the
partial result with updating every object, and reports any difference as a session error. The test asserts
that no error was reported, that every partial pass was verified, that partial passes happen for single
object and light edits, that no partial pass happens while a volume object exists or after a material
change, and that the frame after the edits equals a fresh render. Scene in memory, never saved.
"""

import argparse, json, os, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
import hou
import numpy as np

os.environ["CYCLES_VERIFY_OBJECT_FLAGS"] = "1"
os.environ.pop("CYCLES_FULL_OBJECT_FLAGS", None)
os.environ.pop("CYCLES_FULL_OBJECT_TABLE_UPDATE", None)
import cycles_houdini as ch
from cycles_houdini import device_selection, light_properties, materials, native_scene, object_properties, registry
from cycles_houdini.rop import passes
from support.devices import render_device_evidence

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
device = json.loads(device) if device.startswith("[") else device
report = dict(result="running", device=device, scope=__doc__, states=[])

obj = hou.node("/obj")
cam = obj.createNode("cam", "of_cam")
cam.setParms(dict(tx=0, ty=4, tz=9, rx=-22, resx=320, resy=180))
ground = obj.createNode("geo", "of_ground", run_init_scripts=False)
object_properties.install(ground)
ground.createNode("grid").setParms(dict(sizex=14, sizey=14, rows=10, cols=10))
sphere = obj.createNode("geo", "of_sphere", run_init_scripts=False)
object_properties.install(sphere)
s = sphere.createNode("sphere")
s.setParms(dict(type=2, freq=12, ty=1))
sv = sphere.createNode("attribwrangle")
sv.setInput(0, s)
sv.parm("snippet").set("v@Cd = set(0.5 + 0.5 * @P.x, 0.4, 0.5); v@v = set(2, 0, 0);")
sv.setRenderFlag(True)
sv.setDisplayFlag(True)
inst = obj.createNode("geo", "of_instances", run_init_scripts=False)
object_properties.install(inst)
isph = inst.createNode("sphere")
isph.setParms(dict(type=2, freq=3, scale=0.06))
ipts = inst.createNode("grid")
ipts.setParms(dict(sizex=6, sizey=6, rows=45, cols=45, tz=-3))
iw = inst.createNode("attribwrangle", "of_move")
iw.setInput(0, ipts)
iw.addSpareParmTuple(hou.IntParmTemplate("nmove", "nmove", 1))
iw.addSpareParmTuple(hou.IntParmTemplate("every", "every", 1))
iw.parm("snippet").set(
    "@P.y = 0.1; if ((chi('every') > 0 && @ptnum % chi('every') == 0) || (chi('every') == 0 && @ptnum < chi('nmove'))) @P.y += 0.5; v@v = set(0, 0, 1);"
)
icp = inst.createNode("copytopoints::2.0")
icp.setInput(0, isph)
icp.setInput(1, iw)
icp.parm("pack").set(1)  # packed instances: one engine object per copy
icp.setRenderFlag(True)
icp.setDisplayFlag(True)
extra = obj.createNode("geo", "of_extra", run_init_scripts=False)
object_properties.install(extra)
extra.createNode("box").setParms(dict(tx=-2, ty=0.5, scale=0.5))
vol = obj.createNode("geo", "of_volume", run_init_scripts=False)
object_properties.install(vol)
vv = vol.createNode("volume", "density")
vv.setParms(dict(name="density", initialval1=0.3, uniformsamples=0, divsx=8, divsy=8, divsz=8))
vv.parmTuple("size").set((1.5, 1.5, 1.5))
vv.parmTuple("t").set((1.5, 1.0, 0.0))
vv.setDisplayFlag(True)
vv.setRenderFlag(True)
volmat = materials.create_material(name="of_volume_mat", shader="absorption_volume")
vol.parm("shop_materialpath").set(volmat.path())
key = obj.createNode(light_properties.LIGHT_TYPE, "of_key")
key.setParms(dict(cycles_light_type="point", tx=-2, ty=4, tz=3, light_intensity=60))
fill = obj.createNode(light_properties.LIGHT_TYPE, "of_fill")
fill.setParms(dict(cycles_light_type="point", tx=2, ty=3, tz=4, light_intensity=20))
other = materials.create_material(name="of_other", shader="diffuse_bsdf")
rop = hou.node("/out").createNode("cycles_ipr", "of_rop")
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
        lights=key.path() + " " + fill.path(),
    )
)
gm = rop.parm("geometry_motion")
gm.set(next(i for i in gm.parmTemplate().menuItems() if "veloc" in i))
for name, value in (("seed", 1), ("animate_seed", 0), ("use_animated_seed", 0)):
    if rop.parm(name) is not None:
        rop.parm(name).set(value)
passes.add(rop, "lightgroup", enable=1, source="groupA")
device_selection.set_devices(rop, device)
BASE = [ground, sphere, inst]
_orig = registry.Session
registry.Session = lambda **kw: _orig(collect_diagnostics=True, **kw)


def objects(nodes):
    rop.parm("vobject").set(" ".join(n.name() for n in nodes))


def init():
    hou.setFrame(1)
    objects(BASE)


# (name, edit, expectation): "P" partial flags pass expected, "F" full flags pass expected, None either
STATES = [
    ("init", init, "F"),
    ("light_move", lambda: key.parm("tx").set(-1.5), None),
    ("light_move2", lambda: key.parm("tx").set(-1.0), "P"),
    ("object_move", lambda: sphere.parm("tx").set(0.5), "P"),
    ("object_move2", lambda: sphere.parm("tx").set(0.6), "P"),
    (
        "visibility_off",
        lambda: (sphere.parm("cycles_visibility_override").set(1), sphere.parm("cycles_visibility_camera").set(0)),
        None,
    ),
    ("visibility_on", lambda: sphere.parm("cycles_visibility_camera").set(1), None),
    ("holdout_on", lambda: sphere.parm("cycles_holdout").set(1), None),
    ("holdout_off", lambda: sphere.parm("cycles_holdout").set(0), None),
    ("catcher_on", lambda: ground.parm("cycles_shadow_catcher").set(1), None),
    ("object_move_catcher", lambda: sphere.parm("tx").set(0.65), "P"),
    ("catcher_off", lambda: ground.parm("cycles_shadow_catcher").set(0), None),
    ("instances_below_threshold", lambda: iw.parm("nmove").set(50), None),
    # Scattered moves: every 3rd instance (about 675 runs, uploaded whole) and every 40th (about 50 runs,
    # uploaded as ranges), then back to the first ones.
    ("instances_scattered_many", lambda: iw.parm("every").set(3), "P"),
    ("instances_scattered_few", lambda: iw.parm("every").set(40), "P"),
    ("instances_unscattered", lambda: iw.parm("every").set(0), None),
    ("instances_many", lambda: iw.parm("nmove").set(1200), "P"),
    ("instances_all", lambda: inst.parm("ty").set(0.2), "P"),
    ("object_move_after_instances", lambda: sphere.parm("tx").set(0.7), None),
    ("object_move_after_instances2", lambda: sphere.parm("tx").set(0.75), "P"),
    ("material_change", lambda: sphere.parm("shop_materialpath").set(other.path()), "F"),
    ("object_move_after_material", lambda: sphere.parm("tx").set(0.8), None),
    ("object_move_after_material2", lambda: sphere.parm("tx").set(0.85), "P"),
    ("volume_added", lambda: objects(BASE + [vol]), "F"),
    ("object_move_with_volume", lambda: sphere.parm("tx").set(1.2), "F"),
    ("volume_move", lambda: vol.parm("tx").set(-0.5), "F"),
    ("light_move_with_volume", lambda: key.parm("tx").set(-0.8), "F"),
    ("volume_removed", lambda: objects(BASE), "F"),
    ("object_move_after_volume", lambda: sphere.parm("tx").set(1.0), None),
    ("object_move_after_volume2", lambda: sphere.parm("tx").set(0.9), "P"),
    ("object_added", lambda: objects(BASE + [extra]), "F"),
    ("object_move_after_add", lambda: extra.parm("tx").set(-2.5), None),
    ("object_move_after_add2", lambda: extra.parm("tx").set(-2.6), "P"),
    ("blur_on", lambda: rop.parm("allowmotionblur").set(1), "F"),
    ("object_move_blur", lambda: extra.parm("tz").set(0.3), None),
    ("object_move_blur2", lambda: extra.parm("tz").set(0.4), "P"),
    ("blur_off", lambda: rop.parm("allowmotionblur").set(0), "F"),
    ("frame_change", lambda: hou.setFrame(4), None),
    ("object_move_after_frame", lambda: extra.parm("tx").set(-3.0), None),
]


def render(entry):
    cap = native_scene.capture(rop)
    if entry is None:
        entry = registry.for_rop(rop, native_scene.configuration(rop))
    frame = entry.session.wait(entry.apply(cap, hou.time()))
    status = entry.session.status()
    render_device_evidence(status, device)
    stats = entry.session.diagnostics().get("engine_update_seconds", {})
    return entry, frame, status, stats


def counts(stats, name):
    return int(round(stats.get("object/" + name, 0)))


entry = None
try:
    registry.close_all()
    for name, edit, expect in STATES:
        edit()
        entry, frame, status, stats = render(entry)
        error = status.get("error") or ""
        partial = counts(stats, "partial object flags updates") > 0
        verified = counts(stats, "object flags verifications")
        row = dict(
            state=name,
            partial=partial,
            verified=verified,
            objects=counts(stats, "partial object flags objects"),
            table_partial=counts(stats, "partial object table updates") > 0,
            error=error,
            expect=expect,
            **render_device_evidence(status, device),
        )
        report["states"].append(row)
        print("OFSTATE " + json.dumps(row), flush=True)
        assert "verification" not in error and not error, row
        assert verified == (1 if partial else 0), row
        assert not partial or row["table_partial"], row  # only after a partial object table write
        if expect == "P":
            assert partial, row
        elif expect == "F":
            assert not partial, row
    assert sum(r["partial"] for r in report["states"]) >= 8, report["states"]
    last = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).copy()
    registry.close_all()
    entry, fresh_frame, status, _ = render(None)
    fresh = np.frombuffer(fresh_frame["planes"]["combined"]["pixels"], np.float32)
    report["frame_after_partials_max_error"] = float(np.abs(last - fresh).max())
    assert report["frame_after_partials_max_error"] < 1e-5, report["frame_after_partials_max_error"]
    report["result"] = "passed"
finally:
    from support.paths import artifact_path

    artifact_path(
        "engine-contracts",
        "houdini_object_flags_partial_smoke-" + ("cpu" if device == "CPU" else "gpu") + ".json",
    ).write_text(json.dumps(report, indent=2) + "\n")
    registry.close_all()
    print(
        json.dumps(
            dict(
                result=report["result"],
                states=len(report["states"]),
                partial_states=[r["state"] for r in report["states"] if r["partial"]],
                frame_after_partials_max_error=report.get("frame_after_partials_max_error"),
            )
        ),
        flush=True,
    )
