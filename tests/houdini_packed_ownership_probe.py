# SPDX-License-Identifier: Apache-2.0
"""Retained nested packed prototypes survive edits, rebinding and deletion."""

import gc
import json
import math
import os
from pathlib import Path
import re
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
    host_edits=[],
    conversions=[],
    heartbeat_gaps=[],
    scope="Production packed acquisition and mesh conversion on frozen nested prototypes. Material identities only; no asynchronous artist scheduling or speed claim.",
)


def material_key(node):
    return "native-material:" + node.userData("cycles_graph_id") + ":" + str(node.sessionId())


class Workflow:
    def __init__(self):
        self.started = self.last = time.perf_counter()
        self.lease = self.thread = self.error = None
        self.step = 0
        self.rows = []
        self.stop = threading.Event()
        self.timer = QtCore.QTimer(hou.qt.mainWindow())
        self.timer.setInterval(16)
        self.timer.timeout.connect(self.tick)
        self.timer.start()

    def setup(self):
        self.obj = hou.node("/obj").createNode("geo", "frozen_packed", run_init_scripts=False)
        self.mesh = self.obj.createNode("grid", "million_triangle_prototype")
        self.mesh.setParms(dict(rows=721, cols=721))
        self.mesh.parmTuple("size").set((1, 1))
        self.deform = self.obj.createNode("xform", "prototype_edit")
        self.deform.setInput(0, self.mesh)
        inner = self.obj.createNode("grid", "inner_targets")
        inner.setParms(dict(rows=2, cols=2))
        inner.parmTuple("size").set((3, 3))
        orient = self.obj.createNode("attribwrangle", "inner_orientation")
        orient.setInput(0, inner)
        orient.parm("snippet").set("v@N={0,0,1}; v@up={0,1,0};")
        copies = self.obj.createNode("copytopoints", "shared_copies")
        copies.setInput(0, self.deform)
        copies.setInput(1, orient)
        copies.parm("pack").set(1)
        ids = self.obj.createNode("attribwrangle", "inner_identity")
        ids.setInput(0, copies)
        ids.setParms({"class": 1, "snippet": "i@id=10+@primnum;"})
        self.materials = [
            ch.create_material(name="packed_lease_" + name, shader="emission") for name in ("red", "blue")
        ]
        self.initial_keys = [material_key(node) for node in self.materials]
        self.assign = self.obj.createNode("material", "instance_bindings")
        self.assign.setInput(0, ids)
        self.assign.setParms(
            dict(
                num_materials=2,
                group1="0 2",
                group2="1 3",
                shop_materialpath1=self.materials[0].path(),
                shop_materialpath2="op:" + self.materials[1].path(),
            )
        )
        container = self.obj.createNode("pack", "nested_container")
        container.setInput(0, self.assign)
        outer = self.obj.createNode("grid", "outer_targets")
        outer.setParms(dict(rows=2, cols=2))
        outer.parmTuple("size").set((8, 8))
        orient_outer = self.obj.createNode("attribwrangle", "outer_orientation")
        orient_outer.setInput(0, outer)
        orient_outer.parm("snippet").set("v@N={0,0,1}; v@up={0,1,0};")
        outer_copies = self.obj.createNode("copytopoints", "nested_copies")
        outer_copies.setInput(0, container)
        outer_copies.setInput(1, orient_outer)
        outer_copies.parm("pack").set(1)
        outer_ids = self.obj.createNode("attribwrangle", "outer_identity")
        outer_ids.setInput(0, outer_copies)
        outer_ids.setParms({"class": 1, "snippet": "i@id=100+@primnum;"})
        self.source = self.obj.createNode("xform", "outer_transform")
        self.source.setInput(0, outer_ids)
        self.source.parmTuple("s").set((1.5, 1, 0.75))
        self.source.parmTuple("r").set((0, 30, 0))
        self.source.parmTuple("t").set((0.2, 0.4, 0.6))
        self.source.setRenderFlag(True)

        self.lease = ch._native.preserve_packed_geometry(self.source.path(), hou.time(), "/mat/unused_missing_fallback")
        reference = ch._native.convert_preserved_packed_geometry(self.lease)
        assert reference["prototype_meshes"] == 1 and reference["material_variants"] == 2, reference
        assert len(reference["instances"]) == 16
        assert len({row["identity"] for row in reference["instances"]}) == 16
        inner_positions = [tuple(p.position()) for p in inner.geometry().points()]
        outer_positions = [tuple(p.position()) for p in outer.geometry().points()]
        first = tuple(self.mesh.geometry().iterPoints()[0].position())
        # Construct expected transforms from authored S/R/T and small target
        # point sets, independently of packed primitive intrinsic transforms.
        matrix = (
            hou.hmath.buildScale((1.5, 1, 0.75))
            * hou.hmath.buildRotate((0, 30, 0))
            * hou.hmath.buildTranslate((0.2, 0.4, 0.6))
        )
        for row in reference["instances"]:
            identities = [int(value) for value in re.findall(r"packed:id:(\d+)", row["identity"])]
            outer_id, inner_id = identities[0] - 100, identities[-1] - 10
            offset = np.asarray(inner_positions[inner_id]) + np.asarray(outer_positions[outer_id])
            expected = tuple(hou.Vector3(tuple(np.asarray(first) + offset)) * matrix)
            assert np.allclose(row["first"], expected, rtol=0, atol=2e-6), (row, expected)
            corners = [tuple(hou.Vector3(tuple(offset + (x, 0, z))) * matrix) for x in (-0.5, 0.5) for z in (-0.5, 0.5)]
            assert np.allclose(row["minimum"], np.min(corners, axis=0), rtol=0, atol=2e-6)
            assert np.allclose(row["maximum"], np.max(corners, axis=0), rtol=0, atol=2e-6)
            assert row["triangles"] == 2 * 720 * 720
            assert row["materials"] == [self.initial_keys[inner_id % 2]]
        report["reference"] = reference
        self.thread = threading.Thread(target=self.convert, name="Frozen packed converter")
        self.thread.start()

    def convert(self):
        try:
            try:
                ch._native.preserve_packed_geometry("/obj/forbidden", 0.0)
            except RuntimeError as error:
                assert "host thread" in str(error)
            else:
                raise AssertionError("Background packed procedural resolution was allowed")
            while not self.stop.is_set():
                value = ch._native.convert_preserved_packed_geometry(self.lease)
                assert value["fingerprint"] == report["reference"]["fingerprint"], "Frozen packed data changed"
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
            error = (error or "") + " Leaked packed lease"
        report.update(
            result="failed" if error else "passed",
            error=error,
            conversions=self.rows,
            seconds=time.perf_counter() - self.started,
            live_leases=ch._native.geometry_lease_count(),
        )
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        hou.ui.postEventCallback(lambda: hou.exit(1 if error else 0, suppress_save_prompt=True))

    def tick(self):
        try:
            now = time.perf_counter()
            report["heartbeat_gaps"].append(now - self.last)
            self.last = now
            if now - self.started > 180:
                raise TimeoutError("Packed ownership probe stalled")
            if self.error:
                raise RuntimeError(self.error)
            if self.lease is None:
                self.setup()
                return
            if self.step < 8:
                started = time.perf_counter()
                self.deform.parm("ty").set(0.1 * (self.step + 1))
                self.source.parm("tx").set(0.2 + 0.1 * self.step)
                if self.step == 2:
                    self.materials[0].setName("renamed_packed_lease_red")
                    self.assign.parm("shop_materialpath1").set(self.materials[0].path())
                if self.step == 4:
                    self.mesh.parm("rows").set(725)
                if self.step == 5:
                    name = self.materials[1].name()
                    self.materials[1].destroy()
                    self.materials[1] = ch.create_material(name=name, shader="emission")
                    # The default this test was written for (new nodes now take Blender's).
                    self.materials[1].node("surface_shader").setParms(dict(color=(0.8, 0.8, 0.8), strength=10.0))
                    self.assign.parm("shop_materialpath2").set("op:" + self.materials[1].path())
                assert len(self.source.geometry().prims()) == 4
                report["host_edits"].append(time.perf_counter() - started)
                self.step += 1
                return
            if self.step == 8:
                fresh = ch._native.preserve_packed_geometry(
                    self.source.path(), hou.time(), "/mat/unused_missing_fallback"
                )
                current = ch._native.convert_preserved_packed_geometry(fresh)
                assert current["fingerprint"] != report["reference"]["fingerprint"]
                assert current["prototype_meshes"] == 1 and current["material_variants"] == 2
                assert all(row["triangles"] == 2 * 724 * 720 for row in current["instances"])
                assert {key for row in current["instances"] for key in row["materials"]} == {
                    material_key(node) for node in self.materials
                }
                assert material_key(self.materials[1]) != self.initial_keys[1]
                report["fresh_snapshot"] = current
                del fresh
                gc.collect()
                assert ch._native.geometry_lease_count() == 1
                self.obj.destroy()
                for node in self.materials:
                    node.destroy()
                self.deleted = now
                report["conversions_at_delete"] = len(self.rows)
                self.step += 1
                return
            if now - self.deleted < 1 or len(self.rows) <= report["conversions_at_delete"]:
                return
            self.stop.set()
            if self.thread.is_alive():
                return
            self.thread.join()
            if self.error:
                raise RuntimeError(self.error)
            assert all(row["thread"] != threading.get_ident() for row in self.rows)
            self.finish()
        except BaseException:
            self.finish(traceback.format_exc())


workflow = Workflow()
