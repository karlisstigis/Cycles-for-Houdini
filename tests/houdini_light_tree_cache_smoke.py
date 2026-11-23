# SPDX-License-Identifier: Apache-2.0
"""Persistent mesh lights versus fresh cache-disabled renders of the same scene.

Illumination, transforms, packed sharing, source edits, displacement, subdivision,
visibility and retirement are checked independently of cache hit counters.
"""

from support.paths import artifacts_root
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import traceback

import hou
import numpy as np
import cycles_houdini as ch

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="")
args = parser.parse_args()
selection = json.loads(args.device) if args.device.startswith("[") else args.device
devices = [selection] if selection else ["CPU", ch.default_device()]
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage1/light-tree-cache"
destination.mkdir(parents=True, exist_ok=True)
report = dict(result="running", scope=__doc__, devices=[])
path = destination / "report.json"
base_rows = int(os.environ.get("CYCLES_HOUDINI_LIGHT_TREE_ROWS", "5"))
base_cols = int(os.environ.get("CYCLES_HOUDINI_LIGHT_TREE_COLS", "6"))
require_parallel = os.environ.get("CYCLES_HOUDINI_REQUIRE_PARALLEL_LIGHT_TREE") == "1"
reference_history = os.environ.get("CYCLES_HOUDINI_LIGHT_TREE_REFERENCE_HISTORY") == "1"
assert 2 <= base_rows <= 1024 and 2 <= base_cols <= 1024
hou.setFps(24)
hou.setFrame(1)

emitter = hou.node("/obj").createNode("geo", "cache_emitter", run_init_scripts=False)
emitter.setParms(dict(tx=-0.65, tz=1))
grid = emitter.createNode("grid", "mesh_light")
grid.setParms(dict(type=0, orient=0, rows=base_rows, cols=base_cols))
grid.parmTuple("size").set((0.9, 0.8))
shape = emitter.createNode("attribwrangle", "animated_mesh_light")
shape.setInput(0, grid)
shape.addSpareParmTuple(hou.FloatParmTemplate("amplitude", "Shape", 1))
shape.parm("snippet").set('@P.z += chf("amplitude")*sin(@P.x*8+@P.y*5+@Time*24);')
pack = emitter.createNode("pack", "shared_mesh_light")
pack.setInput(0, shape)
pack.parm("pivot").set("origin")
targets = emitter.createNode("grid", "light_instances")
targets.setParms(dict(type=0, orient=0, rows=2, cols=2))
targets.parmTuple("size").set((0.6, 0.6))
orient = emitter.createNode("attribwrangle", "keep_instance_orientation")
orient.setInput(0, targets)
orient.parm("snippet").set("v@N={0,0,1}; v@up={0,1,0};")
copies = emitter.createNode("copytopoints", "four_mesh_lights")
copies.setInput(0, pack)
copies.setInput(1, orient)

secondary = hou.node("/obj").createNode("geo", "second_cache_emitter", run_init_scripts=False)
secondary.setParms(dict(tx=-0.1, ty=0.8, tz=1.2))
secondary_grid = secondary.createNode("grid", "independent_light_topology")
secondary_grid.setParms(dict(type=0, orient=0, rows=4, cols=5))
secondary_grid.parmTuple("size").set((0.4, 0.35))

receiver = hou.node("/obj").createNode("geo", "lit_receiver", run_init_scripts=False)
receiver.parm("tx").set(0.75)
receiver_grid = receiver.createNode("grid", "receiver")
receiver_grid.setParms(dict(type=0, orient=0, rows=2, cols=2))
receiver_grid.parmTuple("size").set((1.0, 1.0))
receiver_front = receiver.createNode("reverse", "face_mesh_light")
receiver_front.setInput(0, receiver_grid)
camera = hou.node("/obj").createNode("cam", "light_cache_camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=4.5))
width, height, samples = 96, 72, 256
worker_logs = {}


def material(color=(0.3, 1.0, 0.1), height=None, kind="emission"):
    if height is None:
        return dict(key="emitter", kind=kind, color=color, strength=3.0)
    return dict(
        key="emitter",
        kind="graph",
        graph=dict(
            nodes=[
                dict(id="surface", definition="cycles/emission/1", parameters=dict(color=color, strength=3.0)),
                dict(
                    id="offset",
                    definition="cycles/displacement/1",
                    parameters=dict(height=height, midlevel=0.0, scale=1.0, space="object"),
                ),
            ],
            links=[],
            surface=dict(node="surface", socket="emission"),
            displacement=dict(node="offset", socket="displacement"),
            displacement_method="true",
        ),
    )


def pixels(frame, name):
    plane = frame["planes"][name]
    return np.frombuffer(plane["pixels"], np.float32).reshape(height, width, plane["channels"])


def log_records(session, marker="CH_LIGHT_TREE_CACHE "):
    log = session.diagnostics()["controller"]["worker_log"]
    if log is not None:
        worker_logs[id(session)] = log
    else:
        log = worker_logs.get(id(session))
    if log is None:
        return []
    return [
        json.loads(line.split(marker, 1)[1])
        for line in Path(log).read_text(encoding="utf-8", errors="replace").splitlines()
        if line.startswith(marker)
    ]


def new_session(device):
    return ch.Session(
        device=device,
        width=width,
        height=height,
        samples=samples,
        interactive=False,
        threads=2,
        execution="worker",
        use_texture_cache=False,
        collect_diagnostics=True,
        passes=["combined", "depth", "normal", "position"],
    )


original_disable = os.environ.pop("CYCLES_HOUDINI_DISABLE_LIGHT_TREE_CACHE", None)
original_profile = os.environ.get("CYCLES_HOUDINI_PROFILE_LIGHT_TREE")
os.environ["CYCLES_HOUDINI_PROFILE_LIGHT_TREE"] = "1"
try:
    for device in devices:
        emitter.setParms(dict(tx=-0.65, ty=0, tz=1, rx=0, ry=0, rz=0, sx=1, sy=1, sz=1))
        camera.parm("tx").set(0)
        secondary.setParms(dict(tx=-0.1, ty=0.8, tz=1.2, rz=0, sx=1, sy=1, sz=1))
        targets.setParms(dict(rows=2, cols=2))
        grid.setParms(dict(rows=base_rows, cols=base_cols))
        shape.parm("amplitude").set(0)
        hou.setFrame(1)
        state = dict(
            source=shape,
            surface=material(),
            visible=255,
            subdiv=dict(type="none"),
            included=True,
            tree=True,
            secondary=False,
        )
        rows = []
        device_report = dict(
            device=device,
            checks=rows,
            reference="independent cache-disabled worker with matching edit history"
            if reference_history
            else "fresh cache-disabled worker per state",
        )
        report["devices"].append(device_report)
        session = new_session(device)
        reference_owner = [None]

        def scene():
            geometry = [ch.geometry_request(receiver, receiver_front, material="receiver", material_mode="uniform")]
            if state["included"]:
                request = ch.geometry_request(
                    emitter, state["source"], material="emitter", material_mode="uniform", subdivision=state["subdiv"]
                )
                request["visibility"] = state["visible"]
                geometry.append(request)
            if state["secondary"]:
                geometry.append(
                    ch.geometry_request(secondary, secondary_grid, material="secondary", material_mode="uniform")
                )
            return dict(
                geometry=geometry,
                materials=[
                    state["surface"],
                    dict(key="receiver", kind="diffuse", color=(0.8, 0.8, 0.8)),
                    dict(key="secondary", kind="emission", color=(0.1, 0.2, 1.0), strength=3.0),
                ],
                camera_path=camera.path(),
                background=(0, 0, 0),
                rendering=dict(
                    samples=samples, adaptive_sampling=False, light_tree=state["tree"], seed=7, animate_seed=False
                ),
            )

        def compare(name, cache=None, camera_only=False, write=False, require_swap=False):
            before = session.status()
            count = len(log_records(session)) if before["generation"] else 0
            update = scene()
            actual = session.wait(session.apply(dict(camera_path=camera.path()) if camera_only else update))
            status = session.status()
            assert status["state"] == "complete" and not status["error"], status
            assert not status["source_diagnostics"], status["source_diagnostics"]
            records = log_records(session)[count:]
            complete = [r for r in records if r.get("complete")]
            for record in complete:
                assert record["enabled"] and 0 <= record["bytes"] <= record["budget_bytes"], record
                assert record["entries"] <= 1024, record
            if cache in ("hit", "miss"):
                assert complete and complete[-1][cache + "es" if cache == "miss" else "hits"] >= 1, (name, complete)
                if cache == "hit":
                    assert complete[-1]["view_nodes"] > 0 and complete[-1]["clone_seconds"] == 0, complete[-1]
            elif cache == "all-hit":
                assert complete and complete[-1]["hits"] >= 1 and complete[-1]["misses"] == 0, (name, complete)
            elif cache == "empty":
                assert complete and complete[-1]["entries"] == complete[-1]["bytes"] == 0, (name, complete)
            phase_records = log_records(session, "CH_LIGHT_TREE_PHASES ")
            phase = phase_records[-1] if records and phase_records else None
            if require_parallel and cache in ("hit", "all-hit"):
                if os.environ.get("CYCLES_HOUDINI_SERIAL_LIGHT_TREE_FLATTEN") != "1":
                    assert (
                        phase
                        and phase["parallel_cached_nodes"] + phase.get("retained_cached_nodes", 0) >= 4096
                        and phase["parallel_cached_ranges"] + phase.get("retained_cached_ranges", 0) > 0
                    ), (name, phase)
            if require_swap:
                assert phase and phase["complete"] and phase["instance_swaps"] > 0, (name, phase)
            if camera_only:
                assert status["geometry_conversions"] == before["geometry_conversions"], (before, status)
                assert not any(
                    v["calls"]
                    for k, v in session.diagnostics()["buffer_copy_requests"].items()
                    if k in ("tri_verts", "prim_tri_verts", "tri_vindex", "tri_shader", "prim_tri_index")
                ), session.diagnostics()
            # The normal suite starts an independent uncached worker per state.
            # The dense suite keeps the uncached worker's edit history matched:
            # fresh displacement can round positions/normals differently even
            # with caching disabled. Both suites keep the same pixel tolerances.
            os.environ["CYCLES_HOUDINI_DISABLE_LIGHT_TREE_CACHE"] = "1"
            if reference_history:
                if reference_owner[0] is None:
                    reference_owner[0] = new_session(device)
                reference_session = reference_owner[0]
            else:
                reference_session = new_session(device)
            try:
                reference = reference_session.wait(reference_session.apply(update))
                assert not reference_session.status()["source_diagnostics"]
                log_records(reference_session)
            finally:
                if not reference_history:
                    reference_session.close(wait=True)
                os.environ.pop("CYCLES_HOUDINI_DISABLE_LIGHT_TREE_CACHE", None)
            reference_records = log_records(reference_session)
            if state["tree"]:
                assert any(not r["enabled"] for r in reference_records if "enabled" in r), reference_records
            assert not any(r.get("enabled", False) for r in reference_records), reference_records
            if not reference_history:
                assert reference_records[-1].get("retired") and reference_records[-1]["bytes"] == 0, reference_records
            differences = {}
            hashes = {}
            for plane in ("combined", "depth", "normal", "position"):
                a, b = pixels(actual, plane), pixels(reference, plane)
                assert np.isfinite(a).all() and np.isfinite(b).all(), plane
                differences[plane] = float(np.abs(a - b).max())
                hashes[plane] = hashlib.sha256(a.tobytes()).hexdigest()
                if not np.allclose(a, b, rtol=3e-4, atol=2e-5):
                    np.savez_compressed(
                        destination / f"comparison-failure-{len(rows):02d}-{plane}.npz", actual=a, reference=b
                    )
                assert np.allclose(a, b, rtol=3e-4, atol=2e-5), (name, plane, differences)
            x = round((0.75 / 4.5 + 0.5) * width)
            illumination = pixels(actual, "combined")[height // 2 - 2 : height // 2 + 2, x - 2 : x + 2, :3].mean(
                axis=(0, 1)
            )
            if not rows:
                assert illumination[1] > 0.005 and illumination[1] > illumination[0] * 2, illumination
            if write:
                output = destination / (("cpu" if device == "CPU" else "optix") + f"-frame-{int(hou.frame())}.exr")
                session.write_exr(output)
                assert output.is_file() and output.stat().st_size > 1024
            rows.append(
                dict(
                    name=name,
                    differences=differences,
                    receiver_rgb=illumination.tolist(),
                    cache=records,
                    phase=phase,
                    status=status,
                    diagnostics=session.diagnostics(),
                    image_hashes=hashes,
                )
            )
            path.write_text(json.dumps(report, indent=2))
            return status

        try:
            compare("Fresh emitter illuminates an independently positioned diffuse receiver", "miss")
            receiver_grid.parmTuple("t").set((0, 0, 0.05))
            compare("Receiver position edit retains every unchanged emitter hierarchy", "all-hit")
            receiver_grid.parmTuple("t").set((0, 0, 0))
            compare("Receiver position repair retains every unchanged emitter hierarchy", "all-hit")
            receiver_grid.setParms(dict(rows=4, cols=5))
            compare("Receiver topology edit repacks buffers without invalidating emitter source data", "all-hit")
            receiver_grid.setParms(dict(rows=2, cols=2))
            compare("Receiver topology repair retains emitter data at current kernel offsets", "all-hit")
            emitter.parm("tx").set(-0.64)
            compare("A transform reuses the unchanged mesh-local construction", "hit")
            camera.parm("tx").set(0.08)
            compare("Camera-only update retains geometry and correct illumination", camera_only=True)
            for translation, rotation, scale in (
                (-0.5, 17, (1, 1, 1)),
                (-0.7, -23, (-1, 1, 1)),
                (-0.6, 8, (1.1, 0.7, 1.25)),
            ):
                emitter.setParms(dict(tx=translation, rz=rotation))
                emitter.parmTuple("s").set(scale)
                compare("Current translation/rotation/negative/nonuniform scale uses retained local data", "hit")
            emitter.setParms(dict(tx=-0.65, rz=0))
            emitter.parmTuple("s").set((1, 1, 1))
            shape.parm("amplitude").set(0.15)
            compare("Changed vertex positions rebuild local construction", "miss")
            grid.parm("rows").set(base_rows + 2)
            compare("Changed connectivity rebuilds local construction", "miss")
            state["surface"] = material((0.8, 0.2, 0.1))
            compare("Changed emission estimates rebuild local construction", "miss")
            state["surface"] = material()
            compare("Emission repair restores the correct receiver color from its retained palette", "hit")
            state["source"] = copies
            conversions = session.status()["geometry_conversions"]
            status = compare("Four packed lights share one geometry and local hierarchy", "all-hit")
            assert status["geometry_conversions"] == conversions, status
            assert status["geometry_resources"] == 2 and status["object_resources"] == 5, status
            emitter.parm("tx").set(-0.5)
            compare("Packed transforms preserve sharing and use current instance bounds", "hit")
            targets.setParms(dict(rows=3, cols=5))
            compare("Fifteen instances retain one shared local hierarchy")
            emitter.parm("sx").set(-1)
            compare(
                "A different first packed instance uses the correct cached root and primitive offsets",
                "hit",
                require_swap=True,
            )
            emitter.parmTuple("s").set((-1.1, 0.7, 1.2))
            emitter.parm("rz").set(29)
            compare("Rotated nonuniform packed instances preserve independent receiver lighting", "hit")
            state["secondary"] = True
            compare("Distinct emitter topology and shader palette add a second independent local range", "miss")
            secondary.setParms(dict(tx=-0.2, ty=0.75, rz=-17))
            emitter.parm("tx").set(-0.6)
            compare("Two cached mesh ranges use current indices and their own emission estimates", "hit")
            secondary.parmTuple("s").set((-0.8, 1.2, 1.1))
            compare("Negative/nonuniform transforms on the second cache range retain correct lighting", "hit")
            main_rows, secondary_rows = grid.evalParm("rows"), secondary_grid.evalParm("rows")
            grid.parm("rows").set(main_rows + 3)
            compare("A cold first emitter preserves logical indices of the cached later emitter", "miss")
            if os.environ.get("CYCLES_HOUDINI_DISABLE_LIGHT_TREE_EMITTER_VIEWS") != "1":
                assert (
                    rows[-1]["phase"]["cached_source_emitters"] > 0 and rows[-1]["phase"]["dense_source_emitters"] > 16
                ), rows[-1]["phase"]
            secondary_grid.parm("rows").set(secondary_rows + 2)
            compare("A cold later emitter preserves logical indices after cached earlier triangles", "miss")
            if os.environ.get("CYCLES_HOUDINI_DISABLE_LIGHT_TREE_EMITTER_VIEWS") != "1":
                assert (
                    rows[-1]["phase"]["cached_source_emitters"] > 0 and rows[-1]["phase"]["dense_source_emitters"] > 16
                ), rows[-1]["phase"]
            emitter.parm("tx").set(emitter.evalParm("tx") + 0.01)
            compare("Both newly cached spans retain their original logical triangle order", "all-hit")
            grid.parm("rows").set(main_rows)
            secondary_grid.parm("rows").set(secondary_rows)
            compare("Mixed cached and cold source repairs preserve independent illumination", "miss")
            state["secondary"] = False
            conversions = session.status()["geometry_conversions"]
            # Cycles can tag surviving geometry during a structural repack.
            # Conservative engine invalidation may rebuild the local tree;
            # host geometry must stay retained and rendered correctly.
            deleted = compare("Deleting another emitter prunes its cache and preserves independent lighting")
            assert deleted["geometry_conversions"] == conversions, (conversions, deleted)
            emitter.parmTuple("s").set((1, 1, 1))
            emitter.parm("rz").set(0)
            targets.setParms(dict(rows=2, cols=2))
            for frame in range(1, 6):
                hou.setFrame(frame)
                compare(
                    "Five-frame animated packed prototype sequence writes correct images",
                    None if frame == 1 else "miss",
                    write=True,
                )
            state["source"] = shape
            state["subdiv"] = dict(type="catmull_clark", space="object", dicing_rate=0.12, max_level=3)
            compare("Render-time subdivision rebuilds the local hierarchy", "miss")
            state["surface"] = material(height=0.15)
            displaced = compare("True displacement invalidates the geometry revision", "miss")
            state["surface"] = material(height=0.35)
            changed = compare("A displacement-only shader edit rebuilds the local hierarchy", "miss")
            assert changed["displacement_rebuilds"] > displaced["displacement_rebuilds"], (changed, displaced)
            emitter.parm("tx").set(emitter.evalParm("tx") + 0.04)
            compare("Transform of an unchanged subdivided/displaced mesh uses retained local data", "hit")
            state["visible"] = 0
            compare("Disabling all rays retires the mesh-local cache", "empty")
            state["visible"] = 255
            compare("Restoring visibility rebuilds current local lighting", "miss")
            state["surface"] = material(kind="diffuse")
            compare("Removing emission releases the retained local hierarchy", "empty")
            state["surface"] = material()
            compare("Restoring emission after retirement reconstructs current local lighting", "miss")
            state["tree"] = False
            compare("Disabling the light tree follows the uncached distribution path")
            state["tree"] = True
            compare("Reenabling the light tree cannot restore data retained across disabled state", "miss")
            state["included"] = False
            compare("Removing the last mesh light releases its cache", "empty")
            state["included"] = True
            compare("Recreating an emitter cannot restore a retired mesh address", "miss")
        finally:
            unwinding = sys.exc_info()[0] is not None
            try:
                reference_session = reference_owner[0]
                reference_owner[0] = None
                if reference_session is not None:
                    reference_session.close(wait=True)
                    device_report["reference_closed"] = reference_session.status()
                    assert (
                        device_report["reference_closed"]["device_allocation_bytes"]
                        == device_report["reference_closed"]["owned_geometry_bytes"]
                        == 0
                    )
                    records = log_records(reference_session)
                    assert records[-1].get("retired") and records[-1]["bytes"] == 0, records
                session.close(wait=True)
                device_report["closed"] = session.status()
                device_report["cache_retirement"] = log_records(session)
                assert device_report["cache_retirement"] and device_report["cache_retirement"][-1].get("retired")
                assert device_report["cache_retirement"][-1]["bytes"] == 0
                assert session.status()["device_allocation_bytes"] == session.status()["owned_geometry_bytes"] == 0
            except BaseException:
                device_report["cleanup_error"] = traceback.format_exc()
                if not unwinding:
                    raise
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    if original_disable is not None:
        os.environ["CYCLES_HOUDINI_DISABLE_LIGHT_TREE_CACHE"] = original_disable
    else:
        os.environ.pop("CYCLES_HOUDINI_DISABLE_LIGHT_TREE_CACHE", None)
    if original_profile is None:
        os.environ.pop("CYCLES_HOUDINI_PROFILE_LIGHT_TREE", None)
    else:
        os.environ["CYCLES_HOUDINI_PROFILE_LIGHT_TREE"] = original_profile
    path.write_text(json.dumps(report, indent=2))
print(path)
