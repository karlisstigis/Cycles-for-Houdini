# SPDX-License-Identifier: Apache-2.0
"""Packed sharing, nested transforms and retirement with independent box expectations."""

from support.paths import artifacts_root
from array import array
import json
import os
from pathlib import Path

import hou
import cycles_houdini as ch

root = artifacts_root() / "stage2/packed"
root.mkdir(parents=True, exist_ok=True)
geometry = hou.node("/obj").createNode("geo", "packed_fixture", run_init_scripts=False)
box = geometry.createNode("box")
box.parmTuple("size").set((0.5, 0.7, 0.4))
points = geometry.createNode("python", "instance_points")
points.parm("python").set("""import hou
g = hou.pwd().geometry()
g.addAttrib(hou.attribType.Point, 'id', 0)
for identity, x in [(10, -0.8), (20, 0.8)]:
    point = g.createPoint(); point.setPosition((x, 0, 0)); point.setAttribValue('id', identity)
""")
copy_node = geometry.createNode("copytopoints")
copy_node.setInput(0, box)
copy_node.setInput(1, points)
copy_node.parm("pack").set(1)
copies = geometry.createNode("python", "identified_instances")
copies.setInput(0, copy_node)
copies.parm("python").set("""import hou
g = hou.pwd().geometry()
g.addAttrib(hou.attribType.Prim, 'id', 0)
source = hou.node('../instance_points').geometry()
for primitive, point in zip(g.prims(), source.points()):
    primitive.setAttribValue('id', point.attribValue('id'))
""")
copies.setRenderFlag(True)
assert len(copies.geometry().prims()) == 2
assert all(prim.type() == hou.primType.PackedGeometry for prim in copies.geometry().prims())
camera = hou.node("/obj").createNode("cam", "packed_camera")
camera.parm("tz").set(5)
camera.parm("projection").set(1)
camera.parm("orthowidth").set(4)


def values(frame, name):
    data = array("f")
    data.frombytes(frame["planes"][name]["pixels"])
    return data


def front(frame, expected_x, expected_y=0, expected_z=0.2, expected_color=None):
    # Orthographic horizontal width 4: independently project the specified box
    # centre. At the front face, world Z=.2 and camera depth=4.8.
    x = round((expected_x / 4 + 0.5) * frame["width"])
    y = round((expected_y / 2.25 + 0.5) * frame["height"])
    index = y * frame["width"] + x
    position = values(frame, "position")[index * 3 : index * 3 + 3]
    normal = values(frame, "normal")[index * 3 : index * 3 + 3]
    depth = values(frame, "depth")[index]
    assert abs(position[0] - expected_x) < 0.025 and abs(position[1] - expected_y) < 0.025, position
    assert abs(position[2] - expected_z) < 0.001 and abs(depth - (5 - expected_z)) < 0.001, (position, depth)
    assert abs(normal[2] - 1) < 0.001 and abs(normal[0]) < 0.001 and abs(normal[1]) < 0.001, normal
    if expected_color is not None:
        color = values(frame, "combined")[index * 4 : index * 4 + 3]
        assert all(abs(actual - expected) < 1e-5 for actual, expected in zip(color, expected_color)), color
    return list(position)


reports = []
for device in ("CPU", ch.default_device()):
    with ch.Session(device=device, width=320, height=180, samples=16) as session:
        request = ch.geometry_request(geometry, copies, material="red")
        scene = dict(
            geometry=[request],
            camera_path=camera.path(),
            background=(0, 0, 0),
            materials=[dict(key="red", kind="emission", color=(0.8, 0.02, 0.01))],
        )
        frame = session.wait(session.apply(scene))
        initial = session.status()
        assert initial["geometry_resources"] == 1 and initial["object_resources"] == 2, initial
        assert initial["index_instance_ids"] == 0, initial
        identities = session.native_object_keys()
        # Native object keys include the leaf geometry domain after the packed
        # identity. Check the authored instance ID, independent of that suffix.
        assert len(identities) == 2 and {key.rsplit("/packed:id:", 1)[1].split("/", 1)[0] for key in identities} == {
            "10",
            "20",
        }, identities
        positions = [front(frame, x) for x in (-0.8, 0.8)]
        session.write_exr(root / ("optix.exr" if device != "CPU" else "cpu.exr"))
        camera.parm("tx").set(0.1)
        session.wait(session.apply(dict(geometry=[request], camera_path=camera.path())))
        assert session.status()["geometry_conversions"] == initial["geometry_conversions"]
        assert session.status()["object_updates"] == initial["object_updates"], (
            "Camera edit resynchronized unchanged instances"
        )
        camera.parm("tx").set(0)
        geometry.parm("tx").set(0.25)
        moved = ch.geometry_request(geometry, copies, material="red")
        frame = session.wait(session.apply(dict(geometry=[moved], camera_path=camera.path())))
        for x in (-0.55, 1.05):
            front(frame, x)
        assert session.status()["geometry_conversions"] == initial["geometry_conversions"]

        # A second group adds owners of the same physical mesh. A different
        # material adds a logical binding while source storage remains shared.
        second = dict(moved, object_key="independent-second-group")
        second["transform"] = list(moved["transform"])
        second["transform"][7] = -0.8
        session.wait(session.apply(dict(geometry=[moved, second])))
        shared = session.status()
        assert shared["geometry_resources"] == 1 and shared["object_resources"] == 4
        blue = dict(second, material="blue")
        bound_frame = session.wait(
            session.apply(
                dict(geometry=[moved, blue], materials=[dict(key="blue", kind="emission", color=(0.01, 0.02, 0.8))])
            )
        )
        bound = session.status()
        assert bound["geometry_resources"] == 2 and bound["object_resources"] == 4
        assert bound["mesh_family_resources"] == 1 and bound["mesh_family_members"] == 2, bound
        assert bound["mesh_variant_resources"] == shared["mesh_variant_resources"] == 1, (shared, bound)
        assert bound["mesh_variant_bytes"] == shared["mesh_variant_bytes"], (shared, bound)
        assert bound["owned_geometry_bytes"] == bound["mesh_variant_bytes"] + bound["retained_mesh_source_bytes"], bound
        for x in (-0.55, 1.05):
            front(bound_frame, x, expected_color=(0.8, 0.02, 0.01))
            front(bound_frame, x, -0.8, expected_color=(0.01, 0.02, 0.8))
        variant_memory = bound["owned_geometry_bytes"]
        session.wait(session.apply(dict(geometry=[moved], deleted_materials=["blue"])))
        retired = session.status()
        assert retired["geometry_resources"] == 1 and retired["object_resources"] == 2
        assert retired["mesh_family_members"] == 1
        assert retired["owned_geometry_bytes"] == shared["owned_geometry_bytes"], (shared, retired)
        assert (
            retired["owned_geometry_bytes"] == retired["mesh_variant_bytes"] + retired["retained_mesh_source_bytes"]
        ), retired

        # Reordering primitives must retain the same object identities and
        # transforms. Duplicate IDs keep every instance using a local index.
        original_points = points.parm("python").eval()
        points.parm("python").set(original_points.replace("[(10, -0.8), (20, 0.8)]", "[(20, 0.8), (10, -0.8)]"))
        count = session.status()["object_updates"]
        session.wait(session.apply(dict(geometry=[moved])))
        assert session.native_object_keys() == identities
        assert session.status()["object_updates"] == count, "Primitive reorder changed persistent instance transforms"
        generation = session.status()["generation"]
        points.parm("python").set(original_points.replace("[(10, -0.8), (20, 0.8)]", "[(10, -0.8), (10, 0.8)]"))
        session.wait(session.apply(dict(geometry=[moved])))
        assert session.status()["generation"] > generation
        assert session.status()["object_resources"] == 2 and session.status()["geometry_resources"] == 1
        assert not session.status()["source_diagnostics"]
        points.parm("python").set(original_points)
        session.wait(session.apply(dict(geometry=[moved])))
        assert session.native_object_keys() == identities and not session.status()["source_diagnostics"]

        # Nest the pair inside another packed primitive with an explicit local
        # translation. World = OBJ translation + outer packed + inner packed.
        pack = geometry.createNode("pack")
        pack.setInput(0, copies)
        outer = geometry.createNode("xform")
        outer.setInput(0, pack)
        outer.parmTuple("t").set((0, 0.25, 0.3))
        nested = ch.geometry_request(geometry, outer, material="red")
        frame = session.wait(session.apply(dict(geometry=[nested])))
        for x in (-0.55, 1.05):
            front(frame, x, 0.25, 0.5)
        assert session.status()["geometry_resources"] == 1 and session.status()["object_resources"] == 2
        session.wait(session.apply(dict(geometry=[])))
        assert session.status()["geometry_resources"] == session.status()["object_resources"] == 0
        session.wait(session.apply(dict(geometry=[nested])))
        assert session.status()["geometry_resources"] == 1 and session.status()["object_resources"] == 2
        reports.append(
            dict(
                device=device,
                initial=initial,
                expected_front_positions=positions,
                variant_bytes=variant_memory,
                persistent_ids="reorder passed; duplicate rejected",
                nested="passed",
                retirement="passed",
                final=session.status(),
            )
        )
        geometry.parm("tx").set(0)
        pack.destroy()
        outer.destroy()
    assert session.status()["device_allocation_bytes"] == 0

report = dict(
    result="passed",
    fixtures=reports,
    scope="Packed polygon prototypes, uniform materials, nested transforms and ownership; motion/attributes remain separate work",
)
(root / "houdini-packed-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(root / "houdini-packed-smoke.json")
