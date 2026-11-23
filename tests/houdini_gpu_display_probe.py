# SPDX-License-Identifier: Apache-2.0
"""Independent OCIO CPU/GPU color and background-context feasibility check."""

import json
import os
from pathlib import Path
import runpy
import statistics
import threading
import time
import traceback

import hou
import numpy as np
import PyOpenColorIO as OCIO
from PySide6 import QtCore

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
helpers = runpy.run_path(str(root / "tests/gpu_color_reference.py"), run_name="gpu_reference")
path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
surface = helpers["make_surface"]()
report = dict(
    result="running",
    cases=[],
    scope="Isolated GPU color reference, not the installed panel. Float32 HDR/RGBA upload, generated OCIO shaders, RGBA8 readback on a background graphics context.",
)
finished = threading.Event()
gaps = []
previous = time.perf_counter()


def run():
    renderer = None
    try:
        renderer = helpers["GpuColorReference"](surface)
        rng = np.random.default_rng(39)
        pixels = np.empty((540, 960, 4), np.float32)
        pixels[:, :, :3] = np.exp2(rng.uniform(-12, 10, (540, 960, 3))).astype(np.float32)
        pixels[::5, :, :3] *= -0.1
        pixels[:, :, 3] = rng.choice(np.asarray([0.0, 0.01, 0.25, 0.5, 1.0], np.float32), (540, 960))
        # Keep nonzero additive RGB at zero coverage; otherwise premultiply.
        mask = pixels[:, :, 3] != 0
        pixels[mask, :3] *= pixels[mask, 3, None]
        config = OCIO.GetCurrentConfig()
        display = config.getDefaultDisplay()
        view = config.getDefaultView(display)
        report.update(
            ocio=OCIO.__version__, display=display, view=view, context_version=list(renderer.context.format().version())
        )
        for source in ("Linear Rec.709 (sRGB)", "ACEScg"):
            processor = config.getProcessor(source, display, view, OCIO.TRANSFORM_DIR_FORWARD)
            began = time.perf_counter()
            renderer.configure(processor)
            compile_seconds = time.perf_counter() - began
            for exposure, gamma in [(0.0, 1.0), (2.0, 1.0), (-2.0, 1.4)]:
                reference = pixels[:, :, :3].copy()
                reference[mask] /= pixels[mask, 3, None]
                reference *= 2.0**exposure
                cpu = processor.getDefaultCPUProcessor()
                cpu.apply(OCIO.PackedImageDesc(reference, 960, 540, 3))
                if gamma != 1:
                    reference = np.maximum(reference, 0.0) ** (1.0 / gamma)
                reference = np.floor(
                    np.clip(np.nan_to_num(reference, nan=0.0, posinf=0.0, neginf=0.0), 0, 1) * 255 + 0.5
                ).astype(np.uint8)
                expected_alpha = np.floor(pixels[:, :, 3] * 255 + 0.5).astype(np.uint8)
                samples = []
                for index in range(13):
                    began = time.perf_counter()
                    result = renderer.convert(pixels, exposure, gamma)
                    if index:
                        samples.append(time.perf_counter() - began)
                difference = np.abs(result[:, :, :3].astype(np.int16) - reference.astype(np.int16))
                case = dict(
                    source=source,
                    exposure=exposure,
                    gamma=gamma,
                    maximum_byte_error=int(difference.max()),
                    p999_byte_error=float(np.quantile(difference, 0.999)),
                    differing_channels=int(np.count_nonzero(difference)),
                    alpha_maximum_byte_error=int(np.abs(result[:, :, 3].astype(np.int16) - expected_alpha).max()),
                    compile_seconds=compile_seconds,
                    median_seconds=statistics.median(samples),
                    timings=samples,
                )
                report["cases"].append(case)
                assert case["maximum_byte_error"] <= 2 and case["p999_byte_error"] <= 1, case
                assert case["alpha_maximum_byte_error"] <= 1, case
        report["result"] = "passed"
    except BaseException:
        report.update(result="failed", error=traceback.format_exc())
    finally:
        try:
            if renderer:
                renderer.close()
        except BaseException:
            report.update(result="failed", cleanup_error=traceback.format_exc())
        finally:
            finished.set()


worker = threading.Thread(target=run, name="Cycles GPU color reference")
worker.start()
timer = QtCore.QTimer(hou.qt.mainWindow())
timer.setInterval(16)


def poll():
    global previous
    now = time.perf_counter()
    gaps.append(now - previous)
    previous = now
    if not finished.is_set():
        return
    timer.stop()
    worker.join()
    surface.destroy()
    report.update(heartbeat_maximum_seconds=max(gaps), heartbeat_median_seconds=statistics.median(gaps))
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    hou.ui.postEventCallback(lambda: hou.exit(0 if report["result"] == "passed" else 1, suppress_save_prompt=True))


timer.timeout.connect(poll)
timer.start()
