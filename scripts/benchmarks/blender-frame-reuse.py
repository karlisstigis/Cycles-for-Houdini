# SPDX-License-Identifier: Apache-2.0
"""Measure Blender instance updates in an isolated Houdini package."""

import argparse
import hashlib
import json
from pathlib import Path
import threading
from time import perf_counter
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene, blend_worker, object_properties
from cycles_houdini.device_selection import set_devices

parser = argparse.ArgumentParser()
for name in ("device", "out", "host", "hip"):
    parser.add_argument("--" + name, required=True)
parser.add_argument("--asset", choices=("dense", "shape"), required=True)
parser.add_argument("--dense", help="Blend file containing the Dense collection")
parser.add_argument("--instrumented", action="store_true")
parser.add_argument(
    "--probe-frames",
    nargs="+",
    type=int,
    metavar="FRAME",
    help="Request these Blender frames in order on every instance for per-array profiling",
)
args = parser.parse_args()
if args.asset == "dense" and not args.dense:
    parser.error("--dense is required for the dense collection")
out = Path(args.out)
out.mkdir(exist_ok=True, parents=True)
assert Path(ch.__file__).resolve().is_relative_to(Path(args.host).resolve())
hip = Path(args.hip).resolve()
try:
    hou.hipFile.load(str(hip), suppress_save_prompt=True)
except hou.LoadWarning:
    pass
hou.setFrame(1)
w = hou.node("/obj/geo1/pointwrangle1")
rop = hou.node("/out/cycles_render")
ref = (
    (hip.parent / "assets/Instance_Source.blend").as_posix() + "#object=Shape"
    if args.asset == "shape"
    else Path(args.dense).resolve().as_posix() + "#collection=Dense"
)
for p in ("testseed", "testnewframe"):
    w.addSpareParmTuple(hou.IntParmTemplate(p, p, 1))
if args.probe_frames:
    choose_frame = "f@index = chi('testnewframe');"
else:
    choose_frame = """int frames[] = {0,15,20,25,30};
f@index = frames[floor(rand(@ptnum + chi('testseed'))*len(frames))];
if (chi('testnewframe') && @ptnum == 0) f@index = 18;"""
w.parm("snippet").set(f"s@instance = {json.dumps(ref)};\n{choose_frame}\nmatrix3 m = ident(); 3@transform = m * .8;")
object_properties.install(w.parent())
w.parent().parm("cycles_blender_subdivision").set(1)
device = args.device
set_devices(rop, device)
rop.setParms(
    dict(
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
from cycles_houdini import blender_assets

pipe_time = header_time = convert_time = 0.0
total_received = 0
receive = blend_worker._Pipe.recv_bytes
read_context = threading.local()
recv_json = blend_worker._recv_json
recv_result = blend_worker._recv_result


def read_header(*args, **kwargs):
    read_context.header = True
    try:
        return recv_json(*args, **kwargs)
    finally:
        read_context.header = False


def counted(*args, **kwargs):
    global total_received, pipe_time, header_time
    start = perf_counter()
    result = receive(*args, **kwargs)
    duration = perf_counter() - start
    pipe_time += duration
    if getattr(read_context, "header", False):
        header_time += duration
        read_context.header = False
    total_received += len(result)
    return result


def received_result(*args, **kwargs):
    # The profiled worker reads its header directly to separate I/O and JSON decode.
    read_context.header = True
    try:
        return recv_result(*args, **kwargs)
    finally:
        read_context.header = False


prepare_result = blender_assets._prepare_result


def converted(*args, **kwargs):
    global convert_time
    start = perf_counter()
    try:
        return prepare_result(*args, **kwargs)
    finally:
        convert_time += perf_counter() - start


if args.instrumented:
    blend_worker._recv_json = read_header
    blend_worker._recv_result = received_result
    blend_worker._Pipe.recv_bytes = counted
    blender_assets._prepare_result = converted
rows = []
started = perf_counter()
with ch.Session(
    device=device,
    width=216,
    height=122,
    samples=1,
    interactive=False,
    execution="worker",
    threads=4,
    collect_diagnostics=args.instrumented,
) as session:
    edits = [("cold", 0, 0), ("reseed", 1, 0), ("new18", 1, 1), ("return", 0, 0)]
    if args.probe_frames:
        edits = [(f"probe-{i}-{frame}", 0, frame) for i, frame in enumerate(args.probe_frames)]
    for index, (label, seed, extra) in enumerate(edits):
        edit = started if index == 0 else perf_counter()
        w.parm("testseed").set(seed)
        w.parm("testnewframe").set(extra)
        start = perf_counter()
        before = total_received
        pipe_before, header_before, convert_before = pipe_time, header_time, convert_time
        update = native_scene.capture(rop)
        captured = perf_counter()
        frame = session.wait(session.apply(update), timeout=240)
        finish = perf_counter()
        diag = session.diagnostics() if args.instrumented else {}
        status = session.status()
        assert not status["error"] and frame["complete"] and status["current_sample"] == 1, status
        assert not any(
            "unavailable" in d["code"] or d.get("severity") == "error" for d in status["source_diagnostics"]
        ), status["source_diagnostics"]
        assert status["object_resources"] >= (10 if args.asset == "shape" else 30), status
        pixels = np.frombuffer(frame["planes"]["combined"]["pixels"], dtype=np.float32)
        assert np.isfinite(pixels).all() and pixels.std() > 0.05
        np.save(out / (label + ".npy"), pixels)
        rows.append(
            dict(
                label=label,
                capture=captured - start,
                first=finish - edit,
                wire_bytes=total_received - before if args.instrumented else None,
                pipe=pipe_time - pipe_before if args.instrumented else None,
                header_wait=header_time - header_before if args.instrumented else None,
                convert=convert_time - convert_before if args.instrumented else None,
                cycles=diag.get("core_update_seconds"),
                engine=diag.get("engine_update_seconds"),
                image_sha256=hashlib.sha256(pixels).hexdigest(),
                objects=status["object_resources"],
                conversions=status["geometry_conversions"],
            )
        )
        print(rows[-1], flush=True)
(out / "result.json").write_text(json.dumps(dict(host=ch.__file__, device=device, rows=rows), indent=2))
