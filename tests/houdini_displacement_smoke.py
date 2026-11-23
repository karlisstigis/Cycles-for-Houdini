# SPDX-License-Identifier: Apache-2.0
"""Native displacement: independent positions, source ownership and variants."""

from support.paths import artifacts_root
from cycles_houdini.device_selection import set_devices as set_rop_devices
from cycles_houdini import passes
from copy import deepcopy
import json
import os
from pathlib import Path

import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage2/displacement"
destination.mkdir(parents=True, exist_ok=True)
hou.setFrame(1)
obj = hou.node("/obj").createNode("geo", "displacement_receiver", run_init_scripts=False)
raw_grid = obj.createNode("grid")
raw_grid.parm("type").set(0)
raw_grid.parm("orient").set(0)
raw_grid.parm("rows").set(2)
raw_grid.parm("cols").set(2)
raw_grid.parmTuple("size").set((1, 1))
# The XY Grid faces -Z in Houdini. State the fixture's +Z normal explicitly.
grid = obj.createNode("reverse", "positive_z_receiver")
grid.setInput(0, raw_grid)
assert np.allclose(tuple(grid.geometry().prims()[0].normal()), (0, 0, 1))
camera = hou.node("/obj").createNode("cam", "displacement_camera")
camera.parm("tz").set(5)
camera.parm("projection").set(1)
camera.parm("orthowidth").set(4)
builder = ch.create_material(name="native_displacement", shader="emission")
builder.node("surface_shader").parmTuple("color").set((0.2, 0.4, 0.8))
builder.node("surface_shader").parm("strength").set(1)
displacement = builder.createNode("cycles::vector_displacement::1", "offset")
# Object space at Scale 1, set: the node defaults are Blender's (Tangent, 0.01).
displacement.setParms(dict(space="object", scale=1.0))
displacement.parmTuple("vector").set((0, 0, 0.3))
builder.node("surface_output").setNamedInput("displacement", displacement, "displacement")
builder.parm("cycles_displacement_method").set("true")
seam_builders = []
for index, z in enumerate((0.1, 0.5)):
    material = ch.create_material(name="native_displacement_seam_" + str(index), shader="emission")
    material.node("surface_shader").parmTuple("color").set((0.2, 0.4, 0.8))
    material.node("surface_shader").parm("strength").set(1)
    offset = material.createNode("cycles::vector_displacement::1")
    offset.setParms(dict(space="object", scale=1.0))  # not the Blender defaults (Tangent, 0.01)
    offset.parmTuple("vector").set((0, 0, z))
    material.node("surface_output").setNamedInput("displacement", offset, "displacement")
    material.parm("cycles_displacement_method").set("true")
    seam_builders.append(material)
seams = obj.createNode("python", "material_seams")
seams.parm("python").set(
    """import hou
g = hou.pwd().geometry(); g.addAttrib(hou.attribType.Prim, 'shop_materialpath', '')
points = []
for position in [(-1,-.5,0),(0,-.5,0),(1,-.5,0),(-1,.5,0),(0,.5,0),(1,.5,0)]:
    point = g.createPoint(); point.setPosition(position); points.append(point)
for indices, path in zip([(0,1,4,3),(1,2,5,4)], %r):
    polygon = g.createPolygon(); polygon.setAttribValue('shop_materialpath', path)
    for index in reversed(indices): polygon.addVertex(points[index])
"""
    % [material.path() for material in seam_builders]
)
uv_grid = obj.createNode("python", "known_uv_plane")
uv_grid.parm("python").set("""import hou
g = hou.pwd().geometry(); g.addAttrib(hou.attribType.Vertex, 'uv', (0.,0.,0.))
polygon = g.createPolygon()
for position, uv in reversed([((-.5,-.5,0),(0,0,0)),((.5,-.5,0),(1,0,0)),((.5,.5,0),(1,1,0)),((-.5,.5,0),(0,1,0))]):
    point = g.createPoint(); point.setPosition(position); vertex = polygon.addVertex(point); vertex.setAttribValue('uv', tuple(float(value) for value in uv))
""")
uv_grid.geometry()
assert not uv_grid.errors(), uv_grid.errors()
moving = obj.createNode("xform", "deforming_plane")
moving.setInput(0, grid)
moving.parm("tx").setExpression("$T * 24", hou.exprLanguage.Hscript)


def graph_material():
    return dict(key="surface", kind="graph", graph=ch.material_graph(builder))


def planes(frame, name):
    plane = frame["planes"][name]
    return np.frombuffer(plane["pixels"], dtype=np.float32).reshape(frame["height"], frame["width"], plane["channels"])


def front(frame, x=0, z=0.3):
    # Horizontal orthographic width 4, camera Z=5, and a +Z-facing plane.
    column = round((x / 4 + 0.5) * frame["width"])
    row = frame["height"] // 2
    position = planes(frame, "position")[row, column]
    depth = planes(frame, "depth")[row, column, 0]
    assert abs(float(position[2]) - z) < 0.001, (position.tolist(), z)
    assert abs(float(depth) - (5 - z)) < 0.001, (depth, z)
    assert np.max(np.abs(planes(frame, "combined")[row, column, :3] - (0.2, 0.4, 0.8))) < 0.003
    return position.tolist()


def request(**extra):
    return dict(ch.geometry_request(obj, grid, material="surface"), **extra)


def reject(session, update, fragment):
    before = session.status()
    try:
        session.apply(update)
    except (RuntimeError, ValueError) as error:
        assert fragment in str(error), str(error)
    else:
        raise AssertionError("Accepted unsupported displacement: " + fragment)
    assert session.status()["generation"] == before["generation"]
    assert session.status()["owned_geometry_bytes"] == before["owned_geometry_bytes"]


reports = []
for device in ("CPU", ch.default_device()):
    checks = []
    camera.parm("tx").set(0)
    obj.parmTuple("s").set((1, 1, 1))
    displacement.parm("space").set("object")
    displacement.parmTuple("vector").set((0, 0, 0.3))
    builder.parm("cycles_displacement_method").set("true")
    with ch.Session(device=device, width=160, height=90, samples=16, collect_diagnostics=True) as session:
        frame = session.wait(
            session.apply(
                dict(
                    geometry=[request()], materials=[graph_material()], camera_path=camera.path(), background=(0, 0, 0)
                )
            )
        )
        checks.append(dict(name="Native object vector displacement", position=front(frame), status=session.status()))
        before = session.status()
        camera.parm("tx").set(0.1)
        frame = session.wait(session.apply(dict(camera_path=camera.path())))
        front(frame)
        after = session.status()
        assert after["geometry_conversions"] == before["geometry_conversions"]
        assert after["displacement_rebuilds"] == before["displacement_rebuilds"]
        assert not any(
            value["calls"]
            for name, value in session.diagnostics()["buffer_copy_requests"].items()
            if name in ("prim_tri_verts", "prim_tri_index", "attributes_float", "attributes_float3")
        )
        checks.append(dict(name="Camera edit retains displaced geometry and buffers", status=after))
        camera.parm("tx").set(0)
        warm = []
        for iteration in range(24):
            offset = 0.1 if iteration % 2 else 0.4
            displacement.parmTuple("vector").set((0, 0, offset))
            frame = session.wait(session.apply(dict(materials=[graph_material()])))
            front(frame, z=offset)
            status = session.status()
            assert status["geometry_conversions"] == before["geometry_conversions"]
            if iteration >= 8:
                warm.append((iteration % 2, status["owned_geometry_bytes"], status["device_allocation_bytes"]))
        for phase in (0, 1):
            assert len({entry[1:] for entry in warm if entry[0] == phase}) == 1, warm
        checks.append(dict(name="24 edits restore original vertices without memory growth", warm=warm))
        for method, z in (("bump", 0), ("both", 0.1), ("true", 0.1)):
            builder.parm("cycles_displacement_method").set(method)
            front(session.wait(session.apply(dict(materials=[graph_material()]))), z=z)
        checks.append(dict(name="Bump true both method transitions restore geometry"))
        before = session.status()
        builder.node("surface_shader").parm("strength").set(2)
        session.wait(session.apply(dict(materials=[graph_material()])))
        assert session.status()["displacement_rebuilds"] == before["displacement_rebuilds"], session.status()
        builder.node("surface_shader").parm("strength").set(1)
        session.wait(session.apply(dict(materials=[graph_material()])))
        checks.append(dict(name="Surface-only edit preserves displaced vertices"))

        # Two explicit host requests share the same prototype and binding.
        left, right = request(object_key="left"), request(object_key="right")
        left["transform"][3] = -0.8
        right["transform"][3] = 0.8
        right["transform"][10] = 2
        displacement.parmTuple("vector").set((0, 0, 0.3))
        frame = session.wait(
            session.apply(dict(geometry=[left, right], materials=[graph_material()], camera_path=camera.path()))
        )
        front(frame, -0.8, 0.3)
        front(frame, 0.8, 0.6)
        assert session.status()["geometry_resources"] == 1 and session.status()["mesh_variant_resources"] == 1
        checks.append(
            dict(name="Object-space displacement shares compatible nonuniform instances", status=session.status())
        )
        displacement.parm("space").set("world")
        frame = session.wait(session.apply(dict(materials=[graph_material()])))
        front(frame, -0.8, 0.3)
        front(frame, 0.8, 0.3)
        assert session.status()["geometry_resources"] == 1 and session.status()["mesh_variant_resources"] == 2
        checks.append(dict(name="World-space displacement creates transform variants", status=session.status()))
        right["transform"][10] = 3
        frame = session.wait(session.apply(dict(geometry=[left, right])))
        front(frame, -0.8, 0.3)
        front(frame, 0.8, 0.3)
        assert session.status()["mesh_variant_resources"] == 2
        session.wait(session.apply(dict(geometry=[left])))
        assert session.status()["mesh_variant_resources"] == 1
        displacement.parm("space").set("object")
        frame = session.wait(session.apply(dict(geometry=[left, right], materials=[graph_material()])))
        front(frame, -0.8, 0.3)
        front(frame, 0.8, 0.9)
        assert session.status()["mesh_variant_resources"] == 1
        checks.append(dict(name="Transform edits and deletion retire unused variants"))

        malformed = graph_material()
        malformed["graph"]["displacement_method"] = "unknown"
        reject(session, dict(materials=[malformed]), "Unknown displacement method")
        malformed = graph_material()
        malformed["graph"]["displacement"] = malformed["graph"]["surface"]
        reject(session, dict(materials=[malformed]), "float3 vector")
        displacement.parm("space").set("tangent")
        frame = session.wait(session.apply(dict(materials=[graph_material()])))
        front(frame, -0.8, 0)
        front(frame, 0.8, 0)
        assert any(d["code"] == "material.missing_uv" for d in session.status()["source_diagnostics"])
        displacement.parm("space").set("object")
        checks.append(dict(name="Malformed displacement stays strict; missing UVs disable only tangent displacement"))

        scalar = builder.createNode("cycles::displacement::1", "normal_offset")
        scalar.parm("height").set(0.7)
        scalar.parm("scale").set(1)
        builder.node("surface_output").setNamedInput("displacement", scalar, "displacement")
        frame = session.wait(session.apply(dict(geometry=[request()], materials=[graph_material()])))
        front(frame, z=0.2)
        checks.append(dict(name="Scalar height midlevel scale use the source normal", position=front(frame, z=0.2)))
        builder.node("surface_output").setNamedInput("displacement", displacement, "displacement")

        displacement.parm("space").set("tangent")
        displacement.parmTuple("vector").set((0.2, 0.3, 0.1))
        frame = session.wait(
            session.apply(
                dict(geometry=[ch.geometry_request(obj, uv_grid, material="surface")], materials=[graph_material()])
            )
        )
        front(frame, 0.2, 0.3)
        alpha = planes(frame, "combined")[:, :, 0] / 0.2
        columns = np.arange(frame["width"]) + 0.5
        rows = np.arange(frame["height"]) + 0.5
        centre = ((alpha.sum(axis=0) * columns).sum() / alpha.sum(), (alpha.sum(axis=1) * rows).sum() / alpha.sum())
        measured = ((centre[0] / frame["width"] - 0.5) * 4, (centre[1] / frame["height"] - 0.5) * 2.25)
        assert np.max(np.abs(np.array(measured) - (0.2, 0.1))) < 0.025, dict(
            centroid=measured,
            host_uv=[vertex.attribValue("uv") for vertex in uv_grid.geometry().prims()[0].vertices()],
            rendered_uv=planes(frame, "uv")[45, 80].tolist(),
            graph=graph_material(),
            extent=planes(frame, "position")[alpha > 0.99].min(axis=0).tolist()
            + planes(frame, "position")[alpha > 0.99].max(axis=0).tolist(),
        )
        checks.append(dict(name="UV tangent normal bitangent axes", centroid=measured, position=front(frame, 0.2, 0.3)))
        displacement.parm("space").set("object")
        displacement.parmTuple("vector").set((0, 0, 0.3))

        frame = session.wait(
            session.apply(
                dict(
                    geometry=[ch.geometry_request(obj, seams, material_mode="native")],
                    material_resolver=native_scene.resolve_materials,
                )
            )
        )
        front(frame, -0.5, 0.1)
        front(frame, 0.5, 0.5)
        assert session.status()["geometry_resources"] == session.status()["mesh_variant_resources"] == 1
        session.write_exr(destination / ("cpu-seams.exr" if device == "CPU" else "optix-seams.exr"))
        checks.append(
            dict(name="Shared source vertices split across displacement material seams", status=session.status())
        )

        motion = dict(enabled=True, fps=24, shutter_frames=0.5, offset=0, samples=3)
        deform = ch.geometry_request(obj, moving, material="surface", motion="deformation", at_time=0)
        frame = session.wait(
            session.apply(dict(geometry=[deform], materials=[graph_material()], motion=motion), at_time=0)
        )
        front(frame)
        checks.append(
            dict(name="Object-vector displacement retains stable deformation samples", status=session.status())
        )
        builder.node("surface_output").setNamedInput("displacement", scalar, "displacement")
        frame = session.wait(session.apply(dict(materials=[graph_material()])))
        front(frame, z=0.2)
        assert not session.status()["source_diagnostics"], session.status()
        builder.node("surface_output").setNamedInput("displacement", displacement, "displacement")
        session.wait(session.apply(dict(materials=[graph_material()])))
        assert not session.status()["source_diagnostics"], session.status()
        checks.append(dict(name="Normal-dependent displacement evaluates across deformation samples"))
        session.wait(session.apply(dict(motion_blur=False)))

        session.wait(session.apply(dict(geometry=[])))
        assert session.status()["mesh_variant_resources"] == session.status()["retained_mesh_source_bytes"] == 0
        assert session.status()["owned_geometry_bytes"] == 0
        frame = session.wait(session.apply(dict(geometry=[request()], materials=[graph_material()])))
        front(frame)
        session.write_exr(destination / ("cpu.exr" if device == "CPU" else "optix.exr"))
        checks.append(dict(name="Source and variants retire then recreate correctly"))
        final = session.status()
    closed = session.status()
    assert closed["owned_geometry_bytes"] == closed["device_allocation_bytes"] == 0
    checks.append(dict(name="Shutdown releases source variants and device allocations"))
    reports.append(dict(device=device, checks=checks, final=final, closed=closed))

grid.setRenderFlag(True)
grid.setDisplayFlag(True)
obj.parm("shop_materialpath").set(builder.path())
rop = hou.node("/out").createNode("cycles_ipr", "native_displacement_cycles")
passes.add(rop, "depth")
set_rop_devices(rop, "CPU")
for name, value in dict(camera=camera.path(), soppath=grid.path(), resx=160, resy=90, samples=16).items():
    rop.parm(name).set(value)
rop.parmTuple("background").set((0, 0, 0))
rop.parmTuple("lightstrength").set((0, 0, 0))
hou.hipFile.save(str(destination / "native-displacement.hiplc"))
import OpenImageIO as OIIO

displacement.parmTuple("vector")[2].setExpression("$F * .1", hou.exprLanguage.Hscript)
rop.parm("picture").set(str(destination / "displacement-frame.$F4.exr"))
rop.render(frame_range=(1, 3, 1))
for number in (1, 2, 3):
    image = OIIO.ImageBuf(str(destination / f"displacement-frame.{number:04}.exr"))
    depth = image.get_pixels()[45, 80, list(image.spec().channelnames).index("depth.Z")]
    assert abs(float(depth) - (5 - number * 0.1)) < 0.001, (number, depth)
(destination / "houdini-displacement-smoke.json").write_text(
    json.dumps(dict(result="passed", devices=reports), indent=2)
)
print(json.dumps(dict(result="passed", checks_per_device=[len(report["checks"]) for report in reports])))
