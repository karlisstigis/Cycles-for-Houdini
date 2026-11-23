# SPDX-License-Identifier: Apache-2.0
"""Heavy SOP IPR: status stability, real mouse/key navigation and cropped output."""

import os
from pathlib import Path
import runpy
import sys
import time
import traceback
import hou
from PySide6 import QtCore, QtTest
from cycles_houdini import registry

os.environ["CYCLES_HOUDINI_BENCH_SHARED_VIEWS"] = "0"
os.environ["CYCLES_HOUDINI_BENCH_SAMPLES"] = "16"
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
sys.path.insert(0, str(root / "tests"))
from panel_navigation_checks import check_navigation, draw_region

HeavyResponse = runpy.run_path(
    str(root / "tests/houdini_heavy_response_benchmark.py"), run_name="panel_navigation_fixture"
)["HeavyResponse"]


class NavigationWorkflow(HeavyResponse):
    def configure_scene(self):
        self.ground.parm("resolution").set(560)

    def tick(self):
        if self.tick_busy:
            return
        self.tick_busy = True
        try:
            if time.monotonic() - self.began > 165:
                raise TimeoutError("Image navigation workflow stalled")
            entry = registry.get(self.key)
            if entry is None:
                return
            status = entry.session.status()
            if entry.error or status["state"] == "failed":
                raise RuntimeError(entry.error or status["error"])
            frame = entry.session.frame_info()
            self.report["current"] = dict(stage=self.stage, status=status, frame=frame)
            self.save()
            if (
                not frame
                or not frame["complete"]
                or frame["generation"] != status["generation"]
                or self.panel.canvas.generation != status["generation"]
                or status["generation"] <= self.before
            ):
                return
            assert (frame["full_width"], frame["full_height"]) == (960, 540)
            if self.stage == "initial":
                assert self.report["fixture"]["mesh_triangles"] >= 1000000
                self.report["navigation"] = check_navigation(self.panel)
                self.panel.grab().save(str(self.path.with_suffix(".100percent.png")))
                self.rectangle = self.panel.canvas.image_rect()
                self.previous = entry.session.status()
                self.before = self.previous["generation"]
                self.stage = "camera-update"
                self.camera.parm("tx").set(self.camera.evalParm("tx") + 0.05)
            elif self.stage == "camera-update":
                assert self.panel.canvas.image_rect() == self.rectangle, "Scene update reset image navigation"
                assert entry.session.status()["geometry_conversions"] == self.previous["geometry_conversions"]
                self.before = entry.session.status()["generation"]
                self.stage = "region"
                self.expected_region = draw_region(self.panel)
            elif self.stage == "region":
                actual = tuple(frame[name] for name in ("x", "y", "width", "height"))
                assert actual == self.expected_region, (actual, self.expected_region)
                self.report["region"] = dict(expected=self.expected_region, actual=actual)
                self.before = entry.session.status()["generation"]
                self.stage = "full-frame"
                assert self.panel.region.isChecked(), "Shift-drag must enable Region"
                QtTest.QTest.mouseClick(self.panel.region, QtCore.Qt.LeftButton)
            elif self.stage == "full-frame":
                assert tuple(frame[name] for name in ("x", "y", "width", "height")) == (0, 0, 960, 540)
                assert not self.panel.region.isChecked() and not self.panel.canvas.region_enabled
                QtTest.QTest.mouseClick(self.panel.fit_button, QtCore.Qt.LeftButton)
                self.panel.grab().save(str(self.path.with_suffix(".fit.png")))
                self.report["last_status"] = entry.session.status()
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
    test = NavigationWorkflow()
