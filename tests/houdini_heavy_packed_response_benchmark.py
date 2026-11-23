# SPDX-License-Identifier: Apache-2.0
"""Heavy changing packed prototypes, with object/camera input during capture."""

import os
from pathlib import Path
import runpy
import time
import hou
from cycles_houdini import native_scene, registry

os.environ["CYCLES_HOUDINI_BENCH_SHARED_VIEWS"] = "0"
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
HeavyResponse = runpy.run_path(
    str(root / "tests/houdini_heavy_response_benchmark.py"), run_name="packed_response_fixture"
)["HeavyResponse"]


class PackedResponse(HeavyResponse):
    def save(self):
        # Keep measurement-time I/O small. Serializing the growing diagnostics
        # and heartbeat history on the GUI thread would measure the test itself.
        if self.report.get("result") == "running":
            import json

            progress = {
                key: self.report.get(key) for key in ("result", "phase", "execution", "device", "host_pid", "fixture")
            }
            progress["completed_edits"] = len(self.report["edits"])
            self.path.write_text(json.dumps(progress, indent=2))
        else:
            super().save()

    def __init__(self):
        self.painted = {}
        super().__init__()
        self.original_paint = type(self.panel.canvas).paintEvent

        def painted(canvas, event):
            self.original_paint(canvas, event)
            if canvas is not self.panel.canvas or canvas.image.isNull():
                return
            metadata = canvas.frame_metadata
            if not metadata:
                return
            now = time.monotonic()
            frame = self.painted.setdefault(metadata["generation"], dict(first=now, metadata=dict(metadata)))
            frame["latest"] = now
            frame["latest_metadata"] = dict(metadata)
            if metadata["rendered_width"] == 960 and metadata["rendered_height"] == 540:
                frame.setdefault("full", now)

        type(self.panel.canvas).paintEvent = painted

    def configure_scene(self):
        self.packed_obj = hou.node("/obj").createNode("geo", "heavy_packed", run_init_scripts=False)
        self.packed_obj.parm("tz").set(-5)
        self.packed_obj.parm("shop_materialpath").set("/mat/courtyard_noise_displaced_stone")
        self.prototype = self.packed_obj.createNode("grid", "MILLION_TRIANGLE_PROTOTYPE")
        self.prototype_rows = int(os.environ.get("CYCLES_HOUDINI_BENCH_PACKED_ROWS", "721"))
        self.prototype.setParms(dict(rows=self.prototype_rows, cols=self.prototype_rows))
        self.prototype.parmTuple("size").set((1.3, 1.3))
        self.deform = self.packed_obj.createNode("xform", "DEFORM_PROTOTYPE")
        self.deform.setInput(0, self.prototype)
        uv = self.packed_obj.createNode("attribwrangle", "PROTOTYPE_UVS")
        uv.setInput(0, self.deform)
        uv.parm("snippet").set("v@uv=set(@P.x/1.3+.5, @P.z/1.3+.5, 0);")
        source = uv
        self.instance_count = int(os.environ.get("CYCLES_HOUDINI_BENCH_PACKED_COUNT", "16"))
        if self.instance_count != 16 and (self.instance_count < 200 or self.instance_count % 100):
            raise ValueError("Packed count must be 16 or a multiple of 100")
        spacings = (1.8, 4.2) if self.instance_count == 16 else (20.0,)
        for level, spacing in enumerate(spacings):
            targets = self.packed_obj.createNode("grid", "targets_" + str(level))
            targets.setParms(
                dict(
                    rows=2 if self.instance_count == 16 else self.instance_count // 100,
                    cols=2 if self.instance_count == 16 else 100,
                )
            )
            targets.parmTuple("size").set((spacing, spacing if self.instance_count == 16 else 12.0))
            if self.instance_count != 16:
                self.prototype.parmTuple("size").set((20 / 99 * 0.65, 12 / (self.instance_count // 100 - 1) * 0.65))
            copies = self.packed_obj.createNode("copytopoints", "packed_level_" + str(level))
            copies.setInput(0, source)
            copies.setInput(1, targets)
            copies.parm("pack").set(1)
            source = copies
        source.setDisplayFlag(True)
        source.setRenderFlag(True)
        self.rop.parm("vobject").set("fx_courtyard heavy_packed")
        if os.environ.get("CYCLES_HOUDINI_BENCH_NO_MOTION") == "1":
            self.rop.parm("allowmotionblur").set(0)
        # Explicitly control both profiles; viewer settings must not inherit
        # whichever interactive denoising/sample defaults the asset ships with.
        self.rop.setParms(
            dict(
                samples=self.samples,
                ipr_samples=self.samples,
                adaptive_sampling=0,
                ipr_adaptive_sampling=0,
                use_denoise=0,
                ipr_use_denoise=0,
            )
        )
        self.navigation = []
        self.navigation_cases = set()

    def edit(self, name, repetition):
        if name == "transform":
            self.packed_obj.parm("tx").set(0.03 * (1 + repetition % 2))
        elif name == "mesh-deform":
            self.deform.parm("rx").set(5 + repetition * 7)
        elif name == "mesh-topology":
            self.prototype.parm("rows").set(722 + repetition * 2)
        else:
            super().edit(name, repetition)

    def tick(self):
        entry = registry.get(self.key)
        phase = self.report.get("phase", "")
        if (
            entry
            and phase.startswith(("mesh-deform-", "mesh-topology-"))
            and phase not in self.navigation_cases
            and entry.session._active_capture
        ):
            active = entry.session._active_capture
            if not active["future"].done():
                self.navigation_cases.add(phase)
                began = time.perf_counter()
                self.packed_obj.parm("tx").set(0.05 * len(self.navigation_cases))
                object_generation = entry.apply(native_scene.capture(self.rop), hou.time())
                object_seconds = time.perf_counter() - began
                began = time.perf_counter()
                self.camera.parm("tx").set(self.camera_x + 0.015 * len(self.navigation_cases))
                camera_generation = entry.apply(native_scene.capture(self.rop, camera_only=True), hou.time())
                self.navigation.append(
                    dict(
                        phase=phase,
                        stage=active["stage"],
                        object_seconds=object_seconds,
                        camera_seconds=time.perf_counter() - began,
                        object_generation=object_generation,
                        camera_generation=camera_generation,
                    )
                )
                self.report["concurrent_navigation"] = self.navigation
        if entry and self.stage == "editing":
            status = entry.session.status()
            frame = self.painted.get(status["generation"])
            if frame and status["generation"] > self.before:
                self.case.setdefault("first_painted_preview_seconds", frame["first"] - self.edit_started)
                self.case.setdefault("first_painted_frame", frame["metadata"])
                if "full" in frame:
                    self.case["full_frame_painted_seconds"] = frame["full"] - self.edit_started
                if self.ready(entry):
                    self.case.setdefault("updated_geometry_painted_seconds", frame["latest"] - self.edit_started)
                    self.case.setdefault("updated_geometry_frame", frame["latest_metadata"])
            if self.case["name"] == "camera" and self.ready(entry):
                capture = entry.session.diagnostics()["capture"]["events"][-1]
                assert capture["scan_batches"] == 0, ("Camera-only edit traversed the geometry roots", capture)
        super().tick()

    def ready(self, entry):
        if not super().ready(entry):
            return False
        status = entry.session.status()
        painted = self.painted.get(status["generation"], {})
        return bool(
            painted.get("full") and painted["latest_metadata"].get("render_revision") == status.get("render_revision")
        )

    def finish(self):
        assert len(self.navigation) == 6, self.navigation
        self.report["packed_fixture"] = dict(
            rows=self.prototype_rows,
            cols=self.prototype_rows,
            triangles_per_prototype=2 * (self.prototype_rows - 1) ** 2,
            instances=self.instance_count,
            nesting_levels=2 if self.instance_count == 16 else 1,
            motion_enabled=bool(self.rop.evalParm("allowmotionblur")),
            construction="Native Grid, Transform and Copy to Points SOPs",
        )
        self.report["packed_fixture"]["transform_workload"] = self.packed_obj.path() + " (moves every packed instance)"
        self.report["measurement"] = (
            "Actual Qt paint completion; first available scene image and full-resolution image recorded separately. Heartbeats are event-loop proxies. Only compact progress is written during measurements; full diagnostics serialize after timing stops."
        )
        type(self.panel.canvas).paintEvent = self.original_paint
        super().finish()


if __name__ == "__main__":
    workflow = PackedResponse()
