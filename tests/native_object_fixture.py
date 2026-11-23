# SPDX-License-Identifier: Apache-2.0
"""Authored native objects with independently known transforms and colors."""

from cycles_houdini.device_selection import set_devices as set_rop_devices
from types import SimpleNamespace
import hou
import cycles_houdini as ch


def fixture(device="CPU", size=(128, 96), samples=8):
    scene = hou.node("/obj").createNode("subnet", "native_objects")
    parent = scene.createNode("subnet", "nested")
    parent.parm("tx").set(1)
    colors = {"red": (0.8, 0.1, 0.05), "blue": (0.05, 0.1, 0.8), "green": (0.05, 0.7, 0.1)}
    materials = {}
    for name, color in colors.items():
        material = ch.create_material(name="native_object_" + name, shader="emission")
        material.node("surface_shader").parmTuple("color").set(color)
        material.node("surface_shader").parm("strength").set(1)
        materials[name] = material
    code = """import hou
g=hou.pwd().geometry();face=g.createPolygon()
for x,y in ((-.4,-.4),(.4,-.4),(.4,.4),(-.4,.4)):
    p=g.createPoint();p.setPosition((x,y,0));face.addVertex(p)
"""

    def geometry(network, name, color, x, y=0):
        obj = network.createNode("geo", name, run_init_scripts=False)
        source = obj.createNode("python", "render_surface")
        source.parm("python").set(code)
        source.setRenderFlag(True)
        display = obj.createNode("box", "different_display_geometry")
        display.setDisplayFlag(True)
        obj.parmTuple("t").set((x, y, 0))
        obj.parm("shop_materialpath").set(materials[color].path())
        if obj.parm("vm_rendervisibility") is None:
            obj.addSpareParmTuple(
                hou.StringParmTemplate("vm_rendervisibility", "Render Visibility", 1, default_value=("*",))
            )
        return obj, source, display

    red, red_source, red_display = geometry(parent, "red_object", "red", -2)
    blue, blue_source, blue_display = geometry(scene, "blue_object", "blue", 1)
    hidden, hidden_source, _ = geometry(scene, "hidden_object", "green", 0, 1)
    hidden.setDisplayFlag(False)
    # All filtering is rooted at our object network; unrelated /obj content is ignored.
    unrelated = hou.node("/obj").createNode("geo", "outside_scene_root", run_init_scripts=False)
    unrelated.createNode("sphere", "unsupported_outside_root").setRenderFlag(True)
    camera = hou.node("/obj").createNode("cam", "native_objects_camera")
    camera.parm("tz").set(5)
    camera.parm("projection").set(1)
    camera.parm("orthowidth").set(4)
    light = scene.createNode("hlight", "native_point_light")
    light.parmTuple("t").set((-4, 0, 5))
    light.parm("light_intensity").set(100)
    rop = hou.node("/out").createNode("cycles_ipr", "native_objects_rop")
    set_rop_devices(rop, device)
    for name, value in dict(
        source_mode="objects",
        objectroot=scene.path(),
        camera=camera.path(),
        resx=size[0],
        resy=size[1],
        samples=samples,
        material_kind="emission",
    ).items():
        rop.parm(name).set(value)
    rop.parmTuple("background").set((0, 0, 0))
    rop.parm("transparent_background").set(1)
    return SimpleNamespace(**locals())
