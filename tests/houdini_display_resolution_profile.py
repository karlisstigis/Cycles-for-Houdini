# SPDX-License-Identifier: Apache-2.0
"""Installed display conversion and GUI image assembly at 540p, 1080p and 4K.

Owned calibration frames outlive renderer shutdown. Seven warm observations
per case; excludes scene cooking, live rendering and compositor presentation.
"""

import json
import os
from pathlib import Path
import statistics
import threading
import time
import traceback

import hou
import numpy as np
from PySide6 import QtCore
import cycles_houdini as ch
from cycles_houdini import panel as panel_module
from cycles_houdini.display_queue import DisplayQueue
from cycles_houdini.gpu_display import Converter


class Profile:
    sizes = [(960, 540), (1920, 1080), (3840, 2160)]
    cases = [
        ("view", {}),
        ("gamma_exposure", dict(gamma=1.4, exposure=0.75)),
        ("alpha", dict(channels="alpha")),
        ("normal", {"pass": "normal"}),
    ]

    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.device = os.environ["CYCLES_HOUDINI_GUI_DEVICE"]
        self.converter = getattr(panel_module, "CanvasConverter", Converter)()
        self.gui_thread = threading.get_ident()
        self.conversion_threads = set()
        self.conversion_seconds = []
        self.queue = DisplayQueue(self.convert)
        # Keep graphics-context cleanup on the conversion thread even though
        # this profiling wrapper, rather than Converter itself, is callable.
        self.queue.cleanup = self.converter.close
        self.image = None
        self.size_index = self.case_index = self.repetition = 0
        self.phase = "fixture"
        self.began = time.perf_counter()
        self.finished = False
        self.busy = False
        self.report = dict(
            result="running", scope=__doc__, device=self.device, converter=type(self.converter).__name__, cases=[]
        )
        self.timer = QtCore.QTimer(hou.qt.mainWindow())
        self.timer.setInterval(1)
        self.timer.timeout.connect(self.tick)
        self.timer.start()

    def convert(self, frame, settings):
        self.conversion_threads.add(threading.get_ident())
        started = time.perf_counter()
        result = self.converter(frame, settings)
        self.conversion_seconds.append(time.perf_counter() - started)
        return result

    def request(self):
        self.requested = time.perf_counter()
        self.queue.request(
            (self.size_index, self.case_index, self.repetition),
            *self.owned,
            dict(self.cases[self.case_index][1], collect_diagnostics=True),
        )
        self.phase = "waiting"

    def save(self):
        self.path.write_text(json.dumps(self.report, indent=2))

    def tick(self):
        if self.finished or self.busy:
            return
        self.busy = True
        try:
            assert time.perf_counter() - self.began < 280, (self.phase, self.size_index, self.case_index)
            if self.phase == "fixture":
                if self.size_index == len(self.sizes):
                    self.finish()
                    return
                width, height = self.sizes[self.size_index]
                # Fixture production is outside the display measurements.
                with ch.Session(
                    device=self.device,
                    width=width,
                    height=height,
                    samples=1,
                    interactive=False,
                    threads=4,
                    passes=["combined", "normal"],
                ) as session:
                    session.wait(session.calibration(), timeout=120)
                    self.owned = session.display_frame()
                self.fixture_session = session
                self.phase = "closing"
                return
            if self.phase == "closing":
                closed = self.fixture_session.status()
                if closed["state"] != "closed":
                    return
                assert closed["device_allocation_bytes"] == closed["owned_geometry_bytes"] == 0
                self.phase = "case"
            if self.phase == "case":
                if self.case_index == len(self.cases):
                    self.size_index += 1
                    self.case_index = 0
                    self.phase = "fixture"
                    return
                self.expected = ch._native.display_image(self.owned[0], self.cases[self.case_index][1])
                self.timings = []
                self.repetition = 0
                self.request()
                return
            ready = self.queue.take()
            if ready is None:
                return
            _, actual, error = ready
            assert error is None, error
            received = time.perf_counter()
            started = time.perf_counter()
            self.image = panel_module.ImageCanvas.merge_frame(self.image, actual)
            assembly_seconds = time.perf_counter() - started
            if self.repetition:
                self.timings.append(
                    dict(
                        queue_to_gui_seconds=received - self.requested,
                        conversion_seconds=self.conversion_seconds[-1],
                        gui_assembly_seconds=assembly_seconds,
                    )
                )
            self.repetition += 1
            if self.repetition <= 7:
                self.request()
                return
            expected = self.expected
            width, height = expected["width"], expected["height"]
            observed = np.ndarray((height, self.image.bytesPerLine() // 4, 4), np.uint8, buffer=self.image.constBits())[
                :, :width
            ]
            reference = np.frombuffer(expected["rgba"], np.uint8).reshape(height, width, 4)
            error = int(np.abs(observed.astype(np.int16) - reference.astype(np.int16)).max())
            backend = actual["diagnostics"]["backend"]
            assert error <= (2 if backend == "opengl" else 0), (self.sizes[self.size_index], self.case_index, error)
            assert self.gui_thread not in self.conversion_threads
            self.report["cases"].append(
                dict(
                    width=width,
                    height=height,
                    name=self.cases[self.case_index][0],
                    backend=backend,
                    maximum_byte_error=error,
                    rgba_bytes=width * height * 4,
                    timings=self.timings,
                    median_seconds={key: statistics.median(x[key] for x in self.timings) for key in self.timings[0]},
                    p95_seconds={
                        key: float(np.percentile([x[key] for x in self.timings], 95)) for key in self.timings[0]
                    },
                    last_diagnostics=actual["diagnostics"],
                )
            )
            self.case_index += 1
            self.phase = "case"
            self.save()
        except BaseException:
            self.finish(traceback.format_exc())
        finally:
            self.busy = False

    def finish(self, error=None):
        if self.finished:
            return
        self.finished = True
        self.timer.stop()
        self.queue.close(wait=True)
        assert not self.queue.thread or not self.queue.thread.is_alive()
        self.report.update(
            result="failed" if error else "passed", error=error, wall_seconds=time.perf_counter() - self.began
        )
        self.save()
        hou.ui.postEventCallback(lambda: hou.exit(1 if error else 0, suppress_save_prompt=True))


profile = Profile()
