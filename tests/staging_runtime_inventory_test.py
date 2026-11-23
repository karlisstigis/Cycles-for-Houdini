# SPDX-License-Identifier: Apache-2.0
"""Run the real staging inventory commands beneath an enclosing Git ignore."""

from support.paths import artifacts_root

import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RuntimeInventoryTest(unittest.TestCase):
    def test_source_copy_and_frozen_host_inventory_include_ignored_assets(self):
        with tempfile.TemporaryDirectory(dir=artifacts_root()) as directory:
            parent = Path(directory)
            subprocess.run(["git", "init", "-q", str(parent)], check=True)
            (parent / ".gitignore").write_bytes(b"*.png\n*.hip*\n*.exr\n*.pdb\n/build/\n")
            source = parent / "houdini/config"
            host = parent / "build/hosts/fixture"
            expected = {
                "branding/installer.ico",
                "branding/installer-welcome.png",
                "branding/installer-header.png",
                "branding/cycles-houdini-logo.png",
                "branding/cycles-houdini-logo.svg",
                "scene.hiplc",
                "texture.exr",
                "normal.py",
            }
            for tree in (source, host):
                for name in expected | {"__pycache__/ignored.pyc", "nested/__pycache__/ignored.pyc"}:
                    path = tree / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"fixture asset\r\n")
            for name in ("bin/ch_worker.exe", "dso/_cycles_houdini.dll"):
                path = host / name
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(b"fixture asset\r\n")
            code = (
                '$ErrorActionPreference="Stop"; . $env:CH_INVENTORY_SCRIPT; '
                "@{source=@(Get-StagingRuntimeFiles $env:CH_INVENTORY_SOURCE);"
                "host=@(Get-StagingRuntimeFiles $env:CH_INVENTORY_HOST)} | ConvertTo-Json -Compress"
            )
            env = dict(
                os.environ,
                CH_INVENTORY_SOURCE=str(source),
                CH_INVENTORY_HOST=str(host),
                CH_INVENTORY_SCRIPT=str(ROOT / "scripts/staging-runtime-files.ps1"),
            )
            output = subprocess.check_output(
                ["pwsh", "-NoProfile", "-EncodedCommand", base64.b64encode(code.encode("utf-16le")).decode()], env=env
            )
            records = json.loads(output.decode("utf-8-sig"))
            for label, tree in (("source", source), ("host", host)):
                wanted = expected if label == "source" else expected | {"bin/ch_worker.exe", "dso/_cycles_houdini.dll"}
                self.assertEqual({Path(path).relative_to(tree).as_posix() for path in records[label]}, wanted)
                self.assertTrue(all(Path(path).read_bytes() == b"fixture asset\r\n" for path in records[label]))


if __name__ == "__main__":
    unittest.main()
