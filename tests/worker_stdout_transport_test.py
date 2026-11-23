# SPDX-License-Identifier: Apache-2.0
"""Real executable keeps CRT/Win32 library noise out of the binary control pipe."""

import argparse
import os
from pathlib import Path
import struct
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument("--worker", default=None)
args = parser.parse_args()
worker = Path(args.worker) if args.worker else Path(os.environ["CYCLES_HOUDINI_HOST_ROOT"]) / "bin/ch_worker.exe"
result = subprocess.run(
    [str(worker), "--transport-smoke"],
    capture_output=True,
    timeout=20,
    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
)
assert result.returncode == 0, (result.returncode, result.stderr.decode(errors="replace"))
expected = struct.pack("<4sHHQQI", b"CHWP", 1, 3 | 0x8000, 41, 0, 0)
assert result.stdout == expected, (result.stdout.hex(), result.stdout.decode(errors="replace"))
for marker in (b"CH_WORKER_CRT_STDOUT_NOISE", b"CH_WORKER_WIN32_STDOUT_NOISE"):
    assert marker in result.stderr, (marker, result.stderr)
print("Passed: real worker CRT and Win32 stdout noise is logged; binary protocol stream remains exact")
