# SPDX-License-Identifier: Apache-2.0
"""Exploratory edit-to-Qt-paint timings, including navigation during geometry capture.

One persistent worker and one viewer; scripted native parameter edits. This is
not physical mouse/compositor latency certification or a production timing gate.
"""

import json
import os
import time
import traceback
from pathlib import Path
import hou
import numpy as np
from PySide6 import QtCore, QtTest
import cycles_houdini as ch
from cycles_houdini import registry
from packed_deformation_fixture import Fixture


class Benchmark:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.f = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        f = self.f
        self.prototype_rows = int(os.environ.get("CYCLES_HOUDINI_PACKED_PREVIEW_ROWS", "721"))
        self.instance_count = int(os.environ.get("CYCLES_HOUDINI_PACKED_PREVIEW_COUNT", "16"))
        assert self.prototype_rows >= 2
        assert self.instance_count == 16 or (self.instance_count >= 1000 and self.instance_count % 100 == 0)
        f.width, f.height = 960, 540
        f.motion.parm("test_rows").set(self.prototype_rows)
        rows, columns = (4, 4) if self.instance_count == 16 else (self.instance_count // 100, 100)
        f.targets.setParms(dict(rows=rows, cols=columns))
        f.targets.parmTuple("size").set((6, 6))
        if self.instance_count > 16:
            # Retain visible gaps rather than cover the complete camera with
            # overlapping identical emitting planes in the density stress case.
            f.grid.parmTuple("size").set((min(0.4, 3.9 / (columns - 1)), min(0.8, 3.9 / (rows - 1))))
        self.rop = f.rop(f.nested, interactive_samples=32)
        light_tree = os.environ.get("CYCLES_HOUDINI_PREVIEW_LIGHT_TREE", "1")
        assert light_tree in ("0", "1")
        self.rop.parm("light_tree").set(int(light_tree))
        self.material_mode = os.environ.get("CYCLES_HOUDINI_PREVIEW_MATERIAL", "diffuse")
        assert self.material_mode in ("diffuse", "emission", "diagnostic")
        if self.material_mode == "diffuse":
            material = ch.create_material(name="preview_diffuse_surface", shader="diffuse_bsdf")
            material.node("surface_shader").parmTuple("color").set(tuple(float(v) for v in f.color))
            f.obj.parm("shop_materialpath").set(material.path())
            self.rop.parmTuple("background").set((0.2, 0.2, 0.2))
        elif self.material_mode == "diagnostic":
            self.unsupported = hou.node("/mat").createNode("principledshader::2.0", "preview_unsupported_surface")
            f.obj.parm("shop_materialpath").set(self.unsupported.path())
        self.original_factory = registry.session_from_configuration

        def diagnostic_factory(*args, **kwargs):
            return self.original_factory(*args, **dict(kwargs, collect_diagnostics=True))

        registry.session_from_configuration = diagnostic_factory
        self.scene = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.SceneViewer, size=(650, 500))
        self.scene.curViewport().setCamera(f.camera)
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(1100, 800))
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.panel = None
        self.started = time.perf_counter()
        self.last_beat = self.started
        self.phase = "mount"
        self.case = None
        self.index = 0
        self.finished = self.busy = False
        self.error = None
        self.retiring = None
        self.report = dict(
            result="running",
            scope=__doc__,
            device=f.device,
            fixture=dict(
                width=960,
                height=540,
                samples=32,
                initial_prototype_triangles=2 * (self.prototype_rows - 1) ** 2,
                instances=self.instance_count,
                motion_samples=3,
                denoising=False,
                material=self.material_mode,
                light_tree=light_tree == "1",
            ),
            cases=[],
            heartbeat_gaps=[],
            paints=[],
        )
        self.timer = QtCore.QTimer(hou.qt.mainWindow())
        self.timer.setInterval(16)
        self.timer.timeout.connect(self.beat)
        self.timer.start()
        hou.ui.addEventLoopCallback(self.tick)
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.report, indent=2))

    def beat(self):
        now = time.perf_counter()
        self.report["heartbeat_gaps"].append(dict(phase=self.phase, seconds=now - self.last_beat))
        self.last_beat = now

    def painted(self, generation):
        if self.finished or self.case is None or "first_paint_seconds" in self.case:
            return
        try:
            metadata = dict(self.panel.canvas.frame_metadata)
            if (
                generation <= self.case["before_generation"]
                or metadata.get("render_revision", 0) <= self.case["before_render_revision"]
            ):
                return
            image = self.panel.canvas.image
            if image.isNull():
                return
            assert image.depth() == 32, image.format()
            # A newer revision may initially contain only a cleared buffer.
            # Inspect a bounded sparse view of owned display pixels, without
            # copying/assembling a full image in the GUI thread.
            rgba = np.ndarray((image.height(), image.bytesPerLine() // 4, 4), np.uint8, buffer=image.constBits())
            colors = rgba[::16, : image.width() : 16, :3]
            span = colors.max(axis=(0, 1)).astype(int) - colors.min(axis=(0, 1)).astype(int)
            if span.max() < 4:
                return
            session = self.panel.entry.session
            # Read cached controller state here. Pumping host acquisition from
            # a paint callback would distort the measurement and invite reentry.
            status = session._controller.status()
            self.case.update(
                first_paint_seconds=time.perf_counter() - self.action_started,
                first_paint=metadata,
                first_paint_color_span=span.tolist(),
                capture_pending_at_paint=session._active_capture is not None or session._pending_capture is not None,
                pending_at_paint=status["pending_update"],
            )
            with session._controller.condition:
                # Exact no-op captures can acknowledge the same physical
                # generation later. Attribute its first image to the commit
                # that actually created it, rather than that later ACK.
                event = next(
                    (
                        x
                        for x in session._controller.events
                        if x.get("physical_generation") == metadata.get("render_revision")
                    ),
                    None,
                )
                self.case["first_commit"] = dict(event) if event else None
            self.report["paints"].append(dict(case=self.index, metadata=metadata))
        except Exception:
            self.error = traceback.format_exc()

    def begin_action(self, status):
        f = self.f
        self.case = dict(
            kind=self.kind,
            repetition=self.index % 5,
            before_generation=status["generation"],
            before_render_revision=status.get("render_revision", 0),
            capture_pending_before_action=status.get("capture_pending", False),
        )
        self.action_started = time.perf_counter()
        offset = 0.04 if self.index % 2 == 0 else -0.03
        if self.kind.startswith("camera"):
            parm = f.camera.parm("tx")
        else:
            parm = f.obj.parm("tx")
        parm.set(parm.eval() + offset)
        self.case["parameter_action_seconds"] = time.perf_counter() - self.action_started
        self.phase = "await-paint"
        self.save()

    def next_case(self, entry):
        if self.index == 20:
            self.report["diagnostics"] = entry.session.diagnostics()
            for kind in ("camera", "transform", "camera-during-geometry", "transform-during-geometry"):
                values = [x["first_paint_seconds"] for x in self.report["cases"] if x["kind"] == kind]
                self.report.setdefault("timings", {})[kind] = dict(
                    count=len(values),
                    median=float(np.median(values)),
                    p95=float(np.percentile(values, 95)),
                    maximum=max(values),
                )
            self.retiring = entry.session
            self.panel.dispose()
            registry.close_all()
            self.phase = "retiring"
            return
        self.kind = ("camera", "transform", "camera-during-geometry", "transform-during-geometry")[self.index // 5]
        if "during" in self.kind:
            parm = self.f.motion.parm("test_rows")
            parm.set(self.prototype_rows + 2 if parm.eval() == self.prototype_rows else self.prototype_rows)
            self.geometry_started = time.perf_counter()
            self.phase = "await-capture"
        else:
            self.begin_action(entry.session.status())

    def tick(self):
        if self.finished or self.busy:
            return
        self.busy = True
        try:
            assert time.perf_counter() - self.started < 330, (self.phase, self.report.get("current"))
            if self.error:
                raise RuntimeError(self.error)
            if self.phase == "mount":
                self.panel = self.tab.activeInterfaceRootWidget()
                if self.panel is None:
                    return
                self.panel.set_rop(self.rop)
                self.panel.canvas.framePainted.connect(self.painted)
                QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                self.phase = "initial"
                return
            if self.retiring is not None:
                status = self.retiring.status()
                if status["state"] != "closed":
                    return
                assert status["owned_geometry_bytes"] == status["device_allocation_bytes"] == 0, status
                self.report["closed"] = status
                self.finish()
                return
            entry = self.panel.entry
            if entry is None:
                return
            status = entry.session.status()
            assert status["state"] != "failed" and not entry.error and not status["error"], status
            self.report["current"] = dict(phase=self.phase, status=status)
            if self.phase == "await-capture":
                if status.get("capture_pending"):
                    self.begin_action(status)
                else:
                    assert time.perf_counter() - self.geometry_started < 15, "Geometry capture was not observed"
                return
            frame = entry.session.frame_info()
            if (
                status.get("capture_pending")
                or status.get("pending_update")
                or not frame
                or not frame["complete"]
                or not frame.get("current_scene")
                or frame["generation"] != status["generation"]
                or self.panel.canvas.frame_metadata.get("render_revision") != frame.get("render_revision")
            ):
                return
            assert status["render_device_ids"] == [self.f.device], status
            assert status["geometry_resources"] == 1 and status["object_resources"] == self.instance_count, status
            issues = status["source_diagnostics"]
            if self.material_mode == "diagnostic":
                assert len(issues) == 1 and issues[0]["code"] == "material.unsupported", issues
                assert issues[0]["source"] == self.unsupported.path(), issues
            else:
                assert not issues, issues
            if self.phase == "initial":
                self.pid = status["renderer_pid"]
                self.next_case(entry)
                return
            assert status["renderer_pid"] == self.pid, status
            if "first_paint_seconds" not in self.case:
                return
            diagnostics = entry.session.diagnostics()
            self.case["engine"] = {
                key: diagnostics[key]
                for key in (
                    "generation",
                    "core_update_seconds",
                    "scene_boundary_wait_seconds",
                    "shader_graph_preparation_seconds",
                    "engine_update_seconds",
                    "buffer_copy_requests",
                    "engine_statistics_pending",
                )
            }
            self.case.update(settled_seconds=time.perf_counter() - self.action_started, final_status=status)
            self.report["cases"].append(self.case)
            self.case = None
            self.index += 1
            self.save()
            self.next_case(entry)
        except Exception:
            self.finish(traceback.format_exc())
        finally:
            self.busy = False

    def finish(self, error=None):
        if self.finished:
            return
        self.finished = True
        self.timer.stop()
        if self.tick in hou.ui.eventLoopCallbacks():
            hou.ui.removeEventLoopCallback(self.tick)
        if self.panel:
            self.panel.dispose()
        registry.session_from_configuration = self.original_factory
        registry.close_all()
        self.report.update(
            result="failed" if error else "passed", error=error, wall_seconds=time.perf_counter() - self.started
        )
        self.save()
        hou.exit(1 if error else 0, suppress_save_prompt=True)


if __name__ == "__main__":
    benchmark = Benchmark()
