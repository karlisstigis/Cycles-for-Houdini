# SPDX-License-Identifier: Apache-2.0
"""Native particle spheres: independently specified size, transforms and attributes."""

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

root = artifacts_root() / "stage2/particles"
root.mkdir(parents=True, exist_ok=True)
obj = hou.node("/obj").createNode("geo", "particle_fixture", run_init_scripts=False)
source = obj.createNode("python", "particle_points")
original = """import hou
g = hou.pwd().geometry()
g.addAttrib(hou.attribType.Point, 'id', 0)
g.addAttrib(hou.attribType.Point, 'pscale', 0.2)
g.addAttrib(hou.attribType.Point, 'Cd', (1.0, 1.0, 1.0))
g.addAttrib(hou.attribType.Point, 'uv', (0.0, 0.0))
for identity, x, radius, color, uv in [(10, -0.8, 0.2, (0.8, 0.1, 0.02), (0.2, 0.3)), (20, 0.8, 0.3, (0.02, 0.1, 0.8), (0.6, 0.7))]:
    p = g.createPoint(); p.setPosition((x, 0, 0))
    for name, value in [('id', identity), ('pscale', radius), ('Cd', color), ('uv', uv)]: p.setAttribValue(name, value)
"""
source.parm("python").set(original)
particles = obj.createNode("add", "particle_system")
particles.setInput(0, source)
particles.parm("addparticlesystem").set(1)
particles.setRenderFlag(True)
assert len(particles.geometry().prims()) == 1
assert particles.geometry().prims()[0].type() == hou.primType.ParticleSystem, particles.geometry().prims()
camera = hou.node("/obj").createNode("cam", "particle_camera")
camera.parm("tz").set(5)
camera.parm("projection").set(1)
camera.parm("orthowidth").set(4)


def values(frame, name):
    data = array("f")
    data.frombytes(frame["planes"][name]["pixels"])
    return data


def pixel(frame, x, y=0):
    return round((y / 2.25 + 0.5) * frame["height"]) * frame["width"] + round((x / 4 + 0.5) * frame["width"])


def rgb(frame, x):
    index = pixel(frame, x) * 4
    return list(values(frame, "combined")[index : index + 3])


def sphere(frame, local_x, radius, color=None, uv=None, scale=(1, 1, 1), translation=(0, 0, 0), offset=(0, 0)):
    # Orthographic rays intersect the independently specified transformed sphere.
    centre = (local_x * scale[0] + translation[0], translation[1], translation[2])
    index = pixel(frame, centre[0] + offset[0], centre[1] + offset[1])
    position = values(frame, "position")[index * 3 : index * 3 + 3]
    local = [(position[i] - centre[i]) / scale[i] for i in range(3)]
    expected_z = centre[2] + abs(scale[2]) * math.sqrt(radius**2 - local[0] ** 2 - local[1] ** 2)
    assert abs(position[2] - expected_z) < 0.002, (position, expected_z, scale)
    assert abs(values(frame, "depth")[index] - (5 - position[2])) < 0.002
    normal = values(frame, "normal")[index * 3 : index * 3 + 3]
    gradient = [(position[i] - centre[i]) / (scale[i] * radius) ** 2 for i in range(3)]
    length = math.sqrt(sum(value**2 for value in gradient))
    assert max(abs(normal[i] - gradient[i] / length) for i in range(3)) < 0.012, (normal, gradient)
    if color:
        rendered = values(frame, "combined")[index * 4 : index * 4 + 3]
        assert max(abs(a - b) for a, b in zip(rendered, color)) < 0.002, (rendered, color)
    if uv:
        rendered = values(frame, "uv")[index * 3 : index * 3 + 2]
        assert max(abs(a - b) for a, b in zip(rendered, uv)) < 0.002, (rendered, uv)
    return list(position)


reports = []
for device in ("CPU", ch.default_device()):
    # Data position/depth are first-sample passes; one sample also makes normal
    # refer to that same ray instead of a differently filtered sample average.
    with ch.Session(device=device, width=320, height=180, samples=1) as session:
        material = dict(key="points", kind="emission", color=(1, 1, 1), color_attribute="Cd")
        request = ch.geometry_request(obj, particles, material="points")
        frame = session.wait(
            session.apply(
                dict(geometry=[request], camera_path=camera.path(), background=(0, 0, 0), materials=[material])
            )
        )
        initial = session.status()
        first_render = initial
        assert initial["points"] == 2 and initial["curves"] == initial["index_primitive_ids"] == 0, initial
        assert initial["geometry_resources"] == initial["object_resources"] == 1, initial
        fronts = [
            sphere(frame, -0.8, 0.2, (0.8, 0.1, 0.02), (0.2, 0.3)),
            sphere(frame, 0.8, 0.3, (0.02, 0.1, 0.8), (0.6, 0.7)),
        ]
        session.write_exr(root / ("optix.exr" if device != "CPU" else "cpu.exr"))
        reference = compare_reference(frame, device, "points", root)
        source.parm("python").set(
            original + "\ngroup = g.createPointGroup('render_particles'); group.add(g.points()[0])\n"
        )
        particles.parm("particlegroup").set("render_particles")
        frame = session.wait(session.apply(dict(geometry=[request])))
        assert session.status()["points"] == 1 and max(rgb(frame, 0.8)) == 0
        sphere(frame, -0.8, 0.2, (0.8, 0.1, 0.02), (0.2, 0.3))
        pack = obj.createNode("pack", "packed_point_prototype")
        pack.setInput(0, source)
        first = ch.geometry_request(obj, pack, material="points", point_radius=0.2)
        second = dict(first, object_key="second-point-group", point_radius=0.4)
        second["transform"] = list(first["transform"])
        second["transform"][7] = -0.8
        session.wait(session.apply(dict(geometry=[first, second])))
        assert session.status()["geometry_resources"] == 1, (
            "Explicit pscale should allow sharing across different fallback settings"
        )
        without_scale = original.replace("g.addAttrib(hou.attribType.Point, 'pscale', 0.2)\n", "").replace(
            "('pscale', radius), ", ""
        )
        source.parm("python").set(without_scale)
        frame = session.wait(session.apply(dict(geometry=[first, second])))
        assert session.status()["geometry_resources"] == session.status()["object_resources"] == 2
        sphere(frame, -0.8, 0.2)
        sphere(frame, -0.8, 0.4, translation=(0, -0.8, 0))
        source.parm("python").set(original)
        pack.destroy()
        particles.parm("particlegroup").set("")
        source.parm("python").set(original)
        session.wait(session.apply(dict(geometry=[request])))
        initial = session.status()
        camera.parm("tx").set(0.1)
        session.wait(session.apply(dict(geometry=[request], camera_path=camera.path())))
        assert session.status()["geometry_conversions"] == initial["geometry_conversions"]
        assert session.status()["object_updates"] == initial["object_updates"]
        camera.parm("tx").set(0)
        for scale in ((2, 2, 2), (1.5, 0.75, 2), (-1.5, 0.75, 2)):
            obj.parmTuple("s").set(scale)
            obj.parmTuple("t").set((0.1, 0.1, 0.2))
            request = ch.geometry_request(obj, particles, material="points")
            frame = session.wait(session.apply(dict(geometry=[request], camera_path=camera.path())))
            sphere(frame, -0.8, 0.2, scale=scale, translation=(0.1, 0.1, 0.2))
            sphere(frame, 0.8, 0.3, scale=scale, translation=(0.1, 0.1, 0.2))
            sphere(
                frame,
                -0.8,
                0.2,
                scale=scale,
                translation=(0.1, 0.1, 0.2),
                offset=(0.08 * abs(scale[0]), 0.05 * abs(scale[1])),
            )
            assert session.status()["geometry_conversions"] == initial["geometry_conversions"]
        obj.parmTuple("s").set((1, 1, 1))
        obj.parmTuple("t").set((0, 0, 0))
        request = ch.geometry_request(obj, particles, material="points")
        frame = session.wait(
            session.apply(dict(geometry=[request], materials=[dict(material, color_attribute="point_random")]))
        )
        random_before = [rgb(frame, x) for x in (-0.8, 0.8)]
        assert random_before[0] != random_before[1] and all(0 < value < 1 for color in random_before for value in color)
        reordered = original.replace(
            "(10, -0.8, 0.2, (0.8, 0.1, 0.02), (0.2, 0.3)), (20, 0.8, 0.3, (0.02, 0.1, 0.8), (0.6, 0.7))",
            "(20, 0.8, 0.3, (0.02, 0.1, 0.8), (0.6, 0.7)), (10, -0.8, 0.2, (0.8, 0.1, 0.02), (0.2, 0.3))",
        )
        source.parm("python").set(reordered)
        frame = session.wait(session.apply(dict(geometry=[request])))
        assert all(
            max(abs(a - b) for a, b in zip(rgb(frame, x), old)) < 0.002 for x, old in zip((-0.8, 0.8), random_before)
        )
        frame = session.wait(session.apply(dict(materials=[dict(material, kind="principled_volume")])))
        assert any(d["code"] == "material.geometry_binding" for d in session.status()["source_diagnostics"])
        assert all(max(abs(a - b) for a, b in zip(rgb(frame, x), (1, 0, 1))) < 0.002 for x in (-0.8, 0.8))
        source.parm("python").set(original.replace("(20, 0.8,", "(10, 0.8,"))
        frame = session.wait(session.apply(dict(geometry=[request], materials=[material])))
        assert not session.status()["source_diagnostics"]
        sphere(frame, -0.8, 0.2, (0.8, 0.1, 0.02), (0.2, 0.3))
        sphere(frame, 0.8, 0.3, (0.02, 0.1, 0.8), (0.6, 0.7))
        source.parm("python").set(
            original.replace("(10, -0.8, 0.2, (0.8, 0.1, 0.02)", "(10, -0.8, 0.25, (0.2, 0.7, 0.1)")
        )
        frame = session.wait(session.apply(dict(geometry=[request], materials=[material])))
        sphere(frame, -0.8, 0.25, (0.2, 0.7, 0.1), (0.2, 0.3))
        source.parm("python").set(original)
        # Unconnected points follow the same radius and UV conventions.
        request = ch.geometry_request(obj, source, material="points")
        frame = session.wait(session.apply(dict(geometry=[request])))
        sphere(frame, -0.8, 0.2, (0.8, 0.1, 0.02), (0.2, 0.3))
        warm = session.status()["device_allocation_bytes"]
        peaks = []
        for iteration in range(8):
            source.parm("python").set(
                original.replace("(10, -0.8, 0.2,", "(10, -0.8, " + str(0.2 + 0.01 * (iteration % 2)) + ",")
            )
            session.wait(session.apply(dict(geometry=[request])))
            peaks.append(session.status()["device_allocation_bytes"])
        assert max(peaks) - min(peaks) < 4096, peaks
        source.parm("python").set("import hou\ng = hou.pwd().geometry()")
        frame = session.wait(session.apply(dict(geometry=[request])))
        assert session.status()["points"] == 0 and max(values(frame, "combined")[::4]) == 0
        session.wait(session.apply(dict(geometry=[])))
        assert session.status()["geometry_resources"] == session.status()["object_resources"] == 0
        source.parm("python").set(original)
        session.wait(session.apply(dict(geometry=[request])))
        assert session.status()["points"] == 2
        reports.append(
            dict(
                device=device,
                initial=first_render,
                pre_camera_edit=initial,
                sphere_fronts=fronts,
                reference=reference,
                identity_colors=random_before,
                transforms="uniform, nonuniform and mirrored passed",
                packed_radius_variants="passed",
                tracked_allocation_samples=peaks,
                final=session.status(),
            )
        )
    assert session.status()["device_allocation_bytes"] == session.status()["owned_geometry_bytes"] == 0

rop = hou.node("/out").createNode("cycles_ipr", "cycles_particles")
set_rop_devices(rop, ch.default_device())
for name, value in dict(
    soppath=particles.path(),
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
hou.hipFile.save(str(root / "native-particles.hiplc"))
(root / "houdini-points-smoke.json").write_text(
    json.dumps(
        dict(
            result="passed",
            fixtures=reports,
            scope="Native point spheres and particle primitives; static identity and transforms; motion remains separate",
        ),
        indent=2,
    ),
    encoding="utf-8",
)
print(root / "houdini-points-smoke.json")
