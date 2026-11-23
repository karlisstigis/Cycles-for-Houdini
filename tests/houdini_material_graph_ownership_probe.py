# SPDX-License-Identifier: Apache-2.0
"""Owned native graphs survive host edits/deletion and still render correctly."""

import gc
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import traceback
import weakref

import hou
import numpy as np
from PySide6 import QtCore
import cycles_houdini as ch
from cycles_houdini import native_scene

path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
report = dict(
    result="running",
    scope="Frozen graph values and deferred diagnostics, not asynchronous artist capture or frozen texture file contents.",
    host_edits=[],
    checks=[],
)


class Workflow:
    def __init__(self):
        self.started = time.monotonic()
        self.step = 0
        self.lease = None
        self.thread = None
        self.error = None
        self.stop = threading.Event()
        self.reads = 0
        self.timer = QtCore.QTimer(hou.qt.mainWindow())
        self.timer.setInterval(20)
        self.timer.timeout.connect(self.tick)
        self.timer.start()

    def setup(self):
        hou.setFps(24)
        hou.setFrame(1)
        self.builder = ch.create_material(name="frozen_graph_surface", shader="emission")
        self.surface = self.builder.node("surface_shader")
        self.surface.parm("strength").setExpression("1+$T", hou.exprLanguage.Hscript)
        self.subnet = ch.create_subnet(self.builder, "color_controls")
        self.ramp = self.subnet.createNode("cycles::color_ramp::1", "color_ramp")
        self.ramp.parm("ramp").set(
            hou.Ramp((hou.rampBasis.Linear,) * 2, (0.0, 1.0), ((0.2, 0.4, 0.8), (0.8, 0.2, 0.1)))
        )
        self.factor = self.subnet.createNode("cycles::value::1", "factor")
        self.factor.parm("value").setExpression(".25+.5*$T", hou.exprLanguage.Hscript)
        self.ramp.setNamedInput("factor", self.factor, "value")
        output = ch.create_connector(self.subnet, "result", kind="output", type="color")
        output.setInput(0, self.ramp)
        self.surface.setNamedInput("color", self.subnet, "result")
        self.displace = self.builder.createNode("cycles::vector_displacement::1", "offset")
        # The default this test was written for (new nodes now take Blender's).
        self.displace.setParms(dict(scale=1.0, space="object"))
        self.displace.parmTuple("vector").set((0.0, 0.0, 0.0))
        self.displace.parmTuple("vector")[2].setExpression(".2+.2*$T", hou.exprLanguage.Hscript)
        self.builder.node("surface_output").setNamedInput("displacement", self.displace, "displacement")
        self.builder.parm("cycles_displacement_method").set("true")
        self.volume = ch.create_material(name="frozen_graph_volume", shader="principled_volume")
        self.volume.node("volume_shader").parm("density").setExpression("2+$T", hou.exprLanguage.Hscript)
        self.bad = ch.create_material(name="frozen_graph_invalid", shader="emission")
        # The default this test was written for (new nodes now take Blender's).
        self.bad.node("surface_shader").setParms(dict(color=(0.8, 0.8, 0.8), strength=10.0))
        self.bad.node("surface_output").setInput(0, None)
        self.paths = [self.builder.path(), self.volume.path()]
        candidates = self.paths + ["op:" + self.paths[0], self.bad.path(), "/mat/frozen_graph_missing"]
        calls = []
        returned = []

        def resolver(references, at_time):
            calls.extend(row["key"] for row in references)
            result = native_scene.resolve_materials(references, at_time)
            returned.extend(result)
            return result

        resolver_ref = weakref.ref(resolver)
        self.lease = ch._native.preserve_material_graphs(candidates, resolver, 0.5)
        del resolver
        assert resolver_ref() is None, "Snapshot retained the host callback"
        assert len(calls) == len(set(calls)) == 3, calls
        self.reference = ch._native.convert_preserved_material_graphs(self.lease, self.paths)
        self.wire = self.reference["wire"]
        report["wire_sha256"] = hashlib.sha256(self.wire).hexdigest()
        surface, volume = self.reference["materials"]
        nodes = surface["graph"]["nodes"]
        emission = next(node for node in nodes if node["definition"] == "cycles/emission/1")
        assert emission["parameters"]["strength"] == 1.5
        offset = next(node for node in nodes if node["definition"] == "cycles/vector_displacement/1")
        assert np.allclose(offset["parameters"]["vector"], (0, 0, 0.3))
        assert surface["graph"]["displacement_method"] == "true"
        assert (
            next(node for node in volume["graph"]["nodes"] if node["definition"] == "cycles/principled_volume/1")[
                "parameters"
            ]["density"]
            == 2.5
        )
        # Mutable dictionaries returned by a callback are not the owned C++ graph.
        for value in returned:
            value["graph"]["nodes"].clear()
        assert ch._native.convert_preserved_material_graphs(self.lease, self.paths)["wire"] == self.wire
        for selected in (self.bad.path(), "/mat/frozen_graph_missing"):
            recovered = ch._native.convert_preserved_material_graphs(self.lease, [selected])
            assert recovered["materials"][0]["graph"] is None
            assert recovered["materials"][0]["kind"] == "diagnostic_surface"
        for selected, fragment in (("/mat/not_acquired", "not frozen"),):
            try:
                ch._native.convert_preserved_material_graphs(self.lease, [selected])
            except RuntimeError as error:
                assert fragment.lower() in str(error).lower(), str(error)
            else:
                raise AssertionError("Invalid selected material accepted")
        report["checks"].append(
            "Host-time values, alias deduplication, callback/dictionary ownership, deferred invalid/missing candidates"
        )
        self.thread = threading.Thread(target=self.convert)
        self.thread.start()

    def convert(self):
        try:
            for action in (
                lambda: ch._native.preserve_material_graphs([], native_scene.resolve_materials, 0.5),
                lambda: ch.material_graph(None),
                lambda: native_scene.resolve_materials([], 0.5),
            ):
                try:
                    action()
                except RuntimeError as error:
                    assert "host thread" in str(error), str(error)
                else:
                    raise AssertionError("Background live graph acquisition was allowed")
            while not self.stop.is_set():
                assert ch._native.convert_preserved_material_graphs(self.lease, self.paths)["wire"] == self.wire
                self.reads += 1
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
            error = (error or "") + " Leaked graph lease"
        report.update(
            result="failed" if error else "passed",
            error=error,
            background_reads=self.reads,
            live_leases=ch._native.geometry_lease_count(),
            seconds=time.monotonic() - self.started,
        )
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        hou.ui.postEventCallback(lambda: hou.exit(1 if error else 0, suppress_save_prompt=True))

    def tick(self):
        try:
            if time.monotonic() - self.started > 180:
                raise TimeoutError("Frozen graph ownership stalled")
            if self.error:
                raise RuntimeError(self.error)
            if self.lease is None:
                self.setup()
                return
            if self.step < 8:
                started = time.perf_counter()
                self.factor.parm("value").deleteAllKeyframes()
                self.factor.parm("value").set(0.1 * self.step)
                self.surface.parm("strength").deleteAllKeyframes()
                self.surface.parm("strength").set(2 + self.step)
                self.displace.parmTuple("vector")[2].deleteAllKeyframes()
                self.displace.parmTuple("vector")[2].set(0.4 + 0.01 * self.step)
                if self.step == 2:
                    self.builder.setName("renamed_frozen_graph")
                if self.step == 4:
                    self.surface.setInput(self.surface.inputNames().index("color"), None)
                    self.surface.parmTuple("color").set((0.1, 0.2, 0.3))
                report["host_edits"].append(time.perf_counter() - started)
                self.step += 1
                return
            if self.step == 8:
                fresh = ch._native.preserve_material_graphs([self.builder.path()], native_scene.resolve_materials, 0.5)
                current = ch._native.convert_preserved_material_graphs(fresh, [self.builder.path()])
                assert current["wire"] != self.wire
                assert current["materials"][0]["key"] == self.reference["materials"][0]["key"]
                del fresh
                self.builder.destroy()
                self.volume.destroy()
                self.bad.destroy()
                replacement = ch.create_material(name=self.paths[0].split("/")[-1], shader="emission")
                fresh = ch._native.preserve_material_graphs([replacement.path()], native_scene.resolve_materials, 0.5)
                assert (
                    ch._native.convert_preserved_material_graphs(fresh, [replacement.path()])["materials"][0]["key"]
                    != self.reference["materials"][0]["key"]
                )
                del fresh
                replacement.destroy()
                report["reads_at_delete"] = self.reads
                self.step += 1
                return
            if self.reads <= report["reads_at_delete"]:
                return
            self.stop.set()
            if self.thread.is_alive():
                return
            self.thread.join()
            if self.error:
                raise RuntimeError(self.error)
            # Render retained graph values after every source material is gone.
            obj = hou.node("/obj").createNode("geo", "frozen_graph_receiver", run_init_scripts=False)
            raw = obj.createNode("grid")
            raw.setParms(dict(type=0, orient=0, rows=2, cols=2))
            raw.parmTuple("size").set((8, 8))
            grid = obj.createNode("reverse")
            grid.setInput(0, raw)
            camera = hou.node("/obj").createNode("cam", "frozen_graph_camera")
            camera.setParms(dict(tz=5, projection=1, orthowidth=4))
            material = dict(self.reference["materials"][0], key="retained_surface")
            with ch.Session(
                device=ch.default_device(), width=32, height=32, samples=8, passes=["combined", "position"]
            ) as session:
                generation = session.apply(
                    dict(
                        camera_path=camera.path(),
                        geometry=[ch.geometry_request(obj, grid, material="retained_surface")],
                        materials=[material],
                        background=(0, 0, 0),
                    )
                )
                frame = session.wait(generation)
                rgb = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).reshape(32, 32, 4)[16, 16, :3]
                position = np.frombuffer(frame["planes"]["position"]["pixels"], np.float32).reshape(32, 32, 3)[16, 16]
                assert np.allclose(rgb, (0.75, 0.45, 0.675), atol=2e-5), rgb
                assert abs(position[2] - 0.3) < 1e-5, position
                report["rendered_rgb"] = rgb.tolist()
                report["rendered_position"] = position.tolist()
            report["checks"].append(
                "Concurrent parameter edits, rewiring, rename/delete/recreate, worker wire round trip and independent emission/displacement render"
            )
            self.finish()
        except BaseException:
            self.finish(traceback.format_exc())


workflow = Workflow()
