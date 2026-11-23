# SPDX-License-Identifier: Apache-2.0
"""Actual Houdini viewer paints without its polling timer; retired pulses are safe."""

from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import os
from pathlib import Path
import threading
import time
import traceback

import hou
import shiboken6
from PySide6 import QtCore
from cycles_houdini import native_scene, registry
from cycles_houdini.panel import RenderPanel, ImageCanvas
from cycles_houdini.display_queue import DisplayQueue
from cycles_houdini.viewer_wakeup import ViewerWakeup

path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
report = dict(
    result="running",
    checks=[],
    scope="One actual Cycles viewer, its 16 ms poll timer disabled; real native parameter edits and display conversion.",
)
main_thread = threading.get_ident()
started = time.monotonic()
stage = "coalesced"
in_delivery = False
present_original = RenderPanel.present_available
capture_original = native_scene.capture
merge_original = ImageCanvas.merge_frame
request_original = DisplayQueue.request
merge_threads = []
present_threads = []
captures = []
panel = None
probe = None
producer = None
request_threads = []
release = threading.Event()
entered = threading.Event()
stale_generation = None


def present(self):
    global in_delivery
    assert threading.get_ident() == main_thread
    present_threads.append(threading.get_ident())
    in_delivery = True
    try:
        return present_original(self)
    finally:
        in_delivery = False


def capture(*args, **kwargs):
    assert not in_delivery, "Image-ready notification entered Houdini scene capture"
    captures.append(time.monotonic())
    return capture_original(*args, **kwargs)


def merge(image, frame):
    assert threading.get_ident() == main_thread
    if stale_generation is not None:
        assert frame["generation"] > stale_generation, "Retired conversion replaced the current image"
    merge_threads.append(threading.get_ident())
    return merge_original(image, frame)


def request(*args, **kwargs):
    request_threads.append(threading.get_ident())
    return request_original(*args, **kwargs)


RenderPanel.present_available = present
native_scene.capture = capture
ImageCanvas.merge_frame = staticmethod(merge)
DisplayQueue.request = request


class Probe(QtCore.QObject):
    def __init__(self):
        super().__init__()
        self.count = 0
        self.wakeup = ViewerWakeup(self.receive)

    @QtCore.Slot()
    def receive(self):
        assert threading.get_ident() == main_thread
        self.count += 1


probe = Probe()


def burst():
    for _ in range(50000):
        probe.wakeup.notify()


producer = threading.Thread(target=burst)
producer.start()
producer.join(5)
assert not producer.is_alive() and probe.count == 0 and probe.wakeup.pending
timer = QtCore.QTimer(hou.qt.mainWindow())
timer.setInterval(10)


def finish(error=None):
    global panel
    timer.stop()
    release.set()
    if panel:
        panel.dispose()
        panel.close()
        panel = None
    if probe and shiboken6.isValid(probe):
        probe.wakeup.close()
        probe.deleteLater()
    if producer:
        producer.join(5)
    registry.close_all()
    RenderPanel.present_available = present_original
    native_scene.capture = capture_original
    ImageCanvas.merge_frame = staticmethod(merge_original)
    DisplayQueue.request = request_original
    report.update(
        result="failed" if error else "passed",
        error=error,
        merge_count=len(merge_threads),
        present_count=len(present_threads),
        capture_count=len(captures),
        wall_seconds=time.monotonic() - started,
    )
    report["producer_request_count"] = sum(value != main_thread for value in request_threads)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    hou.ui.postEventCallback(lambda: hou.exit(1 if error else 0, suppress_save_prompt=True))


def tick():
    global stage, panel, probe, producer, stale_generation, old_image, before, conversion_original, camera, rop
    try:
        if time.monotonic() - started > 85:
            raise TimeoutError("Viewer notification workflow stalled at " + stage)
        if stage == "coalesced":
            if probe.count != 1:
                return
            assert not probe.wakeup.pending
            report["checks"].append("50,000 producer notifications coalesce to one GUI delivery")
            retained = probe.wakeup
            retained.notify()
            retained.close()
            probe.deleteLater()
            producer = threading.Thread(target=lambda: [retained.notify() for _ in range(50000)])
            producer.start()
            stage = "destroyed"
            return
        if stage == "destroyed":
            if shiboken6.isValid(probe) or producer.is_alive():
                return
            assert retained_closed(probe)
            report["checks"].append("Already queued and late producer pulses are harmless after receiver deletion")
            geo = hou.node("/obj").createNode("geo", "wakeup_pig", run_init_scripts=False)
            pig = geo.createNode("testgeometry_pighead")
            pig.setRenderFlag(True)
            pig.setDisplayFlag(True)
            camera = hou.node("/obj").createNode("cam", "wakeup_camera")
            camera.parm("tz").set(5)
            rop = hou.node("/out").createNode("cycles_ipr", "wakeup_render")
            set_rop_devices(rop, os.environ.get("CYCLES_HOUDINI_GUI_DEVICE", "CPU"))
            rop.setParms(
                dict(
                    vobject=geo.name(),
                    camera=camera.path(),
                    resx=320,
                    resy=180,
                    samples=65536,
                    ipr_samples=65536,
                    adaptive_sampling=0,
                    ipr_adaptive_sampling=0,
                    use_denoise=0,
                    ipr_use_denoise=0,
                )
            )
            panel = RenderPanel()
            panel.resize(900, 650)
            panel.set_rop(rop)
            panel.show()
            panel.render()
            panel.timer.stop()
            stage = "initial"
            return
        assert not panel.timer.isActive(), "Polling fallback was accidentally enabled"
        if stage == "closed":
            assert panel._disposed and panel._frame_wakeup.closed and panel._frame_session is None
            assert not panel._display_queue.thread.is_alive() or panel._display_queue.closing
            report["checks"].append("Viewer disposal unregisters producers and makes queued or late wakeups inert")
            finish()
            return
        status = panel.entry.session.status()
        if status["state"] == "failed":
            raise RuntimeError(status["error"])
        metadata = getattr(panel.canvas, "frame_metadata", {})
        current = bool(
            metadata
            and metadata["generation"] == status["committed_generation"]
            and metadata["render_revision"] == status["render_revision"]
            and not status.get("pending_update")
        )
        if stage == "initial" and current and not panel.canvas.image.isNull():
            assert merge_threads and present_threads
            report["checks"].append("Initial render reaches the actual canvas with the viewer polling timer stopped")
            old_image = panel.canvas.image.copy()
            before = status["generation"]
            panel.exposure.setValue(1.5)
            stage = "exposure"
            return
        if stage == "exposure" and not panel._settings_dirty and panel.canvas.image != old_image:
            assert status["generation"] == before
            report["checks"].append("Exposure updates the displayed pixels without polling or restarting rendering")
            conversion_original = panel._display_queue.convert

            def held(frame, settings):
                if not entered.is_set():
                    entered.set()
                    assert release.wait(10), "Held display conversion was not released"
                return conversion_original(frame, settings)

            panel._display_queue.convert = held
            panel.gamma.setValue(1.3)
            stage = "held"
            return
        if stage == "held" and entered.is_set():
            before = status["generation"]
            camera.parm("tx").set(0.3)
            panel.queue_render()
            stage = "new-scene"
            return
        if stage == "new-scene" and status["committed_generation"] > before:
            stale_generation = before
            release.set()
            stage = "repaired"
            return
        if stage == "repaired" and current and metadata["generation"] > before:
            report["checks"].append(
                "Camera edits progress during held conversion; superseded pixels never replace the accepted scene"
            )
            assert len(merge_threads) > 2 and set(merge_threads) == set(present_threads) == {main_thread}
            report["checks"].append(
                "Image assembly and painting stay on the GUI; image notifications never enter scene capture"
            )
            assert any(value != main_thread for value in request_threads)
            report["checks"].append(
                "Renderer controller submits owned frames for conversion without waiting for the GUI"
            )
            panel.grab().save(str(path.with_suffix(".panel.png")))
            # A completion arriving after disposal must not retain or revive the
            # QWidget. Exercise the actual panel's registered subscription.
            retained = panel._frame_wakeup
            retained.notify()
            began_close = time.monotonic()
            panel.dispose()
            report["dispose_seconds"] = time.monotonic() - began_close
            producer = threading.Thread(target=lambda: [retained.notify() for _ in range(50000)])
            producer.start()
            panel.close()
            stage = "closed"
            return
    except BaseException:
        finish(traceback.format_exc())


def retained_closed(value):
    # The Python wrapper remains available after its native QObject is deleted.
    return value.count == 1 and value.wakeup.closed


timer.timeout.connect(tick)
timer.start()
