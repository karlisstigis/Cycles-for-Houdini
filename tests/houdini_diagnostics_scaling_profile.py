# SPDX-License-Identifier: Apache-2.0
"""Measure how source diagnostics scale through status, transport and the panel.

Each case renders N separate meshes that lack UVs. The "recovered" material
needs UVs, so every mesh carries a local diagnostic; the "plain" control has
the same geometry and no diagnostics. After completion, steady-state polls are
sampled from the worker controller, plus host status() calls and the panel's
per-poll tooltip formatting. Observations only; no pass/fail budget.
"""

from support.paths import artifacts_root

import argparse
import json
import os
from pathlib import Path
import statistics
import time

import hou
import numpy as np
import OpenImageIO as OIIO
import cycles_houdini as ch

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
parser.add_argument("--counts", default="1,250,1000")
parser.add_argument("--materials", default="plain,recovered")
parser.add_argument("--wait-seconds", type=float, default=600.0)
args = parser.parse_args()
device = json.loads(args.device) if args.device.startswith("[") else args.device
counts = [int(value) for value in args.counts.split(",")]
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
dest = artifacts_root() / "stage1/diagnostics-scaling"
dest.mkdir(parents=True, exist_ok=True)
texture = dest / ("color-" + str(os.getpid()) + ".exr")
pixels = np.full((4, 4, 3), 0.5, np.float32)
writer = OIIO.ImageOutput.create(str(texture))
writer.open(str(texture), OIIO.ImageSpec(4, 4, 3, OIIO.FLOAT))
writer.write_image(pixels)
writer.close()
cam = hou.node("/obj").createNode("cam", "scaling_camera")
cam.setParms(dict(tz=40, resx=320, resy=180))
recovered = dict(
    key="surface",
    kind="graph",
    graph=dict(
        nodes=[
            dict(
                id="tex", definition="cycles/image_texture/1", parameters=dict(filename=str(texture), colorspace="data")
            ),
            dict(id="bsdf", definition="cycles/diffuse_bsdf/1", parameters={}),
        ],
        links=[{"from": dict(node="tex", socket="color"), "to": dict(node="bsdf", socket="color")}],
        surface=dict(node="bsdf", socket="BSDF"),
    ),
)
plain = dict(key="surface", kind="diffuse", color=(0.5, 0.5, 0.5))
objects = []


def ensure(count):
    while len(objects) < count:
        index = len(objects)
        obj = hou.node("/obj").createNode("geo", f"piece_{index}", run_init_scripts=False)
        grid = obj.createNode("grid")
        grid.setParms(dict(type=0, orient=0, rows=3, cols=3))
        grid.parmTuple("size").set((0.4, 0.4))
        grid.setRenderFlag(True)
        obj.setParms(dict(tx=(index % 40) - 20, ty=(index // 40) - 12))
        objects.append((obj, grid))


def percentile(values, q):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))]


def measure(count, material):
    ensure(count)
    with ch.Session(
        device=device,
        width=320,
        height=180,
        samples=1,
        interactive=True,
        execution="worker",
        use_texture_cache=False,
        passes=["combined"],
    ) as session:
        geometry = [
            ch.geometry_request(obj, grid, material="surface", material_mode="uniform") for obj, grid in objects[:count]
        ]
        submitted = time.monotonic()
        target = session.apply(
            dict(
                geometry=geometry,
                materials=[material],
                camera_path=cam.path(),
                background=(0, 0, 0),
                rendering=dict(samples=1, adaptive_sampling=False),
            )
        )
        # Timeline instead of a bare wait: when completion stalls, record which
        # condition is outstanding and how long each host query takes.
        timeline, next_sample = [], 0.0
        while True:
            now = time.monotonic() - submitted
            started = time.perf_counter()
            status = session.status()
            status_seconds = time.perf_counter() - started
            started = time.perf_counter()
            frame = session.frame_info()
            frame_seconds = time.perf_counter() - started
            done = (
                frame
                and frame["generation"] == target
                and frame["complete"]
                and frame.get("current_scene", True)
                and not status.get("pending_update")
                and not status.get("capture_pending")
            )
            if now >= next_sample or done:
                profile = getattr(session._controller.backend, "last_exchange_profile", None) or {}
                events = session._controller.events
                timeline.append(
                    dict(
                        seconds=round(now, 3),
                        state=status["state"],
                        generation=status["generation"],
                        committed=status.get("committed_generation"),
                        pending_update=status.get("pending_update"),
                        capture_pending=status.get("capture_pending"),
                        diagnostics=len(status.get("source_diagnostics", [])),
                        frame=dict(
                            generation=frame["generation"],
                            complete=frame["complete"],
                            current=frame.get("current_scene"),
                        )
                        if frame
                        else None,
                        host_status_ms=round(1e3 * status_seconds, 3),
                        host_frame_ms=round(1e3 * frame_seconds, 3),
                        last_exchange=dict(
                            command=profile.get("command"),
                            ms=round(1e3 * profile.get("exchange_seconds", 0), 3),
                            output_bytes=profile.get("output_bytes"),
                        ),
                        last_apply_seconds=events[-1]["apply_seconds"] if events else None,
                    )
                )
                print("TRACE", count, json.dumps(timeline[-1]), flush=True)
                next_sample = now + 1.0
            if done or status["state"] == "failed" or now > args.wait_seconds:
                break
            time.sleep(0.01)
        assert done, timeline[-3:]
        backend = session._controller.backend
        polls, seen, end = [], set(), time.monotonic() + 3.0
        while time.monotonic() < end:
            profile = backend.last_exchange_profile
            if profile and profile["command"] == 3 and profile["request"] not in seen:
                seen.add(profile["request"])
                polls.append(profile)
            time.sleep(0.001)
        host = []
        for _ in range(60):
            started = time.perf_counter()
            status = session.status()
            host.append(time.perf_counter() - started)
        issues = status.get("source_diagnostics", [])
        tooltip = []
        for _ in range(60):
            started = time.perf_counter()
            text = "\n\n".join(f"{i['source']}\n{i['message']}\n{i['action']}" for i in issues)
            text += "\n\n".join(f"{i['source']}\n{i['message']}\n{i['action']}" for i in issues)
            tooltip.append(time.perf_counter() - started)
        exchange = [p["exchange_seconds"] for p in polls]
        return dict(
            count=count,
            material="recovered" if material is recovered else "plain",
            diagnostics=len(issues),
            completion_seconds=timeline[-1]["seconds"],
            timeline=timeline,
            polls=len(polls),
            poll_exchange_ms=dict(median=1e3 * statistics.median(exchange), p95=1e3 * percentile(exchange, 0.95))
            if exchange
            else None,
            poll_reply_bytes=statistics.median(p["output_bytes"] for p in polls) if polls else None,
            host_status_ms=dict(median=1e3 * statistics.median(host), p95=1e3 * percentile(host, 0.95)),
            panel_tooltip_ms=dict(median=1e3 * statistics.median(tooltip), p95=1e3 * percentile(tooltip, 0.95)),
        )


cases = []
selected = [dict(plain=plain, recovered=recovered)[name] for name in args.materials.split(",")]
for count in counts:
    for material in selected:
        cases.append(measure(count, material))
        print(json.dumps(cases[-1]), flush=True)
report = dict(result="passed", device=device, scope=__doc__, cases=cases)
(dest / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
