# SPDX-License-Identifier: Apache-2.0
"""Real mesh conversion reads a retained SOP while the host edits/deletes it."""

import json
import gc
import os
from pathlib import Path
import statistics
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
    cases=[],
    scope="Geometry ownership experiment using the production mesh converter, not asynchronous artist IPR. Native SOPs, approximately one million triangles, two repetitions of no lease/preserve/copy plus native material rename/delete during retained conversion.",
)


class Workflow:
    def __init__(self):
        self.index = 0
        self.case = None
        self.lease = None
        self.thread = None
        self.stop = threading.Event()
        self.rows = []
        self.worker_error = None
        self.began = time.perf_counter()
        self.main = threading.get_ident()
        self.timer = QtCore.QTimer(hou.qt.mainWindow())
        self.timer.setInterval(16)
        self.timer.timeout.connect(self.tick)
        self.timer.start()

    def setup(self):
        mode = (
            ("baseline", "preserve", "copy")[self.index % 3]
            if self.index < 6
            else ("native-preserve", "native-copy")[self.index - 6]
        )
        self.native = mode.startswith("native-")
        self.obj = hou.node("/obj").createNode("geo", "lease_fixture", run_init_scripts=False)
        self.grid = self.obj.createNode("grid")
        self.grid.setParms(dict(rows=721, cols=721))
        self.deform = self.obj.createNode("attribwrangle")
        self.deform.setInput(0, self.grid)
        self.deform.parm("snippet").set(
            "@P.y=.1*sin(@P.x*3)*cos(@P.z*2); v@Cd=set(.2+@P.x*.01,.3,.6); v@uv=set(@P.x*.1+.5,@P.z*.1+.5,0);"
        )
        self.source = self.obj.createNode("xform")
        self.source.setInput(0, self.deform)
        self.source.setRenderFlag(True)
        self.transform = self.source
        self.materials = []
        if self.native:
            self.materials = [
                ch.create_material(name="lease_material_" + str(self.index) + "_" + str(i), shader="emission")
                for i in range(2)
            ]
            self.material_paths = [node.path() for node in self.materials]
            self.material_keys = [
                "native-material:" + node.userData("cycles_graph_id") + ":" + str(node.sessionId())
                for node in self.materials
            ]
            self.source = self.obj.createNode("material")
            self.source.setInput(0, self.transform)
            self.source.setParms(
                dict(num_materials=1, group1="0-259199", shop_materialpath1="op:" + self.material_paths[0])
            )
            self.source.setRenderFlag(True)
        geometry = self.source.geometry()
        expected = np.frombuffer(geometry.pointFloatAttribValuesAsString("P"), np.float32).reshape(-1, 3)
        first = expected[0].astype(float).tolist()
        sums = expected.sum(axis=0, dtype=np.float64).tolist()
        self.case = dict(
            mode=mode,
            repetition=self.index // 3,
            host_edits=[],
            gaps=[],
            worker_conversions=[],
            points=len(expected),
            expected_triangles=2 * 720 * 720,
            independent_first=first,
            independent_sums=sums,
        )
        self.lease = (
            ch._native.preserve_geometry(
                self.source.path(), hou.time(), mode.endswith("copy"), self.material_paths[1] if self.native else None
            )
            if mode != "baseline"
            else None
        )
        self.rows = []
        self.worker_error = None
        self.stop = threading.Event()
        if self.lease:
            reference = ch._native.convert_preserved_mesh(self.lease)
            assert reference["points"] == len(expected) and reference["triangles"] == self.case["expected_triangles"]
            assert reference["data_ids"] == reference["source_data_ids"], "Snapshot lost source data identifiers"
            assert (reference["detail_id"] == reference["source_id"]) == mode.endswith("preserve")
            if self.native:
                assert {row["key"]: row["triangles"] for row in reference["materials"]} == dict.fromkeys(
                    self.material_keys, 518400
                )
            assert np.allclose(reference["first_position"], first, rtol=0, atol=1e-7)
            assert np.allclose(reference["position_sums"], sums, rtol=0, atol=1e-6)
            self.case["reference"] = reference
            self.thread = threading.Thread(target=self.convert, name="Frozen geometry reader")
            self.thread.start()
        else:
            self.thread = None
        self.edit = 0
        self.deleted_at = None
        self.last = time.perf_counter()
        self.phase_started = self.last

    def convert(self):
        try:
            try:
                ch._native.preserve_geometry("/obj/forbidden_background_lookup", 0.0)
            except RuntimeError as error:
                assert "host thread" in str(error)
            else:
                raise AssertionError("Background OP lookup was not rejected")
            while not self.stop.is_set():
                value = ch._native.convert_preserved_mesh(self.lease)
                value["thread"] = threading.get_ident()
                self.rows.append(value)
                if value["fingerprint"] != self.case["reference"]["fingerprint"]:
                    raise AssertionError("Retained mesh changed during a concurrent SOP edit")
        except BaseException:
            self.worker_error = traceback.format_exc()

    def finish(self, error=None):
        self.timer.stop()
        self.stop.set()
        if self.thread:
            self.thread.join()
        self.lease = None
        gc.collect()
        report["live_leases_after_close"] = ch._native.geometry_lease_count()
        if report["live_leases_after_close"]:
            error = (error or "") + " Retained geometry leases after closure"
        report.update(
            result="failed" if error else "passed", error=error, wall_seconds=time.perf_counter() - self.began
        )
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        hou.ui.postEventCallback(lambda: hou.exit(1 if error else 0, suppress_save_prompt=True))

    def tick(self):
        try:
            if time.perf_counter() - self.began > 150:
                raise TimeoutError("Geometry ownership probe stalled")
            if self.worker_error:
                raise RuntimeError(self.worker_error)
            if self.case is None:
                if self.index == 8:
                    self.finish()
                    return
                self.setup()
                return
            now = time.perf_counter()
            self.case["gaps"].append(now - self.last)
            self.last = now
            if self.edit < 20:
                started = time.perf_counter()
                self.transform.parm("tx").set(0.025 * (self.edit + 1))
                if self.edit in (5, 10, 15):
                    self.grid.parm("rows").set(721 + self.edit)
                if self.native and self.edit == 5:
                    for node in self.materials:
                        node.setName(node.name() + "_renamed")
                    self.case["materials_renamed"] = True
                if self.native and self.edit == 10:
                    for node in self.materials:
                        node.destroy()
                    self.materials = [
                        ch.create_material(name=path.rsplit("/", 1)[1], shader="emission")
                        for path in self.material_paths
                    ]
                    self.source.parm("shop_materialpath1").set("op:" + self.material_paths[0])
                    fresh = ch._native.preserve_geometry(self.source.path(), hou.time(), True, self.material_paths[1])
                    fresh_result = ch._native.convert_preserved_mesh(fresh)
                    del fresh
                    fresh_keys = {row["key"] for row in fresh_result["materials"]}
                    assert not fresh_keys.intersection(self.material_keys), (
                        "Recreated nodes reused a frozen material identity"
                    )
                    self.case["fresh_material_keys"] = sorted(fresh_keys)
                geometry = self.source.geometry()
                self.case["host_edits"].append(time.perf_counter() - started)
                assert abs(geometry.iterPoints()[0].position()[0] - self.case["independent_first"][0]) > 0.001
                self.edit += 1
                return
            if self.deleted_at is None:
                self.obj.destroy()
                self.deleted_at = now
                self.case["conversions_at_delete"] = len(self.rows)
                return
            if now - self.deleted_at < 0.3:
                return
            self.stop.set()
            if self.thread and self.thread.is_alive():
                return
            if self.thread:
                self.thread.join()
                assert len(self.rows) > self.case["conversions_at_delete"], (
                    "No conversion completed after source deletion"
                )
                assert all(row["thread"] != self.main for row in self.rows)
                assert all(row["fingerprint"] == self.case["reference"]["fingerprint"] for row in self.rows)
            self.case["worker_conversions"] = self.rows
            self.case["host_edit_median_seconds"] = statistics.median(self.case["host_edits"])
            self.case["host_edit_maximum_seconds"] = max(self.case["host_edits"])
            self.case["heartbeat_maximum_seconds"] = max(self.case["gaps"])
            self.case["result"] = "passed"
            report["cases"].append(self.case)
            for node in self.materials:
                node.destroy()
            self.lease = None
            self.case = None
            self.index += 1
            assert ch._native.geometry_lease_count() == 0, "A completed case retained its source detail"
        except BaseException:
            self.finish(traceback.format_exc())


workflow = Workflow()
