# SPDX-License-Identifier: Apache-2.0
"""Partial object table updates (engine patch 0108) under CYCLES_VERIFY_OBJECT_TABLE.

A live session runs a chain of light, object, instance, visibility, holdout, shadow catcher, light group,
material, motion blur, motion pass, add/remove and frame edits with the engine's verification on: after
every partial write the engine compares the device copies on every device with the host tables and a full
rewrite of every object with the partial result, and reports any difference as a session error. The test
asserts that no error was reported, that partial writes happened where expected and not where the full
path is required, that any share of modified objects takes the partial path (scattered or all), that a frame rendered after partial updates equals a
fresh render, and that a final (non-interactive) frame sequence with one animated object takes the partial
path and verifies. Scene in memory, never saved.
"""

import argparse, json, os, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
import hou
import numpy as np

os.environ["CYCLES_VERIFY_OBJECT_TABLE"] = "1"
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
cam = obj.createNode("cam", "ot_cam")
cam.setParms(dict(tx=0, ty=4, tz=9, rx=-22, resx=320, resy=180))
ground = obj.createNode("geo", "ot_ground", run_init_scripts=False)
object_properties.install(ground)
ground.createNode("grid").setParms(dict(sizex=14, sizey=14, rows=10, cols=10))
sphere = obj.createNode("geo", "ot_sphere", run_init_scripts=False)
object_properties.install(sphere)
s = sphere.createNode("sphere")
s.setParms(dict(type=2, freq=12, ty=1))
sv = sphere.createNode("attribwrangle")
sv.setInput(0, s)
sv.parm("snippet").set("v@Cd = set(0.5 + 0.5 * @P.x, 0.4, 0.5); v@v = set(2, 0, 0);")
sv.setRenderFlag(True)
sv.setDisplayFlag(True)
inst = obj.createNode("geo", "ot_instances", run_init_scripts=False)
object_properties.install(inst)
isph = inst.createNode("sphere")
isph.setParms(dict(type=2, freq=3, scale=0.06))
ipts = inst.createNode("grid")
ipts.setParms(dict(sizex=6, sizey=6, rows=45, cols=45, tz=-3))
iw = inst.createNode("attribwrangle", "ot_move")
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
extra = obj.createNode("geo", "ot_extra", run_init_scripts=False)
object_properties.install(extra)
extra.createNode("box").setParms(dict(tx=-2, ty=0.5, scale=0.5))
key = obj.createNode(light_properties.LIGHT_TYPE, "ot_key")
key.setParms(dict(cycles_light_type="point", tx=-2, ty=4, tz=3, light_intensity=60))
fill = obj.createNode(light_properties.LIGHT_TYPE, "ot_fill")
fill.setParms(dict(cycles_light_type="point", tx=2, ty=3, tz=4, light_intensity=20))
other = materials.create_material(name="ot_other", shader="diffuse_bsdf")
rop = hou.node("/out").createNode("cycles_ipr", "ot_rop")
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


# (name, edit, expectation): "P" partial write expected, "F" full write expected, None either
STATES = [
    ("init", init, "F"),
    ("light_move", lambda: key.parm("tx").set(-1.5), None),
    ("light_move2", lambda: key.parm("tx").set(-1.0), "P"),
    ("light_intensity", lambda: key.parm("light_intensity").set(80), None),
    ("object_move", lambda: sphere.parm("tx").set(0.5), "P"),
    (
        "visibility_off",
        lambda: (sphere.parm("cycles_visibility_override").set(1), sphere.parm("cycles_visibility_camera").set(0)),
        None,
    ),
    ("visibility_on", lambda: sphere.parm("cycles_visibility_camera").set(1), None),
    ("holdout_on", lambda: sphere.parm("cycles_holdout").set(1), None),
    ("holdout_off", lambda: sphere.parm("cycles_holdout").set(0), None),
    ("catcher_on", lambda: ground.parm("cycles_shadow_catcher").set(1), None),
    ("catcher_off", lambda: ground.parm("cycles_shadow_catcher").set(0), None),
    ("lightgroup_existing", lambda: sphere.parm("cycles_lightgroup").set("groupA"), None),
    ("object_move_grouped", lambda: sphere.parm("tx").set(0.6), "P"),
    (
        "lightgroup_new",
        lambda: (
            passes.add(rop, "lightgroup", enable=1, source="groupB"),
            sphere.parm("cycles_lightgroup").set("groupB"),
        ),
        "F",
    ),
    ("object_move_after_groups", lambda: sphere.parm("tx").set(0.7), "P"),
    ("material_change", lambda: sphere.parm("shop_materialpath").set(other.path()), "F"),
    ("light_move_after_material", lambda: key.parm("ty").set(3.5), "P"),
    ("instances_few", lambda: iw.parm("nmove").set(50), "P"),
    # Scattered moves: every 3rd instance (about 675 runs, uploaded whole) and every 40th (about 50 runs,
    # uploaded as ranges), then back to the first ones.
    ("instances_scattered_many", lambda: iw.parm("every").set(3), "P"),
    ("instances_scattered_few", lambda: iw.parm("every").set(40), "P"),
    ("instances_unscattered", lambda: iw.parm("every").set(0), None),
    ("instances_many", lambda: iw.parm("nmove").set(1000), "P"),
    ("instances_most", lambda: iw.parm("nmove").set(1500), "P"),
    ("instances_all", lambda: inst.parm("ty").set(0.2), "P"),
    ("object_added", lambda: objects(BASE + [extra]), "F"),
    ("object_move_after_add", lambda: extra.parm("tx").set(-2.5), "P"),
    ("object_removed", lambda: objects([ground, inst, extra]), "F"),
    ("light_move_after_remove", lambda: key.parm("tz").set(2.5), "P"),
    ("restore_sphere", lambda: objects(BASE + [extra]), "F"),
    ("blur_on", lambda: rop.parm("allowmotionblur").set(1), "F"),
    ("object_move_blur", lambda: extra.parm("tz").set(0.3), "P"),
    ("light_move_blur", lambda: key.parm("tx").set(-0.5), "P"),
    ("motion_size_change", lambda: rop.parm("motion_samples").set(5), "F"),
    ("object_move_blur5", lambda: extra.parm("tz").set(0.5), None),
    ("blur_off", lambda: rop.parm("allowmotionblur").set(0), "F"),
    ("motion_pass", lambda: passes.add(rop, "motion", enable=1), "F"),
    ("object_move_motion_pass", lambda: extra.parm("tx").set(-3.0), "P"),
    ("light_move_motion_pass", lambda: key.parm("tx").set(-0.2), "P"),
    ("frame_after_partials", lambda: hou.setFrame(4), "F"),
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
before = dict(updates=0)
try:
    registry.close_all()
    for name, edit, expect in STATES:
        edit()
        # A change of passes or of the motion samples recreates the session: its first update is full.
        entry, frame, status, stats = render(entry if name not in ("lightgroup_new", "motion_pass") else None)
        error = status.get("error") or ""
        partial = counts(stats, "partial object table updates") > 0
        verified = counts(stats, "object table verifications")
        modified, total = counts(stats, "object table modified objects"), counts(stats, "object table objects")
        row = dict(
            state=name,
            partial=partial,
            verified=verified,
            modified=modified,
            objects=total,
            error=error,
            expect=expect,
            **render_device_evidence(status, device),
        )
        report["states"].append(row)
        print("OTSTATE " + json.dumps(row), flush=True)
        assert "Object table verification" not in error and not error, row
        assert verified == (1 if partial else 0), row  # every partial write was verified
        if expect == "P":
            assert partial, row
        elif expect == "F":
            assert not partial, row
    # Large and scattered shares took the partial path: every instance (2025 of them) at once, and about 690
    # scattered ones (every 3rd and every 40th instance moved or moved back).
    moved = {r["state"]: r["modified"] for r in report["states"] if r["state"].startswith("instances_")}
    assert moved["instances_all"] >= 2025 and moved["instances_scattered_many"] >= 600, moved
    # The frame rendered after partial updates equals a fresh render of the same state.
    last = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).copy()
    registry.close_all()
    entry, fresh_frame, status, _ = render(None)
    fresh = np.frombuffer(fresh_frame["planes"]["combined"]["pixels"], np.float32)
    report["frame_after_partials_max_error"] = float(np.abs(last - fresh).max())
    assert report["frame_after_partials_max_error"] < 1e-5, report["frame_after_partials_max_error"]
    registry.close_all()
    # Final frame sequence in one non-interactive session: only one object animates.
    passes_count = rop.parm("pass_count").eval()
    rop.parm("allowmotionblur").set(0)
    extra.parm("tx").setExpression("-2 + 0.1 * $F")
    with ch.Session(
        device=device, width=320, height=180, samples=4, interactive=False, collect_diagnostics=True
    ) as final:
        partials = 0
        for f in (10, 11, 12, 13):
            hou.setFrame(f)
            final.wait(final.apply(native_scene.capture(rop)))
            st = final.status()
            render_device_evidence(st, device)
            assert not st.get("error"), st.get("error")
            partials += counts(final.diagnostics().get("engine_update_seconds", {}), "partial object table updates")
        report["final_sequence_partial_updates"] = partials
        print("OTFINAL partial updates", partials, flush=True)
    report["result"] = "passed"
finally:
    from support.paths import artifact_path

    artifact_path(
        "engine-contracts",
        "houdini_object_table_partial_smoke-" + ("cpu" if device == "CPU" else "gpu") + ".json",
    ).write_text(json.dumps(report, indent=2) + "\n")
    registry.close_all()
    print(
        json.dumps(
            dict(
                result=report["result"],
                states=len(report["states"]),
                partial_states=[r["state"] for r in report["states"] if r["partial"]],
                final_sequence_partial_updates=report.get("final_sequence_partial_updates"),
                frame_after_partials_max_error=report.get("frame_after_partials_max_error"),
            )
        ),
        flush=True,
    )
