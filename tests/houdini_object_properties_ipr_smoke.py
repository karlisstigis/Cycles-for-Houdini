# SPDX-License-Identifier: Apache-2.0
"""Native object controls during a real heavy SOP IPR; one render viewer.

HOM edits native parameters and QtTest clicks the artist-facing panel controls.
The viewport and native parameter editor remain open throughout the workflow.
"""

import os
from pathlib import Path
import runpy
import time
import traceback
import hou
from PySide6 import QtCore, QtTest
from cycles_houdini import registry, object_properties

os.environ["CYCLES_HOUDINI_BENCH_SHARED_VIEWS"] = "0"
os.environ["CYCLES_HOUDINI_BENCH_SAMPLES"] = "32"
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
HeavyResponse = runpy.run_path(
    str(root / "tests/houdini_heavy_response_benchmark.py"), run_name="object_properties_fixture"
)["HeavyResponse"]


class ObjectWorkflow(HeavyResponse):
    def configure_scene(self):
        self.ground.parm("resolution").set(560)
        self.test_object = hou.node("/obj").createNode("geo", "cycles_object_test")
        for node in self.test_object.children():
            node.destroy()
        self.control = self.test_object.createNode("torus", "native_quad_torus")
        self.control.setParms(dict(rows=12, cols=24, radx=0.8, rady=0.24, orient=1))
        self.control.setRenderFlag(True)
        self.control.setDisplayFlag(True)
        self.test_object.parmTuple("t").set((2, 1, 1))
        self.test_object.parm("shop_materialpath").set("/mat/courtyard_burnished_brass")
        self.rop.parm("vobject").set("fx_courtyard cycles_object_test")
        assert self.test_object.parm("cycles_subdivision_override") is None, "Object properties must be opt-in"
        object_properties.install(self.test_object)
        self.report["object_events"] = []
        self.control_primitives = self.control.geometry().intrinsicValue("primitivecount")
        self.actions = [
            (
                "Enable object subdivision",
                dict(
                    cycles_subdivision_override=1,
                    cycles_subdivision_type="catmull_clark",
                    cycles_subdivision_adaptive=0,
                    cycles_subdivision_ipr_level=1,
                ),
                1,
            ),
            ("Increase IPR levels", dict(cycles_subdivision_ipr_level=2), 1),
            ("Choose Simple / Linear", dict(cycles_subdivision_type="linear"), 1),
            (
                "Switch to adaptive Pixel space",
                dict(
                    cycles_subdivision_type="catmull_clark",
                    cycles_subdivision_adaptive=1,
                    cycles_subdivision_space="pixel",
                    cycles_subdivision_pixel_size=1,
                ),
                1,
            ),
            ("Decrease target pixel size", dict(cycles_subdivision_pixel_size=0.25), 1),
            ("Move the camera", None, 0),
            ("Hide this object from camera rays", dict(cycles_visibility_override=1, cycles_visibility_camera=0), 0),
            ("Show this object again", dict(cycles_visibility_camera=1), 0),
        ]

    def finish(self):
        if hasattr(self, "parameters"):
            self.parameters.close()
        super().finish()

    def tick(self):
        if self.tick_busy:
            return
        self.tick_busy = True
        try:
            if time.monotonic() - self.began > 165:
                raise TimeoutError("Object settings workflow stalled")
            entry = registry.get(self.key)
            if entry is None or not self.ready(entry):
                return
            frame = entry.session.frame_info()
            if not frame or not frame["complete"]:
                return
            if self.stage == "initial":
                assert self.report["fixture"]["mesh_triangles"] >= 1000000, self.report["fixture"]
                self.original_entry = entry
                self.original_pid = entry.session.status().get("renderer_pid", os.getpid())
                self.parameters = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm)
                self.parameters.setCurrentNode(self.test_object)
                self.parameters.setPin(True)
                window = hou.qt.floatingPanelWindow(self.parameters.floatingPanel())
                window.resize(650, 1050)
                # Select the Cycles native tab through its public FolderSet parm.
                for parm in self.test_object.parmTuples():
                    template = parm.parmTemplate()
                    if isinstance(template, hou.FolderSetParmTemplate) and "Cycles" in template.folderNames():
                        parm[0].set(template.folderNames().index("Cycles"))
                        break
                self.stage = "object-edit"
                self.index = 0
            elif self.stage == "object-wait":
                status = entry.session.status()
                assert entry is self.original_entry and len(registry.entries()) == 1
                assert status["geometry_conversions"] == self.previous["geometry_conversions"] + self.expected_delta, (
                    self.action,
                    status,
                    self.previous,
                )
                assert status.get("renderer_pid", os.getpid()) == self.original_pid
                assert self.control.geometry().intrinsicValue("primitivecount") == self.control_primitives
                if self.action == "Increase IPR levels":
                    assert status["subdivision_triangles"] == self.previous["subdivision_triangles"] * 4
                if self.action == "Decrease target pixel size":
                    assert status["subdivision_triangles"] > self.previous["subdivision_triangles"]
                self.report["object_events"].append(
                    dict(name=self.action, status=status, edit_to_image_seconds=time.monotonic() - self.edit_started)
                )
                window = hou.qt.floatingPanelWindow(self.parameters.floatingPanel())
                window.grab().save(str(self.path.with_suffix(".object-settings.png")))
                self.panel.grab().save(str(self.path.with_suffix(".render.png")))
                self.index += 1
                self.stage = "object-edit"
            elif self.stage == "rop-settings":
                assert self.settings_pane.currentNode() == self.rop
                hou.qt.floatingPanelWindow(self.settings_pane.floatingPanel()).grab().save(
                    str(self.path.with_suffix(".rop-settings.png"))
                )
                self.finish()
                return
            if self.stage == "object-edit":
                if self.index == len(self.actions):
                    self.report["phase"] = "open-native-rop-settings"
                    self.settings_pane = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm)
                    self.settings_pane.setCurrentNode(self.rop)
                    self.stage = "rop-settings"
                    self.before = 0
                    self.save()
                    return
                self.action, values, self.expected_delta = self.actions[self.index]
                self.previous = entry.session.status()
                self.before = self.previous["generation"]
                self.edit_started = time.monotonic()
                self.report["phase"] = self.action
                if values is None:
                    self.camera.parm("tx").set(self.camera.evalParm("tx") + 0.05)
                else:
                    with hou.undos.group(self.action):
                        self.test_object.setParms(values)
                self.stage = "object-wait"
                self.save()
        except Exception:
            self.report.update(result="failed", error=traceback.format_exc())
            self.save()
            self.timer.stop()
            hou.ui.removeEventLoopCallback(self.callback)
            try:
                registry.close_all()
            except Exception:
                pass
            hou.exit(1, suppress_save_prompt=True)
        finally:
            self.tick_busy = False


workflow = ObjectWorkflow()
