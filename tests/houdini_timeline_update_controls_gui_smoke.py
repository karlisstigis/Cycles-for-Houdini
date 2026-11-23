# SPDX-License-Identifier: Apache-2.0
"""Actual panel rapid-frame coalescing and queued Auto/Pause/Stop/final controls.

Run explicitly with run-gui-test.ps1 -Device CPU. This is a correctness gate,
not a latency benchmark. The Houdini and render windows stay on the main 4K
screen. It creates only a disposable native scene and never opens artist files.
"""

import json
import os
from pathlib import Path
import time
import traceback

import hou
from PySide6 import QtCore, QtTest, QtWidgets
import cycles_houdini as ch
from cycles_houdini import device_selection, native_scene, registry


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        assert os.environ["CYCLES_HOUDINI_GUI_DEVICE"] == "CPU", "This small control gate uses CPU only"
        self.report = dict(result="running", checks=[], captures=[])
        self.started = time.monotonic()
        self.finished = self.busy = False
        self.panel = self.final_token = None
        self.phase = "mount"
        self.case_index = 0
        self.cases = [
            ("rapid", None, False),
            ("queued-stop", "stop", True),
            ("auto-off", "auto", False),
            ("pause", "pause", False),
            ("queued-pause", "pause", True),
            ("final-pause", "final", False),
            ("queued-final-pause", "final", True),
            ("queued-auto-off", "auto", True),
        ]
        self.select_screen()
        self.create_scene()
        self.original_capture = native_scene.capture

        def capture(rop, *args, **kwargs):
            captured = kwargs.get("at_time")
            if captured is None:
                captured = args[0] if args else hou.time()
            if rop == self.rop and not kwargs.get("camera_only"):
                self.report["captures"].append(dict(time=float(captured), phase=self.phase))
            return self.original_capture(rop, *args, **kwargs)

        native_scene.capture = capture
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(1000, 740))
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.timer = QtCore.QTimer(hou.qt.mainWindow())
        self.timer.setInterval(16)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.save()

    def select_screen(self):
        def four_k(screen):
            size, ratio = screen.geometry().size(), screen.devicePixelRatio()
            return size.width() * ratio >= 3800 and size.height() * ratio >= 2100

        primary = QtWidgets.QApplication.primaryScreen()
        candidates = [screen for screen in QtWidgets.QApplication.screens() if four_k(screen)]
        self.screen = primary if four_k(primary) else candidates[0] if len(candidates) == 1 else None
        assert self.screen is not None, "A unique main 4K screen is required"
        area = self.screen.availableGeometry()
        main = hou.qt.mainWindow()
        if main.windowHandle() is not None:
            main.windowHandle().setScreen(self.screen)
        main.setGeometry(area.x(), area.y(), min(area.width(), 1800), min(area.height(), 1200))
        self.report["screen"] = dict(
            name=self.screen.name(),
            geometry=[area.x(), area.y(), area.width(), area.height()],
            device_pixel_ratio=self.screen.devicePixelRatio(),
        )

    def create_scene(self):
        hou.setFrame(1)
        geo = hou.node("/obj").createNode("geo", "timeline_controls", run_init_scripts=False)
        grid = geo.createNode("grid")
        grid.setParms(dict(rows=2, cols=2, orient=0))
        grid.parmTuple("size").set((1.2, 0.8))
        motion = geo.createNode("attribwrangle", "frame_motion")
        motion.setInput(0, grid)
        motion.parm("snippet").set("@P.x += .02*(@Frame-1);")
        motion.setDisplayFlag(True)
        motion.setRenderFlag(True)
        material = ch.create_material(name="timeline_controls_surface", shader="emission")
        # The default this test was written for (new nodes now take Blender's).
        material.node("surface_shader").setParms(dict(strength=10.0))
        material.node("surface_shader").parmTuple("color").set((0.7, 0.2, 0.05))
        geo.parm("shop_materialpath").set(material.path())
        camera = hou.node("/obj").createNode("cam", "timeline_controls_camera")
        camera.setParms(dict(tz=5, projection=1, orthowidth=4))
        self.rop = hou.node("/out").createNode("cycles_ipr", "timeline_controls")
        device_selection.set_devices(self.rop, "CPU")
        self.rop.setParms(
            dict(
                source_mode="objects",
                objectroot="/obj",
                vobject=geo.path(),
                forceobject=geo.path(),
                camera=camera.path(),
                lights="",
                resx=128,
                resy=72,
                samples=16,
                ipr_samples=16,
                viewer_interactive=1,
                render_threads=2,
                use_denoise=0,
                ipr_use_denoise=0,
                adaptive_sampling=0,
                ipr_adaptive_sampling=0,
                allowmotionblur=0,
                world_source="color",
                seed=0,
                animate_seed=0,
            )
        )
        self.rop.parmTuple("background").set((0, 0, 0))

    def save(self):
        self.report["phase"] = self.phase
        self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")

    def status(self):
        entry = self.panel.entry if self.panel is not None else None
        if entry is None:
            return None
        value = entry.session._controller.status()
        assert not value.get("error") and not entry.error, (value, entry.error)
        return value

    def ready(self, status):
        if status is None:
            return False
        entry, session = self.panel.entry, self.panel.entry.session
        metadata = getattr(self.panel.canvas, "frame_metadata", {})
        return (
            not status.get("pending_update")
            and session._active_capture is None
            and session._pending_capture is None
            and entry.change_serial == entry.applied_serial
            and entry.observed_time == hou.time()
            and not self.panel._pending
            and not self.panel._in_render
            and not self.panel._render_queued
            and metadata.get("generation") == status["generation"]
            and metadata.get("render_revision") == status["render_revision"]
            and metadata.get("samples", 0) > 0
        )

    def click(self, button):
        assert button.isEnabled(), button.text()
        QtTest.QTest.mouseClick(button, QtCore.Qt.LeftButton)

    def control(self, name, enabled):
        if name == "auto":
            if self.panel.auto.isChecked() != enabled:
                self.click(self.panel.auto)
        elif name == "pause":
            if self.panel.pause_button.isChecked() != enabled:
                self.click(self.panel.pause_button)
        elif name == "stop":
            self.click(self.panel.stop_button if enabled else self.panel.render_button)
        elif name == "final":
            if enabled:
                self.final_token = registry.begin_local_final()
                # A completed IPR retains its image under the global final
                # lease. Only a still-rendering IPR also sets final_paused.
                assert registry.local_final_active()
                self.case["final_paused"] = self.panel.entry.final_paused
            else:
                registry.end_local_final(self.final_token)
                self.final_token = None

    def start_case(self, status):
        name, control, queued = self.cases[self.case_index]
        self.phase = name
        self.case = dict(
            name=name,
            control=control,
            queued=queued,
            before_captures=len(self.report["captures"]),
            before_generation=status["generation"],
        )
        self.target_frame = int(hou.frame()) + 3
        if control and not queued:
            self.control(control, False if control == "auto" else True)
        for frame in range(self.target_frame - 2, self.target_frame + 1):
            hou.setFrame(frame)
        if queued:
            # Exercise the actual host interleaving: Auto has posted one source
            # callback, then a control changes before that callback is serviced.
            self.panel.poll()
            assert self.panel._render_queued, "Timeline did not enqueue an immediate coalesced host callback"
            self.control(control, False if control == "auto" else True)
        self.hold_until = time.monotonic() + 0.35
        self.phase = name + ("-held" if control else "-wait")
        self.save()

    def tick(self):
        if self.finished or self.busy:
            return
        self.busy = True
        try:
            assert time.monotonic() - self.started < 90, "GUI controls gate timed out"
            if self.phase == "mount":
                self.panel = self.tab.activeInterfaceRootWidget()
                if self.panel is None:
                    return
                self.panel.set_rop(self.rop)
                self.panel.auto.setChecked(True)
                window = hou.qt.floatingPanelWindow(self.tab.floatingPanel())
                if window is None:
                    return
                window.winId()
                window.windowHandle().setScreen(self.screen)
                area = self.screen.availableGeometry()
                window.setGeometry(area.x() + 80, area.y() + 80, min(1000, area.width()), min(740, area.height()))
                window.show()
                self.click(self.panel.render_button)
                self.phase = "initial"
                return
            status = self.status()
            if self.phase == "initial":
                if self.ready(status):
                    self.start_case(status)
                return
            if self.phase.endswith("-held"):
                assert len(self.report["captures"]) == self.case["before_captures"], self.case
                assert status["generation"] == self.case["before_generation"], status
                if time.monotonic() >= self.hold_until:
                    self.control(self.case["control"], True if self.case["control"] == "auto" else False)
                    self.phase = self.case["name"] + "-wait"
                return
            if self.phase.endswith("-wait"):
                if not self.ready(status):
                    return
                captures = self.report["captures"][self.case["before_captures"] :]
                assert len(captures) == 1, (self.case, captures)
                assert abs(captures[0]["time"] - hou.frameToTime(self.target_frame)) < 1e-9, captures
                assert self.panel._timeline_pending_time is None
                self.stable_until = time.monotonic() + 0.2
                self.phase = self.case["name"] + "-stable"
                return
            if self.phase.endswith("-stable"):
                assert len(self.report["captures"]) == self.case["before_captures"] + 1, "Same frame queued repeatedly"
                if time.monotonic() < self.stable_until:
                    return
                self.report["checks"].append(
                    dict(**self.case, target_frame=self.target_frame, captured_time=self.report["captures"][-1]["time"])
                )
                self.case_index += 1
                if self.case_index == len(self.cases):
                    self.finish()
                else:
                    self.start_case(status)
        except Exception:
            self.finish(traceback.format_exc())
        finally:
            self.busy = False

    def finish(self, error=None):
        if self.finished:
            return
        self.finished = True
        self.timer.stop()
        native_scene.capture = self.original_capture
        if self.final_token is not None:
            registry.end_local_final(self.final_token)
        if self.panel is not None:
            self.panel.dispose()
        registry.close_all()
        self.report.update(result="failed" if error else "passed", error=error, seconds=time.monotonic() - self.started)
        self.save()
        hou.ui.postEventCallback(lambda: hou.exit(1 if error else 0, suppress_save_prompt=True))


workflow = Workflow()
