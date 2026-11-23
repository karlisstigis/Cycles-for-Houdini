# SPDX-License-Identifier: Apache-2.0
"""Actual owned-frame GPU conversion, CPU fallback and worker-thread cleanup."""

import json
import os
from pathlib import Path
import time
import traceback

import hou
import numpy as np
import PyOpenColorIO as OCIO
from PySide6 import QtCore
import cycles_houdini as ch
from cycles_houdini.display_queue import DisplayQueue
from cycles_houdini.gpu_display import Converter

path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
report = dict(
    result="running",
    cases=[],
    scope="Installed converter on retained frames after renderer closure; pixel comparison, fallback and explicit resource cleanup.",
)
with ch.Session(
    device="CPU", width=960, height=540, samples=1, interactive=False, passes=["combined", "normal", "diffuse_color"]
) as session:
    session.wait(session.calibration(), timeout=120)
    full = session.display_frame()
    session.wait(session.apply(dict(crop=(19, 23, 311, 179))), timeout=120)
    crop = session.display_frame()
assert session.status()["device_allocation_bytes"] == 0
queue = DisplayQueue(Converter())
cases = [
    ("color", full, {}),
    ("exposure", full, dict(exposure=2.0)),
    ("gamma", full, dict(gamma=1.4)),
    ("rgb", full, dict(channels="rgb")),
    ("crop", crop, {}),
    ("alpha", full, dict(channels="alpha")),
    ("raw", full, dict(raw=True)),
    ("normal", full, {"pass": "normal"}),
    ("lighting-rgb", full, {"pass": "diffuse_color"}),
]
ordinary_count = len(cases)
index = 0
expected = None
began = time.perf_counter()
submitted = 0.0
timings = []
old_config = OCIO.GetCurrentConfig()
timer = QtCore.QTimer(hou.qt.mainWindow())
timer.setInterval(16)


def finish(error=None):
    timer.stop()
    queue.close(wait=True)
    try:
        assert queue.cleanup_called and queue.cleanup_error is None, queue.cleanup_error
        assert queue.convert.context is None and queue.convert.released
    except Exception:
        error = traceback.format_exc()
    OCIO.SetCurrentConfig(old_config)
    report.update(result="failed" if error else "passed", error=error, wall_seconds=time.perf_counter() - began)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    hou.ui.postEventCallback(lambda: hou.exit(1 if error else 0, suppress_save_prompt=True))


def tick():
    global index, expected, submitted
    try:
        if time.perf_counter() - began > 90:
            raise TimeoutError("GPU display workflow stalled")
        if expected is None:
            if index == ordinary_count and len(cases) == ordinary_count:
                # Old source pixels must retain their meaning after changing
                # the active working role; the cross-policy route stays CPU.
                config = OCIO.Config.CreateFromStream(old_config.serialize())
                config.setWorkingDir(old_config.getWorkingDir())
                current = config.getColorSpace(OCIO.ROLE_SCENE_LINEAR).getName()
                config.setRole(OCIO.ROLE_SCENE_LINEAR, "ACEScg" if current != "ACEScg" else "Linear Rec.709 (sRGB)")
                OCIO.SetCurrentConfig(config)
                cases.append(("old-working-policy", full, {}))
            elif index >= len(cases):
                finish()
                return
            name, owned, settings = cases[index]
            expected = ch._native.display_image(owned[0], settings)
            expected["render_revision"] = expected["generation"]
            submitted = time.perf_counter()
            queue.request((index,), *owned, dict(settings, collect_diagnostics=True))
            return
        ready = queue.take()
        if ready is None:
            return
        _, actual, error = ready
        assert error is None, error
        diagnostic = actual.pop("diagnostics")
        a = np.frombuffer(actual.pop("rgba"), np.uint8).astype(np.int16)
        b = np.frombuffer(expected.pop("rgba"), np.uint8).astype(np.int16)
        assert actual == expected, (actual, expected)
        difference = int(np.abs(a - b).max())
        name = cases[index][0]
        wanted = "opengl" if name in ("color", "exposure", "gamma", "rgb", "crop", "lighting-rgb") else "cpu"
        assert diagnostic["backend"] == wanted, (name, diagnostic)
        assert difference <= (2 if wanted == "opengl" else 0), (name, difference)
        report["cases"].append(
            dict(
                name=name,
                maximum_byte_error=difference,
                diagnostics=diagnostic,
                submission_to_poll_seconds=time.perf_counter() - submitted,
            )
        )
        index += 1
        expected = None
    except BaseException:
        finish(traceback.format_exc())


timer.timeout.connect(tick)
timer.start()
