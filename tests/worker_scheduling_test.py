# SPDX-License-Identifier: Apache-2.0
"""Unsupported scheduling is optional; successful requests affect only our child."""

import ctypes
from pathlib import Path
import runpy

request = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "houdini/python3.13libs/cycles_houdini/ipr/worker_scheduling.py")
)["prioritize_interaction"]


class Process:
    _handle = 12345


class Call:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


class Library:
    def __init__(self, fail=False):
        self.priority = 2
        self.handles = []
        self.fail = fail
        self.D3DKMTGetProcessSchedulingPriorityClass = Call(self.get)
        self.D3DKMTSetProcessSchedulingPriorityClass = Call(self.change)

    def get(self, handle, value):
        self.handles.append(handle.value)
        value._obj.value = self.priority
        return 0

    def change(self, handle, priority):
        self.handles.append(handle.value)
        if self.fail:
            return -1073741790
        self.priority = priority
        return 0


original = ctypes.WinDLL
try:
    library = Library()
    ctypes.WinDLL = lambda *args, **kwargs: library
    result = request(Process())
    assert result["applied"] and result["before"] == 2 and result["after"] == 1, result
    assert library.handles == [12345] * 3
    library = Library(fail=True)
    result = request(Process())
    assert not result["applied"] and result["after"] == 2 and result["set_status"] < 0, result

    def unavailable(*args, **kwargs):
        raise OSError("Scheduling API unavailable")

    ctypes.WinDLL = unavailable
    result = request(Process())
    assert not result["applied"] and "unavailable" in result["reason"], result
finally:
    ctypes.WinDLL = original
print("Passed: owned child priority, driver refusal and unavailable API fallback")
