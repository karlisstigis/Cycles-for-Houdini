# SPDX-License-Identifier: Apache-2.0
"""One live Cycles Viewer: native packed-field edits/repair with a retained million-triangle prototype.

HOM changes actual SOP/OBJ parameters; Qt clicks Render/Stop. This is a viewer
integration check, not a physical-input or end-to-end latency certification.
"""

import json, os, time, traceback, struct
from pathlib import Path
import hou
import numpy as np
from PySide6 import QtCore, QtTest
import cycles_houdini as ch
from cycles_houdini import registry, _native
from packed_instance_attribute_fixture import Fixture


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.native = os.environ.get("CYCLES_HOUDINI_PACKED_FIELDS_VIEWER") == "native"
        self.f = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.heavy_geo = self.f.add_heavy()
        self.key = f"rop:{self.f.rop.sessionId()}"
        self.principled = ch.create_material(name="principled_layout_check").node("surface_shader")
        layout = _native.vop_connector_layout(self.principled.sessionId())
        groups = [p["label"] for p in layout if p["group"]]
        assert groups[0] == "Surface" and "Other" not in groups, layout
        self.settings = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm, size=(650, 600))
        self.settings.setPin(True)
        self.settings.setCurrentNode(self.principled)
        if self.native:
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.IPRViewer)
            self.tab.setRopNode(self.f.rop)
            self.tab.setPreview(True)
            self.tab.setAutoUpdate(True)
            self.tab.setDelay(0.02)
        else:
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(1000, 740))
            self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.steps = [
            ("Initial", lambda: None, False),
            ("Float edit", lambda: self.f.set_fields(offset=0.1), False),
            ("Float repair", lambda: self.f.set_fields(), False),
            ("Camera move", lambda: self.f.camera.parm("tx").set(0.05), False),
            ("Object move", lambda: self.f.obj.parm("tx").set(0.05), False),
            ("Bad one", lambda: self.f.set_fields("bad_one"), True),
            ("Repair", lambda: self.f.set_fields(), False),
            ("Bad type", lambda: self.f.set_fields("bad_type"), True),
            ("Repair type", lambda: self.f.set_fields(), False),
            ("Missing", lambda: self.f.set_fields("missing"), True),
            ("Repair missing", lambda: self.f.set_fields(), False),
            ("Packed point fields", lambda: self.f.set_fields(owner="point"), False),
            ("Primitive fields", lambda: self.f.set_fields(), False),
            ("Vector primitive", lambda: self.vector_fields(), False),
            ("Vector edit", lambda: self.vector_fields(offset=0.05), False),
            ("Vector point", lambda: self.vector_fields(owner="point", offset=0.05), False),
            ("Bad vector component", lambda: self.vector_fields(mode="bad_vector"), True),
            ("Vector repair", lambda: self.vector_fields(), False),
            ("Two component vector", lambda: self.vector_fields(mode="float2"), False),
            ("Two component edit", lambda: self.vector_fields(mode="float2", offset=0.07), False),
        ]
        self.index = self.generation = 0
        self.panel = self.entry = self.session = None
        self.phase = "mount"
        self.busy = self.finished = False
        self.began = self.edited_at = time.monotonic()
        self.report = dict(
            result="running",
            events=[],
            viewer="native" if self.native else "cycles",
            principled_layout=layout,
            scope=__doc__,
        )
        hou.ui.addEventLoopCallback(self.tick)
        if self.native:
            self.tab.startRender()

    def save(self):
        self.path.write_text(json.dumps(self.report, indent=2))

    def vector_fields(self, mode="vector", owner="primitive", offset=0):
        self.f.emission.setNamedInput("color", self.f.rand, "vector")
        self.f.set_fields(mode, owner=owner, offset=offset)

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
            if self.phase == "mount":
                if not self.native:
                    self.panel = self.tab.activeInterfaceRootWidget()
                    if self.panel is None:
                        return
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
                index=self.index,
                change_serial=entry.change_serial,
                applied_serial=entry.applied_serial,
                requested_serial=entry.requested_serial,
                status=status,
            )
            assert not entry.error and status["state"] != "failed", (entry.error, status)
            if (
                entry.change_serial != entry.applied_serial
                or entry.requested_serial
                or time.monotonic() - self.edited_at < 0.3
            ):
                return
            unchanged_owner = self.steps[self.index][0] in ("Packed point fields", "Primitive fields", "Vector point")
            if (
                status.get("pending_update")
                or status.get("capture_pending")
                or not info
                or not info["complete"]
                or info["generation"] < self.generation
                or (info["generation"] == self.generation and not unchanged_owner)
                or info["generation"] != status["generation"]
            ):
                return
            if not self.native and (
                self.panel._live_generation != info["generation"]
                or self.panel.canvas.frame_metadata.get("sequence") != info["sequence"]
            ):
                return
            label, _, warning = self.steps[self.index]
            colors = self.f.colors(entry.session.frame(), warning=warning)
            assert bool(status["source_diagnostics"]) == warning, status["source_diagnostics"]
            assert status["packed_instances"] == 16 and len(registry.entries()) == 1, status
            if self.native:
                if not entry.helper_pid:
                    return
                p = entry.session.frame()["planes"]["combined"]
                pixels = np.frombuffer(p["pixels"], np.float32).reshape(self.f.height, self.f.width, p["channels"])
                try:
                    if not np.allclose(self.tab.pixel("C", 43, 25)[:3], pixels[25, 43, :3], atol=3e-4, rtol=0):
                        return
                except hou.OperationFailed:
                    return
            else:
                assert (
                    entry.views == 1 and not self.panel.render_button.isEnabled() and self.panel.stop_button.isEnabled()
                )
            if self.report["events"]:
                before = self.report["events"][0]["status"]
                assert status["renderer_pid"] == before["renderer_pid"] and self.f.heavy.cookCount() == self.cooks, (
                    status
                )
                if self.steps[self.index][0] in (
                    "Float edit",
                    "Float repair",
                    "Camera move",
                    "Object move",
                    "Packed point fields",
                    "Primitive fields",
                    "Vector edit",
                    "Vector point",
                    "Two component edit",
                ):
                    assert (
                        status["geometry_conversions"] == self.report["events"][-1]["status"]["geometry_conversions"]
                    ), status
            self.cooks = self.f.heavy.cookCount()
            self.generation = info["generation"]
            self.report["events"].append(dict(label=label, status=status, colors=colors, heavy_cooks=self.cooks))
            self.save()
            self.index += 1
            if self.index == len(self.steps):
                if self.panel:
                    self.panel.grab().save(str(self.path.with_suffix(".png")))
                hou.qt.floatingPanelWindow(self.settings.floatingPanel()).grab().save(
                    str(self.path.with_name(self.path.stem + "-principled.png"))
                )
                self.close()
                return
            self.steps[self.index][1]()
            self.edited_at = time.monotonic()
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
        if error and self.entry:
            source = self.f.fields.geometry()
            self.report["failure_source"] = dict(
                snippet=self.f.fields.evalParm("snippet"),
                rand=[p.attribValue("rand") for p in source.prims()] if source.findPrimAttrib("rand") else None,
            )
            acknowledged = self.entry.session._controller.acknowledged
            if acknowledged:
                data = _native.encode_snapshot(acknowledged)
                values = []
                start = 0
                token = b"rand" + struct.pack("<i", 1)
                while (index := data.find(token, start)) >= 0:
                    start = index + len(token)
                    if len(data) - start >= 12:
                        values.append(struct.unpack_from("<3f", data, start))
                self.report["failure_owned_rand_candidates"] = values
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
