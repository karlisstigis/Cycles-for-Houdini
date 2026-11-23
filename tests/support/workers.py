# SPDX-License-Identifier: Apache-2.0
"""Bounded process execution with complete process-tree retirement."""

import os
from pathlib import Path
import subprocess
import sys


def console_options():
    """Keep Windows console children hidden even when their owner is pythonw."""
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def ordinary_python():
    """Use SDK Python for pure children: hython cannot bootstrap under ``-I``."""
    executable = Path(sys.executable)
    if executable.stem.casefold() == "hython":
        executable = executable.parent.parent / f"python{sys.version_info.major}{sys.version_info.minor}" / "python.exe"
    if not executable.is_file():
        raise RuntimeError(f"Ordinary Python interpreter is missing: {executable}")
    return executable


def pure_python_environment(pythonpath=()):
    """Keep pure subprocesses independent of the parent's Houdini package state."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("CYCLES_HOUDINI_", "HOUDINI_"))
        and key not in ("PYTHONHOME", "PYTHONPATH", "HFS", "HHP", "HH", "HB")
    }
    env["PYTHONNOUSERSITE"] = "1"
    if pythonpath:
        env["PYTHONPATH"] = os.pathsep.join(map(str, pythonpath))
    return env


def terminate_process(process):
    """Attempt tree retirement, then bound direct-child kill and reap even if it fails."""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            import signal

            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, subprocess.TimeoutExpired):
        pass
    finally:
        try:
            if process.poll() is None:
                process.kill()
        finally:
            process.wait(timeout=15)


def run_process(command, *, timeout, destination, env=None, cwd=None):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with (
        (destination / "stdout.log").open("w", encoding="utf-8") as stdout,
        (destination / "stderr.log").open("w", encoding="utf-8") as stderr,
    ):
        options = {"start_new_session": True} if os.name != "nt" else console_options()
        # Popen.__exit__ waits without a timeout. Retire and reap explicitly so
        # a failing tree helper cannot leave a bounded lifetime probe hanging.
        process = subprocess.Popen(command, stdout=stdout, stderr=stderr, env=env, cwd=cwd, **options)
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            terminate_process(process)
            raise
    assert code == 0, f"{command[0]} exited {code}; see {destination / 'stderr.log'}"
