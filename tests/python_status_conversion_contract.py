# SPDX-License-Identifier: Apache-2.0
"""Compile and compare isolated legacy/new CPython conversion runners."""

from support.paths import artifacts_root

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
from native_compiler import find_vs_dev_cmd

parser = argparse.ArgumentParser()
parser.add_argument("--houdini-root", type=Path, required=True)
parser.add_argument("--artifacts", type=Path)
parser.add_argument("--vs-dev-cmd", type=Path)
args = parser.parse_args()
vs_dev_cmd = find_vs_dev_cmd(args.vs_dev_cmd)
root = Path(__file__).resolve().parents[1]
output = (args.artifacts or artifacts_root() / "tests/status-conversion-isolated").resolve()
output.mkdir(parents=True, exist_ok=True)
exe = output / "status-contract.exe"
command = output / "compile.cmd"
host = args.houdini_root.resolve()
command.write_text(f'''@echo off
call "{vs_dev_cmd}" -no_logo -arch=x64 -host_arch=x64 >nul
if errorlevel 1 exit /b %errorlevel%
cl /nologo /std:c++20 /EHsc /MD /utf-8 /I"{root / "include"}" /I"{root / "include/third_party"}" /I"{host / "toolkit/include/python3.13"}" "{root / "tests/python_status_conversion_contract.cpp"}" "{root / "src/houdini/python_status.cpp"}" "{root / "src/core/fixture.cpp"}" /Fe"{exe}" /Fo"{output.as_posix()}/" /link /LIBPATH:"{host / "python313/libs"}" python313.lib
''')
compiled = subprocess.run(["cmd", "/c", str(command)], capture_output=True, text=True)
(output / "compile.log").write_text(compiled.stdout + compiled.stderr)
assert compiled.returncode == 0, compiled.stdout + compiled.stderr
env = dict(os.environ, PYTHONHOME=str(host / "python313"))
env["PATH"] = str(host / "bin") + os.pathsep + env["PATH"]
rows = []
for mode in ("baseline", "candidate"):
    run = subprocess.run([str(exe), mode], capture_output=True, text=True, env=env)
    (output / (mode + ".json")).write_text(run.stdout)
    (output / (mode + ".stderr")).write_text(run.stderr)
    assert run.returncode == 0, (mode, run.returncode, run.stderr)
    rows.append(json.loads(run.stdout))
assert rows[0] == rows[1], "Legacy/new typed fields, optional fields or exceptions differ"
report = dict(
    result="passed",
    typed_fixtures=len(rows[0]),
    polls_per_process=2000,
    isolated_processes=2,
    legacy_fixture_sha256=hashlib.sha256((root / "tests/fixtures/python_status_legacy.inc").read_bytes()).hexdigest(),
)
(output / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
