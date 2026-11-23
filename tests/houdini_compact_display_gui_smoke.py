# SPDX-License-Identifier: Apache-2.0
"""Owned odd-size compact passes through the installed GPU/CPU display routes."""

import json
import os
from pathlib import Path
import time
import traceback
import hou
import numpy as np
from PySide6 import QtCore, QtGui
from cycles_houdini import _native
from cycles_houdini.display_queue import DisplayQueue
from cycles_houdini.gpu_display import Converter
from cycles_houdini.panel import ImageCanvas

path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
began = time.monotonic()
owned = _native.compact_frame_fixture()
queue = DisplayQueue(Converter())
cases = [
    ("beauty", {}),
    ("noisy", {"pass": "noisy_combined"}),
    ("exposure", dict(exposure=1.5)),
    ("gamma", dict(gamma=1.4)),
    ("rgb", dict(channels="rgb")),
    ("alpha", dict(channels="alpha")),
    ("raw", dict(raw=True)),
    ("normal", {"pass": "normal"}),
    ("depth", {"pass": "depth"}),
    ("cryptomatte", {"pass": "crypto_object00"}),
]
cases[5:5] = [(token, dict(channels=token)) for token in ("r", "g", "b")]
report = dict(
    result="running",
    cases=[],
    scope="Different compact pass sizes, odd logical crop/aspect, CPU reference and actual OpenGL conversion; no renderer scene.",
)
index = 0
expected = None
timer = QtCore.QTimer(hou.qt.mainWindow())
timer.setInterval(10)


def finish(error=None):
    timer.stop()
    queue.close(wait=True)
    report.update(result="failed" if error else "passed", error=error, wall_seconds=time.monotonic() - began)
    path.write_text(json.dumps(report, indent=2))
    hou.ui.postEventCallback(lambda: hou.exit(1 if error else 0, suppress_save_prompt=True))


def tick():
    global index, expected
    try:
        if time.monotonic() - began > 40:
            raise TimeoutError("Compact display stalled")
        if expected is None:
            if index == len(cases):
                finish()
                return
            name, settings = cases[index]
            component = {"r": 0, "g": 1, "b": 2}.get(settings.get("channels"))
            expected = _native.display_image(
                owned, dict(settings, channels="rgb") if component is not None else settings
            )
            if component is not None:
                rgba = np.frombuffer(expected["rgba"], np.uint8).reshape(-1, 4).copy()
                value = rgba[:, component].copy()
                rgba[:, :3] = value[:, None]
                rgba[:, 3] = 255
                expected["rgba"] = rgba.tobytes()
            expected["render_revision"] = expected["generation"]
            queue.request((index,), owned, 7, 11, dict(settings, collect_diagnostics=True))
            return
        ready = queue.take()
        if ready is None:
            return
        _, actual, error = ready
        assert error is None, error
        diagnostic = actual.pop("diagnostics")
        wanted = "opengl" if index < 8 else "cpu"
        assert diagnostic["backend"] == wanted, (cases[index][0], diagnostic)
        a = np.frombuffer(actual["rgba"], np.uint8).astype(np.int16)
        b = np.frombuffer(expected["rgba"], np.uint8).astype(np.int16)
        difference = int(np.abs(a - b).max())
        assert difference <= (2 if wanted == "opengl" else 0)
        assert {k: v for k, v in actual.items() if k != "rgba"} == {k: v for k, v in expected.items() if k != "rgba"}
        image = ImageCanvas.merge_frame(None, actual)
        expected_tile = QtGui.QImage(expected["rgba"], 7, 5, 28, QtGui.QImage.Format_RGBA8888)
        for y in range(5):
            for x in range(7):
                pixel = image.pixelColor(2 + x, 11 - 3 - 5 + y)
                target = expected_tile.pixelColor(x, y)
                assert (
                    max(
                        abs(pixel.red() - target.red()),
                        abs(pixel.green() - target.green()),
                        abs(pixel.blue() - target.blue()),
                        abs(pixel.alpha() - target.alpha()),
                    )
                    <= 2
                )
        assert float(image.text("cycles:pixel_aspect")) == float(actual["pixel_aspect"])
        assert image.pixelColor(0, 0) == QtGui.QColor(28, 28, 28)
        report["cases"].append(dict(name=cases[index][0], maximum_byte_error=difference, diagnostics=diagnostic))
        index += 1
        expected = None
    except BaseException:
        finish(traceback.format_exc())


timer.timeout.connect(tick)
timer.start()
