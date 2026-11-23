# SPDX-License-Identifier: Apache-2.0
"""Prove raw Cycles, native imdisplay and composited Render View agree after edits."""

import ctypes
import json
import os
from pathlib import Path
import time
import traceback

import hou
import numpy as np
from PySide6 import QtCore, QtGui
import cycles_houdini as ch
from cycles_houdini import registry
from cycles_houdini.device_selection import set_devices
import native_window_capture


class Trial:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.report = dict(result="running", scope=__doc__, events=[])
        self.started = time.monotonic()
        self.stage = "initial"
        self.generation = -1
        self.ready_at = None
        self.mapped = False
        self.foreground = ctypes.windll.user32.GetForegroundWindow()
        obj = hou.node("/obj").createNode("geo", "presented_reference", run_init_scripts=False)
        box = obj.createNode("box")
        box.setRenderFlag(True)
        box.setDisplayFlag(True)
        self.material = ch.create_material(name="presented_reference", shader="emission")
        self.shader = self.material.node("surface_shader")
        self.shader.parm("strength").set(1)
        self.shader.parmTuple("color").set((0.8, 0.02, 0.01))
        obj.parm("shop_materialpath").set(self.material.path())
        camera = hou.node("/obj").createNode("cam", "presented_camera")
        camera.setParms(dict(tz=5, projection=1, orthowidth=3))
        self.rop = hou.node("/out").createNode("cycles_ipr", "presented_reference")
        self.rop.setParms(
            dict(
                source_mode="objects",
                vobject=obj.path(),
                soppath=box.path(),
                camera=camera.path(),
                resx=320,
                resy=240,
                ipr_samples=8,
                ipr_use_denoise=0,
            )
        )
        set_devices(self.rop, os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.key = f"rop:{self.rop.sessionId()}"
        self.viewer = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.IPRViewer, size=(950, 750))
        self.viewer.floatingPanel().setName("cycles_native_presentation_probe")
        self.viewer.floatingPanel().setPosition((30, 30))
        self.viewer.setRopNode(self.rop)
        self.viewer.setPreview(True)
        self.viewer.setAutoUpdate(True)
        self.viewer.startRender()
        self.callback = self.tick
        hou.ui.addEventLoopCallback(self.callback)

    def capture(self, label):
        # QWidget.grab invokes QWidget rendering; QScreen grabs the composited
        # desktop. Keep both: GL pane rendering can be absent from QWidget.grab.
        desktop, widget, result = native_window_capture.capture(self.viewer)
        result["qt_widget_available"] = widget is not None
        for name, source in (("desktop", desktop), ("widget", widget)):
            if source is None:
                continue
            image = source.convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
            assert not image.isNull(), f"{name} capture is empty"
            destination = self.path.with_name(f"{label}-{name}.png")
            assert image.save(str(destination))
            pixels = np.frombuffer(image.bits(), np.uint8).reshape(image.height(), image.bytesPerLine())
            rgba = pixels[:, : image.width() * 4].reshape(image.height(), image.width(), 4)
            roi = rgba[
                int(image.height() * 0.25) : int(image.height() * 0.90),
                int(image.width() * 0.15) : int(image.width() * 0.85),
                :3,
            ].astype(np.int16)
            red = (roi[:, :, 0] > roi[:, :, 1] + 30) & (roi[:, :, 0] > roi[:, :, 2] + 30)
            green = (roi[:, :, 1] > roi[:, :, 0] + 30) & (roi[:, :, 1] > roi[:, :, 2] + 30)
            result[name] = dict(
                path=str(destination),
                size=[image.width(), image.height()],
                red_pixels=int(red.sum()),
                green_pixels=int(green.sum()),
                rgb_std=float(roi.std()),
                unique_colors=int(len(np.unique(roi.reshape(-1, 3), axis=0))),
            )
        return result

    def tick(self):
        try:
            assert time.monotonic() - self.started < 100, self.report
            entry = registry.get(self.key)
            if self.stage == "stop":
                if entry is not None or self.viewer.isRendering():
                    return
                self.report["events"].append(dict(action="stop", registry_retired=True))
                self.shader.parmTuple("color").set((0.8, 0.02, 0.01))
                self.rop.parm("source_mode").set("sop")
                self.viewer.startRender()
                self.stage, self.generation, self.ready_at = "restart", -1, None
                return
            if not entry:
                return
            if not self.mapped:
                try:
                    self.report["owned_window_mapping"] = native_window_capture.show(self.viewer)
                except AssertionError:
                    if time.monotonic() - self.started < 5:
                        return  # Native window creation follows the pane API.
                    raise
                assert self.report["owned_window_mapping"]["visible"], self.report["owned_window_mapping"]
                self.mapped = True
            status = entry.session.status()
            info = entry.session.frame_info()
            self.report["last"] = dict(
                stage=self.stage,
                error=entry.error,
                status=status,
                frame=info,
                socket_port=entry.socket_port,
                rop_errors=self.rop.errors(),
            )
            assert not entry.error, entry.error
            if not info or info["generation"] != status["generation"] or info["generation"] <= self.generation:
                return
            expected = (0.01, 0.8, 0.02) if self.stage == "edit" else (0.8, 0.02, 0.01)
            frame = entry.session.frame()
            rgba = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).reshape(240, 320, 4)
            raw = rgba[120, 160, :3]
            native = self.viewer.pixel("C", 160, 120)[:3]
            self.report["raw_center"] = raw.tolist()
            self.report["native_center"] = list(native)
            if not np.allclose(raw, expected, atol=0.003) or not np.allclose(native, expected, atol=0.003):
                return
            assert entry.socket_port, "No native imdisplay delivery socket"
            if self.ready_at is None:
                self.ready_at = time.monotonic() + 1.5
                return
            if time.monotonic() < self.ready_at:
                return
            try:
                capture = self.capture(self.stage)
            except native_window_capture.WindowOccluded as error:
                self.report["presentation_unavailable"] = str(error)
                native_window_capture.show(self.viewer)
                self.ready_at = time.monotonic() + 0.5
                return
            self.report["events"].append(
                dict(
                    action=self.stage,
                    generation=info["generation"],
                    source_mode=self.rop.evalParm("source_mode"),
                    engine_rgb=raw.tolist(),
                    imdisplay_rgb=list(native),
                    socket_port=entry.socket_port,
                    capture=capture,
                )
            )
            self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")
            measured = capture["desktop"]
            assert capture["visible"], "Owned native test pane is not mapped"
            color = "green_pixels" if self.stage == "edit" else "red_pixels"
            assert measured[color] > 2000, (
                self.stage,
                "raw and imdisplay agree but composited pane is missing the known image",
                capture,
            )
            assert measured["unique_colors"] > 20, capture
            if self.stage == "initial":
                self.generation = info["generation"]
                self.shader.parmTuple("color").set((0.01, 0.8, 0.02))
                self.stage, self.ready_at = "edit", None
            elif self.stage == "edit":
                assert measured["red_pixels"] < measured["green_pixels"] // 3, "Stale red reference remains"
                self.viewer.killRender()
                self.stage = "stop"
            else:
                self.finish()
        except Exception:
            self.finish(traceback.format_exc())

    def finish(self, error=None):
        hou.ui.removeEventLoopCallback(self.callback)
        self.report.update(result="failed" if error else "passed", error=error, seconds=time.monotonic() - self.started)
        self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")
        self.viewer.killRender()
        registry.close_all()
        if self.foreground:
            ctypes.windll.user32.SetForegroundWindow(self.foreground)
        hou.ui.postEventCallback(lambda: hou.exit(1 if error else 0, suppress_save_prompt=True))


trial = Trial()
