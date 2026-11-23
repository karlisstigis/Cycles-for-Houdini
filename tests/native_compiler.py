# SPDX-License-Identifier: Apache-2.0
"""Locate an installed MSVC developer command prompt for standalone contracts."""

import os
from pathlib import Path
import subprocess


def find_vs_dev_cmd(explicit=None):
    if explicit is not None:
        candidate = Path(explicit).resolve()
        if not candidate.is_file():
            raise FileNotFoundError("MSVC developer command not found: " + str(candidate))
        return candidate
    if os.environ.get("VSINSTALLDIR"):
        candidate = Path(os.environ["VSINSTALLDIR"]) / "Common7/Tools/VsDevCmd.bat"
        if candidate.is_file():
            return candidate
    program_files = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    vswhere = program_files / "Microsoft Visual Studio/Installer/vswhere.exe"
    if vswhere.is_file():
        found = subprocess.run(
            [
                str(vswhere),
                "-latest",
                "-products",
                "*",
                "-requires",
                "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                "-property",
                "installationPath",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if found:
            candidate = Path(found) / "Common7/Tools/VsDevCmd.bat"
            if candidate.is_file():
                return candidate
    raise RuntimeError("MSVC C++ tools were not found; pass --vs-dev-cmd or install Visual Studio C++ Build Tools")
