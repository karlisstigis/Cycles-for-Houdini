# SPDX-License-Identifier: Apache-2.0
"""Host-independent retention, shared allocation and transport budget contracts."""

from support.package_loader import import_product
import os
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import numpy as np


path = Path(__file__).resolve().parents[1] / "houdini/python3.13libs/cycles_houdini/blender/blend_cache.py"
cache = import_product("blend_cache")
worker = import_product("blend_worker")


class DigestReuseTests(unittest.TestCase):
    def test_disable_toggle_bypasses_budget_parsing_and_retention(self):
        with patch.dict(os.environ, {"CH_BLEND_DIGEST_REUSE": "0", "CYCLES_HOUDINI_BLEND_DIGEST_MB": "0"}):
            self.assertIsNone(worker._make_digest_cache())
        with patch.dict(os.environ, {"CH_BLEND_DIGEST_REUSE": "1", "CYCLES_HOUDINI_BLEND_DIGEST_MB": "2"}):
            self.assertEqual(worker._make_digest_cache().target_bytes, 2 * 1024 * 1024)
        with patch.dict(os.environ, {"CH_BLEND_DIGEST_REUSE": "1", "CYCLES_HOUDINI_BLEND_DIGEST_MB": "0"}):
            with self.assertRaises(ValueError):
                worker._make_digest_cache()

    def test_single_vertex_change_cannot_reuse_digest_or_skip_transfer(self):
        retained = worker._DigestCache(1024 * 1024)
        positions = np.zeros((2048, 3), dtype=np.float32)
        mesh_arrays = {"mesh_000000/positions": positions}
        manifest = dict(instances=[dict(mesh_key="mesh_000000", source_name="Deforming", source_library="")])
        known = set()

        class Connection:
            def __init__(self):
                self.frames = []

            def send_bytes(self, data):
                self.frames.append(bytes(data))

            def recv_bytes(self, limit):
                descriptors = json.loads(self.frames[0])["arrays"]
                self.missing = list(
                    dict.fromkeys(row["identity"] for row in descriptors if row["identity"] not in known)
                )
                known.update(self.missing)
                return json.dumps(dict(missing=self.missing)).encode()

        def send():
            connection, phases = Connection(), {}
            worker._send_result(connection, manifest, mesh_arrays, phases, retained)
            for identity, payload in zip(connection.missing, connection.frames[1:]):
                values = np.frombuffer(payload, dtype=np.float32).reshape(positions.shape)
                self.assertEqual(worker._array_id(values), identity)
            self.assertEqual(sum(row["sent_bytes"] for row in phases["arrays"]), phases["sent_bytes"])
            return phases

        first = send()
        repeated = send()
        self.assertEqual(first["hashed_bytes"], positions.nbytes)
        self.assertEqual(repeated["hashed_bytes"], 0)
        self.assertEqual(repeated["compared_bytes"], positions.nbytes)
        self.assertEqual(repeated["sent_bytes"], 0)
        self.assertEqual(repeated["arrays"][0]["object"], "Deforming")
        # In-place mutation also proves the previous snapshot owns its bytes.
        positions[-1, 2] = np.nextafter(np.float32(0), np.float32(1))
        changed = send()
        self.assertEqual(changed["hashed_bytes"], positions.nbytes)
        self.assertEqual(changed["sent_bytes"], positions.nbytes)
        self.assertNotEqual(first["arrays"][0]["identity"], changed["arrays"][0]["identity"])
        self.assertFalse(changed["arrays"][0]["digest_reused"])

    def test_same_count_topology_and_all_attribute_edits_rehash(self):
        retained = worker._DigestCache(1024 * 1024)
        for name, dtype, shape in (
            ("vertex_indices", np.int32, (2048,)),
            ("edges", np.int32, (2048, 2)),
            ("uv_0", np.float32, (2048, 2)),
            ("color_0", np.float32, (2048, 4)),
            ("attribute_0", np.int32, (2048,)),
            ("material_indices", np.int32, (2048,)),
            ("smooth", np.uint8, (8192,)),
            ("normals", np.float32, (2048, 3)),
        ):
            with self.subTest(name=name):
                values = np.zeros(shape, dtype=dtype)
                original = retained.identity(name, values)[0]
                self.assertTrue(retained.identity(name, values.copy())[3])
                values.reshape(-1)[-1] = 1
                identity, hashed, _, reused = retained.identity(name, values)
                self.assertFalse(reused)
                self.assertEqual(hashed, values.nbytes)
                self.assertEqual(identity, worker._array_id(values))
                self.assertNotEqual(identity, original)

    def test_bitwise_comparison_preserves_signed_zero_and_nan_payloads(self):
        retained = worker._DigestCache(1024, minimum_bytes=0)
        values = np.array([0.0, np.nan], dtype=np.float32)
        initial = retained.identity("a", values)[0]
        self.assertTrue(retained.identity("a", values.copy())[3])
        values[0] = -0.0
        signed = retained.identity("a", values)[0]
        self.assertNotEqual(signed, initial)
        values.view(np.uint32)[1] ^= 1
        self.assertNotEqual(retained.identity("a", values)[0], signed)

    def test_comparison_reads_beyond_first_bounded_scratch_chunk(self):
        values = np.zeros(3 * 1024 * 1024, dtype=np.uint8)
        retained = worker._DigestCache(values.nbytes)
        original = retained.identity("a", values)[0]
        self.assertTrue(retained.identity("a", values)[3])
        values[-1] = 1
        identity, hashed, compared, reused = retained.identity("a", values)
        self.assertNotEqual(identity, original)
        self.assertFalse(reused)
        self.assertEqual(hashed, values.nbytes)
        self.assertEqual(compared, values.nbytes)

    def test_shape_dtype_reload_dependency_and_budget_invalidation(self):
        retained = worker._DigestCache(8192)
        values = np.zeros(2048, dtype=np.float32)
        context = ("same.blend", {"same.blend": (1, 2, 3, 4)})
        retained.set_context(context)
        retained.identity("a", values)
        self.assertFalse(retained.identity("a", values.reshape(1024, 2))[3])
        self.assertFalse(retained.identity("a", values.view(np.int32).reshape(1024, 2))[3])
        retained.identity("a", values)
        retained.set_context(context)
        self.assertTrue(retained.identity("a", values)[3])
        retained.set_context(("same.blend", {"same.blend": (2, 2, 3, 4)}))
        self.assertFalse(retained.identity("a", values)[3])
        retained.clear()  # Also required for a forced reload at the same path/signature.
        self.assertEqual(retained.resident_bytes, 0)
        self.assertFalse(retained.identity("a", values)[3])
        retained.identity("b", values)
        self.assertEqual(list(retained.entries), ["b"])
        self.assertLessEqual(retained.resident_bytes, retained.target_bytes)
        self.assertFalse(retained.identity("oversize", np.zeros(4096, dtype=np.float32))[3])
        self.assertEqual(list(retained.entries), ["b"])
        self.assertFalse(retained.identity("small", np.zeros(4, dtype=np.float32))[3])
        self.assertNotIn("small", retained.entries)

    def test_shared_payload_is_attributed_once_and_baseline_has_same_sha(self):
        values = np.zeros(2048, dtype=np.float32)
        arrays = {"mesh_0/positions": values, "mesh_1/positions": values.copy()}
        manifest = dict(
            instances=[dict(mesh_key="mesh_0", source_name="Static"), dict(mesh_key="mesh_1", source_name="Rotating")]
        )

        class Connection:
            def send_bytes(self, data):
                pass

            def recv_bytes(self, limit):
                return json.dumps(dict(missing=[worker._array_id(values)])).encode()

        phases = {}
        worker._send_result(Connection(), manifest, arrays, phases)
        self.assertEqual(phases["hashed_bytes"], 2 * values.nbytes)
        self.assertEqual(phases["sent_bytes"], values.nbytes)
        self.assertEqual([row["sent_bytes"] for row in phases["arrays"]], [values.nbytes, 0])
        self.assertTrue(all(row["requested"] for row in phases["arrays"]))
        self.assertEqual({row["identity"] for row in phases["arrays"]}, {worker._array_id(values)})


class ByteBudgetTests(unittest.TestCase):
    def test_evicts_bytes_in_lru_order(self):
        assets = cache.ByteBudgetCache(10, storage=lambda value: value)
        assets["old"] = {"old": 7}
        assets["small"] = {"small": 2}
        assets["new"] = {"new": 6}
        self.assertEqual(assets.trim()["evicted"], ["old"])
        self.assertEqual(assets.snapshot()["resident_bytes"], 8)
        # Reading old/small entries renews them, rather than relying on frame count.
        assets["small"]
        assets["last"] = {"last": 4}
        self.assertEqual(assets.trim()["evicted"], ["new"])
        self.assertEqual(set(assets), {"small", "last"})

    def test_shared_storage_counts_once_until_last_owner_evicted(self):
        assets = cache.ByteBudgetCache(10, storage=lambda value: value)
        assets["frame1"] = {"topology": 6, "pose1": 2}
        assets["frame2"] = {"topology": 6, "pose2": 2}
        self.assertEqual(assets.snapshot()["resident_bytes"], 10)
        assets["frame3"] = {"topology": 6, "pose3": 2}
        self.assertEqual(assets.trim()["evicted"], ["frame1"])
        self.assertEqual(assets.snapshot()["resident_bytes"], 10)

    def test_multiple_live_rops_are_pinned_and_over_budget_is_visible(self):
        assets = cache.ByteBudgetCache(10, storage=lambda value: value, label="test cache")
        assets["large.blend frame 1"] = {"topology": 4, "pose1": 7}
        assets["large.blend frame 2"] = {"topology": 4, "pose2": 6}
        assets["inactive"] = {"unused": 9}
        assets.pin("rop1", ["large.blend frame 1"])
        assets.pin("rop2", ["large.blend frame 2"])
        report = assets.trim()
        self.assertEqual(report["evicted"], ["inactive"])
        self.assertEqual(report["active_bytes"], 17)
        self.assertEqual(report["active_over_budget_bytes"], 7)
        self.assertEqual(report["pinned_entries"], 2)
        self.assertEqual(report["largest_assets"][0]["asset"], "large.blend frame 1")
        self.assertTrue(report["largest_assets"][0]["active"])
        # Updating one ROP releases only its previous active set.
        assets.pin("rop1", [])
        self.assertEqual(assets.trim()["evicted"], ["large.blend frame 1"])
        assets.unpin("rop2")
        assets.target_bytes = 0
        self.assertEqual(assets.trim()["evicted"], ["large.blend frame 2"])

    def test_numpy_views_and_bytes_backings_count_allocation_once(self):
        root = np.zeros(100, dtype=np.float32)
        assets = cache.ByteBudgetCache(400)
        assets["a"] = root[::2]
        assets["b"] = root[25:75]
        self.assertEqual(assets.snapshot()["resident_bytes"], 400)
        data = bytes(40)
        assets["c"] = np.frombuffer(data, dtype=np.int32).reshape(2, 5)
        assets["d"] = np.frombuffer(data, dtype=np.int32)
        self.assertEqual(assets.snapshot()["resident_bytes"], 440)

    def test_iteration_and_values_do_not_invalidate_lru(self):
        assets = cache.ByteBudgetCache(3, size=len)
        assets["a"] = "12"
        assets["b"] = "34"
        self.assertEqual(list(assets.items()), [("a", "12"), ("b", "34")])
        self.assertEqual(list(assets.values()), ["12", "34"])
        self.assertEqual(assets.trim()["evicted"], ["a"])
        assets.clear()
        self.assertEqual(assets.snapshot()["resident_bytes"], 0)

    def test_invalid_accounting_is_rejected(self):
        assets = cache.ByteBudgetCache(1, storage=lambda value: {"invalid": -1})
        with self.assertRaises(ValueError):
            assets["asset"] = object()

    def test_accounting_runs_once_per_insertion_and_replacement(self):
        calls = []

        def storage(value):
            calls.append(value)
            return value

        assets = cache.ByteBudgetCache(10, storage=storage)
        assets["a"] = {"shared": 5}
        for unused in range(5):
            assets["a"]
            assets.snapshot()
            assets.pin("rop", ["a"])
            assets.trim()
        self.assertEqual(len(calls), 1)
        assets["a"] = {"new": 8}
        self.assertEqual(assets.snapshot()["resident_bytes"], 8)
        self.assertEqual(len(calls), 2)
        assets.unpin("rop")
        assets.target_bytes = 0
        assets.trim()
        self.assertEqual(assets.snapshot()["resident_bytes"], 0)


class TransportBudgetTests(unittest.TestCase):
    def test_aggregate_limit_and_exception_release(self):
        budget = cache.InFlightBudget(100)
        budget.reserve(60)
        with self.assertRaises(MemoryError):
            budget.reserve(41)
        self.assertEqual(budget.used_bytes, 60)
        with self.assertRaisesRegex(RuntimeError, "consumer"):
            with budget.lease(40):
                self.assertEqual(budget.used_bytes, 100)
                raise RuntimeError("consumer")
        self.assertEqual(budget.used_bytes, 60)
        self.assertEqual(budget.peak_bytes, 100)
        budget.release(60)
        self.assertEqual(budget.used_bytes, 0)
        with self.assertRaises(MemoryError):
            budget.reserve(101)
        with self.assertRaises(ValueError):
            budget.release(1)

    def test_config_is_finite_positive_and_bounded(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(cache.budget_bytes("BLEND_TEST_MB", 256), 256 * cache.MiB)
        with patch.dict(os.environ, {"BLEND_TEST_MB": "1.5"}):
            self.assertEqual(cache.budget_bytes("BLEND_TEST_MB", 256), 3 * cache.MiB // 2)
        for value in ("0", "-1", "nan", "inf", "bogus", "65537"):
            with self.subTest(value=value), patch.dict(os.environ, {"BLEND_TEST_MB": value}):
                with self.assertRaises(ValueError):
                    cache.budget_bytes("BLEND_TEST_MB", 256)


if __name__ == "__main__":
    unittest.main()
