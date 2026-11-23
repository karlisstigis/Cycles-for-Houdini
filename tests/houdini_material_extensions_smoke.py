# SPDX-License-Identifier: Apache-2.0
"""Artist-authored numeric/color graphs and independently specified host inputs."""

from support.paths import artifacts_root
from cycles_houdini.device_selection import set_devices as set_rop_devices
import argparse
import json
import os
from pathlib import Path
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene, passes

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage3/materials/extensions"
destination.mkdir(parents=True, exist_ok=True)
obj = hou.node("/obj").createNode("geo", "material_extensions")
grid = obj.createNode("grid", "native_grid")
grid.setParms(dict(type=0, orient=0, rows=2, cols=2))
grid.parmTuple("size").set((4, 4))
grid.setRenderFlag(True)
cd = obj.createNode("attribwrangle", "float_color")
cd.setInput(0, grid)
cd.parm("snippet").set("v@Cd={1.2,.4,.25};")
cd.setRenderFlag(True)
camera = hou.node("/obj").createNode("cam", "extension_camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=3))
builder = ch.create_material(name="native_extensions", shader="emission")
emission = builder.node("surface_shader")
emission.parmTuple("color").set((0.2, 0.4, 0.8))
emission.parm("strength").set(1)
rop = hou.node("/out").createNode("cycles_ipr", "extension_rop")
set_rop_devices(rop, device)
rop.setParms(
    dict(
        source_mode="sop",
        soppath=cd.path(),
        camera=camera.path(),
        materialpath=builder.path(),
        resx=160,
        resy=120,
        samples=32,
    )
)
passes.add(rop, "position")
rop.parmTuple("background").set((0, 0, 0))
rop.parmTuple("lightstrength").set((0, 0, 0))
checks = []


def pixels(frame, name="combined"):
    plane = frame["planes"][name]
    return np.frombuffer(plane["pixels"], np.float32).reshape(120, 160, plane["channels"])


def center(frame):
    return pixels(frame)[58:62, 78:82, :3].mean((0, 1))


with ch.Session(device=device, width=160, height=120, samples=32, collect_diagnostics=True) as session:

    def render(at_time=None):
        return session.wait(session.apply(native_scene.capture(rop, at_time=at_time), at_time=at_time))

    first = render()
    conversions = session.status()["geometry_conversions"]
    clock = builder.createNode("cycles::scene_time::1", "host_time")
    emission.setNamedInput("strength", clock, "frame")
    timed = render(hou.frameToTime(5))
    assert np.allclose(center(timed), (0.2 * 5, 0.4 * 5, 0.8 * 5), atol=1e-5), center(timed)
    emission.setNamedInput("strength", clock, "seconds")
    timed = render(0.25)
    assert np.allclose(center(timed), (0.05, 0.1, 0.2), atol=1e-5), center(timed)
    assert session.status()["geometry_conversions"] == conversions
    checks.append("Scene Time evaluates Houdini frame/seconds explicitly without recooking the mesh")
    emission.setInput(emission.inputIndex("strength"), None)
    emission.parm("strength").set(1)
    mapping = builder.createNode("cycles::map_range::1", "numeric_range")
    # The default this test was written for (new nodes now take Blender's).
    mapping.setParms(dict(clamp=0))
    mapping.setParms(dict(value=0.25, from_min=0, from_max=1, to_min=0, to_max=2))
    emission.setNamedInput("color", mapping, "result")
    mapped = render()
    assert np.allclose(center(mapped), (0.5, 0.5, 0.5), atol=1e-5), center(mapped)
    mixer = builder.createNode("cycles::mix_color::1", "native_color_mix")
    mixer.parm("fac").set(0.25)
    mixer.parmTuple("a").set((1, 0, 0))
    mixer.parmTuple("b").set((0, 0, 1))
    emission.setNamedInput("color", mixer, "result")
    mixed = render()
    assert np.allclose(center(mixed), (0.75, 0, 0.25), atol=1e-5), center(mixed)
    checks.append("Map Range float-to-color conversion and Mix Color obey independently authored values")
    coordinates = builder.createNode("cycles::texture_coordinate::1", "normalized_sop_bounds")
    emission.setNamedInput("color", coordinates, "generated")
    generated = render()
    position = pixels(generated, "position")
    expected = np.zeros((120, 160, 3), np.float32)
    expected[:, :, 0] = (position[:, :, 0] + 2) / 4
    expected[:, :, 1] = (position[:, :, 1] + 2) / 4
    assert np.max(np.abs(pixels(generated)[:, :, :3] - expected)) < 0.006
    before = session.status()
    camera.parm("tx").set(0.1)
    render()
    after = session.status()
    assert after["geometry_conversions"] == before["geometry_conversions"]
    assert not {"tri_verts", "tri_vindex", "attributes_float3"} & session.diagnostics()["buffer_copy_requests"].keys()
    checks.append("Generated coordinates normalize undeformed SOP bounds and remain on the GPU through camera edits")
    camera.parm("tx").set(0)
    color = builder.createNode("cycles::vertex_color::1", "houdini_cd")
    color.parm("layer_name").set("Cd")
    emission.setNamedInput("color", color, "color")
    colored = render()
    assert np.allclose(center(colored), (1.2, 0.4, 0.25), atol=1e-5), center(colored)
    checks.append("Color Attribute reads HDR float Houdini Cd without byte quantization")
    geometry = builder.createNode("cycles::geometry::1", "intrinsic_geometry")
    emission.setNamedInput("color", geometry, "pointiness")
    pointiness = render()
    assert np.allclose(center(pointiness), (0.5, 0.5, 0.5), atol=1e-5), center(pointiness)
    assert not session.status()["source_diagnostics"], session.status()
    emission.setNamedInput("color", color, "color")
    render()
    checks.append("Pointiness derives flat geometric curvature without an authored attribute")
    curve = obj.createNode("python", "independent_curve_keys")
    curve.parm("python").set("""import hou
g=hou.pwd().geometry();g.addAttrib(hou.attribType.Point,'width',.2)
face=g.createPolygon(is_closed=False)
for y in (-1,0,1):
    point=g.createPoint();point.setPosition((0,y,0));face.addVertex(point)
""")
    rop.parm("soppath").set(curve.path())
    hair = builder.createNode("cycles::hair_info::1", "native_curve_metadata")
    emission.setNamedInput("color", hair, "size")
    length = render()
    assert np.allclose(center(length), (2, 2, 2), atol=1e-5), center(length)
    emission.setNamedInput("color", hair, "intercept")
    intercept = render()
    assert np.allclose(center(intercept), (0.5, 0.5, 0.5), atol=0.02), center(intercept)
    checks.append("Hair Info length and root-to-tip intercept use the independently specified control polyline")
report = dict(
    result="passed", device=device, execution=os.environ.get("CYCLES_HOUDINI_EXECUTION", "sync"), checks=checks
)
(destination / "houdini-material-extensions-smoke.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
