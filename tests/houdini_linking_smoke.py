# SPDX-License-Identifier: Apache-2.0
"""Native light/shadow selection, independent illumination and repair through the worker."""

import argparse, json, traceback
import hou, numpy as np, OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import native_scene, registry, light_properties, object_properties, final_render
from light_shader_fixture import Fixture
from cycles_houdini import passes

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
f = Fixture(p.parse_args().device)
for name in ("normal", "position", "depth"):
    passes.add(f.rop, name)
object_properties.install(f.obj)
f.light.parmTuple("cycles_light_color").set((1, 0, 0))
fill = f.scene.createNode("hlight::2.0", "green_fill")
light_properties.install(fill)
# Native lights default to -noshadow, an active category selector. Start without
# a selector so the dormant-metadata check below really is disconnected.
fill.setParms(dict(tz=3, light_intensity=100, light_exposure=0, cycles_light_radius=0, shadowcategories=""))
fill.parmTuple("light_color").set((0, 1, 0))
entry = f.session()
report = dict(result="running", device=f.device, checks=[])
dest = f.dest.parent / "linking"
dest.mkdir(exist_ok=True)


def render(label, multiplier, code=None, *, expect_linking=None):
    update = None
    now = hou.time()
    if expect_linking is not None:
        camera = native_scene.capture(f.rop, now, camera_only=True)
        update = entry.narrow_update(now, camera["motion"], camera)
        if expect_linking:
            assert update is not None and update["_narrow"] == "linking", entry.narrow_reason
            assert set(update) == set(camera) | {"linking", "_narrow", "_basis"}, update.keys()
        else:
            assert update is None, entry.narrow_reason
    if update is None:
        frame = f.update()
    else:
        submission = entry.apply(update, now, change_serial=entry.change_serial)
        frame = entry.session.wait(submission, timeout=60)
    status = entry.session.status()
    f.check(frame, multiplier)
    assert status["state"] == "complete" and status["render_device_ids"] == [f.device], status
    assert {d["code"] for d in status["source_diagnostics"]} == ({code} if code else set()), status
    report["checks"].append(dict(label=label, status=status))
    return frame


try:
    assert f.light.type().definition().extraInfoValue("subtype") == "light"
    assert all(child.type().category() == hou.sopNodeTypeCategory() for child in f.light.children())
    baseline = render("Both native lights match independent Lambertian illumination", (1, 1, 0))
    pid = entry.session.status()["renderer_pid"]
    converted = entry.session.status()["geometry_conversions"]
    f.obj.parm("lightmask").set(f.light.path())
    red = render("Native object Light Mask selects red with a linking-only update", (1, 0, 0), expect_linking=True)
    for name in ("normal", "position", "depth"):
        assert np.array_equal(f.pixels(baseline, name), f.pixels(red, name))
    f.obj.parm("lightmask").set("*")
    restored = render("Clearing the last selection restores both lights", (1, 1, 0), expect_linking=True)
    assert entry.light_basis["update"]["linking"] == dict(receivers=[], blockers=[])
    np.testing.assert_array_equal(f.pixels(baseline), f.pixels(restored))
    f.obj.parm("lightmask").set("")
    render("Empty mask removes illumination without removing geometry", (0, 0, 0))
    f.obj.parm("lightmask").set("* ^" + f.light.path())
    render("Native exclusion selects green only", (0, 1, 0))
    f.obj.parm("lightmask").set("../" + f.light.name())
    render("Relative native mask resolves from the owning object", (1, 0, 0))
    for mask, expected in (
        (f.light.name(), (1, 0, 0)),
        ("* ^" + f.light.name(), (0, 1, 0)),
        ("../" + f.light.name()[:-1] + "*", (1, 0, 0)),
    ):
        f.obj.parm("lightmask").set(mask)
        render("Light Linker mask " + mask, expected)
    f.obj.parm("lightmask").set("*")
    f.light.parm("cycles_illuminates").set("")
    render("Emitter receiver mask excludes only red illumination", (0, 1, 0))
    f.light.parm("cycles_illuminates").set(f.obj.path())
    render("Emitter receiver repair restores red and clears no geometry", (1, 1, 0))
    f.obj.parm("lightmask").set(fill.path())
    render("Object and emitter selections intersect", (0, 1, 0))
    bundle = hou.addNodeBundle("cycles_linking_test")
    bundle.addNode(f.light)
    f.obj.parm("lightmask").set("@" + bundle.name())
    render("Native light bundle selects red", (1, 0, 0))
    f.obj.parm("lightmask").set("* ^@" + bundle.name())
    render("Excluding the Light Linker bundle removes the Cycles emitter", (0, 1, 0))
    f.obj.parm("lightmask").set("@" + bundle.name())
    bundle.removeNode(f.light)
    bundle.addNode(fill)
    render("Changing bundle membership selects green", (0, 1, 0))
    f.obj.parm("lightmask").set("*")
    f.light.parm("categories").set("key warm")
    fill.parm("categories").set("fill cool")
    f.obj.parm("lightcategories").set("key & warm | missing")
    render("Native category precedence selects red", (1, 0, 0))
    f.obj.parm("lightcategories").set("-key")
    render("Native negative category selects green", (0, 1, 0))
    f.obj.parm("lightcategories").set("(key)")
    render("Invalid category omits only that selector", (1, 1, 0), "linking.source_unavailable")
    f.obj.parm("lightcategories").set("")
    render("Category repair restores ordinary linking", (1, 1, 0))
    assert entry.session.status()["geometry_conversions"] == converted
    allocations = {0: [], 1: []}
    for index in range(100):
        selected = index % 2
        f.obj.parm("lightmask").set(f.light.path() if selected == 0 else fill.path())
        frame = f.update()
        f.check(frame, (1, 0, 0) if selected == 0 else (0, 1, 0))
        status = entry.session.status()
        assert status["geometry_conversions"] == converted and status["renderer_pid"] == pid, status
        copies = entry.session.diagnostics()["buffer_copy_requests"]
        assert not {"tri_verts", "tri_vindex"} & copies.keys(), copies
        if index >= 10:
            allocations[selected].append(status["device_allocation_bytes"])
    assert all(len(set(values)) == 1 for values in allocations.values()), allocations
    report["checks"].append(
        dict(
            label="One hundred link edits retain geometry buffers and stable warm tracked device allocations",
            status=status,
            allocations=allocations,
        )
    )
    f.obj.parm("lightmask").set("*")
    frame = f.update()
    f.check(frame, (1, 1, 0))
    blocker = f.scene.createNode("geo", "shadow_blocker", run_init_scripts=False)
    black = ch.create_material(name="linking_black_blocker", shader="diffuse_bsdf")
    black.node("surface_shader").parmTuple("color").set((0, 0, 0))
    f.rop.parm("use_material_assignments").set(1)
    if blocker.parm("shop_materialpath") is None:
        blocker.addSpareParmTuple(hou.StringParmTemplate("shop_materialpath", "Material", 1))
    blocker.parm("shop_materialpath").set(black.path())
    grid = blocker.createNode("grid")
    grid.setParms(dict(type=0, orient=0, rows=2, cols=2))
    grid.parmTuple("size").set((2, 2))
    grid.setDisplayFlag(True)
    grid.setRenderFlag(True)
    blocker.parm("tz").set(1.5)
    blocker.addSpareParmTuple(
        hou.StringParmTemplate("vm_rendervisibility", "Visibility", 1, default_value=("-primary",))
    )
    render("Opaque blocker shadows both lights while staying out of camera rays", (0, 0, 0))
    shadow_converted = entry.session.status()["geometry_conversions"]
    blocker.addSpareParmTuple(hou.IntParmTemplate("categories", "Categories", 1))
    assert isinstance(blocker.parm("categories").eval(), int)
    render("Unused malformed category metadata stays quiet", (0, 0, 0))
    f.light.parm("shadowcategories").set("blocker")
    render("Malformed blocker tags retain only the affected candidate", (0, 0, 0), "linking.source_unavailable")
    f.light.parm("shadowmask").set(f.obj.path())
    render("Masks exclude irrelevant malformed category metadata", (1, 0, 0))
    group = blocker.parmTemplateGroup()
    group.replace("categories", hou.StringParmTemplate("categories", "Categories", 1))
    blocker.setParmTemplateGroup(group)
    blocker.parm("categories").set("blocker")
    f.light.parm("shadowmask").set("*")
    render("Repairing category tag types restores filtering and clears diagnostics", (0, 0, 0))
    f.light.parm("shadowcategories").set("")
    f.light.parm("shadowmask").set(f.obj.path())
    render("Light Linker Shadow Mask releases red but retains green occlusion", (1, 0, 0))
    fill.parm("shadowmask").set("* ^" + blocker.path())
    render("Native Shadow Mask independently releases green", (1, 1, 0))
    copies = entry.session.diagnostics()["buffer_copy_requests"]
    assert not {"tri_verts", "tri_vindex"} & copies.keys(), copies
    f.light.parm("shadowmask").set("*")
    fill.parm("shadowmask").set("*")
    render("Shadow mask repair restores both shadows", (0, 0, 0))
    f.light.parm("shadowcategories").set("(bad)")
    f.light.parm("cycles_light_cast_shadow").set(0)
    render("Disabled shadows ignore a dormant invalid shadow selector", (1, 0, 0))
    f.light.parm("cycles_light_cast_shadow").set(1)
    render(
        "Enabled shadows report the invalid selector and retain ordinary occlusion",
        (0, 0, 0),
        "linking.source_unavailable",
    )
    f.light.parm("shadowcategories").set("")
    render("Repairing the shadow selector clears the warning", (0, 0, 0))
    assert entry.session.status()["geometry_conversions"] == shadow_converted
    blocker.destroy()
    render("Deleting a blocker restores illumination and retains receiver", (1, 1, 0))
    assert entry.session.status()["renderer_pid"] == pid
    width = f.sop.parm("sizex")
    mask = f.obj.parm("lightmask")
    width.setExpression('if(strlen(chs("../lightmask")) > 1, 2, 8)')
    wide = render("Register a SOP channel that reads the receiver Light Mask", (1, 1, 0))
    assert width in mask.parmsReferencingThis()
    mask.set("* ^" + fill.name())
    narrower = render("The receiver Light Mask changes illumination and SOP geometry", (1, 0, 0), expect_linking=False)
    assert width.eval() == 2
    assert not np.array_equal(f.pixels(wide, "position"), f.pixels(narrower, "position"))
    width.deleteAllKeyframes()
    width.set(8)
    # Ordered final output must apply animated masks and recover a bad category.
    f.obj.parm("lightmask").setExpression(
        repr(f.light.name()) + " if int(hou.frame())%2 else " + repr("* ^" + f.light.name()), hou.exprLanguage.Python
    )
    f.obj.parm("lightcategories").setExpression("'(key)' if int(hou.frame())==3 else ''", hou.exprLanguage.Python)
    f.rop.parm("picture").set(str(dest / "frame.$F4.exr"))
    f.rop.render(frame_range=(1, 5, 1))
    summary = f.rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 5 and summary["created_sessions"] == 1 and not summary["session_alive"], (
        summary
    )
    frames = []
    for index in range(1, 6):
        image = oiio.ImageBuf(str(dest / f"frame.{index:04d}.exr"))
        pixels = image.get_pixels()
        rgb = [list(image.spec().channelnames).index(c) for c in ("R", "G", "B")]
        np.testing.assert_allclose(
            pixels[31:33, 31:33][:, :, rgb].mean((0, 1)),
            f.expected((1, 0, 0) if index % 2 else (0, 1, 0)),
            atol=0.006,
            rtol=0,
        )
        issues = json.loads(image.spec().get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        assert bool(issues) == (index == 3), issues
        frames.append(dict(frame=index, diagnostics=issues))
    report.update(result="passed", batch=frames, final=summary)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    registry.close_all()
    (dest / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
