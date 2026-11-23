# SPDX-License-Identifier: Apache-2.0
"""Mandatory tier entrypoint: real isolated hython -m pytest discovery."""

import argparse
import os
from pathlib import Path
import subprocess
import sys

from support.workers import console_options
from support.paths import ROOT, artifact_path

parser = argparse.ArgumentParser()
parser.add_argument("--native", action="store_true")
parser.add_argument("--markers", default="pure")
args = parser.parse_args()
selected_dependencies = os.environ.get("CYCLES_HOUDINI_TEST_DEPENDENCIES")
if not selected_dependencies:
    raise RuntimeError(
        "Select the isolated pytest target through scripts/run-tests.ps1 or CYCLES_HOUDINI_TEST_DEPENDENCIES"
    )
dependencies = Path(selected_dependencies)
if not (dependencies / "pytest/__init__.py").is_file():
    raise RuntimeError(
        "Missing pytest dependencies; run scripts/bootstrap-test-deps.ps1 -Download, "
        "or provision a saved offline wheelhouse with -Wheelhouse"
    )
env = dict(
    os.environ,
    PYTHONPATH=os.pathsep.join((str(ROOT / "houdini/python3.13libs"), str(dependencies))),
    PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
)
command = [
    sys.executable,
    "-m",
    "pytest",
    "-c",
    str(ROOT / "tests/pytest.ini"),
    str(ROOT / "tests/pytest_cases"),
    "-m",
    args.markers,
    "--junitxml",
    str(artifact_path("pytest", "results.xml")),
]
if args.native:
    command += ["--native"]
raise SystemExit(subprocess.call(command, env=env, **console_options()))
