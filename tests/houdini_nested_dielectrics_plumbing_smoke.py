# SPDX-License-Identifier: Apache-2.0
"""Nested dielectrics plumbing. The ROP toggle, the object Dielectric Priority and the
packed cycles_dielectric_priority override must arrive in the Cycles scene (read back from the engine
objects in a diagnostic session). IPR priority/IOR/transform edits restart the render, judged by
render_revision and a fully re-accumulated frame, and the camera-only reuse guard rejects them.
The scene is generated here; no artist HIP file is opened or saved."""

from support.paths import artifacts_root

import pass_fixtures as pass_fixture
from cycles_houdini.device_selection import set_devices as set_rop_devices
import argparse, json, os, time, traceback
from pathlib import Path
import hou
from cycles_houdini import native_scene, object_properties, registry, materials

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
parser.add_argument("--out", default="")
parser.add_argument("--threads", type=int, default=4)
args = parser.parse_args()
device = args.device
out = Path(args.out) if args.out else artifacts_root() / "nested-plumbing"
out.mkdir(parents=True, exist_ok=True)
checks = []
SAMPLES = 8


def check(name, **details):
    checks.append(dict(name=name, **details))
    print("ok:", name, flush=True)


# Read engine objects back: keep the viewer's worker execution, add diagnostics.
_session = registry.session_from_configuration
registry.session_from_configuration = lambda config, **kw: _session(config, **dict(kw, collect_diagnostics=True))

hou.setFrame(1)
scene = hou.node("/obj").createNode("subnet", "nested_plumbing")


def geo(name):
    node = scene.createNode("geo", name)
    for child in node.children():
        child.destroy()
    assert object_properties.install(node)
    return node


glass = geo("glass")
sphere = glass.createNode("sphere")
sphere.parm("type").set(1)
sphere.setRenderFlag(True)
sphere.setDisplayFlag(True)
packed = geo("packed")
packed.parm("tx").set(2.5)
proto = packed.createNode("sphere", "proto")
proto.parm("type").set(1)
proto.parmTuple("rad").set((0.3, 0.3, 0.3))
points = packed.createNode("add", "placements")
pass_fixture.set_parameters(points, dict(points=3, usept0=1, pt0x=0, usept1=1, pt1x=0.8, usept2=1, pt2x=-0.8))
template = packed.createNode("attribwrangle", "template_field")
template.setInput(0, points)
template.parm("class").set(2)
copies = packed.createNode("copytopoints", "instances")
copies.setInput(0, proto)
copies.setInput(1, template)
copies.parm("pack").set(1)


def transfer(pattern, apply_to="point"):
    # Copy to Points > Attributes: one entry copying the template point attribute.
    copies.parm("targetattribs").set(1 if pattern else 0)
    if not pattern:
        return
    entry = {
        p.name()[:-1]: p
        for p in copies.parms()
        if p.name().endswith("1") and p.name()[:-1] in ("applyto", "applymethod", "applyattribs")
    }
    entry["applyattribs"].set(pattern)
    for name, key in (("applyto", apply_to), ("applymethod", "copy")):
        if name in entry:
            parm = entry[name]
            items = parm.menuItems()
            parm.set(
                next(
                    i
                    for i, (token, label) in enumerate(zip(items, parm.menuLabels()))
                    if key in (token + label).lower()
                )
                if parm.parmTemplate().type() == hou.parmTemplateType.Int
                else next(token for token, label in zip(items, parm.menuLabels()) if key in (token + label).lower())
            )


# The artist's override: a primitive attribute on the packed primitives.
field = packed.createNode("attribwrangle", "priority_field")
field.setInput(0, copies)
field.parm("class").set(1)
field.parm("snippet").set("i@cycles_dielectric_priority = array(0, 5, -2)[@primnum];")
field.setRenderFlag(True)
field.setDisplayFlag(True)
camera = hou.node("/obj").createNode("cam", "nested_camera")
pass_fixture.set_parameters(camera, dict(tx=1.2, tz=8, resx=96, resy=64))
material = materials.create_material(name="nested_glass", shader="glass_bsdf")
shader = material.node("surface_shader")
ior = next(p for p in shader.parms() if p.name().lower() == "ior")
rop = hou.node("/out").createNode("cycles_ipr", "nested_plumbing_rop")
set_rop_devices(rop, device)
pass_fixture.set_parameters(
    rop,
    dict(
        source_mode="objects",
        objectroot=scene.path(),
        camera=camera.path(),
        resx=96,
        resy=64,
        samples=SAMPLES,
        ipr_samples=SAMPLES,
        render_threads=args.threads,
        adaptive_sampling=0,
        ipr_adaptive_sampling=0,
        use_denoise=0,
        ipr_use_denoise=0,
        use_material_assignments=0,
        materialpath=material.path(),
        allowmotionblur=0,
    ),
)

toggle = rop.parmTemplateGroup().find("nested_dielectrics")
assert toggle is not None and toggle.label() == "Nested Dielectrics" and toggle.defaultValue() == False
assert rop.parm("nested_dielectrics").containingFolders()[-1] == "Light Paths", rop.parm(
    "nested_dielectrics"
).containingFolders()
assert "Dielectric Priority" in toggle.help()
prio = glass.parm("cycles_dielectric_priority")
assert prio.parmTemplate().label() == "Dielectric Priority" and prio.eval() == 0
assert prio.containingFolders()[-1] == "Shading", prio.containingFolders()
check("ROP toggle in Rendering > Light Paths (off); object Dielectric Priority in Shading (0)")

entry = registry.for_rop(rop, native_scene.configuration(rop))
from cycles_houdini import render_warnings


def no_warnings(status, label=""):
    assert (
        not render_warnings.warnings(status)
        and status.get("nested_probe_unresolved", 0) == 0
        and not status.get("nested_stack_depth_clamped")
    ), (label, render_warnings.warnings(status), status.get("nested_probe_unresolved"))


def render():
    update = native_scene.capture(rop)
    frame = entry.session.wait(entry.apply(update, hou.time()))
    status = entry.session.status()
    no_warnings(status, "normal scene")
    return update, frame, status


def revision(status):
    return status.get("render_revision", status["generation"])


def engine(status):
    return dict(
        nested=status["engine_nested_dielectrics"],
        objects=status["dielectric_priority_objects"],
        low=status["dielectric_priority_min"],
        high=status["dielectric_priority_max"],
    )


def restarted(label, action, expect=None):
    _, _, before = render()
    action()
    update, frame, after = render()
    assert revision(after) > revision(before), (label, revision(before), revision(after))
    assert frame["complete"] and frame["samples"] == SAMPLES, (label, frame["samples"])
    if expect:
        expect(update, frame, after)
    check(
        label + " restarts the render",
        revision_before=revision(before),
        revision_after=revision(after),
        samples=frame["samples"],
        engine=engine(after),
    )
    return after


try:
    update, frame, status = render()
    assert status["rendering"]["nested_dielectrics"] is False and engine(status) == dict(
        nested=False, objects=0, low=0, high=0
    ), engine(status)
    assert all(request["object_properties"]["dielectric_priority"] == 0 for request in update["geometry"])
    check("Feature off: integrator off, every engine object at priority 0", engine=engine(status))

    def glass_three(update, frame, status):
        by_path = {r["object_path"]: r for r in update["geometry"]}
        assert by_path[glass.path()]["object_properties"]["dielectric_priority"] == 3
        # The packed field is requested only while the feature is on.
        assert engine(status) == dict(nested=False, objects=1, low=0, high=3), engine(status)

    restarted("Object priority edit (feature off)", lambda: prio.set(3), glass_three)

    def on(update, frame, status):
        assert status["rendering"]["nested_dielectrics"] is True
        assert engine(status) == dict(nested=True, objects=3, low=-2, high=5), engine(status)

    restarted("ROP Nested Dielectrics on", lambda: rop.parm("nested_dielectrics").set(1), on)

    restarted(
        "Packed primitive field edit",
        lambda: field.parm("snippet").set("i@cycles_dielectric_priority = array(0, 7, -2)[@primnum];"),
        lambda u, f, s: (engine(s) == dict(nested=True, objects=3, low=-2, high=7))
        or (_ for _ in ()).throw(AssertionError(engine(s))),
    )
    # The same override as a point attribute on the packed primitives' points.
    restarted(
        "Packed point field",
        lambda: (
            field.parm("class").set(2),
            field.parm("snippet").set("i@cycles_dielectric_priority = array(0, 8, -3)[@ptnum];"),
        ),
        lambda u, f, s: (engine(s) == dict(nested=True, objects=3, low=-3, high=8))
        or (_ for _ in ()).throw(AssertionError(engine(s))),
    )

    def artist(update, frame, status):
        assert engine(status) == dict(nested=True, objects=3, low=-4, high=6), engine(status)

    restarted(
        "Artist workflow: template point attribute copied to packed points by Copy to Points",
        lambda: (
            field.parm("snippet").set(""),
            template.parm("snippet").set("i@cycles_dielectric_priority = array(0, 6, -4)[@ptnum];"),
            transfer("cycles_dielectric_priority", "point"),
        ),
        artist,
    )
    g = copies.geometry()
    check(
        "Copy to Points output carries the field",
        point=bool(g.findPointAttrib("cycles_dielectric_priority")),
        prim=bool(g.findPrimAttrib("cycles_dielectric_priority")),
    )
    restarted(
        "Artist workflow: Copy to Points applying the field to primitives",
        lambda: transfer("cycles_dielectric_priority", "prim"),
        artist,
    )

    def inherited(update, frame, status):
        # Without a field the packed children inherit the OBJ priority (three instances at 6, glass at 3).
        assert engine(status) == dict(nested=True, objects=4, low=3, high=6), engine(status)

    restarted(
        "Packed field removed, OBJ priority inherited",
        lambda: (
            field.parm("snippet").set(""),
            template.parm("snippet").set(""),
            transfer(""),
            packed.parm("cycles_dielectric_priority").set(6),
        ),
        inherited,
    )

    restarted("Material IOR edit", lambda: ior.set(1.333))
    restarted("Object transform edit", lambda: glass.parm("ty").set(0.25))

    # Camera-only reuse: a pending camera notification must be revoked by a priority edit.
    render()
    assert entry.change_serial == entry.applied_serial
    entry.changed(camera_only=True)
    assert entry.camera_only_pending
    serial = entry.change_serial
    prio.set(9)
    assert entry.change_serial > serial and not entry.camera_only_pending, "Priority edit must revoke camera-only reuse"
    motion = native_scene.motion_configuration(rop, hou.time())
    assert not entry.can_capture_camera_only(motion, hou.time())
    _, _, status = render()
    assert engine(status)["high"] == 9, engine(status)
    check("Camera-only reuse guard rejects a priority edit", engine=engine(status))

    restarted(
        "ROP Nested Dielectrics off",
        lambda: rop.parm("nested_dielectrics").set(0),
        lambda u, f, s: (not engine(s)["nested"]) or (_ for _ in ()).throw(AssertionError(engine(s))),
    )

    # Final (sync) render path carries the toggle as well.
    from cycles_houdini import final_render

    rop.parm("nested_dielectrics").set(1)
    rop.parm("picture").set(str(out / "nested-final.exr"))
    rop.render(frame_range=(1, 1, 1))
    summary = rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 1 and summary["last_status"]["engine_nested_dielectrics"] is True, summary
    assert summary["last_status"]["rendering"]["nested_dielectrics"] is True
    assert not render_warnings.warnings(summary["last_status"])
    check("Final render enables the integrator toggle; normal scenes report no render warnings")

    # Forcing fixture: the camera inside 40 concentric participating spheres exceeds the media stack.
    import contextlib, io

    force = hou.node("/obj").createNode("subnet", "nested_force")
    shells = force.createNode("geo", "shells")
    for child in shells.children():
        child.destroy()
    assert object_properties.install(shells)
    unit = shells.createNode("sphere")
    unit.parm("type").set(1)
    centres = shells.createNode("add")
    centres.parm("points").set(40)
    for n in range(40):
        centres.parm("usept%d" % n).set(1)
    size = shells.createNode("attribwrangle")
    size.setInput(0, centres)
    size.parm("class").set(2)
    size.parm("snippet").set("f@pscale = 1.0 + 0.02*@ptnum;")
    nested = shells.createNode("copytopoints")
    nested.setInput(0, unit)
    nested.setInput(1, size)
    nested.parm("pack").set(1)
    order = shells.createNode("attribwrangle")
    order.setInput(0, nested)
    order.parm("class").set(1)
    order.parm("snippet").set("i@cycles_dielectric_priority = 40 - @primnum;")
    order.setRenderFlag(True)
    order.setDisplayFlag(True)
    inside = hou.node("/obj").createNode("cam", "nested_force_camera")
    pass_fixture.set_parameters(inside, dict(resx=64, resy=48))
    forced = hou.node("/out").createNode("cycles_ipr", "nested_force_rop")
    set_rop_devices(forced, device)
    pass_fixture.set_parameters(
        forced,
        dict(
            source_mode="objects",
            objectroot=force.path(),
            camera=inside.path(),
            resx=64,
            resy=48,
            samples=4,
            ipr_samples=4,
            render_threads=args.threads,
            adaptive_sampling=0,
            ipr_adaptive_sampling=0,
            use_denoise=0,
            ipr_use_denoise=0,
            use_material_assignments=0,
            materialpath=material.path(),
            allowmotionblur=0,
            nested_dielectrics=1,
        ),
    )
    registry.close_all()
    fentry = registry.for_rop(forced, native_scene.configuration(forced))
    fentry.session.wait(fentry.apply(native_scene.capture(forced), hou.time()))
    fs = fentry.session.status()
    found = render_warnings.warnings(fs)
    codes = [c for c, _, _ in found]
    assert fs["nested_probe_unresolved"] > 0 and "render.nested_probe_limit" in codes, (
        fs["nested_probe_unresolved"],
        found,
    )
    message = next(m for c, m, _ in found if c == "render.nested_probe_limit")
    assert message.startswith(
        "Nested Dielectrics: medium could not be determined for %d camera rays (probe limit)."
        % fs["nested_probe_unresolved"]
    ), message
    assert "Nested Dielectrics: medium could not be determined" in render_warnings.headline(fs)
    console = io.StringIO()
    reporter = render_warnings.Reporter(stream=console)
    reporter.report_changes(fs)
    reporter.report_changes(fs)
    assert console.getvalue().count("Cycles warning: Nested Dielectrics: medium") == 1, console.getvalue()
    check(
        "Forcing fixture reports the probe-limit warning (viewer status line, console once)",
        unresolved=fs["nested_probe_unresolved"],
        depth_clamped=fs["nested_stack_depth_clamped"],
        depth=fs["nested_stack_depth"],
        codes=codes,
    )
    forced.parm("nested_dielectrics").set(0)
    fentry.session.wait(fentry.apply(native_scene.capture(forced), hou.time()))
    fs_off = fentry.session.status()
    assert not render_warnings.warnings(fs_off) and fs_off["nested_probe_unresolved"] == 0
    check("Feature off: the forcing fixture reports no warning")
    forced.parm("nested_dielectrics").set(1)
    registry.close_all()
    forced.parm("picture").set(str(out / "nested-force-$F.exr"))
    log = io.StringIO()
    with contextlib.redirect_stdout(log):
        forced.render(frame_range=(1, 2, 1))  # must not raise: warnings never fail a render
    lines = [l for l in log.getvalue().splitlines() if l.startswith("Cycles warning [render.nested_probe_limit]:")]
    from cycles_houdini import final_render

    summary = forced.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 2 and any(
        "medium could not be determined" in w for w in summary.get("warnings", [])
    ), summary.get("warnings")
    assert len(lines) == 2 and all(sum(("frame %d," % frame) in line for line in lines) == 1 for frame in (1, 2)), (
        log.getvalue()[-2000:],
        summary.get("warnings"),
    )
    check(
        "Final render succeeds; its log reports each structured warning once per frame after the render; the summary keeps them",
        lines=lines,
        summary_warnings=summary.get("warnings"),
    )
    report = dict(result="passed", device=device, checks=checks)
except Exception:
    report = dict(result="failed", device=device, checks=checks, error=traceback.format_exc())
    print(report["error"], flush=True)
finally:
    registry.close_all()
(out / "houdini-nested-dielectrics-plumbing.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result=report["result"], checks=len(checks))), flush=True)
hou.exit(0 if report["result"] == "passed" else 1)
