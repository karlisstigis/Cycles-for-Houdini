# SPDX-License-Identifier: Apache-2.0
"""Real PowerShell context guards with disposable payloads and mocked launches."""

import os
import shutil

import pytest

from support.paths import ROOT, artifact_path
from support.workers import run_process

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


@pytest.mark.parametrize(
    "script",
    [
        "archive_test_manifest_test.ps1",
        "installer_refresh_members_test.ps1",
        "native_test_context_test.ps1",
        "run_harness_context_test.ps1",
        "run_tests_selection_test.ps1",
        "test_runner_routing_test.ps1",
        "run_hython_log_routing_test.ps1",
    ],
)
def test_powershell_context_guard(script):
    executable = shutil.which("pwsh")
    assert executable, "PowerShell 7 is required for the Windows context guard contracts"
    destination = artifact_path("pytest-powershell", script.removesuffix(".ps1"), "output").parent
    run_process(
        [executable, "-NoLogo", "-NoProfile", "-File", str(ROOT / "tests" / script)],
        timeout=90,
        destination=destination,
        env=dict(os.environ),
    )
