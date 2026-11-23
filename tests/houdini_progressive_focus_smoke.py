# SPDX-License-Identifier: Apache-2.0
"""Click to focus must work before a long progressive render completes."""

import pass_fixtures as pass_fixture

from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import os
from pathlib import Path
import time
import traceback

import hou
from PySide6 import QtCore, QtTest
import cycles_houdini as ch
from cycles_houdini import registry, _native
from cycles_houdini.panel import RenderPanel
from gui_shutdown import retire_and_exit


class ProgressiveFocus:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.report = dict(result="running", checks=[])
        self.started, self.finished, self.busy = time.monotonic(), False, False
        self.first_image = None
        self.stage = "first"
        self.width, self.height = map(int, os.environ.get("CYCLES_HOUDINI_FOCUS_SIZE", "384x216").split("x"))
        obj = hou.node("/obj").createNode("geo", "progressive_focus_geometry", run_init_scripts=False)
        box = obj.createNode("box")
        box.setRenderFlag(True)
        material = ch.create_material(name="progressive_focus_surface", shader="emission")
        # The default this test was written for (new nodes now take Blender's).
        material.node("surface_shader").setParms(dict(color=(0.8, 0.8, 0.8), strength=10.0))
        pass_fixture.parm(obj, "shop_materialpath").set(material.path())
        self.camera = hou.node("/obj").createNode("cam", "progressive_focus_camera")
        pass_fixture.set_parameters(self.camera, dict(tz=5, projection=1, orthowidth=3, focus=10))
        rop = hou.node("/out").createNode("cycles_ipr", "progressive_focus_rop")
        set_rop_devices(rop, os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        pass_fixture.set_parameters(
            rop,
            dict(
                source_mode="sop",
                soppath=box.path(),
                camera=self.camera.path(),
                resx=self.width,
                resy=self.height,
                viewer_interactive=1,
                resolution_percent=100,
                ipr_resolution_percent=0,
                ipr_samples=1048576,
                ipr_adaptive_sampling=0,
                ipr_use_denoise=0,
                allowmotionblur=0,
                pass_depth=0,
                pass_position=0,
                pass_normal=1,
                pass_sample_count=1,
            ),
        )
        self.rop = rop
        self.factory = registry.session_from_configuration

        def diagnostic_session(*args, **kwargs):
            return self.factory(*args, **dict(kwargs, collect_diagnostics=True))

        registry.session_from_configuration = diagnostic_session
        self.panel = RenderPanel()
        self.panel.resize(900, 600)
        self.panel.show()
        self.panel.set_rop(rop)
        self.panel.focus_pick.setChecked(True)
        self.timer = QtCore.QTimer(hou.qt.mainWindow())
        self.timer.setInterval(20)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)

    def tick(self):
        if self.finished or self.busy:
            return
        self.busy = True
        try:
            assert time.monotonic() - self.started < 45, "Progressive focus did not receive a current image"
            entry = self.panel.entry
            if not entry:
                return
            state, frame = entry.session.status(), entry.session.frame_info()
            assert state["state"] != "failed" and not entry.error, (state, entry.error)
            if (
                not frame
                or self.panel._live_generation != state["generation"]
                or frame["generation"] != state["generation"]
                or not frame.get("current_scene", True)
            ):
                return
            assert not frame["complete"], "Fixture must still be rendering when focus is picked"
            if self.first_image is None:
                self.first_image = time.monotonic()
            if self.stage == "disabled":
                assert state["generation"] == self.disabled_generation, (
                    "Disarming focus restarted the progressive render"
                )
                if time.monotonic() - self.disabled_since < 0.5:
                    return
                self.report["checks"].append("Disarming focus during progressive rendering does not restart it")
                self.finish(0)
                return
            if self.stage == "dwell":
                sample = entry.session.focus_sample(self.width // 2, self.height // 2)
                if not sample or sample["rendered_width"] != self.width or sample["rendered_height"] != self.height:
                    return
                counts = {
                    key: value["calls"] for key, value in entry.session.diagnostics()["output_plane_reads"].items()
                }
                assert counts.get("normal", 0) == counts.get("sample_count", 0) == 0, counts
                if not hasattr(self, "cache_counts"):
                    self.cache_counts, self.cache_since = counts, time.monotonic()
                    return
                assert counts.get("position") == self.cache_counts.get("position") and counts.get(
                    "depth"
                ) == self.cache_counts.get("depth"), counts
                if time.monotonic() - self.cache_since < 0.4:
                    return
                self.report["checks"].append(
                    "Focus data is cached at full resolution; unrelated enabled AOVs are not downloaded"
                )
                self.report.update(output_plane_reads=counts, resolution=[self.width, self.height])
                assert (
                    pass_fixture.evaluate(self.rop, "pass_depth")
                    == pass_fixture.evaluate(self.rop, "pass_position")
                    == 0
                )
                self.panel.focus_pick.setChecked(False)
                assert entry.session.focus_sample(self.width // 2, self.height // 2) is None
                self.report["checks"].append(
                    "Disabling the tool retires the picking buffer without changing saved pass toggles"
                )
                self.disabled_generation = state["generation"]
                self.disabled_since = time.monotonic()
                self.stage = "disabled"
                return
            if self.stage == "camera" and frame["generation"] <= self.before:
                return
            expected = 4.5 if self.stage == "first" else 5.5
            sample = entry.session.focus_sample(self.width // 2, self.height // 2)
            if (
                sample is not None
                and self.stage == "first"
                and (sample["rendered_width"], sample["rendered_height"]) == (self.width, self.height)
            ):
                # A background click is ordinary input, not an uncaught native error.
                self.panel.pick_focus(0, 0)
                assert "No rendered surface" in self.panel.info.text(), self.panel.info.text()
                assert pass_fixture.evaluate(self.camera, "focus") == 10
                self.report["checks"].append("A background click reports no surface and leaves focus unchanged")
            original = _native.frame

            def no_full_frame(*args, **kwargs):
                raise AssertionError("Focus picking copied/expanded the full raw image")

            _native.frame = no_full_frame
            clicked = time.perf_counter()
            try:
                # Exercise the same canvas event and screen-to-image mapping an artist uses.
                QtTest.QTest.mouseClick(
                    self.panel.canvas, QtCore.Qt.LeftButton, pos=self.panel.canvas.image_rect().center().toPoint()
                )
            finally:
                _native.frame = original
            if abs(pass_fixture.evaluate(self.camera, "focus") - expected) < 1e-4:
                self.report.setdefault("focus_clicks", []).append(
                    dict(
                        stage=self.stage,
                        click_handler_seconds=time.perf_counter() - clicked,
                        workflow_seconds_since_first_image=time.monotonic() - self.first_image,
                        rendered_samples=frame["samples"],
                        image_complete=frame["complete"],
                    )
                )
                self.report["checks"].append("Clicking a progressive image sets focus before the final sample")
                if self.stage == "first":
                    # A newer camera has not yet reached the displayed image.
                    self.before = frame["generation"]
                    pass_fixture.parm(self.camera, "tz").set(6)
                    self.panel.pick_focus(self.width // 2, self.height // 2)
                    assert abs(pass_fixture.evaluate(self.camera, "focus") - 4.5) < 1e-4
                    self.report["checks"].append("A stale image cannot set focus after an unrendered camera edit")
                    self.stage = "camera"
                else:
                    self.panel.auto.setChecked(False)
                    self.stage = "dwell"
            else:
                assert time.monotonic() - self.first_image < 5, self.panel.info.text()
        except Exception:
            self.report["error"] = traceback.format_exc()
            self.finish(1)
        finally:
            self.busy = False

    def finish(self, code):
        if self.finished:
            return
        self.finished = True
        self.timer.stop()
        registry.session_from_configuration = self.factory
        entry = self.panel.entry
        retire_and_exit(
            self.path,
            self.report,
            [entry.session] if entry else [],
            code,
            (self.panel.dispose, self.panel.close, registry.close_all),
        )


test = ProgressiveFocus()
