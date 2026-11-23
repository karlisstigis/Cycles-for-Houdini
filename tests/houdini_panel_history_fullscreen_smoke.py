# SPDX-License-Identifier: Apache-2.0
"""Artist workflow on the heavy SOP scene: snapshots, fullscreen and live edits."""

import os
from pathlib import Path
import runpy
import time
import traceback
import hou
from PySide6 import QtCore, QtGui, QtTest, QtWidgets
from cycles_houdini import registry

os.environ["CYCLES_HOUDINI_BENCH_SHARED_VIEWS"] = "0"
os.environ["CYCLES_HOUDINI_BENCH_SAMPLES"] = "16"
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
HeavyResponse = runpy.run_path(str(root / "tests/houdini_heavy_response_benchmark.py"), run_name="snapshot_fixture")[
    "HeavyResponse"
]


class HistoryWorkflow(HeavyResponse):
    def __init__(self):
        super().__init__()
        if self.panel._rop is None:
            raise RuntimeError("ROP configuration rejected: " + self.panel.info.text())

    def configure_scene(self):
        self.ground.parm("resolution").set(560)

    def complete(self, entry):
        status = entry.session.status()
        frame = entry.session.frame_info()
        return (
            frame
            and frame["complete"]
            and frame["generation"] == status["generation"]
            and self.panel._live_generation == status["generation"]
            and status["generation"] > self.before
        )

    def browse(self, key, index, image):
        QtTest.QTest.keyClick(self.panel.history_slider, key)
        assert self.panel.snapshot_menu.currentIndex() == index
        assert self.panel.canvas.image == image

    def open_fullscreen(self, stage):
        self.image_before_fullscreen = self.panel.canvas.image.copy()
        self.canvas_identity = id(self.panel.canvas)
        self.status_before_fullscreen = self.panel.entry.session.status()
        QtTest.QTest.mouseClick(self.panel.fullscreen_button, QtCore.Qt.LeftButton)
        window = self.panel._fullscreen
        QtGui.QCursor.setPos(window.mapToGlobal(window.rect().center()))
        self.stage = stage
        self.next_at = time.monotonic() + 0.5

    def check_fullscreen(self, entry):
        window = self.panel._fullscreen
        assert window is not None and window.isFullScreen() and window.isVisible()
        assert id(self.panel.canvas) == self.canvas_identity
        assert self.panel.canvas.window() is window
        assert self.panel.canvas.geometry() == window.rect(), "Fullscreen image does not fill its window"
        assert not window.top.isVisible() and not window.bottom.isVisible(), "Fullscreen chrome was not hidden"
        if self.panel.snapshot_menu.currentIndex():
            assert self.panel.canvas.image == self.image_before_fullscreen
        else:
            # The worker may deliver its final progressive image after the
            # completion event. Live must keep consuming those updates.
            assert self.panel.canvas.image == self.panel._live_image
        assert entry is self.original_entry and entry.views == 1
        assert len(registry.entries()) == 1
        for name in ("generation", "geometry_conversions", "device_allocation_bytes"):
            assert entry.session.status()[name] == self.status_before_fullscreen[name], name
        return window

    def tick(self):
        if self.tick_busy:
            return
        self.tick_busy = True
        try:
            if time.monotonic() - self.began > 220:
                raise TimeoutError(self.stage + ": " + self.panel.info.text())
            entry = registry.get(self.key)
            if entry is None:
                if self.stage != "retirement":
                    return
                entry = self.original_entry
            status = entry.session.status()
            if entry.error or status["state"] == "failed":
                raise RuntimeError(entry.error or status["error"])
            self.report["current"] = dict(stage=self.stage, status=status)
            self.save()
            if self.stage == "initial":
                if not self.complete(entry):
                    return
                assert self.report["fixture"]["mesh_triangles"] >= 1000000
                assert not self.panel._snapshots and self.panel.snapshot_menu.count() == 1
                assert not self.panel.history_slider.isEnabled(), "Render was saved without Snapshot"
                self.original_entry = entry
                self.first = self.panel.canvas.image.copy()
                QtTest.QTest.mouseClick(self.panel.snapshot_button, QtCore.Qt.LeftButton)
                assert len(self.panel._snapshots) == 1
                self.browse(QtCore.Qt.Key_Home, 1, self.first)
                assert not self.panel.save_button.isEnabled() and not self.panel.exposure.isEnabled()
                self.before = status["generation"]
                self.conversions = status["geometry_conversions"]
                self.camera.parm("tx").set(self.camera_x + 0.35)
                self.stage = "edit-while-browsing"
            elif self.stage == "edit-while-browsing":
                if not self.complete(entry):
                    return
                assert self.panel.canvas.image == self.first, "Live update overwrote selected snapshot"
                assert len(self.panel._snapshots) == 1, "Live edit created an automatic snapshot"
                assert status["geometry_conversions"] == self.conversions
                assert self.panel._live_image != self.first, "Live image stopped refreshing during history browsing"
                self.browse(QtCore.Qt.Key_End, 0, self.panel._live_image)
                assert self.panel.canvas.frame_metadata == self.panel._live_frame_metadata
                assert self.panel.canvas.frame_metadata["generation"] == self.panel._live_generation
                assert self.panel.canvas.frame_metadata["render_revision"] == status["render_revision"]
                assert not self.panel._settings_dirty, "Returning to cached Live requested another display conversion"
                self.second = self.panel.canvas.image.copy()
                QtTest.QTest.mouseClick(self.panel.snapshot_button, QtCore.Qt.LeftButton)
                self.panel.exposure.setValue(1)
                self.panel.poll()
                self.stage = "exposure-snapshot"
            elif self.stage == "exposure-snapshot":
                if self.panel._settings_dirty:
                    return
                self.third = self.panel.canvas.image.copy()
                assert self.third != self.second
                QtTest.QTest.mouseClick(self.panel.snapshot_button, QtCore.Qt.LeftButton)
                assert len(self.panel._snapshots) == 3
                self.browse(QtCore.Qt.Key_Home, 1, self.first)
                self.browse(QtCore.Qt.Key_Right, 2, self.second)
                self.browse(QtCore.Qt.Key_Right, 3, self.third)
                self.browse(QtCore.Qt.Key_Right, 0, self.panel._live_image)
                assert self.panel.exposure.isEnabled() and self.panel.gamma.isEnabled()
                assert self.panel.channels.isEnabled() and self.panel.pass_menu.isEnabled()
                assert entry.session.status()["generation"] == status["generation"]
                self.panel.grab().save(str(self.path.with_suffix(".history.png")))
                self.panel.canvas.fit_image()
                self.open_fullscreen("fullscreen-live")
            elif self.stage == "fullscreen-live" and time.monotonic() >= self.next_at:
                window = self.check_fullscreen(entry)
                window.grab().save(str(self.path.with_suffix(".fullscreen.png")))
                QtTest.QTest.keyClick(self.panel.canvas, QtCore.Qt.Key_F)
                physical_width = self.panel.canvas.image_rect().width() * self.panel.canvas.devicePixelRatioF()
                assert abs(physical_width - 960) < 1e-6
                QtTest.QTest.keyClick(self.panel.canvas, QtCore.Qt.Key_F, QtCore.Qt.ShiftModifier)
                assert self.panel.canvas.view_mode == "fit"
                start = self.panel.canvas.rect().center()
                QtTest.QTest.mousePress(self.panel.canvas, QtCore.Qt.MiddleButton, pos=start)
                QtTest.QTest.mouseMove(self.panel.canvas, start + QtCore.QPoint(32, 18))
                QtTest.QTest.mouseRelease(self.panel.canvas, QtCore.Qt.MiddleButton, pos=start + QtCore.QPoint(32, 18))
                assert self.panel.canvas.view_mode == "manual"
                self.before = status["generation"]
                self.hero.parm("twist").set(self.hero.evalParm("twist") + 0.4)
                self.stage = "fullscreen-edit"
            elif self.stage == "fullscreen-edit":
                if not self.complete(entry):
                    return
                assert self.panel._fullscreen.isFullScreen()
                assert self.panel.canvas.image == self.panel._live_image
                assert self.panel.canvas.image != self.third
                assert len(self.panel._snapshots) == 3
                assert id(self.panel.canvas) == self.canvas_identity and entry.views == 1
                self.latest = self.panel.canvas.image.copy()
                QtTest.QTest.keyClick(self.panel.canvas, QtCore.Qt.Key_Escape)
                assert self.panel._fullscreen is None and self.panel.layout().indexOf(self.panel.canvas) >= 0
                assert self.panel.canvas.image == self.panel._live_image
                self.panel.snapshot_menu.setCurrentIndex(3)
                self.open_fullscreen("fullscreen-close")
            elif self.stage == "fullscreen-close" and time.monotonic() >= self.next_at:
                window = self.check_fullscreen(entry)
                window.close()
                assert self.panel._fullscreen is None and self.panel.canvas.parentWidget() is self.panel
                assert self.panel.canvas.image == self.third
                # Explicit snapshots only. Evicting the selected oldest image
                # returns to Live rather than showing a different saved image.
                self.browse(QtCore.Qt.Key_Home, 1, self.first)
                for _ in range(6):
                    QtTest.QTest.mouseClick(self.panel.snapshot_button, QtCore.Qt.LeftButton)
                assert len(self.panel._snapshots) == 8 and self.panel.snapshot_menu.currentIndex() == 0
                assert self.panel.canvas.image == self.panel._live_image
                assert self.panel.history_slider.maximum() == 9
                # Exercise the byte limit separately from the eight-item limit.
                large = QtGui.QImage(2048, 2048, QtGui.QImage.Format_RGBA8888)
                large.fill(QtCore.Qt.black)
                for _ in range(5):
                    self.panel.add_history(large, "Budget check")
                retained = sum(image.sizeInBytes() for image in self.panel._snapshots)
                assert retained <= 64 * 2**20 and len(self.panel._snapshots) == 4
                oversized = QtGui.QImage(4097, 4097, QtGui.QImage.Format_RGBA8888)
                oversized.fill(QtCore.Qt.black)
                self.panel.add_history(oversized, "Must reject")
                assert len(self.panel._snapshots) == 4
                del large, oversized
                self.panel.history_slider.setValue(self.panel.history_slider.maximum())
                self.open_fullscreen("dispose-fullscreen")
                self.report["history_bytes"] = retained
            elif self.stage == "dispose-fullscreen" and time.monotonic() >= self.next_at:
                self.check_fullscreen(entry)
                self.panel.dispose()
                assert self.panel._fullscreen is None and not self.panel._snapshots
                assert self.panel.canvas.image.isNull() and self.panel._live_image is None
                # Native pane teardown schedules retirement on the next host
                # event-loop poll; do not force it from a QWidget close event.
                self.stage = "retirement"
                self.retirement_started = time.monotonic()
                self.next_at = time.monotonic() + 0.05
            elif self.stage == "retirement" and time.monotonic() >= self.next_at:
                if registry.get(self.key) is not None:
                    return
                assert registry.get(self.key) is None
                retired = self.original_entry.session.status()
                self.report["retirement_status"] = retired
                if retired["state"] == "closing":
                    if time.monotonic() - self.retirement_started > 10:
                        raise TimeoutError("Viewer closed, but renderer retirement did not finish: " + str(retired))
                    return
                assert retired["state"] == "closed", retired
                assert not retired["backend_alive"] and retired["device_allocation_bytes"] == 0, retired
                self.report["retirement_seconds"] = time.monotonic() - self.retirement_started
                self.report["checks"] = [
                    "No automatic render snapshots",
                    "Explicit Snapshot and integer-key browsing",
                    "Live advances while history stays selected",
                    "History browsing does not restart rendering",
                    "Returning to completed Live restores exact pixel identity without another conversion",
                    "Single session and canvas in image-only fullscreen",
                    "Fullscreen pan and physical 100% zoom",
                    "Heavy SOP deformation updates during fullscreen",
                    "Escape and window close restore the panel",
                    "Eight-image and 64 MiB bounds",
                    "Oversized image rejected before copy",
                    "Panel disposal during fullscreen releases renderer and images",
                ]
                self.finish()
                return
        except Exception:
            self.report.update(result="failed", error=traceback.format_exc())
            self.save()
            self.timer.stop()
            hou.ui.removeEventLoopCallback(self.callback)
            if self.panel:
                self.panel.dispose()
            registry.close_all()
            hou.exit(1, suppress_save_prompt=True)
        finally:
            self.tick_busy = False


workflow = HistoryWorkflow()
