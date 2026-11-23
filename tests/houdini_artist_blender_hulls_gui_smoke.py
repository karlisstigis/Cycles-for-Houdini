# SPDX-License-Identifier: Apache-2.0
"""Read-only artist HIP viewport reproduction; visual verdict is external."""

import hashlib
import json
import os
from pathlib import Path
import time
import traceback

import hou
from PySide6 import QtGui
from cycles_houdini.blender import blend_bounds, blend_catalogue, node_bounds
from support.private_scenes import artist_scene


ORIGINAL = artist_scene("Cycles_Test/Blender_Instancing.hiplc")
SETTLE = 2.0


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class Reproduction:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.verdict = self.path.with_suffix(".verdict.json")
        self.original_hash = sha256(ORIGINAL)
        hou.hipFile.load(str(ORIGINAL), suppress_save_prompt=True, ignore_load_warnings=True)
        self.geo = hou.node("/obj/bottles1")
        self.source = self.geo.node("blender_reference2")
        self.transform = self.geo.node("transform1")
        self.random = self.geo.node("attribrandomize1")
        self.output = self.geo.node("copytopoints1")
        assert all((self.geo, self.source, self.transform, self.random, self.output))
        assert self.output.inputs() == (self.transform, self.random)
        for obj in hou.node("/obj").children():
            if hasattr(obj, "setDisplayFlag"):
                obj.setDisplayFlag(obj == self.geo)
        for node in self.geo.children():
            if hasattr(node, "setTemplateFlag"):
                node.setTemplateFlag(False)
            node.setSelected(False)
        for pane in hou.ui.curDesktop().paneTabs():
            if pane.type() == hou.paneTabType.SceneViewer:
                pane.setType(hou.paneTabType.NetworkEditor)
        self.output.setDisplayFlag(True)
        self.output.setRenderFlag(True)
        self.source.setParms(
            dict(
                ref_target1="wine*",
                max_hull_vertices=16,
                shape_frame=1,
                ref_frame1=1,
                viewport_shape="hull",
                viewport_wireframe=0,
            )
        )
        self.random.parm("seed").set(5737)
        hou.setFrame(1)
        self.base_scale = self.transform.parm("scale").eval()
        self.base_rotation = self.transform.parmTuple("r").eval()
        self.viewer = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.SceneViewer, size=(1700, 1000))
        self.viewer.setPwd(self.geo)
        self.viewer.setCurrentNode(self.output)
        self.viewport = self.viewer.curViewport()
        self.viewport.changeType(hou.geometryViewportType.Perspective)
        self.window = hou.qt.floatingPanelWindow(self.viewer.floatingPanel())
        screens = QtGui.QGuiApplication.screens()
        candidates = [
            screen
            for screen in screens
            if "UP2720Q" in " ".join((screen.name(), screen.model(), screen.manufacturer())).upper()
        ]
        if not candidates:
            candidates = [
                screen
                for screen in screens
                if "DELL" in " ".join((screen.name(), screen.model(), screen.manufacturer())).upper()
                and (
                    round(screen.size().width() * screen.devicePixelRatio()),
                    round(screen.size().height() * screen.devicePixelRatio()),
                )
                == (3840, 2160)
            ]
        assert len(candidates) == 1, [(screen.name(), screen.model()) for screen in screens]
        screen = candidates[0]
        self.screen = screen
        self.window.showNormal()
        if self.window.windowHandle():
            self.window.windowHandle().setScreen(screen)
        area = screen.availableGeometry()
        self.window.setGeometry(
            area.left() + 35, area.top() + 50, min(1800, area.width() - 70), min(1100, area.height() - 100)
        )
        self.window.raise_()
        self.report = dict(
            result="running",
            product_acceptance=False,
            original=str(ORIGINAL),
            original_sha256=self.original_hash,
            original_saved=False,
            screen=screen.name(),
            screen_model=screen.model(),
            screen_geometry=list(screen.geometry().getRect()),
            captures=[],
            capture_settle_seconds=SETTLE,
            first_image_latency_measured=False,
            source=self.source.path(),
            output=self.output.path(),
            verdict_file=str(self.verdict),
        )
        self.stages = [
            dict(label="boxes_seed5737", shape="bounds", wire=0, seed=5737),
            dict(label="boxes_piece_off_seed5737", shape="bounds", wire=0, seed=5737, piece=0),
            dict(label="boxes_piece_on_restored_seed5737", shape="bounds", wire=0, seed=5737),
            dict(label="boxes_seed5738", shape="bounds", wire=0, seed=5738),
            dict(label="hull_seed5737", shape="hull", wire=0, seed=5737),
            dict(label="hull_piece_off_seed5737", shape="hull", wire=0, seed=5737, piece=0),
            dict(label="hull_piece_on_restored_seed5737", shape="hull", wire=0, seed=5737),
            dict(label="hull_seed5738", shape="hull", wire=0, seed=5738),
            dict(label="wire_seed5737", shape="hull", wire=1, seed=5737),
            dict(label="wire_seed5738", shape="hull", wire=1, seed=5738),
            dict(label="hull_after_wire_seed5737", shape="hull", wire=0, seed=5737),
            dict(label="rotated_scaled_seed5737", shape="hull", wire=0, seed=5737, transform=True),
            dict(label="rotated_scaled_seed5738", shape="hull", wire=0, seed=5738, transform=True),
            dict(label="timeline9_seed5737", shape="hull", wire=0, seed=5737, frame=9),
            dict(label="timeline1_seed5737", shape="hull", wire=0, seed=5737, frame=1),
            dict(label="exact_subset_seed5737", shape="hull", wire=0, seed=5737, subset=True),
            dict(label="wine_wildcard_restored_seed5737", shape="hull", wire=0, seed=5737),
            dict(label="wine_wildcard_restored_seed5738", shape="hull", wire=0, seed=5738),
        ]
        if os.getenv("CH_ARTIST_REPRO_SHORT") == "1":
            selected = {
                "boxes_seed5737",
                "boxes_seed5738",
                "hull_seed5737",
                "hull_piece_off_seed5737",
                "hull_piece_on_restored_seed5737",
                "hull_seed5738",
                "wire_seed5737",
                "wire_seed5738",
                "exact_subset_seed5737",
                "wine_wildcard_restored_seed5737",
                "wine_wildcard_restored_seed5738",
            }
            self.stages = [stage for stage in self.stages if stage["label"] in selected]
        self.started = time.monotonic()
        self.index = 0
        self.phase = "catalogue"
        self.job = blend_catalogue.request_catalogues(self.source, refresh_bounds=False)
        self.save()
        hou.ui.addEventLoopCallback(self.tick)

    def save(self):
        self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")

    def apply_stage(self):
        stage = self.stages[self.index]
        self.source.setParms(
            dict(
                viewport_shape=stage["shape"],
                viewport_wireframe=stage["wire"],
                ref_target1=self.subset if stage.get("subset") else "wine*",
            )
        )
        self.random.parm("seed").set(stage["seed"])
        self.output.parm("useidattrib").set(stage.get("piece", 1))
        self.transform.parm("scale").set(self.base_scale * (1.35 if stage.get("transform") else 1))
        self.transform.parmTuple("r").set((20, 45, 15) if stage.get("transform") else self.base_rotation)
        hou.setFrame(stage.get("frame", 1))
        blend_catalogue.viewport_changed(self.source)
        self.output.setDisplayFlag(True)
        self.viewer.setPwd(self.geo)
        self.viewer.setCurrentNode(self.output)
        started = time.perf_counter()
        geometry = self.output.geometry()
        self.cook_seconds = time.perf_counter() - started
        assert not self.output.errors(), self.output.errors()
        if self.index == 0:
            self.viewport.frameBoundingBox(geometry.boundingBox())
        self.window.setWindowTitle("Artist HIP viewport — " + stage["label"])
        self.phase = "capture"
        self.due = time.monotonic() + SETTLE

    def capture(self):
        stage = self.stages[self.index]
        geometry = self.output.geometry()
        source_geo = self.source.geometry()
        primitives = geometry.prims()
        counts, details, hull_points, lod = {}, set(), set(), {}
        for primitive in primitives:
            name = primitive.attribValue("name")
            counts[name] = counts.get(name, 0) + 1
            details.add(primitive.intrinsicValue("cycles_blender_viewport_detail_id"))
            hull_points.add(primitive.intrinsicValue("cycles_blender_viewport_hull_points"))
            value = primitive.intrinsicValue("viewportlod")
            lod[value] = lod.get(value, 0) + 1
        names = set(source_geo.primStringAttribValues("name"))
        target_names = self.random.geometry().pointStringAttribValues("name")
        expected = (
            sum(name in names for name in target_names)
            if self.output.evalParm("useidattrib")
            else len(names) * len(target_names)
        )
        assert len(primitives) == expected, (len(primitives), expected, names)
        if not stage.get("subset") and self.output.evalParm("useidattrib"):
            assert len(primitives) == 4912, len(primitives)
        filename = self.path.with_suffix("." + stage["label"] + ".png")
        assert self.screen.grabWindow(int(self.window.winId())).save(str(filename))
        box = geometry.boundingBox()
        self.report["captures"].append(
            dict(
                stage=stage["label"],
                image=str(filename),
                frame=hou.frame(),
                seed=self.random.evalParm("seed"),
                shape=self.source.evalParm("viewport_shape"),
                useidattrib=self.output.evalParm("useidattrib"),
                idattrib=self.output.evalParm("idattrib"),
                pack=self.output.evalParm("pack"),
                wire=self.source.evalParm("viewport_wireframe"),
                selection=self.source.evalParm("ref_target1"),
                source_primitives=len(source_geo.prims()),
                output_primitives=len(primitives),
                expected_primitives=expected,
                target_points=len(target_names),
                counts=counts,
                detail_ids=sorted(details),
                hull_points=sorted(hull_points),
                lod=lod,
                bbox=list(box.minvec()) + list(box.maxvec()),
                cook_seconds=self.cook_seconds,
                source_warnings=self.source.warnings(),
                output_warnings=self.output.warnings(),
                source_scale=self.transform.evalParm("scale"),
                source_rotation=list(self.transform.parmTuple("r").eval()),
                viewer_context=self.viewer.pwd().path(),
                display_node=self.geo.displayNode().path(),
                settled_capture=True,
                visual_visibility="requires external visual verdict",
            )
        )
        self.save()
        self.index += 1
        if self.index < len(self.stages):
            self.apply_stage()
        else:
            assert sha256(ORIGINAL) == self.original_hash, "Original HIP bytes changed"
            self.report.update(
                result="awaiting_visual_review",
                original_sha256_after=sha256(ORIGINAL),
                review_notes="Primitives/cooked counts do not prove visible drawing. Compare screenshots.",
            )
            self.phase = "review"
            self.save()

    def tick(self):
        try:
            if time.monotonic() - self.started > 540:
                raise TimeoutError("Artist viewport reproduction timed out awaiting capture/review")
            if self.phase == "catalogue":
                blend_catalogue.drain_catalogues()
                if self.job["done"].is_set():
                    assert not self.job["error"], self.job["error"]
                    self.job = blend_bounds.refresh_node_bounds(self.source)
                    self.phase = "refresh"
            elif self.phase == "refresh":
                if self.job.done.is_set():
                    self.job.publish()
                    self.source.cook(force=True)
                    names = sorted(self.source.geometry().primStringAttribValues("name"))
                    assert len(names) == 4, names
                    self.subset = " ".join(names[:2])
                    self.report["source_targets"] = names
                    self.apply_stage()
            elif self.phase == "capture" and time.monotonic() >= self.due:
                node_bounds.drain_pending_node_bounds()
                self.capture()
            elif self.phase == "review" and self.verdict.is_file():
                verdict = json.loads(self.verdict.read_text(encoding="utf-8-sig"))
                if verdict.get("result") not in ("passed", "failed"):
                    return
                self.report.update(
                    result=verdict["result"],
                    visual_verdict=verdict,
                    product_acceptance=verdict.get("product_acceptance", False),
                )
                self.save()
                hou.ui.removeEventLoopCallback(self.tick)
                hou.exit(0 if verdict["result"] == "passed" else 1, suppress_save_prompt=True)
        except Exception:
            self.report.update(result="failed", error=traceback.format_exc(), original_sha256_after=sha256(ORIGINAL))
            self.save()
            hou.ui.removeEventLoopCallback(self.tick)
            hou.exit(1, suppress_save_prompt=True)


workflow = Reproduction()
