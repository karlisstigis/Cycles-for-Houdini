# SPDX-License-Identifier: Apache-2.0
"""One viewer: native parameter edits, off-frame repairs and a million-triangle shared prototype."""

from native_test_runtime import runtime_executable
import json, os, subprocess, time, traceback
from pathlib import Path
import hou, numpy as np, OpenImageIO as oiio
from PySide6 import QtCore, QtTest
import cycles_houdini as ch
from cycles_houdini import registry, object_properties
from sampled_override_fixture import Fixture
from houdini_material_override_gui_smoke import Workflow as ParameterWorkflow
from light_shader_fixture import write_image


class Workflow(ParameterWorkflow):
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.native = os.environ.get("CYCLES_HOUDINI_OVERRIDE_VIEWER") == "native"
        self.f = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.f.camera = hou.node(self.f.rop.evalParm("camera"))
        self.f.obj.node("grid1").setParms(dict(rows=129, cols=257))
        self.f.sop.addSpareParmTuple(hou.FloatParmTemplate("test_mask", "Displacement Field", 1, default_value=(0.5,)))
        heavy = hou.node("/obj").createNode("geo", "resident_shared_geometry", run_init_scripts=False)
        object_properties.install(heavy)
        heavy.parm("cycles_geometry_motion").set("transform")
        torus = heavy.createNode("torus", "million_triangle_prototype")
        torus.setParms(dict(type=0, rows=512, cols=1024))
        targets = heavy.createNode("grid", "twelve_targets")
        targets.setParms(dict(type=0, rows=3, cols=4))
        targets.parmTuple("size").set((8, 6))
        targets.parmTuple("t").set((18, 0, -8))
        copies = heavy.createNode("copytopoints", "shared_instances")
        copies.setInput(0, torus)
        copies.setInput(1, targets)
        copies.parm("pack").set(1)
        copies.setDisplayFlag(True)
        copies.setRenderFlag(True)
        self.f.rop.parm("vobject").set(self.f.obj.path() + " " + heavy.path())
        root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
        reference_dir = self.path.parent / "references"
        run = subprocess.run(
            [
                str(runtime_executable("ch_harness.exe")),
                "--resources",
                os.environ["CYCLES_HOUDINI_RESOURCES"],
                "--device",
                os.environ["CYCLES_HOUDINI_GUI_DEVICE"],
                "--displacement-motion-reference",
                "--output",
                str(reference_dir),
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert run.returncode == 0, (run.stdout, run.stderr)
        self.references = {}
        for samples in (3, 5):
            image = oiio.ImageBuf(str(reference_dir / f"position-{samples}.exr"))
            pixels = image.get_pixels()
            channels = list(image.spec().channelnames)
            self.references[samples] = np.flipud(pixels[:, :, [channels.index(c) for c in ("R", "G", "B")]])
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
        self.settings.setCurrentNode(self.f.assigned)
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
        self.expected = "reference3"
        self.shift = 0
        self.factor = 1
        self.began = self.last_tick = time.monotonic()
        self.next_at = self.saved_at = 0
        self.generation = 0
        self.finished = self.busy = False
        self.commit_deadline = None
        self.timer = QtCore.QTimer(hou.qt.mainWindow())
        hou.ui.addEventLoopCallback(self.tick)
        if self.native:
            self.tab.startRender()

    def finish(self, error=None):
        if self.tick in hou.ui.eventLoopCallbacks():
            hou.ui.removeEventLoopCallback(self.tick)
        super().finish(error)

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
                matches = value == self.text if isinstance(value, str) else float(value) == float(self.text)
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
            frame = entry.session.frame()
            pixels = self.f.pixels(frame)["combined"][:, :, :3]
            assert (
                np.isfinite(pixels).all() and status["geometry_resources"] == 2 and status["packed_instances"] == 12
            ), status
            if self.expected.startswith("reference"):
                expected = self.references[int(self.expected[-1])] * self.factor
                if self.shift:
                    expected = np.roll(expected, self.shift, axis=1).copy()
                    if self.shift < 0:
                        expected[:, self.shift :] = 0
                    else:
                        expected[:, : self.shift] = 0
                assert np.abs(pixels - expected).mean() < 0.0008, (self.phase, float(np.abs(pixels - expected).mean()))
            elif self.expected == "static":
                np.testing.assert_allclose(
                    pixels[30:60, 55:105], np.broadcast_to((0.2, 0.4, 0.8), (30, 50, 3)), atol=2e-5, rtol=0
                )
            elif self.expected == "texture_fallback":
                x = np.where(pixels[:, :, 2] > 0.01)[1]
                assert len(x) and 92 < x.max() - x.min() < 106, (self.phase, x)
            elif self.expected == "expanded":
                assert pixels[30:60, 150:157, 2].mean() > 0.01, (self.phase, pixels[:, :, 2].max())
            codes = {issue["code"] for issue in status["source_diagnostics"]}
            wanted = (
                {"texture.unavailable", "motion.displacement_samples"}
                if self.phase == "file_broken"
                else {"motion.displacement_binding"}
                if self.phase == "binding_broken"
                else {"motion.displacement_samples"}
                if self.phase == "field_broken"
                else set()
            )
            assert codes == wanted and not status["motion_warnings"], status
            if self.native:
                if not entry.helper_pid or self.tab.imageResolution() != (160, 90):
                    return
                try:
                    if any(
                        not np.allclose(self.tab.pixel("C", x, y)[:3], pixels[y, x], atol=2e-5, rtol=0)
                        for x, y in ((55, 45), (80, 45), (128, 45), (150, 45), (80, 30))
                    ):
                        return
                except hou.OperationFailed:
                    return
            else:
                assert not self.panel.render_button.isEnabled() and self.panel.stop_button.isEnabled()
            assert len(registry.entries()) == 1
            if self.report["events"]:
                assert status["renderer_pid"] == self.pid, status
            else:
                self.pid = status["renderer_pid"]
            self.record(self.phase, status)
            if self.phase == "first":
                self.edit(self.f.assigned.parm("test_speed"), 0, "static", "static")
                return
            if self.phase == "static":
                self.edit(self.f.assigned.parm("test_speed"), 72, "restored", "reference3")
                return
            if self.phase == "restored":
                self.edit(self.f.rop.parm("motion_samples"), 5, "samples", "reference5")
                return
            if self.phase == "samples":
                self.retained = status
                self.shift = -2
                self.edit(self.f.camera.parm("tx"), 0.05, "camera", "reference5")
                return
            if self.phase == "camera":
                assert all(
                    status[key] == self.retained[key] for key in ("geometry_conversions", "displacement_rebuilds")
                ), status
                self.f.setup("image")
                self.f.rop.parm("motion_samples").set(3)
                self.phase = "image"
                self.expected = "reference3"
                self.shift = 0
                return
            if self.phase == "image":
                self.retained = status
                self.f.files[0].unlink()
                self.phase = "file_broken"
                self.expected = "texture_fallback"
                return
            if self.phase == "file_broken":
                assert status["geometry_conversions"] == self.retained["geometry_conversions"], status
                write_image(self.f.files[0], (0.25,) * 3)
                self.phase = "file_repaired"
                self.expected = "reference3"
                return
            if self.phase == "file_repaired":
                assert status["geometry_conversions"] == self.retained["geometry_conversions"], status
                self.f.setup("parameter")
                self.f.assigned.parm("test_speed").set(0)
                self.f.sop.parm("snippet").set('v@N=set(1,0,0);@P.x+=@Time*72;f@existing_mask=chf("test_mask");')
                self.missing = "/mat/gui_shutter_repair"
                self.f.output.parm("snippet").set('if(@Time>0)s@shop_materialpath="' + self.missing + '";')
                self.phase = "binding_broken"
                return
            if self.phase == "binding_broken":
                self.repaired = ch.create_material(name=self.missing.rsplit("/", 1)[1], shader="emission")
                self.displacement = self.repaired.createNode("cycles::displacement::1", "displacement")
                self.displacement.setParms(dict(height=0.5, midlevel=0, scale=1))
                ch.promote_parameter(self.repaired, self.displacement, "height", name="height")
                ch.promote_parameter(self.repaired, self.repaired.node("surface_shader"), "strength", name="power")
                self.repaired.parm("power").set(1)
                self.repaired.node("surface_output").setNamedInput("displacement", self.displacement, "displacement")
                self.repaired.parm("cycles_displacement_method").set("true")
                self.phase = "binding_repaired"
                return
            if self.phase == "binding_repaired":
                self.field = self.repaired.createNode("cycles::attribute::1", "shutter_field")
                self.field.parm("attribute").set("existing_mask")
                self.displacement.setNamedInput("height", self.field, "fac")
                self.edit(self.f.sop.parm("test_mask"), 1, "field_live", "expanded")
                return
            if self.phase == "field_live":
                self.edit(self.field.parm("attribute"), "missing_mask", "field_broken", "reference3")
                return
            if self.phase == "field_broken":
                assert any(issue.get("attribute") == "missing_mask" for issue in status["source_diagnostics"]), status
                self.edit(self.field.parm("attribute"), "existing_mask", "field_repaired", "expanded")
                return
            if self.phase == "field_repaired":
                self.edit(self.f.sop.parm("test_mask"), 0.5, "mask_reset", "reference3")
                return
            if self.phase == "mask_reset":
                self.f.assigned.setParms(dict(num_local1=2, local1_name2="power", local1_type2="float", local1_fval2=1))
                self.phase = "color_ready"
                return
            if self.phase == "color_ready":
                self.retained = status
                self.factor = 1.5
                self.edit(self.f.assigned.parm("local1_fval2"), 1.5, "color_only", "reference3")
                return
            if self.phase == "color_only":
                assert all(
                    status[key] == self.retained[key]
                    for key in (
                        "geometry_conversions",
                        "displacement_rebuilds",
                        "subdivision_tessellations",
                        "engine_shader_resources",
                    )
                ), status
                self.retained = status
                self.shift = 2
                self.edit(self.f.obj.parm("tx"), -0.45, "object", "reference3")
                return
            if self.phase == "object":
                assert all(
                    status[key] == self.retained[key] for key in ("geometry_conversions", "displacement_rebuilds")
                ), status
                target = self.path.parent / "sampled-overrides-viewer.png"
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
