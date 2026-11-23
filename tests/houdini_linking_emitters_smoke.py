# SPDX-License-Identifier: Apache-2.0
"""Independent mesh/world linking and shared packed-root bindings."""

import argparse, json, traceback
import hou, numpy as np
import cycles_houdini as ch
from cycles_houdini import object_properties, light_properties, registry
from light_shader_fixture import Fixture
from cycles_houdini import passes

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
f = Fixture(p.parse_args().device)
passes.add(f.rop, "position")
object_properties.install(f.obj)
f.light.parm("light_enable").set(0)
f.rop.parmTuple("background").set((0.1, 0.2, 0.4))
f.rop.setParms(dict(samples=1024, ipr_samples=1024, world_sampling="none"))
entry = f.session()
report = dict(result="running", device=f.device, checks=[])
dest = f.dest.parent / "linking-emitters"
dest.mkdir(exist_ok=True)


def render(label, expected=None, tolerance=0.006, code=None):
    frame = f.update()
    status = entry.session.status()
    pixels = f.pixels(frame)
    assert np.isfinite(pixels).all()
    if expected is not None:
        np.testing.assert_allclose(pixels[31:33, 31:33, :3].mean((0, 1)), expected, atol=tolerance, rtol=0)
    assert (
        status["state"] == "complete"
        and {d["code"] for d in status["source_diagnostics"]} == ({code} if code else set())
        and status["render_device_ids"] == [f.device]
    ), status
    report["checks"].append(dict(label=label, status=status))
    return frame


def visibility(obj, text):
    if obj.parm("vm_rendervisibility") is None:
        obj.addSpareParmTuple(hou.StringParmTemplate("vm_rendervisibility", "Visibility", 1))
    obj.parm("vm_rendervisibility").set(text)


def material(obj, builder):
    if obj.parm("shop_materialpath") is None:
        obj.addSpareParmTuple(hou.StringParmTemplate("shop_materialpath", "Material", 1))
    obj.parm("shop_materialpath").set(builder.path())


def grid(name, z):
    obj = f.scene.createNode("geo", name, run_init_scripts=False)
    sop = obj.createNode("grid")
    sop.setParms(dict(type=0, orient=0, rows=2, cols=2))
    sop.parmTuple("size").set((2, 2))
    sop.setDisplayFlag(True)
    sop.setRenderFlag(True)
    obj.parm("tz").set(z)
    return obj


try:
    f.obj.parm("lightmask").set("__world")
    render("World alias preserves independently known constant radiance", (0.1, 0.2, 0.4))
    converted = entry.session.status()["geometry_conversions"]
    pid = entry.session.status()["renderer_pid"]
    f.obj.parm("lightmask").set("* ^__world")
    render(
        "Unsupported world exclusion retains world illumination with a local diagnostic",
        (0.1, 0.2, 0.4),
        code="linking.world_unsupported",
    )
    f.camera.parm("tx").set(10)
    render("Primary camera still sees the world", (0.1, 0.2, 0.4), code="linking.world_unsupported")
    f.camera.parm("tx").set(0)
    f.obj.parm("lightmask").set("__world")
    f.rop.parm("world_sampling").set("auto")
    render("World linking also works with environment MIS", (0.1, 0.2, 0.4), 0.012)
    f.obj.parm("lightmask").set("* ^__world")
    render(
        "Environment MIS also preserves the documented world-linking limitation",
        (0.1, 0.2, 0.4),
        0.012,
        "linking.world_unsupported",
    )
    world = ch.create_world_material(name="linking_world")
    background = world.node("background_shader")
    background.parmTuple("color").set((0, 0, 0))
    background.parm("strength").set(0)
    f.rop.setParms(dict(world_source="material", world_material=world.path()))
    render("Inactive world graph does not invent an unsupported-link warning", (0, 0, 0))
    background.parmTuple("color").set((0.1, 0.2, 0.4))
    background.parm("strength").set(1)
    render(
        "Activating a world graph updates its linking diagnostic without losing illumination",
        (0.1, 0.2, 0.4),
        0.012,
        "linking.world_unsupported",
    )
    f.obj.parm("lightmask").set("*")
    render("Repairing world selection clears its diagnostic", (0.1, 0.2, 0.4), 0.012)
    assert entry.session.status()["geometry_conversions"] == converted
    f.rop.parmTuple("background").set((0, 0, 0))
    f.obj.parm("lightmask").set("*")
    f.rop.setParms(dict(use_material_assignments=1, world_source="color"))
    emitter = grid("mesh_emitter", 3)
    emitter.parm("ry").set(180)
    object_properties.install(emitter)
    visibility(emitter, "-primary")
    emission = ch.create_material(name="linking_emission", shader="emission")
    emission.node("surface_shader").parmTuple("color").set((1, 0, 0))
    emission.node("surface_shader").parm("strength").set(2)
    material(emitter, emission)
    n = 512
    xy = (np.arange(n) + 0.5) * 2 / n - 1
    integral = float(np.sum(9 / (9 + xy[:, None] ** 2 + xy[None, :] ** 2) ** 2) * 4 / n**2)
    expected = (2 * integral / np.pi, 0, 0)
    baseline = render("Mesh emission agrees with an independent area integral", expected, 0.012)
    converted = entry.session.status()["geometry_conversions"]
    f.obj.parm("lightmask").set("__world")
    render("Object mask can exclude mesh emission", (0, 0, 0))
    f.obj.parm("lightmask").set(emitter.path())
    render("Object mask can select a mesh emitter", expected, 0.012)
    emitter.parm("cycles_illuminates").set("")
    render("Mesh emitter receiver selection omits its illumination", (0, 0, 0))
    visibility(emitter, "*")
    render("Excluded mesh emission remains directly visible to the camera", (2, 0, 0))
    visibility(emitter, "-primary")
    emitter.parm("cycles_illuminates").set(f.obj.path())
    render("Mesh emission receiver repair restores illumination", expected, 0.012)
    f.rop.parm("light_tree").set(0)
    render("Linked mesh illumination remains correct without light-tree sampling", expected, 0.016)
    f.obj.parm("lightmask").set("__world")
    render("Mesh exclusion still works without light-tree sampling", (0, 0, 0))
    f.obj.parm("lightmask").set(emitter.path())
    f.rop.parm("light_tree").set(1)
    assert entry.session.status()["geometry_conversions"] == converted
    blocker = grid("mesh_shadow_blocker", 1.5)
    visibility(blocker, "-primary")
    black = ch.create_material(name="mesh_linking_black", shader="diffuse_bsdf")
    black.node("surface_shader").parmTuple("color").set((0, 0, 0))
    material(blocker, black)
    render("Mesh emitter shadow is independently occluded", (0, 0, 0))
    converted = entry.session.status()["geometry_conversions"]
    emitter.parm("cycles_shadow_objects").set(f.obj.path())
    render("Mesh shadow linking omits only the blocker", expected, 0.012)
    emitter.parm("cycles_shadow_objects").set("*")
    render("Mesh shadow repair restores occlusion", (0, 0, 0))
    assert entry.session.status()["geometry_conversions"] == converted
    blocker.destroy()
    emitter.destroy()
    f.obj.parm("lightmask").set("*")
    f.rop.setParms(dict(use_material_assignments=0, max_bounce=0, diffuse_bounce=0))
    f.light.setParms(dict(light_enable=1, light_intensity=100))
    f.light.parmTuple("cycles_light_color").set((1, 0, 0))
    green = f.scene.createNode(light_properties.LIGHT_TYPE, "packed_green")
    green.setParms(dict(cycles_light_type="point", tz=3, light_intensity=100, light_exposure=0, cycles_light_radius=0))
    green.parmTuple("cycles_light_color").set((0, 1, 0))
    source = f.scene.createNode("geo", "prototype_source", run_init_scripts=False)
    torus = source.createNode("torus")
    torus.setParms(dict(type=0, rows=128, cols=256))
    pack = source.createNode("pack")
    pack.setInput(0, torus)
    pack.setDisplayFlag(True)
    pack.setRenderFlag(True)
    source.setDisplayFlag(False)
    roots = []
    for name, x, light in [("packed_left", -1.2, f.light), ("packed_right", 1.2, green)]:
        obj = f.scene.createNode("geo", name, run_init_scripts=False)
        merge = obj.createNode("object_merge")
        merge.setParms(dict(objpath1=pack.path(), xformtype=0))
        merge.setDisplayFlag(True)
        merge.setRenderFlag(True)
        obj.parmTuple("t").set((x, 0, 1))
        object_properties.install(obj)
        obj.parm("lightmask").set(light.path())
        roots.append(obj)
    f.rop.parm("vobject").set("receiver packed_left packed_right")

    def isolated(frame, swapped=False):
        rgb = f.pixels(frame)[:, :, :3]
        position = f.pixels(frame, "position")
        for negative, channel in [(True, 1 if swapped else 0), (False, 0 if swapped else 1)]:
            mask = (position[:, :, 2] > 0.25) & ((position[:, :, 0] < -0.2) if negative else (position[:, :, 0] > 0.2))
            assert mask.sum() > 20
            measured = rgb[mask].mean(0)
            assert measured[channel] > 0.01 and np.max(np.delete(measured, channel)) < 0.002, measured

    frame = render("Two packed OBJ roots share one prototype with independent light masks")
    isolated(frame)
    status = entry.session.status()
    assert status["packed_instances"] == 2 and status["geometry_resources"] == 2, status
    converted = status["geometry_conversions"]
    roots[0].parm("lightmask").set(green.path())
    roots[1].parm("lightmask").set(f.light.path())
    frame = render("Swapping packed light selections keeps the prototype shared")
    isolated(frame, True)
    assert (
        entry.session.status()["geometry_conversions"] == converted
        and entry.session.status()["geometry_resources"] == 2
    )
    copies = entry.session.diagnostics()["buffer_copy_requests"]
    assert not {"tri_verts", "tri_vindex"} & copies.keys(), copies
    assert entry.session.status()["renderer_pid"] == pid
    report.update(result="passed", area_integral=integral)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    registry.close_all()
    (dest / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
