# SPDX-License-Identifier: Apache-2.0
"""Capture real native hull drawing, then await an explicit visual verdict.

Run with scripts/run-gui-test.ps1. Only owned fixture assets and floating windows
are created. Inspect the captured viewport/parameter widgets and write the
adjacent .verdict.json with result passed/failed to finish the isolated host.
"""

import json
import os
from pathlib import Path
import subprocess
import time
import traceback

import hou
from PySide6 import QtGui

from cycles_houdini import blender_assets
from cycles_houdini.blender import blend_bounds, blend_catalogue, node_bounds


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.verdict = self.path.with_suffix(".verdict.json")
        self.report = dict(result="running", captures=[], owned_assets=True, native_viewport=True)
        self.fixture = self.path.parent / "assets" / "hull-fixture.blend"
        executable = str(blender_assets._blender())
        command = [
            executable,
            "--background",
            "--factory-startup",
            "--disable-autoexec",
            "--python-exit-code",
            "1",
            "--python",
            str(Path(__file__).with_name(getattr(self, "fixture_script", "blend_reference_hulls_fixture.py"))),
            "--",
            "--out",
            str(self.fixture),
        ]
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "OCIO")}
        with (self.path.parent / "fixture.log").open("w") as log:
            subprocess.run(
                command,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=True,
                timeout=60,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        self.geo = hou.node("/obj").createNode("geo", "owned_reference_hull_acceptance", run_init_scripts=False)
        self.node = self.geo.createNode("cycles::blender_reference")
        assert self.node.evalParm("ref_frame1") == 1
        self.node.setParms(dict(file=self.fixture.as_posix(), ref_target1="Hull_Left", viewport_shape="hull"))
        self.node.setDisplayFlag(True)
        self.node.setRenderFlag(True)
        self.node.setSelected(True, clear_all_selected=True)
        self.viewer = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.SceneViewer, size=(1200, 850))
        self.viewer.setPwd(self.geo)
        self.viewer.curViewport().changeType(hou.geometryViewportType.Front)
        self.viewer.curViewport().frameBoundingBox(hou.BoundingBox(-3.5, -1.8, -1.8, 3.5, 1.8, 1.8))
        self.parms = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm, size=(620, 850))
        self.parms.setCurrentNode(self.node)
        self.parms.setPin(True)
        screens = QtGui.QGuiApplication.screens()
        screen = next((s for s in screens if "UP2720Q" in s.name()), None)
        if screen is None:
            screen = next(
                (
                    s
                    for s in screens
                    if (round(s.size().width() * s.devicePixelRatio()), round(s.size().height() * s.devicePixelRatio()))
                    == (3840, 2160)
                ),
                None,
            )
        if screen is None:
            raise RuntimeError("The requested main 4K monitor is unavailable; no viewport screenshot was taken")
        area = screen.availableGeometry()
        self.windows = [hou.qt.floatingPanelWindow(p.floatingPanel()) for p in (self.viewer, self.parms)]
        for window, x, width in zip(self.windows, (30, 1260), (1200, 620)):
            window.showNormal()
            if window.windowHandle():
                window.windowHandle().setScreen(screen)
            window.setGeometry(area.left() + x, area.top() + 70, width, min(850, area.height() - 140))
        self.report.update(
            screen=screen.name(),
            selected_screen_geometry=list(screen.geometry().getRect()),
            verdict_file=str(self.verdict),
            fixture=str(self.fixture),
        )
        self.stage = 0
        self.phase = "catalogue"
        self.job = blend_catalogue.request_catalogues(self.node)
        self.started = time.monotonic()
        self.due = 0
        hou.ui.addEventLoopCallback(self.tick)

    def save(self):
        self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")

    def capture(self, label):
        # Cook in the preceding phase, then allow a viewport redraw before
        # grabbing. A screenshot taken immediately after cook can show the
        # previous GPU state and conceal a repeated-cook display regression.
        geometry = self.node.geometry()
        assert not self.node.errors(), self.node.errors()
        primitives = geometry.prims()
        assert len(primitives) == (2 if label == "appended_object" else 1)
        assert len({p.points()[0].number() for p in primitives}) == len(primitives)
        assert all(p.intrinsicValue("cycles_blender_viewport_hull_points") > 3 for p in primitives)
        pieces = [len(json.loads(p.intrinsicValue("cycles_blender_viewport_hull"))["pieces"]) for p in primitives]
        assert pieces == ([2] if label == "collection_members" else [1] * len(primitives)), pieces
        images = []
        for name, window in zip(("viewport", "parameters"), self.windows):
            filename = self.path.with_suffix("." + label + "." + name + ".png")
            assert window.grab().save(str(filename))
            images.append(str(filename))
        self.report["captures"].append(
            dict(
                stage=label,
                images=images,
                targets=[p.intrinsicValue("cycles_blend_target") for p in primitives],
                hull_points=[p.intrinsicValue("cycles_blender_viewport_hull_points") for p in primitives],
                hull_pieces=[
                    len(json.loads(p.intrinsicValue("cycles_blender_viewport_hull"))["pieces"]) for p in primitives
                ],
                source_frames=[p.intrinsicValue("cycles_blender_frame") for p in primitives],
                distinct_points=len({p.points()[0].number() for p in primitives}),
                warnings=self.node.warnings(),
            )
        )
        self.save()

    def tick(self):
        try:
            if time.monotonic() - self.started > 200:
                raise TimeoutError("Hull GUI acceptance timed out")
            if self.phase == "review":
                if not self.verdict.is_file():
                    return
                verdict = json.loads(self.verdict.read_text(encoding="utf-8-sig"))
                if verdict.get("result") not in ("passed", "failed"):
                    return
                self.report.update(result=verdict["result"], visual_verdict=verdict)
                self.save()
                hou.ui.removeEventLoopCallback(self.tick)
                hou.exit(0 if verdict["result"] == "passed" else 1, suppress_save_prompt=True)
            elif self.phase == "catalogue":
                blend_catalogue.drain_catalogues()
                if self.job["done"].is_set():
                    assert not self.job["error"], self.job["error"]
                    self.job = blend_bounds.refresh_node_bounds(self.node)
                    self.phase = "refresh"
            elif self.phase == "refresh":
                if self.job.done.is_set():
                    self.job.publish()
                    self.node.cook(force=True)
                    self.phase, self.due = "capture", time.monotonic() + 1.5
            elif self.phase == "capture" and time.monotonic() >= self.due:
                labels = (
                    "single_loaded",
                    "single_after_validation",
                    "single_repeated_cook",
                    "appended_object",
                    "collection_whole",
                    "collection_members",
                )
                self.capture(labels[self.stage])
                self.stage += 1
                if self.stage == 1:
                    self.phase, self.due = "validation", time.monotonic() + 3
                elif self.stage == 2:
                    self.remaining_cooks = 3
                    self.phase, self.due = "repeat", time.monotonic() + 0.5
                elif self.stage == 3:
                    self.node.parm("references").set(2)
                    blend_catalogue.initialize_rows(self.node)
                    assert self.node.evalParm("ref_frame2") == 1
                    self.node.parm("ref_target2").set("Hull_Right")
                    self.job = blend_bounds.refresh_node_bounds(self.node)
                    self.phase = "refresh"
                elif self.stage == 4:
                    self.node.parm("references").set(1)
                    self.node.setParms(dict(ref_target_kind1="collection", ref_target1="Hull_Collection"))
                    self.job = blend_bounds.refresh_node_bounds(self.node)
                    self.phase = "refresh"
                elif self.stage == 5:
                    self.node.parm("ref_hull_per_member1").set(1)
                    self.node.cook(force=True)
                    self.phase, self.due = "capture", time.monotonic() + 1.5
                else:
                    self.report.update(
                        result="ready",
                        pending_visual_checks=[
                            "Single shaded convex hull remains visible after dependency validation",
                            "Single shaded convex hull remains visible after three additional forced cooks",
                            "Appended row shows two separated shaded hulls with separate packed points",
                            "Whole collection hull bridges both objects",
                            "Hull per member shows two separated shaded hulls",
                            "Both fresh row Source Frame fields display 1 with a slider",
                        ],
                    )
                    self.save()
                    self.phase = "review"
            elif self.phase == "repeat" and time.monotonic() >= self.due:
                self.node.cook(force=True)
                self.remaining_cooks -= 1
                self.phase = "capture" if not self.remaining_cooks else "repeat"
                self.due = time.monotonic() + 0.5
            elif self.phase == "validation":
                node_bounds.drain_pending_node_bounds()
                if time.monotonic() >= self.due and not any(s["pending"] for s in node_bounds._subscribers.values()):
                    self.node.cook(force=True)
                    self.phase, self.due = "capture", time.monotonic() + 0.5
        except Exception:
            self.report.update(result="failed", error=traceback.format_exc())
            self.save()
            hou.ui.removeEventLoopCallback(self.tick)
            hou.exit(1, suppress_save_prompt=True)


if __name__ == "__main__":
    workflow = Workflow()
