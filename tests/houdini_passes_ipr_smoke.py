# SPDX-License-Identifier: Apache-2.0
"""Artist selects light/data/volume AOVs during a real million-triangle SOP IPR."""

import pass_fixtures as pass_fixture

import os
from pathlib import Path
import runpy
import time
import traceback
import numpy as np
import hou
from PySide6 import QtCore, QtTest
from cycles_houdini import registry
from cycles_houdini.passes import catalogue

os.environ["CYCLES_HOUDINI_BENCH_SHARED_VIEWS"] = "0"
os.environ["CYCLES_HOUDINI_BENCH_SAMPLES"] = "65536"
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
HeavyResponse = runpy.run_path(str(root / "tests/houdini_heavy_response_benchmark.py"), run_name="passes_fixture")[
    "HeavyResponse"
]


class PassWorkflow(HeavyResponse):
    def configure_scene(self):
        pass_fixture.parm(self.ground, "resolution").set(560)
        for descriptor in catalogue():
            parm = pass_fixture.parm(self.rop, "pass_" + descriptor["id"])
            if parm:
                parm.set(1)
        self.expected_passes = {"combined"} | {
            descriptor["id"] for descriptor in catalogue() if pass_fixture.parm(self.rop, "pass_" + descriptor["id"])
        }
        self.inspect = ["diffuse_direct", "normal", "volume_direct", "sample_count"]
        self.inspect_index = 0

    def select_pass(self, name):
        menu = self.panel.pass_menu
        index = menu.findData(name)
        assert index >= 0, name
        QtTest.QTest.mouseClick(menu, QtCore.Qt.LeftButton)
        QtTest.QTest.keyClick(menu, QtCore.Qt.Key_Home)
        for _ in range(index):
            QtTest.QTest.keyClick(menu, QtCore.Qt.Key_Down)
        QtTest.QTest.keyClick(menu, QtCore.Qt.Key_Return)
        assert menu.currentData() == name, (menu.currentData(), name)

    def tick(self):
        if self.tick_busy:
            return
        self.tick_busy = True
        try:
            if time.monotonic() - self.began > 165:
                raise TimeoutError("Progressive pass workflow stalled")
            entry = registry.get(self.key)
            if entry is None:
                return
            status = entry.session.status()
            frame = entry.session.frame_info()
            self.report["current"] = dict(stage=self.stage, status=status, frame=frame)
            self.save()
            if entry.error or status["state"] == "failed":
                raise RuntimeError(entry.error or status["error"])
            if (
                not frame
                or frame["generation"] != status["generation"]
                or self.panel.canvas.generation != status["generation"]
            ):
                return
            assert not frame["complete"], "AOV interaction happened only after rendering completed"
            if self.stage == "initial":
                assert self.report["fixture"]["mesh_triangles"] >= 1000000
                assert set(frame["enabled_passes"]) == self.expected_passes, (
                    frame["enabled_passes"],
                    self.expected_passes,
                )
                self.generation = status["generation"]
                self.converted = status["geometry_conversions"]
                self.session = entry.session
                self.rectangle = self.panel.canvas.image_rect()
                self.report["pass_selections"] = []
                self.stage = "inspect"
                self.select_pass(self.inspect[0])
            elif self.stage == "inspect":
                name = self.inspect[self.inspect_index]
                if name not in frame["passes"] or self.panel._sequence < frame["sequence"]:
                    return
                assert status["generation"] == self.generation
                assert status["geometry_conversions"] == self.converted
                assert entry.session is self.session
                assert self.panel.canvas.image_rect() == self.rectangle
                if name == "volume_direct":
                    pixels = np.frombuffer(entry.session.frame()["planes"][name]["pixels"], np.float32)
                    energy = float(pixels.max())
                    assert energy > 0, "Pyro must contribute to the inspected volume pass"
                    self.report["volume_direct_maximum"] = energy
                self.panel.grab().save(str(self.path.with_suffix("." + name + ".png")))
                self.report["pass_selections"].append(
                    dict(pass_id=name, generation=status["generation"], samples=status["current_sample"])
                )
                self.inspect_index += 1
                if self.inspect_index < len(self.inspect):
                    self.select_pass(self.inspect[self.inspect_index])
                else:
                    self.stage = "disable-selected"
                    self.before = self.generation
                    pass_fixture.parm(self.rop, "pass_sample_count").set(0)
            elif self.stage == "disable-selected":
                if status["generation"] <= self.before or "sample_count" in frame["enabled_passes"]:
                    return
                assert self.panel.pass_menu.findData("sample_count") < 0
                assert self.panel.pass_menu.currentData() == "combined"
                assert status["geometry_conversions"] == self.converted
                assert entry.session is self.session
                self.report["pass_change"] = dict(
                    generation=status["generation"],
                    geometry_conversions=status["geometry_conversions"],
                    session="retained",
                    selected="combined",
                    diagnostics=entry.session.diagnostics(),
                )
                self.finish()
                return
            self.save()
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


if __name__ == "__main__":
    test = PassWorkflow()
