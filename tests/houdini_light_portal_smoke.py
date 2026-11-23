# SPDX-License-Identifier: Apache-2.0
"""Light portals: Portal toggle on Cycles and native area lights, capture, and an interior noise check.

A closed room lit only by the environment through one window renders with the same mean brightness
with and without a portal in the window, and with clearly lower noise with it.
"""

from support.paths import artifacts_root
from cycles_houdini.device_selection import set_devices as set_rop_devices
import argparse
import json

import hou
import numpy as np
import OpenImageIO as OIIO
from cycles_houdini import light_properties, object_scene

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
destination = artifacts_root() / "stage3/light-portal"
destination.mkdir(parents=True, exist_ok=True)
report = dict(device=device, checks=[])
obj = hou.node("/obj")

# Interface: the toggle exists on area lights only, and a portal hides what it ignores.
portal = obj.createNode("cycles::light::1", "window_portal")
# The default this test was written for (before the Cycles Light took Blender's defaults):
# Rectangle, which shows Size Y.
portal.setParms(dict(cycles_light_type="grid", cycles_light_area_shape="rectangle", areasizex=1.0, areasizey=1.0))
portal.updateParmStates()
assert not portal.parm("cycles_light_is_portal").isHidden() and not portal.evalParm("cycles_light_is_portal")
assert not portal.parm("light_intensity").isHidden() and not portal.parm("cycles_light_spread").isHidden()
portal.parm("cycles_light_is_portal").set(1)
portal.updateParmStates()
for name in (
    "cycles_light_color",
    "light_intensity",
    "light_exposure",
    "normalizearea",
    "cycles_light_spread",
    "singlesided",
):
    assert portal.parmTuple(name).isHidden(), name
for name in ("areasizex", "areasizey", "cycles_light_area_shape", "reverse", "light_enable"):
    assert not portal.parm(name).isHidden(), name
portal.parm("cycles_light_type").set("point")
portal.updateParmStates()
assert portal.parm("cycles_light_is_portal").isHidden() and not portal.parm("light_intensity").isHidden()
portal.parm("cycles_light_type").set("grid")
native = obj.createNode("hlight::2.0", "native_portal")
native.setParms(dict(light_type="grid", singlesided=0))
assert light_properties.install(native)
assert not light_properties.install(native)
native.parm("cycles_light_is_portal").set(1)
native.updateParmStates()
assert native.parm("cycles_light_spread").isHidden() and native.parm("cycles_lightgroup").isHidden()
assert not native.parm("cycles_light_is_portal").isHidden()
definitions = object_scene.light_definitions(native, hou.time(), (hou.time(),), [])
assert len(definitions) == 1 and definitions[0]["is_portal"] and definitions[0]["kind"] == "area", definitions
native.parm("cycles_light_is_portal").set(0)
assert len(object_scene.light_definitions(native, hou.time(), (hou.time(),), [])) == 2  # two-sided again
assert "is_portal" not in object_scene.light_definitions(native, hou.time(), (hou.time(),), [])[0]
native.destroy()
report["checks"].append("Portal toggle on Cycles and native area lights; ignored controls hidden; one-sided capture")

# Render: closed room, window in the +Z wall, constant environment only.
room = obj.createNode("geo", "room")
merge = room.createNode("merge")
panels = [
    ((0, -0.05, 0), (4.2, 0.1, 4.2)),
    ((0, 3.05, 0), (4.2, 0.1, 4.2)),
    ((-2.05, 1.5, 0), (0.1, 3.2, 4.2)),
    ((2.05, 1.5, 0), (0.1, 3.2, 4.2)),
    ((0, 1.5, -2.05), (4.2, 3.2, 0.1)),
    ((-1.3, 1.5, 2.05), (1.6, 3.2, 0.1)),
    ((1.3, 1.5, 2.05), (1.6, 3.2, 0.1)),
    ((0, 0.5, 2.05), (1.0, 1.0, 0.1)),
    ((0, 2.55, 2.05), (1.0, 1.1, 0.1)),
]
for index, (center, size) in enumerate(panels):
    box = room.createNode("box")
    box.parmTuple("t").set(center)
    box.parmTuple("size").set(size)
    merge.setInput(index, box)
merge.setRenderFlag(True)
camera = obj.createNode("cam", "room_camera")
camera.parmTuple("t").set((0, 1.6, 1.8))
camera.parm("rx").set(-12)
camera.parm("focal").set(18)
sky = obj.createNode("envlight", "sky")
light_properties.install(sky)
portal.parmTuple("t").set((0, 1.5, 1.995))
rop = hou.node("/out").createNode("cycles_ipr", "portal_room")
set_rop_devices(rop, device)
rop.setParms(
    dict(
        source_mode="objects",
        objectroot="/obj",
        vobject="*",
        camera=camera.path(),
        resx=96,
        resy=64,
        adaptive_sampling=0,
        use_denoise=0,
        world_source="scene",
        use_material_assignments=0,
        material_kind="diffuse",
        roughness=0,
    )
)
rop.parmTuple("basecolor").set((0.7, 0.7, 0.7))


def render(name, lights, samples, seed=0):
    path = destination / (name + ".exr")
    rop.setParms(dict(lights=lights, samples=samples, seed=seed, picture=str(path)))
    rop.render()
    image = OIIO.ImageBuf(str(path))
    return image.get_pixels(OIIO.FLOAT)[..., :3]


reference = render("reference", "*", 2048, seed=5)
plain = render("without_portal", sky.path(), 32)
guided = render("with_portal", "*", 32)
rmse = {
    name: float(np.sqrt(((image - reference) ** 2).mean())) for name, image in (("plain", plain), ("portal", guided))
}
means = dict(reference=float(reference.mean()), plain=float(plain.mean()), portal=float(guided.mean()))
report.update(rmse=rmse, means=means)
assert abs(means["portal"] - means["reference"]) < 0.05 * means["reference"], means
assert abs(means["plain"] - means["reference"]) < 0.1 * means["reference"], means
assert rmse["plain"] > 2 * rmse["portal"], rmse
report["checks"].append("Equal mean brightness with and without the portal; at least half the error with it")
report["result"] = "passed"
(destination / "houdini-light-portal-smoke.json").write_text(json.dumps(report, indent=1))
print(json.dumps(report, indent=1))
