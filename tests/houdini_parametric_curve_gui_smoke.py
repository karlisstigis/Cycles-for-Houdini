# SPDX-License-Identifier: Apache-2.0
"""One viewer: native curve/field repair, sampling controls and retained heavy instances."""

import os
from pathlib import Path
from types import SimpleNamespace
import time
import traceback

import hou
import numpy as np
from PySide6 import QtCore, QtTest, QtWidgets
from cycles_houdini import object_properties, registry
from parametric_curve_recovery_fixture import Fixture, pixels
from houdini_volume_vector_gui_smoke import Workflow as ParameterWorkflow


class Workflow(ParameterWorkflow):
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.native = os.environ.get("CYCLES_HOUDINI_CURVE_VIEWER") == "native"
        self.fixture = f = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"], scale=2)
        self.rop, self.source = f.rop, f.control
        object_properties.install(f.obj)
        f.obj.parm("cycles_curve_segments").set(8)
        self.heavy = hou.node("/obj").createNode("geo", "retained_curve_instances", run_init_scripts=False)
        self.torus = self.heavy.createNode("torus", "million_triangle_prototype")
        self.torus.setParms(dict(type=0, rows=512, cols=1024))
        targets = self.heavy.createNode("grid", "twelve_targets")
        targets.setParms(dict(type=0, rows=3, cols=4))
        targets.parmTuple("size").set((8, 6))
        targets.parmTuple("t").set((0, 0, 20))
        copies = self.heavy.createNode("copytopoints", "shared_instances")
        copies.setInput(0, self.torus)
        copies.setInput(1, targets)
        copies.parm("pack").set(1)
        copies.setDisplayFlag(True)
        copies.setRenderFlag(True)
        self.heavy.parm("shop_materialpath").set("/mat/parametric_sentinel")
        self.rop.parm("vobject").set(f.obj.path() + " " + self.heavy.path())
        self.steps = []

        def step(label, mode=0, root="Cd", edits=None, **flags):
            self.steps.append(
                dict(
                    label=label,
                    case=SimpleNamespace(mode=mode, root=root),
                    edits=edits
                    if edits is not None
                    else [(f.control.parm("mode"), mode), (f.reader.parm("attribute"), root)],
                    **flags,
                )
            )

        step("Initial")
        step("Object override on", edits=[(f.obj.parm("cycles_curve_segments_override"), True)])
        step("Object sampling", edits=[(f.obj.parm("cycles_curve_segments"), 4)])
        step("Object override off", edits=[(f.obj.parm("cycles_curve_segments_override"), False)])
        for mode, root in (
            (1, "Cd"),
            (2, "Cd"),
            (3, "Cd"),
            (4, "signal"),
            (5, "uv"),
            (7, "Cd"),
            (9, "Cd"),
            (10, "Cd"),
            (8, "Cd"),
        ):
            step("Broken " + str(mode), mode, root)
            step("Repair " + str(mode))
            if mode == 1:
                for label, parm, value in (
                    ("Camera move", f.camera.parm("tx"), 0.02),
                    ("Camera return", f.camera.parm("tx"), 0),
                    ("Instance move", self.heavy.parm("tx"), 0.5),
                    ("Instance return", self.heavy.parm("tx"), 0),
                ):
                    step(label, edits=[(parm, value)], navigation=True)
        for value in (4, 32, 16):
            step("ROP sampling " + str(value), edits=[(f.rop.parm("curve_segments"), value)])
        self.settings = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm, size=(650, 600))
        self.settings.setPin(True)
        self.settings.setCurrentNode(self.source)
        self.panel = None
        if self.native:
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.IPRViewer)
            self.tab.setRopNode(self.rop)
            self.tab.setPreview(True)
            self.tab.setAutoUpdate(True)
            self.tab.setDelay(0.02)
        else:
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(1000, 740))
            self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.report = dict(
            result="running",
            viewer="native" if self.native else "cycles",
            events=[],
            render_clicks=1,
            scope=__doc__,
            physical_mouse_certification=False,
            toggle_entry="HOM; native numeric and string fields use Qt parameter entry",
            heavy_scope="12 behind-camera instances retain a million-triangle prototype; ownership stress, not a ray-performance benchmark",
        )
        self.index = self.generation = 0
        self.phase = "mount"
        self.began = time.monotonic()
        self.next_at = self.saved_at = 0
        self.busy = self.finished = False
        self.entry = self.session = self.previous = self.canvas_geometry = None
        hou.hipFile.save(str(f.dest / ("parametric-recovery-" + self.report["viewer"] + ".hiplc")))
        hou.ui.addEventLoopCallback(self.tick)
        if self.native:
            self.tab.startRender()

    def tick(self):
        if (
            not self.busy
            and self.phase in ("type", "commit")
            and not isinstance(self.value, bool)
            and time.monotonic() >= self.next_at
        ):
            focus = QtWidgets.QApplication.focusWidget()
            # Windows may reject an asynchronous activateWindow request while
            # a native pane is rebuilding. Never type into the render panel.
            if (
                not self.window.isActiveWindow()
                or focus is None
                or not (focus is self.window or self.window.isAncestorOf(focus))
            ):
                self.busy = True
                try:
                    self.report.setdefault("focus_retries", []).append(
                        dict(
                            parameter=self.parm.path(),
                            phase=self.phase,
                            widget=focus.metaObject().className() if focus else None,
                        )
                    )
                    self.window.raise_()
                    self.window.activateWindow()
                    QtWidgets.QApplication.setActiveWindow(self.window)
                    self.window.setFocus(QtCore.Qt.OtherFocusReason)
                    self.phase = "focus"
                    self.next_at = time.monotonic() + 0.2
                finally:
                    self.busy = False
                return
        if not self.busy and self.phase == "type" and isinstance(self.value, bool):
            self.busy = True
            try:
                self.phase = "commit"
                self.parm.set(self.value)
                self.next_at = time.monotonic() + 0.15
                self.report.setdefault("hom_toggles", []).append(dict(path=self.parm.path(), value=self.value))
            except BaseException:
                self.close(traceback.format_exc())
            finally:
                self.busy = False
            return
        if not self.busy and self.phase == "type":
            focus = QtWidgets.QApplication.focusWidget()
            self.report["last_input_target"] = dict(
                parameter=self.parm.path(),
                value=self.value,
                widget_class=focus.metaObject().className() if focus else None,
                widget_name=focus.objectName() if focus else None,
                window_active=self.window.isActiveWindow(),
            )
        super().tick()

    def focus_edit(self):
        self.parm, self.value = self.edits[0]
        self.settings.setCurrentNode(self.parm.node(), pick_node=False)
        self.settings.setIsCurrentTab()
        for folder, index in zip(self.parm.containingFolderSetParmTuples(), self.parm.containingFolderIndices()):
            folder[0].set(1 if folder.parmTemplate().folderType() == hou.folderType.Collapsible else index)
        self.window = hou.qt.floatingPanelWindow(self.settings.floatingPanel())
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()
        QtWidgets.QApplication.setActiveWindow(self.window)
        self.window.setFocus()
        self.phase = "focus"
        self.next_at = time.monotonic() + 0.4

    def ready(self):
        entry = registry.get(f"rop:{self.rop.sessionId()}")
        if entry is None:
            return False
        self.entry = entry
        status = entry.session.status()
        info = entry.session.frame_info()
        assert not entry.error and status["state"] != "failed", (entry.error, status)
        if (
            not info
            or not info["complete"]
            or info["generation"] <= self.generation
            or info["generation"] != status["generation"]
            or status.get("capture_pending")
            or status.get("pending_update")
        ):
            return False
        if not self.native and (
            self.panel._live_generation != info["generation"]
            or self.panel.canvas.frame_metadata.get("sequence") != info["sequence"]
        ):
            return False
        frame = entry.session.frame()
        image = pixels(frame)
        step = self.steps[self.index]
        mode = step["case"].mode
        curves = 0 if mode == 9 else 1 if mode in (1, 7, 10) else 2
        assert status["curves"] == curves and status["points"] == 0 and status["packed_instances"] == 12, status
        assert (
            status["geometry_resources"] == 3 and status["object_resources"] == 14 and len(registry.entries()) == 1
        ), status
        assert np.isfinite(image).all()
        np.testing.assert_allclose(image[48, 142], (0.1, 0.7, 0.2), atol=1e-4, rtol=0)
        codes = {d["code"] for d in status["source_diagnostics"]}
        if mode in (1, 7, 9, 10):
            assert "geometry.curve_unavailable" in codes, status
        elif mode == 2:
            assert "geometry.invalid_width" in codes, status
        elif mode == 3:
            assert codes & {"geometry.invalid_color", "material.missing_color"}, status
        elif mode == 4:
            assert any(d["attribute"] == "signal" for d in status["source_diagnostics"]), status
        elif mode == 5:
            assert codes & {"geometry.invalid_uv", "material.missing_uv"}, status
        else:
            assert not codes, status
        if self.native:
            if not entry.helper_pid or self.tab.imageResolution() != (160, 96):
                return False
            try:
                if any(
                    not np.allclose(self.tab.pixel("C", x, y)[:3], image[y, x], atol=2e-4, rtol=0)
                    for x, y in ((80, 48), (142, 48), (80, 80))
                ):
                    return False
            except hou.OperationFailed:
                return False
        else:
            assert not self.panel.render_button.isEnabled() and self.panel.stop_button.isEnabled()
            geometry = (self.panel.canvas.width(), self.panel.canvas.height())
            if self.canvas_geometry is not None:
                assert geometry == self.canvas_geometry, (geometry, self.canvas_geometry)
            self.canvas_geometry = geometry
        cooks = self.torus.cookCount()
        if self.previous:
            assert status["renderer_pid"] == self.previous["renderer_pid"] and cooks == self.heavy_cooks, status
            if step.get("navigation"):
                assert all(status[k] == self.previous[k] for k in ("extractions", "geometry_conversions")), status
        self.generation = info["generation"]
        self.report["events"].append(
            dict(label=step["label"], mode=mode, root=step["case"].root, status=status, heavy_cooks=cooks)
        )
        self.previous = status
        self.heavy_cooks = cooks
        return True

    def close(self, error=None):
        if error and getattr(self, "window", None):
            target = self.path.with_name("parameter-entry-failure.png")
            self.report["parameter_failure_capture"] = str(target)
            self.window.grab().save(str(target))
        if self.report.get("viewer_capture_saved"):
            old = Path(self.report["viewer_capture"])
            new = old.with_name("parametric-viewer.png")
            if old != new and old.exists():
                old.rename(new)
                self.report["viewer_capture"] = str(new)
        super().close(error)


if __name__ == "__main__":
    workflow = Workflow()
