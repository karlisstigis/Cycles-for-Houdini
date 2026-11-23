# SPDX-License-Identifier: Apache-2.0
"""Artist workflow on the courtyard, including native viewport and panel chrome.

Mouse clicks exercise real UI controls. HOM constructs and edits actual Houdini
nodes; no mock renderer, fake image, or replacement viewport is used.
"""

from support.paths import artifacts_root

from subdivision_test_setup import configure_render, set_subdivision
from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import os
from pathlib import Path
import time
import traceback

import hou
from PySide6 import QtCore, QtWidgets, QtTest
from cycles_houdini import registry, Session
from cycles_houdini.panel import RenderPanel


class ArtistWorkflow:
    def __init__(self):
        self.destination = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
        example = artifacts_root() / "examples/sop-fx-courtyard"
        self.report = dict(
            result="running",
            host_pid=os.getpid(),
            device=os.environ["CYCLES_HOUDINI_GUI_DEVICE"],
            events=[],
            updates=[],
            heartbeat_gaps=[],
            panel_polls=[],
            display_calls=[],
            phase="opening",
        )
        self.started = time.monotonic()
        self.in_tick = False
        self.last_tick = 0
        self.stage = 0
        hou.hipFile.load(str(example / "sop-fx-courtyard-current.hiplc"), suppress_save_prompt=True)
        hou.putenv("OCIO", str(example / "showcase.ocio"))
        hou.Color.reloadOCIO()
        self.rop = hou.node("/out/cycles_sop_courtyard")
        # Native lighting is required for the live area-light test. Preserve the
        # legacy demonstration's numeric key light in this isolated test scene.
        light = hou.node("/obj/courtyard_key_light")
        if light.type().name() != "hlight::2.0":
            light = hou.node("/obj").createNode("hlight::2.0", "artist_key")
            light.parmTuple("t").set(hou.node("/obj/courtyard_key_light").parmTuple("t").eval())
            light.parmTuple("light_color").set((1, 6500 / 8500, 4500 / 8500))
            light.parm("light_intensity").set(34000)
        set_rop_devices(self.rop, self.report["device"])
        configure_render(
            self.rop, dict(source_mode="objects", vobject="fx_courtyard", lights="*", samples=65536, resx=960, resy=540)
        )
        self.key = f"rop:{self.rop.sessionId()}"
        self.scene_view = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.SceneViewer)
        self.scene_view.setPwd(hou.node("/obj"))
        self.scene_view.curViewport().setCamera(hou.node("/obj/courtyard_camera"))
        self.scene_window = hou.qt.floatingPanelWindow(self.scene_view.floatingPanel())
        self.original_poll = RenderPanel.poll

        def measured_poll(panel):
            began = time.monotonic()
            try:
                return self.original_poll(panel)
            finally:
                self.report["panel_polls"].append(dict(phase=self.report["phase"], seconds=time.monotonic() - began))

        RenderPanel.poll = measured_poll
        self.original_display = Session.display_image

        def measured_display(session, **kwargs):
            began = time.monotonic()
            result = self.original_display(session, **kwargs)
            self.report["display_calls"].append(
                dict(
                    phase=self.report["phase"],
                    seconds=time.monotonic() - began,
                    generation=result["generation"],
                    sequence=result["sequence"],
                )
            )
            return result

        Session.display_image = measured_display
        self.panel_tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel)
        self.panel_tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.panel = self.panel_tab.activeInterfaceRootWidget()
        self.panel.set_rop(self.rop)
        self.panel.window().resize(1200, 900)
        self.panel_window = hou.qt.floatingPanelWindow(self.panel_tab.floatingPanel())
        self.original_apply = registry.Entry.apply
        self.original_factory = registry.session_from_configuration

        def diagnostic_session(*args, **kwargs):
            kwargs["collect_diagnostics"] = True
            return self.original_factory(*args, **kwargs)

        registry.session_from_configuration = diagnostic_session

        def measured_apply(entry, update, now, **kwargs):
            began = time.monotonic()
            try:
                return self.original_apply(entry, update, now, **kwargs)
            finally:
                self.report["updates"].append(
                    dict(
                        phase=self.report["phase"],
                        seconds=time.monotonic() - began,
                        status=entry.session.status(),
                        diagnostics=entry.session.diagnostics(),
                    )
                )

        registry.Entry.apply = measured_apply
        self.last_heartbeat = time.monotonic()
        self.heartbeat = QtCore.QTimer(hou.qt.mainWindow())
        self.heartbeat.setInterval(16)
        self.heartbeat.timeout.connect(self.beat)
        self.heartbeat.start()
        self._tick = self.tick
        hou.ui.addEventLoopCallback(self._tick)
        self.report["phase"] = "initial-render"
        self.save()
        QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)

    def save(self):
        self.destination.write_text(json.dumps(self.report, indent=2), encoding="utf-8")

    def beat(self):
        now = time.monotonic()
        gap = now - self.last_heartbeat
        self.last_heartbeat = now
        self.report["heartbeat_gaps"].append(dict(seconds=gap, phase=self.report["phase"]))

    def record(self, name):
        entry = registry.get(self.key)
        self.report["events"].append(
            dict(
                name=name,
                seconds=time.monotonic() - self.started,
                status=entry.session.status() if entry else None,
                diagnostics=entry.session.diagnostics() if entry else None,
                edit_to_display_seconds=time.monotonic() - self.edit_started if hasattr(self, "edit_started") else None,
            )
        )
        self.save()

    def ready(self):
        entry = registry.get(self.key)
        if not entry:
            return False
        if entry.error:
            raise AssertionError(entry.error)
        status = entry.session.status()
        frame = entry.session.frame_info()
        if status["state"] == "failed":
            raise AssertionError(status["error"])
        return (
            frame
            and frame["generation"] == status["generation"]
            and status["generation"] > self.before
            and self.panel.canvas.generation == status["generation"]
        )

    def advance(self, phase):
        entry = registry.get(self.key)
        self.before = entry.session.status()["generation"]
        self.report["phase"] = phase
        self.edit_started = time.monotonic()

    def click_gear(self):
        self.panel_tab.showToolbar(True)
        self.panel_window = hou.qt.floatingPanelWindow(self.panel_tab.floatingPanel())
        self.panel_window.grab().save(str(self.destination.with_suffix(".before-gear.png")))
        QtCore.QTimer.singleShot(350, self.close_menu)
        QtTest.QTest.mouseClick(
            self.panel_window, QtCore.Qt.LeftButton, pos=QtCore.QPoint(self.panel_window.width() - 85, 30)
        )

    def close_menu(self):
        for window in QtWidgets.QApplication.topLevelWidgets():
            if (
                window.isVisible()
                and not window.windowTitle()
                and 100 <= window.width() <= 450
                and window.height() > 60
            ):
                window.grab().save(str(self.destination.with_suffix(".gear-menu.png")))
                QtTest.QTest.keyClick(window, QtCore.Qt.Key_Escape)
        self.record("Opened and dismissed the Python Panel gear menu while rendering")

    def move_light(self):
        if getattr(self, "finished", False):
            return
        self.moves += 1
        self.area.parmTuple("t").set((-3 + self.moves * 0.18, 5, 4))
        self.area.setCurrent(True, clear_all_selected=True)
        self.scene_view.enterCurrentNodeState()
        if self.moves < 15:
            QtCore.QTimer.singleShot(40, self.move_light)
        else:
            self.moved_at = time.monotonic()

    def tick(self):
        if self.in_tick or time.monotonic() - self.last_tick < 0.05:
            return
        self.in_tick = True
        self.last_tick = time.monotonic()
        try:
            entry = registry.get(self.key)
            self.report["current"] = dict(stage=self.stage, state=entry.session.status() if entry else None)
            if time.monotonic() - self.started > 100:
                raise TimeoutError("Artist workflow stalled")
            if self.stage == 0:
                self.before = 0
                if not self.ready():
                    return
                self.record("Clicked Render; courtyard appears in the actual panel")
                self.advance("create-area-light")
                self.stage = 1
                self.area = hou.node("/obj").createNode("hlight::2.0", "artist_area")
                self.area.setParms(dict(light_type="grid", light_intensity=3500))
                self.area.parmTuple("areasize").set((3, 2))
                self.area.parmTuple("t").set((-3, 5, 4))
                self.area.parmTuple("r").set((-50, -30, 0))
                self.area.setCurrent(True, clear_all_selected=True)
                self.scene_view.enterCurrentNodeState()
                self.save()
            elif self.stage == 1 and self.ready():
                assert entry.session.status()["light_resources"] == 3
                self.record("Added and selected a native two-sided area light during IPR")
                self.advance("drag-area-light")
                self.stage = 2
                self.moves = 0
                self.moved_at = 0
                self.move_light()
                QtCore.QTimer.singleShot(220, self.click_gear)
            elif self.stage == 2 and self.moved_at and self.ready():
                self.record("Moved the selected area light fifteen times while its native viewport handle was active")
                self.advance("geometry-deformation")
                self.stage = 3
                self.hero = hou.node("/obj/fx_courtyard/TWISTED_TURBINE")
                self.hero.parm("twist").set(3.1)
                self.hero.setCurrent(True, clear_all_selected=True)
                self.scene_view.setPwd(self.hero.parent())
                self.save()
            elif self.stage == 3 and self.ready():
                self.record("Reloaded the turbine geometry with the native viewport visible")
                self.advance("noise-displacement")
                self.stage = 4
                hou.node("/mat/courtyard_noise_displaced_stone").parm("rock_relief").set(0.23)
                self.save()
            elif self.stage == 4 and self.ready():
                self.record("Changed true noise displacement interactively")
                self.advance("camera-edit")
                self.stage = 7
                self.conversions = entry.session.status()["geometry_conversions"]
                camera = hou.node("/obj/courtyard_camera")
                camera.parm("tx").set(camera.evalParm("tx") + 0.1)
            elif self.stage == 7 and self.ready():
                assert entry.session.status()["geometry_conversions"] == self.conversions
                copies = entry.session.diagnostics()["buffer_copy_requests"]
                assert not {"tri_verts", "tri_vindex", "tri_shader", "curve_keys", "points"} & copies.keys(), copies
                self.record("Camera edit retains geometry and uploads no mesh buffers")
                self.advance("object-transform")
                self.stage = 8
                hou.node("/obj/fx_courtyard").parm("tx").set(0.1)
            elif self.stage == 8 and self.ready():
                assert entry.session.status()["geometry_conversions"] == self.conversions
                copies = entry.session.diagnostics()["buffer_copy_requests"]
                assert not {"tri_verts", "tri_vindex", "tri_shader", "curve_keys", "points"} & copies.keys(), copies
                self.record("Object transform retains geometry and updates acceleration instances")
                self.advance("surface-material")
                self.stage = 9
                hou.node("/mat/courtyard_blue_steel/surface_shader").parm("roughness").set(0.45)
            elif self.stage == 9 and self.ready():
                assert entry.session.status()["geometry_conversions"] == self.conversions
                self.record("Surface roughness edit retains SOP geometry")
                self.advance("geometry-topology")
                self.stage = 10
                self.hero.parm("blades").set(11)
            elif self.stage == 10 and self.ready():
                assert entry.session.status()["geometry_conversions"] > self.conversions
                self.record("Changing native blade count updates topology during IPR")
                self.report["phase"] = "panel-chrome"
                self.stage = 5
                self.reload_at = time.monotonic()
                self.settings_pane = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm)
                self.settings_pane.setCurrentNode(self.rop)
                self.click_gear()
                self.save()
            elif self.stage == 5 and time.monotonic() - self.reload_at > 1:
                assert self.settings_pane.currentNode() == self.rop
                self.report["native_parameter_folders"] = {
                    name: list(self.rop.parm(name).containingFolders())
                    for name in ("samples", "use_denoise", "compute_backend", "soppath", "dicing_rate", "picture")
                }
                hou.qt.floatingPanelWindow(self.settings_pane.floatingPanel()).grab().save(
                    str(self.destination.with_suffix(".settings.png"))
                )
                self.settings_pane.close()
                self.retained_session = entry.session
                self.report["phase"] = "reload-panel"
                self.stage = 6
                self.reload_at = time.monotonic()
                self.panel_tab.reloadActiveInterface()
                self.panel = self.panel_tab.activeInterfaceRootWidget()
                self.panel.set_rop(self.rop)
                self.save()
            elif self.stage == 6 and time.monotonic() - self.reload_at > 1:
                assert self.panel.entry.session is self.retained_session and len(registry.entries()) == 1
                self.record("Reloaded the panel interface during IPR and retained the active session")
                self.panel.grab().save(str(self.destination.with_suffix(".panel.png")))
                hou.qt.floatingPanelWindow(self.scene_view.floatingPanel()).grab().save(
                    str(self.destination.with_suffix(".viewport.png"))
                )
                self.viewer = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.IPRViewer)
                self.viewer.setRopNode(self.rop)
                self.viewer.setPreview(True)
                self.viewer.setAutoUpdate(True)
                self.advance("native-ipr-join")
                self.join_reuses = entry.session.status()["unchanged_reuses"]
                self.join_physical = entry.session._controller.physical
                self.join_conversions = entry.session.status()["geometry_conversions"]
                self.stage = 11
                self.viewer.startRender()
            elif self.stage == 11 and entry.helper_pid and self.viewer.isActive():
                assert self.panel.entry is entry and len(registry.entries()) == 1
                status = entry.session.status()
                if status["pending_update"] or status["unchanged_reuses"] <= self.join_reuses:
                    return
                assert status["unchanged_reuses"] == self.join_reuses + 1, status
                assert entry.session._controller.physical == self.join_physical, "Joining IPR reset the renderer"
                assert status["geometry_conversions"] == self.join_conversions
                if self.panel.canvas.generation != status["generation"]:
                    return
                # The second viewer must receive the existing image, not merely
                # attach a control pipe. This sky pixel has known linear color.
                try:
                    pixel = self.viewer.pixel("C", 10, 530)[:3]
                except hou.OperationFailed:
                    return
                if max(abs(a - b) for a, b in zip(pixel, (0.025, 0.04, 0.065))) > 0.001:
                    return
                self.record("Native IPR joins the same courtyard session with the viewport visible")
                self.advance("native-ipr-geometry-edit")
                self.stage = 12
                self.hero.parm("twist").set(3.35)
            elif self.stage == 12 and self.ready():
                pixel = self.viewer.pixel("C", 480, 270)
                assert all(abs(v) < 1e6 for v in pixel)
                self.record("Edited native SOP deformation with IPR, panel and viewport all open")
                self.panel.grab().save(str(self.destination.with_suffix(".panel.png")))
                self.finish(0)
        except Exception:
            self.report["error"] = traceback.format_exc()
            self.report["sop_errors"] = {
                node.path(): node.errors() for node in hou.node("/obj/fx_courtyard").children() if node.errors()
            }
            self.finish(1)
        finally:
            self.in_tick = False

    def finish(self, code):
        self.finished = True
        hou.ui.removeEventLoopCallback(self._tick)
        registry.Entry.apply = self.original_apply
        registry.session_from_configuration = self.original_factory
        try:
            self.heartbeat.stop()
        except RuntimeError:
            pass  # The panel reload can delete this timer's parent.
        gaps = sorted(value["seconds"] for value in self.report["heartbeat_gaps"])
        self.report["heartbeat_summary"] = dict(
            max_seconds=max(gaps, default=0), p95_seconds=gaps[int(0.95 * (len(gaps) - 1))] if gaps else 0
        )
        self.report["result"] = "passed" if not code else "failed"
        entry = registry.get(self.key)
        if hasattr(self, "viewer"):
            self.viewer.killRender()
            self.viewer.close()
        self.panel.dispose()
        registry.close_all()
        RenderPanel.poll = self.original_poll
        Session.display_image = self.original_display
        self.report["closed"] = entry.session.status() if entry else None
        self.save()
        assert self.report["closed"]["device_allocation_bytes"] == self.report["closed"]["owned_geometry_bytes"] == 0
        self.panel_tab.close()
        self.scene_view.close()
        hou.exit(code, suppress_save_prompt=True)


test = ArtistWorkflow()
