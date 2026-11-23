# SPDX-License-Identifier: Apache-2.0
"""Production native scene conversion survives concurrent edits and deletion.

One Python SOP supplies a small curve/point fixture. The heavy mesh, nested
instances, volume and deformation use native SOPs. This checks the conversion
boundary, not the artist controller's scheduling or an IPR latency budget.
"""

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
from cycles_houdini import native_scene

path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
report = dict(
    result="running",
    host_edits=[],
    conversions=[],
    heartbeat_gaps=[],
    scope="Complete production geometry acquisition/conversion with nested mixed packed leaves and sampled deformation. Artist capture remains synchronous.",
)


class Workflow:
    def __init__(self):
        self.started = self.last = time.perf_counter()
        self.step = 0
        self.thread = self.error = self.capture = self.engine = None
        self.leases = []
        self.converted = []
        self.deleted = threading.Event()
        self.timer = QtCore.QTimer(hou.qt.mainWindow())
        self.timer.setInterval(16)
        self.timer.timeout.connect(self.tick)
        self.timer.start()

    def setup(self):
        hou.setFps(24)
        hou.setFrame(1)
        self.obj = hou.node("/obj").createNode("geo", "frozen_mixed_packed", run_init_scripts=False)
        self.obj.parm("tx").setExpression("$T * .24", hou.exprLanguage.Hscript)
        self.surface = ch.create_material(name="frozen_scene_surface", shader="emission")
        self.surface.node("surface_shader").parmTuple("color").set((0.2, 0.4, 0.8))
        self.surface.node("surface_shader").parm("strength").set(1)
        self.medium = ch.create_material(name="frozen_scene_medium", shader="absorption_volume")
        self.medium.node("volume_shader").parm("density").set(0.4)
        self.mesh = self.obj.createNode("grid", "million_triangle_mesh")
        self.mesh.setParms(dict(rows=721, cols=721, orient=0))
        self.mesh.parmTuple("size").set((1, 1))
        self.mesh_edit = self.obj.createNode("xform", "edit_mesh")
        self.mesh_edit.setInput(0, self.mesh)
        self.extra = self.obj.createNode("python", "one_small_curve_and_points")
        self.extra.parm("python").set("""import hou
g=hou.pwd().geometry()
g.addAttrib(hou.attribType.Point,'width',.05)
g.addAttrib(hou.attribType.Point,'pscale',.05)
curve=g.createPolygon(is_closed=False)
for p in [(0.7,-.5,0),(0.7,.5,0)]:
 point=g.createPoint();point.setPosition(p);curve.addVertex(point)
for p in [(1,-.5,0),(1,-.2,0),(1,.2,0),(1,.5,0)]:
 point=g.createPoint();point.setPosition(p)
""")
        box = self.obj.createNode("box", "volume_source")
        box.parmTuple("size").set((0.5, 0.5, 0.5))
        box.parmTuple("t").set((-1, 0, 0))
        vdb = self.obj.createNode("vdbfrompolygons", "density")
        vdb.setInput(0, box)
        vdb.setParms(dict(voxelsize=0.05, builddistance=0, buildfog=1, fillinterior=1))
        self.volume = self.obj.createNode("volumewrangle", "edit_density")
        self.volume.setInput(0, vdb)
        self.volume.parm("snippet").set("@density=.75;")
        assigned = self.obj.createNode("material", "volume_binding")
        assigned.setInput(0, self.volume)
        assigned.parm("shop_materialpath1").set(self.medium.path())
        merge = self.obj.createNode("merge", "mixed_prototype")
        for index, node in enumerate((self.mesh_edit, self.extra, assigned)):
            merge.setInput(index, node)
        targets = self.obj.createNode("grid", "instance_targets")
        targets.setParms(dict(rows=2, cols=2, orient=0))
        targets.parmTuple("size").set((4, 3))
        orientation = self.obj.createNode("attribwrangle", "orient_instances")
        orientation.setInput(0, targets)
        orientation.parm("snippet").set("v@N={0,0,1}; v@up={0,1,0};")
        copies = self.obj.createNode("copytopoints", "mixed_instances")
        copies.setInput(0, merge)
        copies.setInput(1, orientation)
        copies.parm("pack").set(1)
        identities = self.obj.createNode("attribwrangle", "instance_identity")
        identities.setInput(0, copies)
        identities.setParms({"class": 1, "snippet": "i@id=100+@primnum;"})
        nested = self.obj.createNode("pack", "nested_mixed_instances")
        nested.setInput(0, identities)
        self.source = self.obj.createNode("attribwrangle", "container_identity")
        self.source.setInput(0, nested)
        self.source.setParms({"class": 1, "snippet": "i@id=9;"})
        self.source.setRenderFlag(True)

        self.deform_obj = hou.node("/obj").createNode("geo", "sampled_mesh", run_init_scripts=False)
        plane = self.deform_obj.createNode("grid")
        plane.setParms(dict(rows=31, cols=31, orient=0))
        plane.parmTuple("size").set((0.6, 0.6))
        self.deform = self.deform_obj.createNode("xform")
        self.deform.setInput(0, plane)
        self.deform.parm("tx").setExpression("$T * 24", hou.exprLanguage.Hscript)
        self.deform.parm("tz").set(0.5)
        self.deform.setRenderFlag(True)
        self.camera = hou.node("/obj").createNode("cam", "frozen_scene_camera")
        self.camera.setParms(dict(tz=8, projection=1, orthowidth=9))
        self.capture = ch._native.create(
            os.environ.get("CYCLES_HOUDINI_GUI_DEVICE") or ch.default_device(),
            160,
            100,
            8,
            False,
            False,
            True,
            False,
            None,
            True,
        )
        self.baseline = ch._native.apply(
            self.capture,
            dict(
                camera_path=self.camera.path(),
                background=(1, 1, 1),
                motion=dict(enabled=True, fps=24, shutter_frames=0.5, samples=3),
            ),
            0.0,
        )
        self.engine = ch._native.engine_create(self.capture)
        self.scene = dict(
            geometry=[
                dict(
                    ch.geometry_request(self.obj, self.source, material_mode="native", motion="deformation", at_time=0),
                    object_material_path=self.surface.path(),
                ),
                dict(
                    ch.geometry_request(
                        self.deform_obj, self.deform, material_mode="native", motion="deformation", at_time=0
                    ),
                    object_material_path=self.surface.path(),
                ),
            ],
            motion=dict(enabled=True, fps=24, shutter_frames=0.5, samples=3),
            material_resolver=native_scene.resolve_materials,
        )
        self.reference_snapshot = ch._native.apply(self.capture, self.scene, 0.0)
        self.reference = ch._native.snapshot_info(self.reference_snapshot)
        assert self.reference["mesh_triangles"] == 2 * 720 * 720 + 2 * 30 * 30, self.reference
        assert (
            self.reference["points"] == 4 and self.reference["curves"] == 1 and self.reference["volume_fields"] == 1
        ), self.reference
        assert self.reference["geometry_resources"] == 5 and self.reference["objects"] == 17, self.reference
        report["reference"] = self.reference
        # All four jobs acquire the same source state before any host edit.
        self.leases = [ch._native.preserve_native_scene(self.scene, 0.0, self.baseline) for _ in range(4)]
        self.thread = threading.Thread(target=self.convert, name="Prepared native scene conversion")
        self.thread.start()

    def convert(self):
        try:
            try:
                ch._native.preserve_native_scene({}, 0.0, self.baseline)
            except RuntimeError as error:
                assert "host thread" in str(error)
            else:
                raise AssertionError("Background scene acquisition was allowed")
            for index, lease in enumerate(self.leases):
                # Prove conversion after deletion as well as during edits.
                if index == len(self.leases) - 1 and not self.deleted.wait(30):
                    raise TimeoutError("Host deletion did not complete")
                result = ch._native.convert_preserved_native_scene(lease)
                snapshot = result.pop("snapshot")
                assert ch._native.snapshot_info(snapshot) == self.reference
                result["after_delete"] = self.deleted.is_set()
                report["conversions"].append(result)
                self.converted.append(snapshot)
            try:
                ch._native.convert_preserved_native_scene(self.leases[0])
            except RuntimeError as error:
                assert "consumed" in str(error)
            else:
                raise AssertionError("Prepared scene was consumed twice")
        except BaseException:
            self.error = traceback.format_exc()

    def begin_render(self, snapshot):
        self.generation = ch._native.apply_snapshot(self.engine, snapshot)

    def rendered(self):
        frame = ch._native.frame(self.engine)
        if frame is None or not frame["complete"] or frame["generation"] != self.generation:
            return None
        return {key: np.frombuffer(value["pixels"], np.float32).copy() for key, value in frame["planes"].items()}

    def finish(self, error=None):
        self.timer.stop()
        self.deleted.set()
        if self.thread:
            self.thread.join()
        self.leases.clear()
        self.converted.clear()
        if self.engine:
            ch._native.close(self.engine)
        if self.capture:
            ch._native.close(self.capture)
        self.engine = self.capture = None
        gc.collect()
        count = ch._native.geometry_lease_count()
        if count:
            error = (error or "") + f" Retained native leases: {count}"
        report.update(
            result="failed" if error else "passed",
            error=error,
            live_leases=count,
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
                raise TimeoutError("Scene ownership workflow timed out")
            if self.error:
                raise RuntimeError(self.error)
            if self.capture is None:
                self.setup()
                return
            if self.step < 8:
                started = time.perf_counter()
                self.mesh_edit.parm("tz").set(0.1 * (self.step + 1))
                self.volume.parm("snippet").set("@density=" + str(1 + self.step) + ";")
                self.deform.parm("tz").set(0.5 + 0.1 * self.step)
                self.surface.node("surface_shader").parm("strength").set(2 + self.step)
                if self.step == 4:
                    self.mesh.parm("rows").set(725)
                    self.surface.setName("renamed_frozen_scene_surface")
                    for request in self.scene["geometry"]:
                        request["object_material_path"] = self.surface.path()
                self.source.cook(force=True)
                self.deform.cook(force=True)
                report["host_edits"].append(time.perf_counter() - started)
                self.step += 1
                return
            if self.step == 8:
                fresh = ch._native.preserve_native_scene(self.scene, 0.0, self.baseline)
                current = ch._native.convert_preserved_native_scene(fresh)
                fresh_info = ch._native.snapshot_info(current["snapshot"])
                assert fresh_info["mesh_triangles"] == 2 * 724 * 720 + 2 * 30 * 30, fresh_info
                report["fresh"] = fresh_info
                del fresh, current
                self.obj.destroy()
                self.deform_obj.destroy()
                self.surface.destroy()
                self.medium.destroy()
                self.camera.destroy()
                self.deleted.set()
                self.step = 9
                return
            if self.step == 9:
                if self.thread.is_alive():
                    return
                self.thread.join()
                if self.error:
                    raise RuntimeError(self.error)
                assert len(self.converted) == 4 and any(row["after_delete"] for row in report["conversions"])
                self.begin_render(self.reference_snapshot)
                self.step = 10
                return
            if self.step == 10:
                self.reference_image = self.rendered()
                if self.reference_image is None:
                    return
                self.begin_render(self.converted[-1])
                self.step = 11
                return
            if self.step == 11:
                image = self.rendered()
                if image is None:
                    return
                report["plane_max_errors"] = {}
                for name, values in self.reference_image.items():
                    assert np.isfinite(image[name]).all(), name
                    error = float(np.max(np.abs(values - image[name])))
                    assert error < 1e-5, (name, error)
                    report["plane_max_errors"][name] = error
                assert np.std(image["combined"]) > 0.1
                report["checks"] = [
                    "Independently specified mixed prototype counts and instance sharing",
                    "Concurrent geometry/material edits and source deletion",
                    "Single-use transaction and host-only acquisition guards",
                    "Retained surface, volume, motion, camera and data passes match pre-edit reference after deletion",
                ]
                self.finish()
        except BaseException:
            self.finish(traceback.format_exc())


workflow = Workflow()
