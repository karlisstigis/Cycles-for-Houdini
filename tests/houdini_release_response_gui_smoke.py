# SPDX-License-Identifier: Apache-2.0
"""Warm one-million-triangle, 960x540 edit-to-paint measurement.

One native viewport and one Cycles Viewer, persistent worker, programmatic
Houdini parameter edits. Qt paint completion is measured; physical input and
compositor presentation are not. Prospective latency budgets are reported
separately from mandatory correctness and lifecycle assertions.
"""

import json
import math
import os
from pathlib import Path
import statistics
import time
import traceback

import hou
from PySide6 import QtCore, QtTest
import cycles_houdini as ch
from cycles_houdini import device_selection, menu_actions, native_scene, registry


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.repetitions = int(os.environ.get("CYCLES_HOUDINI_RELEASE_REPETITIONS", "30"))
        assert 5 <= self.repetitions <= 200
        self.root = hou.node("/obj").createNode("subnet", "release_response")
        self.obj = self.root.createNode("geo", "million_triangles", run_init_scripts=False)
        grid = self.obj.createNode("grid", "receiver")
        grid.setParms(dict(type=0, orient=0, rows=501, cols=1001))
        grid.parmTuple("size").set((4, 2.25))
        # Explicit source inventory; no instancing or hidden additional meshes.
        geo = grid.geometry()
        assert geo.intrinsicValue("pointcount") == 501 * 1001
        assert geo.intrinsicValue("primitivecount") == 500 * 1000
        self.camera = self.root.createNode("cam", "camera")
        self.camera.setParms(dict(tz=5, projection=1, orthowidth=4, near=0.01, far=10000))
        material = ch.create_material(name="release_response_diffuse", shader="diffuse_bsdf")
        self.shader = material.node("surface_shader")
        self.shader.parmTuple("color").set((0.6, 0.2, 0.1))
        self.obj.parm("shop_materialpath").set(material.path())
        self.rop = menu_actions.create_rop()
        self.rop.setParms(
            dict(
                objectroot=self.root.path(),
                vobject="*",
                lights="",
                camera=self.camera.path(),
                resx=960,
                resy=540,
                resolution_percent=100,
                ipr_resolution_percent=0,
                samples=8,
                ipr_samples=8,
                use_denoise=0,
                ipr_use_denoise=0,
                use_texture_cache=0,
                allowmotionblur=0,
                world_source="color",
                film_exposure=1,
            )
        )
        self.rop.parmTuple("background").set((0.5, 0.5, 0.5))
        device = os.environ["CYCLES_HOUDINI_GUI_DEVICE"]
        self.devices = device_selection.set_devices(
            self.rop, json.loads(device) if device.startswith("[") else [device]
        )
        config = native_scene.configuration(self.rop)
        entry = registry.for_rop(self.rop, config)
        entry.session.close(wait=True)
        entry.session = registry.session_from_configuration(config, collect_diagnostics=True)
        self.scene = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.SceneViewer)
        self.scene.setPwd(self.root)
        self.scene.curViewport().setCamera(self.camera)
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(1100, 760))
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.key = f"rop:{self.rop.sessionId()}"
        self.panel = self.entry = self.session = None
        self.paint_original = self.canvas_type = None
        self.paints = {}
        self.case = None
        self.steps = []
        for kind in ("camera", "transform", "material"):
            self.steps.append((kind, -1))  # Reset and warm this isolated block.
            self.steps.extend((kind, i) for i in range(self.repetitions))
        self.report = dict(
            result="running",
            scope=__doc__,
            device_ids=self.devices,
            fixture=dict(
                triangles=1000000,
                width=960,
                height=540,
                samples=8,
                denoising=False,
                adaptive_subdivision=False,
                packed_instances=0,
            ),
            percentile="nearest rank",
            records=[],
            warmup_records=[],
        )
        self.index = 0
        self.phase = "mount"
        self.busy = self.finished = False
        self.started = self.beat_at = time.monotonic()
        self.heartbeats = []
        self.timer = QtCore.QTimer()
        self.timer.setInterval(10)
        self.timer.timeout.connect(self.beat)
        self.timer.start()
        hou.ui.addEventLoopCallback(self.tick)

    def beat(self):
        now = time.monotonic()
        self.heartbeats.append(now - self.beat_at)
        self.beat_at = now

    def save(self):
        self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")

    def edit(self, status):
        kind, repeat = self.steps[self.index]
        self.case = dict(kind=kind, repetition=repeat, before=status, input_at=time.monotonic())
        if repeat < 0:
            self.camera.parm("tx").set(0.01 if kind == "camera" else 0)
            self.obj.parm("tx").set(0.01 if kind == "transform" else 0)
            self.shader.parmTuple("color").set((0.61, 0.2, 0.1) if kind == "material" else (0.6, 0.2, 0.1))
        else:
            value = 0.02 if repeat % 2 else -0.02
            if kind == "camera":
                self.camera.parm("tx").set(value)
            elif kind == "transform":
                self.obj.parm("tx").set(value)
            else:
                self.shader.parmTuple("color").set((0.62 if repeat % 2 else 0.58, 0.2, 0.1))
        self.case["parameter_action_seconds"] = time.monotonic() - self.case["input_at"]
        case = self.case
        QtCore.QTimer.singleShot(0, lambda: case.update(ui_ready_at=time.monotonic()))

    def tick(self):
        if self.finished or self.busy:
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
                self.timer.stop()
                self.save()
                hou.ui.removeEventLoopCallback(self.tick)
                hou.ui.postEventCallback(lambda: hou.exit(1 if self.error else 0, suppress_save_prompt=True))
                return
            assert time.monotonic() - self.started < 1200, (self.phase, self.index)
            step_started = self.case["input_at"] if self.case else self.started
            assert time.monotonic() - step_started < 60, (self.phase, self.index, self.report.get("last_status"))
            if self.phase == "mount":
                self.panel = self.tab.activeInterfaceRootWidget()
                if self.panel is None:
                    return
                self.panel.set_rop(self.rop)
                self.canvas_type = type(self.panel.canvas)
                self.paint_original = self.canvas_type.paintEvent

                def paint(canvas, event):
                    self.paint_original(canvas, event)
                    if canvas is self.panel.canvas:
                        metadata = dict(getattr(canvas, "frame_metadata", {}))
                        key = metadata.get("generation"), metadata.get("render_revision")
                        self.paints.setdefault(key, dict(at=time.monotonic(), metadata=metadata))

                self.canvas_type.paintEvent = paint
                QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                self.phase = "running"
            self.entry = registry.get(self.key)
            if self.entry is None:
                return
            status, info = self.entry.session.status(), self.entry.session.frame_info()
            self.report["last_status"] = status
            assert not self.entry.error and status["state"] != "failed", (self.entry.error, status)
            if self.case:
                candidates = [
                    p
                    for (generation, revision), p in list(self.paints.items())
                    if generation is not None
                    and generation > self.case["before"]["generation"]
                    and revision != self.case["before"]["render_revision"]
                    and p["at"] >= self.case["input_at"]
                ]
                if candidates and "first_paint" not in self.case:
                    self.case["first_paint"] = min(candidates, key=lambda p: p["at"])
            metadata = getattr(self.panel.canvas, "frame_metadata", {})
            if (
                self.entry.change_serial != self.entry.applied_serial
                or self.entry.requested_serial
                or status["state"] != "complete"
                or status.get("pending_update")
                or status.get("capture_pending")
                or not info
                or not info["complete"]
                or not info.get("current_scene")
                or info["generation"] != status["generation"]
                or metadata.get("generation") != status["generation"]
                or metadata.get("render_revision") != status["render_revision"]
            ):
                return
            assert status["renderer_pid"] > 0 and len(registry.entries()) == 1
            assert (
                not status["source_diagnostics"] and status["geometry_resources"] == status["object_resources"] == 1
            ), status
            if self.case:
                if status["generation"] <= self.case["before"]["generation"] or "first_paint" not in self.case:
                    return
                before = self.case["before"]
                assert status["renderer_pid"] == before["renderer_pid"]
                assert status["geometry_conversions"] == before["geometry_conversions"], (self.case["kind"], status)
                diagnostics = self.entry.session.diagnostics()
                assert (
                    not {"tri_verts", "tri_vindex", "curve_keys", "points"} & diagnostics["buffer_copy_requests"].keys()
                ), diagnostics
                self.case.update(
                    completed=status,
                    total_seconds=self.case["first_paint"]["at"] - self.case["input_at"],
                    completed_seconds=time.monotonic() - self.case["input_at"],
                    reported_cooking_seconds=status["cooking_seconds"],
                    reported_extraction_seconds=status["extraction_seconds"],
                    diagnostics=diagnostics,
                    host_memory=ch.process_memory(),
                )
                (self.report["warmup_records"] if self.case["repetition"] < 0 else self.report["records"]).append(
                    self.case
                )
                self.index += 1
                self.save()
            if self.index == len(self.steps):
                self.summarize()
                self.close()
            else:
                self.edit(status)
        except BaseException:
            if self.phase == "closing":
                self.report.update(result="failed", error=traceback.format_exc())
                self.finished = True
                self.timer.stop()
                self.save()
                hou.ui.removeEventLoopCallback(self.tick)
                hou.ui.postEventCallback(lambda: hou.exit(1, suppress_save_prompt=True))
            else:
                self.close(traceback.format_exc())
        finally:
            self.busy = False

    def summarize(self):
        distributions = {}
        for kind in ("camera", "transform", "material"):
            values = sorted(r["total_seconds"] for r in self.report["records"] if r["kind"] == kind)
            p95 = values[math.ceil(len(values) * 0.95) - 1]
            budget = 0.15 if kind == "camera" else 0.5
            distributions[kind] = dict(
                count=len(values),
                median_seconds=statistics.median(values),
                p95_seconds=p95,
                maximum_seconds=max(values),
                prospective_budget_seconds=budget,
                budget_met=p95 <= budget if kind == "camera" else p95 < budget,
            )
        self.report.update(
            distributions=distributions,
            prospective_latency_budgets_met=all(d["budget_met"] for d in distributions.values()),
            heartbeat=dict(count=len(self.heartbeats), maximum_seconds=max(self.heartbeats)),
            memory_scope="Host process observations only; no worker/device plateau certificate.",
        )
        self.panel.grab().save(str(self.path.with_suffix(".png")))

    def close(self, error=None):
        self.error = error
        self.session = self.entry.session if self.entry else None
        if self.canvas_type:
            self.canvas_type.paintEvent = self.paint_original
        if self.panel:
            self.panel.dispose()
        registry.close_all()
        self.phase = "closing"
        self.deadline = time.monotonic() + 30


workflow = Workflow()
