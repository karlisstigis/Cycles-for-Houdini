# SPDX-License-Identifier: Apache-2.0
"""Actual native IPR/panel dependency updates for geometry-assigned materials."""

from support.paths import artifacts_root

from cycles_houdini.device_selection import selected_devices as rop_devices
from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import os
from pathlib import Path
import time
import traceback

import hou
import numpy as np
from PySide6 import QtGui
import cycles_houdini as ch
from cycles_houdini import registry, native_scene
from ipr_update_checks import recovered_update
import native_window_capture


class AssignmentLifecycle:
    def __init__(self):
        root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
        hou.hipFile.load(
            str(artifacts_root() / "stage3/materials/assignments/native-material-assignments.hiplc"),
            suppress_save_prompt=True,
        )
        hou.setFrame(1)
        self.rop = hou.node("/out/native_assignment_cycles")
        self.obj = hou.node("/obj/assignment_receiver")
        self.assignment = self.obj.node("primitive_materials")
        self.detail = self.obj.node("detail_fallback")
        self.red = hou.node("/mat/assigned_red")
        self.blue = hou.node("/mat/assigned_blue")
        self.green = hou.node("/mat/assigned_green")
        self.colors = {
            "red": (0.8, 0.1, 0.05),
            "blue": (0.05, 0.1, 0.8),
            "green": (0.05, 0.7, 0.1),
            "fallback": (0.4, 0.1, 0.5),
        }
        set_rop_devices(self.rop, os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.rop.parm("samples").set(65536)
        self.key = f"rop:{self.rop.sessionId()}"
        self.report = dict(device=rop_devices(self.rop, hou.time())[0], host_pid=os.getpid(), events=[])
        self.started = time.monotonic()
        self.last_tick = 0.0
        self.stage = 0
        self.generation = 0
        self.presentation_proof = os.environ.get("CYCLES_NATIVE_PRESENTATION_PROOF") == "1"
        self.presentation_ready = {}
        self.viewer = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.IPRViewer)
        self.viewer.setRopNode(self.rop)
        self.viewer.setPreview(True)
        self.viewer.setAutoUpdate(True)
        self.viewer.setDelay(0.02)
        self._tick = self.tick
        hou.ui.addEventLoopCallback(self._tick)
        self.viewer.startRender()
        self.save()

    def save(self):
        Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"]).write_text(json.dumps(self.report, indent=2), encoding="utf-8")

    def image(self, entry, names, gains=None, newer=True):
        if not entry or entry.error:
            return False
        status = entry.session.status()
        frame = entry.session.frame_info()
        if newer and status["generation"] <= self.generation:
            return False
        if not frame or frame["generation"] != status["generation"]:
            return False
        gains = gains or (1, 1, 1, 1)
        try:
            actual = [self.viewer.pixel("C", x, y)[:3] for x, y in ((24, 24), (72, 24), (24, 72), (72, 72))]
        except hou.OperationFailed:
            return False
        return all(
            abs(a - v * gain) < 0.001
            for pixel, name, gain in zip(actual, names, gains)
            for a, v in zip(pixel, self.colors[name])
        )

    def record(self, name, entry, reuse=False):
        if reuse:
            assert entry.session.status()["geometry_conversions"] == self.conversions
            assert entry.session.status()["material_binding_scans"] == self.scans
        self.report["events"].append(
            dict(
                name=name,
                seconds=time.monotonic() - self.started,
                status=entry.session.status(),
                native_materials=entry.session.native_material_references(),
                soho=entry.last_soho.copy(),
                helper_pid=entry.helper_pid,
            )
        )
        self.save()

    def advance(self, stage, entry):
        state = entry.session.status()
        self.generation = state["generation"]
        self.conversions = state["geometry_conversions"]
        self.scans = state["material_binding_scans"]
        self.stage = stage

    def failed(self, entry, fragment):
        return recovered_update(entry, self.generation, fragment)

    def fresh_fallback_control(self, entry):
        """Compare delivered native pixels with a fresh scene at the same edit."""
        config = native_scene.configuration(self.rop)
        with ch.Session(
            device=config[0],
            width=config[1],
            height=config[2],
            samples=config[3],
            interactive=True,
            execution="sync",
            threads=int(config[6]),
            use_texture_cache=bool(config[4]),
            auto_texture_cache=bool(config[5]),
        ) as fresh:
            frame = fresh.wait(fresh.apply(native_scene.capture(self.rop)), timeout=30)
            plane = frame["planes"]["combined"]
            rgb = np.frombuffer(plane["pixels"], np.float32).reshape(frame["height"], frame["width"], 4)
            expected = [rgb[y, x, :3].tolist() for x, y in ((24, 24), (72, 24), (24, 72), (72, 72))]
        native = [list(self.viewer.pixel("C", x, y)[:3]) for x, y in ((24, 24), (72, 24), (24, 72), (72, 72))]
        assert np.allclose(native, expected, rtol=0, atol=0.001), (native, expected)
        self.report["obj_fallback_fresh_control"] = dict(
            native=native, fresh=expected, authored=self.obj.evalParm("shop_materialpath")
        )

    def presented(self, label, blue_majority):
        if not self.presentation_proof:
            return True
        if label not in self.presentation_ready:
            self.viewer.floatingPanel().setName("cycles_native_assignment_proof")
            self.viewer.floatingPanel().setPosition((30, 30))
            native_window_capture.show(self.viewer)
            self.presentation_ready[label] = time.monotonic() + 0.5
            return False
        if time.monotonic() < self.presentation_ready[label]:
            return False
        try:
            image, _, window = native_window_capture.capture(self.viewer)
        except native_window_capture.WindowOccluded as error:
            self.report["presentation_unavailable"] = str(error)
            native_window_capture.show(self.viewer)
            self.presentation_ready[label] = time.monotonic() + 0.5
            return False
        image = image.convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
        array = np.frombuffer(image.bits(), np.uint8).reshape(image.height(), image.bytesPerLine())
        rgb = array[:, : image.width() * 4].reshape(image.height(), image.width(), 4)[:, :, :3].astype(np.int16)
        rgb = rgb[
            int(image.height() * 0.25) : int(image.height() * 0.90),
            int(image.width() * 0.15) : int(image.width() * 0.85),
        ]
        green = int(((rgb[:, :, 1] > rgb[:, :, 0] + 30) & (rgb[:, :, 1] > rgb[:, :, 2] + 30)).sum())
        blue = int(((rgb[:, :, 2] > rgb[:, :, 0] + 30) & (rgb[:, :, 2] > rgb[:, :, 1] + 30)).sum())
        if min(green, blue) < 200 or (blue <= green * 2 if blue_majority else green <= blue * 1.5):
            self.report["presentation_waiting"] = dict(label=label, green=green, blue=blue)
            return False  # Uniform or retained old pixels cannot qualify.
        path = self.path if hasattr(self, "path") else Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        image_path = path.with_name(label + "-native-desktop.png")
        assert image.save(str(image_path))
        self.report.setdefault("native_presentation", {})[label] = dict(
            window=window, path=str(image_path), green_pixels=green, blue_pixels=blue
        )
        return True

    def tick(self):
        if time.monotonic() - self.last_tick < 0.1:
            return
        self.last_tick = time.monotonic()
        try:
            entry = registry.get(self.key)
            self.report["current"] = dict(
                stage=self.stage,
                error=entry.error if entry else "",
                status=entry.session.status() if entry else None,
                rop_errors=self.rop.errors(),
            )
            self.save()
            if time.monotonic() - self.started > 90:
                raise TimeoutError("Assigned-material native IPR stalled")
            if self.stage == 0:
                if (
                    not entry
                    or not entry.helper_pid
                    or not self.image(entry, ("red", "blue", "green", "green"), newer=False)
                ):
                    return
                if not self.presented("initial-assignment", blue_majority=False):
                    return
                assert entry.last_soho["host_pid"] == os.getpid() and entry.session.status()["material_resources"] == 4
                self.record("Material SOP subsets and OBJ fallback render in native IPR", entry)
                before_panel = entry.session.status()
                self.panel_tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel)
                self.panel_tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
                self.panel = self.panel_tab.activeInterfaceRootWidget()
                self.panel.set_rop(self.rop)
                assert self.panel.entry is entry and len(registry.entries()) == 1
                after_panel = entry.session.status()
                for name in (
                    "generation",
                    "geometry_resources",
                    "material_resources",
                    "engine_shader_resources",
                    "owned_geometry_bytes",
                ):
                    assert after_panel[name] == before_panel[name], (name, before_panel, after_panel)
                entry.session.cancel()
                quiescent = entry.session.status()
                self.panel.set_rop(self.rop)
                assert self.panel.entry is entry and len(registry.entries()) == 1
                assert entry.session.status()["device_allocation_bytes"] == quiescent["device_allocation_bytes"]
                self.report["panel_memory"] = dict(
                    active_before=before_panel,
                    active_after=after_panel,
                    quiescent_before=quiescent,
                    quiescent_after=entry.session.status(),
                )
                self.record("Assigned materials share one renderer across both viewers", entry)
                self.advance(1, entry)
                with hou.undos.group("Assigned red emission edit"):
                    self.red.node("surface_shader").parm("strength").set(0.5)
            elif self.stage == 1:
                if not self.image(entry, ("red", "blue", "green", "green"), (0.5, 1, 1, 1)):
                    return
                self.record(
                    "Assigned shader parameter updates without geometry conversion or binding scan", entry, reuse=True
                )
                self.advance(2, entry)
                hou.undos.performUndo()
            elif self.stage == 2:
                if not self.image(entry, ("red", "blue", "green", "green")):
                    return
                self.record("Assigned shader undo", entry, reuse=True)
                self.advance(3, entry)
                hou.undos.performRedo()
            elif self.stage == 3:
                if not self.image(entry, ("red", "blue", "green", "green"), (0.5, 1, 1, 1)):
                    return
                self.record("Assigned shader redo", entry, reuse=True)
                self.advance(4, entry)
                self.assignment.parm("shop_materialpath1").set(self.green.path())
            elif self.stage == 4:
                if not self.image(entry, ("green", "blue", "green", "green")):
                    return
                assert entry.session.status()["material_resources"] == 3
                self.record("Primitive reassignment retires the now unused red shader", entry)
                self.advance(5, entry)
                self.obj.parm("shop_materialpath").set(self.blue.path())
            elif self.stage == 5:
                if not self.image(entry, ("green", "blue", "blue", "blue")):
                    return
                if not self.presented("obj-fallback-blue", blue_majority=True):
                    return
                self.fresh_fallback_control(entry)
                self.record("OBJ material edit updates unassigned primitives", entry)
                self.advance(6, entry)
                self.obj.parm("shop_materialpath").set(self.green.path())
            elif self.stage == 6:
                if not self.image(entry, ("green", "blue", "green", "green")):
                    return
                self.record("Restoring the OBJ fallback recovers the previous bindings", entry)
                self.advance(7, entry)
                self.blue.node("surface_output").setInput(0, None)
            elif self.stage == 7:
                if not self.failed(entry, "Connect a surface closure"):
                    return
                self.record("Invalid assigned graph renders a local diagnostic material", entry)
                self.stage = 8
                self.blue.node("surface_output").setNamedInput("surface", self.blue.node("surface_shader"), "emission")
            elif self.stage == 8:
                if not self.image(entry, ("green", "blue", "green", "green")):
                    return
                self.record("Assigned graph rewiring automatically recovers both viewers", entry)
                self.advance(9, entry)
                self.old_blue_key = next(
                    r["key"] for r in entry.session.native_material_references() if r["path"] == self.blue.path()
                )
                # Keep an actual stale path in geometry. Material SOP itself
                # skips missing paths and reports its own host warning.
                self.detail.parm("decorator").set("g.prim(1).setAttribValue('shop_materialpath','/mat/assigned_blue')")
                self.blue.destroy()
            elif self.stage == 9:
                if not self.failed(entry, "Missing Cycles material"):
                    return
                self.record("Deleted geometry binding renders a diagnostic material in the new image", entry)
                self.stage = 10
                self.blue = ch.create_material(name="assigned_blue", shader="emission")
                self.blue.node("surface_shader").parmTuple("color").set(self.colors["blue"])
                self.blue.node("surface_shader").parm("strength").set(1.0)
            elif self.stage == 10:
                if not self.image(entry, ("green", "blue", "green", "green")):
                    return
                assert self.old_blue_key not in {r["key"] for r in entry.session.native_material_references()}
                self.record("Nearest-parent creation notification recovers a material with a new identity", entry)
                self.advance(11, entry)
                self.detail.parm("decorator").set("")
                self.blue.setName("renamed_assigned_blue")
            elif self.stage == 11:
                if not self.image(entry, ("green", "blue", "green", "green")):
                    return
                assert self.assignment.evalParm("shop_materialpath2") == self.blue.path()
                assert any(r["path"] == self.blue.path() for r in entry.session.native_material_references())
                self.record("Native material rename follows Houdini operator path references", entry)
                self.advance(12, entry)
                self.rop.parm("use_material_assignments").set(0)
            elif self.stage == 12:
                if not self.image(entry, ("fallback",) * 4):
                    return
                assert (
                    not entry.session.native_material_references() and entry.session.status()["material_resources"] == 1
                )
                self.record("Uniform ROP controls retire all native assignment resources", entry)
                self.advance(13, entry)
                self.rop.parm("use_material_assignments").set(1)
            elif self.stage == 13:
                if not self.image(entry, ("green", "blue", "green", "green")):
                    return
                self.record("Native assignment mode restores graph authoring without a second session", entry)
                self.advance(14, entry)
                self.assignment.parm("shop_materialpath1").set(self.red.path())
            elif self.stage == 14:
                if not self.image(entry, ("red", "blue", "green", "green"), (0.5, 1, 1, 1)):
                    return
                self.record("Reassigning a retired material uses its current parameter values", entry)
                self.panel.poll()
                preview = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"]).parent / "assigned-material-panel.png"
                assert self.panel.grab().save(str(preview))
                self.report["panel_preview"] = str(preview)
                self.advance(15, entry)
                self.viewer.killRender()
                self.viewer.close()
            elif self.stage == 15:
                if entry.helper_pid or entry.channel:
                    return
                assert registry.get(self.key) is entry and entry.views == 1
                self.record("Panel retains assigned materials after the native viewer closes", entry)
                self.stage = 16
                self.panel.render()
            elif self.stage == 16:
                frame = entry.session.frame_info()
                if not frame or frame["generation"] <= self.generation:
                    return
                self.record("Panel continues native material rendering independently", entry)
                self.finish(0)
        except Exception:
            self.report["error"] = traceback.format_exc()
            self.finish(1)

    def finish(self, code):
        self.report["result"] = "passed" if code == 0 else "failed"
        self.save()
        hou.ui.removeEventLoopCallback(self._tick)
        try:
            self.viewer.killRender()
            self.viewer.close()
        except (hou.ObjectWasDeleted, hou.OperationFailed):
            pass
        if hasattr(self, "panel"):
            self.panel.dispose()
            try:
                self.panel_tab.close()
            except (hou.ObjectWasDeleted, hou.OperationFailed):
                pass
        registry.close_all()
        self._exit_code = code
        self._exit_at = time.monotonic() + 0.5
        self._shutdown = self.shutdown
        hou.ui.addEventLoopCallback(self._shutdown)

    def shutdown(self):
        if time.monotonic() >= self._exit_at:
            hou.ui.removeEventLoopCallback(self._shutdown)
            hou.ui.postEventCallback(lambda: self.exit(self._exit_code))

    def exit(self, code):
        try:
            hou.exit(code, suppress_save_prompt=True)
        except SystemExit:
            pass


test = AssignmentLifecycle()
