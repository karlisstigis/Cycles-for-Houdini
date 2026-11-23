# SPDX-License-Identifier: Apache-2.0
"""Kept attribute tables (engine patch 0115) under CYCLES_VERIFY_ATTRIBUTE_TABLES.

A live session runs a chain of light, object, instance (with a per-instance colour),
visibility, holdout, shadow catcher, material, geometry, add/remove, motion blur, motion pass and frame edits
with the engine's verification on: after every update that kept the attribute tables, maps and requests and
wrote only the changed objects' values and offsets, the engine compares every device copy with the host and
runs the full attribute update, comparing its tables, map and kernel objects with the kept ones; any
difference is a session error. The test asserts that no error was reported, that every kept update was
verified, that the tables were kept for object and light edits and not where an input changed, and that the
frame after the edits equals a fresh render. Scene in memory, never saved.
"""

import argparse, json, os, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
import hou
import numpy as np

os.environ["CYCLES_VERIFY_ATTRIBUTE_TABLES"] = "1"
os.environ.pop("CYCLES_FULL_ATTRIBUTE_TABLES", None)
import cycles_houdini as ch
from cycles_houdini import device_selection, light_properties, materials, native_scene, object_properties, registry
from cycles_houdini.rop import passes

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
device = json.loads(device) if device.startswith("[") else device
report = dict(result="running", device=device, scope=__doc__, states=[])

obj = hou.node("/obj")
cam = obj.createNode("cam", "at_cam")
cam.setParms(dict(tx=0, ty=4, tz=9, rx=-22, resx=320, resy=180))
ground = obj.createNode("geo", "at_ground", run_init_scripts=False)
object_properties.install(ground)
ground.createNode("grid").setParms(dict(sizex=14, sizey=14, rows=10, cols=10))
sphere = obj.createNode("geo", "at_sphere", run_init_scripts=False)
object_properties.install(sphere)
s = sphere.createNode("sphere")
s.setParms(dict(type=2, freq=12, ty=1))
sv = sphere.createNode("attribwrangle")
sv.setInput(0, s)
sv.parm("snippet").set("v@Cd = set(0.5 + 0.5 * @P.x, 0.4, 0.5); v@v = set(2, 0, 0);")
sv.setRenderFlag(True)
sv.setDisplayFlag(True)
inst = obj.createNode("geo", "at_instances", run_init_scripts=False)
object_properties.install(inst)
isph = inst.createNode("sphere")
isph.setParms(dict(type=2, freq=3, scale=0.06))
ipts = inst.createNode("grid")
ipts.setParms(dict(sizex=6, sizey=6, rows=45, cols=45, tz=-3))
iw = inst.createNode("attribwrangle", "at_move")
iw.setInput(0, ipts)
iw.addSpareParmTuple(hou.IntParmTemplate("nmove", "nmove", 1))
iw.parm("snippet").set("@P.y = 0.1; if (@ptnum < chi('nmove')) @P.y += 0.5; v@v = set(0, 0, 1);")
icp = inst.createNode("copytopoints::2.0")
icp.setInput(0, isph)
icp.setInput(1, iw)
icp.parm("pack").set(1)  # packed instances: one engine object per copy
# Per-instance colour on the packed primitives (an instance attribute, as a Color SOP after the copy).
pcol = inst.createNode("attribwrangle", "at_instance_colour")
pcol.setInput(0, icp)
pcol.parm("class").set(pcol.parm("class").menuItems().index("primitive"))
pcol.addSpareParmTuple(hou.IntParmTemplate("tint", "tint", 1))
pcol.addSpareParmTuple(hou.IntParmTemplate("tintn", "tintn", 1))
pcol.parm("snippet").set(
    "v@Cd = set(0.2 + 0.6 * (@primnum % 7) / 7.0, 0.4, 0.3 + (@primnum < chi('tintn') ? 0.1 * chi('tint') : 0.0));"
)
pcol.setRenderFlag(True)
pcol.setDisplayFlag(True)
extra = obj.createNode("geo", "at_extra", run_init_scripts=False)
object_properties.install(extra)
extra.createNode("box").setParms(dict(tx=-2, ty=0.5, scale=0.5))
key = obj.createNode(light_properties.LIGHT_TYPE, "at_key")
key.setParms(dict(cycles_light_type="point", tx=-2, ty=4, tz=3, light_intensity=60))
fill = obj.createNode(light_properties.LIGHT_TYPE, "at_fill")
fill.setParms(dict(cycles_light_type="point", tx=2, ty=3, tz=4, light_intensity=20))
other = materials.create_material(name="at_other", shader="diffuse_bsdf")
# Instance colour read by the instances' material: per-instance Cd becomes an object attribute.
cdmat = materials.create_material(name="at_instance_colour", shader="diffuse_bsdf")
cd_attr = cdmat.createNode("cycles::attribute::1", "instance_colour")
cd_attr.parm("attribute").set("Cd")
cdmat.node("surface_shader").setNamedInput("color", cd_attr, "color")
rop = hou.node("/out").createNode("cycles_ipr", "at_rop")
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


# (name, edit, expectation): "K" tables kept, "k" kept if the attribute update runs, "F" full attribute update,
# None either
STATES = [
    ("init", init, "F"),
    ("light_move", lambda: key.parm("tx").set(-1.5), None),
    # Light edits do not run the attribute update at all (none expected); when one does, it must keep them.
    ("light_move2", lambda: key.parm("tx").set(-1.0), "k"),
    ("light_intensity", lambda: key.parm("light_intensity").set(80), "k"),
    ("object_move", lambda: sphere.parm("tx").set(0.5), "K"),
    ("object_move2", lambda: sphere.parm("tx").set(0.6), "K"),
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
    ("instances_few", lambda: iw.parm("nmove").set(50), None),
    ("instances_many", lambda: iw.parm("nmove").set(1200), None),
    ("instance_colours", lambda: pcol.parm("tint").set(3), None),
    # The instances' material reads their colour (a primitive attribute of the packed copies, as a Color
    # SOP after the copy would write): a material change (full), then colour edits of a few instances and
    # of all of them, with no geometry change. The integration's direct attribute updates change only the
    # objects' attribute values (not node sockets), so these updates keep the tables and write the values;
    # each is verified against the full update and its frame, which must differ from the previous one,
    # against a fresh render.
    ("instance_colour_material", lambda: inst.parm("shop_materialpath").set(cdmat.path()), "F"),
    ("object_move_after_colour_material", lambda: sphere.parm("tx").set(0.65), None),
    ("instance_colour_few", lambda: (pcol.parm("tintn").set(20), pcol.parm("tint").set(5)), "K"),
    ("object_move_after_colour_few", lambda: sphere.parm("tx").set(0.66), "K"),
    ("instances_move_after_colour_few", lambda: iw.parm("nmove").set(30), "K"),
    ("instance_colour_all", lambda: (pcol.parm("tintn").set(100000), pcol.parm("tint").set(7)), "K"),
    ("object_move_after_colour_all", lambda: sphere.parm("tx").set(0.67), "K"),
    ("instances_move_after_colour_all", lambda: iw.parm("nmove").set(40), "K"),
    ("instances_all", lambda: inst.parm("ty").set(0.2), None),
    ("object_move_after_instances", lambda: sphere.parm("tx").set(0.7), "K"),
    ("material_change", lambda: sphere.parm("shop_materialpath").set(other.path()), "F"),
    ("object_move_after_material", lambda: sphere.parm("tx").set(0.8), None),
    ("object_move_after_material2", lambda: sphere.parm("tx").set(0.85), "K"),
    ("geometry_edit", lambda: s.parm("freq").set(10), "F"),
    ("object_move_after_geometry", lambda: sphere.parm("tx").set(0.9), "K"),
    ("object_added", lambda: objects(BASE + [extra]), "F"),
    ("object_move_after_add", lambda: extra.parm("tx").set(-2.5), "K"),
    ("object_removed", lambda: objects([ground, inst, extra]), "F"),
    ("object_move_after_remove", lambda: extra.parm("tx").set(-2.4), "K"),
    ("restore_sphere", lambda: objects(BASE + [extra]), "F"),
    ("blur_on", lambda: rop.parm("allowmotionblur").set(1), "F"),
    ("object_move_blur", lambda: extra.parm("tz").set(0.3), None),
    ("object_move_blur2", lambda: extra.parm("tz").set(0.4), "K"),
    ("blur_off", lambda: rop.parm("allowmotionblur").set(0), "F"),
    ("motion_pass", lambda: passes.add(rop, "motion", enable=1), "F"),
    ("object_move_motion_pass", lambda: extra.parm("tx").set(-3.0), None),
    ("object_move_motion_pass2", lambda: extra.parm("tx").set(-3.1), "K"),
    ("frame_change", lambda: hou.setFrame(4), None),
]


def render(entry):
    cap = native_scene.capture(rop)
    if entry is None:
        entry = registry.for_rop(rop, native_scene.configuration(rop))
    frame = entry.session.wait(entry.apply(cap, hou.time()))
    status = entry.session.status()
    stats = entry.session.diagnostics().get("engine_update_seconds", {})
    return entry, frame, status, stats


COMPARE_FRESH = ("instance_colour_few", "instance_colour_all", "instances_move_after_colour_all")


def counts(stats, name):
    return int(round(stats.get("geometry/" + name, 0)))


entry = None
previous_pixels = None
try:
    registry.close_all()
    for name, edit, expect in STATES:
        edit()
        # A change of passes recreates the session: its first update is full.
        entry, frame, status, stats = render(entry if name != "motion_pass" else None)
        error = status.get("error") or ""
        kept = counts(stats, "kept attribute tables updates") > 0
        verified = counts(stats, "attribute table verifications")
        attributes_ran = "geometry/device_update (attributes)" in stats
        row = dict(
            state=name,
            kept=kept,
            verified=verified,
            attributes_ran=attributes_ran,
            error=error,
            expect=expect,
            direct_attribute_updates=status.get("direct_attribute_updates"),
        )
        report["states"].append(row)
        print("ATSTATE " + json.dumps(row), flush=True)
        assert "verification" not in error and not error, row
        assert verified == (1 if kept else 0), row
        pixels = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).copy()
        if name in ("instance_colour_few", "instance_colour_all"):
            # The colour edit is visible: the frame changed against the previous state.
            row["changed_pixels"] = int((np.abs(pixels - previous_pixels) > 1e-3).sum())
            print(
                "ATCHANGE "
                + json.dumps(
                    dict(
                        state=name,
                        changed=row["changed_pixels"],
                        stats=sorted(k for k in stats if not k.startswith("cancel/")),
                    )
                ),
                flush=True,
            )
            assert row["changed_pixels"] > 0, row
        previous_pixels = pixels
        if name in COMPARE_FRESH:
            # The edited colours are on screen: the frame equals a fresh render of the same state.
            shown = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).copy()
            registry.close_all()
            entry, fresh_frame, _, _ = render(None)  # a new session renders the same state from scratch
            fresh = np.frombuffer(fresh_frame["planes"]["combined"]["pixels"], np.float32)
            row["fresh_max_error"] = float(np.abs(shown - fresh).max())
            print("ATFRESH " + json.dumps(dict(state=name, error=row["fresh_max_error"])), flush=True)
            assert row["fresh_max_error"] < 1e-5, row
        if expect == "K":
            assert kept, row
        elif expect == "k":
            assert kept or not attributes_ran, row
        elif expect == "F":
            assert not kept, row
    assert sum(r["kept"] for r in report["states"]) >= 10, report["states"]
    last = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).copy()
    registry.close_all()
    entry, fresh_frame, status, _ = render(None)
    fresh = np.frombuffer(fresh_frame["planes"]["combined"]["pixels"], np.float32)
    report["frame_after_kept_max_error"] = float(np.abs(last - fresh).max())
    assert report["frame_after_kept_max_error"] < 1e-5, report["frame_after_kept_max_error"]
    report["result"] = "passed"
finally:
    from support.paths import artifact_path

    artifact_path(
        "engine-contracts",
        "houdini_attribute_tables_kept_smoke-" + ("cpu" if device.upper().startswith("CPU") else "gpu") + ".json",
    ).write_text(json.dumps(report, indent=2) + "\n")
    registry.close_all()
    print(
        json.dumps(
            dict(
                result=report["result"],
                states=len(report["states"]),
                kept_states=[r["state"] for r in report["states"] if r["kept"]],
                frame_after_kept_max_error=report.get("frame_after_kept_max_error"),
            )
        ),
        flush=True,
    )
