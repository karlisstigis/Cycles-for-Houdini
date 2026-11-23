# SPDX-License-Identifier: Apache-2.0
"""Compile shared-buffer/material-variant contracts against the compact old oracle."""

from support.paths import artifacts_root

import argparse
import hashlib
import json
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
output = (args.artifacts or artifacts_root() / "tests/external-binding").resolve()
output.mkdir(parents=True, exist_ok=True)
host = args.houdini_root.resolve()
exe = output / "material-contract.exe"
command = output / "compile.cmd"
command.write_text(f'''@echo off
call "{vs_dev_cmd}" -no_logo -arch=x64 -host_arch=x64 >nul
if errorlevel 1 exit /b %errorlevel%
cl /nologo /std:c++20 /EHsc /MD /DAMD64 /D_USE_MATH_DEFINES /DSESI_LITTLE_ENDIAN /DSIZEOF_VOID_P=8 /DWIN32 /D_WIN32_WINNT=0x0A00 /I"{root / "include"}" /I"{host / "toolkit/include"}" "{root / "tests/native_external_binding_contract.cpp"}" "{root / "src/houdini/native_external_binding.cpp"}" /Fe"{exe}" /Fo"{output.as_posix()}/" /link /LIBPATH:"{host / "custom/houdini/dsolib"}"
''')
compiled = subprocess.run(["cmd", "/c", str(command)], capture_output=True, text=True)
(output / "compile.log").write_text(compiled.stdout + compiled.stderr)
assert compiled.returncode == 0, compiled.stdout + compiled.stderr
run = subprocess.run([str(exe)], capture_output=True, text=True)
(output / "candidate.txt").write_text(run.stdout)
assert run.returncode == 0, run.stdout + run.stderr
expected = (root / "tests/fixtures/native_external_binding_expected.txt").read_text()
assert run.stdout == expected, "Binding key/revision/reference/diagnostic/resource oracle changed"
report = dict(
    result="passed",
    rounds=2,
    variants_per_round=8,
    oracle_sha256=hashlib.sha256(expected.encode()).hexdigest(),
    checked="Two palettes; material-only edit; field priority; styles; diagnostics; warm reuse; shared source/position/topology/UV/attribute storage",
)
(output / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
