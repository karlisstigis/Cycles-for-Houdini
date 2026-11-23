# SPDX-License-Identifier: Apache-2.0
"""Frozen mixed shutter samples survive concurrent host edits and node deletion."""

import gc
import json
import os
from pathlib import Path
import threading
import time
import traceback

import hou
import numpy as np
from PySide6 import QtCore
import cycles_houdini as ch

path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
report = dict(
    result="running",
    scope="Production deformation conversion on frozen samples in a background thread. This is an ownership test, not asynchronous artist IPR or an interactive performance benchmark.",
    conversions=[],
    host_edits=[],
    heartbeat_gaps=[],
)
hou.setFps(24)
hou.setFrame(1)


class Workflow:
    def __init__(self):
        self.started = time.perf_counter()
        self.last = self.started
        self.step = 0
        self.lease = None
        self.thread = None
        self.error = None
        self.rows = []
        self.stop = threading.Event()
        self.timer = QtCore.QTimer(hou.qt.mainWindow())
        self.timer.setInterval(16)
        self.timer.timeout.connect(self.tick)
        self.timer.start()

    def setup(self):
        self.obj = hou.node("/obj").createNode("geo", "frozen_shutter", run_init_scripts=False)
        self.mesh = self.obj.createNode("grid")
        self.mesh.setParms(dict(rows=721, cols=721))
        self.extra = self.obj.createNode("python", "one_curve_and_loose_points")
        curve = [(2 + i * 0.1, 0, 2 + i * 0.2) for i in range(8)]
        points = [(i % 4 / 3, 3 + i // 4 / 3, 1) for i in range(16)]
        self.extra.parm("python").set(
            "import hou\ng=hou.pwd().geometry()\ncurve=g.createPolygon(is_closed=False)\n"
            + "for p in "
            + repr(curve)
            + ":\n point=g.createPoint();point.setPosition(p);curve.addVertex(point)\n"
            + "for p in "
            + repr(points)
            + ":\n point=g.createPoint();point.setPosition(p)\n"
        )
        merge = self.obj.createNode("merge")
        merge.setInput(0, self.mesh)
        merge.setInput(1, self.extra)
        ids = self.obj.createNode("attribwrangle")
        ids.setInput(0, merge)
        ids.parm("snippet").set("i@id=@ptnum; f@width=.05; f@pscale=.025;")
        self.source = self.obj.createNode("xform")
        self.source.setInput(0, ids)
        self.source.parm("tx").setExpression("$T * 24", hou.exprLanguage.Hscript)
        self.source.setRenderFlag(True)
        self.material = ch.create_material(name="frozen_shutter_material", shader="emission")
        # The default this test was written for (new nodes now take Blender's).
        self.material.node("surface_shader").setParms(dict(color=(0.8, 0.8, 0.8), strength=10.0))
        material_key = (
            "native-material:" + self.material.userData("cycles_graph_id") + ":" + str(self.material.sessionId())
        )
        mesh = np.frombuffer(self.mesh.geometry().pointFloatAttribValuesAsString("P"), np.float32).reshape(-1, 3)
        expected = dict(mesh=mesh, curves=np.asarray(curve, np.float32), points=np.asarray(points, np.float32))
        self.lease = ch._native.preserve_deformation(
            self.source.path(), hou.time(), hou.fps(), 1.0, 3, self.material.path()
        )
        reference = ch._native.convert_preserved_deformation(self.lease)
        assert {row["kind"] for row in reference["components"]} == set(expected)
        for row in reference["components"]:
            values = expected[row["kind"]]
            assert row["points"] == len(values)
            assert row["materials"] == [material_key]
            assert len(row["samples"]) == 3
            for sample, delta in zip(row["samples"], (0, -0.5, 0.5)):
                # The SOP translates at 24 units/second; a one-frame shutter at
                # 24 fps spans exactly +/- 0.5 units, independent of conversion.
                transformed = values.astype(np.float64) + np.asarray((delta, 0, 0))
                assert np.allclose(sample["first"], transformed[0], atol=2e-6, rtol=0), (row, sample)
                assert np.allclose(sample["sums"], transformed.sum(axis=0), atol=0.015, rtol=0), (row["kind"], sample)
        assert np.allclose(tuple(self.source.geometry().iterPoints()[0].position()), mesh[0], atol=1e-7, rtol=0), (
            "Nominal cook was not restored"
        )
        assert hou.frame() == 1
        report["reference"] = reference
        report["source_triangles"] = 2 * 720 * 720
        self.thread = threading.Thread(target=self.convert, name="Frozen deformation converter")
        self.thread.start()

    def convert(self):
        try:
            try:
                ch._native.preserve_deformation("/obj/forbidden", 0.0, 24.0, 1.0)
            except RuntimeError as error:
                assert "host thread" in str(error)
            else:
                raise AssertionError("Background shutter cooking was allowed")
            while not self.stop.is_set():
                result = ch._native.convert_preserved_deformation(self.lease)
                assert result["fingerprint"] == report["reference"]["fingerprint"], "Frozen shutter samples changed"
                result["thread"] = threading.get_ident()
                self.rows.append(result)
        except BaseException:
            self.error = traceback.format_exc()

    def finish(self, error=None):
        self.timer.stop()
        self.stop.set()
        if self.thread:
            self.thread.join()
        self.lease = None
        gc.collect()
        remaining = ch._native.geometry_lease_count()
        if remaining:
            error = (error or "") + " Retained deformation lease after closure"
        report.update(
            result="failed" if error else "passed",
            error=error,
            conversions=self.rows,
            live_leases=remaining,
            seconds=time.perf_counter() - self.started,
        )
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        hou.ui.postEventCallback(lambda: hou.exit(1 if error else 0, suppress_save_prompt=True))

    def tick(self):
        try:
            now = time.perf_counter()
            report["heartbeat_gaps"].append(now - self.last)
            self.last = now
            if now - self.started > 150:
                raise TimeoutError("Frozen shutter probe stalled")
            if self.error:
                raise RuntimeError(self.error)
            if self.lease is None:
                self.setup()
                return
            if self.step < 20:
                began = time.perf_counter()
                hou.setFrame(2 + self.step)
                if self.step == 8:
                    self.mesh.parm("rows").set(740)
                if self.step == 12:
                    self.material.setName("renamed_during_deformation")
                geometry = self.source.geometry()
                assert (
                    abs(
                        geometry.iterPoints()[0].position().x()
                        - report["reference"]["components"][0]["samples"][0]["first"][0]
                    )
                    > 0.5
                )
                report["host_edits"].append(time.perf_counter() - began)
                self.step += 1
                return
            if self.step == 20:
                self.obj.destroy()
                self.material.destroy()
                self.deleted = now
                report["conversions_at_delete"] = len(self.rows)
                self.step += 1
                return
            if now - self.deleted < 1:
                return
            self.stop.set()
            if self.thread.is_alive():
                return
            self.thread.join()
            assert len(self.rows) > report["conversions_at_delete"], "No conversion completed after source deletion"
            assert all(row["thread"] != threading.get_ident() for row in self.rows)
            if self.error:
                raise RuntimeError(self.error)
            self.finish()
        except BaseException:
            self.finish(traceback.format_exc())


workflow = Workflow()
