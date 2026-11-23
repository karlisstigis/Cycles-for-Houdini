# SPDX-License-Identifier: Apache-2.0
"""The host boundary wrapper supplies the harness's mandatory launch context."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import native_test_runtime


class WrapperTest(unittest.TestCase):
    def test_verified_resources_and_explicit_cpu_reach_the_actual_contract_route(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "custom-build/bin/ch_harness.exe"
            host = root / "selected-installed-host"
            report = root / "explicit-output/report.json"

            def child(command, **kwargs):
                self.assertEqual(
                    command,
                    [
                        str(executable),
                        "--resources",
                        str(host / "resources"),
                        "--device",
                        "CPU",
                        "--subdivision-shading-contracts",
                    ],
                )
                self.assertEqual(kwargs["timeout"], 90)
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps(dict(result="passed", subdivision_count_overflow_checks=32)),
                    stderr="",
                )

            with (
                patch.object(native_test_runtime, "runtime_executable", return_value=executable),
                patch.object(native_test_runtime, "runtime_context", return_value=dict(host=str(host))),
                patch("subprocess.run", side_effect=child) as dispatch,
                patch.object(
                    sys,
                    "argv",
                    ["core_subdivision_count_overflow_smoke.py", "--device", "CPU", "--report", str(report)],
                ),
                redirect_stdout(io.StringIO()),
            ):
                runpy.run_path(
                    str(Path(__file__).with_name("core_subdivision_count_overflow_smoke.py")), run_name="__main__"
                )
            dispatch.assert_called_once()
            self.assertEqual(json.loads(report.read_text())["result"], "passed")


if __name__ == "__main__":
    unittest.main()
