# SPDX-License-Identifier: Apache-2.0
"""Render real OBJ subdivision edits through a persistent Houdini IPR worker."""

import argparse
import hashlib
from itertools import combinations
import json
import math
from pathlib import Path
import time
import traceback

import cycles_houdini as ch
from cycles_houdini import device_selection, menu_actions, native_scene, object_properties, passes
import hou
import numpy as np

WIDTH, HEIGHT, SAMPLES = 128, 96, 8
MIB = 1024 * 1024
CACHE = "cycles_subdivision_cache_topology"
BUDGET = "ipr_topology_cache_mib"
STATS = (
    "hits",
    "misses",
    "admissions",
    "evictions",
    "oversized",
    "entries",
    "bytes",
    "budget_bytes",
    "active_retired_bytes",
)
PLANES = ("combined", "depth", "normal", "position", "uv")
COUNTS = ("subdivision_control_faces", "subdivision_vertices", "subdivision_triangles")
POSES = ((5.2, 0, 0), (3.6, 0.19, -0.13), (4.3, -0.17, 0.15))

# The SOP cooks at arbitrary shutter sample times. Face-varying values can
# change without changing the vertex-equivalence partition, or split a seam.
GRID = """
import hou
node = hou.pwd()
geo = node.geometry()
size = node.evalParm("size")
edit = node.evalParm("edit")
frame = hou.frame()
uv = geo.addAttrib(hou.attribType.Vertex, "uv", (0., 0., 0.))
edge = geo.addAttrib(hou.attribType.Vertex, "creaseweight", 0.)
corner = geo.addAttrib(hou.attribType.Point, "cornerweight", 0.)
points = []
for y in range(size):
    for x in range(size):
        point = geo.createPoint()
        z = ((x + 2*y) % 5) * .12
        dx = 0.
        if x == y == 1:
            if edit == "position":
                z += .65
            if edit.startswith("motion"):
                dx = .38 * (frame - 12.)
                z += dx
            if edit == "vertex-crease":
                point.setAttribValue(corner, 6.)
        point.setPosition((1.9 * x / (size-1) - .95 + dx, 1.5 * y / (size-1) - .75, z))
        points.append(point)
width = size - 1
count = width**2
middle = width + 1
order = list(range(count))
if edit == "face-order":
    order = order[1:] + order[:1]
for face in order:
    v = (face // width) * size + face % width
    quad = (v, v + size, v + size + 1, v + 1)
    if face == middle and edit == "connectivity":
        polygons = ((quad[0], quad[1], quad[2]), (quad[0], quad[2], quad[3]))
    elif face == middle and edit == "corner-order":
        polygons = (quad[1:] + quad[:1],)
    else:
        polygons = (quad,)
    for polygon in polygons:
        prim = geo.createPolygon()
        for p in polygon:
            vertex = prim.addVertex(points[p])
            offset = .125 if edit == "uv-values" else 0.
            if face == middle and (edit == "uv-seam" or (edit == "motion-seam" and frame > 12.1)):
                offset += .25
            u, v = (p % size) / size, (p // size) / size
            vertex.setAttribValue(uv, (u*u + offset, v*v, 0.))
            if face == middle and p == quad[0] and edit == "edge-crease":
                vertex.setAttribValue(edge, 6.)
"""


def cache_stats(status):
    result = {key: status["subdivision_topology_cache_" + key] for key in STATS}
    assert all(isinstance(value, int) and value >= 0 for value in result.values()), result
    assert result["bytes"] <= result["budget_bytes"] and result["active_retired_bytes"] == 0, result
    assert bool(result["entries"]) == bool(result["bytes"]), result
    return result


def delta(before, after, key):
    return after[key] - before[key]


class Fixture:
    def __init__(self, device, samples):
        self.samples = samples
        self.device = device
        self.scene = hou.node("/obj").createNode("subnet", "topology_cache_fixture")
        material = ch.create_material(name="subdivision_uv", shader="emission")
        material.parm("cycles_emission_sampling").set("none")
        coords = material.createNode("cycles::texture_coordinate::1", "coordinates")
        material.node("surface_shader").setNamedInput("color", coords, "UV")
        material.node("surface_shader").parm("strength").set(1)
        self.meshes = [self.create_mesh("primary", material, -1), self.create_mesh("secondary", material, 1)]
        self.camera = self.scene.createNode("cam", "camera")
        self.camera.setParms(dict(tz=5, projection=1, shutter=1))
        self.rop = menu_actions.create_rop()
        device_selection.set_devices(self.rop, [device])
        self.rop.setParms(
            dict(
                source_mode="objects",
                objectroot=self.scene.path(),
                vobject="*",
                lights="",
                camera=self.camera.path(),
                geometry_motion="deformation",
                resx=WIDTH,
                resy=HEIGHT,
                samples=self.samples,
                ipr_samples=self.samples,
                use_denoise=0,
                ipr_use_denoise=0,
                adaptive_sampling=0,
                ipr_adaptive_sampling=0,
                film_exposure=1,
                world_source="color",
                transparent_background=1,
                viewer_interactive=1,
                allowmotionblur=0,
                max_subdivisions=5,
                ipr_dicing_rate=1,
                dicing_rate=1,
                offscreen_dicing_scale=1,
                tessellation_update=1,
            )
        )
        self.rop.parmTuple("background").set((0, 0, 0))
        for name in PLANES[1:]:
            passes.add(self.rop, name)
        assert self.rop.evalParm(BUDGET) == 512

    def create_mesh(self, name, material, x):
        obj = self.scene.createNode("geo", name, run_init_scripts=False)
        obj.setParms(dict(tx=x, shop_materialpath=material.path()))
        sop = obj.createNode("python", "control_grid")
        sop.addSpareParmTuple(hou.IntParmTemplate("size", "Control Grid Size", 1, default_value=(4,)))
        sop.addSpareParmTuple(hou.StringParmTemplate("edit", "Control Grid Edit", 1, default_value=("",)))
        sop.parm("python").set(GRID)
        sop.setDisplayFlag(True)
        sop.setRenderFlag(True)
        object_properties.install(obj)
        assert obj.evalParm(CACHE) == 1
        obj.setParms(
            dict(
                cycles_subdivision_override=1,
                cycles_subdivision_type="catmull_clark",
                cycles_subdivision_adaptive=1,
                cycles_subdivision_space="pixel",
                cycles_subdivision_pixel_size=3,
                cycles_subdivision_fvar="boundaries",
                cycles_subdivision_boundary="edge_and_corner",
            )
        )
        return obj, sop

    def pose(self, index):
        width, tx, ty = POSES[index]
        self.camera.setParms(dict(orthowidth=width, tx=tx, ty=ty))

    def setup(self, budget, opted, *, pressure=False):
        hou.setFrame(12)
        self.rop.setParms(dict(vobject="*", allowmotionblur=0))
        self.rop.parm(BUDGET).set(budget)
        for (obj, sop), size, edit, enabled in zip(self.meshes, (4, 5), ("", "uv-seam"), opted):
            sop.setParms(dict(size=65 if pressure else size, edit=edit))
            obj.setParms(dict(cycles_subdivision_fvar="boundaries", cycles_subdivision_boundary="edge_and_corner"))
            obj.parm(CACHE).set(enabled)
        self.pose(0)

    def session(self, *, interactive=True, execution="worker"):
        return ch.Session(
            device=self.device,
            width=WIDTH,
            height=HEIGHT,
            samples=self.samples,
            execution=execution,
            interactive=interactive,
            collect_diagnostics=True,
            use_texture_cache=False,
            threads=2,
        )

    def submit(self, session, camera_only=False, *, refresh=None):
        update = native_scene.capture(self.rop, camera_only=camera_only)
        update["defer_camera_subdivision"] = False
        if refresh is not None:
            update["dicing"]["refresh_revision"] = refresh
            # Two placements request the same source SOP and materials, while
            # their independent OBJ overrides keep the cache policy explicit.
            a, b = update["geometry"]
            b.update(sop=a["sop"], geometry_key=a["geometry_key"])
        return session.apply(update)


def capture(fixture, session, records, label, *, pose=None, camera_only=False, refresh=None):
    if pose is not None:
        fixture.pose(pose)
    previous = session.status()
    start = time.perf_counter()
    generation = fixture.submit(session, camera_only=camera_only, refresh=refresh)
    frame = session.wait(generation, timeout=90)
    elapsed = time.perf_counter() - start
    status = session.status()
    assert frame["generation"] == generation and frame["complete"], status
    assert (frame["width"], frame["height"], frame["samples"]) == (WIDTH, HEIGHT, fixture.samples), status
    assert status["subdivision_control_faces"] and status["subdivision_triangles"], status
    assert not status["source_diagnostics"], status
    assert status["render_device_ids"] == [fixture.device], status
    if camera_only:
        for name in ("renderer_pid", "backend_epoch", "geometry_conversions", "coalesced_updates"):
            assert status[name] == previous[name], (label, name, previous[name], status[name])
        assert delta(previous, status, "subdivision_tessellations") == status["mesh_variant_resources"], (
            label,
            previous,
            status,
        )
    raw = {name: frame["planes"][name]["pixels"] for name in PLANES}
    rgba = np.frombuffer(raw["combined"], np.float32).reshape(-1, 4)
    assert np.isfinite(rgba).all() and 0 < np.count_nonzero(rgba[:, 3]) < len(rgba)
    assert np.std(rgba[:, :2]) > 0.06
    assert all(np.isfinite(np.frombuffer(data, np.float32)).all() for data in raw.values()), label
    assert np.count_nonzero(np.frombuffer(raw["uv"], np.float32)) > 100, label
    report = dict(
        counts={name: status[name] for name in COUNTS},
        resources={
            name: status[name]
            for name in ("object_resources", "geometry_resources", "mesh_variant_resources", "mesh_family_resources")
        },
        motion_samples=status["motion_samples"],
        seconds=elapsed,
        pid=status.get("renderer_pid"),
        epoch=status.get("backend_epoch"),
        tessellations=status["subdivision_tessellations"],
        conversions=status["geometry_conversions"],
        sha256={name: hashlib.sha256(data).hexdigest() for name, data in raw.items()},
    )
    report["cache"] = cache_stats(status)
    records[label] = report
    return raw


def parity_walk(fixture, records, budget, opted):
    fixture.setup(budget, opted)
    obj, sop = fixture.meshes[0]
    frames = {}
    with fixture.session() as session:

        def shot(label, **kwargs):
            frames[label] = capture(fixture, session, records, label, **kwargs)

        shot("cold")
        for label, pose in (("admit", 1), ("warm", 2), ("return", 0)):
            shot(label, pose=pose, camera_only=True)
        assert records["warm"]["pid"] == records["cold"]["pid"] != 0
        assert records["admit"]["counts"]["subdivision_triangles"] != records["cold"]["counts"]["subdivision_triangles"]

        for edit in (
            "position",
            "connectivity",
            "face-order",
            "corner-order",
            "edge-crease",
            "vertex-crease",
            "boundary",
            "fvar-mode",
            "uv-values",
            "uv-seam",
            "motion-position",
            "motion-seam",
        ):
            sop.parm("edit").set("")
            obj.setParms(dict(cycles_subdivision_fvar="boundaries", cycles_subdivision_boundary="edge_and_corner"))
            fixture.rop.parm("allowmotionblur").set(0)
            shot(edit + "-reset", pose=0)
            sop.parm("edit").set(edit)
            if edit == "boundary":
                obj.parm("cycles_subdivision_boundary").set("edge_only")
            if edit == "fvar-mode":
                obj.parm("cycles_subdivision_fvar").set("all")
            fixture.rop.parm("allowmotionblur").set(edit.startswith("motion"))
            shot(edit, pose=1)
            if edit.startswith("motion"):
                assert records[edit]["motion_samples"] >= 3, records[edit]
            shot(edit + "-redice", pose=2, camera_only=True)
            shot(edit + "-revisit", pose=1, camera_only=True)

        for edit in (
            "position",
            "connectivity",
            "edge-crease",
            "vertex-crease",
            "uv-values",
            "uv-seam",
            "motion-position",
        ):
            assert frames[edit]["combined"] != frames["admit"]["combined"], edit

    check_walk(records, opted, budget)
    closed = cache_stats(session.status())
    assert closed["bytes"] == closed["entries"] == 0, closed
    return frames


def check_walk(records, opted, budget):
    first, second, warm, returned = (records[label]["cache"] for label in ("cold", "admit", "warm", "return"))
    for record in records.values():
        assert record["cache"]["budget_bytes"] == budget * MIB, record
    if not budget:
        assert all(all(value == 0 for value in record["cache"].values()) for record in records.values()), records
        return
    enabled = sum(opted)
    assert first["misses"] == second["admissions"] == warm["entries"] == enabled, records
    assert second["misses"] == 2 * enabled and second["hits"] == first["entries"] == 0, records
    assert delta(second, warm, "hits") == delta(warm, returned, "hits") == enabled, records
    assert warm["evictions"] == warm["oversized"] == 0, records
    for label in (
        "connectivity",
        "face-order",
        "corner-order",
        "edge-crease",
        "vertex-crease",
        "boundary",
        "fvar-mode",
        "uv-values",
        "uv-seam",
    ):
        before, after = (records[key] for key in (label + "-reset", label))
        misses = delta(before["cache"], after["cache"], "misses")
        hits = delta(before["cache"], after["cache"], "hits")
        # Full updates may dice again while preparing a replacement mesh.
        # In the mixed run only the edited mesh can contribute a miss.
        assert misses >= 1 and hits + misses <= delta(before, after, "tessellations"), (label, before, after)
        if enabled == 1:
            assert hits + misses < delta(before, after, "tessellations"), (label, before, after)
    before, after = (records[key]["cache"] for key in ("position-reset", "position"))
    assert delta(before, after, "misses") == 0 and delta(before, after, "hits") >= 1, (before, after)


def difference(reference, candidate, *, gpu):
    assert reference.keys() == candidate.keys()
    maxima = {}
    for label, expected in reference.items():
        for name, data in candidate[label].items():
            assert len(expected[name]) == len(data), (label, name)
            if not gpu:
                assert expected[name] == data, (label, name)
                continue
            left, right = (np.frombuffer(blob, np.float32) for blob in (expected[name], data))
            assert np.isfinite(left).all() and np.isfinite(right).all(), (label, name)
            channels = [(name, left, right)]
            if name == "combined":
                left, right = left.reshape(-1, 4), right.reshape(-1, 4)
                channels = [("rgb", left[:, :3], right[:, :3]), ("alpha", left[:, 3], right[:, 3])]
            for channel, a, b in channels:
                if channel in ("alpha", "depth", "position"):
                    assert a.tobytes() == b.tobytes(), (label, channel)
                diff = a.astype(np.float64) - b
                peak, rms = float(np.max(np.abs(diff))), float(np.sqrt(np.mean(diff * diff)))
                old_peak, old_rms = maxima.get(channel, (0.0, 0.0))
                maxima[channel] = max(old_peak, peak), max(old_rms, rms)
    return maxima


def shared_source_check(fixture, records):
    images = {}
    modes = (("off", (0, 0)), ("mixed", (1, 0)), ("mixed-reversed", (0, 1)))
    for mode, opted in modes:
        fixture.setup(32, opted)
        for obj, _ in fixture.meshes:
            obj.setParms(dict(cycles_subdivision_space="object", cycles_subdivision_edge_length=0.1))
        try:
            with fixture.session() as session:
                for phase in range(3):
                    label = f"shared-{mode}-{phase}"
                    raw = capture(fixture, session, records, label, pose=phase, refresh=phase)
                    row = records[label]
                    physical = row["resources"]
                    assert physical["object_resources"] == 2, row
                    assert physical["mesh_family_resources"] == physical["mesh_variant_resources"] == 1, row
                    assert row["tessellations"] == phase + 1, row
                    assert images.setdefault(phase, raw) == raw, label
                cached = row["cache"]
                assert cached["entries"] == int(any(opted)), cached
                assert cached["hits"] == int(any(opted)), cached
        finally:
            for obj, _ in fixture.meshes:
                obj.parm("cycles_subdivision_space").set("pixel")


def wait_for_state(session, wanted):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        status = session.status()
        if status["state"] == wanted:
            return status
        if status["state"] == "failed":
            break
        time.sleep(0.01)
    raise AssertionError((wanted, status))


def lifetime_checks(fixture, records):
    fixture.setup(32, (1, 0))
    with fixture.session() as first:
        for label, pose in (("first-cold", 0), ("first-admit", 1), ("first-warm", 2)):
            expected = capture(fixture, first, records, label, pose=pose, camera_only=pose != 0)
        warm = records["first-warm"]
        assert warm["cache"]["entries"] == 1 and warm["cache"]["hits"] == 1, warm

        # Both workers are live and hold the same authored scene at once.
        with fixture.session() as second:
            capture(fixture, second, records, "second-cold", pose=0)
            cold = records["second-cold"]
            assert cold["pid"] != warm["pid"] and cold["cache"]["misses"] == 1, cold
            assert cold["cache"]["entries"] == cold["cache"]["hits"] == 0, cold
            capture(fixture, second, records, "second-admit", pose=1, camera_only=True)
            assert cache_stats(first.status()) == warm["cache"]
        retired = wait_for_state(second, "closed")
        assert cache_stats(retired)["bytes"] == 0, retired
        assert not retired["owned_geometry_bytes"] and not retired["device_allocation_bytes"], retired

        # Stop while a new render is pending and recover on the same worker.
        first.pause()
        wait_for_state(first, "paused")
        fixture.pose(1)
        fixture.submit(first, camera_only=True)
        first.cancel()
        stopped = wait_for_state(first, "cancelled")
        assert stopped["renderer_pid"] == warm["pid"], stopped
        frame = capture(fixture, first, records, "first-recover", pose=2)
        assert frame == expected
        assert records["first-recover"]["pid"] == warm["pid"]
        assert records["first-recover"]["cache"]["entries"] == 1

    # A noninteractive session must ignore cache requests even if a client
    # supplies an IPR payload directly. Exercise repeated dicing, then compare
    # a separately rendered noninteractive reference at the same camera.
    final = {}
    for mode, budget in (("final-requested", 32), ("final-reference", 0)):
        fixture.setup(budget, (1, 1))
        with fixture.session(interactive=False, execution="sync") as session:
            for pose in (0, 1, 2):
                label = f"{mode}-{pose}"
                final[budget, pose] = capture(fixture, session, records, label, pose=pose)
                assert all(value == 0 for value in records[label]["cache"].values()), records[label]
    for pose in (0, 1, 2):
        assert final[32, pose] == final[0, pose], pose


def pressure_walk(fixture, records):
    def edit(session, label, pose, *, camera_only):
        return capture(fixture, session, records, label, pose=pose, camera_only=camera_only)

    # Measure each entry's real charge before choosing a budget that fits one
    # entry but cannot retain both. The two grids have distinct UV partitions.
    sizes = []
    for obj, _ in fixture.meshes:
        fixture.setup(128, (1, 1), pressure=True)
        fixture.rop.parm("vobject").set(obj.name())
        with fixture.session() as session:
            edit(session, obj.name() + "-cold", 0, camera_only=False)
            edit(session, obj.name() + "-admit", 1, camera_only=True)
            cache = records[obj.name() + "-admit"]["cache"]
            assert cache["entries"] == 1 and cache["admissions"] == 1, cache
            sizes.append(cache["bytes"])
    limit = math.ceil(max(sizes) / MIB)
    assert min(sizes) > MIB and max(sizes) <= limit * MIB < sum(sizes), sizes

    fixture.setup(0, (1, 1), pressure=True)
    with fixture.session() as session:
        expected = {
            pose: edit(session, f"pressure-reference-{pose}", pose, camera_only=pose != 0) for pose in (0, 1, 2)
        }
    fixture.setup(128, (1, 1), pressure=True)
    with fixture.session() as session:

        def shot(label, pose, camera_only=True):
            assert expected[pose] == edit(session, label, pose, camera_only=camera_only), label
            return records[label]["cache"]

        shot("pressure-cold", 0, False)
        warm = shot("pressure-admit", 1)
        assert warm["entries"] == warm["admissions"] == 2 and warm["bytes"] == sum(sizes), warm
        fixture.rop.parm(BUDGET).set(limit)
        first = shot("pressure-tighten", 2, False)
        assert first["entries"] == 1 and first["evictions"] >= 1, first
        assert first["budget_bytes"] == limit * MIB and first["oversized"] == 0, first
        second = shot("pressure-revisit", 0)
        assert second["entries"] == 1 and second["oversized"] == 0, second
        assert second["misses"] > first["misses"], second

        fixture.rop.parm(BUDGET).set(1)
        oversized = shot("oversized", 0, False)
        oversized_again = shot("oversized-again", 1)
        assert oversized["entries"] == oversized_again["entries"] == 0, records
        assert oversized_again["budget_bytes"] == MIB, oversized_again
        assert delta(oversized, oversized_again, "oversized") == 2, oversized_again

        fixture.rop.parm(BUDGET).set(128)
        shot("rehydrate-start", 2, False)
        shot("rehydrate-admit", 0)
        ready = shot("rehydrate-warm", 1)
        assert ready["entries"] == 2 and ready["bytes"] == sum(sizes), ready
        assert delta(oversized_again, ready, "admissions") == 2, ready
        returned = shot("rehydrate-return", 2)
        assert delta(ready, returned, "hits") == 2, returned

        fixture.rop.parm(BUDGET).set(0)
        disabled = shot("disable-live", 0, False)
        assert disabled["entries"] == disabled["bytes"] == disabled["budget_bytes"] == 0, disabled
        assert shot("disabled-redice", 1) == disabled


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default="CPU", help="CPU, OPTIX, or an exact Cycles OptiX device ID")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--optix-variation", action="store_true", help="Record optional eight-sample GPU variation")
    args = parser.parse_args()
    devices = ch.devices()
    if args.device == "OPTIX":
        device = next((item["id"] for item in devices if item["backend"] == "OPTIX"), None)
    else:
        device = next((item["id"] for item in devices if item["id"] == args.device), None)
    assert device is not None, (args.device, devices)
    gpu = device != "CPU"
    if args.optix_variation and not gpu:
        parser.error("--optix-variation requires an OptiX device")
    # OptiX can accumulate independent samples in different orders. One
    # path per pixel allows bitwise pass comparison without a tolerance.
    samples = SAMPLES if not gpu or args.optix_variation else 1
    report = dict(
        result="running",
        houdini=hou.applicationVersionString(),
        device=device,
        samples=samples,
        comparison="repeat-variation" if args.optix_variation else "bitwise",
        walks={},
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    try:
        fixture = Fixture(device, samples)

        def walk(name, budget, opted):
            records = report["walks"][name] = {}
            return parity_walk(fixture, records, budget, opted)

        reference = walk("off", 0, (0, 0))
        failures = []
        if gpu and not args.optix_variation:
            difference(reference, walk("off-repeat", 0, (0, 0)), gpu=False)
        if args.optix_variation:
            controls = {"off": reference}
            for repeat in range(1, 4):
                name = f"off-repeat-{repeat}"
                controls[name] = walk(name, 0, (0, 0))
            pairs = {f"{a}/{b}": difference(controls[a], controls[b], gpu=True) for a, b in combinations(controls, 2)}
            report["gpu_control_differences"] = pairs
            report["gpu_repeat_floor"] = {
                plane: tuple(max(pair[plane][metric] for pair in pairs.values()) for metric in range(2))
                for plane in next(iter(pairs.values()))
            }
        for name, opted in (("on", (1, 1)), ("mixed", (1, 0))):
            actual = difference(reference, walk(name, 32, opted), gpu=args.optix_variation)
            if args.optix_variation:
                report.setdefault("gpu_differences", {})[name] = actual
                for plane, observed in actual.items():
                    floor = report["gpu_repeat_floor"][plane]
                    if any(a > b for a, b in zip(observed, floor)):
                        failures.append((name, plane, observed, floor))
            for label, row in report["walks"][name].items():
                assert row["counts"] == report["walks"]["off"][label]["counts"], (name, label, row)
        if args.optix_variation:
            report["gpu_exceeds_repeat"] = failures
        if not gpu:
            report.update(lifetimes={}, pressure={}, shared={})
            lifetime_checks(fixture, report["lifetimes"])
            pressure_walk(fixture, report["pressure"])
            shared_source_check(fixture, report["shared"])
        report["result"] = "passed"
    except BaseException:
        report.update(result="failed", error=traceback.format_exc())
        raise
    finally:
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
