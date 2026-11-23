# SPDX-License-Identifier: Apache-2.0
"""One heavy viewer: native camera parameter edits, local lens recovery and repair."""

from support.paths import artifacts_root

import hashlib, json, os, re, time, traceback
from pathlib import Path
import hou, numpy as np, OpenImageIO as oiio
from PySide6 import QtCore, QtTest
from cycles_houdini import registry, object_properties
from camera_lens_motion_fixture import Fixture
from houdini_material_override_gui_smoke import Workflow as ParameterWorkflow
from gui_shutdown import retire_and_exit


class Workflow(ParameterWorkflow):
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.native = os.environ.get("CYCLES_HOUDINI_CAMERA_LENS_VIEWER") == "native"
        device = os.environ["CYCLES_HOUDINI_GUI_DEVICE"]
        root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
        self.references = (
            artifacts_root() / "stage2/cameras/lens-motion" / ("references-" + re.sub("[^A-Za-z0-9_-]", "_", device))
        )
        self.images = {}
        self.f = Fixture(device)
        self.f.grid.setParms(dict(rows=129, cols=257))
        self.heavy = hou.node("/obj").createNode("geo", "resident_camera_instances", run_init_scripts=False)
        object_properties.install(self.heavy)
        self.heavy.parm("cycles_geometry_motion").set("velocity")
        torus = self.heavy.createNode("torus", "million_triangle_prototype")
        torus.setParms(dict(type=0, rows=512, cols=1024))
        targets = self.heavy.createNode("grid", "twelve_targets")
        targets.setParms(dict(type=0, rows=3, cols=4))
        targets.parmTuple("size").set((8, 6))
        targets.parmTuple("t").set((18, 0, -8))
        copies = self.heavy.createNode("copytopoints", "shared_instances")
        copies.setInput(0, torus)
        copies.setInput(1, targets)
        copies.parm("pack").set(1)
        copies.setDisplayFlag(True)
        copies.setRenderFlag(True)
        self.configure("dof")
        self.cook_sources = (self.f.sop, torus)
        self.retained = None
        self.retained_cooks = {}
        self.report = dict(
            result="running",
            viewer="native" if self.native else "cycles",
            scope=__doc__,
            physical_mouse_certification=False,
            events=[],
            tick_intervals=[],
            render_clicks=1,
        )
        self.settings = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm, size=(650, 550))
        self.settings.setCurrentNode(self.f.camera)
        self.panel = None
        if self.native:
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.IPRViewer)
            self.tab.setRopNode(self.f.rop)
            self.tab.setPreview(True)
            self.tab.setAutoUpdate(True)
            self.tab.setDelay(0.02)
        else:
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(1000, 740))
            self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.phase = "mount"
        self.expected = "dof"
        self.offset = 0
        self.began = self.last_tick = time.monotonic()
        self.next_at = self.saved_at = 0
        self.generation = 0
        self.finished = self.busy = False
        self.commit_deadline = None
        self.image_phase = None
        self.image_deadline = 0
        self.timer = QtCore.QTimer(hou.qt.mainWindow())
        hou.ui.addEventLoopCallback(self.tick)
        if self.native:
            self.tab.startRender()

    def configure(self, case):
        self.f.configure(case)
        self.f.rop.parm("vobject").set(self.f.obj.path() + " " + self.heavy.path())

    def image(self, name):
        name += "-3-" + ("p" if self.offset else "c")
        if name not in self.images:
            path = self.references / (name + ".exr")
            image = oiio.ImageBuf(str(path))
            channels = list(image.spec().channelnames)
            self.images[name] = (
                np.flipud(image.get_pixels()[:, :, [channels.index(c) for c in ("R", "G", "B")]]),
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        return self.images[name]

    def edit(self, parm, text, phase, expected):
        # Typing the evaluated value does not remove a Houdini expression.
        # Explicitly remove its channel (the native Delete Channels action)
        # before the actual keyboard entry. Record this HOM authoring step.
        if parm.keyframes():
            self.report.setdefault("channel_removals", []).append(dict(parameter=parm.path(), phase=phase))
            parm.deleteAllKeyframes()
        super().edit(parm, text, phase, expected)

    def finish(self, error=None):
        if self.finished:
            return
        if self.tick in hou.ui.eventLoopCallbacks():
            hou.ui.removeEventLoopCallback(self.tick)
        super().finish(error)

    def quit(self):
        if self.quit in hou.ui.eventLoopCallbacks():
            hou.ui.removeEventLoopCallback(self.quit)
        sessions = [entry.session for entry in registry.entries()]
        actions = ([self.panel.dispose] if self.panel else []) + [
            registry.close_all,
            self.settings.close,
            self.tab.close,
            self.timer.deleteLater,
        ]
        retire_and_exit(self.path, self.report, sessions, 1 if self.report["error"] else 0, actions)

    def tick(self):
        if self.phase in ("mount", "focus", "keys", "commit"):
            return super().tick()
        if self.finished or self.busy:
            return
        self.busy = True
        try:
            now = time.monotonic()
            self.report["tick_intervals"].append(dict(phase=self.phase, seconds=now - self.last_tick))
            self.last_tick = now
            assert now - self.began < 300, self.phase
            if now - self.saved_at > 0.5:
                self.report["phase"] = self.phase
                self.path.write_text(json.dumps(self.report), encoding="utf-8")
                self.saved_at = now
            if now < self.next_at:
                return
            if self.commit_deadline:
                value = self.parm.eval()
                matches = abs(float(value) - float(self.text)) < 1e-6
                assert matches or now < self.commit_deadline, (self.parm.path(), value, self.text)
                if not matches:
                    return
                self.commit_deadline = None
            entry = registry.get(f"rop:{self.f.rop.sessionId()}")
            if entry is None:
                return
            status = entry.session.status()
            info = entry.session.frame_info()
            assert not entry.error and status["state"] != "failed", (entry.error, status)
            if (
                not info
                or not info["complete"]
                or info["generation"] != status["generation"]
                or status["generation"] <= self.generation
                or status.get("pending_update")
                or status.get("capture_pending")
            ):
                return
            if not self.native and (
                self.panel._live_generation != info["generation"]
                or self.panel.canvas.frame_metadata.get("sequence") != info["sequence"]
            ):
                return
            plane = entry.session.frame()["planes"]["combined"]
            pixels = np.frombuffer(plane["pixels"], np.float32).reshape(96, 160, plane["channels"])[:, :, :3]
            resources = 3 if self.phase.startswith("near_") else 2
            assert (
                np.isfinite(pixels).all()
                and status["geometry_resources"] == resources
                and status["object_resources"] == resources + 11
                and status["packed_instances"] == 12
            ), status
            expected, digest = self.image(self.expected)
            difference = float(np.abs(pixels - expected).mean())
            if self.image_phase != self.phase:
                self.image_phase = self.phase
                self.image_deadline = now + 8
            if difference >= 0.0012:
                observed = dict(
                    phase=self.phase,
                    generation=info["generation"],
                    sequence=info["sequence"],
                    mean_difference=difference,
                )
                interim = self.report.setdefault("interim_frames", [])
                if not interim or any(interim[-1][key] != observed[key] for key in ("phase", "generation", "sequence")):
                    interim.append(observed)
                if now < self.image_deadline:
                    return
                np.savez(self.path.parent / "failed-image.npz", expected=expected, actual=pixels)
                raise AssertionError((self.phase, difference))
            if self.phase == "near_visible":
                assert float((pixels[:, :, 0] - pixels[:, :, 1]).max()) > 0.03
            codes = {"focus_invalid": {"camera.invalid_dof"}, "transform_invalid": {"camera.shutter_transform"}}.get(
                self.phase, set()
            )
            assert {issue["code"] for issue in status["source_diagnostics"]} == codes and not status[
                "motion_warnings"
            ], status
            if self.native:
                if not entry.helper_pid or self.tab.imageResolution() != (160, 96):
                    return
                try:
                    if any(
                        not np.allclose(self.tab.pixel("C", x, y)[:3], pixels[y, x], atol=2e-5, rtol=0)
                        for x, y in ((35, 48), (55, 48), (80, 48), (128, 48), (150, 48))
                    ):
                        return
                except hou.OperationFailed:
                    return
            else:
                assert not self.panel.render_button.isEnabled() and self.panel.stop_button.isEnabled()
            assert len(registry.entries()) == 1
            if self.retained:
                assert status["renderer_pid"] == self.pid, status
                if self.phase not in ("zoom_ready", "near_broken"):
                    assert all(status[key] == self.retained[key] for key in ("geometry_conversions", "extractions")), (
                        status
                    )
                    counts = {node.path(): node.cookCount() for node in self.cook_sources}
                    assert counts == self.retained_cooks, (counts, self.retained_cooks)
                    if status["render_revision"] != self.retained["render_revision"]:
                        assert (
                            not {"tri_verts", "tri_vindex"} & entry.session.diagnostics()["buffer_copy_requests"].keys()
                        )
                    self.report.setdefault("navigation_cooks", []).append(dict(phase=self.phase, counts=counts))
            else:
                self.pid = status["renderer_pid"]
            self.record(self.phase, status)
            self.report["events"][-1].update(mean_difference=difference, reference_sha256=digest)
            self.retained = status
            self.retained_cooks = {node.path(): node.cookCount() for node in self.cook_sources}
            if self.phase == "first":
                self.edit(self.f.camera.parm("focus"), 3.5, "focus_changed", "gui_dof_focus")
                return
            if self.phase == "focus_changed":
                self.edit(self.f.camera.parm("focus"), 2.5, "focus_restored", "dof")
                return
            if self.phase == "focus_restored":
                self.f.camera.parm("focus").setExpression("2.5+$T*24", hou.exprLanguage.Hscript)
                self.phase = "focus_animated"
                self.expected = "focus_pull"
                return
            if self.phase == "focus_animated":
                self.edit(self.f.camera.parm("focus"), 2.5, "focus_repaired", "dof")
                return
            if self.phase == "focus_repaired":
                self.f.camera.parm("cycles_focus_distance").setExpression("float('nan')", hou.exprLanguage.Python)
                self.phase = "focus_invalid"
                self.expected = "pinhole"
                return
            if self.phase == "focus_invalid":
                self.edit(self.f.camera.parm("cycles_focus_distance"), 2.5, "focus_valid", "dof")
                return
            if self.phase == "focus_valid":
                self.configure("zoom")
                self.phase = "zoom_ready"
                self.expected = "zoom"
                return
            if self.phase == "zoom_ready":
                self.edit(self.f.camera.parm("focal"), 50, "zoom_stopped", "pinhole")
                return
            if self.phase == "zoom_stopped":
                self.f.camera.parm("focal").setExpression("50/(1+$T*24)", hou.exprLanguage.Hscript)
                self.phase = "zoom_restored"
                self.expected = "zoom"
                return
            if self.phase == "zoom_restored":
                self.edit(self.f.camera.parm("tx"), 0, "transform_stopped", "held_transform_zoom")
                return
            if self.phase == "transform_stopped":
                self.f.camera.parm("tx").setExpression(
                    "float('nan') if hou.time()<0 else hou.time()*6", hou.exprLanguage.Python
                )
                self.phase = "transform_invalid"
                return
            if self.phase == "transform_invalid":
                self.edit(self.f.camera.parm("tx"), 0, "transform_repaired", "held_transform_zoom")
                return
            if self.phase == "transform_repaired":
                self.f.camera.parm("tx").setExpression("$T*6", hou.exprLanguage.Hscript)
                self.phase = "transform_restored"
                self.expected = "zoom"
                return
            if self.phase == "transform_restored":
                self.configure("clipping")
                self.f.witness()
                self.f.rop.parm("vobject").set(self.f.rop.evalParm("vobject") + " " + self.heavy.path())
                self.offset = 0.75
                self.f.rop.parm("shutteroffset").set(0.75)
                self.phase = "near_broken"
                self.expected = "animated_near_witness"
                return
            if self.phase == "near_broken":
                self.edit(self.f.camera.parm("near"), 0.01, "near_visible", "near_witness")
                return
            if self.phase == "near_visible":
                self.edit(self.f.camera.parm("near"), 0.1, "near_clipped", "pinhole")
                return
            if self.phase == "near_clipped":
                target = self.path.parent / "camera-lens-viewer.png"
                self.report["viewer_capture"] = str(target)
                self.report["viewer_capture_saved"] = (
                    hou.qt.floatingPanelWindow(self.tab.floatingPanel()).grab().save(str(target))
                )
                if self.native:
                    self.tab.killRender()
                else:
                    QtTest.QTest.mouseClick(self.panel.stop_button, QtCore.Qt.LeftButton)
                self.finish()
        except BaseException:
            self.finish(traceback.format_exc())
        finally:
            self.busy = False


if __name__ == "__main__":
    workflow = Workflow()
