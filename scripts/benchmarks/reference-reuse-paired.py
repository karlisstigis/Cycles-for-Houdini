# SPDX-License-Identifier: Apache-2.0
"""Paired public attribute/packed Blender reference benchmark on an artist fixture.

Each invocation is one cold process. Run each form/target three times against the
same pinned host, without rebuilding or changing fixture files between pairs.
"""

import argparse
import ctypes as ct
from ctypes import wintypes as wt
import hashlib
import json
import os
from pathlib import Path
from time import perf_counter

import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene, blend_worker, blender_assets
from cycles_houdini.device_selection import set_devices

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("--hip", required=True, type=Path)
p.add_argument("--blend", required=True, type=Path)
p.add_argument("--host", required=True, type=Path)
p.add_argument("--out", required=True, type=Path)
p.add_argument("--target", choices=("Shape", "wine_bottles_01"), required=True)
p.add_argument("--form", choices=("attribute", "packed"), required=True)
p.add_argument("--count", type=int, default=51294)
p.add_argument("--trial", type=int, default=1)
p.add_argument("--device", default="CPU")
p.add_argument("--capture-only", action="store_true")
p.add_argument("--reuse-sequence", action="store_true", help="Attribute frame-set reassignment and one new sample")
a = p.parse_args()
assert Path(ch.__file__).resolve().is_relative_to(a.host.resolve()), ch.__file__
try:
    hou.hipFile.load(str(a.hip), suppress_save_prompt=True)
except hou.LoadWarning:
    pass
hou.setFrame(1)
obj = hou.node("/obj/geo1") or hou.node("/obj/bottles")
w = obj.node("pointwrangle1")
scatter = obj.node("scatter1")
scatter.bypass(False)
w.setInput(0, scatter)
for name, value in (("forcetotal", 1), ("npts", a.count)):
    scatter.parm(name).deleteAllKeyframes()
    scatter.parm(name).set(value)
kind = "object" if a.target == "Shape" else "collection"
reference = a.blend.resolve().as_posix() + "#" + kind + "=" + a.target
w.parm("snippet").set("s@instance=" + json.dumps(reference) + "; f@index=1; matrix3 m=ident(); 3@transform=m*.2;")
output = w
if a.form == "packed":
    packed = obj.createNode("cycles::blender_reference", "paired_reference")
    packed.parm("frame").deleteAllKeyframes()
    packed.setParms(dict(file=a.blend.resolve().as_posix(), target_kind=kind, target=a.target, frame=1))
    # Point transforms and scale are identical; Copy to Points consumes s@instance
    # only as an ordinary unused attribute, so remove it for the packed path.
    remove = obj.createNode("attribdelete", "paired_remove_instance")
    remove.setInput(0, w)
    remove.parm("ptdel").set("instance index")
    output = obj.createNode("copytopoints", "paired_packed_copies")
    output.setInput(0, packed)
    output.setInput(1, remove)
    output.parm("pack").set(0)
output.setRenderFlag(True)
output.setDisplayFlag(True)
start = perf_counter()
output.cook(force=True)
cook = perf_counter() - start
assert len(w.geometry().points()) == a.count
rop = hou.node("/out/cycles_render") or hou.node("/out/cam_1")
device = a.device
if device == "OPTIX":
    device = next(x["id"] for x in ch.devices() if x["backend"] == "OPTIX" and "4090" in x["name"])
set_devices(rop, device)
rop.setParms(
    dict(
        source_mode="objects",
        objectroot="/obj",
        vobject=obj.path(),
        forceobject="",
        samples=1,
        ipr_samples=1,
        use_denoise=0,
        ipr_use_denoise=0,
        resx=216,
        resy=122,
        adaptive_sampling=0,
        ipr_adaptive_sampling=0,
        allowmotionblur=0,
        render_threads=4,
        seed=0,
        animate_seed=0,
    )
)
obj.setDisplayFlag(True)
rop.parm("forceobject").set(obj.path())
metrics = dict(evaluations=0, batches=0, evaluation_transfer_seconds=0.0, native_conversion_seconds=0.0, wire_bytes=0)
original_evaluate = blend_worker.evaluate_many
original_prepare = blender_assets._prepare_result
original_recv = blend_worker._Pipe.recv_bytes


def evaluate(source, executable, requests, *args, **kwargs):
    metrics["evaluations"] += len(requests)
    metrics["batches"] += 1
    started = perf_counter()
    try:
        return original_evaluate(source, executable, requests, *args, **kwargs)
    finally:
        metrics["evaluation_transfer_seconds"] += perf_counter() - started


def prepare(*args, **kwargs):
    started = perf_counter()
    try:
        return original_prepare(*args, **kwargs)
    finally:
        metrics["native_conversion_seconds"] += perf_counter() - started


def recv(*args, **kwargs):
    result = original_recv(*args, **kwargs)
    metrics["wire_bytes"] += len(result)
    return result


blend_worker.evaluate_many = evaluate
blender_assets._prepare_result = prepare
blend_worker._Pipe.recv_bytes = recv
report = dict(
    host=ch.__file__,
    target=a.target,
    form=a.form,
    count=a.count,
    trial=a.trial,
    device=device,
    sop_cook_seconds=cook,
    blend_sha256=hashlib.sha256(a.blend.read_bytes()).hexdigest(),
    hip_sha256=hashlib.sha256(a.hip.read_bytes()).hexdigest(),
    point_positions_sha256=hashlib.sha256(
        np.asarray(w.geometry().pointFloatAttribValues("P"), np.float32).tobytes()
    ).hexdigest(),
    houdini=hou.applicationVersionString(),
    rows=[],
)
a.out.parent.mkdir(parents=True, exist_ok=True)


def memory(pid):
    # Peak working set is maintained by Windows for the process lifetime, so
    # this includes short-lived cold allocation peaks between observations.
    class Counters(ct.Structure):
        _fields_ = [("cb", wt.DWORD), ("faults", wt.DWORD)] + [
            (key, ct.c_size_t)
            for key in (
                "peak_working_set",
                "working_set",
                "peak_paged_pool",
                "paged_pool",
                "peak_nonpaged_pool",
                "nonpaged_pool",
                "pagefile",
                "peak_pagefile",
                "private_usage",
            )
        ]

    kernel = ct.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel.OpenProcess.restype = wt.HANDLE
    kernel.K32GetProcessMemoryInfo.argtypes = [wt.HANDLE, ct.POINTER(Counters), wt.DWORD]
    kernel.K32GetProcessMemoryInfo.restype = wt.BOOL
    kernel.CloseHandle.argtypes = [wt.HANDLE]
    handle = kernel.OpenProcess(0x1010, False, pid)
    try:
        data = Counters()
        data.cb = ct.sizeof(data)
        if not handle or not kernel.K32GetProcessMemoryInfo(handle, ct.byref(data), data.cb):
            return dict(pid=pid, error=ct.get_last_error())
        return dict(
            pid=pid,
            peak_working_set_bytes=data.peak_working_set,
            working_set_bytes=data.working_set,
            sampled_private_commit_bytes=data.private_usage,
        )
    finally:
        if handle:
            kernel.CloseHandle(handle)


try:
    session = (
        None
        if a.capture_only
        else ch.Session(
            device=device,
            width=216,
            height=122,
            samples=1,
            interactive=False,
            execution="worker",
            threads=4,
            collect_diagnostics=True,
        )
    )
    sequence = [("cold", 1, [1]), ("unchanged", 1, [1]), ("houdini_time", 2, [1])]
    if a.reuse_sequence:
        assert a.form == "attribute"
        sequence = [
            ("cold", 1, [1, 15]),
            ("unchanged", 1, [1, 15]),
            ("houdini_time", 2, [1, 15]),
            ("reassign", 2, [15, 1]),
            ("one_new", 2, [1, 15, 20]),
            ("return", 2, [1, 15]),
        ]
    for label, frame, frames in sequence:
        hou.setFrame(frame)
        if a.reuse_sequence:
            w.parm("snippet").set(
                "s@instance="
                + json.dumps(reference)
                + "; int frames[]={"
                + ",".join(map(str, frames))
                + "}; f@index=frames[@ptnum%len(frames)]; matrix3 m=ident(); 3@transform=m*.2;"
            )
        before = dict(metrics)
        started = perf_counter()
        update = native_scene.capture(rop)
        assert update["geometry"], (obj.path(), rop.path(), update)
        captured = perf_counter()
        row = dict(
            label=label,
            capture_seconds=captured - started,
            metrics={key: value - before[key] for key, value in metrics.items()},
            cache=blender_assets.cache_diagnostics(workers=True),
            diagnostics=update["source_diagnostics"],
        )
        row["metrics"]["blender_evaluation_transfer_seconds"] = max(
            0.0, row["metrics"]["evaluation_transfer_seconds"] - row["metrics"]["native_conversion_seconds"]
        )
        row["memory"] = dict(
            parent=memory(os.getpid()),
            blender=[memory(worker.process.pid) for worker in blend_worker._workers.values()],
            scope="Separate OS process peaks; no aggregate RSS or per-process VRAM attribution",
        )
        if session is not None:
            applied = session.apply(update)
            submitted = perf_counter()
            image = session.wait(applied, timeout=240)
            finished = perf_counter()
            pixels = np.frombuffer(image["planes"]["combined"]["pixels"], np.float32)
            status = session.status()
            assert image["complete"] and not status["error"] and np.isfinite(pixels).all(), status
            row.update(
                apply_seconds=submitted - captured,
                render_wait_seconds=finished - submitted,
                first_complete_seconds=finished - started,
                status=status,
                renderer=session.diagnostics(),
                image_sha256=hashlib.sha256(pixels).hexdigest(),
                pixel_std=float(pixels.std()),
            )
            row["memory"]["parent"] = memory(os.getpid())
            row["memory"]["renderer"] = memory(status["renderer_pid"])
            host_status = ch._native.status(session._handle)
            row["host_instancing"] = {
                key: host_status[key]
                for key in (
                    "point_instance_syntheses",
                    "point_instance_cache_hits",
                    "point_instances_synthesized",
                    "point_instance_seconds",
                )
            }
        report["rows"].append(row)
        a.out.write_text(json.dumps(report, indent=2, default=str))
        print(json.dumps(dict(target=a.target, form=a.form, trial=a.trial, **row), default=str), flush=True)
    report["result"] = "passed"
finally:
    if session is not None:
        session.close()
    a.out.write_text(json.dumps(report, indent=2, default=str))
