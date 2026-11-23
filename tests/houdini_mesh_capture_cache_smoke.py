# SPDX-License-Identifier: Apache-2.0
"""Cached native polygon capture versus Houdini triangulation and analytic AOVs."""

from support.paths import artifacts_root
import json
import os
from pathlib import Path

import hou
import numpy as np
import cycles_houdini as ch

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage1/mesh-capture"
destination.mkdir(parents=True, exist_ok=True)
obj = hou.node("/obj").createNode("geo", "mesh_capture_receiver", run_init_scripts=False)
stash = obj.createNode("stash", "small_independent_polygon_fixture")
positions = [
    (-1.9, -0.65, 0),
    (-1.05, -0.65, 0),
    (-1.9, 0.15, 0),
    (-0.8, -0.65, 0),
    (-0.1, -0.65, 0),
    (-0.1, 0.15, 0),
    (-0.8, 0.15, 0),
    (0.25, -0.65, 0),
    (1.35, -0.65, 0),
    (1.35, 0.15, 0),
    (0.8, -0.15, 0),
    (0.25, 0.15, 0),
]
# Houdini's native outward winding faces the camera at positive Z.
faces = [(0, 2, 1), (3, 6, 5, 4), (7, 11, 10, 9, 8)]


def geometry(connectivity=faces, shift=0):
    data = hou.Geometry()
    points = []
    for x, y, z in positions:
        point = data.createPoint()
        point.setPosition((x, y, z + shift))
        points.append(point)
    for indices in connectivity:
        polygon = data.createPolygon()
        for index in indices:
            polygon.addVertex(points[index])
    return data


stash.parm("stash").set(geometry())
assert all(p.normal()[2] > 0.999 for p in stash.geometry().prims())
edit = obj.createNode("attribwrangle", "native_position_edits")
edit.setInput(0, stash)
attributes = obj.createNode("attribwrangle", "native_vertex_attributes")
attributes.setInput(0, edit)
attributes.parm("class").set(3)
divide = obj.createNode("divide", "independent_houdini_triangulation")
divide.setInput(0, attributes)
divide.setParms(dict(convex=1, usemaxsides=1, numsides=3, planar=0))
soup = obj.createNode("polysoup", "polygon_soup")
soup.setInput(0, attributes)
camera = hou.node("/obj").createNode("cam", "mesh_capture_camera")
camera.setParms(dict(tz=4, projection=1, orthowidth=4.4))


def set_attributes(color=(0.2, 0.4, 0.7), u_offset=0, tilted=False):
    normal = "set(.2,0,1)" if tilted else "set(0,0,1)"
    attributes.parm("snippet").set(
        f"v@uv=set((@P.x+2)/4+{u_offset},(@P.y+1)/2,0); v@Cd=set({color[0]},{color[1]},{color[2]}); v@N={normal};"
    )


def plane(frame, name):
    value = frame["planes"][name]
    return np.frombuffer(value["pixels"], np.float32).reshape(frame["height"], frame["width"], value["channels"])


material = dict(key="surface", kind="emission", color=(1, 1, 1), color_attribute="Cd", strength=1)
reports = []
for device in ("CPU", ch.default_device()):
    stash.parm("stash").set(geometry())
    edit.parm("snippet").set("")
    set_attributes()
    checks = []
    with (
        ch.Session(device=device, width=176, height=96, samples=64) as session,
        ch.Session(device=device, width=176, height=96, samples=64) as reference,
    ):

        def compare(label, source=attributes, color=(0.2, 0.4, 0.7), u_offset=0, tilted=False):
            frame = session.wait(
                session.apply(
                    dict(
                        geometry=[ch.geometry_request(obj, source, material="surface")],
                        materials=[material],
                        camera_path=camera.path(),
                        transparent_background=True,
                        background=(0, 0, 0),
                    )
                )
            )
            expected = reference.wait(
                reference.apply(
                    dict(
                        geometry=[ch.geometry_request(obj, divide, material="surface")],
                        materials=[material],
                        camera_path=camera.path(),
                        transparent_background=True,
                        background=(0, 0, 0),
                    )
                )
            )
            rgba, target = plane(frame, "combined"), plane(expected, "combined")
            error = float(np.mean(np.abs(rgba - target)))
            assert error < 0.0001, (label, error)
            mask = (rgba[:, :, 3] > 0.99999) & (target[:, :, 3] > 0.99999)
            assert mask.sum() > 500, (label, mask.sum())
            position = plane(frame, "position")[mask]
            assert np.max(np.abs(position - plane(expected, "position")[mask])) < 0.0001, label
            expected_uv = np.column_stack(((position[:, 0] + 2) / 4 + u_offset, (position[:, 1] + 1) / 2))
            uv_error = np.abs(plane(frame, "uv")[mask, :2] - expected_uv)
            worst = int(np.argmax(np.max(uv_error, axis=1)))
            # Allow finite ray/barycentric precision in the analytic comparison;
            # the independently triangulated reference must agree more closely.
            assert np.max(np.abs(plane(frame, "uv")[mask] - plane(expected, "uv")[mask])) < 0.0001, label
            assert np.max(uv_error) < 0.001, (
                label,
                float(uv_error.max()),
                position[worst].tolist(),
                plane(frame, "uv")[mask, :2][worst].tolist(),
                expected_uv[worst].tolist(),
                float(np.max(np.abs(plane(frame, "uv")[mask] - plane(expected, "uv")[mask]))),
            )
            assert np.max(np.abs(rgba[mask, :3] - color)) < 0.0001, label
            normal = np.asarray((0.2, 0, 1) if tilted else (0, 0, 1), np.float32)
            normal /= np.linalg.norm(normal)
            normal_error = np.max(np.abs(plane(frame, "normal")[mask] - normal), axis=1)
            normal_worst = int(np.argmax(normal_error))
            assert normal_error.max() < 0.0001, (
                label,
                float(normal_error.max()),
                plane(frame, "normal")[mask][normal_worst].tolist(),
                plane(expected, "normal")[mask][normal_worst].tolist(),
                [v.attribValue("N") for v in attributes.geometry().prims()[0].vertices()],
            )
            status = session.status()
            checks.append(dict(name=label, mean_rgba_difference=error, interior_pixels=int(mask.sum()), status=status))
            return status

        before = compare("Triangle, quad and concave n-gon agree with independently triangulated Houdini geometry")
        assert before["mesh_topology_builds"] == 1 and before["mesh_capture_cache_bytes"] > 0
        original_diagonals = [[v.point().number() for v in p.vertices()] for p in divide.geometry().prims()]
        edit.parm("snippet").set("if(@ptnum==4) @P=set(-.68,-.48,0);")
        after = compare("Deformation makes the quad concave and updates its diagonal without changing connectivity")
        assert original_diagonals != [[v.point().number() for v in p.vertices()] for p in divide.geometry().prims()]
        assert after["mesh_topology_reuses"] == before["mesh_topology_reuses"] + 1, after
        assert after["mesh_triangulated_faces"] > before["mesh_triangulated_faces"], after
        assert after["mesh_retained_faces"] > before["mesh_retained_faces"], after
        before = after
        set_attributes((0.7, 0.25, 0.1), 0.17, True)
        after = compare(
            "UV, split-normal and color edits change the expected AOVs while retaining triangulation",
            color=(0.7, 0.25, 0.1),
            u_offset=0.17,
            tilted=True,
        )
        assert after["mesh_topology_reuses"] == before["mesh_topology_reuses"] + 1, after
        assert after["mesh_triangulated_faces"] == before["mesh_triangulated_faces"], after
        before = after
        stash.parm("stash").set(geometry(shift=0.12))
        after = compare(
            "Recreated detail IDs retain exact connectivity and move the rendered surface",
            color=(0.7, 0.25, 0.1),
            u_offset=0.17,
            tilted=True,
        )
        assert after["mesh_topology_scans"] > before["mesh_topology_scans"], after
        assert after["mesh_topology_reuses"] == before["mesh_topology_reuses"] + 1, after
        before = after
        # Same counts and face sizes, different connections: a count-only cache
        # would silently retain the old triangle rather than this new surface.
        stash.parm("stash").set(geometry([(0, 2, 3), (1, 6, 5, 4), faces[2]], shift=0.12))
        after = compare(
            "Same-size connectivity edit rebuilds topology and matches the independent reference",
            color=(0.7, 0.25, 0.1),
            u_offset=0.17,
            tilted=True,
        )
        assert after["mesh_topology_builds"] == before["mesh_topology_builds"] + 1, after
        compare(
            "Polygon soup preserves separate face provenance and vertex attributes",
            source=soup,
            color=(0.7, 0.25, 0.1),
            u_offset=0.17,
            tilted=True,
        )
        session.wait(session.apply(dict(geometry=[])))
        assert session.status()["mesh_capture_cache_bytes"] == 0
        assert session.status()["geometry_resources"] == 0
        checks.append(dict(name="Retirement releases triangulation metadata and geometry resources"))
    assert session.status()["owned_geometry_bytes"] == session.status()["mesh_capture_cache_bytes"] == 0
    reports.append(dict(device=device, checks=checks))
(destination / "houdini-mesh-capture-cache-smoke.json").write_text(
    json.dumps(dict(result="passed", devices=reports), indent=2), encoding="utf-8"
)
print(json.dumps(dict(result="passed", checks_per_device=[len(r["checks"]) for r in reports])))
