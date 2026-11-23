# SPDX-License-Identifier: Apache-2.0
"""Host-independent contracts for evaluated packed Blender reference bounds."""

from copy import deepcopy
import importlib
import itertools
import sys
import threading
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "houdini/python3.13libs"))
bounds = importlib.import_module("cycles_houdini.blender.blend_bounds")
pytestmark = [pytest.mark.pure, pytest.mark.cpu]


def fixture():
    positions = np.array([[-2, -1, -3], [4, 5, 6], [0, 1, 2]], dtype=np.float32)
    matrix = np.array([[-2, 0.5, 0, 11], [0, 0, 3, -7], [0, -4, 0, 2], [0, 0, 0, 1]], dtype=np.float64)
    manifest = dict(
        source="asset.blend",
        selector_type="COLLECTION",
        selector="Set",
        frame=1.25,
        dependency_versions={"asset.blend": [100, 200, 300, 400], "linked.blend": [1, 2, 3, 4]},
        meshes=[dict(key="mesh", arrays=dict(positions="positions"), subdivision=dict(level=3))],
        instances=[dict(mesh_key="mesh", transform=matrix.reshape(-1).tolist())],
        materials=[],
    )
    return manifest, dict(positions=positions), matrix


class EvaluatedBoundsTests(unittest.TestCase):
    def test_negative_scale_collection_transform_and_axis_conversion_are_enclosed(self):
        manifest, arrays, matrix = fixture()
        record = bounds.evaluated_record(manifest, arrays)
        self.assertTrue(record["known"])
        box = np.array(record["bounds"])
        points = arrays["positions"] @ matrix[:3, :3].T + matrix[:3, 3]
        self.assertTrue((points >= box[:3]).all())
        self.assertTrue((points <= box[3:]).all())
        # Every control-hull corner is included, not just occupied vertices.
        corners = np.array(list(itertools.product(*zip(arrays["positions"].min(0), arrays["positions"].max(0)))))
        transformed = corners @ matrix[:3, :3].T + matrix[:3, 3]
        self.assertTrue((transformed >= box[:3]).all())
        self.assertTrue((transformed <= box[3:]).all())

    def test_distinct_frame_selector_and_dependency_revisions_have_distinct_identity(self):
        manifest, arrays, _ = fixture()
        identity = bounds.evaluated_record(manifest, arrays)["source_identity"]
        changes = [
            dict(frame=1.5),
            dict(selector="Other"),
            dict(selector_type="OBJECT"),
            dict(dependency_versions={"asset.blend": [101, 200, 300, 400]}),
        ]
        for change in changes:
            self.assertNotEqual(identity, bounds.evaluated_record(dict(manifest, **change), arrays)["source_identity"])

    def test_empty_and_shader_displaced_geometry_expose_unknown(self):
        manifest, arrays, _ = fixture()
        displaced = dict(
            manifest, materials=[dict(settings=dict(displacement_method="BOTH"), outputs=dict(displacement={}))]
        )
        self.assertFalse(bounds.evaluated_record(displaced, arrays)["known"])
        self.assertFalse(bounds.evaluated_record(manifest, dict(positions=np.empty((0, 3))))["known"])
        bumped = deepcopy(displaced)
        bumped["materials"][0]["settings"]["displacement_method"] = "BUMP"
        self.assertTrue(bounds.evaluated_record(bumped, arrays)["known"])

    def test_dependency_retention_is_bounded(self):
        manifest, arrays, _ = fixture()
        manifest["dependency_versions"] = {str(i): [1, 2, 3, 4] for i in range(bounds.MAX_DEPENDENCIES + 1)}
        record = bounds.evaluated_record(manifest, arrays)
        self.assertFalse(record["known"])
        self.assertEqual(record["dependency_versions"], {})
        manifest["dependency_versions"] = {"x" * bounds.MAX_RECORD_BYTES: [1, 2, 3, 4]}
        record = bounds.evaluated_record(manifest, arrays)
        self.assertFalse(record["known"])
        self.assertEqual(record["dependency_versions"], {})

    def test_native_resident_boxes_match_arrays_without_re_evaluation(self):
        manifest, arrays, _ = fixture()
        box = [*arrays["positions"].min(0), *arrays["positions"].max(0)]
        resident = bounds.evaluated_record(manifest, None, mesh_boxes=dict(mesh=box))
        direct = bounds.evaluated_record(manifest, arrays)
        self.assertEqual(resident, direct)

    def test_invalid_positions_and_projective_matrices_are_rejected(self):
        manifest, arrays, _ = fixture()
        with self.assertRaises(ValueError):
            bounds.evaluated_record(manifest, dict(positions=np.array([[np.nan, 0, 0]])))
        manifest["instances"][0]["transform"][-1] = 2
        with self.assertRaises(ValueError):
            bounds.evaluated_record(manifest, arrays)

    def test_busy_refresh_does_not_claim_a_different_reference_succeeded(self):
        job = bounds.BoundsRefresh()
        job.key = ("a.blend#object=Cube", 1.25, True)
        fake_assets = SimpleNamespace()
        fake_hou = SimpleNamespace()
        with (
            patch.object(bounds, "_refresh_job", job),
            patch.dict(
                sys.modules,
                {
                    "cycles_houdini.blender.blender_assets": fake_assets,
                    "hou": fake_hou,
                },
            ),
        ):
            self.assertIs(bounds.refresh_bounds("a.blend#object=Cube", 1.25), job)
            with self.assertRaisesRegex(RuntimeError, "different.*already running"):
                bounds.refresh_bounds("b.blend#object=Cube", 1.25)

    def test_resident_mesh_is_reduced_once_for_many_instances(self):
        manifest, arrays, _ = fixture()
        entry = dict(
            bounds_manifest=manifest,
            cache_key=(None,) * 7 + (True,),
            instances=[dict(geometry_key="mesh", mesh=object(), transform=manifest["instances"][0]["transform"])] * 100,
        )
        calls = []

        def mesh_bounds(capsule):
            calls.append(capsule)
            return [*arrays["positions"].min(0), *arrays["positions"].max(0)]

        package = importlib.import_module("cycles_houdini")
        with patch.object(package, "_native", SimpleNamespace(blend_mesh_bounds=mesh_bounds)):
            record = bounds.prepared_record(entry)
        self.assertTrue(record["known"])
        self.assertEqual(len(calls), 1)

    def test_large_transform_cancellation_still_contains_exact_corner(self):
        manifest, arrays, _ = fixture()
        arrays["positions"] = np.array([[2**80, 1, 0]], dtype=np.float32)
        matrix = np.eye(4)
        matrix[0] = [1, 1, 0, -float(2**80)]
        manifest["instances"][0]["transform"] = matrix.reshape(-1).tolist()
        record = bounds.evaluated_record(manifest, arrays)
        # Exact arithmetic gives x=1 although floating dot-product cancellation
        # can give zero. Margin must scale with intermediate products.
        self.assertTrue(record["known"])
        self.assertLessEqual(record["bounds"][0], 1)
        self.assertGreaterEqual(record["bounds"][3], 1)

    def test_scene_change_during_async_evaluation_prevents_publication(self):
        manifest, arrays, _ = fixture()
        entered, release = threading.Event(), threading.Event()
        key = ("blender.exe", "asset.blend", "COLLECTION", "Set", 1.25, None, "working", True)
        fake_assets = SimpleNamespace(_resolve_spec=lambda spec: (key, {}), _hip_event=object())
        fake_hou = SimpleNamespace(hipFile=SimpleNamespace(eventCallbacks=lambda: [fake_assets._hip_event]))

        def evaluate_many(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("Test release barrier was not signalled")
            return [(manifest, arrays)]

        fake_worker = SimpleNamespace(
            _epoch=lambda source: 0,
            launch_signature=lambda *args: 0,
            evaluate_many=evaluate_many,
            _signature=lambda path: tuple(manifest["dependency_versions"][path]),
            StaleEvaluationError=RuntimeError,
        )
        package = importlib.import_module("cycles_houdini.blender")
        with (
            patch.object(bounds, "_refresh_job", None),
            patch.object(bounds, "_refresh_generation", 0),
            patch.object(bounds, "publish_record") as publish,
            patch.object(package, "blender_assets", fake_assets, create=True),
            patch.object(package, "blend_worker", fake_worker, create=True),
            patch.dict(sys.modules, {"hou": fake_hou}),
        ):
            job = bounds.refresh_bounds("asset.blend#collection=Set", 1.25)
            self.assertTrue(entered.wait(3))
            bounds.invalidate_refreshes()
            release.set()
            self.assertTrue(job.done.wait(3))
            self.assertIsInstance(job.error, RuntimeError)
            self.assertIn("scene changed", str(job.error))
            publish.assert_not_called()

    def test_ui_refresh_stops_when_original_node_is_deleted(self):
        primitive = SimpleNamespace(
            intrinsicNames=lambda: ("cycles_blender_reference", "cycles_blender_frame"),
            intrinsicValue=lambda name: "asset.blend#object=Cube" if name == "cycles_blender_reference" else 1,
        )
        node = SimpleNamespace(
            geometry=lambda: SimpleNamespace(prims=lambda: [primitive]), sessionId=lambda: 42, parm=lambda _: None
        )
        callbacks = []
        fake_hou = SimpleNamespace(
            isUIAvailable=lambda: True,
            nodeBySessionId=lambda ident: None,
            ui=SimpleNamespace(addEventLoopCallback=callbacks.append, removeEventLoopCallback=callbacks.remove),
        )
        job = bounds.BoundsRefresh()
        job.done.set()
        node_bounds = importlib.import_module("cycles_houdini.blender.node_bounds")
        with (
            patch.dict(sys.modules, {"hou": fake_hou}),
            patch.object(bounds, "refresh_many", return_value=job),
            patch.object(node_bounds, "cook_node"),
            patch.object(node_bounds, "_rows", return_value=[("asset.blend#object=Cube", 1, "asset.blend")]),
        ):
            bounds.refresh_node_bounds(node)
            self.assertEqual(len(callbacks), 1)
            callbacks[0]()
            self.assertEqual(callbacks, [])

    def test_first_refresh_subscribes_before_requesting_hull_evaluation(self):
        node_bounds = importlib.import_module("cycles_houdini.blender.node_bounds")
        order = []
        node = SimpleNamespace(
            sessionId=lambda: 42,
            parm=lambda name: SimpleNamespace(evalAsString=lambda: "hull", eval=lambda: "hull")
            if name == "viewport_shape"
            else None,
        )
        job = bounds.BoundsRefresh()

        def refresh(specs, **kwargs):
            self.assertEqual(order, ["subscribe"])
            self.assertEqual(specs, [("asset.blend#object=Cube", 1.25, True)])
            self.assertEqual(kwargs, dict(supersede=True, hulls=True, hull_vertices=64, preview_owner=None))
            order.append("refresh")
            return job

        with (
            patch.dict(sys.modules, {"hou": SimpleNamespace(isUIAvailable=lambda: False)}),
            patch.object(node_bounds, "cook_node", side_effect=lambda _: order.append("subscribe")),
            patch.object(node_bounds, "_rows", return_value=[("asset.blend#object=Cube", 1.25, "asset.blend")]),
            patch.object(bounds, "refresh_many", side_effect=refresh),
        ):
            self.assertIs(bounds.refresh_node_bounds(node), job)
        self.assertEqual(order, ["subscribe", "refresh"])
        self.assertEqual(job.owner, (42, node))


if __name__ == "__main__":
    unittest.main()
