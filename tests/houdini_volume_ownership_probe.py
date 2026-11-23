# SPDX-License-Identifier: Apache-2.0
"""Native VDB edits cannot mutate retained trees, transforms or metadata."""

import gc
import json
import os
from pathlib import Path
import threading
import time
import traceback

import hou
from PySide6 import QtCore
import cycles_houdini as ch

path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
report = dict(
    result="running",
    cases=[],
    scope="Frozen native VDB acquisition and production conversion under concurrent host edits. No render session or asynchronous artist capture; timings include native SOP cooking.",
)


class Workflow:
    def __init__(self):
        self.started = self.last = time.perf_counter()
        self.index = -1
        self.thread = None
        self.lease = None
        self.error = None
        self.stop = threading.Event()
        self.timer = QtCore.QTimer(hou.qt.mainWindow())
        self.timer.setInterval(16)
        self.timer.timeout.connect(self.tick)
        self.timer.start()

    def setup(self):
        self.index += 1
        self.stop.clear()
        self.rows = []
        self.step = 0
        self.obj = hou.node("/obj").createNode("geo", "frozen_volume", run_init_scripts=False)
        box = self.obj.createNode("box")
        box.parmTuple("size").set((4, 4, 4))
        vdb = self.obj.createNode("vdbfrompolygons")
        vdb.setInput(0, box)
        vdb.setParms(dict(voxelsize=0.04, builddistance=0, buildfog=1, fillinterior=1))
        self.values = self.obj.createNode("volumewrangle")
        self.values.setInput(0, vdb)
        self.values.addSpareParmTuple(hou.FloatParmTemplate("density_base", "Density", 1, default_value=(0.75,)))
        self.values.parm("snippet").set('@density=chf("density_base")+.01*@P.x;')
        self.transform = self.obj.createNode("xform")
        self.transform.setInput(0, self.values)
        self.source = self.obj.createNode("attribwrangle", "field_name")
        self.source.setInput(0, self.transform)
        self.source.parm("class").set(1)
        self.source.parm("snippet").set('s@name="density";')
        self.source.setRenderFlag(True)
        primitive = self.source.geometry().prims()[0]
        assert primitive.attribValue("name") == "density"
        assert abs(primitive.voxel((0, 0, 0)) - 0.75) < 1e-6
        self.lease = ch._native.preserve_volume(self.source.path(), hou.time())
        reference = ch._native.convert_preserved_volume(self.lease)
        assert len(reference["fields"]) == 1
        field = reference["fields"][0]
        assert field["name"] == "density" and field["active_voxels"] > 100_000
        assert abs(field["voxel_zero"][0] - 0.75) < 1e-6
        assert max(abs(v - 0.04) for v in field["voxel_size"]) < 1e-7
        assert max(abs(v) for v in field["origin"]) < 1e-7
        self.case = dict(
            mode="baseline" if self.index == 0 else "frozen",
            reference=reference,
            host_edits=[],
            heartbeat_gaps=[],
            conversions=[],
        )
        report["cases"].append(self.case)
        if self.index == 0:
            self.lease = None
            gc.collect()
            assert ch._native.geometry_lease_count() == 0
        else:
            self.thread = threading.Thread(target=self.convert, name="Frozen VDB converter")
            self.thread.start()

    def convert(self):
        try:
            try:
                ch._native.preserve_volume("/obj/forbidden", 0.0)
            except RuntimeError as error:
                assert "host thread" in str(error)
            else:
                raise AssertionError("Background VDB node acquisition was allowed")
            while not self.stop.is_set():
                value = ch._native.convert_preserved_volume(self.lease)
                assert value["fingerprint"] == self.case["reference"]["fingerprint"], "Retained VDB changed"
                value["thread"] = threading.get_ident()
                self.rows.append(value)
        except BaseException:
            self.error = traceback.format_exc()

    def finish(self, error=None):
        self.timer.stop()
        self.stop.set()
        if self.thread:
            self.thread.join()
        self.lease = None
        gc.collect()
        if ch._native.geometry_lease_count():
            error = (error or "") + " Leaked volume lease"
        report.update(
            result="failed" if error else "passed",
            error=error,
            seconds=time.perf_counter() - self.started,
            live_leases=ch._native.geometry_lease_count(),
        )
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        hou.ui.postEventCallback(lambda: hou.exit(1 if error else 0, suppress_save_prompt=True))

    def tick(self):
        try:
            now = time.perf_counter()
            if now - self.started > 180:
                raise TimeoutError("VDB ownership probe stalled")
            if self.error:
                raise RuntimeError(self.error)
            if self.index < 0:
                self.setup()
                self.last = time.perf_counter()
                return
            self.case["heartbeat_gaps"].append(now - self.last)
            self.last = now
            if self.step < 8:
                started = time.perf_counter()
                self.values.parm("density_base").set(1.0 + 0.1 * self.step)
                self.transform.parm("tx").set(0.1 * (self.step + 1))
                if self.step == 4:
                    self.source.parm("snippet").set('s@name="renamed_density";')
                geometry = self.source.geometry()
                assert geometry.prims()[0].voxel((0, 0, 0)) > 0.9
                assert abs(geometry.prims()[0].indexToPos((0, 0, 0))[0] - 0.1 * (self.step + 1)) < 1e-6
                if self.step >= 4:
                    assert geometry.prims()[0].attribValue("name") == "renamed_density"
                self.case["host_edits"].append(time.perf_counter() - started)
                self.step += 1
                return
            if self.step == 8:
                fresh = ch._native.preserve_volume(self.source.path(), hou.time())
                current = ch._native.convert_preserved_volume(fresh)
                assert current["fingerprint"] != self.case["reference"]["fingerprint"]
                assert current["fields"][0]["name"] == "renamed_density"
                assert abs(current["fields"][0]["voxel_zero"][0] - 1.7) < 1e-6
                assert abs(current["fields"][0]["origin"][0] - 0.8) < 1e-6
                del fresh
                gc.collect()
                assert ch._native.geometry_lease_count() == self.index
                self.case["fresh_snapshot"] = current
                self.obj.destroy()
                self.deleted = now
                self.case["conversions_at_delete"] = len(self.rows)
                self.step += 1
                if self.index == 0:
                    self.setup()
                    self.last = time.perf_counter()
                return
            if now - self.deleted < 1 or len(self.rows) <= self.case["conversions_at_delete"]:
                return
            self.stop.set()
            if self.thread.is_alive():
                return
            self.thread.join()
            if self.error:
                raise RuntimeError(self.error)
            assert all(row["thread"] != threading.get_ident() for row in self.rows)
            self.case["conversions"] = self.rows
            self.finish()
        except BaseException:
            self.finish(traceback.format_exc())


workflow = Workflow()
