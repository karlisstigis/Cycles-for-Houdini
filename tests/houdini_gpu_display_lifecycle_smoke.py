# SPDX-License-Identifier: Apache-2.0
"""Owned GUI: graphics failure falls back and repeated converters release resources."""

import json
import os
from pathlib import Path
import time
import traceback

import hou
import numpy as np
import shiboken6
from PySide6 import QtCore
import cycles_houdini as ch
from cycles_houdini.display_queue import DisplayQueue
from cycles_houdini import gpu_display as gpu

path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
report = dict(
    result="running",
    cases=[],
    scope="Injected surface/draw failures, sticky CPU fallback, unused and repeated active converter closure.",
)
with ch.Session(device="CPU", width=960, height=540, samples=1, interactive=False) as session:
    session.wait(session.calibration(), timeout=120)
    owned = session.display_frame()
expected = ch._native.display_image(owned[0], {})
expected["render_revision"] = expected["generation"]
original_surface = gpu.make_surface
original_convert = gpu.GpuContext.convert
queue = None
retired = []
index = 0
requests = 0
submitted = False
injected_draws = 0
began = time.perf_counter()
cases = ["unused", "surface-failure", "draw-failure", "repeat-1", "repeat-2", "repeat-3", "repeat-4"]
timer = QtCore.QTimer(hou.qt.mainWindow())
timer.setInterval(16)


def fail_surface():
    raise RuntimeError("Injected offscreen surface failure")


def fail_draw(self, *args, **kwargs):
    global injected_draws
    original_convert(self, *args, **kwargs)  # Allocate and exercise actual GL resources first.
    injected_draws += 1
    raise RuntimeError("Injected display draw failure")


def retire():
    global queue
    queue.close(wait=True)
    assert queue.cleanup_called and queue.cleanup_error is None, queue.cleanup_error
    assert queue.thread is None or not queue.thread.is_alive()
    assert queue.convert.context is None
    if queue.convert.owner:
        assert queue.convert.released
        retired.append(queue.convert.owner)
    queue = None


def finish(error=None):
    timer.stop()
    gpu.make_surface = original_surface
    gpu.GpuContext.convert = original_convert
    if queue:
        try:
            retire()
        except Exception:
            error = (error or "") + "\nCleanup: " + traceback.format_exc()
    report.update(
        result="failed" if error else "passed",
        error=error,
        injected_draws=injected_draws,
        wall_seconds=time.perf_counter() - began,
    )
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    hou.ui.postEventCallback(lambda: hou.exit(1 if error else 0, suppress_save_prompt=True))


def tick():
    global queue, index, requests, submitted
    try:
        if time.perf_counter() - began > 60:
            raise TimeoutError("Display lifecycle workflow stalled")
        if queue is None:
            # Surface destruction is delivered on the GUI thread after worker closure.
            if any(shiboken6.isValid(owner) for owner in retired):
                return
            if index == len(cases):
                assert injected_draws == 1, "A failed graphics context was retried"
                finish()
                return
            name = cases[index]
            gpu.make_surface = fail_surface if name == "surface-failure" else original_surface
            gpu.GpuContext.convert = fail_draw if name == "draw-failure" else original_convert
            queue = DisplayQueue(gpu.Converter())
            gpu.make_surface = original_surface
            requests = 0
            submitted = False
            if name == "unused":
                retire()
                report["cases"].append(dict(name=name, result="passed"))
                index += 1
                return
        name = cases[index]
        if not submitted:
            queue.request((name, requests), *owned, dict(collect_diagnostics=True))
            submitted = True
            return
        ready = queue.take()
        if ready is None:
            return
        _, actual, error = ready
        assert error is None, error
        diagnostics = actual.pop("diagnostics")
        wanted = "cpu" if name.endswith("failure") else "opengl"
        assert diagnostics["backend"] == wanted, diagnostics
        difference = int(
            np.abs(
                np.frombuffer(actual["rgba"], np.uint8).astype(np.int16)
                - np.frombuffer(expected["rgba"], np.uint8).astype(np.int16)
            ).max()
        )
        assert difference <= (0 if wanted == "cpu" else 2), difference
        assert {k: v for k, v in actual.items() if k != "rgba"} == {k: v for k, v in expected.items() if k != "rgba"}
        if wanted == "cpu":
            assert "Injected" in diagnostics["fallback"], diagnostics
        report["cases"].append(
            dict(name=name, request=requests, maximum_byte_error=difference, diagnostics=diagnostics)
        )
        requests += 1
        submitted = False
        if requests == 2:
            retire()
            index += 1
    except BaseException:
        finish(traceback.format_exc())


timer.timeout.connect(tick)
timer.start()
