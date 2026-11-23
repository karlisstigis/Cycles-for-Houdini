# SPDX-License-Identifier: Apache-2.0
"""Opt-in properties, a dedicated light asset, independent irradiance and updates."""

from support.paths import artifacts_root
from cycles_houdini.device_selection import set_devices as set_rop_devices
import argparse
import json
import math
import os
from pathlib import Path
import hou
import numpy as np
from cycles_houdini import light_properties, menu_actions, native_scene, object_properties, object_scene, registry
from parameter_template_checks import native_layout

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage3/light-properties"
destination.mkdir(parents=True, exist_ok=True)
scene = hou.node("/obj").createNode("subnet", "light_properties_test")
obj = scene.createNode("geo", "receiver")
assert obj.parm("vm_rendervisibility") is not None and obj.parm("cycles_subdivision_override") is None
obj.parm("tx").set(0.25)
native_template = native_layout(obj.parmTemplateGroup())
assert menu_actions.add_properties(nodes=[obj])["changed"] == [obj.path()]
assert obj.parmTemplateGroup().find("cycles_native_controls") is None
assert obj.parm("cycles_subdivision_override").containingFolders()[0] == "Cycles"
assert obj.parm("tx").containingFolders()[0] == "Transform"
assert obj.parm("vm_rendervisibility").containingFolders()[0] == "Render"
assert not menu_actions.add_properties(nodes=[obj])["changed"]
obj.parm("cycles_subdivision_ipr_level").set(3)
assert menu_actions.remove_properties(nodes=[obj]) == [obj.path()]
assert obj.parm("cycles_subdivision_ipr_level") is None and obj.evalParm("tx") == 0.25
assert native_layout(obj.parmTemplateGroup()) == native_template
obj.parm("tx").set(0)


obj.parm("tx").deleteAllKeyframes()
obj.parm("tx").set(0)
obj.parm("ty").deleteAllKeyframes()
obj.parm("ty").set(0)
grid = obj.createNode("grid", "native_receiver")
grid.setParms(dict(type=0, orient=0, rows=2, cols=2))
grid.parmTuple("size").set((8, 8))
grid.setRenderFlag(True)
assert all(abs(point.position()[2]) < 1e-6 for point in grid.geometry().points())
native = scene.createNode("hlight::2.0", "native_area")
native.setParms(dict(light_type="disk", coneenable=1, coneangle=36, conedelta=7, coneroll=2))
native.parm("tx").setExpression("$F*.1", hou.exprLanguage.Hscript)
native_before = native_layout(native.parmTemplateGroup())
assert native.parm("cycles_light_visibility_override") is None
assert menu_actions.add_properties("lights", [native])["changed"] == [native.path()]
assert native.parmTemplateGroup().find("cycles_native_controls") is None
assert native.parm("cycles_light_radius").containingFolders()[0] == "Cycles Light"
assert native.evalParm("light_type") == native.parm("light_type").parmTemplate().menuItems().index("disk")
assert native.evalParm("coneangle") == 36 and native.evalParm("conedelta") == 7 and native.evalParm("coneroll") == 2
assert native.parm("tx").expression() == "$F*.1"
assert native.parm("coneenable").containingFolders()[0] == "Light"
assert not menu_actions.add_properties("lights", [native])["changed"]
assert menu_actions.remove_properties([native]) == [native.path()]
assert native_layout(native.parmTemplateGroup()) == native_before
assert native.parm("tx").expression() == "$F*.1"
native.destroy()
light = scene.createNode(light_properties.LIGHT_TYPE, "cycles_area")
light.setParms(dict(cycles_light_type="grid", tz=3, light_intensity=100, light_exposure=0, singlesided=0))
light.setParms(dict(areasizex=1, areasizey=1))
# Camera ray visibility defaults off (Blender); this fixture also checks the visible emitter surface.
light.setParms(dict(cycles_light_visibility_camera=1))
assert light.type().definition().extraInfoValue("subtype") == "light"
assert scene.recursiveGlob("*", filter=hou.nodeTypeFilter.ObjLight) == (light,)
assert light.children() == (light.displayNode(),)
assert light.displayNode().type().category() == hou.sopNodeTypeCategory()
assert light.parmTemplateGroup().find("cycles_native_controls") is None
assert light.parm("tx").containingFolders()[0] == "Transform"
assert light.parm("cycles_light_radius").containingFolders()[0] == "Cycles Light"
assert light.evalParm("light_intensity") == 100 and light.evalParm("light_contribprimary") == 1
camera = hou.node("/obj").createNode("cam", "light_properties_camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=4))
rop = hou.node("/out").createNode("cycles_ipr", "light_properties_rop")
set_rop_devices(rop, device)
rop.setParms(
    dict(
        source_mode="objects",
        objectroot=scene.path(),
        resx=96,
        resy=64,
        samples=1024,
        camera=camera.path(),
        material_kind="diffuse",
        use_material_assignments=0,
        roughness=0,
    )
)
rop.parmTuple("basecolor").set((1, 1, 1))
rop.parmTuple("background").set((0, 0, 0))
assert object_scene.selected(rop, hou.time(), lights=True) == [light], (
    "Viewport preview must not become a duplicate renderer light"
)
entry = registry.for_rop(rop, native_scene.configuration(rop))
checks = []


def render():
    return entry.session.wait(entry.apply(native_scene.capture(rop), hou.time()))


def pixels(frame):
    return np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).reshape(64, 96, 4)


def receiver(frame):
    return float(pixels(frame)[31:33, 71:73, :3].mean())


def record(name, **details):
    checks.append(dict(name=name, status=entry.session.status(), **details))


try:
    first = render()
    converted = entry.session.status()["geometry_conversions"]
    # Independent midpoint irradiance integration at receiver x=1,z=0.
    n = 512
    x = (np.arange(n) + 0.5) / n - 0.5
    y = x.copy()
    distance = 3
    integral = float(np.sum(distance**2 / (distance**2 + y[:, None] ** 2 + (x[None, :] - 1) ** 2) ** 2) / n**2)
    expected = 50 / math.pi**2 * integral
    measured = receiver(first)
    assert abs(measured - expected) < expected * 0.025, (measured, expected)
    assert pixels(first)[32, 48, 0] > 10 and entry.session.status()["light_resources"] == 2
    record(
        "Cycles area illuminates the receiver and its two-sided surface is visible",
        expected=expected,
        measured=measured,
    )
    light.parm("cycles_light_visibility_camera").set(0)
    hidden = render()
    assert pixels(hidden)[32, 48, 0] < 1 and abs(receiver(hidden) - measured) < 0.005
    assert entry.session.status()["geometry_conversions"] == converted
    light.parm("cycles_light_visibility_camera").set(1)
    render()
    light.parm("light_exposure").set(1)
    brighter = render()
    assert abs(receiver(brighter) / measured - 2) < 0.04
    record("Camera visibility and exposure update the light while retaining receiver geometry")
    light.parm("cycles_light_area_shape").set("disk")
    disk = render()
    definitions = object_scene.light_definitions(light, hou.time(), [hou.time()])
    assert all(item["ellipse"] for item in definitions)
    assert np.isfinite(pixels(disk)).all() and not np.array_equal(pixels(brighter), pixels(disk))
    light.setParms(
        dict(
            cycles_light_type="spot",
            cycles_light_visibility_camera=0,
            cycles_light_radius=0,
            cycles_light_spot_angle=30,
            cycles_light_spot_smooth=0,
        )
    )
    assert light.evalParm("coneenable") == 1
    assert light.parm("light_type").evalAsString() == "point"
    assert abs(light.evalParm("coneangle") - 30) < 1e-5
    assert abs(light.evalParm("conedelta")) < 1e-5
    spot = render()
    assert pixels(spot)[32, 48, 0] > 0.01 and pixels(spot)[32, 84, 0] < 1e-5
    light.parm("cycles_light_spot_angle").set(90)
    wide = render()
    assert pixels(wide)[32, 84, 0] > 0.001
    record("Disk changes actual light shape; widening a spot cone illuminates the independently located outer receiver")
    light.setParms(dict(cycles_light_type="grid", cycles_light_area_shape="rectangle", cycles_light_spread=180))
    wide = render()
    light.parm("cycles_light_spread").set(10)
    narrow = render()
    assert receiver(narrow) < receiver(wide) * 0.1
    record("Area spread changes actual illumination")
    light.setParms(dict(cycles_light_type="sun", vm_envangle=0, light_intensity=1, light_exposure=0))
    sun = render()
    assert abs(receiver(sun) - 1 / math.pi) < 0.002
    light.setParms(dict(cycles_light_type="point", cycles_light_radius=0))
    point = render()
    assert receiver(point) > 0
    light.parm("normalizearea").set(0)
    radiance = render()
    assert abs(receiver(radiance) / receiver(point) - 4) < 0.08
    light.parm("normalizearea").set(1)
    record("Point normalization is wired: zero-radius non-normalized intensity is four times normalized intensity")
    for shape, size in (
        ("square", (1.5, 1.5)),
        ("rectangle", (1.5, 0.75)),
        ("disk", (1.5, 1.5)),
        ("ellipse", (1.5, 0.75)),
    ):
        light.setParms(dict(cycles_light_type="grid", cycles_light_area_shape=shape, cycles_light_spread=180))
        light.setParms(dict(areasizex=1.5, areasizey=0.75))
        definition = object_scene.light_definitions(light, hou.time(), [hou.time()])[0]
        assert definition["ellipse"] == (shape in ("disk", "ellipse")) and tuple(definition["size"]) == size
        assert light.parm("light_type").evalAsString() == ("disk" if definition["ellipse"] else "grid")
        shaped = render()
        assert np.isfinite(pixels(shaped)).all() and receiver(shaped) > 0
        assert light.parmTuple("areasize").eval() == size
    record("All four Area shapes render with the same dimensions as their native viewport approximation")
    light.parm("cycles_light_type").set("point")
    light.parm("light_enable").set(0)
    off = render()
    assert receiver(off) == 0 and entry.session.status()["light_resources"] == 0
    assert entry.session.status()["geometry_conversions"] == converted
    record("Sun has analytic Lambertian brightness; point, type switches and disable retire the correct resources")
    path = destination / "cycles-light-properties.hiplc"
    light_path = light.path()
    hou.hipFile.save(str(path))
    registry.close_all()
    hou.hipFile.load(str(path), suppress_save_prompt=True)
    loaded = hou.node(light_path)
    assert loaded.type().name() == light_properties.LIGHT_TYPE and loaded.evalParm("cycles_light_type") == "point"
    assert loaded.type().definition().extraInfoValue("subtype") == "light"
    assert tuple(loaded.parm("cycles_light_type").parmTemplate().menuItems()) == ("point", "spot", "grid", "sun")
    for name in (
        "cycles_light_layout_version",
        "cycles_light_visibility_override",
        "cycles_shadow_objects",
    ):
        assert loaded.parm(name) is None, name
    loaded.setParms(
        dict(
            cycles_light_type="grid",
            cycles_light_area_shape="ellipse",
            cycles_light_visibility_camera=0,
            cycles_light_cast_shadow=0,
        )
    )
    loaded.setParms(dict(areasizex=2, areasizey=1))
    assert loaded.evalParm("light_contribprimary") == 0
    assert loaded.parm("shadow_type").evalAsString() == "off"
    assert loaded.parm("light_type").evalAsString() == "disk"
    assert loaded.parmTuple("areasize").eval() == (2, 1)
    definition = object_scene.light_definitions(loaded, hou.time(), [hou.time()])[0]
    assert definition["kind"] == "area" and definition["ellipse"] and not definition["cast_shadow"]
    assert not definition["visibility"] & 1 and tuple(definition["size"]) == (2, 1)
    record("Current light fields directly drive the renderer and native OBJ light")
    report = dict(
        result="passed", device=device, execution=os.environ.get("CYCLES_HOUDINI_EXECUTION", "sync"), checks=checks
    )
    (destination / "houdini-light-properties-smoke.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(dict(result="passed", checks=len(checks))))
finally:
    registry.close_all()
