# SPDX-License-Identifier: Apache-2.0
"""Calibrated own-process telemetry, renderer retirement and read-only queries."""

from support.paths import artifacts_root
import json
import os
from pathlib import Path
import time

import cycles_houdini as ch


def gpu_usage(snapshot):
    result = {}
    for adapter in snapshot["gpu"]["adapters"]:
        if "local" not in adapter or "current_usage_bytes" not in adapter["local"]:
            raise AssertionError(f"Unavailable DXGI counter on the certified configuration: {adapter}")
        result[adapter["luid"]] = adapter["local"]["current_usage_bytes"]
    return result


root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage1/memory"
destination.mkdir(parents=True, exist_ok=True)
baseline = ch.process_memory()
assert baseline["pid"] == os.getpid()
assert baseline["schema_version"] == 1
assert baseline["host"]["working_set_bytes"] > 0
assert baseline["host"]["private_commit_bytes"] > 0
assert baseline["host"]["peak_working_set_bytes"] >= baseline["host"]["working_set_bytes"]
baseline_gpu = gpu_usage(baseline)
assert baseline_gpu, "Certified workstation exposes no hardware adapters"

available = ch.devices()
gpu = [device for device in available if device["backend"] == "OPTIX" and device["name"] == "NVIDIA GeForce RTX 4090"]
assert len(gpu) == 1, "This telemetry certification requires the explicit RTX 4090"
adapter = [item for item in baseline["gpu"]["adapters"] if item["name"] == "NVIDIA GeForce RTX 4090"]
assert len(adapter) == 1
gpu_luid = adapter[0]["luid"]
records = []
for device in ("CPU", gpu[0]["id"]):
    for repetition in range(3):
        before = ch.process_memory()
        with ch.Session(device=device, width=96, height=54, samples=8) as session:
            generation = session.calibration()
            session.wait(generation)
            info = session.frame_info()
            status = session.status()
            active = [ch.process_memory() for _ in range(5)]
            assert all(snapshot["pid"] == os.getpid() for snapshot in active)
            assert session.frame_info() == info, "Telemetry modified a completed image event"
            assert session.status()["generation"] == generation
            assert session.status()["geometry_conversions"] == status["geometry_conversions"]
            usage = gpu_usage(active[-1])
            if device == "CPU":
                assert usage == gpu_usage(before), "CPU rendering allocated GPU segment memory"
            else:
                assert usage[gpu_luid] > gpu_usage(before)[gpu_luid] + 1024 * 1024, (
                    "Live GPU renderer is absent from DXGI process usage"
                )
        assert session.status()["device_allocation_bytes"] == 0
        deadline = time.monotonic() + 3
        # Driver release is allowed to complete asynchronously. This budget
        # concerns OS-reported local segment usage, not a physical VRAM claim.
        closed = ch.process_memory()
        while any(value > gpu_usage(before)[luid] + 16 * 1024 * 1024 for luid, value in gpu_usage(closed).items()):
            if time.monotonic() > deadline:
                raise AssertionError(
                    f"Closed renderer retained more than the 16-MiB GPU segment budget: {before}, {closed}"
                )
            time.sleep(0.05)
            closed = ch.process_memory()
        records.append(
            dict(
                device=device,
                repetition=repetition,
                before=before,
                active=active,
                closed=closed,
                tracked_after_close=session.status()["device_allocation_bytes"],
            )
        )

checks = [
    "pid",
    "schema",
    "host counters",
    "hardware adapters",
    "explicit GPU identity",
    "read-only frame",
    "read-only scene",
    "CPU GPU usage unchanged",
    "live GPU usage",
    "tracked retirement",
    "closed GPU segment budget",
]
report = dict(
    result="passed",
    baseline=baseline,
    records=records,
    checks=checks,
    scope="Three complete CPU and explicit RTX 4090 session lifecycles; read-only whole-process Windows/DXGI counters. "
    "Independent CUDA calibration is recorded separately. No exact residency, mixed-device or large FX plateau certification.",
)
(destination / "houdini-memory-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result="passed", checks=len(checks), lifecycles=len(records))))
