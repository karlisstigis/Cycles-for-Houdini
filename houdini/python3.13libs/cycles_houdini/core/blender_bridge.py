# SPDX-License-Identifier: Apache-2.0
"""Optional .blend bridge policy and lazy, process-free executable discovery.

Importing this module does no discovery. A capability query or a requested
.blend asset discovers at most once per environment/configuration; ordinary
scene capture and frame updates never walk Blender installation directories.
"""

from functools import lru_cache
import os
from pathlib import Path
import re
import shutil
import sys


def _host():
    # Use an already loaded Houdini host without importing hou in pure tools.
    return sys.modules.get("hou")


def _environment_value(name, default=""):
    getenv = getattr(_host(), "getenv", None)
    value = getenv(name) if callable(getenv) else os.environ.get(name)
    return default if value is None else value


def _configured_executable(explicit=None):
    configured = _environment_value("CYCLES_HOUDINI_BLENDER") if explicit is None else str(explicit)
    host = _host()
    expand = getattr(getattr(host, "text", None), "expandString", None)
    if configured and callable(expand):
        configured = expand(configured)
    return os.path.expandvars(os.path.expanduser(configured))


def enabled():
    # Old packages did not record a component selection and retain the bridge.
    return _environment_value("CYCLES_HOUDINI_BLENDER_BRIDGE", "1").lower() not in ("0", "off", "false", "disabled")


@lru_cache(maxsize=8)
def _discover(explicit, search_path, program_files):
    if explicit:
        path = Path(explicit)
        return str(path) if path.is_file() else None
    executable = shutil.which("blender", path=search_path)
    if executable:
        return executable
    foundation = Path(program_files) / "Blender Foundation"
    installed = [path for path in foundation.glob("*/blender.exe") if path.is_file()]
    installed.sort(key=lambda path: tuple(map(int, re.findall(r"\d+", path.parent.name)[:3])), reverse=True)
    return str(installed[0]) if installed else None


def status(explicit=None):
    """Describe current usability without starting Blender or emitting diagnostics."""
    selected = enabled()
    configured = _configured_executable(explicit)
    executable = (
        _discover(
            configured,
            os.environ.get("PATH", ""),
            os.environ.get("ProgramFiles", "C:/Program Files"),
        )
        if selected
        else None
    )
    # Recheck only the chosen file, never rescan installations per frame.
    if executable and not Path(executable).is_file():
        executable = None
    return dict(enabled=selected, available=bool(executable), executable=executable, configured=str(configured))


def require_executable(explicit=None):
    """Report an actionable error only when an artist requests a .blend asset."""
    bridge = status(explicit)
    if not bridge["enabled"]:
        raise ValueError(
            "The Blender bridge is disabled for this installation. Run Cycles Setup again and select "
            "'Blender .blend asset bridge' to use this .blend asset. Native Houdini rendering needs no Blender."
        )
    executable = bridge["executable"]
    if not executable or not Path(executable).is_file():
        location = bridge["configured"] or "PATH or Program Files/Blender Foundation"
        raise ValueError(
            "Blender not found at " + location + ". Install Blender or set CYCLES_HOUDINI_BLENDER to its "
            "blender.exe, then restart Houdini to use this .blend asset."
        )
    return Path(executable)
