# SPDX-License-Identifier: Apache-2.0
"""Render a saved HIP through the same ordered Cycles ROP used in Houdini."""

import argparse
import json
import math
from pathlib import Path
import sys
import time
import traceback

import hou
from . import final_render


def render_saved_hip(scene, rop_path, *, frame_range=None, output=None, fail_on_missing_inputs=None):
    scene = Path(scene).resolve()
    if not scene.is_file():
        raise FileNotFoundError("Scene file does not exist: " + str(scene))
    if frame_range is not None:
        start, end, step = frame_range
        if not all(math.isfinite(v) for v in frame_range) or step <= 0 or end < start:
            raise ValueError("Use a finite ascending frame range and a positive increment")
    # No snapshots, UI, background jobs or persistent-session state are required.
    # Loading the original file establishes its normal HIP/HIPNAME context.
    hou.hipFile.load(str(scene), suppress_save_prompt=True, ignore_load_warnings=True)
    rop = hou.node(rop_path)
    if not isinstance(rop, hou.RopNode) or rop.type().name() != "cycles_ipr":
        raise ValueError("Cycles render ROP not found: " + rop_path)
    if output is not None:
        rop.parm("picture").set(output)
    if fail_on_missing_inputs is not None:
        rop.parm("fail_on_missing_inputs").set(bool(fail_on_missing_inputs))
    if frame_range is None:
        frame_range = rop.parmTuple("f").eval() if rop.evalParm("trange") else (hou.frame(), hou.frame(), 1)
    start, end, step = frame_range
    if not all(math.isfinite(v) for v in frame_range) or step <= 0 or end < start:
        raise ValueError("Use a finite ascending frame range and a positive increment")
    expected = math.floor((end - start) / step + 1e-8) + 1
    began = time.monotonic()
    published = 0.0

    def progress(event, value):
        nonlocal published
        if event != "progress":
            return
        now = time.monotonic()
        if now - published < 0.5:
            return
        state = final_render._active.get(rop.sessionId())
        completed = state.completed_frames + state.skipped_frames if state else 0
        percentage = min(100.0, 100 * (completed + value[1]["progress"]) / expected)
        print(f"ALF_PROGRESS {percentage:.1f}%", flush=True)
        published = now

    with final_render.observe(progress):
        rop.render(frame_range=frame_range)
    summary = rop.cachedUserData(final_render._summary_key)
    if not summary or summary["error"] or summary["completed_frames"] + summary["skipped_frames"] != expected:
        raise RuntimeError("Render did not complete every requested frame: " + str(summary))
    print("ALF_PROGRESS 100%", flush=True)
    return dict(
        result="passed",
        scene=str(scene),
        rop=rop.path(),
        frame_range=list(frame_range),
        elapsed_seconds=time.monotonic() - began,
        summary=summary,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scene", help="Saved .hip, .hiplc or .hipnc scene")
    parser.add_argument("--rop", required=True, help="Cycles ROP path, for example /out/cycles1")
    parser.add_argument("--start", type=int)
    parser.add_argument("--end", type=int)
    parser.add_argument("--step", type=int, default=1)
    parser.add_argument("--output", help="Optional Output Picture override; supports Houdini variables")
    parser.add_argument(
        "--fail-on-missing-inputs",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Override the ROP missing-input policy for final farm output",
    )
    parser.add_argument("--report", type=Path, help="Write a JSON success/failure report")
    args = parser.parse_args(argv)
    if (args.start is None) != (args.end is None):
        parser.error("--start and --end must be supplied together")
    try:
        result = render_saved_hip(
            args.scene,
            args.rop,
            frame_range=(args.start, args.end, args.step) if args.start is not None else None,
            output=args.output,
            fail_on_missing_inputs=args.fail_on_missing_inputs,
        )
        code = 0
    except Exception as error:
        node = hou.node(args.rop)
        summary = node.cachedUserData(final_render._summary_key) if node is not None else None
        result = dict(result="failed", scene=str(args.scene), rop=args.rop, error=str(error), summary=summary)
        traceback.print_exc(file=sys.stderr)
        code = 1
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result), flush=True)
    return code
