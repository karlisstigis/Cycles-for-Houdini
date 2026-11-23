# SPDX-License-Identifier: Apache-2.0
"""Current showcase: optional shelf properties, split ground and live native lights.

Uses a single Cycles panel and native shelf actions with a test-owned area light.
"""

from support.paths import artifacts_root

from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import os
from pathlib import Path
import time
import traceback
import hou
import numpy as np
from PySide6 import QtCore, QtTest, QtWidgets
from cycles_houdini import light_properties, object_scene, registry


class ArtistSceneWorkflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
        self.example = artifacts_root() / "examples/sop-fx-courtyard"
        self.report = dict(
            result="running",
            host_pid=os.getpid(),
            events=[],
            render_viewers=["cycles"],
            input_scope="HOM parameter edits, public SceneViewer shelf-tool invocation and QtTest panel clicks",
        )
        self.began = time.monotonic()
        self.busy = False
        self.stage = "initial"
        self.before = 0
        hou.hipFile.load(str(self.example / "sop-fx-courtyard-adaptive.hiplc"), suppress_save_prompt=True)
        self.rop = hou.node("/out/cycles_sop_courtyard")
        self.ground = hou.node("/obj/ground")
        for light in object_scene.selected(self.rop, hou.time(), lights=True):
            light.parm("light_enable").set(0)
        self.area = hou.node("/obj").createNode("hlight::2.0", "workflow_area")
        self.area.setParms(
            dict(
                light_type="grid",
                tx=0,
                ty=5,
                tz=4,
                rx=-50,
                ry=-20,
                light_intensity=4000,
                singlesided=0,
                normalizearea=1,
                light_contribprimary=1,
            )
        )
        self.area.parmTuple("areasize").set((2, 1))
        self.report["saved_masks"] = {
            name: self.rop.evalParm(name) for name in ("vobject", "forceobject", "excludeobject")
        }
        assert self.ground is not None and self.area is not None, (
            "The artist fixture has changed; update the independent reproduction"
        )
        self.report["saved_selection"] = [node.path() for node in object_scene.selected(self.rop, hou.time())]
        self.rop.parm("vobject").set("*")
        self.report["corrected_selection"] = [node.path() for node in object_scene.selected(self.rop, hou.time())]
        assert self.ground.path() in self.report["corrected_selection"]
        fx = hou.node("/obj/fx_courtyard")
        assert fx.parm("vm_rendervisibility") is not None
        repaired = self.path.parent / "showcase-live.hiplc"
        hou.hipFile.save(str(repaired))
        self.report["repaired_scene"] = str(repaired)
        hou.putenv("OCIO", str(self.example / "showcase.ocio"))
        hou.Color.reloadOCIO()
        set_rop_devices(self.rop, os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.rop.setParms(dict(resx=480, resy=270, samples=128, ipr_samples=128, use_denoise=0, ipr_use_denoise=0))
        self.key = f"rop:{self.rop.sessionId()}"
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel)
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.panel = self.tab.activeInterfaceRootWidget()
        self.panel.window().resize(1180, 860)
        self.panel.set_rop(self.rop)
        self.scene = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.SceneViewer)
        self.scene.setPwd(hou.node("/obj"))
        self.scene.curViewport().setCamera(hou.node(self.rop.evalParm("camera")))
        self.callback = self.tick
        hou.ui.addEventLoopCallback(self.callback)
        QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.report, indent=2))

    def click_menu(self, *labels):
        # The top menu was deliberately removed. Exercise the installed
        # shelf actions through Houdini's public dispatch, with the current
        # selection, instead of binding nonexistent main-menu commands.
        commands = {
            ("Add Object Parameters",): "add_object_parameters",
            ("Add Light Parameters",): "add_light_parameters",
            ("Remove Cycles Parameters",): "remove_parameters",
            ("Create Light", "Area"): "create_area_light",
        }
        assert hou.selectedNodes(), "Shelf tool requires the artist selection"
        viewer = hou.ui.curDesktop().paneTabOfType(hou.paneTabType.SceneViewer)
        assert viewer is not None
        viewer.runShelfTool("cycles_" + commands[labels])
        self.report["menu_input"] = (
            "Public native shelf-tool invocation with the artist selection; no synthetic main menu"
        )

    def advance(self, name, entry, action):
        self.before = entry.session.status()["generation"]
        self.stage = name
        self.save()
        action()

    def record(self, name, entry, **values):
        assert len(registry.entries()) == 1 and self.panel.entry is entry
        self.report["events"].append(dict(name=name, status=entry.session.status(), **values))
        self.save()

    def tick(self):
        if self.busy:
            return
        self.busy = True
        try:
            if time.monotonic() - self.began > 150:
                raise TimeoutError("Artist scene IPR workflow stalled")
            if self.stage.startswith("menu-"):
                self.menu_tick()
                return
            entry = registry.get(self.key)
            if not entry:
                return
            status = entry.session.status()
            frame = entry.session.frame_info()
            if entry.error or status["state"] == "failed":
                raise RuntimeError(entry.error or status["error"])
            if (
                not frame
                or not frame["complete"]
                or frame["generation"] != status["generation"]
                or status["generation"] <= self.before
            ):
                return
            if self.panel.canvas.generation != status["generation"]:
                return
            image = (
                np.frombuffer(entry.session.frame()["planes"]["combined"]["pixels"], np.float32)
                .reshape(270, 480, 4)
                .copy()
            )
            assert np.isfinite(image).all()
            if self.stage == "initial":
                self.original_entry = entry
                self.converted = status["geometry_conversions"]
                self.base = image
                assert status["light_resources"] == 2
                self.panel.grab().save(str(self.path.with_suffix(".lit.png")))
                self.record("Split ground is included; native two-sided area light is present", entry)
                self.advance("light-off", entry, lambda: self.area.parm("light_enable").set(0))
            elif self.stage == "light-off":
                assert status["light_resources"] == 0 and status["geometry_conversions"] == self.converted
                self.off = image
                difference = self.base[:, :, :3] - image[:, :, :3]
                assert float(difference.mean()) > 1e-5 and float(difference.max()) > 1
                # Receiver lighting differs at many pixels, beyond the directly
                # visible rectangle (whose area is small in this saved camera).
                assert int(np.count_nonzero(difference.mean(2) > 0.0001)) > 1000
                self.record(
                    "Disabling native area light removes its visible surface and illumination",
                    entry,
                    mean_rgb_contribution=float(difference.mean()),
                    changed_pixels=int(np.count_nonzero(difference.mean(2) > 0.0001)),
                )
                self.advance("light-on", entry, lambda: self.area.parm("light_enable").set(1))
            elif self.stage == "light-on":
                # Removing/recreating the light can change its sampling index.
                # Compare restored energy rather than requiring the same noise.
                relative_mean_error = abs(float(image[:, :, :3].mean()) / float(self.base[:, :, :3].mean()) - 1)
                assert relative_mean_error < 0.025, relative_mean_error
                self.base = image
                self.record(
                    "Re-enabled light restores scene brightness within sampling tolerance",
                    entry,
                    relative_mean_error=relative_mean_error,
                )
                self.advance("surface-hidden", entry, lambda: self.area.parm("light_contribprimary").set(0))
            elif self.stage == "surface-hidden":
                visible = self.base[:, :, :3] - image[:, :, :3]
                assert float(visible.max()) > 1
                assert int(np.count_nonzero(np.any(visible > 1, axis=2))) > 2
                assert float(np.maximum(image[:, :, :3] - self.off[:, :, :3], 0).mean()) > 1e-5
                self.record("Render Light Geometry hides the rectangle while preserving its illumination", entry)
                self.advance(
                    "light-moved",
                    entry,
                    lambda: self.area.setParms(dict(light_contribprimary=1, tx=self.area.evalParm("tx") + 0.75)),
                )
            elif self.stage == "light-moved":
                assert status["geometry_conversions"] == self.converted and not np.array_equal(image, self.base)
                self.record("Native area movement updates the existing scene without geometry conversion", entry)
                self.advance("light-added", entry, self.add_light)
            elif self.stage == "light-added":
                assert status["light_resources"] == 3 and status["geometry_conversions"] == self.converted, (
                    status["light_resources"],
                    status["geometry_conversions"],
                    self.converted,
                )
                self.record("Adding a native area light during IPR updates the single renderer session", entry)
                self.advance("light-deleted", entry, self.new_light.destroy)
            elif self.stage == "light-deleted":
                assert status["light_resources"] == 2 and status["geometry_conversions"] == self.converted
                self.record("Deleting the added light retires its resources", entry)
                self.before_ground = image
                self.advance("ground-hidden", entry, lambda: self.ground.setDisplayFlag(False))
            elif self.stage == "ground-hidden":
                assert not np.array_equal(image, self.before_ground)
                self.record("Split ground visibility changes the actual rendered image", entry)
                assert entry is self.original_entry
                self.begin_menu_workflow()
        except Exception:
            self.report["error"] = traceback.format_exc()
            self.finish(1)
        finally:
            self.busy = False

    def add_light(self):
        self.new_light = hou.node("/obj").createNode("hlight::2.0", "live_area_test")
        self.new_light.setParms(dict(light_type="grid", light_intensity=2000, singlesided=1))
        self.new_light.parmTuple("areasize").set((3, 2))
        self.new_light.parmTuple("r").set((-50, -30, 0))
        self.new_light.parmTuple("t").set((-3.2, 5, 4))

    def begin_menu_workflow(self):
        self.menu_object = hou.node("/obj").createNode("geo", "menu_object_test")
        assert (
            self.menu_object.parm("cycles_subdivision_override") is None
            and self.menu_object.parm("vm_rendervisibility") is not None
        )
        self.menu_steps = [
            (
                ("Add Object Parameters",),
                self.menu_object,
                lambda: self.menu_object.parm("cycles_subdivision_override") is not None,
            ),
            (
                ("Remove Cycles Parameters",),
                self.menu_object,
                lambda: self.menu_object.parm("cycles_subdivision_override") is None,
            ),
            (
                ("Add Light Parameters",),
                self.area,
                lambda: self.area.parm("cycles_light_visibility_override") is not None,
            ),
            (
                ("Remove Cycles Parameters",),
                self.area,
                lambda: self.area.parm("cycles_light_visibility_override") is None,
            ),
            (
                ("Create Light", "Area"),
                self.area,
                lambda: bool(hou.selectedNodes()) and light_properties.is_cycles_light(hou.selectedNodes()[0]),
            ),
        ]
        self.menu_index = 0
        self.stage = "menu-select"
        self._binding = None

    def menu_tick(self):
        labels, node, check = self.menu_steps[self.menu_index]
        if self.stage == "menu-select":
            node.setCurrent(True, clear_all_selected=True)
            self.stage = "menu-key"
            self.menu_at = time.monotonic()
            return
        if self.stage == "menu-key":
            if time.monotonic() - self.menu_at < 0.2:
                return
            self.click_menu(*labels)
            self.stage = "menu-check"
            self.menu_at = time.monotonic()
            return
        if not check():
            if time.monotonic() - self.menu_at > 3:
                raise AssertionError("Native shelf tool did not execute: " + str(labels))
            return
        if self._binding:
            hou.hotkeys.removeAssignment("h", self._binding[0], self._binding[1])
            self._binding = None
        self.menu_index += 1
        if self.menu_index < len(self.menu_steps):
            self.stage = "menu-select"
            return
        created = hou.selectedNodes()[0]
        assert created.evalParm("cycles_light_type") == "grid"
        created.destroy()
        self.menu_object.destroy()
        self.report["tools"] = "Native shelf actions add/remove optional properties and create a Cycles area light"
        self.finish(0)

    def finish(self, code):
        hou.ui.removeEventLoopCallback(self.callback)
        if getattr(self, "_binding", None):
            hou.hotkeys.removeAssignment("h", self._binding[0], self._binding[1])
        self.report["result"] = "passed" if code == 0 else "failed"
        self.report["seconds"] = time.monotonic() - self.began
        self.save()
        self.panel.dispose()
        registry.close_all()
        self.tab.close()
        self.scene.close()
        hou.exit(code, suppress_save_prompt=True)


test = ArtistSceneWorkflow()
