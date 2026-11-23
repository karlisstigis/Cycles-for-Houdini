# SPDX-License-Identifier: Apache-2.0
"""Existing assert-script contracts gain isolated pytest discovery incrementally."""

from pathlib import Path

import pytest

from support.paths import ROOT, artifact_path, artifacts_root
from support.workers import ordinary_python, pure_python_environment, run_process

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


@pytest.mark.parametrize(
    "script",
    [
        "queued_rejected_navigation_test.py",
        "native_test_runtime_test.py",
        "material_override_expression_test.py",
        "material_override_evaluation_test.py",
        "camera_dof_motion_source_test.py",
        "dlss_host_policy_test.py",
        "local_final_async_rollback_test.py",
        "local_final_ownership_edges_test.py",
        "viewer_memory_idle_test.py",
        "startup_dependency_guard_test.py",
        "workstation_ci_test.py",
        "dlss_menu_test.py",
        "exchange_watchdog_test.py",
        "image_iris_controls_source_test.py",
        "local_final_lifecycle_test.py",
        "local_final_suspension_test.py",
        "material_authoring_test.py",
        "native_scene_definition_test.py",
        "offline_archive_test.py",
        "offline_scripts_test.py",
        "patch_test_matrix_test.py",
        "engine_fork_test.py",
        "source_history_test.py",
        "subdivision_count_wrapper_test.py",
        "system_input_evidence_test.py",
        "test_rop_help_source.py",
        "viewer_priority_delivery_test.py",
        "worker_scheduling_test.py",
        "build_layout_test.py",
        "output_sequence_paths_test.py",
    ],
)
def test_existing_pure_contract(script):
    # A legacy fake-host module cannot contaminate other collected contracts.
    env = pure_python_environment()
    env["CYCLES_HOUDINI_TEST_ARTIFACTS"] = str(artifacts_root())
    run_process(
        [str(ordinary_python()), str(ROOT / "tests" / script)],
        timeout=90,
        destination=artifact_path("pytest", Path(script).stem, "output").parent,
        env=env,
    )
