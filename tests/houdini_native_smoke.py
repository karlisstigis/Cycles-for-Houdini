# SPDX-License-Identifier: Apache-2.0
"""Runs inside hython; verifies package loading, HDK extraction and the native ROP."""

from support.paths import artifacts_root
from array import array
import json
from pathlib import Path
import os
import hou
import cycles_houdini as ch
from native_test_runtime import runtime_context


root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage0"
destination.mkdir(parents=True, exist_ok=True)
assert hou.applicationVersionString() == runtime_context()["houdini"]
assert "cycles_native" not in hou.nodeTypeCategories()["Driver"].nodeTypes()
assert "cycles_ipr" in hou.nodeTypeCategories()["Driver"].nodeTypes()
assert ch.capabilities()["point_spheres"] and ch.capabilities()["linear_round_curves"]
obj = hou.node("/obj")
geo = obj.createNode("geo", "cycles_smoke_geometry", run_init_scripts=False)
box = geo.createNode("box", "source")
box.parmTuple("size").set((1.7, 1.2, 0.3))
box.setRenderFlag(True)
point_colors = geo.createNode("attribwrangle", "point_colors")
point_colors.setInput(0, box)
point_colors.parm("snippet").set("v@Cd = set(0.15, 0.6, 0.25);")
vertex_colors = geo.createNode("python", "vertex_colors")
vertex_colors.setInput(0, point_colors)
vertex_colors.parm("python").set(
    "import hou\ng = hou.pwd().geometry(); g.addAttrib(hou.attribType.Vertex, 'Cd', (0.7, 0.2, 0.1))\nfor primitive in g.prims():\n    for vertex in primitive.vertices(): vertex.setAttribValue('Cd', (0.7, 0.2, 0.1))"
)
camera = obj.createNode("cam", "cycles_smoke_camera")
camera.parmTuple("t").set((0, 0, 5))
records = []
for device in ["CPU", ch.default_device()]:
    with ch.Session(device=device, width=320, height=180, samples=16) as session:
        request = ch.geometry_request(geo, box, material="red")
        update = dict(
            geometry=[request],
            camera_path=camera.path(),
            background=(0.05, 0.05, 0.05),
            materials=[dict(key="red", kind="diffuse", color=(0.8, 0.05, 0.025))],
            lights=[dict(key="key", strength=(500, 500, 500), transform=(1, 0, 0, -2, 0, 1, 0, 3, 0, 0, 1, 4))],
        )
        generation = session.apply(update)
        image = session.wait(generation)
        pixels = array("f")
        pixels.frombytes(image["planes"]["combined"]["pixels"])
        centre = (image["height"] // 2 * image["width"] + image["width"] // 2) * 4
        assert pixels[centre] > pixels[centre + 1] * 2
        first = session.status()
        session.write_exr(destination / ("native_cpu.exr" if device == "CPU" else "native_optix4090.exr"))
        # Reusing the same SOP data should avoid both extraction and engine geometry conversion.
        camera.parm("tx").set(0.1)
        generation = session.apply(dict(geometry=[request], camera_path=camera.path()))
        session.wait(generation)
        warm = session.status()
        assert warm["geometry_conversions"] == first["geometry_conversions"]
        assert warm["extractions"] == first["extractions"]
        assert warm["cache_hits"] > first["cache_hits"]
        # A SOP parameter edit must produce new geometry.
        box.parm("sizex").set(1.9)
        generation = session.apply(dict(geometry=[request]))
        session.wait(generation)
        changed = session.status()
        assert changed["geometry_conversions"] == warm["geometry_conversions"] + 1
        for node, expected in ((point_colors, (0.075, 0.6, 0.2)), (vertex_colors, (0.35, 0.2, 0.08))):
            colored = ch.geometry_request(geo, node, material="red")
            image = session.wait(
                session.apply(
                    dict(
                        geometry=[colored],
                        materials=[dict(key="red", kind="emission", color=(0.5, 1, 0.8), color_attribute="Cd")],
                    )
                )
            )
            data = array("f")
            data.frombytes(image["planes"]["combined"]["pixels"])
            assert max(abs(a - b) for a, b in zip(data[centre : centre + 3], expected)) < 0.001, (
                data[centre : centre + 3],
                expected,
            )
        conversions = session.status()["geometry_conversions"]
        point_colors.parm("snippet").set("v@Cd = set(0.25, 0.4, 0.2);")
        image = session.wait(session.apply(dict(geometry=[ch.geometry_request(geo, point_colors, material="red")])))
        data = array("f")
        data.frombytes(image["planes"]["combined"]["pixels"])
        assert max(abs(a - b) for a, b in zip(data[centre : centre + 3], (0.125, 0.4, 0.16))) < 0.001
        assert session.status()["geometry_conversions"] == conversions + 1
        point_colors.parm("snippet").set("v@Cd = set(0.15, 0.6, 0.25);")
        records.append(
            dict(
                device=device, initial=first, camera_edit=warm, geometry_edit=changed, mesh_point_vertex_color="passed"
            )
        )
        camera.parm("tx").set(0)
        box.parm("sizex").set(1.7)

rop = hou.node("/out").createNode("cycles_ipr", "cycles_smoke_rop")
rop.parm("soppath").set(box.path())
rop.parm("camera").set(camera.path())
from cycles_houdini.device_selection import set_devices, gpu_parameters

set_devices(rop, "CPU")
rop.setParms(dict(source_mode="sop", use_denoise=0, ipr_use_denoise=0))
rop.parm("resx").set(320)
rop.parm("resy").set(180)
rop.parm("samples").set(16)
rop.parm("picture").set(str(destination / "native_rop_cpu.exr"))
rop.render(frame_range=(hou.frame(), hou.frame()))
assert (destination / "native_rop_cpu.exr").is_file()
# Failed batch work must raise an error rather than leaving a successful ROP.
rop.parm("device_mode").set("gpu")
for p in gpu_parameters(rop):
    rop.parm(p.name()).set(0)
try:
    rop.render(frame_range=(hou.frame(), hou.frame()))
except hou.OperationFailed:
    pass
else:
    raise AssertionError("Unavailable device did not fail the native ROP")

report = dict(
    result="passed",
    houdini=hou.applicationVersionString(),
    license=str(hou.licenseCategory()),
    ocio=hou.getenv("OCIO"),
    capabilities=ch.capabilities(),
    devices=ch.devices(),
    renders=records,
    rop="passed",
    invalid_device="rejected",
)
(destination / "houdini-native-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
