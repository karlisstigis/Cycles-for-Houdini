# SPDX-License-Identifier: Apache-2.0
"""Heavy native SOP artist workflow: AOV preview, display fields, render controls."""

import pass_fixtures as pass_fixture

import os
from pathlib import Path
import runpy
import sys
import time
import traceback
import numpy as np
import hou
from PySide6 import QtCore, QtGui, QtTest, QtWidgets
from cycles_houdini import registry, object_scene, object_properties, light_properties

os.environ["CYCLES_HOUDINI_BENCH_SHARED_VIEWS"] = "0"
os.environ["CYCLES_HOUDINI_BENCH_SAMPLES"] = "1048576"
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
sys.path.insert(0, str(root / "tests"))
HeavyResponse = runpy.run_path(str(root / "tests/houdini_heavy_response_benchmark.py"), run_name="aov_fixture")[
    "HeavyResponse"
]


class AovWorkflow(HeavyResponse):
    def configure_scene(self):
        pass_fixture.set_parameters(
            self.rop,
            dict(
                viewer_interactive=1,
                ipr_samples=self.samples,
                ipr_adaptive_sampling=0,
                ipr_use_denoise=0,
                ipr_resolution_percent=100,
            ),
        )
        pass_fixture.parm(self.ground, "resolution").set(560)
        self.tint = self.stone.createNode("cycles::aov_output::1", "artist_tint")
        pass_fixture.set_parameters(self.tint, dict(name="PaintTint", value=0.17))
        self.tint.parmTuple("color").set((0.1, 0.35, 0.7))
        self.mask = self.stone.createNode("cycles::aov_output::1", "artist_mask")
        pass_fixture.set_parameters(self.mask, dict(name="NoiseMask", value=0.4))
        noise = next(node for node in self.stone.children() if node.type().name() == "cycles::noise_texture::1")
        self.mask.setInput(1, noise, noise.outputNames().index("fac"))
        pass_fixture.parm(self.rop, "aov_count").set(2)
        pass_fixture.set_parameters(
            self.rop, dict(aov_name1="PaintTint", aov_type1="color", aov_name2="NoiseMask", aov_type2="value")
        )
        object_properties.install(self.obj)
        pass_fixture.parm(self.obj, "cycles_lightgroup").set("FX")
        pass_fixture.parm(self.obj, "cycles_pass_index").set(13)
        pass_fixture.set_parameters(self.rop, dict(pass_object_id=True, pass_material_id=True, material_pass_index=7))
        self.lights = object_scene.selected(self.rop, hou.time(), lights=True)
        assert len(self.lights) == 1
        light_properties.install(self.lights[0])
        pass_fixture.parm(self.lights[0], "cycles_lightgroup").set("Key")
        pass_fixture.parm(self.rop, "background_lightgroup").set("World")
        pass_fixture.set_parameters(
            self.rop, dict(crypto_object=True, crypto_material=True, crypto_asset=True, crypto_levels=6)
        )
        pass_fixture.parm(self.rop, "lightgroup_count").set(4)
        for index, name in enumerate(("Key", "FX", "World", "Unused"), 1):
            pass_fixture.parm(self.rop, "lightgroup_name" + str(index)).set(name)
        self.group_passes = ["lightgroup_Key", "lightgroup_FX", "lightgroup_World"]
        self.display_checks = []
        self.expected_generation = 0

    def select_pass(self, name):
        menu = self.panel.pass_menu
        index = menu.findData(name)
        assert index >= 0, name
        menu.setFocus()
        QtTest.QTest.keyClick(menu, QtCore.Qt.Key_Home)
        for _ in range(index):
            QtTest.QTest.keyClick(menu, QtCore.Qt.Key_Down)
        assert menu.currentData() == name

    def start_drag(self, control):
        assert isinstance(control.input, hou.qt.InputField)
        line = self.panel.exposure_label if control is self.panel.exposure else self.panel.gamma_label
        self.drag_control, self.drag_line = control, line
        self.drag_initial = control.value()
        self.drag_start = QtCore.QPoint(18, line.height() // 2)
        self.drag_image = self.panel.canvas.image.copy()
        QtTest.QTest.mouseMove(line, self.drag_start, delay=20)
        QtTest.QTest.mousePress(line, QtCore.Qt.LeftButton, pos=self.drag_start)

    def move_drag(self, distance):
        target = self.drag_start + QtCore.QPoint(distance, 0)
        move = QtGui.QMouseEvent(
            QtCore.QEvent.MouseMove,
            QtCore.QPointF(target),
            QtCore.QPointF(self.drag_line.mapToGlobal(target)),
            QtCore.Qt.NoButton,
            QtCore.Qt.LeftButton,
            QtCore.Qt.NoModifier,
        )
        QtWidgets.QApplication.sendEvent(self.drag_line, move)

    def finish_drag(self):
        QtTest.QTest.mouseRelease(self.drag_line, QtCore.Qt.LeftButton, pos=self.drag_start + QtCore.QPoint(50, 0))
        assert self.drag_control.value() != self.drag_initial, (
            "Horizontal label drag did not change the native field",
            self.drag_initial,
            self.drag_control.value(),
        )
        self.display_checks.append(dict(before=self.drag_initial, after=self.drag_control.value()))
        self.panel.poll()
        assert self.panel.canvas.image != self.drag_image

    def tick(self):
        if self.tick_busy:
            return
        self.tick_busy = True
        try:
            if time.monotonic() - self.began > 180:
                raise TimeoutError("Native AOV/display workflow stalled")
            entry = registry.get(self.key)
            if entry is None:
                return
            status = entry.session.status()
            if entry.error or status["state"] == "failed":
                raise RuntimeError(entry.error or status["error"])
            frame = entry.session.frame_info()
            if not frame:
                return
            self.panel.poll()
            self.report.update(phase=self.stage, current=status, frame=frame)
            self.save()
            if self.stage == "initial":
                if "NoiseMask" not in frame["enabled_passes"] or status["state"] != "rendering":
                    return
                assert (
                    not self.panel.render_button.isEnabled()
                    and self.panel.pause_button.isEnabled()
                    and self.panel.stop_button.isEnabled()
                )
                from panel_navigation_checks import check_layout

                check_layout(self.panel)
                self.expected_generation = status["generation"]
                self.conversions = status["geometry_conversions"]
                self.select_pass("NoiseMask")
                self.stage = "scalar-preview"
            elif self.stage == "scalar-preview":
                if "NoiseMask" not in frame["passes"] or self.panel._settings_dirty:
                    return
                assert not frame["complete"] and status["generation"] == self.expected_generation
                assert (
                    not self.panel.exposure.isEnabled()
                    and not self.panel.gamma.isEnabled()
                    and not self.panel.channels.isEnabled()
                )
                self.panel.grab().save(str(self.path.with_suffix(".mask.png")))
                self.select_pass("PaintTint")
                self.stage = "color-preview"
            elif self.stage == "color-preview":
                if "PaintTint" not in frame["passes"] or self.panel._settings_dirty:
                    return
                assert (
                    self.panel.channels.isEnabled() and self.panel.exposure.isEnabled() and self.panel.gamma.isEnabled()
                )
                self.panel.channels.setFocus()
                QtTest.QTest.keyClick(self.panel.channels, QtCore.Qt.Key_End)
                assert self.panel.channels.currentData() == "alpha"
                self.stage = "color-alpha"
            elif self.stage == "color-alpha":
                if self.panel._settings_dirty:
                    return
                assert (
                    not self.panel.exposure.isEnabled()
                    and not self.panel.gamma.isEnabled()
                    and not self.panel.display.isEnabled()
                )
                assert status["generation"] == self.expected_generation
                self.panel.channels.setFocus()
                QtTest.QTest.keyClick(self.panel.channels, QtCore.Qt.Key_Home)
                self.select_pass(self.group_passes[0])
                self.stage = "group-preview"
            elif self.stage == "group-preview":
                name = self.group_passes[0]
                if name not in frame["passes"] or self.panel._settings_dirty:
                    return
                assert frame["pass_metadata"][name]["channels"] == ["R", "G", "B"] and self.panel.gamma.isEnabled()
                assert not self.panel.channels.model().item(self.panel.channels.findData("alpha")).isEnabled()
                raw = entry.session.frame()["planes"][name]["pixels"]
                assert np.frombuffer(raw, np.float32).max() > 0
                assert status["generation"] == self.expected_generation
                self.group_passes.pop(0)
                if self.group_passes:
                    self.select_pass(self.group_passes[0])
                else:
                    self.select_pass("CryptoObject00")
                    self.stage = "crypto-preview"
            elif self.stage == "crypto-preview":
                if "CryptoObject00" not in frame["passes"] or self.panel._settings_dirty:
                    return
                assert frame["pass_metadata"]["CryptoObject00"]["cryptomatte"]
                assert (
                    not self.panel.gamma.isEnabled()
                    and not self.panel.channels.isEnabled()
                    and not self.panel.display.isEnabled()
                )
                assert status["generation"] == self.expected_generation
                assert entry.session.output_metadata() == {}
                self.panel.grab().save(str(self.path.with_suffix(".crypto.png")))
                self.select_pass("object_id")
                self.stage = "index-preview"
            elif self.stage == "index-preview":
                if "object_id" not in frame["passes"] or self.panel._settings_dirty:
                    return
                assert (
                    not self.panel.gamma.isEnabled()
                    and not self.panel.channels.isEnabled()
                    and not self.panel.display.isEnabled()
                )
                assert status["generation"] == self.expected_generation
                values = np.frombuffer(entry.session.frame()["planes"]["object_id"]["pixels"], np.float32)
                assert set(np.unique(values)) <= {0, 13} and np.count_nonzero(values == 13) > 0
                self.panel.grab().save(str(self.path.with_suffix(".index.png")))
                if status["state"] == "rendering":
                    self.displacement_rebuilds = status["displacement_rebuilds"]
                    pass_fixture.parm(self.lights[0], "cycles_lightgroup").set("World")
                    self.stage = "regrouping"
            elif self.stage == "regrouping":
                if status["generation"] <= self.expected_generation or frame["generation"] != status["generation"]:
                    return
                assert (
                    status["geometry_conversions"] == self.conversions
                    and status["displacement_rebuilds"] == self.displacement_rebuilds
                )
                self.expected_generation = status["generation"]
                self.select_pass("lightgroup_Key")
                self.stage = "empty-key"
            elif self.stage == "empty-key":
                if "lightgroup_Key" not in frame["passes"] or self.panel._settings_dirty:
                    return
                assert (
                    np.count_nonzero(
                        np.frombuffer(entry.session.frame()["planes"]["lightgroup_Key"]["pixels"], np.float32)
                    )
                    == 0
                )
                self.select_pass("PaintTint")
                self.stage = "restored-color"
            elif self.stage == "restored-color":
                if "PaintTint" not in frame["passes"] or self.panel._settings_dirty:
                    return
                QtTest.QTest.mouseClick(self.panel.pause_button, QtCore.Qt.LeftButton)
                self.stage = "paused"
            elif self.stage == "paused":
                if status["state"] != "paused":
                    return
                self.start_drag(self.panel.exposure)
                self.stage = "exposure-origin"
            elif self.stage in ("exposure-origin", "gamma-origin"):
                self.move_drag(0)
                self.stage = self.stage.replace("origin", "move")
            elif self.stage in ("exposure-move", "gamma-move"):
                self.move_drag(50)
                self.stage = self.stage.replace("move", "finish")
            elif self.stage == "exposure-finish":
                if self.panel._settings_dirty:
                    return
                self.finish_drag()
                self.start_drag(self.panel.gamma)
                self.stage = "gamma-origin"
            elif self.stage == "gamma-finish":
                if self.panel._settings_dirty:
                    return
                self.finish_drag()
                assert status["generation"] == self.expected_generation
                from panel_navigation_checks import check_navigation

                self.report["navigation"] = check_navigation(self.panel)
                self.panel.grab().save(str(self.path.with_suffix(".display.png")))
                QtTest.QTest.mouseClick(self.panel.stop_button, QtCore.Qt.LeftButton)
                self.stage = "stopped"
            elif self.stage == "stopped":
                if status["state"] != "cancelled":
                    return
                assert (
                    self.panel.render_button.isEnabled()
                    and not self.panel.pause_button.isEnabled()
                    and not self.panel.stop_button.isEnabled()
                )
                pass_fixture.set_parameters(
                    self.rop, dict(viewer_interactive=0, samples=16, adaptive_sampling=0, aov_type1="value")
                )
                self.next_at = time.monotonic() + 0.3
                self.stage = "stays-stopped"
            elif self.stage == "stays-stopped":
                if time.monotonic() < self.next_at:
                    return
                assert entry.session.status()["state"] == "cancelled", (
                    "Parameter edits restarted an explicitly stopped render"
                )
                QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                self.stage = "resumed-final"
            elif self.stage == "resumed-final":
                if (
                    not frame["complete"]
                    or status["generation"] <= self.expected_generation
                    or self.panel.canvas.generation != status["generation"]
                ):
                    return
                assert status["geometry_conversions"] == self.conversions
                assert not frame["pass_metadata"]["PaintTint"]["color"] and not self.panel.gamma.isEnabled()
                assert len(registry.entries()) == 1 and entry.views == 1 and self.viewer is None
                # Reaching the sample budget retains the artist's rendering mode.
                assert (
                    not self.panel.render_button.isEnabled()
                    and self.panel.pause_button.isEnabled()
                    and self.panel.stop_button.isEnabled()
                )
                entry.session.write_exr(str(self.path.with_suffix(".exr")))
                import json

                metadata = entry.session.output_metadata()
                for suffix in ("Object", "Material", "Asset"):
                    prefix = next(
                        key[:-4]
                        for key, value in metadata.items()
                        if key.endswith("/name") and value == "Crypto" + suffix
                    )
                    names = json.loads(metadata[prefix + "manifest"])
                    assert names
                    if suffix == "Object":
                        assert self.obj.path() in names and any("/packed:" in name for name in names)
                self.select_pass("combined")
                self.panel.exposure.setValue(0)
                self.panel.gamma.setValue(1)
                self.panel.canvas.fit_image()
                self.panel.poll()
                self.final_status = status
                self.stage = "final-preview"
            elif self.stage == "final-preview":
                if self.panel._settings_dirty:
                    return
                self.panel.grab().save(str(self.path.with_suffix(".final.png")))
                self.report.update(
                    last_status=self.final_status,
                    display_drags=self.display_checks,
                    checks="Progressive Color/Value AOV, RGB light-group and Cryptomatte previews; alpha and raw-data control eligibility; completed mixed/packed manifests; live light regrouping without geometry conversion/displacement rebuild; horizontal label drags and zoom; Stop/Pause/Render; stopped edits and live AOV type change",
                )
                self.finish()
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


def main():
    global workflow
    workflow = AovWorkflow()


if __name__ == "__main__":
    main()
