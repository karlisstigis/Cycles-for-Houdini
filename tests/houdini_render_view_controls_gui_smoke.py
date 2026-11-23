# SPDX-License-Identifier: Apache-2.0
"""Exercise the native Render View and ROP without opening the Cycles panel."""

import json
import math
import os
from pathlib import Path
import time
import traceback

import hou
import pass_fixtures
from cycles_houdini import registry
from cycles_houdini.device_selection import set_devices


def hscript(command):
    output, error = hou.hscript(command)
    if error.strip():
        raise RuntimeError(f"{command}: {error}")
    return output.split()


class RenderViewControls:
    def __init__(self):
        self.report = {"events": []}
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.deadline = time.monotonic() + 60
        self.stage = "first image"
        geo = hou.node("/obj").createNode("geo", run_init_scripts=False)
        box = geo.createNode("box")
        box.setRenderFlag(True)
        hou.setFrame(1)
        camera = hou.node("/obj").createNode("cam")
        camera.parm("tz").setExpression("4 + $F", hou.exprLanguage.Hscript)
        self.rop = hou.node("/out").createNode("cycles_ipr")
        pass_fixtures.set_parameters(
            self.rop, dict(soppath=box.path(), camera=camera.path(), resx=96, resy=64, pass_depth=1, ipr_samples=4)
        )
        set_devices(self.rop, os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.key = f"rop:{self.rop.sessionId()}"
        self.previous_views = set(hscript("imgviewls -r"))
        self.viewer = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.IPRViewer)
        self.viewer.setRopNode(self.rop)
        self.viewer.setPreview(True)
        self.viewer.setAutoUpdate(True)
        self.viewer.startRender()
        self.callback = self.tick
        hou.ui.addEventLoopCallback(self.callback)

    def tick(self):
        try:
            if time.monotonic() > self.deadline:
                raise TimeoutError(f"Render View timed out during {self.stage}: {self.rop.errors()}")
            entry = registry.get(self.key)
            if self.stage == "stop":
                if entry is None and not self.viewer.isRendering():
                    self.report["events"].append("Render View Stop retired the ROP session")
                    self.finish(0)
                return
            if not entry:
                self.report["last_state"] = {"entries": [item.key for item in registry.entries()]}
                return
            if entry.error:
                raise RuntimeError(entry.error)
            frame = entry.session.frame_info()
            status = entry.session.status()
            self.report["last_state"] = dict(
                stage=self.stage,
                state=status["state"],
                generation=status["generation"],
                soho=entry.last_soho.get("mode"),
                helper=entry.helper_pid,
            )
            if not frame:
                return
            if self.stage == "first image":
                try:
                    planes = self.viewer.planes()
                    self.report["last_state"]["planes"] = list(planes)
                except hou.OperationFailed:
                    return
                if "depth" not in planes or "C" not in planes:
                    return
                new_views = set(hscript("imgviewls -r")) - self.previous_views
                assert len(new_views) == 1, new_views
                self.image_view = new_views.pop()
                hscript(f"imgview -p depth 0 {self.image_view}")
                self.stage = "depth selection"
            elif self.stage == "depth selection":
                if self.viewer.displayedPlane() != "depth":
                    return
                depth = self.viewer.pixel("depth", 48, 32)[0]
                assert math.isclose(depth, 4.5, abs_tol=0.001), depth
                self.report["events"].append({"frame": 1, "selected_plane": "depth", "center_depth": depth})
                hou.setFrame(2)
                self.stage = "animated depth"
            elif self.stage == "animated depth":
                depth = self.viewer.pixel("depth", 48, 32)[0]
                if not math.isclose(depth, 5.5, abs_tol=0.001):
                    return
                self.report["events"].append({"frame": 2, "center_depth": depth})
                self.generation = frame["generation"]
                self.rop.parmTuple("render_region_bounds").set((0.25, 0.75, 0.25, 0.75))
                self.rop.parm("render_region").set(1)
                self.stage = "cropped image"
            elif self.stage == "cropped image":
                bounds = tuple(frame[key] for key in ("x", "y", "width", "height"))
                if frame["generation"] <= self.generation or bounds != (24, 16, 48, 32):
                    return
                if self.viewer.imageResolution() != (96, 64):
                    return
                hscript(f"imgview -p C 0 {self.image_view}")
                self.report["events"].append({"rop_region": bounds, "generation": frame["generation"]})
                self.stage = "beauty selection"
            elif self.stage == "beauty selection" and self.viewer.displayedPlane() == "C":
                self.report["events"].append("switched back to Beauty")
                self.viewer.killRender()
                self.stage = "stop"
        except Exception:
            self.report["error"] = traceback.format_exc()
            self.finish(1)

    def finish(self, code):
        hou.ui.removeEventLoopCallback(self.callback)
        self.report["result"] = "failed" if code else "passed"
        self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")
        registry.close_all()
        hou.ui.postEventCallback(lambda: hou.exit(code, suppress_save_prompt=True))


trial = RenderViewControls()
