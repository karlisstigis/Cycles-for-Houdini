# SPDX-License-Identifier: Apache-2.0
"""Open linear hair: analytic tubes, width ownership and per-curve identities."""

from support.paths import artifacts_root
from cycles_houdini.device_selection import set_devices as set_rop_devices
from array import array
import json
import math
import os
from pathlib import Path

import hou
import cycles_houdini as ch
from point_curve_reference import compare_reference

root = artifacts_root() / "stage2/hair"
root.mkdir(parents=True, exist_ok=True)
obj = hou.node("/obj").createNode("geo", "hair_fixture", run_init_scripts=False)
source = obj.createNode("python", "linear_strands")
original = """import hou
g = hou.pwd().geometry()
g.addAttrib(hou.attribType.Prim, 'id', 0)
g.addAttrib(hou.attribType.Point, 'width', 0.2)
g.addAttrib(hou.attribType.Point, 'Cd', (1.0, 1.0, 1.0))
g.addAttrib(hou.attribType.Vertex, 'uv', (0.0, 0.0, 0.0))
for identity, x, width, color, uv in [(10, -0.8, 0.2, (0.8, 0.1, 0.02), (0.2, 0.3, 0.0)), (20, 0.8, 0.3, (0.02, 0.1, 0.8), (0.6, 0.7, 0.0))]:
    curve = g.createPolygon(is_closed=False); curve.setAttribValue('id', identity)
    for y in (-0.6, 0.6):
        p = g.createPoint(); p.setPosition((x, y, 0))
        p.setAttribValue('width', width); p.setAttribValue('Cd', color)
        vertex = curve.addVertex(p); vertex.setAttribValue('uv', uv)
"""
source.parm("python").set(original)
source.setRenderFlag(True)
camera = hou.node("/obj").createNode("cam", "hair_camera")
camera.parm("tz").set(5)
camera.parm("projection").set(1)
camera.parm("orthowidth").set(4)

# HOM/VEX may promote same-name point/vertex attributes. Author four different
# names, then rename only their JSON descriptors in Houdini's own .geo format.
# Loading verifies that the host really retains all four conflicting owners.
width_data = hou.Geometry()
width_curve = width_data.createPolygon(is_closed=False)
for y in (-0.6, 0.6):
    point = width_data.createPoint()
    point.setPosition((-0.8, y, 0))
    width_curve.addVertex(point)
for kind, name, value in (
    (hou.attribType.Global, "width_detail", 0.24),
    (hou.attribType.Prim, "width_primitive", 0.26),
    (hou.attribType.Point, "width_point", 0.28),
    (hou.attribType.Vertex, "width_vertex", 0.30),
):
    width_data.addAttrib(kind, name, value)
width_path = root / "width-ownership.geo"
width_data.saveToFile(str(width_path))
width_text = width_path.read_text(encoding="utf-8")
for name in ("width_detail", "width_primitive", "width_point", "width_vertex"):
    width_text = width_text.replace('"' + name + '"', '"width"')
width_path.write_text(width_text, encoding="utf-8")
width_stash = obj.createNode("stash", "conflicting_width_owners")


def values(frame, name):
    data = array("f")
    data.frombytes(frame["planes"][name]["pixels"])
    return data


def pixel(frame, x, y=0):
    return round((y / 2.25 + 0.5) * frame["height"]) * frame["width"] + round((x / 4 + 0.5) * frame["width"])


def rgb(frame, x):
    index = pixel(frame, x) * 4
    return list(values(frame, "combined")[index : index + 3])


def tube(frame, local_x, radius, color=None, uv=None, scale=(1, 1, 1), translation=(0, 0, 0)):
    centre_x = local_x * scale[0] + translation[0]
    index = pixel(frame, centre_x, translation[1])
    position = values(frame, "position")[index * 3 : index * 3 + 3]
    local_dx = (position[0] - centre_x) / scale[0]
    expected_z = translation[2] + abs(scale[2]) * math.sqrt(radius**2 - local_dx**2)
    assert abs(position[2] - expected_z) < 0.002, (position, expected_z, scale)
    assert abs(values(frame, "depth")[index] - (5 - position[2])) < 0.002
    normal = values(frame, "normal")[index * 3 : index * 3 + 3]
    gradient = (
        (position[0] - centre_x) / (scale[0] * radius) ** 2,
        0,
        (position[2] - translation[2]) / (scale[2] * radius) ** 2,
    )
    length = math.sqrt(sum(value**2 for value in gradient))
    assert max(abs(normal[i] - gradient[i] / length) for i in range(3)) < 0.012, (normal, gradient)
    if color:
        rendered = values(frame, "combined")[index * 4 : index * 4 + 3]
        assert max(abs(a - b) for a, b in zip(rendered, color)) < 0.002, (rendered, color)
    if uv:
        rendered = values(frame, "uv")[index * 3 : index * 3 + 2]
        assert max(abs(a - b) for a, b in zip(rendered, uv)) < 0.002, (rendered, uv)
    # Independently check that the horizontal silhouette has the specified radius.
    assert max(rgb(frame, centre_x + abs(scale[0]) * radius + 0.04)) == 0
    return list(position)


reports = []
for device in ("CPU", ch.default_device()):
    with ch.Session(device=device, width=320, height=180, samples=1) as session:
        material = dict(key="hair", kind="emission", color=(1, 1, 1), color_attribute="Cd")
        request = ch.geometry_request(obj, source, material="hair")
        frame = session.wait(
            session.apply(
                dict(geometry=[request], camera_path=camera.path(), background=(0, 0, 0), materials=[material])
            )
        )
        initial = session.status()
        assert initial["curves"] == 2 and initial["points"] == initial["index_primitive_ids"] == 0, initial
        assert initial["geometry_resources"] == initial["object_resources"] == 1
        fronts = [
            tube(frame, -0.8, 0.1, (0.8, 0.1, 0.02), (0.2, 0.3)),
            tube(frame, 0.8, 0.15, (0.02, 0.1, 0.8), (0.6, 0.7)),
        ]
        session.write_exr(root / ("optix.exr" if device != "CPU" else "cpu.exr"))
        reference = compare_reference(frame, device, "hair", root)
        camera.parm("tx").set(0.1)
        session.wait(session.apply(dict(geometry=[request], camera_path=camera.path())))
        assert session.status()["geometry_conversions"] == initial["geometry_conversions"]
        assert session.status()["object_updates"] == initial["object_updates"]
        camera.parm("tx").set(0)
        for scale in ((2, 2, 2), (1.5, 0.75, 2), (-1.5, 0.75, 2)):
            obj.parmTuple("s").set(scale)
            obj.parmTuple("t").set((0.1, 0.1, 0.2))
            request = ch.geometry_request(obj, source, material="hair")
            frame = session.wait(session.apply(dict(geometry=[request], camera_path=camera.path())))
            for x, radius in ((-0.8, 0.1), (0.8, 0.15)):
                tube(frame, x, radius, scale=scale, translation=(0.1, 0.1, 0.2))
            assert session.status()["geometry_conversions"] == initial["geometry_conversions"]
        obj.parmTuple("s").set((1, 1, 1))
        obj.parmTuple("t").set((0, 0, 0))
        request = ch.geometry_request(obj, source, material="hair")
        frame = session.wait(
            session.apply(dict(geometry=[request], materials=[dict(material, color_attribute="curve_random")]))
        )
        random_before = [rgb(frame, x) for x in (-0.8, 0.8)]
        assert random_before[0] != random_before[1] and all(0 < value < 1 for color in random_before for value in color)
        source.parm("python").set(
            original.replace(
                "(10, -0.8, 0.2, (0.8, 0.1, 0.02), (0.2, 0.3, 0.0)), (20, 0.8, 0.3, (0.02, 0.1, 0.8), (0.6, 0.7, 0.0))",
                "(20, 0.8, 0.3, (0.02, 0.1, 0.8), (0.6, 0.7, 0.0)), (10, -0.8, 0.2, (0.8, 0.1, 0.02), (0.2, 0.3, 0.0))",
            )
        )
        frame = session.wait(session.apply(dict(geometry=[request])))
        assert all(
            max(abs(a - b) for a, b in zip(rgb(frame, x), old)) < 0.002 for x, old in zip((-0.8, 0.8), random_before)
        )
        frame = session.wait(session.apply(dict(materials=[dict(material, kind="principled_volume")])))
        assert any(d["code"] == "material.geometry_binding" for d in session.status()["source_diagnostics"])
        fallback_colors = [rgb(frame, x) for x in (-0.8, 0.8)]
        assert all(max(abs(a - b) for a, b in zip(color, (1, 0, 1))) < 0.002 for color in fallback_colors), (
            fallback_colors
        )
        converted = session.status()["geometry_conversions"]
        frame = session.wait(session.apply(dict(materials=[material])))
        assert not session.status()["source_diagnostics"]
        tube(frame, -0.8, 0.1, (0.8, 0.1, 0.02))
        tube(frame, 0.8, 0.15, (0.02, 0.1, 0.8))
        assert session.status()["geometry_conversions"] == converted
        source.parm("python").set(original.replace("(20, 0.8,", "(10, 0.8,"))
        frame = session.wait(session.apply(dict(geometry=[request], materials=[material])))
        assert not session.status()["source_diagnostics"]
        assert session.status()["curves"] == 2
        # Width ownership precedence: vertex, point, primitive, detail, fallback.
        widths = []
        width_data = hou.Geometry()
        width_data.loadFromFile(str(width_path))
        getters = (
            width_data.findVertexAttrib,
            width_data.findPointAttrib,
            width_data.findPrimAttrib,
            width_data.findGlobalAttrib,
        )
        assert all(getter("width") is not None for getter in getters)
        for index, (ownership, width) in enumerate(
            (("Vertex", 0.30), ("Point", 0.28), ("Prim", 0.26), ("Global", 0.24), (None, 0.22))
        ):
            assert sum(getter("width") is not None for getter in getters) == 4 - index
            width_stash.parm("stash").set(width_data)
            request = ch.geometry_request(obj, width_stash, material="hair", curve_width=0.22)
            frame = session.wait(
                session.apply(dict(geometry=[request], materials=[dict(material, color_attribute="")]))
            )
            tube(frame, -0.8, width / 2, (1, 1, 1))
            widths.append(dict(owner=ownership or "fallback", width=width))
            if index < 4:
                getters[index]("width").destroy()
        pack = obj.createNode("pack", "packed_hair_prototype")
        pack.setInput(0, width_stash)
        first = ch.geometry_request(obj, pack, material="hair", curve_width=0.2)
        second = dict(first, object_key="second-hair-group", curve_width=0.4)
        second["transform"] = list(first["transform"])
        second["transform"][3] = 1.6
        frame = session.wait(session.apply(dict(geometry=[first, second])))
        assert session.status()["geometry_resources"] == session.status()["object_resources"] == 2
        tube(frame, -0.8, 0.1)
        tube(frame, -0.8, 0.2, translation=(1.6, 0, 0))
        pack.destroy()
        # A linear tapered strand and per-key color interpolate along the segment.
        tapered = """import hou
g = hou.pwd().geometry()
g.addAttrib(hou.attribType.Prim, 'name', 'taper')
g.addAttrib(hou.attribType.Vertex, 'width', 0.0)
g.addAttrib(hou.attribType.Vertex, 'Cd', (0.0, 0.0, 0.0))
curve = g.createPolygon(is_closed=False)
for y, width, color in [(-0.6, 0.3, (0.8, 0.1, 0.02)), (0.6, 0.1, (0.02, 0.1, 0.8))]:
    p = g.createPoint(); p.setPosition((-0.8, y, 0)); vertex = curve.addVertex(p)
    vertex.setAttribValue('width', width); vertex.setAttribValue('Cd', color)
"""
        source.parm("python").set(tapered)
        request = ch.geometry_request(obj, source, material="hair")
        frame = session.wait(session.apply(dict(geometry=[request], materials=[material])))
        index = pixel(frame, -0.8)
        position = values(frame, "position")[index * 3 : index * 3 + 3]
        # Cycles round linear curves are the envelope of varying-radius spheres.
        # The contributing centre is displaced along a tapered segment by
        # radius*slope; its attribute parameter differs from projected world Y.
        slope = (0.05 - 0.15) / 1.2
        t = ((position[1] + 0.6) + slope * 0.15) / (1.2 * (1 - slope**2))
        radius_at_y = 0.15 + slope * (position[1] + 0.6)
        cross_section_radius = radius_at_y / math.sqrt(1 - slope**2)
        assert abs(position[2] - math.sqrt(cross_section_radius**2 - (position[0] + 0.8) ** 2)) < 0.002, position
        expected = [a * (1 - t) + b * t for a, b in zip((0.8, 0.1, 0.02), (0.02, 0.1, 0.8))]
        assert max(abs(a - b) for a, b in zip(rgb(frame, -0.8), expected)) < 0.003, (rgb(frame, -0.8), expected)
        source.parm("python").set(original)
        frame = session.wait(session.apply(dict(geometry=[request])))
        peaks = []
        for iteration in range(8):
            source.parm("python").set(
                original.replace("(10, -0.8, 0.2,", "(10, -0.8, " + str(0.2 + 0.01 * (iteration % 2)) + ",")
            )
            session.wait(session.apply(dict(geometry=[request])))
            peaks.append(session.status()["device_allocation_bytes"])
        assert max(peaks) - min(peaks) < 4096, peaks
        session.wait(session.apply(dict(geometry=[])))
        assert session.status()["geometry_resources"] == session.status()["object_resources"] == 0
        source.parm("python").set(original)
        session.wait(session.apply(dict(geometry=[request])))
        assert session.status()["curves"] == 2
        reports.append(
            dict(
                device=device,
                initial=initial,
                tube_fronts=fronts,
                reference=reference,
                identity_colors=random_before,
                transforms="uniform, nonuniform and mirrored passed",
                width_precedence=widths,
                taper_and_vertex_color="passed",
                packed_width_variants="passed",
                tracked_allocation_samples=peaks,
                final=session.status(),
            )
        )
    assert session.status()["device_allocation_bytes"] == session.status()["owned_geometry_bytes"] == 0

rop = hou.node("/out").createNode("cycles_ipr", "cycles_hair")
set_rop_devices(rop, ch.default_device())
for name, value in dict(
    soppath=source.path(),
    camera=camera.path(),
    resx=320,
    resy=180,
    samples=64,
    material_kind="emission",
    color_attribute="Cd",
).items():
    rop.parm(name).set(value)
rop.parmTuple("basecolor").set((1, 1, 1))
rop.parmTuple("background").set((0, 0, 0))
hou.hipFile.save(str(root / "native-linear-hair.hiplc"))
(root / "houdini-hair-smoke.json").write_text(
    json.dumps(
        dict(
            result="passed",
            fixtures=reports,
            scope="Native open linear round curves; width, attributes and static identity; other bases and motion remain separate",
        ),
        indent=2,
    ),
    encoding="utf-8",
)
print(root / "houdini-hair-smoke.json")
