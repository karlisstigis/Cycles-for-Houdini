# SPDX-License-Identifier: Apache-2.0
"""Inline Color and Alpha ramps: no editor window, live Blender RGB interpolation and whole
Color Ramp promotion with retained heavy instances (native parameter edits drive the IPR)."""

import json, math, os, time, traceback
from pathlib import Path
import hou, numpy as np
from PySide6 import QtCore, QtGui, QtTest, QtWidgets
import cycles_houdini as ch
from cycles_houdini import registry, ramp_editor, ramps, _native
from cycles_houdini.ramps import ColorRampControls
from hue_ramp_fixture import Fixture


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.native = os.environ.get("CYCLES_HOUDINI_RGBA_VIEWER") == "native"
        self.f = f = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"], "blender-rgba-ramps.json")
        f.height = 96
        f.rop.parm("resy").set(96)
        f.choose("rgb_near_nonuniform")
        f.ramp.parm("ramp_interpolation").set("LINEAR")
        f.displace()
        self.heavy = f.add_heavy()
        # The Color Ramp is edited inline: two native ramp gadgets, no button
        # and no separate window.
        self.inline(f.ramp, "ramp", "alpha_ramp")
        assert f.ramp.parm("edit_ramp") is None and not hasattr(ramp_editor, "_windows")
        self.menu = lambda suffix: self.f.ramp.parm("ramp" + suffix)
        self.color = f.ramp.parm("ramp")
        self.alpha = f.ramp.parm("alpha_ramp")
        self.alpha_prefix = "alpha_ramp"
        self.key = "rop:" + str(f.rop.sessionId())
        self.output = "color"
        self.animation = None
        self.rapid = False
        self.steps = [
            ("Initial Blender RGB", lambda: None),
            ("Ease RGB", lambda: self.menu("_interpolation").set("EASE")),
            ("B-Spline RGB", lambda: self.menu("_interpolation").set("B_SPLINE")),
            ("Cardinal RGB", lambda: self.menu("_interpolation").set("CARDINAL")),
            ("Cardinal Alpha", lambda: self.connect("alpha")),
            ("Constant Alpha", lambda: self.menu("_interpolation").set("CONSTANT")),
            ("Linear Alpha", lambda: self.menu("_interpolation").set("LINEAR")),
            ("Promote whole Color Ramp while rendering", self.promote),
            ("Promoted Ease", lambda: self.menu("_interpolation").set("EASE")),
            ("Promoted B-Spline", lambda: self.menu("_interpolation").set("B_SPLINE")),
            ("Promoted Cardinal", lambda: self.menu("_interpolation").set("CARDINAL")),
            ("Promoted Alpha stop", lambda: self.stop(1, "value").set(0.45)),
            ("Animate last Alpha", self.animate),
            ("Add Alpha stop preserves animation", self.add),
            ("Move promoted Alpha stop", lambda: self.stop(2, "pos").set(0.15)),
            ("Remove Alpha stop preserves animation", self.remove),
            ("Frame advance", lambda: hou.setFrame(2)),
            ("Frame restore", lambda: hou.setFrame(1)),
            ("Used broken Alpha", lambda: self.stop(1, "value").set(float("nan"))),
            ("Repair promoted Alpha", lambda: self.stop(1, "value").set(0.6)),
            ("Color output", lambda: self.connect("color")),
            ("Used broken Color", lambda: self.f.mat.parm("palette1cr").set(float("nan"))),
            ("Repair promoted Color", lambda: self.f.mat.parmTuple("palette1c").set((0.7, 0.15, 0.3))),
            ("Switch to HSV", lambda: self.menu("_color_space").set("HSV")),
            ("Hue direction", lambda: self.menu("_hue_interpolation").set("CW")),
            ("Back to RGB", lambda: self.menu("_color_space").set("RGB")),
            ("Rapid promoted edits", self.rapid_edits),
            ("Alpha output", lambda: self.connect("alpha")),
            ("Finite Alpha edit", lambda: self.stop(1, "value").set(0.35)),
            ("Undo", self.undo),
            ("Redo", self.redo),
            ("Camera navigation", lambda: f.camera.parm("ty").set(0.1)),
            ("Packed object movement", lambda: self.heavy.parm("tx").set(0.5)),
            ("Rename and animate", self.rename),
        ]
        self.settings = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm, size=(550, 700))
        self.settings.setPin(True)
        self.settings.setCurrentNode(f.ramp)
        self.panel = None
        if self.native:
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.IPRViewer)
            self.tab.setRopNode(f.rop)
            self.tab.setPreview(True)
            self.tab.setAutoUpdate(True)
            self.tab.setDelay(0.02)
        else:
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(1000, 740))
            self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.report = dict(
            result="running",
            viewer="native" if self.native else "cycles",
            scope=__doc__,
            events=[],
            inline=["ramp", "alpha_ramp"],
        )
        self.index = self.generation = 0
        self.phase = "mount"
        self.entry = self.session = None
        self.began = time.monotonic()
        self.due = self.began + 0.2
        self.busy = self.finished = False
        self.callback = self.tick
        hou.ui.addEventLoopCallback(self.callback)
        if self.native:
            self.tab.startRender()

    def inline(self, node, *names):
        for name in names:
            parm = node.parm(name)
            template = parm.parmTemplate()
            assert template.type() == hou.parmTemplateType.Ramp and not template.isHidden() and not parm.isHidden(), (
                node.path(),
                name,
            )
            assert hou.parmCondType.HideWhen not in template.conditionals(), (name, template.conditionals())
        assert (
            node.parm(names[0]).parmTemplate().parmType() == hou.rampParmType.Color
            and node.parm(names[1]).parmTemplate().parmType() == hou.rampParmType.Float
        )

    def no_window(self):
        # Only the test's own floating panes may appear; never a ramp editor.
        titles = [w.windowTitle() for w in QtWidgets.QApplication.topLevelWidgets() if w.isVisible()]
        assert not any("Color Ramp" in title for title in titles), titles

    def stop(self, i, suffix):
        return (self.f.ramp if self.alpha_prefix == "alpha_ramp" else self.f.mat).parm(
            f"{self.alpha_prefix}{i}{suffix}"
        )

    def promote(self):
        before = ramps.preview_sources(self.f.ramp)
        ch.promote_parameter(self.f.mat, self.f.ramp, "ramp", name="palette", label="Palette")
        assert ramps.preview_sources(self.f.ramp) == before
        self.inline(self.f.mat, "palette", "palette_alpha")
        self.no_window()
        self.menu = lambda suffix: self.f.mat.parm("palette" + suffix)
        self.color = self.f.mat.parm("palette")
        self.alpha = self.f.mat.parm("palette_alpha")
        self.alpha_prefix = "palette_alpha"
        self.settings.setCurrentNode(self.f.mat)

    def connect(self, output):
        self.output = output
        self.f.emission.setNamedInput("color", self.f.ramp, output)

    def assert_animation(self):
        if self.animation:
            found = [
                i
                for i in range(1, int(self.alpha.evalAsInt()) + 1)
                if self.stop(i, "value").keyframes() and self.stop(i, "value").expression() == self.animation
            ]
            assert len(found) == 1, found

    def add(self):
        n = self.alpha.evalAsInt()
        self.alpha.insertMultiParmInstance(1)
        self.stop(2, "pos").set(0.12)
        self.stop(2, "value").set(0.5)
        self.stop(2, "interp").set("linear")
        assert self.alpha.evalAsInt() == n + 1
        self.assert_animation()

    def remove(self):
        n = self.alpha.evalAsInt()
        self.alpha.removeMultiParmInstance(1)
        assert self.alpha.evalAsInt() == n - 1
        self.assert_animation()

    def animate(self):
        n = self.alpha.evalAsInt()
        self.animation = ".2+.05*$F"
        self.stop(n, "value").setExpression(self.animation, hou.exprLanguage.Hscript)

    def rapid_edits(self):
        # A stale computation must never win when controls outpace rendering.
        self.rapid = True
        for i in range(12):
            self.f.mat.parmTuple("palette1c").set((0.15 + i * 0.02, 0.3, 0.65 - i * 0.02))

    def rename(self):
        self.f.ramp.setName("animated_rgba")
        hou.setFrame(3)
        self.assert_animation()

    def undo(self):
        hou.undos.performUndo()
        assert abs(self.stop(1, "value").eval() - 0.6) < 1e-6, (
            self.stop(1, "value").eval(),
            hou.undos.undoLabels()[:5],
        )

    def redo(self):
        hou.undos.performRedo()
        assert abs(self.stop(1, "value").eval() - 0.35) < 1e-6

    def save(self):
        self.path.write_text(json.dumps(self.report, indent=2))

    def tick(self):
        if self.busy or self.finished:
            return
        self.busy = True
        try:
            if self.phase == "closing":
                status = self.session.status() if self.session else None
                if status and status["state"] != "closed" and time.monotonic() < self.deadline:
                    return
                if status:
                    assert (
                        status["state"] == "closed"
                        and status["device_allocation_bytes"] == status["owned_geometry_bytes"] == 0
                    ), status
                self.report.update(result="failed" if self.error else "passed", error=self.error, closed=status)
                self.finished = True
                self.save()
                hou.ui.removeEventLoopCallback(self.callback)
                hou.ui.postEventCallback(lambda: hou.exit(1 if self.error else 0, suppress_save_prompt=True))
                return
            assert time.monotonic() - self.began < 240, (self.phase, self.index)
            if self.phase == "mount":
                if not self.native:
                    self.panel = self.tab.activeInterfaceRootWidget()
                    if self.panel is None:
                        return
                    self.panel.set_rop(self.f.rop)
                    QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                self.phase = "wait"
            if time.monotonic() < self.due:
                return
            self.no_window()
            preview = _native.ramp_preview(*ramps.preview_sources(self.f.ramp), tuple(self.f.factors), dict(raw=True))
            entry = registry.get(self.key)
            if entry is None:
                return
            self.entry = entry
            status = entry.session.status()
            info = entry.session.frame_info()
            assert not entry.error and status["state"] != "failed", (entry.error, status)
            if (
                entry.change_serial != entry.applied_serial
                or entry.requested_serial
                or not info
                or not info["complete"]
                or info["generation"] != status["generation"]
                or status.get("capture_pending")
                or status.get("pending_update")
            ):
                return
            if not self.native and (
                self.panel._live_generation != info["generation"]
                or self.panel.canvas.frame_metadata.get("sequence") != info["sequence"]
            ):
                return
            warning = bool(preview[self.output + "_issue"])
            expected = np.frombuffer(preview["linear"], np.float32).reshape(-1, 4)
            target = (
                np.array((1, 0, 1)) if warning else expected[:, :3] if self.output == "color" else expected[:, 3, None]
            )
            frame = entry.session.frame()
            try:
                error = self.f.inspect(frame, target)
            except AssertionError:
                return
            assert bool(status["source_diagnostics"]) == warning, status
            if self.native:
                if not entry.helper_pid:
                    return
                i = len(self.f.factors) // 2
                target_pixel = (
                    (1, 0, 1) if warning else expected[i, :3] if self.output == "color" else (expected[i, 3],) * 3
                )
                try:
                    if not np.allclose(
                        self.tab.pixel("C", 4 * i + 2, self.f.height // 2)[:3], target_pixel, atol=3e-5, rtol=3e-5
                    ):
                        return
                except hou.OperationFailed:
                    return
            else:
                assert not self.panel.render_button.isEnabled() and self.panel.stop_button.isEnabled()
            assert len(registry.entries()) == 1 and status["packed_instances"] == 12, status
            if self.report["events"]:
                first = self.report["events"][0]
                assert (
                    status["renderer_pid"] == first["status"]["renderer_pid"]
                    and status["geometry_conversions"] == first["status"]["geometry_conversions"]
                    and self.f.heavy.cookCount() == first["heavy_cooks"]
                ), status
                # Independent constant height must survive both source failures.
                assert status["displacement_rebuilds"] == first["status"]["displacement_rebuilds"], status
            self.assert_animation()
            self.generation = info["generation"]
            self.report["events"].append(
                dict(
                    label=self.steps[self.index][0],
                    error=error,
                    status=status,
                    heavy_cooks=self.f.heavy.cookCount(),
                    preview_issues={k: bool(preview[k]) for k in ("color_issue", "alpha_issue")},
                )
            )
            self.save()
            self.index += 1
            if self.index == len(self.steps):
                if self.panel:
                    self.panel.grab().save(str(self.path.with_suffix(".png")))
                for n, widget in enumerate(w for w in QtWidgets.QApplication.topLevelWidgets() if w.isVisible()):
                    widget.grab().save(str(self.path.with_name(self.path.stem + f"-window{n}.png")))
                self.close()
                return
            self.steps[self.index][1]()
            self.due = time.monotonic() + 0.2
        except BaseException:
            if self.phase == "closing":
                self.report.update(result="failed", error=traceback.format_exc())
                self.finished = True
                self.save()
                hou.ui.removeEventLoopCallback(self.callback)
                hou.ui.postEventCallback(lambda: hou.exit(1, suppress_save_prompt=True))
            else:
                self.close(traceback.format_exc())
        finally:
            self.busy = False

    def close(self, error=None):
        self.error = error
        self.session = self.entry.session if self.entry else None
        if self.native:
            self.tab.killRender()
        elif self.panel:
            QtTest.QTest.mouseClick(self.panel.stop_button, QtCore.Qt.LeftButton)
            self.panel.dispose()
        registry.close_all()
        self.phase = "closing"
        self.deadline = time.monotonic() + 20


if __name__ == "__main__":
    workflow = Workflow()
