# SPDX-License-Identifier: Apache-2.0
"""Actual viewport, panel and native IPR worker workflow on a scaled SOP courtyard.

Run in a fresh isolated Houdini process using run-prototype-test.ps1.
Exploratory timings include native host cooking. No timing assertions advertise
production budgets; correctness/lifecycle conditions are mandatory.
"""

from support.paths import artifacts_root

from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import math
import os
from pathlib import Path
import time
import traceback
import hou
from PySide6 import QtCore, QtTest, QtWidgets
import cycles_houdini as ch
from cycles_houdini import registry, native_scene, device_selection


class HeavyResponse:
    create_render_panel = True

    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
        example = artifacts_root() / "examples/sop-fx-courtyard"
        self.mode = "worker"
        self.repetitions = int(os.environ.get("CYCLES_HOUDINI_BENCH_REPETITIONS", "3"))
        self.samples = int(os.environ.get("CYCLES_HOUDINI_BENCH_SAMPLES", "65536"))
        self.shared_views = os.environ.get("CYCLES_HOUDINI_BENCH_SHARED_VIEWS", "0") == "1"
        self.device = os.environ["CYCLES_HOUDINI_GUI_DEVICE"]
        self.report = dict(
            result="running",
            execution=self.mode,
            device=self.device,
            host_pid=os.getpid(),
            settings=dict(width=960, height=540, samples=self.samples, denoising=False),
            edits=[],
            applies=[],
            heartbeat_gaps=[],
            phase="opening",
            render_viewers=["cycles", "native"] if self.shared_views else ["cycles"],
            scope="Real Houdini viewport; HOM parameter edits and QtTest controls",
        )
        self.began = time.monotonic()
        self.load_scene(root, example)
        self.obj = hou.node("/obj/fx_courtyard")
        self.hero = self.obj.node("TWISTED_TURBINE")
        self.ground = self.obj.node("NOISE_DISPLACED_GROUND")
        self.ground.parm("resolution").set(512)
        self.obj.node("NOISE_DISPLACED_ROCKS").parm("count").set(80)
        self.obj.node("ROCK_PROTOTYPE_SPHERE").setParms(dict(rows=36, cols=72))
        self.obj.node("CURVE_VEGETATION_AND_CABLES").parm("count").set(600)
        self.obj.node("SPARK_PARTICLES").parm("count").set(1200)
        self.obj.node("INSTANCE_GRID").setParms(dict(rows=17, cols=21))
        self.obj.node("STABLE_INSTANCE_POINTS").parm("snippet").set(
            "int x=int(rint((@P.x/8.64+.5)*20)); int z=int(rint((@P.z/6.24+.5)*16));\n"
            "if(abs(x-10)<2 && abs(z-8)<2) removepoint(0,@ptnum);\n"
            "i@id=1000+z*21+x; v@N={0,0,1}; v@up={0,1,0};"
        )
        self.camera = hou.node("/obj/courtyard_camera")
        self.camera_x = self.camera.parm("tx").eval()
        self.color = hou.node("/mat/courtyard_burnished_brass/surface_shader")
        self.stone = hou.node("/mat/courtyard_noise_displaced_stone")
        self.rop = hou.node("/out/cycles_sop_courtyard")
        set_rop_devices(self.rop, self.device)
        self.rop.setParms(
            dict(resx=960, resy=540, samples=self.samples, source_mode="objects", vobject="fx_courtyard", lights="*")
        )
        self.configure_scene()
        self.report["expected_device_ids"] = device_selection.set_devices(self.rop, self.device)
        self.key = f"rop:{self.rop.sessionId()}"
        self.area = None
        # Inventory uses the actual owned adapter output, before the measurement
        # window. It also warms Houdini's SOP cooks identically in all modes.
        capture = ch._native.create(self.device, 960, 540, self.samples, True, False, True, False, None, True)
        owned = ch._native.apply(capture, native_scene.capture(self.rop), hou.time())
        self.report["fixture"] = ch._native.snapshot_info(owned)
        self.report["fixture"]["source_points"] = self.obj.renderNode().geometry().intrinsicValue("pointcount")
        self.report["fixture"]["source_primitives"] = self.obj.renderNode().geometry().intrinsicValue("primitivecount")
        ch._native.close(capture)
        del owned, capture
        self.scene = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.SceneViewer)
        self.scene.setPwd(hou.node("/obj"))
        self.scene.curViewport().setCamera(self.camera)
        self.tab = self.panel = None
        self.settings_pane = None
        if self.create_render_panel:
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel)
            self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
            self.panel = self.tab.activeInterfaceRootWidget()
            self.panel.set_rop(self.rop)
            self.panel.window().resize(1180, 840)
        self.viewer = None
        self.original_factory = registry.session_from_configuration

        def diagnostic_factory(*args, **kwargs):
            return self.original_factory(*args, **dict(kwargs, collect_diagnostics=True))

        registry.session_from_configuration = diagnostic_factory
        self.original_apply = registry.Entry.apply

        def measured_apply(entry, *args, **kwargs):
            started = time.perf_counter()
            try:
                return self.original_apply(entry, *args, **kwargs)
            finally:
                self.report["applies"].append(
                    dict(
                        phase=self.report["phase"],
                        host_seconds=time.perf_counter() - started,
                        status=entry.session.status(),
                    )
                )

        registry.Entry.apply = measured_apply
        self.stage = "initial"
        self.next_at = 0.0
        self.case = None
        self.index = 0
        self.before = 0
        self.tick_busy = False
        self.last_beat = time.monotonic()
        self.timer = QtCore.QTimer(hou.qt.mainWindow())
        self.timer.setInterval(16)
        self.timer.timeout.connect(self.beat)
        self.timer.start()
        self.callback = self.tick
        hou.ui.addEventLoopCallback(self.callback)
        self.report["phase"] = "initial-render"
        self.save()
        if self.create_render_panel:
            QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)

    def load_scene(self, root, example):
        """Load the benchmark's authored scene; subclasses may generate isolated inputs."""
        hou.hipFile.load(str(example / "sop-fx-courtyard-current.hiplc"), suppress_save_prompt=True)
        hou.putenv("OCIO", str(example / "showcase.ocio"))
        hou.Color.reloadOCIO()

    def configure_scene(self):
        """Optional fixture edits before inventory and the initial render."""

    def save(self):
        self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")

    def beat(self):
        now = time.monotonic()
        self.report["heartbeat_gaps"].append(dict(phase=self.report["phase"], seconds=now - self.last_beat))
        self.last_beat = now

    def ready(self, entry):
        if entry.error:
            raise RuntimeError(entry.error)
        status = entry.session.status()
        if status.get("renderer_pid"):
            assert tuple(status["render_device_ids"]) == tuple(self.report["expected_device_ids"]), status
        if status["state"] == "failed":
            raise RuntimeError(status["error"])
        frame = entry.session.frame_info()
        return (
            frame
            and frame["generation"] == status["generation"]
            and frame.get("current_scene", True)
            and not status.get("pending_update")
            and not status.get("capture_pending")
            and getattr(self.panel.canvas, "frame_metadata", {}).get("render_revision") == status.get("render_revision")
            and self.panel.canvas.generation == status["generation"]
            and frame["rendered_width"] == 960
            and frame["rendered_height"] == 540
            and status["generation"] > self.before
        )

    def edit(self, name, repetition):
        t = repetition % 2
        if name == "camera":
            self.camera.parm("tx").set(self.camera_x + 0.08 * (1 + t))
        elif name == "transform":
            self.obj.parm("tx").set(0.03 * (1 + t))
        elif name == "surface-color":
            self.color.parmTuple("base_color").set((0.42 + 0.04 * t, 0.19, 0.07))
        elif name == "mesh-deform":
            self.hero.parm("twist").set(2.7 + 0.15 * t)
            self.hero.setCurrent(True, clear_all_selected=True)
            self.scene.setPwd(self.obj)
        elif name == "mesh-topology":
            self.ground.parm("resolution").set(513 + t)
        elif name == "noise-displacement":
            self.stone.parm("rock_relief").set(0.22 + 0.04 * t)
        elif name == "curve-width":
            self.obj.node("BEND_AND_TAPER_CURVES").parm("snippet").set(
                "float t=@P.y; @P.x=.16*t*t; f@width=" + str(0.018 + 0.003 * t) + "*(1-t)+.002;"
            )
        elif name == "point-radius":
            self.obj.node("SPARK_PARTICLES").parm("snippet").set(
                self.point_code + "\nf@pscale *= " + str(1.1 + 0.2 * t) + ";"
            )
        elif name == "volume-transform":
            self.obj.node("PLUME_POSITION").parm("tx").set(-1.68 + 0.05 * t)
        elif name == "area-light":
            if self.area is None:
                self.area = hou.node("/obj").createNode("hlight::2.0", "response_area")
                self.area.setParms(dict(light_type="grid", light_intensity=2000))
                self.area.parmTuple("areasize").set((3, 2))
                self.area.parmTuple("r").set((-50, -30, 0))
            self.area.parmTuple("t").set((-3.2 + 0.15 * repetition, 5, 4))
            self.area.setCurrent(True, clear_all_selected=True)
            self.scene.setPwd(hou.node("/obj"))
            self.scene.enterCurrentNodeState()

    def begin_edit(self, entry):
        names = (
            "camera",
            "transform",
            "surface-color",
            "mesh-deform",
            "mesh-topology",
            "noise-displacement",
            "curve-width",
            "point-radius",
            "volume-transform",
            "area-light",
        )
        if self.index == len(names) * self.repetitions:
            self.stage = "settings"
            self.report["phase"] = "native-settings"
            self.settings_pane = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm)
            self.settings_pane.setCurrentNode(self.rop)
            self.next_at = time.monotonic() + 0.5
            return
        name = names[self.index // self.repetitions]
        repetition = self.index % self.repetitions
        self.report["phase"] = name + "-" + str(repetition)
        self.before = entry.session.status()["generation"]
        self.before_status = entry.session.status()
        self.edit_started = time.monotonic()
        self.case = dict(name=name, repetition=repetition, phase=self.report["phase"])
        self.edit(name, repetition)
        self.case["parameter_action_seconds"] = time.monotonic() - self.edit_started
        self.stage = "editing"
        self.save()

    def finish(self):
        self.timer.stop()
        hou.ui.removeEventLoopCallback(self.callback)
        registry.Entry.apply = self.original_apply
        registry.session_from_configuration = self.original_factory
        if self.viewer:
            try:
                self.viewer.killRender()
            except hou.OperationFailed:
                pass
            self.viewer.close()
        if self.panel:
            self.panel.dispose()
        registry.close_all()
        self.report["result"] = "passed"
        self.report["wall_seconds"] = time.monotonic() - self.began
        self.save()
        if self.tab:
            self.tab.close()
        self.scene.close()
        hou.exit(0, suppress_save_prompt=True)

    def tick(self):
        if self.tick_busy:
            return
        self.tick_busy = True
        try:
            if time.monotonic() - self.began > 165:
                raise TimeoutError("Heavy courtyard workflow exceeded its limit")
            entry = registry.get(self.key)
            if entry is None:
                return
            if self.stage == "initial" and self.ready(entry):
                self.report["cold_first_canvas_seconds"] = time.monotonic() - self.began
                self.before = 0
                if self.shared_views:
                    self.viewer = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.IPRViewer)
                    self.viewer.setRopNode(self.rop)
                    self.viewer.setPreview(True)
                    self.viewer.setAutoUpdate(True)
                    self.viewer.setDelay(0.02)
                    self.viewer.startRender()
                self.stage = "attach"
                self.next_at = time.monotonic() + (2.0 if self.shared_views else 0.2)
            elif self.stage == "attach" and time.monotonic() > self.next_at and self.ready(entry):
                assert len(registry.entries()) == 1 and self.panel.entry is entry
                if self.viewer is not None:
                    native_pixel = list(self.viewer.pixel("C", 480, 270))
                    assert all(math.isfinite(v) for v in native_pixel) and any(v > 0.01 for v in native_pixel[:3]), (
                        native_pixel
                    )
                    self.report["native_pixel"] = native_pixel
                self.report["renderer_pid"] = entry.session.status().get("renderer_pid", os.getpid())
                self.report["memory_before"] = ch.process_memory()
                self.point_code = self.obj.node("SPARK_PARTICLES").parm("snippet").eval()
                self.report["phase"] = "memory-observation"
                self.memory_event = None
                if self.mode != "sync":
                    self.memory_event = entry.session._controller.control("memory")
                self.stage = "memory"
                self.next_at = time.monotonic() + 0.25
            elif self.stage == "memory" and time.monotonic() > self.next_at:
                if self.memory_event is not None and not self.memory_event.is_set():
                    return
                if self.mode != "sync":
                    self.report["renderer_memory_before"] = entry.session._controller.backend_memory
                self.begin_edit(entry)
            elif self.stage == "editing" and self.ready(entry):
                self.case["edit_to_canvas_seconds"] = time.monotonic() - self.edit_started
                status = entry.session.status()
                diagnostics = entry.session.diagnostics()
                self.case.update(
                    status=status,
                    diagnostics=diagnostics,
                    extraction_delta=status["extractions"] - self.before_status["extractions"],
                    geometry_conversion_delta=status["geometry_conversions"]
                    - self.before_status["geometry_conversions"],
                    cooking_delta=status["cooking_seconds"] - self.before_status["cooking_seconds"],
                )
                if self.case["name"] in ("camera", "transform"):
                    assert self.case["geometry_conversion_delta"] == 0, self.case
                    assert (
                        not {"tri_verts", "tri_vindex", "curve_keys", "points"}
                        & diagnostics["buffer_copy_requests"].keys()
                    ), diagnostics
                if self.mode != "sync":
                    assert diagnostics["session_cancel_calls"] == 0, diagnostics
                    assert status["renderer_pid"] == self.report["renderer_pid"], status
                assert len(registry.entries()) == 1 and self.panel.entry is entry
                self.report["edits"].append(self.case)
                self.index += 1
                self.stage = "steady"
                self.report["phase"] = "steady"
                self.next_at = time.monotonic() + 0.2
                self.save()
            elif self.stage == "steady" and time.monotonic() > self.next_at:
                self.begin_edit(entry)
            elif self.stage == "settings" and time.monotonic() > self.next_at:
                assert self.settings_pane.currentNode() == self.rop
                hou.qt.floatingPanelWindow(self.tab.floatingPanel()).grab().save(
                    str(self.path.with_suffix(".panel.png"))
                )
                self.report["memory_after"] = ch.process_memory()
                if self.mode != "sync":
                    self.memory_event = entry.session._controller.control("memory")
                self.stage = "stop"
                self.report["phase"] = "stop"
            elif self.stage == "stop":
                if self.memory_event is not None and not self.memory_event.is_set():
                    return
                if self.mode != "sync":
                    self.report["renderer_memory_after"] = entry.session._controller.backend_memory
                started = time.perf_counter()
                self.panel.cancel()
                self.report["cancel_submission_seconds"] = time.perf_counter() - started
                self.stage = "cancelled"
                self.next_at = time.monotonic() + 0.1
            elif (
                self.stage == "cancelled"
                and time.monotonic() > self.next_at
                and entry.session.status()["state"] == "cancelled"
            ):
                self.report["final_diagnostics"] = entry.session.diagnostics()
                self.finish()
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


if __name__ == "__main__":
    workflow = HeavyResponse()
