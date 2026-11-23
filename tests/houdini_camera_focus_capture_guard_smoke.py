# SPDX-License-Identifier: Apache-2.0
"""Focus-tool toggles preserve the cheap, camera-only Qt dispatch path."""

import pass_fixtures as pass_fixture

from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import os
from pathlib import Path
import time
import traceback

import hou
import numpy as np
from PySide6 import QtCore, QtTest
import cycles_houdini as ch
from cycles_houdini import _native, native_scene, registry
from cycles_houdini.panel import RenderPanel
from gui_shutdown import retire_and_exit


class FocusGuard:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.report = dict(result="running", checks=[])
        self.started, self.busy, self.finished = time.monotonic(), False, False
        self.in_camera_dispatch = False
        self.full_acquisitions = self.camera_headers = 0
        self.configuration_reads = self.control_syncs = 0
        self.stage = "initial"
        obj = hou.node("/obj").createNode("geo", "focus_guard_geometry", run_init_scripts=False)
        box = obj.createNode("box")
        box.setRenderFlag(True)
        self.camera = hou.node("/obj").createNode("cam", "focus_guard_camera")
        pass_fixture.set_parameters(self.camera, dict(tz=5, projection=1, orthowidth=3))
        material = ch.create_material(name="focus_guard_surface", shader="emission")
        # The default this test was written for (new nodes now take Blender's).
        material.node("surface_shader").setParms(dict(color=(0.8, 0.8, 0.8), strength=10.0))
        pass_fixture.parm(obj, "shop_materialpath").set(material.path())
        rop = hou.node("/out").createNode("cycles_ipr", "focus_guard_rop")
        set_rop_devices(rop, os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        pass_fixture.set_parameters(
            rop,
            dict(
                source_mode="sop",
                soppath=box.path(),
                camera=self.camera.path(),
                resx=96,
                resy=72,
                viewer_interactive=1,
                resolution_percent=100,
                ipr_resolution_percent=0,
                ipr_samples=16,
                ipr_adaptive_sampling=0,
                ipr_use_denoise=0,
                allowmotionblur=0,
                pass_depth=0,
                pass_position=0,
            ),
        )
        self.panel = RenderPanel()
        self.panel.resize(900, 600)
        self.panel.show()
        self.panel.set_rop(rop)
        self.original_acquire, self.original_capture = _native.acquire_apply, native_scene.capture
        self.original_configuration = native_scene.configuration
        self.original_sync = self.panel.sync_preview_controls
        self.dispatch = self.panel._run_queued_camera

        def acquire(*args, **kwargs):
            assert not self.in_camera_dispatch, "Full acquisition entered the camera-only Qt path"
            self.full_acquisitions += 1
            return self.original_acquire(*args, **kwargs)

        def capture(*args, **kwargs):
            if self.in_camera_dispatch:
                assert kwargs.get("camera_only"), "Full capture entered the camera-only Qt path"
            if kwargs.get("camera_only"):
                self.camera_headers += 1
            return self.original_capture(*args, **kwargs)

        def dispatch():
            self.in_camera_dispatch = True
            try:
                return self.dispatch()
            finally:
                self.in_camera_dispatch = False

        def configuration(*args, **kwargs):
            assert not self.in_camera_dispatch, "Camera-only dispatch rescanned the ROP device/control layout"
            self.configuration_reads += 1
            return self.original_configuration(*args, **kwargs)

        def sync():
            self.control_syncs += 1
            return self.original_sync()

        _native.acquire_apply, native_scene.capture = acquire, capture
        native_scene.configuration = configuration
        self.panel.sync_preview_controls = sync
        self.panel._run_queued_camera = dispatch
        self.timer = QtCore.QTimer(hou.qt.mainWindow())
        self.timer.setInterval(10)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)

    def tick(self):
        if self.busy or self.finished:
            return
        self.busy = True
        try:
            assert time.monotonic() - self.started < 60, self.stage
            entry = self.panel.entry
            if entry is None:
                return
            state, frame = entry.session.status(), entry.session.frame_info()
            assert state["state"] != "failed" and not entry.error, (state, entry.error)
            if (
                not frame
                or not frame["complete"]
                or frame["generation"] != state["generation"]
                or self.panel._live_generation != state["generation"]
                or state.get("capture_pending")
                or state.get("pending_update")
                or not frame.get("current_scene")
            ):
                return
            if self.stage == "initial":
                assert {"position", "depth"} <= set(frame["passes"])
                self.before = state["generation"]
                self.before_reads = self.full_acquisitions
                self.before_sync = self.control_syncs
                # Both edits occur before any queued camera callback can run.
                pass_fixture.parm(self.camera, "tx").set(0.1)
                self.panel.focus_pick.setChecked(True)
                assert self.panel._camera_capture_allowed()
                self.stage = "focus-passes"
            elif self.stage == "focus-passes":
                if state["generation"] <= self.before:
                    return
                assert {"position", "depth"} <= set(frame["passes"])
                assert self.full_acquisitions == self.before_reads
                assert self.control_syncs == self.before_sync, "Camera edit refreshed unrelated ROP controls"
                self.report["checks"].append(
                    "Arming focus during a camera edit does not force full geometry acquisition"
                )
                self.before = state["generation"]
                self.before_reads = self.full_acquisitions
                pass_fixture.parm(self.camera, "tx").set(0.2)
                assert self.panel._camera_capture_allowed()
                self.stage = "camera-only"
            elif self.stage == "camera-only":
                if state["generation"] <= self.before:
                    return
                assert self.full_acquisitions == self.before_reads
                assert self.camera_headers > 0
                plane = entry.session.frame()["planes"]["position"]
                values = np.frombuffer(plane["pixels"], np.float32).reshape(72, 96, plane["channels"])
                assert abs(values[36, 48, 2] - 0.5) < 1e-5 and abs(values[36, 48, 0] - 0.2) < 0.05
                self.report["checks"].append("Camera edits retain pick data and skip ROP layout/control refresh")
                self.before = state["generation"]
                self.before_configs = self.configuration_reads
                self.panel._rop.parm("ipr_samples").set(8)
                pass_fixture.parm(self.camera, "tx").set(0.3)
                assert not self.panel._camera_capture_allowed()
                self.stage = "rop-edit"
            elif self.stage == "rop-edit":
                if state["generation"] <= self.before:
                    return
                assert self.configuration_reads > self.before_configs
                assert state["rendering"]["samples"] == 8
                self.report["checks"].append(
                    "Concurrent ROP edit revokes the fast path and updates the real render settings"
                )
                self.panel._rop.parm("resx").setExpression(
                    '96 + floor(ch("' + self.camera.path() + '/tx") * 10)', hou.exprLanguage.Hscript
                )
                self.stage = "expression-setup"
            elif self.stage == "expression-setup":
                if self.panel.entry.config[1] != 99:
                    return
                self.before = state["generation"]
                pass_fixture.parm(self.camera, "tx").set(0.4)
                self.stage = "expression-edit"
            else:
                if self.panel.entry.config[1] != 100:
                    return
                assert frame["width"] == 100
                self.report["checks"].append(
                    "ROP resolution expressions referencing the edited camera invalidate the cached configuration"
                )
                self.finish(0)
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
        _native.acquire_apply, native_scene.capture = self.original_acquire, self.original_capture
        native_scene.configuration = self.original_configuration
        self.panel.sync_preview_controls = self.original_sync
        self.panel._run_queued_camera = self.dispatch
        entry = self.panel.entry
        self.report.update(camera_only_headers=self.camera_headers, full_acquisitions=self.full_acquisitions)
        retire_and_exit(
            self.path,
            self.report,
            [entry.session] if entry else [],
            code,
            (self.panel.dispose, self.panel.close, registry.close_all),
        )


test = FocusGuard()
