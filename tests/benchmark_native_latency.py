# SPDX-License-Identifier: Apache-2.0
"""Opt-in native latency benchmark; run exclusively, outside the smoke suite.

Measures publication of a full-resolution image, not host UI presentation.
Engine phase instrumentation and the matched harness comparison are separate.
"""

from support.paths import artifacts_root

from native_test_runtime import build_manifest
import argparse
import json
import math
import os
from pathlib import Path
import statistics
import struct
import time

import hou
import cycles_houdini as ch

parser = argparse.ArgumentParser()
parser.add_argument("--device", default=ch.default_device())
parser.add_argument("--repetitions", type=int, default=30)
parser.add_argument("--samples", type=int, default=128)
parser.add_argument("--rows", type=int, default=501)
parser.add_argument("--cols", type=int, default=1001)
parser.add_argument("--destination", type=Path)
parser.add_argument("--diagnostics", action="store_true")
parser.add_argument("--process-memory", action="store_true")
args = parser.parse_args()
if not 5 <= args.repetitions <= 200 or not 2 <= args.rows <= 5001 or not 2 <= args.cols <= 5001:
    raise ValueError("Use 5..200 repetitions and 2..5001 grid rows/columns")
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
destination = args.destination or artifacts_root() / "performance" / f"native-latency-{stamp}"
destination.mkdir(parents=True, exist_ok=False)
manifest = build_manifest()
(destination / "build-manifest.json").write_bytes(manifest.read_bytes())
hou.setFps(24)
hou.setFrame(1)
obj = hou.node("/obj").createNode("geo", "native_latency_fixture", run_init_scripts=False)
grid = obj.createNode("grid", "one_million_triangles")
grid.parm("type").set(0)
grid.parm("orient").set(0)
grid.parmTuple("size").set((4, 2.25))
grid.parm("rows").set(args.rows)
grid.parm("cols").set(args.cols)
camera = hou.node("/obj").createNode("cam", "latency_camera")
camera.parm("tz").set(5)
camera.parm("projection").set(1)
camera.parm("orthowidth").set(4)
camera.parm("near").set(0.01)
camera.parm("far").set(10000)
material = dict(key="surface", kind="diffuse", color=(0.6, 0.2, 0.1), roughness=0.0)
light = dict(
    key="key", kind="point", radius=0.0, strength=(500, 500, 500), transform=(1, 0, 0, -2, 0, 1, 0, 3, 0, 0, 1, 4)
)


def update():
    return dict(
        geometry=[ch.geometry_request(obj, grid, material="surface")],
        materials=[material],
        lights=[light],
        camera_path=camera.path(),
        background=(0.025, 0.025, 0.025),
    )


def quantile(values, probability):
    # Explicit nearest-rank percentile, not an unspecified library convention.
    return sorted(values)[max(0, math.ceil(len(values) * probability) - 1)]


records = []
memory_before = ch.process_memory() if args.process_memory else None
construct_started = time.perf_counter()
session = ch.Session(
    device=args.device, width=960, height=540, samples=args.samples, collect_diagnostics=args.diagnostics
)
construct_seconds = time.perf_counter() - construct_started
with session:

    def measure(workload, repeat):
        begin = time.perf_counter()
        generation = session.apply(update(), at_time=0)
        returned = time.perf_counter()
        status = session.status()
        while True:
            info = session.frame_info()
            if (
                info
                and info["generation"] == generation
                and info["rendered_width"] == 960
                and info["rendered_height"] == 540
            ):
                published = time.perf_counter()
                break
            status = session.status()
            if status["state"] == "failed":
                raise RuntimeError(status["error"])
            if time.perf_counter() - begin > 180:
                raise TimeoutError("No full-resolution image for native latency benchmark")
            time.sleep(0.001)
        # Complete a generation before the next edit, so different runs do not
        # inherit an arbitrary remaining sample workload/cancellation cost.
        while True:
            complete = session.frame_info()
            if complete and complete["generation"] == generation and complete["complete"]:
                completed_at = time.perf_counter()
                break
            status = session.status()
            if status["state"] == "failed":
                raise RuntimeError(status["error"])
            if time.perf_counter() - begin > 180:
                raise TimeoutError("Native latency generation did not complete")
            time.sleep(0.01)
        final = session.status()
        records.append(
            dict(
                workload=workload,
                repeat=repeat,
                generation=generation,
                host_apply_seconds=returned - begin,
                first_image_latency_seconds=published - begin,
                observed_complete_latency_seconds=completed_at - begin,
                frame=info,
                complete_frame=complete,
                status_at_apply=status,
                completed=final,
            )
        )
        if args.diagnostics:
            records[-1]["diagnostics"] = session.diagnostics()
        if args.process_memory:
            records[-1]["memory"] = ch.process_memory()
        (destination / "raw.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
        print(
            json.dumps(
                dict(
                    workload=workload,
                    repeat=repeat,
                    first_image_seconds=published - begin,
                    conversion_seconds=final["extraction_seconds"],
                )
            ),
            flush=True,
        )

    measure("cold", 0)
    full = session.frame()
    pixel = (270 * 960 + 480) * 4 * 4
    rgb = struct.unpack_from("fff", full["planes"]["combined"]["pixels"], pixel)
    if not all(math.isfinite(value) for value in rgb) or min(rgb) <= 0:
        raise AssertionError(f"Benchmark receiver is blank or invalid: {rgb}")
    # Warm up all three classes before recording their distributions.
    for workload in ("camera", "transform", "material"):
        camera.parm("tx").set(0.01 if workload == "camera" else 0)
        obj.parm("tx").set(0.01 if workload == "transform" else 0)
        material["color"] = (0.61, 0.2, 0.1) if workload == "material" else (0.6, 0.2, 0.1)
        measure(f"warmup-{workload}", 0)
    initial_conversions = session.status()["geometry_conversions"]
    for workload in ("camera", "transform", "material"):
        camera.parm("tx").set(0)
        obj.parm("tx").set(0)
        material["color"] = (0.6, 0.2, 0.1)
        measure(f"prepare-{workload}", 0)
        for repeat in range(args.repetitions):
            camera.parm("tx").set((0.02 if repeat % 2 else -0.02) if workload == "camera" else 0)
            obj.parm("tx").set((0.02 if repeat % 2 else -0.02) if workload == "transform" else 0)
            material["color"] = (
                (0.6 + (0.02 if repeat % 2 == 0 else 0), 0.2, 0.1) if workload == "material" else (0.6, 0.2, 0.1)
            )
            measure(workload, repeat)
            if session.status()["geometry_conversions"] != initial_conversions:
                raise AssertionError(f"{workload} unexpectedly reconverted geometry")
    final = session.status()
    session.write_exr(destination / "native-grid.exr")
assert session.status()["device_allocation_bytes"] == 0
memory_closed = ch.process_memory() if args.process_memory else None
distributions = {}
for workload in ("camera", "transform", "material"):
    values = [r["first_image_latency_seconds"] for r in records if r["workload"] == workload]
    distributions[workload] = dict(
        count=len(values),
        median_seconds=statistics.median(values),
        p95_seconds=quantile(values, 0.95),
        maximum_seconds=max(values),
        prospective_500ms_budget_met=quantile(values, 0.95) < 0.5,
    )
report = dict(
    result="measured",
    device=args.device,
    width=960,
    height=540,
    samples=args.samples,
    rows=args.rows,
    cols=args.cols,
    triangles=2 * (args.rows - 1) * (args.cols - 1),
    repetitions=args.repetitions,
    percentile="nearest rank",
    distributions=distributions,
    final=final,
    collect_diagnostics=args.diagnostics,
    process_memory=args.process_memory,
    memory_before=memory_before,
    memory_closed=memory_closed,
    isolated_edit_blocks=True,
    fixture=dict(
        size=(4, 2.25),
        orientation="xy",
        primitive="polygons",
        material_initial=dict(key="surface", kind="diffuse", color=(0.6, 0.2, 0.1), roughness=0.0),
        light=light,
        background=(0.025, 0.025, 0.025),
        ortho_width=4,
        camera_z=5,
        near=0.01,
        far=10000,
        passes=["combined", "depth", "normal", "uv", "position"],
        denoising=False,
        adaptive_sampling=False,
    ),
    session_construct_seconds=construct_seconds,
    cold_first_image_with_construct_seconds=construct_seconds + records[0]["first_image_latency_seconds"],
    scope="Host apply plus full-resolution core image publication; excludes UI presentation and file output. GPU upload and finer engine phases need separate counters. This is not full FX/10-percent certification.",
)
(destination / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
