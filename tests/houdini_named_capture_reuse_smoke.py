# SPDX-License-Identifier: Apache-2.0
"""Per-demanded-field native capture reuse, recovery and same-build read control."""

import argparse
import json
import time
import traceback
import hou
import numpy as np
import cycles_houdini as ch
from mesh_field_fixture import plane
from named_capture_fixture import Fixture


parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
parser.add_argument("--heavy", action="store_true")
args = parser.parse_args()
device = json.loads(args.device) if args.device.startswith("[") else args.device
f = Fixture(device if isinstance(device, str) else "CPU", heavy=args.heavy)
path = f.dest / "named-capture-reuse-report.json"
report = dict(result="running", device=device, heavy=args.heavy, scope=__doc__, recovery=[], controls={})
metrics = (
    "mesh_named_fields_captured",
    "mesh_named_fields_reused",
    "mesh_named_values_read",
    "mesh_named_values_reused",
    "geometry_conversions",
    "mesh_topology_builds",
    "mesh_topology_reuses",
    "mesh_triangulated_faces",
)


def session():
    return ch.Session(
        device=device,
        width=f.width,
        height=f.height,
        samples=1,
        threads=2,
        execution="worker",
        use_texture_cache=False,
        collect_diagnostics=True,
    )


def render(active):
    start = time.perf_counter()
    f.output.geometry()  # Explicitly account for Houdini cooking outside capture.
    cooked = time.perf_counter()
    ids = f.ids()
    update = f.update()
    update["passes"] = ["combined", "position", "depth", "normal", "uv", "material_id"]
    frame = active.wait(active.apply(update, reuse_if_unchanged=True), timeout=240)
    deadline = time.monotonic() + 15
    while True:
        status = active.status()
        if status["state"] == "failed" or (
            status["state"] == "complete" and status["generation"] == frame["generation"]
        ):
            break
        assert time.monotonic() < deadline, status
        time.sleep(0.005)
    assert status["state"] == "complete" and not status["error"], status
    assert status["object_resources"] == 12 and status["mesh_variant_resources"] == 1, status
    images = {name: plane(frame, name).copy() for name in frame["planes"]}
    row = dict(
        status=status,
        source_data_ids=ids,
        source_cook_seconds=cooked - start,
        complete_image_seconds=time.perf_counter() - start,
        capture_cook_seconds=status["cooking_seconds"],
        native_geometry_conversion_seconds=status["geometry_conversion_seconds"],
        apply_profile=dict(active._controller.backend.last_apply_profile),
    )
    return frame, images, row


def check(frame, color, uv=(0.2, 0.4)):
    rgba = plane(frame, "combined")
    mask = (rgba[:, :, 3] > 0.999) & (plane(frame, "depth")[:, :, 0] < 100)
    assert mask.sum() > 500
    np.testing.assert_allclose(rgba[:, :, :3][mask], np.broadcast_to(color, (mask.sum(), 3)), rtol=0, atol=3e-5)
    actual_uv = plane(frame, "uv")[:, :, :2][mask]
    np.testing.assert_allclose(actual_uv, np.broadcast_to(uv, actual_uv.shape), rtol=0, atol=3e-5)
    np.testing.assert_allclose(plane(frame, "position")[:, :, 2][mask], 0, rtol=0, atol=3e-5)


def delta(row, before):
    return {name: row["status"][name] - (before["status"][name] if before else 0) for name in metrics}


def same_images(actual, expected):
    assert actual.keys() == expected.keys()
    for name, pixels in actual.items():
        if name.startswith("Crypto") or name == "material_id":
            np.testing.assert_array_equal(pixels.view(np.uint32), expected[name].view(np.uint32), err_msg=name)
        else:
            np.testing.assert_allclose(pixels, expected[name], rtol=0, atol=2e-5, err_msg=name)


def close_check(active):
    closed = active.status()
    assert (
        closed["device_allocation_bytes"] == closed["owned_geometry_bytes"] == closed["mesh_capture_cache_bytes"] == 0
    ), closed


try:
    # A live worker keeps its capture cache over independent edits and repairs.
    # Expected reads count emitted corners, rather than guessed host point counts.
    with session() as active:
        previous = None
        corner_count = None
        states = (
            ("initial", None, None, None, (0.33, 0.48, 0.65), (0.2, 0.4), False, 2, 0, "all"),
            ("uv_only", "uv", "v@uv.x+=.1;", None, (0.33, 0.48, 0.65), (0.3, 0.4), False, 0, 2, "none"),
            ("color_only", "Cd", "v@Cd.y+=.1;", None, (0.33, 0.58, 0.65), (0.3, 0.4), False, 0, 2, "none"),
            ("vector_edit", "paint", "v@paint.z+=.1;", None, (0.33, 0.58, 0.75), (0.3, 0.4), False, 1, 1, "one"),
            ("scalar_edit", "mask", "f@mask+=.1;", None, (0.43, 0.68, 0.85), (0.3, 0.4), False, 2, 0, "all"),
            ("invalid_vector", "paint", 'v@paint.z=atof("nan");', None, (1, 0, 1), (0.3, 0.4), True, 1, 1, "invalid"),
            ("invalid_vector_cached", "uv", "v@uv.x+=.2;", None, (1, 0, 1), (0.4, 0.4), True, 0, 2, "none"),
            ("repair_vector", "paint", "", None, (0.43, 0.68, 0.75), (0.4, 0.4), False, 1, 1, "one"),
            ("invalid_schema", None, None, 8, (1, 0, 1), (0.4, 0.4), True, 1, 1, "none"),
            ("invalid_schema_rechecked", "uv", "", None, (1, 0, 1), (0.2, 0.4), True, 1, 1, "none"),
            ("repair_schema", None, None, 0, (0.43, 0.68, 0.75), (0.2, 0.4), False, 1, 1, "one"),
            ("deleted_vector", None, None, 7, (1, 0, 1), (0.2, 0.4), True, 0, 1, "none"),
            ("deleted_vector_cached", "uv", "v@uv.x+=.1;", None, (1, 0, 1), (0.3, 0.4), True, 0, 1, "none"),
            ("recreated_vector", None, None, 0, (0.43, 0.68, 0.75), (0.3, 0.4), False, 1, 1, "one"),
            ("float2_schema", None, None, 1, (0.43, 0.68, 0.73), (0.3, 0.4), False, 1, 1, "one"),
            ("integer_schema", None, None, 2, (1.3, 1.55, 1.62), (0.3, 0.4), False, 2, 0, "all"),
            ("vertex_owner", None, None, 3, (0.33, 0.58, 0.65), (0.3, 0.4), False, 1, 1, "one"),
            ("primitive_owner", None, None, 4, (0.33, 0.58, 0.65), (0.3, 0.4), False, 1, 1, "one"),
            ("detail_owner", None, None, 5, (0.33, 0.58, 0.65), (0.3, 0.4), False, 1, 1, "one"),
            ("vertex_wins_over_invalid_detail", None, None, 6, (0.33, 0.58, 0.65), (0.3, 0.4), False, 1, 1, "one"),
            ("point_owner_repaired", None, None, 0, (0.43, 0.68, 0.75), (0.3, 0.4), False, 1, 1, "one"),
            ("camera_only", None, None, None, (0.43, 0.68, 0.75), (0.3, 0.4), False, 0, 0, "none"),
            ("object_only", None, None, None, (0.43, 0.68, 0.75), (0.3, 0.4), False, 0, 0, "none"),
        )
        for label, name, code, mode, color, uv, broken, captured, reused, reads in states:
            if name is not None:
                f.edits[name].parm("snippet").set(code)
            if mode is not None:
                f.set_mode(mode)
            if label == "camera_only":
                f.camera.parm("tx").set(0.07)
            if label == "object_only":
                f.obj.parm("tx").set(0.04)
            frame, images, row = render(active)
            row["label"] = label
            row["delta"] = changes = delta(row, previous)
            report["recovery"].append(row)
            # Persist the accepted status before image checks as well: a
            # diagnostic fallback must remain inspectable when its image is
            # unexpected, not just when a reuse counter is unexpected.
            path.write_text(json.dumps(report, indent=2), encoding="utf-8")
            check(frame, color, uv)
            issues = row["status"]["source_diagnostics"]
            assert bool(issues) == broken, (label, issues)
            if broken:
                assert any(
                    issue.get("attribute") == "paint" or "paint" in issue.get("message", "") for issue in issues
                ), issues
            if corner_count is None:
                assert changes["mesh_named_values_read"] > 0 and changes["mesh_named_values_read"] % 2 == 0
                corner_count = changes["mesh_named_values_read"] // 2
                report["emitted_corners"] = corner_count
            expected_reads = dict(all=corner_count * 2, one=corner_count, none=0, invalid=1)[reads]
            assert changes["mesh_named_fields_captured"] == captured, (label, changes)
            assert changes["mesh_named_fields_reused"] == reused, (label, changes)
            assert changes["mesh_named_values_read"] == expected_reads, (label, changes, expected_reads)
            if previous:
                assert row["source_data_ids"]["point:P"] == previous["source_data_ids"]["point:P"], label
                assert changes["mesh_triangulated_faces"] == 0 and changes["mesh_topology_builds"] == 0, (
                    label,
                    changes,
                )
                if label in ("uv_only", "color_only", "invalid_vector_cached"):
                    for field in ("paint", "mask"):
                        assert (
                            row["source_data_ids"]["point:" + field] == previous["source_data_ids"]["point:" + field]
                        ), (label, field)
                if label == "scalar_edit":
                    for field in ("paint", "mask"):
                        assert (
                            row["source_data_ids"]["point:" + field] != previous["source_data_ids"]["point:" + field]
                        ), (label, field)
            if label in ("camera_only", "object_only"):
                assert changes["geometry_conversions"] == 0, (label, changes)
            path.write_text(json.dumps(report, indent=2), encoding="utf-8")
            previous = row
    close_check(active)

    # Same runtime, shader and scene: the control bumps only demanded named IDs.
    # No old executable, extra execution path or artist-facing option is needed.
    reference = {}
    for mode in ("forced_read", "retained"):
        f.reset()
        f.control.parm("force_ids").set(mode == "forced_read")
        rows = []
        with session() as active:
            for index, offset in enumerate((0, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08)):
                f.edits["uv"].parm("snippet").set(f"v@uv.x+={offset};")
                frame, images, row = render(active)
                check(frame, (0.33, 0.48, 0.65), (0.2 + offset, 0.4))
                assert not row["status"]["source_diagnostics"], row["status"]
                row["delta"] = changes = delta(row, rows[-1] if rows else None)
                if index:
                    assert row["source_data_ids"]["point:P"] == rows[0]["source_data_ids"]["point:P"]
                    assert changes["mesh_topology_builds"] == changes["mesh_triangulated_faces"] == 0, changes
                    assert changes["mesh_named_values_read"] == (2 * corner_count if mode == "forced_read" else 0), (
                        changes
                    )
                    assert changes["mesh_named_values_reused"] == (0 if mode == "forced_read" else 2 * corner_count), (
                        changes
                    )
                    for name in ("paint", "mask"):
                        changed = (
                            row["source_data_ids"]["point:" + name] != rows[-1]["source_data_ids"]["point:" + name]
                        )
                        assert changed == (mode == "forced_read"), (mode, name, row["source_data_ids"])
                if mode == "forced_read":
                    reference[index] = images
                else:
                    same_images(images, reference[index])
                rows.append(row)
                report["controls"][mode] = rows
                path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        close_check(active)

    # A P-only edit can change an n-gon's emitted-corner mapping while all
    # named-field IDs and coarse connectivity stay unchanged. Retaining its
    # previous corner arrays would corrupt the interpolated surface values.
    dense_fixture = f
    f = Fixture(device if isinstance(device, str) else "CPU", heavy=False)
    stash = f.obj.createNode("stash", "independent_quad")
    geo = hou.Geometry()
    points = []
    for position in ((-0.8, -0.65, 0), (-0.1, -0.65, 0), (-0.1, 0.15, 0), (-0.8, 0.15, 0)):
        point = geo.createPoint()
        point.setPosition(position)
        points.append(point)
    polygon = geo.createPolygon()
    for index in (0, 3, 2, 1):
        polygon.addVertex(points[index])
    stash.parm("stash").set(geo)
    seed = f.obj.node("prototype_fields")
    seed.setInput(0, stash)
    seed.parm("snippet").set("v@uv={.2,.4,0}; v@Cd={.2,.4,.6}; v@paint=set(.03,.02,.01)*@ptnum; f@mask=.01*@ptnum;")
    moved = f.obj.createNode("attribwrangle", "concave_deformation")
    moved.setInput(0, f.output)
    f.output = moved
    f.copies.setInput(0, moved)
    divide = f.obj.createNode("divide", "independent_mapping_check")
    divide.setInput(0, moved)
    divide.setParms(dict(convex=1, usemaxsides=1, numsides=3, planar=0))
    report["mapping"] = []
    before = None
    with session() as active:
        for label, code in (("quad", ""), ("concave", "if(@ptnum==1)@P=set(-.68,-.48,0);"), ("quad_repair", "")):
            moved.parm("snippet").set(code)
            frame, images, row = render(active)
            with session() as fresh:
                _, expected, reference_row = render(fresh)
                same_images(images, expected)
            close_check(fresh)
            row.update(
                label=label,
                reference_status=reference_row["status"],
                delta=delta(row, before),
                triangles=[[v.point().number() for v in p.vertices()] for p in divide.geometry().prims()],
            )
            if before:
                assert row["triangles"] != before["triangles"], row["triangles"]
                assert row["source_data_ids"]["point:P"] != before["source_data_ids"]["point:P"]
                for name in ("paint", "mask"):
                    assert row["source_data_ids"]["point:" + name] == before["source_data_ids"]["point:" + name]
                assert (
                    row["delta"]["mesh_named_fields_captured"] == 2 and row["delta"]["mesh_named_fields_reused"] == 0
                ), row["delta"]
                assert row["delta"]["mesh_named_values_read"] == 12, row["delta"]
                assert row["delta"]["mesh_topology_reuses"] == 1, row["delta"]
            report["mapping"].append(row)
            path.write_text(json.dumps(report, indent=2), encoding="utf-8")
            before = row
    close_check(active)
    f = dense_fixture
    report.update(result="passed", recovery_states=len(report["recovery"]), paired_states=len(reference))
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
finally:
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
assert report["result"] == "passed", report.get("error")
print(
    json.dumps(
        dict(result=report["result"], recovery_states=report["recovery_states"], paired_states=report["paired_states"])
    )
)
