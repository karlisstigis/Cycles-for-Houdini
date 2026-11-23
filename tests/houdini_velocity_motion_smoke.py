# SPDX-License-Identifier: Apache-2.0
"""Velocity mode (task M1): static geometry with point v renders blurred along v with no SOP
cooked at other shutter times, for unpacked points/curves, packed primitives (v and w on the
packed points) and point instances. Each velocity render must match a render of the same
geometry actually moving by v (deformation or transform mode). After a render with motion
blur in each Deformation mode, a capture with nothing changed and a material colour edit
re-send no geometry and no objects; a SOP edit, a frame change and a render flag change still
update.

Acceleration, Velocity if Available and the object override (task M2): points, curves, a
mesh, packed primitives and point instances with v and accel render like the same geometry
moving on that parabola, with no shutter cook; without accel Acceleration equals Velocity;
Velocity if Available cooks only the object without v; From ROP follows the ROP and each
object choice wins; an explicit Velocity without v reports one warning; scenes saved with the
former tokens keep their setting. Generated scene only, CPU, small resolution."""

import argparse, json, math, os, sys, tempfile, time, traceback
import numpy as np
import hou

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pass_fixtures as pass_fixture
import cycles_houdini as ch
from cycles_houdini import native_scene, materials, object_properties
from cycles_houdini.device_selection import set_devices
from support.paths import artifacts_root

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
args = parser.parse_args()
out = artifacts_root() / "stage2/motion/velocity-motion"
out.mkdir(parents=True, exist_ok=True)
W, H, SAMPLES = 160, 96, 32
SPEED = 48.0  # Units per second: one unit across a half-frame shutter at 24 FPS.
SPIN = 48.0 * math.pi / 4  # Radians per second: 45 degrees across the shutter.
ACCEL = 0.8 * 96.0**2  # Units per second squared: 0.4 units off the straight line at shutter open and close.
results, failures = [], []


def check(name, ok, **details):
    results.append(dict(name=name, ok=bool(ok), **details))
    print(("ok: " if ok else "FAIL: ") + name, json.dumps(details, default=str)[:600], flush=True)
    if not ok:
        failures.append(name)


hou.setFps(24)
hou.setFrame(10)
T0 = hou.time()
root = hou.node("/obj")
mat = materials.create_material(name="velocity_motion_surface", shader="emission")
emission = mat.node("surface_shader")
pass_fixture.set_parameters(emission, dict(strength=1))
phase = mat.createNode("cycles::attribute::1", "phase_field")
phase.parm("attribute").set("phase")
colour = mat.createNode("cycles::combine_xyz::1", "phase_colour")
colour.setNamedInput("x", phase, "fac")
colour.parm("y").set(0.6)
colour.parm("z").set(0.3)
emission.setNamedInput("color", colour, "vector")


def geo(name):
    node = root.createNode("geo", name)
    for child in node.children():
        child.destroy()
    object_properties.install(node)
    node.parm("shop_materialpath").set(mat.path())
    return node


def wrangle(parent, name, snippet, run_over=0, source=None):
    node = parent.createNode("attribwrangle", name)
    node.parm("class").set(run_over)
    if source is not None:
        node.setInput(0, source)
    for spare, default in (("moving", 0.0), ("speed", SPEED), ("spin", 0.0), ("t0", T0)):
        node.addSpareParmTuple(hou.FloatParmTemplate(spare, spare, 1, default_value=(default,)))
    node.parm("snippet").set(snippet)
    return node


# Loose points (spheres) and open curves. @Time makes the SOP time dependent in every case.
LOOSE = """float d = (@Time - chf("t0")) * chf("moving");
vector v = set(chf("speed"), 0, 0);
for (int i = 0; i < 3; i++) {
    int p = addpoint(0, set(-2.4 + d * chf("speed"), -0.8 + i * 0.8, 0));
    setpointattrib(0, "v", p, v); setpointattrib(0, "phase", p, 0.8);
    int a = addpoint(0, set(-1.7 + d * chf("speed"), -1.0 + i * 0.8, 0));
    int b = addpoint(0, set(-1.7 + d * chf("speed"), -0.6 + i * 0.8, 0));
    setpointattrib(0, "v", a, v); setpointattrib(0, "v", b, v);
    setpointattrib(0, "phase", a, 0.8); setpointattrib(0, "phase", b, 0.8);
    addprim(0, "polyline", a, b);
}"""
loose_obj = geo("loose")
loose = wrangle(loose_obj, "points_and_curves", LOOSE)
loose.setRenderFlag(True)
loose.setDisplayFlag(True)

# Packed boxes on three points. Velocity: v/w on the packed points. Reference: the packed
# points move by v and the packed transforms turn by w about the pivot.
packed_obj = geo("packed")
box = packed_obj.createNode("box", "bar")
box.parmTuple("size").set((0.8, 0.2, 0.2))
pack = packed_obj.createNode("pack", "pack_bar")
pack.setInput(0, box)
targets = wrangle(
    packed_obj,
    "targets",
    "for (int i = 0; i < 3; i++) { int p = addpoint(0, set(0.2, -0.8 + i * 0.8, 0)); "
    'setpointattrib(0, "N", p, {0, 0, 1}); setpointattrib(0, "up", p, {0, 1, 0}); }',
)
copies = packed_obj.createNode("copytopoints::2.0", "copies")
copies.setInput(0, pack)
copies.setInput(1, targets)
PACKED = """float d = (@Time - chf("t0"));
f@phase = 0.7;
if (chf("moving") > 0) {
    @P.x += d * chf("speed");
    matrix3 m = primintrinsic(0, "transform", @ptnum);
    rotate(m, d * chf("spin"), {0, 0, 1});
    setprimintrinsic(0, "transform", @ptnum, m);
} else {
    v@v = set(chf("speed"), 0, 0);
    if (chf("spin") != 0) v@w = set(0, 0, chf("spin"));
}"""
packed_motion = wrangle(packed_obj, "packed_velocity", PACKED, run_over=2, source=copies)
packed_shift = packed_obj.createNode("xform", "render_flag_target")
packed_shift.setInput(0, packed_motion)
packed_shift.parm("ty").set(0.3)
packed_motion.setRenderFlag(True)
packed_motion.setDisplayFlag(True)

# Point instancing (Instance OBJ). Velocity: ptmotionblur velocity with v on static points.
proto = geo("instance_prototype")
proto_box = proto.createNode("box", "cube")
proto_box.parmTuple("size").set((0.3, 0.3, 0.3))
proto_box.setRenderFlag(True)
proto_box.setDisplayFlag(True)
proto.setDisplayFlag(False)
inst = root.createNode("instance", "instancer")
for child in inst.children():
    child.destroy()
object_properties.install(inst)
inst_points = wrangle(
    inst,
    "points",
    'float d = (@Time - chf("t0")) * chf("moving"); for (int i = 0; i < 3; i++) { '
    'int p = addpoint(0, set(1.8 + d * chf("speed"), -0.8 + i * 0.8, 0)); '
    'setpointattrib(0, "v", p, set(chf("speed"), 0, 0)); setpointattrib(0, "phase", p, 0.6); }',
)
inst_points.setRenderFlag(True)
inst_points.setDisplayFlag(True)
inst.parm("instancepath").set(proto.path())
inst.parm("ptinstance").set("fast")
inst.parm("shop_materialpath").set(mat.path())

# Acceleration (task M2): points, a curve and a mesh quad on the parabola P + v d + 1/2 a d^2.
# Attribute modes read v and accel of the frame; "moving" puts the geometry on the path itself.
CURVED = """float d = (@Time - chf("t0")) * chf("moving");
vector v0 = set(chf("speed"), 0, 0), a = set(0, chf("accel"), 0);
vector off = v0 * d + 0.5 * a * d * d, vel = v0 + a * d;
int pts[];
for (int i = 0; i < 2; i++) append(pts, addpoint(0, set(-2.4, -0.9 + i * 0.6, 0) + off));
append(pts, addpoint(0, set(-1.4, -1.0, 0) + off)); append(pts, addpoint(0, set(-1.4, -0.4, 0) + off));
addprim(0, "polyline", pts[2], pts[3]);
vector quad[] = array(set(0.2, -1.0, 0), set(0.8, -1.0, 0), set(0.8, -0.4, 0), set(0.2, -0.4, 0));
foreach (vector q; quad) append(pts, addpoint(0, q + off));
addprim(0, "poly", pts[4], pts[5], pts[6], pts[7]);
foreach (int p; pts) {
    setpointattrib(0, "v", p, vel); setpointattrib(0, "phase", p, 0.9);
    if (chf("has_accel") > 0) setpointattrib(0, "accel", p, a);
}"""
curved_obj = geo("curved")
curved = wrangle(curved_obj, "curved_motion", CURVED)
for spare, default in (("accel", ACCEL), ("has_accel", 1.0)):
    curved.addSpareParmTuple(hou.FloatParmTemplate(spare, spare, 1, default_value=(default,)))
curved.setRenderFlag(True)
curved.setDisplayFlag(True)
# Packed bars on the same parabola, turning by w; reference: the packed points and transforms move.
arc_obj = geo("arc")
arc_box = arc_obj.createNode("box", "bar")
arc_box.parmTuple("size").set((0.8, 0.2, 0.2))
arc_pack = arc_obj.createNode("pack", "pack_bar")
arc_pack.setInput(0, arc_box)
arc_targets = wrangle(
    arc_obj,
    "targets",
    "for (int i = 0; i < 2; i++) { int p = addpoint(0, set(0.2, 0.2 + i * 0.8, 0)); "
    'setpointattrib(0, "N", p, {0, 0, 1}); setpointattrib(0, "up", p, {0, 1, 0}); }',
)
arc_copies = arc_obj.createNode("copytopoints::2.0", "copies")
arc_copies.setInput(0, arc_pack)
arc_copies.setInput(1, arc_targets)
ARC = """float d = (@Time - chf("t0"));
f@phase = 0.5;
vector v0 = set(chf("speed"), 0, 0), a = set(0, chf("accel"), 0);
if (chf("moving") > 0) {
    @P += v0 * d + 0.5 * a * d * d;
    matrix3 m = primintrinsic(0, "transform", @ptnum);
    rotate(m, d * chf("spin"), {0, 0, 1});
    setprimintrinsic(0, "transform", @ptnum, m);
} else {
    v@v = v0; v@accel = a;
    if (chf("spin") != 0) v@w = set(0, 0, chf("spin"));
}"""
arc = wrangle(arc_obj, "arc_motion", ARC, run_over=2, source=arc_copies)
arc.addSpareParmTuple(hou.FloatParmTemplate("accel", "accel", 1, default_value=(ACCEL,)))
arc.parm("spin").set(SPIN)
arc.setRenderFlag(True)
arc.setDisplayFlag(True)
# Point instances on the parabola: the instancer itself chooses Acceleration.
arc_inst = root.createNode("instance", "arc_instancer")
for child in arc_inst.children():
    child.destroy()
object_properties.install(arc_inst)
arc_points = wrangle(
    arc_inst,
    "points",
    'float d = (@Time - chf("t0")) * chf("moving"); vector v0 = set(chf("speed"), 0, 0), a = set(0, chf("accel"), 0); '
    "for (int i = 0; i < 2; i++) { "
    "int p = addpoint(0, set(1.8, 0.2 + i * 0.8, 0) + v0 * d + 0.5 * a * d * d); "
    'setpointattrib(0, "v", p, v0 + a * d); setpointattrib(0, "accel", p, a); setpointattrib(0, "phase", p, 0.4); '
    # Without N or orient Houdini aims an instance along v, which turns as v changes on
    # the parabola; attribute modes hold the frame's orientation (only w turns it).
    'setpointattrib(0, "N", p, {0, 0, 1}); setpointattrib(0, "up", p, {0, 1, 0}); }',
)
arc_points.addSpareParmTuple(hou.FloatParmTemplate("accel", "accel", 1, default_value=(ACCEL,)))
arc_points.setRenderFlag(True)
arc_points.setDisplayFlag(True)
arc_inst.parm("instancepath").set(proto.path())
arc_inst.parm("ptinstance").set("fast")
arc_inst.parm("shop_materialpath").set(mat.path())
# Moving geometry without v: Velocity if Available must cook it, and only it.
plain_obj = geo("plain")
plain = wrangle(
    plain_obj,
    "no_velocity",
    'float d = (@Time - chf("t0")); for (int i = 0; i < 2; i++) { '
    'int p = addpoint(0, set(-0.6 + d * chf("speed"), 0.6 + i * 0.6, 0)); setpointattrib(0, "phase", p, 0.2); }',
)
plain.setRenderFlag(True)
plain.setDisplayFlag(True)
# An object with no points at all (particles born on a later frame).
empty_obj = geo("empty")
empty = wrangle(empty_obj, "no_points", "int unused = 0;")
empty.setRenderFlag(True)
empty.setDisplayFlag(True)

cam = root.createNode("cam", "camera")
pass_fixture.set_parameters(cam, dict(tz=6, resx=W, resy=H))
cam.parm("projection").set("ortho")
cam.parm("orthowidth").set(6)
cam.parm("shutter").set(0.5)
rop = hou.node("/out").createNode("cycles_ipr", "velocity_rop")
set_devices(rop, args.device)
pass_fixture.set_parameters(
    rop,
    dict(
        source_mode="objects",
        objectroot="/obj",
        camera=cam.path(),
        resx=W,
        resy=H,
        samples=SAMPLES,
        ipr_samples=SAMPLES,
        adaptive_sampling=0,
        ipr_adaptive_sampling=0,
        use_denoise=0,
        ipr_use_denoise=0,
        use_material_assignments=1,
        allowmotionblur=1,
        motion_samples=5,
        geometry_motion="velocity",
        point_radius=0.12,
        curve_width=0.12,
        seed=1,
        animate_seed=0,
    ),
)
rop.parmTuple("background").set((0, 0, 0))


def render(label, objects, session, mode=None, blur=True):
    rop.parm("vobject").set(objects)
    if mode:
        rop.parm("geometry_motion").set(mode)
    rop.parm("allowmotionblur").set(int(blur))
    started = time.perf_counter()
    update = native_scene.capture(rop)
    frame = session.wait(session.apply(update))
    status = dict(session.status())
    status["test_seconds"] = time.perf_counter() - started
    plane = frame["planes"]["combined"]
    image = np.frombuffer(plane["pixels"], np.float32).reshape(H, W, plane["channels"]).copy()
    np.save(out / (label + ".npy"), image)
    return image, status


def difference(a, b):
    covered = (a[:, :, :3].sum(2) > 0.01) | (b[:, :, :3].sum(2) > 0.01)
    return float(np.mean(np.abs(a - b)[covered][:, :3])) if covered.sum() > 100 else None, int(covered.sum())


def lit(image):
    return int((image[:, :, :3].sum(2) > 0.05).sum())


def compare(label, velocity, reference, static, tolerance=0.03):
    same, covered = difference(velocity, reference)
    # Blur spreads the same emission over more pixels than the static render.
    spread = lit(velocity) / max(1, lit(static))
    check(
        label,
        same is not None and same < tolerance and spread > 1.3,
        velocity_vs_moving=same,
        lit_velocity_over_static=spread,
        covered=covered,
    )


def no_shutter_cooks(label, status):
    check(
        label + ": no SOP cooked at shutter times",
        status["motion_cooks"] == 0 and status["cooking_seconds"] < 0.05 and not status["error"],
        motion_cooks=status["motion_cooks"],
        cooking_seconds=status["cooking_seconds"],
        diagnostics=status.get("source_diagnostics"),
    )


report = dict(result="running", device=args.device)
try:
    with ch.Session(
        device=args.device, width=W, height=H, samples=SAMPLES, collect_diagnostics=True, use_texture_cache=False
    ) as session:
        # (a) unpacked points and curves.
        static, _ = render("loose_static", "loose", session, "velocity", blur=False)
        velocity, status = render("loose_velocity", "loose", session, "velocity")
        no_shutter_cooks("unpacked points/curves", status)
        # Linear velocity needs shutter open, centre and close only: five configured
        # samples publish the same point/curve motion keys as three.
        rop.parm("motion_samples").set(3)
        _, three = render("loose_velocity_3", "loose", session, "velocity")
        rop.parm("motion_samples").set(5)
        _, five = render("loose_velocity_5", "loose", session, "velocity")
        check(
            "point/curve velocity uses 3 motion steps whatever the sample count",
            three["owned_geometry_bytes"] == five["owned_geometry_bytes"] > 0,
            three=three["owned_geometry_bytes"],
            five=five["owned_geometry_bytes"],
        )
        loose.parm("moving").set(1)
        moving, _ = render("loose_deformation", "loose", session, "deformation")
        loose.parm("moving").set(0)
        compare("static points/curves with v blur like the same geometry moving by v", velocity, moving, static)
        # (b) packed primitives: v, then w.
        static, _ = render("packed_static", "packed", session, "velocity", blur=False)
        velocity, status = render("packed_velocity", "packed", session, "velocity")
        no_shutter_cooks("packed primitives with v", status)
        packed_motion.parm("moving").set(1)
        moving, _ = render("packed_transform", "packed", session, "transform")
        packed_motion.parm("moving").set(0)
        compare(
            "static packed primitives with packed-point v blur like moving packed primitives", velocity, moving, static
        )
        # (d) packed angular velocity w about the pivot.
        packed_motion.parm("speed").set(0)
        packed_motion.parm("spin").set(SPIN)
        velocity, status = render("packed_spin_velocity", "packed", session, "velocity")
        no_shutter_cooks("packed primitives with w", status)
        packed_motion.parm("moving").set(1)
        moving, _ = render("packed_spin_transform", "packed", session, "transform")
        packed_motion.parm("moving").set(0)
        compare(
            "packed-point w turns packed primitives about their pivot like turning transforms", velocity, moving, static
        )
        packed_motion.parm("speed").set(SPEED)
        packed_motion.parm("spin").set(0)
        # (b) point instances.
        inst.parm("ptmotionblur").set("velocity")
        static, _ = render("instances_static", "instancer", session, "velocity", blur=False)
        velocity, status = render("instances_velocity", "instancer", session, "velocity")
        no_shutter_cooks("point instances with v", status)
        inst.parm("ptmotionblur").set("deform")
        inst_points.parm("moving").set(1)
        moving, _ = render("instances_deformation", "instancer", session, "deformation")
        inst_points.parm("moving").set(0)
        inst.parm("ptmotionblur").set("velocity")
        compare("static point instances with v blur like moving point instances", velocity, moving, static)

        # (e) identity across captures, per Geometry Motion mode.
        def counters(status):
            return {k: status[k] for k in ("extractions", "adapter_full_object_updates", "adapter_pose_updates")}

        for mode in ("velocity", "transform", "deformation"):
            render("identity_" + mode + "_first", "loose packed", session, mode)
            for label, action, expect_update in (
                ("no change", lambda: None, False),
                ("material colour edit", lambda: colour.parm("z").set(colour.parm("z").eval() + 0.1), False),
                ("no change after the edit", lambda: None, False),
                ("SOP parameter edit", lambda: box.parm("sizex").set(box.parm("sizex").eval() + 0.1), True),
            ):
                before = counters(session.status())
                action()
                _, status = render("identity_" + mode, "loose packed", session, mode)
                after = counters(status)
                extracted = after["extractions"] - before["extractions"]
                updated = after["adapter_full_object_updates"] + after["adapter_pose_updates"]
                # A geometry edit re-sends geometry; deformation keeps its resource slot, so
                # its objects need no update of their own.
                ok = extracted > 0 if expect_update else (extracted == 0 and updated == 0)
                check(
                    mode + ": " + label + (" updates" if expect_update else " re-sends nothing"),
                    ok,
                    extracted=extracted,
                    full_updates=after["adapter_full_object_updates"],
                    pose_updates=after["adapter_pose_updates"],
                    motion_cooks=status["motion_cooks"],
                    seconds=status["test_seconds"],
                )
            packed_motion.parm("moving").set(1)
            render("identity_" + mode + "_moving", "loose packed", session, mode)
            hou.setFrame(hou.frame() + 1)
            _, status = render("identity_" + mode + "_frame", "loose packed", session, mode)
            check(
                mode + ": a frame change of moving geometry updates",
                status["adapter_full_object_updates"] + status["adapter_pose_updates"] > 0,
                full_updates=status["adapter_full_object_updates"],
                pose_updates=status["adapter_pose_updates"],
            )
            hou.setFrame(10)
            packed_motion.parm("moving").set(0)
            packed_shift.setRenderFlag(True)
            _, status = render("identity_" + mode + "_flag", "loose packed", session, mode)
            check(
                mode + ": a render flag change updates",
                status["adapter_full_object_updates"] + status["adapter_pose_updates"] > 0,
                full_updates=status["adapter_full_object_updates"],
                pose_updates=status["adapter_pose_updates"],
            )
            packed_motion.setRenderFlag(True)

        # (f) Acceleration (task M2): v and accel of the frame against true geometry samples.
        def codes(status, code):
            return [d for d in status.get("source_diagnostics") or [] if d.get("code") == code]

        static, _ = render("curved_static", "curved", session, "acceleration", blur=False)
        accelerated, status = render("curved_acceleration", "curved", session, "acceleration")
        no_shutter_cooks("Acceleration points/curves/mesh", status)
        straight, _ = render("curved_velocity", "curved", session, "velocity")
        curved.parm("moving").set(1)
        moving, _ = render("curved_geometry_samples", "curved", session, "deformation")
        curved.parm("moving").set(0)
        compare(
            "points/curves/mesh with v and accel blur like the same geometry on the parabola",
            accelerated,
            moving,
            static,
        )
        bent, _ = difference(accelerated, straight)
        check("accel bends the blur away from plain Velocity", bent is not None and bent > 0.03, mean_difference=bent)
        curved.parm("has_accel").set(0)
        rop.parm("motion_samples").set(5)
        no_accel, status_a = render("curved_acceleration_no_accel", "curved", session, "acceleration")
        no_accel_v, status_v = render("curved_velocity_no_accel", "curved", session, "velocity")
        check(
            "Acceleration without accel renders exactly as Velocity",
            float(np.abs(no_accel - no_accel_v).max()) == 0
            and status_a["owned_geometry_bytes"] == status_v["owned_geometry_bytes"]
            and status_a["motion_cooks"] == 0,
            max_difference=float(np.abs(no_accel - no_accel_v).max()),
            bytes=(status_a["owned_geometry_bytes"], status_v["owned_geometry_bytes"]),
        )
        check(
            "ROP Acceleration without accel adds no message (only an explicit object choice does)",
            not codes(status_a, "motion.missing_acceleration") and not codes(status_a, "motion.missing_velocity"),
            diagnostics=status_a.get("source_diagnostics"),
        )
        curved_obj.parm("cycles_geometry_motion").set("acceleration")
        _, status = render("curved_explicit_no_accel", "curved", session, "transform")
        notes = codes(status, "motion.missing_acceleration")
        check(
            "an object choosing Acceleration without accel gets one note",
            len(notes) == 1 and notes[0].get("severity") == "info",
            diagnostics=status.get("source_diagnostics"),
        )
        curved_obj.parm("cycles_geometry_motion").set("inherit")
        curved.parm("has_accel").set(1)

        # Packed primitives on the parabola while turning by w.
        static, _ = render("arc_static", "arc", session, "acceleration", blur=False)
        accelerated, status = render("arc_acceleration", "arc", session, "acceleration")
        no_shutter_cooks("Acceleration packed primitives", status)
        arc.parm("moving").set(1)
        moving, _ = render("arc_transform", "arc", session, "transform")
        arc.parm("moving").set(0)
        compare(
            "packed primitives with v, w and accel blur like packed primitives on the arc", accelerated, moving, static
        )

        # Point instances: the Instance OBJ's own Deformation choice wins over its Point Motion Blur.
        arc_inst.parm("ptmotionblur").set("velocity")
        arc_inst.parm("cycles_geometry_motion").set("acceleration")
        static, _ = render("arc_instances_static", "arc_instancer", session, "transform", blur=False)
        accelerated, status = render("arc_instances_acceleration", "arc_instancer", session, "transform")
        no_shutter_cooks("Acceleration point instances", status)
        arc_inst.parm("cycles_geometry_motion").set("deformation")
        arc_points.parm("moving").set(1)
        moving, _ = render("arc_instances_samples", "arc_instancer", session, "transform")
        arc_points.parm("moving").set(0)
        arc_inst.parm("cycles_geometry_motion").set("inherit")
        compare("point instances with v and accel blur like instances on the parabola", accelerated, moving, static)

        # (g) Velocity if Available: per object, cooks only the object without v.
        # A frame change before each capture: both SOPs cook afresh, so cook counts are comparable.
        def available(label, objects, frame):
            hou.setFrame(frame)
            counts = loose.cookCount(), plain.cookCount()
            image, status = render(label, objects, session, "velocity_if_available")
            hou.setFrame(10)
            return image, status, loose.cookCount() - counts[0], plain.cookCount() - counts[1]

        _, alone, _, _ = available("available_loose", "loose", 11)
        _, both, loose_cooks, plain_cooks = available("available_both", "loose plain", 12)
        check(
            "Velocity if Available cooks only the object without v",
            alone["motion_cooks"] == 0 and both["motion_cooks"] > 0 and loose_cooks <= 1 and plain_cooks >= 3,
            loose_alone=alone["motion_cooks"],
            both=both["motion_cooks"],
            loose_sop_cooks=loose_cooks,
            plain_sop_cooks=plain_cooks,
        )
        reference, _ = render("available_reference_samples", "plain", session, "deformation")
        velocity_loose, _ = render("available_velocity_loose", "loose", session, "velocity")
        available_loose, _ = render("available_loose_image", "loose", session, "velocity_if_available")
        available_plain, _ = render("available_plain_image", "plain", session, "velocity_if_available")
        check(
            "Velocity if Available renders each object as Velocity or Geometry Samples",
            float(np.abs(available_loose - velocity_loose).max()) == 0
            and float(np.abs(available_plain - reference).max()) == 0,
            loose=float(np.abs(available_loose - velocity_loose).max()),
            plain=float(np.abs(available_plain - reference).max()),
        )
        check(
            "Velocity if Available reports nothing for geometry without v",
            not codes(both, "motion.missing_velocity"),
            diagnostics=both.get("source_diagnostics"),
        )

        # (h) From ROP follows the ROP; the object's own choice wins either way.
        rop_velocity, _ = render("rop_velocity", "loose", session, "velocity")
        rop_off, _ = render("rop_off", "loose", session, "transform")
        loose_obj.parm("cycles_geometry_motion").set("velocity")
        object_velocity, status = render("object_velocity_over_rop_off", "loose", session, "transform")
        loose_obj.parm("cycles_geometry_motion").set("transform")
        object_off, _ = render("object_off_over_rop_velocity", "loose", session, "velocity")
        loose_obj.parm("cycles_geometry_motion").set("inherit")
        check(
            "an object Deformation choice wins over the ROP, From ROP follows it",
            float(np.abs(object_velocity - rop_velocity).max()) == 0
            and float(np.abs(object_off - rop_off).max()) == 0
            and float(np.abs(rop_velocity - rop_off).max()) > 0,
            velocity=float(np.abs(object_velocity - rop_velocity).max()),
            off=float(np.abs(object_off - rop_off).max()),
        )
        check(
            "an object choosing Velocity with v gets no missing-velocity message",
            not codes(status, "motion.missing_velocity"),
            diagnostics=status.get("source_diagnostics"),
        )

        # (i) Velocity chosen on an object without v: one warning naming it; From ROP: none.
        _, inherited = render("missing_inherited", "plain", session, "velocity")
        plain_obj.parm("cycles_geometry_motion").set("velocity")
        _, explicit = render("missing_explicit", "plain", session, "transform")
        _, again = render("missing_explicit_again", "plain", session, "transform")
        plain_obj.parm("cycles_geometry_motion").set("inherit")
        warnings = codes(explicit, "motion.missing_velocity")
        check(
            "an object choosing Velocity without v reports one warning naming it",
            len(warnings) == 1
            and plain_obj.path() in warnings[0].get("source", "")
            and not codes(inherited, "motion.missing_velocity")
            and len(codes(again, "motion.missing_velocity")) == 1
            and explicit["motion_cooks"] == 0,
            explicit=warnings,
            inherited=codes(inherited, "motion.missing_velocity"),
        )

        # (i2) Velocity chosen on an object that has no points yet: nothing to blur, no warning.
        empty_obj.parm("cycles_geometry_motion").set("velocity")
        _, without_points = render("missing_empty", "empty", session, "transform")
        _, both_objects = render("missing_empty_and_plain", "empty plain", session, "transform")
        empty_obj.parm("cycles_geometry_motion").set("inherit")
        check(
            "an object without points choosing Velocity reports no missing-velocity warning",
            not codes(without_points, "motion.missing_velocity") and not codes(both_objects, "motion.missing_velocity"),
            empty=codes(without_points, "motion.missing_velocity"),
            both=codes(both_objects, "motion.missing_velocity"),
            points=len(empty.geometry().points()),
        )

    # (j) Scenes saved with the former tokens and the former object menu keep their setting.
    from cycles_houdini.scene import instancing

    folder = tempfile.mkdtemp(prefix="cycles_m2_tokens_")
    now = hou.time()

    def former_menu(node):
        """The object menu as saved before task M2: Geometry Motion, four choices, version 21."""
        group = node.parmTemplateGroup()
        old = group.find("cycles_geometry_motion").clone()
        old.setLabel("Geometry Motion")
        old.setMenuItems(("inherit", "transform", "velocity", "deformation"))
        old.setMenuLabels(("Use ROP", "Transform Only", "Velocity", "Deformation"))
        old.setConditional(hou.parmCondType.HideWhen, "")
        group.replace("cycles_geometry_motion", old)
        tab = object_properties.properties_folder(group)
        tab.setTags(dict(tab.tags(), cycles_properties_version="21"))
        group.replace(tab.name(), tab)
        node.setParmTemplateGroup(group)

    for token in ("transform", "velocity", "deformation"):
        former_menu(loose_obj)
        rop.parm("geometry_motion").set(token)
        loose_obj.parm("cycles_geometry_motion").set(token)
        path = os.path.join(folder, token + ".hiplc")
        hou.hipFile.save(path)
        hou.hipFile.load(path, suppress_save_prompt=True, ignore_load_warnings=True)
        rop, loose_obj = hou.node("/out/velocity_rop"), hou.node("/obj/loose")
        before = loose_obj.parm("cycles_geometry_motion").eval()
        object_properties.install(loose_obj)
        parm = loose_obj.parm("cycles_geometry_motion")
        settings = instancing.motion_settings(loose_obj, now, rop.parm("geometry_motion").eval())
        check(
            "a scene saved with " + token + " keeps it on the ROP and the object",
            rop.parm("geometry_motion").eval() == token
            and before == parm.eval() == token
            and parm.parmTemplate().label() == "Deformation"
            and settings["motion"] == token
            and settings["motion_explicit"],
            rop=rop.parm("geometry_motion").eval(),
            object=parm.eval(),
            label=parm.parmTemplate().label(),
            rop_label=rop.parm("geometry_motion").parmTemplate().label(),
        )
    report.update(result="passed" if not failures else "failed", failures=failures)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    report["checks"] = results
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str))
print("RESULT", "PASS" if not failures else "FAIL " + ", ".join(failures))
sys.exit(1 if failures else 0)
