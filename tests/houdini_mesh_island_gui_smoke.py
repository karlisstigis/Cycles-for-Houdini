# SPDX-License-Identifier: Apache-2.0
"""One live viewer: topology/shader repair and navigation beside 12 heavy packed copies.

HOM edits actual native shader/SOP/OBJ/camera parameters; Qt clicks Render/Stop.
This checks viewer integration, not physical mouse input or latency budgets.
"""

from subdivision_test_setup import configure_render, set_subdivision
import json, os, time, traceback
from pathlib import Path
import hou
import numpy as np
from PySide6 import QtCore, QtTest
from cycles_houdini import registry
from mesh_island_fixture import Fixture


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.native = os.environ.get("CYCLES_HOUDINI_ISLAND_VIEWER") == "native"
        self.f = f = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        f.add_heavy()
        self.key = f"rop:{f.rop.sessionId()}"
        f.width = int(os.environ.get("CYCLES_HOUDINI_ISLAND_WIDTH", f.width))
        f.height = int(os.environ.get("CYCLES_HOUDINI_ISLAND_HEIGHT", f.height))
        configure_render(f.rop, dict(resx=f.width, resy=f.height))
        self.trace = os.environ.get("CYCLES_HOUDINI_ISLAND_TRACE") == "1" and not self.native
        self.paint_saved = self.paint_type = self.heartbeat = None
        self.paint_error = None
        self.painted = {}
        self.before_revision = None
        self.input_at = time.monotonic()
        self.action_seconds = 0.0
        if self.native:
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.IPRViewer)
            self.tab.setRopNode(f.rop)
            self.tab.setPreview(True)
            self.tab.setAutoUpdate(True)
            self.tab.setDelay(0.02)
        else:
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(1000, 740))
            self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.steps = [
            ("Initial", lambda: None, {}),
            ("Camera move", lambda: f.camera.parm("tx").set(0.02), {}),
            ("Object move", lambda: f.obj.parm("tx").set(0.02), {}),
            ("Connectivity join", lambda: f.set_mesh(joined=True), dict(joined=True)),
            ("Connectivity repair", lambda: f.set_mesh(), {}),
            ("Mixed points and hair", lambda: f.output.parm("input").set(3), dict(warning=True)),
            ("Mesh repair", lambda: f.output.parm("input").set(0), {}),
            ("Unsupported shader output", lambda: f.shade("unsupported"), dict(warning=True, recover_all=True)),
            ("Shader repair", lambda: f.shade(), {}),
            ("Displacement with seams", lambda: (f.set_mesh(seams=True), f.displace()), dict(displaced=True)),
            ("Displacement removed", lambda: (f.displace(False), f.set_mesh()), {}),
            ("Packed copies", lambda: f.output.parm("input").set(4), dict(packed=True)),
            ("Unpacked mesh", lambda: f.output.parm("input").set(0), {}),
            (
                "Enable subdivision",
                lambda: configure_render(
                    f.rop, dict(subdivision=dict(type="linear", space="object", dicing_rate=0.15, max_level=2))
                ),
                {},
            ),
            ("Broken control winding", lambda: f.set_mesh(fault="winding"), dict(warning=True)),
            ("Navigate with recovered subdivision", lambda: f.camera.parm("tx").set(0.03), dict(warning=True)),
            ("Repair control winding", lambda: f.set_mesh(), {}),
            ("Nonmanifold control mesh", lambda: f.set_mesh(fault="nonmanifold"), dict(warning=True)),
            ("Repair nonmanifold mesh", lambda: f.set_mesh(), {}),
            ("Repeated control point", lambda: f.set_mesh(fault="repeated_point"), dict(warning=True)),
            ("Repair repeated point", lambda: f.set_mesh(), {}),
            ("Disable subdivision", lambda: set_subdivision(f.rop, type="none"), {}),
        ]
        self.index = self.generation = 0
        self.phase = "mount"
        self.busy = self.finished = False
        self.began = self.edited = time.monotonic()
        self.panel = self.entry = self.session = self.baseline = None
        self.report = dict(result="running", events=[], viewer="native" if self.native else "cycles", scope=__doc__)
        self.report.update(resolution=[f.width, f.height], paint_trace_enabled=self.trace, heartbeat_gaps=[])
        hou.hipFile.save(str(f.dest / ("islands-" + self.report["viewer"] + ".hiplc")))
        hou.ui.addEventLoopCallback(self.tick)
        if self.native:
            self.tab.startRender()

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
                hou.ui.removeEventLoopCallback(self.tick)
                hou.ui.postEventCallback(lambda: hou.exit(1 if self.error else 0, suppress_save_prompt=True))
                return
            assert time.monotonic() - self.began < 220, (self.phase, self.index)
            assert self.paint_error is None, self.paint_error
            if self.phase == "mount":
                if not self.native:
                    self.panel = self.tab.activeInterfaceRootWidget()
                    if self.panel is None:
                        return
                    if self.trace:
                        self.paint_type = type(self.panel.canvas)
                        self.paint_saved = self.paint_type.paintEvent
                        paint_original = self.paint_saved

                        def paint(canvas, event):
                            try:
                                paint_original(canvas, event)
                                if canvas is not self.panel.canvas:
                                    return
                                meta = getattr(canvas, "frame_metadata", {})
                                revision = meta.get("render_revision", meta.get("generation"))
                                if (
                                    revision is not None
                                    and revision != self.before_revision
                                    and meta.get("generation", 0) > self.generation
                                ):
                                    self.painted.setdefault(
                                        self.index,
                                        dict(
                                            at=time.monotonic(),
                                            generation=meta["generation"],
                                            revision=revision,
                                            sequence=meta.get("sequence"),
                                            rendered_width=meta.get("rendered_width"),
                                            rendered_height=meta.get("rendered_height"),
                                        ),
                                    )
                            except BaseException:
                                self.paint_error = traceback.format_exc()

                        self.paint_type.paintEvent = paint
                        self.last_beat = time.monotonic()
                        self.heartbeat = QtCore.QTimer(hou.qt.mainWindow())
                        self.heartbeat.setInterval(16)

                        def beat():
                            now = time.monotonic()
                            self.report["heartbeat_gaps"].append(dict(index=self.index, seconds=now - self.last_beat))
                            self.last_beat = now

                        self.heartbeat.timeout.connect(beat)
                        self.heartbeat.start()
                    self.input_at = time.monotonic()
                    self.panel.set_rop(self.f.rop)
                    QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                self.phase = "wait"
            entry = registry.get(self.key)
            if entry is None:
                return
            self.entry = entry
            status = entry.session.status()
            info = entry.session.frame_info()
            self.report["last_wait_state"] = dict(
                index=self.index, change_serial=entry.change_serial, applied_serial=entry.applied_serial, status=status
            )
            assert not entry.error and status["state"] != "failed", (entry.error, status)
            if (
                entry.change_serial != entry.applied_serial
                or entry.requested_serial
                or time.monotonic() - self.edited < 0.3
            ):
                return
            if (
                status.get("pending_update")
                or status.get("capture_pending")
                or not info
                or not info["complete"]
                or info["generation"] <= self.generation
                or info["generation"] != status["generation"]
            ):
                return
            if not self.native and (
                self.panel._live_generation != info["generation"]
                or self.panel.canvas.frame_metadata.get("sequence") != info["sequence"]
            ):
                return
            if self.trace and self.index not in self.painted:
                return
            label, _, expect = self.steps[self.index]
            expect = expect.copy()
            recover_all = expect.pop("recover_all", False)
            frame = entry.session.frame()
            if recover_all:
                assert status["source_diagnostics"], status
                p = frame["planes"]["combined"]
                image = np.frombuffer(p["pixels"], np.float32).reshape(self.f.height, self.f.width, p["channels"])
                visible = image[:, :, 3] > 0.999
                assert visible.sum() > 500
                np.testing.assert_allclose(
                    image[visible, :3], np.broadcast_to((1, 0, 1), image[visible, :3].shape), atol=5e-5, rtol=0
                )
                values = None
            else:
                if self.baseline is not None and not expect.get("joined"):
                    expect["expected"] = self.baseline
                values = self.f.inspect(frame, status, **expect)
                if self.baseline is None:
                    self.baseline = values
            assert len(registry.entries()) == 1
            if self.native:
                if not entry.helper_pid:
                    return
                p = frame["planes"]["combined"]
                image = np.frombuffer(p["pixels"], np.float32).reshape(self.f.height, self.f.width, p["channels"])
                try:
                    x, y = self.f.width // 2, self.f.height // 2
                    if not np.allclose(self.tab.pixel("C", x, y)[:3], image[y, x, :3], atol=3e-4, rtol=0):
                        return
                except hou.OperationFailed:
                    return
            else:
                assert (
                    entry.views == 1 and not self.panel.render_button.isEnabled() and self.panel.stop_button.isEnabled()
                )
            if self.report["events"]:
                previous = self.report["events"][-1]["status"]
                first = self.report["events"][0]
                assert (
                    status["renderer_pid"] == first["status"]["renderer_pid"]
                    and self.f.heavy.cookCount() == first["heavy_cooks"]
                ), status
                if self.index in (1, 2, 7, 8, 15):
                    assert status["geometry_conversions"] == previous["geometry_conversions"], status
            self.generation = info["generation"]
            event = dict(label=label, values=values, status=status, heavy_cooks=self.f.heavy.cookCount())
            if self.trace:
                event.update(
                    first_paint=self.painted[self.index],
                    input_to_first_paint_seconds=self.painted[self.index]["at"] - self.input_at,
                    parameter_action_seconds=self.action_seconds,
                    diagnostics=entry.session.diagnostics(),
                )
            self.report["events"].append(event)
            self.save()
            self.index += 1
            if self.index == len(self.steps):
                if self.panel:
                    self.panel.grab().save(str(self.path.with_suffix(".png")))
                self.close()
                return
            self.before_revision = self.panel.canvas.frame_metadata.get("render_revision") if self.panel else None
            self.input_at = time.monotonic()
            self.steps[self.index][1]()
            self.edited = time.monotonic()
            self.action_seconds = self.edited - self.input_at
        except BaseException:
            if self.phase == "closing":
                self.report.update(result="failed", error=traceback.format_exc())
                self.finished = True
                self.save()
                hou.ui.removeEventLoopCallback(self.tick)
                hou.ui.postEventCallback(lambda: hou.exit(1, suppress_save_prompt=True))
            else:
                self.close(traceback.format_exc())
        finally:
            self.busy = False

    def close(self, error=None):
        if self.heartbeat:
            self.heartbeat.stop()
        if self.paint_type and self.paint_saved:
            self.paint_type.paintEvent = self.paint_saved
            self.paint_type = self.paint_saved = None
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


workflow = Workflow()
