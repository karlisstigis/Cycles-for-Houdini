# SPDX-License-Identifier: Apache-2.0
"""Pure recipe qualification: explicit scopes, concrete required args and bounded defaults."""

from collections import Counter
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("matrix", ROOT / "scripts/patch-test-matrix.py")
matrix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(matrix)


class MatrixTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.build = Path(self.temporary.name)
        self.host = self.build / "hosts/current"
        (self.host / "resources").mkdir(parents=True)
        (self.build / "build-manifest.json").write_text(
            json.dumps(dict(host_sdk=dict(houdini="22.0.466"), resource_root="hosts/current/resources"))
        )
        self.gpu = "CUDA_NVIDIA GeForce RTX 4090_0000:03:00_OptiX"
        fixtures = json.loads((ROOT / "patches/contracts.json").read_text())["fixtures"]
        self.iris_assigned = "tests/core_image_iris_smoke.py" in fixtures.values()

    def plan(self, **kwargs):
        return matrix.plan(self.build, gpu_device=self.gpu, label="unit-plan", **kwargs)

    def test_inventory_has_all_primary_related_contracts_without_isolation_claim(self):
        recipes = matrix.recipes()
        self.assertEqual(len(recipes), 62 + self.iris_assigned)
        self.assertEqual(
            sorted(number for recipe in recipes for number in recipe["patches"]),
            sorted(
                int(number)
                for number, fixture in json.loads((ROOT / "patches/contracts.json").read_text())["fixtures"].items()
                if fixture
            ),
        )
        self.assertEqual(
            Counter(recipe["backend_policy"] for recipe in recipes),
            dict(
                cpu_and_default_gpu=11,
                selected_device=41 + self.iris_assigned,
                native_host_only=5,
                pure_host=2,
                gpu_only=3,
            ),
        )
        self.assertEqual(sum(recipe["gui"] for recipe in recipes), 4)
        self.assertTrue(all("not patch isolation" in recipe["qualification"] for recipe in recipes))

    def test_default_is_bounded_and_host_math_not_duplicated_as_gpu(self):
        result = self.plan()
        self.assertEqual(result["selected_fixture_count"], 3)
        self.assertEqual(len(result["cases"]), 6)
        boundary = [case for case in result["cases"] if case["test"] == "core_subdivision_count_overflow_smoke.py"]
        self.assertEqual(len(boundary), 1)
        self.assertIn("no GPU rendering", boundary[0]["actual_device_scope"][0])
        self.assertTrue(
            all(case.get("qualification", "planned_not_executed") == "planned_not_executed" for case in result["cases"])
        )

    def test_full_plan_preserves_real_backend_policies_and_pure_commands(self):
        result = self.plan(all_primary=True)
        self.assertEqual(len(result["cases"]), 104 + 2 * self.iris_assigned)
        self.assertEqual(result["supplemental_fixture_count"], 2)
        self.assertEqual(len(result["pure_commands"]), 2)
        self.assertEqual(sum(case["gui"] for case in result["cases"]), 8)
        self.assertEqual(sum(case.get("backend_policy") == "cpu_and_default_gpu" for case in result["cases"]), 12)
        self.assertEqual(sum(case.get("backend_policy") == "native_host_only" for case in result["cases"]), 5)
        self.assertNotIn("sdk_dependency_policy_test.py", [case["test"] for case in result["cases"]])

    def test_required_current_runtime_arguments_are_concrete(self):
        result = self.plan(all_primary=True)
        for case in result["cases"]:
            if case["test"] == "houdini_release_runtime_smoke.py":
                self.assertIn(str(self.host), case["arguments"])
                self.assertIn("22.0.466", case["arguments"])
            if case["test"] in ("houdini_rls_hair_smoke.py", "houdini_selective_rls_smoke.py"):
                self.assertIn("--label", case["arguments"])
                self.assertIn("--output", case["arguments"])
                self.assertNotIn("--baseline", case["arguments"])
            if not case["gui"] and case["test"] != "core_gpu_concurrency_smoke.py":
                self.assertTrue(case["report"].startswith("artifacts/"))
                self.assertNotIn("{", case["report"])
            if case["test"] == "core_gpu_concurrency_smoke.py":
                self.assertNotIn("report", case)  # stdout JSON contract; no image output
            if case["test"] in (
                "houdini_dlss_worker_smoke.py",
                "houdini_temporal_sample_count_smoke.py",
                "houdini_dlss_sample_count_smoke.py",
            ):
                self.assertNotEqual(case["device"], "CPU")
                self.assertIn("--report", case["arguments"])
            if case["test"] == "houdini_spectral_nested_smoke.py":
                self.assertIn("--output", case["arguments"])
            if case["test"] == "houdini_blender53_gsplats_smoke.py":
                self.assertIn("--out", case["arguments"])
            self.assertTrue(
                all(
                    not any(token in argument for token in ("{host}", "{output}", "{backend}", "{device}"))
                    for argument in case.get("arguments", [])
                )
            )

    def test_supported_second_gpu_does_not_relabel_coupled_default(self):
        other = "CUDA_NVIDIA GeForce RTX 3080_0000:04:00_OptiX"
        result = matrix.plan(
            self.build, gpu_device=other, default_gpu_device=self.gpu, all_primary=True, label="unit-two-gpus"
        )
        self.assertEqual(len(result["cases"]), 105 + 2 * self.iris_assigned)
        preflight = [case for case in result["cases"] if case["test"] == "houdini_patch_matrix_devices_smoke.py"]
        self.assertEqual({case["device"] for case in preflight}, {self.gpu, other})
        for case in result["cases"]:
            if case.get("backend_policy") == "cpu_and_default_gpu":
                self.assertEqual(case["actual_device_scope"], ["CPU", self.gpu])
        material = next(
            case
            for case in result["cases"]
            if case["test"] == "houdini_light_material_smoke.py" and case["device"] == other
        )
        self.assertNotIn(":", Path(material["report"]).name)

    def test_missing_gpu_and_unknown_selections_fail_before_dispatch(self):
        with self.assertRaisesRegex(ValueError, "exact --gpu-device"):
            matrix.plan(self.build, label="unit-no-gpu")
        with self.assertRaisesRegex(ValueError, "Unknown primary fixtures"):
            self.plan(selection=["unknown.py"])
        with self.assertRaisesRegex(ValueError, "exact --gpu-device"):
            matrix.plan(self.build, backends=["CPU"], selection=["houdini_points_smoke.py"], label="unit-coupled")
        result = matrix.plan(self.build, backends=["CPU"], label="unit-cpu")
        self.assertEqual(len(result["cases"]), 4)

    def test_new_followup_recipes_preserve_gpu_and_compiler_scopes(self):
        temporal = self.plan(selection=["houdini_temporal_sample_count_smoke.py"])
        cases = [case for case in temporal["cases"] if case["test"] == "houdini_temporal_sample_count_smoke.py"]
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["device"], self.gpu)
        self.assertEqual(cases[0]["patches"], [90])
        self.assertEqual(cases[0]["arguments"][0], "--report")
        host = self.plan(selection=["multi_device_range_fault_test.py"], backends=["CPU"])
        self.assertFalse(any(case["test"] == "multi_device_range_fault_test.py" for case in host["cases"]))
        self.assertEqual(len(host["pure_commands"]), 1)
        command = host["pure_commands"][0]
        self.assertEqual(Path(command[1]).name, "multi_device_range_fault_test.py")
        self.assertEqual(
            command[2:], ["--output", str(ROOT / "artifacts/patch-matrix/unit-plan/multi-device-range-fault")]
        )

    def test_gui_subdivision_rebuilds_its_own_current_prerequisites(self):
        result = self.plan(selection=["houdini_subdivision_ipr_smoke.py"])
        names = [case["test"] for case in result["cases"]]
        self.assertEqual(result["selected_fixture_count"], 1)
        self.assertLess(names.index("houdini_subdivision_smoke.py"), names.index("core_subdivision_smoke.py"))
        self.assertLess(names.index("core_subdivision_smoke.py"), names.index("houdini_subdivision_ipr_smoke.py"))

    def test_subdivision_topology_cache_runs_on_the_selected_devices_with_separate_reports(self):
        name = "houdini_subdivision_topology_cache_smoke.py"
        result = self.plan(selection=[name])
        self.assertEqual(result["selected_fixture_count"], 1)
        self.assertEqual(result["supplemental_fixture_count"], 0)
        self.assertEqual(result["pure_commands"], [])
        self.assertEqual(len(result["cases"]), 3)
        cases = [case for case in result["cases"] if case["test"] == name]
        self.assertEqual([case["device"] for case in cases], ["CPU", self.gpu])
        for case, backend in zip(cases, ("CPU", "OPTIX"), strict=True):
            report = f"artifacts/patch-matrix/unit-plan/subdivision-topology-cache-{backend}/report.json"
            self.assertEqual(case["backend_policy"], "selected_device")
            self.assertEqual(case["actual_device_scope"], [case["device"]])
            self.assertEqual(case["execution"], "worker")
            self.assertFalse(case["gui"])
            self.assertEqual(case["patches"], [121])
            self.assertEqual(case["report"], report)
            self.assertEqual(case["arguments"], ["--report", str(ROOT / report)])

    def test_image_iris_keeps_each_device_report_and_render_directory_separate(self):
        name = "core_image_iris_smoke.py"
        source = self.build / "source"
        for directory in ("patches", "tests", "scripts"):
            (source / directory).mkdir(parents=True)
        (source / "patches/contracts.json").write_text(json.dumps({"fixtures": {"119": "tests/" + name}}))
        for relative in (
            Path("tests") / name,
            Path("scripts/run-tests.ps1"),
            Path("tests/core_subdivision_smoke.py"),
            Path("tests/houdini_texture_smoke.py"),
        ):
            (source / relative).write_bytes((ROOT / relative).read_bytes())
        with patch.object(matrix, "ROOT", source):
            cases = [case for case in self.plan(selection=[name])["cases"] if case["test"] == name]
        self.assertEqual([case["device"] for case in cases], ["CPU", self.gpu])
        self.assertEqual(len({Path(case["report"]).parent for case in cases}), 2)
        for case in cases:
            self.assertEqual(case["patches"], [119])
            self.assertEqual(case["backend_policy"], "selected_device")
            self.assertEqual(case["arguments"], ["--report", str(source / case["report"])])


if __name__ == "__main__":
    unittest.main()
