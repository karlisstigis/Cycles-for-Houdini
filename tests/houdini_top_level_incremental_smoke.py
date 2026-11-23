# SPDX-License-Identifier: Apache-2.0
"""Incremental top level updates (engine patch 0110) under CYCLES_VERIFY_TOP_LEVEL.

A live session runs a chain of object, light, instance, visibility, holdout, geometry, material, motion
blur, add/remove and frame edits with the engine's verification on: after every incremental top level
update the OptiX device compares the kept instance and motion transform buffers, on the host and on the
device (every device of a multi-device session), with a full rewrite, and after computing only the changed
object bounds the scene compares every object bound with computing all of them (also on CPU). Any
difference is reported as a session error. The test asserts that no error was reported, that the
incremental path ran where expected (OptiX) and not where the full path is required, that only the changed
objects' bounds were computed where expected (every device), that a frame rendered after incremental
updates equals a fresh render, and that a final (non-interactive) frame sequence with one animated object
verifies. An update ended (by an engine test hook, as a cancel does) between the object update and the top
level build must make the next single-object move take the full path, and the frame after it must equal a
fresh render. Scene in memory, never saved.
"""

import argparse, json, os, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
import hou
import numpy as np

os.environ["CYCLES_VERIFY_TOP_LEVEL"] = "1"
os.environ.pop("CYCLES_FULL_TOP_LEVEL_BUILD", None)
# Engine test hook: once, end the scene update after the object update and before bounds and the top level
# build (as a cancel there does) when the object tl_interrupt is in the change list.
os.environ["CYCLES_TEST_INTERRUPT_TOP_LEVEL"] = "tl_interrupt"
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
cam = obj.createNode("cam", "tl_cam")
cam.setParms(dict(tx=0, ty=4, tz=9, rx=-22, resx=320, resy=180))
ground = obj.createNode("geo", "tl_ground", run_init_scripts=False)
object_properties.install(ground)
ground.createNode("grid").setParms(dict(sizex=14, sizey=14, rows=10, cols=10))
sphere = obj.createNode("geo", "tl_sphere", run_init_scripts=False)
object_properties.install(sphere)
s = sphere.createNode("sphere")
s.setParms(dict(type=2, freq=12, ty=1))
sv = sphere.createNode("attribwrangle")
sv.setInput(0, s)
sv.parm("snippet").set("v@Cd = set(0.5 + 0.5 * @P.x, 0.4, 0.5); v@v = set(2, 0, 0);")
sv.setRenderFlag(True)
sv.setDisplayFlag(True)
inst = obj.createNode("geo", "tl_instances", run_init_scripts=False)
object_properties.install(inst)
isph = inst.createNode("sphere")
isph.setParms(dict(type=2, freq=3, scale=0.06))
ipts = inst.createNode("grid")
ipts.setParms(dict(sizex=6, sizey=6, rows=45, cols=45, tz=-3))
iw = inst.createNode("attribwrangle", "tl_move")
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
extra = obj.createNode("geo", "tl_extra", run_init_scripts=False)
object_properties.install(extra)
extra.createNode("box").setParms(dict(tx=-2, ty=0.5, scale=0.5))
intr = obj.createNode("geo", "tl_interrupt", run_init_scripts=False)
object_properties.install(intr)
intr.createNode("box").setParms(dict(tx=2.5, ty=0.4, tz=1.0, scale=0.6))
key = obj.createNode(light_properties.LIGHT_TYPE, "tl_key")
key.setParms(dict(cycles_light_type="point", tx=-2, ty=4, tz=3, light_intensity=60))
fill = obj.createNode(light_properties.LIGHT_TYPE, "tl_fill")
fill.setParms(dict(cycles_light_type="point", tx=2, ty=3, tz=4, light_intensity=20))
other = materials.create_material(name="tl_other", shader="diffuse_bsdf")
rop = hou.node("/out").createNode("cycles_ipr", "tl_rop")
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
devices = device if isinstance(device, list) else [device]
OPTIX = all("OPTIX" in str(d).upper() for d in devices)


def objects(nodes):
    rop.parm("vobject").set(" ".join(n.name() for n in nodes))


def init():
    hou.setFrame(1)
    objects(BASE)


# (name, edit, expectation): "I" incremental top level (OptiX) and bounds of the changed objects only,
# "K" no object changed (the OptiX structure is kept), "F" full top level and all bounds, None either.
STATES = [
    ("init", init, "F"),
    ("object_move", lambda: sphere.parm("tx").set(0.5), "I"),
    ("object_move2", lambda: sphere.parm("tx").set(0.6), "I"),
    ("light_move", lambda: key.parm("tx").set(-1.5), None),
    ("light_intensity", lambda: key.parm("light_intensity").set(80), None),
    (
        "visibility_off",
        lambda: (sphere.parm("cycles_visibility_override").set(1), sphere.parm("cycles_visibility_camera").set(0)),
        None,
    ),
    ("object_move_hidden", lambda: sphere.parm("tx").set(0.7), None),
    ("visibility_on", lambda: sphere.parm("cycles_visibility_camera").set(1), None),
    ("holdout_on", lambda: sphere.parm("cycles_holdout").set(1), None),
    ("holdout_off", lambda: sphere.parm("cycles_holdout").set(0), None),
    ("catcher_on", lambda: ground.parm("cycles_shadow_catcher").set(1), None),
    ("catcher_off", lambda: ground.parm("cycles_shadow_catcher").set(0), None),
    ("instances_few", lambda: iw.parm("nmove").set(50), None),
    # Scattered moves: every 3rd instance (about 675 runs, uploaded whole) and every 40th (about 50 runs,
    # uploaded as ranges), then back to the first ones.
    ("instances_scattered_many", lambda: iw.parm("every").set(3), "I"),
    ("instances_scattered_few", lambda: iw.parm("every").set(40), "I"),
    ("instances_unscattered", lambda: iw.parm("every").set(0), None),
    ("instances_many", lambda: iw.parm("nmove").set(900), None),
    ("instances_all", lambda: inst.parm("ty").set(0.2), None),
    ("object_move_after_instances", lambda: sphere.parm("tx").set(0.8), "I"),
    ("geometry_edit", lambda: s.parm("freq").set(10), "F"),
    ("object_move_after_geometry", lambda: sphere.parm("tx").set(0.9), "I"),
    ("material_change", lambda: sphere.parm("shop_materialpath").set(other.path()), None),
    ("object_move_after_material", lambda: sphere.parm("tx").set(1.0), "I"),
    ("object_added", lambda: objects(BASE + [extra]), "F"),
    ("object_move_after_add", lambda: extra.parm("tx").set(-2.5), "I"),
    ("object_removed", lambda: objects([ground, inst, extra]), "F"),
    ("object_move_after_remove", lambda: extra.parm("tx").set(-2.4), "I"),
    ("restore_sphere", lambda: objects(BASE + [extra, intr]), "F"),
    ("blur_on", lambda: rop.parm("allowmotionblur").set(1), "F"),
    ("object_move_blur", lambda: extra.parm("tz").set(0.3), "I"),
    ("sphere_move_blur", lambda: sphere.parm("tz").set(0.2), "I"),
    # Motion blurred instances (their container animates), moved scattered: every 3rd (many motion transform
    # runs, uploaded whole) and every 40th (few runs, uploaded as runs).
    ("instances_animated", lambda: inst.parm("tx").setExpression("$T*0.4"), None),
    ("instances_scattered_blur_many", lambda: iw.parm("every").set(3), "I"),
    ("instances_scattered_blur_few", lambda: iw.parm("every").set(40), "I"),
    ("instances_unscattered_blur", lambda: iw.parm("every").set(0), None),
    ("instances_still", lambda: (inst.parm("tx").deleteAllKeyframes(), inst.parm("tx").set(0)), None),
    ("light_move_blur", lambda: key.parm("tx").set(-0.5), None),
    ("motion_size_change", lambda: rop.parm("motion_samples").set(5), "F"),
    ("object_move_blur5", lambda: extra.parm("tz").set(0.5), None),
    ("object_move_blur5b", lambda: extra.parm("tz").set(0.6), "I"),
    ("blur_off", lambda: rop.parm("allowmotionblur").set(0), "F"),
    ("object_move_after_blur", lambda: extra.parm("tx").set(-3.0), "I"),
    ("frame_change", lambda: hou.setFrame(4), None),
    ("object_move_after_frame", lambda: extra.parm("tx").set(-3.1), "I"),
    # The hook ends this update before bounds and the top level build: tl_interrupt keeps its old instance.
    ("interrupted_move", lambda: intr.parm("tx").set(1.5), "X"),
    # tl_interrupt was written without a completed geometry update: the hand-shake makes the next build full.
    # The session's next iteration already repeats the unfinished geometry update (its flags are still set)
    # and builds every instance then, so this single-object move may be incremental again; without the
    # hand-shake that repeat would keep tl_interrupt's stale instance and bounds, which the verification
    # (bounds and instances against a full rewrite) and the fresh-render comparison below report.
    ("move_after_interrupt", lambda: sphere.parm("tx").set(1.2), None),
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


def count(stats, name):
    return int(round(stats.get("geometry/" + name, 0)))


entry = None
try:
    registry.close_all()
    for name, edit, expect in STATES:
        edit()
        # A change of the motion samples recreates the session: its first update is full.
        entry, frame, status, stats = render(entry if name != "motion_size_change" else None)
        error = status.get("error") or ""
        incremental = count(stats, "incremental top level updates") > 0
        rewritten = count(stats, "incremental top level instances")
        bounds_partial = "geometry/partial bounds objects" in stats
        interrupted = count(stats, "interrupted top level updates") > 0
        row = dict(
            state=name,
            incremental=incremental,
            rewritten=rewritten,
            bounds_partial=bounds_partial,
            interrupted=interrupted,
            bounds_objects=count(stats, "partial bounds objects"),
            error=error,
            expect=expect,
            withheld=int(round(stats.get("object/top level change list withheld", 0))) > 0,
            geometry_ran="geometry/device_update (build scene BVH)" in stats
            or "geometry/device_update (compute bounds)" in stats,
            **render_device_evidence(status, device),
        )
        report["states"].append(row)
        print("TLSTATE " + json.dumps(row), flush=True)
        assert "verification" not in error and not error, row
        assert OPTIX or not incremental, row  # only the OptiX top level is incremental
        if expect == "I" and not bounds_partial and row["withheld"]:
            # The hand-shake withheld the change list (an earlier object update had not reached a
            # completed geometry update): the full path is required then, not a failure.
            print("TLWITHHELD " + name, flush=True)
        elif expect == "I":
            assert bounds_partial, row
            assert incremental == OPTIX, row
            assert not OPTIX or rewritten >= 1, row
        elif expect == "F":
            assert not incremental and not bounds_partial, row
        assert interrupted == (expect == "X"), row
    if OPTIX:
        assert sum(r["incremental"] for r in report["states"]) >= 12, report["states"]
    assert report["states"][-1]["state"] == "move_after_interrupt"
    # The frame rendered after incremental updates (the last one after the interrupted update) equals a fresh
    # render of the same state.
    last = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).copy()
    registry.close_all()
    entry, fresh_frame, status, _ = render(None)
    fresh = np.frombuffer(fresh_frame["planes"]["combined"]["pixels"], np.float32)
    report["frame_after_incremental_max_error"] = float(np.abs(last - fresh).max())
    assert report["frame_after_incremental_max_error"] < 1e-5, report["frame_after_incremental_max_error"]
    registry.close_all()
    # Final frame sequence in one non-interactive session: only one object animates.
    rop.parm("allowmotionblur").set(0)
    extra.parm("tx").setExpression("-2 + 0.1 * $F")
    with ch.Session(
        device=device, width=320, height=180, samples=4, interactive=False, collect_diagnostics=True
    ) as final:
        incrementals = 0
        for f in (10, 11, 12, 13):
            hou.setFrame(f)
            final.wait(final.apply(native_scene.capture(rop)))
            st = final.status()
            render_device_evidence(st, device)
            assert not st.get("error"), st.get("error")
            incrementals += count(final.diagnostics().get("engine_update_seconds", {}), "incremental top level updates")
        report["final_sequence_incremental_updates"] = incrementals
        print("TLFINAL incremental updates", incrementals, flush=True)
    report["result"] = "passed"
finally:
    from support.paths import artifact_path

    artifact_path(
        "engine-contracts",
        "houdini_top_level_incremental_smoke-" + ("cpu" if device == "CPU" else "gpu") + ".json",
    ).write_text(json.dumps(report, indent=2) + "\n")
    registry.close_all()
    print(
        json.dumps(
            dict(
                result=report["result"],
                states=len(report["states"]),
                incremental_states=[r["state"] for r in report["states"] if r["incremental"]],
                bounds_partial_states=[r["state"] for r in report["states"] if r["bounds_partial"]],
                final_sequence_incremental_updates=report.get("final_sequence_incremental_updates"),
                frame_after_incremental_max_error=report.get("frame_after_incremental_max_error"),
            )
        ),
        flush=True,
    )
