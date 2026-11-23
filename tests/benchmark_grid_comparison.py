# SPDX-License-Identifier: Apache-2.0
"""Opt-in, sequential matched-engine comparison; invoke through run-hython.ps1.

The grid is independently authored in C++ and Houdini. Planar equivalence does
not imply identical triangulation, BVH layout or pure path-tracing timings.
"""

from support.paths import artifacts_root

from native_test_runtime import build_directory, runtime_executable
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time

import numpy as np
import OpenImageIO as OIIO
import cycles_houdini as ch

parser = argparse.ArgumentParser()
parser.add_argument("--device", default=ch.default_device())
parser.add_argument("--rows", type=int, default=501)
parser.add_argument("--cols", type=int, default=1001)
parser.add_argument("--samples", type=int, default=128)
parser.add_argument("--repetitions", type=int, default=30)
parser.add_argument("--order", choices=("native-first", "harness-first"), default="harness-first")
parser.add_argument("--diagnostics", action="store_true")
parser.add_argument("--memory", action="store_true")
args = parser.parse_args()
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "performance" / ("grid-comparison-" + time.strftime("%Y%m%dT%H%M%S", time.gmtime()))
destination.mkdir(parents=True, exist_ok=False)
build = build_directory()
manifest = (build / "build-manifest.json").read_bytes()
(destination / "build-manifest.json").write_bytes(manifest)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_image(path):
    image = OIIO.ImageInput.open(str(path))
    if not image:
        raise RuntimeError(OIIO.geterror())
    try:
        spec = image.spec()
        if (spec.width, spec.height) != (960, 540):
            raise AssertionError(f"Unexpected benchmark image size: {spec.width}x{spec.height}")
        pixels = image.read_image(format=OIIO.FLOAT)
        if pixels is None or not np.isfinite(pixels).all():
            raise AssertionError("Benchmark image is missing or contains nonfinite values")
        return pixels, list(spec.channelnames)
    finally:
        image.close()


def compare_images():
    native, native_names = read_image(destination / "native/native-grid.exr")
    core, core_names = read_image(destination / "core/core-grid.exr")
    if set(native_names) != set(core_names):
        raise AssertionError(f"Pass channel sets differ: {native_names}, {core_names}")
    # Avoid silhouette filtering. Each data check also has an expectation that
    # is independent of both adapters: z=0, facing +Z, depth=5, alpha=1.
    interior = np.s_[8:-8, 8:-8]
    checks = {}
    for names, expected, tolerance in (
        (("position.Z",), 0.0, 1e-5),
        (("depth.Z",), 5.0, 1e-5),
        (("normal.X", "normal.Y"), 0.0, 1e-5),
        (("normal.Z",), 1.0, 1e-5),
        (("A",), 1.0, 1e-5),
    ):
        for name in names:
            for label, pixels, channel_names in (("native", native, native_names), ("core", core, core_names)):
                error = float(np.max(np.abs(pixels[interior][..., channel_names.index(name)] - expected)))
                if error > tolerance:
                    raise AssertionError(f"{label} {name} violates independent planar expectation: {error}")
                checks[label + "/" + name] = dict(expected=expected, maximum_absolute_error=error, tolerance=tolerance)
    differences = {}
    for name in native_names:
        a = native[interior][..., native_names.index(name)]
        b = core[interior][..., core_names.index(name)]
        error = np.abs(a - b)
        # Position passes can capture different subpixel jitter. Other passes
        # permit small single-precision traversal/filtering/sampling variation.
        tolerance = 0.005 if name in ("position.X", "position.Y") else 0.003
        maximum = float(np.max(error))
        if maximum > tolerance:
            raise AssertionError(f"Equivalent planar fixtures disagree in {name}: {maximum} > {tolerance}")
        differences[name] = dict(
            mean_absolute_error=float(np.mean(error)), maximum_absolute_error=maximum, tolerance=tolerance
        )
    if float(np.mean(native[interior][..., native_names.index("R")])) <= 0:
        raise AssertionError("Matched native beauty is blank")
    return dict(
        result="passed",
        independent_expectations=checks,
        interior_channel_differences=differences,
        excluded_border_pixels=8,
        scope="Planar fixture correctness; not a general scene-conversion proof",
    )


report = dict(
    result="running",
    device=args.device,
    order=args.order,
    collect_diagnostics=args.diagnostics,
    sample_memory=args.memory,
    fixture=dict(
        rows=args.rows,
        cols=args.cols,
        triangles=2 * (args.rows - 1) * (args.cols - 1),
        width=960,
        height=540,
        samples=args.samples,
        repetitions=args.repetitions,
    ),
    manifest_sha256=hashlib.sha256(manifest).hexdigest(),
    binary_sha256={
        str(path.relative_to(build)): sha256(path)
        for path in (runtime_executable(), Path(os.environ["CYCLES_HOUDINI_HOST_ROOT"]) / "dso/_cycles_houdini.dll")
    },
    test_sha256={
        name: sha256(root / "tests" / name) for name in ("benchmark_native_latency.py", "benchmark_grid_comparison.py")
    },
)
if args.memory:
    report["test_sha256"]["benchmark_memory.py"] = sha256(root / "tests/benchmark_memory.py")


def save():
    (destination / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


def summarize_process_memory(before, rows, closed):
    samples = [row["memory"] for row in rows]
    warm = [row["memory"] for row in rows if row["workload"] in ("camera", "transform", "material")]
    hosts = [sample["host"] for sample in samples if "working_set_bytes" in sample.get("host", {})]
    adapters = {}
    for sample in [before, *samples, closed]:
        for adapter in sample.get("gpu", {}).get("adapters", []):
            if "luid" in adapter:
                adapters.setdefault(adapter["luid"], dict(name=adapter["name"], node=adapter["node"]))

    def usage(snapshot, luid, segment):
        adapter = next((item for item in snapshot.get("gpu", {}).get("adapters", []) if item.get("luid") == luid), {})
        return adapter.get(segment, {}).get("current_usage_bytes")

    for luid, entry in adapters.items():
        for segment in ("local", "nonlocal"):
            values = [value for sample in samples if (value := usage(sample, luid, segment)) is not None]
            warm_values = [value for sample in warm if (value := usage(sample, luid, segment)) is not None]
            entry[segment] = dict(
                before_session_bytes=usage(before, luid, segment),
                peak_completed_generation_sample_bytes=max(values, default=None),
                warm_range_bytes=[min(warm_values), max(warm_values)] if warm_values else None,
                after_close_bytes=usage(closed, luid, segment),
            )
    return dict(
        samples=len(samples),
        adapters=adapters,
        peak_completed_generation_working_set_bytes=max((item["working_set_bytes"] for item in hosts), default=None),
        peak_completed_generation_private_commit_bytes=max(
            (item["private_commit_bytes"] for item in hosts), default=None
        ),
        before=before,
        closed=closed,
        scope="Whole calling process sampled at completed generation boundaries and immediately after session close. "
        "DXGI OS-reported GPU segment usage, not per-session allocation or exact physical residency. "
        "Misses intermediate upload/BVH/render peaks; includes benchmark/host/driver overhead.",
    )


save()
try:
    native_command = [
        sys.executable,
        str(root / "tests/benchmark_native_latency.py"),
        "--device",
        args.device,
        "--rows",
        str(args.rows),
        "--cols",
        str(args.cols),
        "--samples",
        str(args.samples),
        "--repetitions",
        str(args.repetitions),
        "--destination",
        str(destination / "native"),
    ]
    core_command = [
        str(runtime_executable()),
        "--resources",
        os.environ["CYCLES_HOUDINI_RESOURCES"],
        "--device",
        args.device,
        "--grid-benchmark",
        "--rows",
        str(args.rows),
        "--cols",
        str(args.cols),
        "--samples",
        str(args.samples),
        "--repetitions",
        str(args.repetitions),
        "--output",
        str(destination / "core"),
    ]
    if args.diagnostics:
        native_command.append("--diagnostics")
        core_command.append("--diagnostics")
    if args.memory:
        native_command.append("--process-memory")
        core_command.append("--process-memory")
    commands = [("native", native_command), ("core", core_command)]
    if args.order == "harness-first":
        commands.reverse()
    for label, command in commands:
        print(f"Running {label} exclusively: {args.rows}x{args.cols}, {args.samples} samples", flush=True)
        start = time.monotonic()
        with (
            (destination / (label + ".stdout.log")).open("w", encoding="utf-8") as stdout,
            (destination / (label + ".stderr.log")).open("w", encoding="utf-8") as stderr,
        ):
            run = subprocess.Popen(command, stdout=stdout, stderr=stderr)
            recorder = None
            try:
                if args.memory:
                    from benchmark_memory import MemoryRecorder

                    recorder = MemoryRecorder(run, destination / (label + ".memory.jsonl"))
                while True:
                    if time.monotonic() - start > 1200:
                        raise TimeoutError(label + " benchmark exceeded 1200 seconds")
                    try:
                        run.wait(timeout=0.1 if recorder else 1.0)
                        break
                    except subprocess.TimeoutExpired:
                        if recorder:
                            recorder.sample()
            finally:
                if run.poll() is None:
                    run.kill()
                    run.wait()
                if recorder:
                    report[label + "_memory"] = recorder.finish()
        report[label + "_process"] = dict(
            exit_code=run.returncode, wall_seconds=time.monotonic() - start, command=command
        )
        save()
        if run.returncode:
            raise RuntimeError(f"{label} benchmark failed with exit {run.returncode}; inspect its saved logs")
    if manifest != (build / "build-manifest.json").read_bytes():
        raise AssertionError("Build manifest changed during sequential benchmark")
    if any(sha256(build / name) != fingerprint for name, fingerprint in report["binary_sha256"].items()):
        raise AssertionError("A renderer binary changed during sequential benchmark")
    native = json.loads((destination / "native/summary.json").read_text(encoding="utf-8"))
    native_rows = json.loads((destination / "native/raw.json").read_text(encoding="utf-8"))
    core_events = [json.loads(line) for line in (destination / "core/core-grid-events.jsonl").read_text().splitlines()]
    fixture = core_events[0]
    if any(
        fixture[name] != value
        for name, value in (
            ("rows", args.rows),
            ("columns", args.cols),
            ("triangles", report["fixture"]["triangles"]),
            ("samples", args.samples),
            ("repetitions", args.repetitions),
        )
    ):
        raise AssertionError("Headless benchmark parameters do not match native parameters")
    core_rows = [event for event in core_events if event.get("event") == "render"]
    if len(native_rows) != len(core_rows) or len(core_rows) != 7 + 3 * args.repetitions:
        raise AssertionError("Matched generation counts differ")
    if not fixture["isolated_edit_blocks"] or not native["isolated_edit_blocks"]:
        raise AssertionError("Matched benchmark does not isolate each measured edit class")
    if args.diagnostics:
        triangle_names = ("tri_verts", "tri_vindex", "tri_shader", "optix temp vertex data", "optix temp index data")
        checks = {}
        for label, rows in (("native", native_rows), ("core", core_rows)):
            for workload in ("camera", "transform", "material"):
                snapshots = [row["diagnostics"] for row in rows if row["workload"] == workload]
                if any(
                    any(name in snapshot["buffer_copy_requests"] for name in triangle_names) for snapshot in snapshots
                ):
                    raise AssertionError(f"{label} {workload} submitted an unnecessary bulk triangle-buffer copy")
                if workload != "material":
                    if len({snapshot["material_builds"] for snapshot in snapshots}) != 1:
                        raise AssertionError(f"{label} {workload} rebuilt unchanged material graphs")
                    if any("svm_nodes" in snapshot["buffer_copy_requests"] for snapshot in snapshots):
                        raise AssertionError(f"{label} {workload} recopied unchanged shader nodes")
                elif len({snapshot["material_builds"] for snapshot in snapshots}) != args.repetitions:
                    raise AssertionError(f"{label} material edits did not rebuild changed graphs")
                if any(snapshot["light_updates"] != 1 or snapshot["background_builds"] != 1 for snapshot in snapshots):
                    raise AssertionError(f"{label} {workload} rebuilt unchanged lights or environment")
                checks[label + "/" + workload] = "passed"
        report["incremental_update_checks"] = checks
    report["images"] = compare_images()
    report["native_first_image_distributions"] = native["distributions"]
    comparisons = {}
    for workload in ("camera", "transform", "material"):
        host = [row["completed"]["elapsed_seconds"] for row in native_rows if row["workload"] == workload]
        headless = [row["completed_seconds"] for row in core_rows if row["workload"] == workload]
        ratio = statistics.median(host) / statistics.median(headless)
        comparisons[workload] = dict(
            count=len(host),
            native_core_median_seconds=statistics.median(host),
            headless_core_median_seconds=statistics.median(headless),
            median_ratio=ratio,
            within_prospective_10_percent_budget=ratio <= 1.1,
        )
    native_allocations = [row["completed"]["device_allocation_bytes"] for row in native_rows]
    core_allocations = [row["tracked_allocation_bytes"] for row in core_rows]
    if args.memory:
        core_events = [
            json.loads(line)
            for line in (destination / "core/core-grid-events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        core_before = next(row["memory_before"] for row in core_events if row.get("event") == "fixture")
        core_closed = next(row["memory"] for row in core_events if row.get("event") == "memory_closed")
        report["process_memory"] = dict(
            native=summarize_process_memory(native["memory_before"], native_rows, native["memory_closed"]),
            core=summarize_process_memory(core_before, core_rows, core_closed),
        )
    report.update(
        result="measured",
        comparisons=comparisons,
        native_geometry_conversions=native["final"]["geometry_conversions"],
        headless_geometry_conversions=core_rows[-1]["geometry_conversions"],
        tracked_allocation_ranges=dict(
            native=[min(native_allocations), max(native_allocations)],
            core=[min(core_allocations), max(core_allocations)],
        ),
        scope="Same binaries/engine build, device, settings and five full-quality passes. "
        "Equivalent independently authored planar grids may have different triangulation/BVH layouts. "
        "Completed-generation time includes engine preparation and output delivery; not isolated steady kernel time. "
        "Native first-image timing excludes viewer presentation. Driver/kernel caches are not cleared. "
        "Tracked allocations are not physical VRAM. These measurements do not certify all FX workloads or Redshift parity.",
    )
    save()
    print(
        json.dumps(dict(report=str(destination / "summary.json"), result=report["result"], comparisons=comparisons)),
        flush=True,
    )
except Exception as error:
    report.update(result="failed", error=str(error))
    save()
    raise
