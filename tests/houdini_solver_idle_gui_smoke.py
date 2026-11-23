# SPDX-License-Identifier: Apache-2.0
"""Fast Pyro/POP idle, public-parameter, frame-change and Refresh IPR checks."""

import json
import os
from pathlib import Path
import time
import traceback

import hou
import numpy as np
from PySide6 import QtCore, QtTest
from cycles_houdini import device_selection, menu_actions, registry
from solver_idle_fixture import create_solvers


class Workflow:
    def __init__(self):
        self.started = time.monotonic()
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.root, self.cases = create_solvers()
        self.camera = self.root.createNode("cam", "camera")
        self.camera.setParms(dict(tz=5, projection=1, orthowidth=2))
        self.rop = menu_actions.create_rop()
        device_selection.set_devices(self.rop, [os.environ["CYCLES_HOUDINI_GUI_DEVICE"]])
        self.rop.setParms(
            dict(
                source_mode="sop",
                camera=self.camera.path(),
                resx=64,
                resy=64,
                samples=2,
                ipr_samples=2,
                use_denoise=0,
                ipr_use_denoise=0,
                adaptive_sampling=0,
                ipr_adaptive_sampling=0,
                allowmotionblur=0,
                use_material_assignments=0,
                film_exposure=1,
                render_threads=2,
                use_texture_cache=0,
                point_radius=0.06,
                emission_strength=1,
                density=0,
                volume_emission_strength=1,
                emission_field="density",
            )
        )
        self.rop.parmTuple("basecolor").set((1, 1, 1))
        self.rop.parmTuple("volume_emission_color").set((1, 0.5, 0.25))
        self.rop.parmTuple("background").set((0, 0, 0))
        self.index = self.stage = 0
        self.generation = 0
        self.worker_pid = None
        self.report = dict(
            result="running",
            states=[],
            limits=dict(
                resolution=[64, 64],
                samples=2,
                pyro_source_voxels=64,
                pyro_voxel_size=0.25,
                pop_births_per_second=48,
                idle_seconds=1.25,
                timeout_seconds=45,
            ),
            scope="Real native Pyro SOP and POP DOP simulations, one live Cycles Viewer and retained worker; CPU host-observer regression",
        )
        self.configure()
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(650, 450))
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.panel = None
        self.phase = "mount"
        self.busy = False
        self.anchor = None
        self.edit_at = time.monotonic()
        hou.ui.addEventLoopCallback(self.tick)

    def configure(self):
        name, obj, source, parm = self.cases[self.index]
        self.rop.setParms(
            dict(soppath=source.path(), material_kind="principled_volume" if name == "Pyro" else "emission")
        )
        hou.setFrame(3)

    def counters(self, status):
        entry = self.panel.entry
        return dict(
            generation=status["generation"],
            revision=status["render_revision"],
            serial=entry.change_serial,
            renderer_pid=status["renderer_pid"],
        )

    def advance(self):
        _, _, _, parm = self.cases[self.index]
        self.stage += 1
        self.phase = "wait"
        self.anchor = None
        self.edit_at = time.monotonic()
        if self.stage == 1:
            parm.set(0.5)
        elif self.stage == 2:
            parm.set(1)
        elif self.stage == 3:
            hou.setFrame(4)
        elif self.stage == 4:
            self.panel.queue_render()
        else:
            self.index += 1
            if self.index == len(self.cases):
                self.finish()
                return
            self.stage = 0
            self.configure()

    def tick(self):
        if self.busy:
            return
        self.busy = True
        try:
            now = time.monotonic()
            assert now - self.started < 45, (self.index, self.stage, self.phase)
            if self.phase == "mount":
                self.panel = self.tab.activeInterfaceRootWidget()
                if self.panel is None:
                    return
                self.panel.set_rop(self.rop)
                self.panel.auto.setChecked(True)
                QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                self.phase = "wait"
                self.edit_at = now
                return
            entry = self.panel.entry
            if entry is None:
                return
            status = entry.session.status()
            assert not entry.error and status["state"] != "failed", (entry.error, status)
            assert entry.observer is not None and not entry.observer._closed
            assert self.panel._observer is not None and not self.panel._observer._closed
            name, obj, source, parm = self.cases[self.index]
            if self.phase == "idle":
                current = self.counters(status)
                assert current == self.anchor, dict(
                    reason="Unedited solver queued an extra capture",
                    solver=name,
                    stage=self.stage,
                    before=self.anchor,
                    after=current,
                )
                if now - self.idle_at < 1.25:
                    return
                self.report["states"].append(
                    dict(
                        solver=name,
                        stage=self.stage,
                        action=("initial", "parameter edit", "parameter restore", "frame change", "Refresh")[
                            self.stage
                        ],
                        frame=hou.frame(),
                        idle_seconds=now - self.idle_at,
                        source_points=self.points,
                        source_primitives=self.prims,
                        **current,
                    )
                )
                self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")
                self.generation = status["generation"]
                self.advance()
                return
            assert now - self.edit_at < 12, dict(
                reason="Solver edit did not complete", solver=name, stage=self.stage, status=status
            )
            frame = entry.session.frame_info()
            if (
                status["state"] != "complete"
                or status.get("pending_update")
                or status.get("capture_pending")
                or not frame
                or not frame["complete"]
                or status["generation"] <= self.generation
                or frame["generation"] != status["generation"]
                or self.panel._live_generation != frame["generation"]
            ):
                return
            assert not status["source_diagnostics"], status["source_diagnostics"]
            if self.worker_pid is None:
                self.worker_pid = status["renderer_pid"]
            assert status["renderer_pid"] == self.worker_pid
            assert len(registry.entries()) == 1
            pixels = np.frombuffer(entry.session.frame()["planes"]["combined"]["pixels"], np.float32).reshape(-1, 4)
            assert np.isfinite(pixels).all() and pixels[:, :3].max() > 0.001, (name, status)
            geometry = source.geometry()
            self.points, self.prims = len(geometry.points()), len(geometry.prims())
            assert not source.errors()
            assert self.prims > 0 if name == "Pyro" else self.points > 0
            assert parm.node() in {node for node, _ in entry.observer.nodes}, parm.path()
            self.anchor = self.counters(status)
            self.idle_at = now
            self.phase = "idle"
        except Exception:
            self.finish(traceback.format_exc())
        finally:
            self.busy = False

    def finish(self, error=None):
        if self.tick in hou.ui.eventLoopCallbacks():
            hou.ui.removeEventLoopCallback(self.tick)
        if self.panel:
            self.panel.dispose()
        registry.close_all()
        self.report.update(
            result="failed" if error else "passed", error=error, elapsed_seconds=time.monotonic() - self.started
        )
        self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")
        hou.exit(1 if error else 0, suppress_save_prompt=True)


workflow = Workflow()
