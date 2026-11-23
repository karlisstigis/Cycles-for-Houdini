# SPDX-License-Identifier: Apache-2.0
"""Actual Houdini IPR subdivision updates, undo, failure recovery and panel sharing."""

from support.paths import artifacts_root

from subdivision_test_setup import configure_render, set_subdivision
from cycles_houdini.device_selection import selected_devices as rop_devices
from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import os
from pathlib import Path
import time
import traceback
import hou
import OpenImageIO as OIIO
from cycles_houdini import registry, passes


class SubdivisionLifecycle:
    def __init__(self):
        root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
        hou.hipFile.load(
            str(artifacts_root() / "stage2/subdivision/native-subdivision.hiplc"), suppress_save_prompt=True
        )
        hou.setFrame(1)
        reference = OIIO.ImageBuf(str(artifacts_root() / "stage2/subdivision/core-cpu/reference-cube.exr"))
        self.round_depth = float(reference.get_pixels()[45, 80, list(reference.spec().channelnames).index("depth.Z")])
        self.rop = hou.node("/out/native_subdivision_cycles")
        # Older fixture HIPs were saved before their producer added Depth for
        # batch rendering. This live depth regression owns its required pass.
        if not passes.has(self.rop, "depth", hou.time()):
            passes.add(self.rop, "depth")
        self.cube = hou.node("/obj/subdivision_receiver/coarse_cube")
        self.camera = hou.node("/obj/subdivision_camera")
        set_rop_devices(self.rop, os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.rop.parm("samples").set(256)
        set_subdivision(self.rop, type="catmull_clark")
        set_subdivision(self.rop, space="object")
        self.cube.parm("sharpness").set(0)
        self.cube.parm("corner").set(0)
        self.key = f"rop:{self.rop.sessionId()}"
        self.stage = 0
        self.generation = 0
        self.started = time.monotonic()
        self.last_tick = 0
        self.report = dict(
            device=rop_devices(self.rop, hou.time())[0],
            host_pid=os.getpid(),
            independent_round_depth=self.round_depth,
            events=[],
        )
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
        Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"]).write_text(json.dumps(self.report, indent=2))

    def image(self, entry, depth, newer=True, tolerance=0.007):
        if not entry or entry.error:
            return False
        status = entry.session.status()
        frame = entry.session.frame_info()
        if newer and status["generation"] <= self.generation:
            return False
        if not frame or frame["generation"] != status["generation"] or not frame["complete"]:
            return False
        try:
            self.report["native_planes"] = list(self.viewer.planes())
            if not {"C", "depth"}.issubset(self.report["native_planes"]):
                return False
            measured = self.viewer.pixel("depth", 80, 45)[0]
            color = self.viewer.pixel("C", 80, 45)[:3]
        except hou.OperationFailed:
            return False
        self.report["measured_depth"] = measured
        self.report["measured_color"] = color
        return abs(measured - depth) < tolerance and max(abs(a - b) for a, b in zip(color, (0.2, 0.4, 0.8))) < 0.003

    def record(self, name, entry):
        self.report["events"].append(
            dict(
                name=name,
                seconds=time.monotonic() - self.started,
                depth=self.report.get("measured_depth"),
                status=entry.session.status(),
                soho=entry.last_soho.copy(),
            )
        )
        self.generation = entry.session.status()["generation"]
        self.save()

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
                raise TimeoutError("Subdivision IPR stalled")
            if self.stage == 0:
                if not entry or not entry.helper_pid or not self.image(entry, self.round_depth, newer=False):
                    return
                assert entry.session.status()["subdivision_control_faces"] == 6
                self.record("Native IPR matches the independently refined rounded cube", entry)
                self.entry = entry
                before = entry.session.status()
                self.panel_tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel)
                self.panel_tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
                self.panel = self.panel_tab.activeInterfaceRootWidget()
                self.panel.set_rop(self.rop)
                assert self.panel.entry is entry and len(registry.entries()) == 1
                for key in ("generation", "geometry_resources", "mesh_variant_resources", "owned_geometry_bytes"):
                    assert entry.session.status()[key] == before[key]
                self.record("Panel and native viewer share control and tessellated buffers", entry)
                self.stage = 1
                with hou.undos.group("Change Cycles subdivision scheme"):
                    set_subdivision(self.rop, type="linear")
            elif self.stage in (1, 2, 3):
                depth = self.round_depth if self.stage == 2 else 4.3
                if not self.image(entry, depth):
                    return
                self.record(
                    (
                        "Linear scheme keeps the original cube extent",
                        "Undo restores Catmull-Clark",
                        "Redo restores Linear",
                    )[self.stage - 1],
                    entry,
                )
                if self.stage == 1:
                    self.stage = 2
                    hou.undos.performUndo()
                elif self.stage == 2:
                    self.stage = 3
                    hou.undos.performRedo()
                else:
                    self.stage = 4
                    set_subdivision(self.rop, type="catmull_clark")
                    self.cube.parm("sharpness").set(10)
            elif self.stage == 4:
                if not self.image(entry, 4.3):
                    return
                self.record("Live Houdini crease edits preserve a fully sharp cube", entry)
                self.stage = 5
                self.cube.parm("sharpness").set(0)
                set_subdivision(self.rop, space="pixel")
                set_subdivision(self.rop, dicing_rate=4)
            elif self.stage == 5:
                if not self.image(entry, self.round_depth, tolerance=0.03):
                    return
                self.record("Pixel-adaptive tessellation renders through native IPR", entry)
                self.triangles = entry.session.status()["subdivision_triangles"]
                self.conversions = entry.session.status()["geometry_conversions"]
                self.camera_tessellations = entry.session.status()["subdivision_tessellations"]
                self.camera_preview_logged = False
                self.stage = 6
                self.camera.parm("orthowidth").set(2)
            elif self.stage == 6:
                if not self.image(entry, self.round_depth, tolerance=0.03):
                    return
                status = entry.session.status()
                assert status["geometry_conversions"] == self.conversions
                if entry.camera_refinement_pending:
                    assert status["subdivision_tessellations"] == self.camera_tessellations
                    if not self.camera_preview_logged:
                        self.record("Camera feedback retains tessellation before scheduled refinement", entry)
                        self.camera_preview_logged = True
                    return
                if (
                    entry.change_serial > entry.applied_serial
                    or status["subdivision_tessellations"] == self.camera_tessellations
                ):
                    return
                assert (
                    status["geometry_conversions"] == self.conversions
                    and status["subdivision_triangles"] > self.triangles
                )
                self.record("Camera edits redice without extracting the control mesh", entry)
                self.camera_tessellations = status["subdivision_tessellations"]
                self.stage = 7
                self.camera.parm("orthowidth").set(4)
            elif self.stage == 7:
                if not self.image(entry, self.round_depth, tolerance=0.03):
                    return
                status = entry.session.status()
                assert status["geometry_conversions"] == self.conversions
                if entry.camera_refinement_pending or entry.change_serial > entry.applied_serial:
                    return
                if status["subdivision_tessellations"] == self.camera_tessellations:
                    return
                assert status["subdivision_triangles"] == self.triangles
                self.record("Restoring the camera restores adaptive tessellation", entry)
                self.good_frame = entry.session.frame_info()
                self.stage = 8
                self.rop.parm("limit_subdivision").set(1)
                self.rop.parm("subdivision_limit").set(1)
            elif self.stage == 8:
                status = entry.session.status()
                if status["state"] != "failed" or "Subdivision triangle budget" not in status["error"]:
                    return
                frame = entry.session.frame_info()
                assert frame and frame["generation"] == self.good_frame["generation"]
                self.record("Budget failure preserves the last complete native image", entry)
                self.stage = 9
                self.rop.parm("subdivision_limit").set(4000000)
            elif self.stage == 9:
                if not self.image(entry, self.round_depth, tolerance=0.03):
                    return
                self.record("A settings edit recovers from the allocation guard", entry)
                self.stage = 10
                self.cube.setName("renamed_control_cube")
            elif self.stage == 10:
                if not self.image(entry, self.round_depth, tolerance=0.03):
                    return
                self.record("Renaming the source retains native render identity", entry)
                self.panel.poll()
                path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"]).parent / "subdivision-panel.png"
                assert self.panel.grab().save(str(path))
                self.report["panel_preview"] = str(path)
                self.viewer.killRender()
                self.viewer.close()
                self.stage = 11
            elif self.stage == 11:
                if entry.helper_pid or entry.channel:
                    return
                assert registry.get(self.key) is entry and entry.views == 1
                self.record("The panel retains subdivision ownership after viewer closure", entry)
                self.panel.render()
                self.stage = 12
            elif self.stage == 12:
                frame = entry.session.frame_info()
                if not frame or frame["generation"] <= self.generation or not frame["complete"]:
                    return
                self.record("The panel continues the same subdivided scene", entry)
                self.finish(0)
        except Exception:
            self.report["error"] = traceback.format_exc()
            self.finish(1)

    def finish(self, code):
        # Keep the real failure receipt even if native teardown needs the
        # runner watchdog. A successful result still requires cleanup below.
        self.report["result"] = "failed" if code else "closing"
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
        self.closing_entry = registry.get(self.key)
        registry.close_all()
        self.code = code
        self.exit_at = time.monotonic() + 0.5
        self.cleanup_deadline = time.monotonic() + 10
        self._shutdown = self.shutdown
        hou.ui.addEventLoopCallback(self._shutdown)

    def shutdown(self):
        if time.monotonic() >= self.exit_at:
            if self.closing_entry:
                self.report["closed"] = self.closing_entry.session.status()
                closed = self.report["closed"]
                if closed["state"] != "closed" or closed["owned_geometry_bytes"] or closed["device_allocation_bytes"]:
                    if time.monotonic() < self.cleanup_deadline:
                        return  # Await the queued worker's real retirement ACK.
                    self.code = 1
                    self.report["error"] = (
                        self.report.get("error", "")
                        + "\nNative IPR cleanup did not release its resources within ten seconds"
                    )
            self.report["result"] = "passed" if self.code == 0 else "failed"
            self.save()
            hou.ui.removeEventLoopCallback(self._shutdown)
            hou.ui.postEventCallback(lambda: self.exit(self.code))

    def exit(self, code):
        try:
            hou.exit(code, suppress_save_prompt=True)
        except SystemExit:
            pass


test = SubdivisionLifecycle()
