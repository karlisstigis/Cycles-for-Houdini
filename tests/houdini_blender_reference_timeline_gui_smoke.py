# SPDX-License-Identifier: Apache-2.0
"""Actual viewport timeline retention with independent Shape/Source Frame.

Constant and expression-driven Source Frame changes never request a new frozen
Shape Frame. Inspect captured owned widgets and write the adjacent visual verdict.
"""

import hashlib
import json
import time
import traceback

import hou

from cycles_houdini.blender import blend_bounds, blend_catalogue, blend_worker
from houdini_blender_reference_hulls_gui_smoke import Workflow


class TimelineWorkflow(Workflow):
    fixture_script = "blend_reference_timeline_fixture.py"

    def __init__(self):
        self.evaluations = []
        original = blend_worker.evaluate_many

        def evaluate(*args, **kwargs):
            self.evaluations.append(dict(requests=repr(args[0]) if args else repr(kwargs)))
            return original(*args, **kwargs)

        blend_worker.evaluate_many = evaluate
        hou.setFrame(1)
        super().__init__()
        self.display = self.node
        self.copies = None
        self.expected_evaluations = None
        self.labels = [
            "constant_h1",
            "constant_h37",
            "constant_h_minus2",
            "copies_h37",
            "animated_source_h1",
            "animated_source_h9",
            "animated_source_h_minus2",
            "shape9_wire",
            "shape9_shaded",
        ]

    def capture(self, label):
        geometry = self.display.geometry()
        primitives = geometry.prims()
        self.report["diagnostic"] = dict(
            stage=label,
            errors=self.node.errors(),
            warnings=self.node.warnings(),
            saved_preview_bytes=len(self.node.parm("saved_viewport_hulls").unexpandedString().encode())
            if self.node.parm("saved_viewport_hulls")
            else None,
            hull_points=[p.intrinsicValue("cycles_blender_viewport_hull_points") for p in primitives],
            preview_job_error=getattr(self.job, "error", None),
        )
        assert len(primitives) == (3 if self.copies else 1)
        assert all(p.intrinsicValue("cycles_blender_viewport_hull_points") > 3 for p in primitives)
        if self.expected_evaluations is not None:
            assert len(self.evaluations) == self.expected_evaluations, "A timeline edit re-evaluated the frozen shape"
        expected_source = hou.frame() + 0.25 if label.startswith("animated_source") or label.startswith("shape9") else 1
        observed = [p.intrinsicValue("cycles_blender_frame") for p in primitives]
        assert observed == [expected_source] * len(primitives), (label, expected_source, observed)
        wireframe = [bool(p.intrinsicValue("cycles_blender_viewport_wireframe")) for p in primitives]
        assert wireframe == [label != "shape9_shaded"] * len(primitives), (label, wireframe)
        images = []
        for kind, window in zip(("viewport", "parameters"), self.windows):
            filename = self.path.with_suffix("." + label + "." + kind + ".png")
            assert window.grab().save(str(filename))
            images.append(str(filename))
        intrinsics = primitives[0].intrinsicNames()
        self.report["captures"].append(
            dict(
                stage=label,
                images=images,
                houdini_frame=hou.frame(),
                source_frames=observed,
                shape_frames=[p.intrinsicValue("cycles_blender_shape_frame") for p in primitives]
                if "cycles_blender_shape_frame" in intrinsics
                else [],
                detail_ids=[p.intrinsicValue("cycles_blender_viewport_detail_id") for p in primitives],
                hull_digest=hashlib.sha256(
                    primitives[0].intrinsicValue("cycles_blender_viewport_hull").encode()
                ).hexdigest(),
                source_time_dependent=self.node.isTimeDependent(),
                source_cook_count=self.node.cookCount(),
                blender_evaluation_batches=len(self.evaluations),
                wireframe=wireframe,
            )
        )
        self.save()

    def make_copies(self):
        targets = self.geo.createNode("attribwrangle", "owned_timeline_targets")
        targets.parm("class").set(0)
        targets.parm("snippet").set("addpoint(0,{-2,0,0});addpoint(0,{0,0,0});addpoint(0,{2,0,0});")
        self.copies = self.geo.createNode("copytopoints::2.0", "owned_timeline_copies")
        self.copies.setInput(0, self.node)
        self.copies.setInput(1, targets)
        self.copies.setDisplayFlag(True)
        self.copies.setRenderFlag(True)
        self.display = self.copies
        self.copies.cook(force=True)

    def tick(self):
        try:
            if time.monotonic() - self.started > 200:
                raise TimeoutError("Source/Shape Frame timeline GUI gate timed out")
            if self.phase == "review":
                if not self.verdict.is_file():
                    return
                verdict = json.loads(self.verdict.read_text(encoding="utf-8-sig"))
                if verdict.get("result") in ("passed", "failed"):
                    self.report.update(result=verdict["result"], visual_verdict=verdict)
                    self.save()
                    hou.ui.removeEventLoopCallback(self.tick)
                    hou.exit(0 if verdict["result"] == "passed" else 1, suppress_save_prompt=True)
            elif self.phase == "catalogue":
                blend_catalogue.drain_catalogues()
                if self.job["done"].is_set():
                    assert not self.job["error"], self.job["error"]
                    self.job = blend_bounds.refresh_node_bounds(self.node)
                    self.phase = "refresh"
            elif self.phase == "refresh":
                if self.job.done.is_set():
                    self.job.publish()
                    self.node.cook(force=True)
                    self.expected_evaluations = len(self.evaluations)
                    self.phase, self.due = "capture", time.monotonic() + 1
            elif self.phase == "capture" and time.monotonic() >= self.due:
                self.capture(self.labels[self.stage])
                self.stage += 1
                if self.stage == 1:
                    hou.setFrame(37)
                elif self.stage == 2:
                    hou.setFrame(-2)
                elif self.stage == 3:
                    self.make_copies()
                    hou.setFrame(37)
                elif self.stage == 4:
                    self.node.parm("ref_frame1").setExpression("$FF + .25", hou.exprLanguage.Hscript)
                    self.node.cook(force=True)  # Only the parameter edit forces a cook.
                    hou.setFrame(1)
                elif self.stage == 5:
                    hou.setFrame(9)  # Playback changes never call cook(force=True).
                elif self.stage == 6:
                    hou.setFrame(-2)
                elif self.stage == 7:
                    self.node.parm("shape_frame").set(9)
                    self.job = blend_bounds.refresh_node_bounds(self.node)
                    self.phase = "refresh"
                elif self.stage == 8:
                    self.node.parm("viewport_wireframe").set(0)
                    self.node.cook(force=True)
                else:
                    self.report.update(
                        result="ready",
                        pending_visual_checks=[
                            "One fixed shape remains visible at Houdini frames 1, 37 and -2",
                            "Three Copy to Points instances remain visible through timeline changes",
                            "Animated Source Frame follows 1.25, 9.25 and -1.75 while Shape Frame stays 1",
                            "Changing Shape Frame to 9 changes only the preview pose",
                            "Wireframe preview has edges only; disabling Wireframe restores shaded hulls",
                        ],
                    )
                    self.save()
                    self.phase = "review"
                self.due = time.monotonic() + 1
        except Exception:
            self.report.update(result="failed", error=traceback.format_exc())
            self.save()
            hou.ui.removeEventLoopCallback(self.tick)
            hou.exit(1, suppress_save_prompt=True)


workflow = TimelineWorkflow()
