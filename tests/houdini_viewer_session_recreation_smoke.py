# SPDX-License-Identifier: Apache-2.0
"""A cached image cannot acknowledge/focus a recreated session with reused IDs."""

from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import os
from pathlib import Path
import threading
import time
import traceback
import hou
from PySide6 import QtCore
from cycles_houdini import _native, camera_tools, registry
from cycles_houdini.panel import RenderPanel

path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
started = time.monotonic()
report = dict(
    result="running",
    checks=[],
    scope="One real viewer, no poll timer; resolution recreates the worker while its first image conversion is held.",
)
geo = hou.node("/obj").createNode("geo", "session_pig", run_init_scripts=False)
pig = geo.createNode("testgeometry_pighead")
pig.setRenderFlag(True)
pig.setDisplayFlag(True)
camera = hou.node("/obj").createNode("cam", "session_camera")
camera.parm("tz").set(5)
rop = hou.node("/out").createNode("cycles_ipr", "session_render")
set_rop_devices(rop, os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
rop.setParms(
    dict(
        vobject=geo.name(),
        camera=camera.path(),
        resx=320,
        resy=180,
        samples=65536,
        ipr_samples=65536,
        use_denoise=0,
        ipr_use_denoise=0,
        adaptive_sampling=0,
        ipr_adaptive_sampling=0,
    )
)
panel = RenderPanel()
panel.set_rop(rop)
panel.resize(900, 650)
panel.show()
panel.render()
panel.timer.stop()
entered = threading.Event()
release = threading.Event()
stage = "initial"
old_session = None
original_focus = camera_tools.focus_distance
focus_calls = []
timer = QtCore.QTimer(hou.qt.mainWindow())
timer.setInterval(10)
# Canvas paints, counted after the panel's own framePainted handler has run
# (connected later, so called later). repaint() is not synchronous when the
# viewer root is a native window (gpu and native display): Qt then paints at
# the next expose/flush. Checks that need a finished paint wait for one.
paints = []
panel.canvas.framePainted.connect(lambda generation: paints.append(generation))
paint_wait = None


def painted_since(count, what):
    if len(paints) > count:
        return True
    if time.monotonic() - paint_wait > 5:
        raise TimeoutError("No canvas paint " + what)
    return False


def finish(error=None):
    timer.stop()
    release.set()
    camera_tools.focus_distance = original_focus
    panel.dispose()
    panel.close()
    registry.close_all()
    report.update(result="failed" if error else "passed", error=error, wall_seconds=time.monotonic() - started)
    path.write_text(json.dumps(report, indent=2))
    hou.ui.postEventCallback(lambda: hou.exit(1 if error else 0, suppress_save_prompt=True))


def tick():
    global stage, old_session, old_metadata, original_convert, original_entry, paint_wait, paint_count
    try:
        if time.monotonic() - started > 65:
            raise TimeoutError("Session recreation stalled at " + stage)
        assert not panel.timer.isActive()
        status = panel.entry.session.status()
        metadata = getattr(panel.canvas, "frame_metadata", {})
        if status["state"] == "failed":
            raise RuntimeError(status["error"])
        if stage == "initial" and metadata and metadata["generation"] == status["committed_generation"]:
            old_session = panel.entry.session
            original_entry = panel.entry
            old_metadata = dict(metadata)
            assert old_metadata["generation"] == old_metadata["render_revision"] == 1
            original_convert = panel._display_queue.convert

            def held(frame, settings):
                if _native.frame_info(frame)["full_width"] == 400 and not entered.is_set():
                    entered.set()
                    assert release.wait(10), "New-session image was not released"
                return original_convert(frame, settings)

            panel._display_queue.convert = held
            rop.parm("resx").set(400)
            panel.queue_render()
            stage = "recreated"
            return
        if stage == "recreated" and entered.is_set():
            assert panel.entry is original_entry and panel.entry.session is not old_session
            assert status["committed_generation"] == status["render_revision"] == 1, (
                "Fixture did not reuse both identities"
            )
            # The old worker keeps rendering until the recreation is applied, so the
            # viewer may still have accepted later frames of the old session after
            # old_metadata was read (it did, intermittently: sequence 3 after 2). Those
            # are still the old session's pixels. A relabel would carry the new
            # session's display epoch or its 400 x 225 frame. (Progressive frames of
            # one session differ in rendered size, so compare the full frame size.)
            shown = (metadata.get("display_epoch"), tuple(panel.canvas.full_size))
            assert shown == (old_metadata["display_epoch"], (320, 180)), (
                "Cached pixels were relabeled before conversion",
                old_metadata,
                metadata,
                shown,
            )
            assert metadata["generation"] == metadata["render_revision"] == 1
            assert metadata["sequence"] >= old_metadata["sequence"], (old_metadata, metadata)
            assert metadata["display_epoch"] != panel._display_epoch
            paint_count, paint_wait = len(paints), time.monotonic()
            panel.canvas.update()
            stage = "recreated-paint"
            return
        if stage == "recreated-paint" and painted_since(paint_count, "of the cached image"):
            controller = panel.entry.session._controller
            assert controller.presented_revision is None, "Cached old image acknowledged the new session"

            def spy(*args):
                focus_calls.append(True)
                return original_focus(*args)

            camera_tools.focus_distance = spy
            before = camera.evalParm("focus")
            panel.pick_focus(100, 80)
            assert not focus_calls and camera.evalParm("focus") == before
            assert "Wait for the current camera image" in panel.info.text()
            report["checks"] += [
                "Resolution recreates the worker while retaining one viewer/entry",
                "Equal logical/physical IDs cannot make cached pixels acknowledge a new session",
                "Stale focus pick is rejected before evaluating its distance",
            ]
            release.set()
            stage = "painted"
            return
        if (
            stage == "painted"
            and metadata.get("display_epoch") == panel._display_epoch
            and metadata.get("rendered_width") == 400
        ):
            paint_count, paint_wait = len(paints), time.monotonic()
            panel.canvas.update()
            stage = "new-paint"
            return
        if stage == "new-paint" and painted_since(paint_count, "of the new-session image"):
            controller = panel.entry.session._controller
            assert controller.presented_revision == (controller.epoch, status["render_revision"]), (
                "New-session image was not acknowledged",
                controller.presented_revision,
                controller.epoch,
                controller.physical,
                controller.committed,
                status,
                metadata,
                type(panel.canvas).__name__,
            )
            assert metadata["generation"] == status["committed_generation"] and len(registry.entries()) == 1
            report["checks"].append("New-session image paints and acknowledges without viewer polling")
            panel.grab().save(str(path.with_suffix(".panel.png")))
            finish()
    except BaseException:
        finish(traceback.format_exc())


timer.timeout.connect(tick)
timer.start()
