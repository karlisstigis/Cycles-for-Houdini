# SPDX-License-Identifier: Apache-2.0
"""Artist-facing OBJ properties, independent surface checks and cache ownership."""

from support.paths import artifacts_root
from subdivision_test_setup import configure_render, set_subdivision
from cycles_houdini.device_selection import set_devices as set_rop_devices
import argparse
import json
import os
from pathlib import Path
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene, object_properties, passes, registry

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
destination = artifacts_root() / "stage3/object-properties"
destination.mkdir(parents=True, exist_ok=True)
hou.setFrame(1)
hou.setFps(24)
scene = hou.node("/obj").createNode("subnet", "cycles_object_properties")
objects = []
for name, x in [("editable", -1), ("unchanged", 1)]:
    obj = scene.createNode("geo", name)
    for child in obj.children():
        child.destroy()
    sop = obj.createNode("box", "native_control_mesh")
    sop.setRenderFlag(True)
    sop.setDisplayFlag(True)
    obj.parm("tx").set(x)
    objects.append(obj)
    assert obj.parm("cycles_subdivision_override") is None, "New Geometry OBJ must remain renderer-neutral"
    assert object_properties.install(obj)
    assert obj.evalParm("cycles_subdivision_ipr") == 1
    panel = obj.parmTemplateGroup().find("cycles_subdivision").parmTemplates()
    assert [parm.name() for parm in panel[:2]] == ["cycles_subdivision_ipr", "cycles_subdivision_type"]
    assert obj.parm("cycles_subdivision_render_level").containingFolders()[0] == "Cycles"
    panels = {
        "cycles_mask": ("Mask", ("cycles_shadow_catcher", "cycles_holdout")),
        "cycles_shadow_terminator": (
            "Shadow Terminator",
            ("cycles_shadow_terminator_geometry", "cycles_shadow_terminator_shading"),
        ),
        "cycles_caustics": ("Caustics", ("cycles_caustics_caster", "cycles_caustics_receiver")),
    }
    shading = obj.parmTemplateGroup().find("cycles_shading")
    for name, (label, children) in panels.items():
        panel = next(parm for parm in shading.parmTemplates() if parm.name() == name)
        assert panel.label() == label and panel.folderType() == hou.folderType.Collapsible
        assert tuple(parm.name() for parm in panel.parmTemplates()) == children
    assert (
        obj.parm("cycles_dielectric_priority").parmTemplate().label() == "Dielectric Priority"
        and obj.evalParm("cycles_dielectric_priority") == 0
    )
    assert "cycles_dielectric_priority" in [parm.name() for parm in shading.parmTemplates()], (
        "Dielectric Priority is its own Shading row"
    )
    assert object_properties.properties_folder(obj.parmTemplateGroup()).tags()["cycles_properties_version"] == "22"
    assert obj.evalParm("cycles_blender_subdivision") == 1
    assert obj.evalParm("cycles_point_render_mode") == "auto"
    assert obj.parm("cycles_point_render_mode").parmTemplate().menuItems() == ("auto", "spheres", "gaussian_splats")
    assert obj.parm("cycles_shadow_terminator_geometry").parmTemplate().label() == "Geometry Offset"
    assert obj.parm("cycles_shadow_terminator_shading").parmTemplate().label() == "Shading Offset"
    assert obj.parm("cycles_shadow_catcher").parmTemplate().label() == "Shadow Catcher"
    assert "Raycast shader node" in obj.parm("cycles_visibility_raycast").parmTemplate().help()
    assert not object_properties.install(obj), "Repeated installation must be idempotent"
obj, static = objects
# Refresh an existing owned tab without losing authored values or expressions.
obj.parm("cycles_subdivision_render_level").set(4)
obj.parm("cycles_subdivision_ipr_level").setExpression("1 + $F", hou.exprLanguage.Hscript)
obj.setParms(
    dict(
        cycles_caustics_caster=1,
        cycles_shadow_catcher=1,
        cycles_shadow_terminator_geometry=0.23,
        cycles_dielectric_priority=4,
    )
)
obj.parm("cycles_shadow_terminator_shading").setExpression("$F / 100", hou.exprLanguage.Hscript)
group = obj.parmTemplateGroup()
folder = object_properties.properties_folder(group)
folder.setTags(dict(folder.tags(), cycles_properties_version="11"))
group.replace(folder.name(), folder)
shading = group.find("cycles_shading")
flat = []
for parm in shading.parmTemplates():
    flat.extend(parm.parmTemplates() if parm.name() in panels else [parm])
shading.setParmTemplates(flat)
group.replace(group.findIndices("cycles_shading"), shading)
group.remove("cycles_subdivision_ipr")
obj.setParmTemplateGroup(group)
assert object_properties.install(obj) and obj.evalParm("cycles_subdivision_ipr") == 1
assert obj.evalParm("cycles_subdivision_render_level") == 4
assert obj.parm("cycles_subdivision_ipr_level").expression() == "1 + $F"
assert obj.evalParm("cycles_caustics_caster") == 1 and obj.evalParm("cycles_shadow_catcher") == 1
assert obj.evalParm("cycles_dielectric_priority") == 4
assert object_properties.component_settings(obj, hou.time())["object_properties"]["dielectric_priority"] == 4
assert abs(obj.evalParm("cycles_shadow_terminator_geometry") - 0.23) < 1e-6
assert obj.parm("cycles_shadow_terminator_shading").expression() == "$F / 100"
assert obj.parm("cycles_caustics_caster").containingFolders()[-1] == "Caustics"
obj.parm("cycles_shadow_terminator_shading").deleteAllKeyframes()
obj.setParms(
    dict(
        cycles_caustics_caster=0,
        cycles_shadow_catcher=0,
        cycles_shadow_terminator_geometry=0.1,
        cycles_shadow_terminator_shading=0,
        cycles_dielectric_priority=0,
    )
)
obj.parm("cycles_subdivision_ipr_level").deleteAllKeyframes()
obj.setParms(dict(cycles_subdivision_ipr_level=1, cycles_subdivision_render_level=2))
camera = hou.node("/obj").createNode("cam", "object_properties_camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=4))
rop = hou.node("/out").createNode("cycles_ipr", "object_properties_rop")
passes.add(rop, "depth")
set_rop_devices(rop, device)
configure_render(
    rop,
    dict(
        source_mode="objects",
        objectroot=scene.path(),
        camera=camera.path(),
        resx=160,
        resy=120,
        samples=16,
        material_kind="emission",
        emission_strength=1,
        transparent_background=1,
    ),
)
rop.parmTuple("basecolor").set((0.2, 0.4, 0.8))
rop.parmTuple("background").set((0, 0, 0))
entry = registry.for_rop(rop, native_scene.configuration(rop))
checks = []


def plane(frame, name):
    value = frame["planes"][name]
    return np.frombuffer(value["pixels"], np.float32).reshape(frame["height"], frame["width"], value["channels"])


def render():
    return entry.session.wait(entry.apply(native_scene.capture(rop), hou.time()))


def depth(frame, column):
    return float(plane(frame, "depth")[60, column, 0])


def record(name, **details):
    checks.append(dict(name=name, status=entry.session.status(), **details))


try:
    original = render()
    assert abs(depth(original, 40) - 4.5) < 1e-5 and abs(depth(original, 120) - 4.5) < 1e-5
    configure_render(rop, dict(allowmotionblur=1, geometry_motion="velocity"))
    captured = native_scene.capture(rop)
    assert all(request["motion"] == "velocity" for request in captured["geometry"])
    fallback = render()
    assert abs(depth(fallback, 40) - 4.5) < 1e-5 and abs(depth(fallback, 120) - 4.5) < 1e-5
    obj.parm("cycles_geometry_motion").set("deformation")
    captured = native_scene.capture(rop)
    by_object = {request["object_path"]: request for request in captured["geometry"]}
    assert by_object[obj.path()]["motion"] == "deformation" and by_object[static.path()]["motion"] == "velocity"
    obj.parm("cycles_motion_blur").set("off")
    by_object = {request["object_path"]: request for request in native_scene.capture(rop)["geometry"]}
    assert not by_object[obj.path()]["motion_blur"] and by_object[static.path()]["motion_blur"]
    render()
    obj.setParms(dict(cycles_geometry_motion="inherit", cycles_motion_blur="inherit"))
    configure_render(rop, dict(allowmotionblur=0, geometry_motion="transform"))
    render()
    record("ROP motion defaults, per-object overrides and absent velocity fallback")
    original_sop = obj.renderNode().geometry()
    sop_count = original_sop.intrinsicValue("primitivecount")
    initial = entry.session.status()["geometry_conversions"]
    obj.setParms(
        dict(cycles_subdivision_override=1, cycles_subdivision_type="catmull_clark", cycles_subdivision_ipr_level=2)
    )
    rounded = render()
    s = entry.session.status()
    assert s["geometry_conversions"] == initial + 1 and s["subdivision_control_faces"] == 6
    # Independent Houdini/OpenSubdiv refinement evaluates the same coarse box.
    # It is rendered through a separate session, without Cycles subdivision.
    reference_object = hou.node("/obj").createNode("geo", "independent_subdivision_reference")
    reference_object.parm("tx").set(-1)
    reference_box = reference_object.createNode("box")
    refined = reference_object.createNode("subdivide")
    refined.setInput(0, reference_box)
    refined.setParms(dict(algorithm="osdcc", iterations=5))
    with ch.Session(
        device=device, width=160, height=120, samples=16, passes=["combined", "depth"]
    ) as reference_session:
        reference = reference_session.wait(
            reference_session.apply(
                dict(
                    geometry=[
                        ch.geometry_request(
                            reference_object, refined, material="reference", subdivision=dict(type="none")
                        )
                    ],
                    materials=[dict(key="reference", kind="emission", color=(0.2, 0.4, 0.8), strength=1)],
                    camera_path=camera.path(),
                    background=(0, 0, 0),
                )
            )
        )
    reference_depth = depth(reference, 40)
    assert 4.57 < depth(rounded, 40) < 4.65 and abs(depth(rounded, 40) - reference_depth) < 0.005, (
        depth(rounded, 40),
        reference_depth,
    )
    assert abs(depth(rounded, 120) - 4.5) < 1e-5
    assert np.array_equal(plane(original, "combined")[:, 100:], plane(rounded, "combined")[:, 100:])
    assert obj.renderNode().geometry().intrinsicValue("primitivecount") == sop_count == 6
    assert all(node.type().name() != "subdivide" for node in obj.children())
    triangles = s["subdivision_triangles"]
    assert triangles == 12 * 4**2, (triangles, s)
    record(
        "Object Catmull-Clark agrees with independent Houdini refinement; unchanged object and six SOP faces are retained",
        cycles_depth=depth(rounded, 40),
        independent_reference_depth=reference_depth,
        reference_levels=5,
    )
    camera.parm("tx").set(0.1)
    render()
    s = entry.session.status()
    assert s["geometry_conversions"] == initial + 1 and s["subdivision_triangles"] == triangles
    camera.parm("tx").set(0)
    render()
    record("Fixed levels are independent of camera position")
    obj.parm("cycles_subdivision_ipr_level").set(3)
    render()
    assert entry.session.status()["subdivision_triangles"] == 12 * 4**3
    request = native_scene.geometry_for_rop(rop, obj.renderNode(), hou.time(), interactive=False)
    assert request["subdivision"]["max_level"] == 2, request
    record("Separate IPR and final levels produce the expected uniform density")
    obj.parm("cycles_subdivision_ipr").set(0)
    unsubdivided = render()
    assert entry.session.status()["subdivision_control_faces"] == 0 and abs(depth(unsubdivided, 40) - 4.5) < 1e-5
    obj.updateParmStates()
    assert (
        obj.parm("cycles_subdivision_ipr_level").isDisabled()
        and not obj.parm("cycles_subdivision_render_level").isDisabled()
    )
    # Exercise the real disk/SOHO path, rather than only inspecting a request.
    import OpenImageIO as oiio
    from cycles_houdini import final_render

    rop.parm("picture").set(str(destination / "ipr-disabled-final.exr"))
    rop.render(frame_range=(1, 1, 1))
    final_status = rop.cachedUserData(final_render._summary_key)
    assert final_status["completed_frames"] == 1 and final_status["last_status"]["subdivision_control_faces"] == 6, (
        final_status
    )
    image = oiio.ImageBuf(str(destination / "ipr-disabled-final.exr"))
    z = list(image.spec().channelnames).index("depth.Z")
    final_depth = float(image.get_pixels()[60, 40, z])
    assert abs(final_depth - depth(rounded, 40)) < 0.005, final_depth
    assert abs(float(image.get_pixels()[60, 120, z]) - 4.5) < 1e-5
    obj.parm("cycles_subdivision_ipr").set(1)
    restored = render()
    assert entry.session.status()["subdivision_control_faces"] == 6 and depth(restored, 40) > 4.57
    record(
        "Enable in IPR disables uniform subdivision, disk still subdivides, and live re-enable restores it",
        final_depth=final_depth,
    )
    obj.parm("cycles_subdivision_type").set("linear")
    linear = render()
    assert abs(depth(linear, 40) - 4.5) < 1e-5
    obj.parm("cycles_subdivision_ipr_level").set(0)
    render()
    assert entry.session.status()["subdivision_control_faces"] == 0
    record("Simple/Linear preserves the original plane; level zero disables subdivision")
    obj.setParms(
        dict(
            cycles_subdivision_type="catmull_clark",
            cycles_subdivision_adaptive=1,
            cycles_subdivision_space="pixel",
            cycles_subdivision_pixel_size=1,
        )
    )
    render()
    before = entry.session.status()
    obj.parm("cycles_subdivision_pixel_size").set(0.25)
    render()
    after = entry.session.status()
    assert after["subdivision_triangles"] > before["subdivision_triangles"]
    request = native_scene.geometry_for_rop(rop, obj.renderNode(), hou.time(), interactive=False)
    assert request["subdivision"]["dicing_rate"] == 0.25
    record(
        "Adaptive pixel size is multiplied by scene IPR and render dicing rates",
        coarse_triangles=before["subdivision_triangles"],
    )
    obj.parm("cycles_subdivision_ipr").set(0)
    plain = render()
    assert entry.session.status()["subdivision_control_faces"] == 0 and abs(depth(plain, 40) - 4.5) < 1e-5
    assert (
        native_scene.geometry_for_rop(rop, obj.renderNode(), hou.time(), interactive=False)["subdivision"]["type"]
        == "catmull_clark"
    )
    obj.parm("cycles_subdivision_ipr").set(1)
    render()
    assert entry.session.status()["subdivision_triangles"] == after["subdivision_triangles"]
    record("Enable in IPR also disables adaptive subdivision and restores the same tessellation")
    obj.setParms(dict(cycles_subdivision_adaptive=0, cycles_subdivision_ipr_level=3, cycles_subdivision_use_creases=1))
    creased = obj.createNode("attribwrangle", "native_creases")
    creased.setInput(0, obj.renderNode())
    creased.parm("class").set(3)
    creased.parm("snippet").set("f@creaseweight=10;")
    creased.setRenderFlag(True)
    sharp = render()
    assert abs(depth(sharp, 40) - 4.5) < 0.001
    obj.parm("cycles_subdivision_use_creases").set(0)
    smooth = render()
    assert depth(smooth, 40) > 4.58
    before = entry.session.status()["geometry_conversions"]
    creased.parm("snippet").set("f@creaseweight=5;")
    render()
    assert entry.session.status()["geometry_conversions"] == before
    record("Creases can be ignored, and ignored crease edits retain mesh conversion")
    before = entry.session.status()["geometry_conversions"]
    obj.setParms(dict(cycles_visibility_override=1, cycles_visibility_camera=0))
    hidden = render()
    assert np.all(plane(hidden, "combined")[60, 40] == 0)
    assert entry.session.status()["geometry_conversions"] == before
    obj.parm("cycles_visibility_camera").set(1)
    render()
    record("Camera visibility edits preserve geometry")
    obj.setParms(
        dict(
            cycles_point_radius_override=1,
            cycles_point_radius=0.23,
            cycles_curve_width_override=1,
            cycles_curve_width=0.04,
            cycles_volume_step_size_override=1,
            cycles_volume_step_size=0.02,
        )
    )
    request = native_scene.geometry_for_rop(rop, obj.renderNode(), hou.time())
    assert request["point_radius"] == 0.23 and request["curve_width"] == 0.04 and request["volume_step_size"] == 0.02
    # Shape bounds of transform motion are specified independently in pixels:
    # velocity 48 units/s, .5-frame shutter at 24 fps => one world-unit span.
    obj.parm("cycles_subdivision_override").set(0)
    obj.parm("tx").setExpression("-1 + $T*48", hou.exprLanguage.Hscript)
    rop.parm("allowmotionblur").set(1)
    camera.parm("shutter").set(0.5)
    obj.parm("cycles_motion_blur").set("off")
    still = render()
    obj.parm("cycles_motion_blur").set("inherit")
    blur = render()
    still_alpha = plane(still, "combined")[60, :80, 3]
    blur_alpha = plane(blur, "combined")[60, :80, 3]
    assert np.count_nonzero(blur_alpha) > np.count_nonzero(still_alpha) + 8
    record("Per-object motion disabling removes transform blur while the ROP retains its shutter")
    obj.parm("tx").deleteAllKeyframes()
    obj.parm("tx").set(-1)
    rop.parm("allowmotionblur").set(0)
    obj.setParms(dict(cycles_subdivision_override=1, cycles_subdivision_adaptive=0, cycles_subdivision_render_level=3))
    path = destination / "object-properties.hiplc"
    object_path = obj.path()
    hou.hipFile.save(str(path))
    registry.close_all()
    hou.hipFile.load(str(path), suppress_save_prompt=True)
    loaded = hou.node(object_path)
    assert loaded.evalParm("cycles_subdivision_render_level") == 3
    assert sum(parm.label() == "Cycles" for parm in loaded.parmTemplateGroup().entries()) == 1
    record("Saved HIP retains object settings without duplicate tabs")
    report = dict(
        result="passed", device=device, execution=os.environ.get("CYCLES_HOUDINI_EXECUTION", "sync"), checks=checks
    )
    (destination / "houdini-object-properties-smoke.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report))
finally:
    registry.close_all()
