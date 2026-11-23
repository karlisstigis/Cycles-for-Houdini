# SPDX-License-Identifier: Apache-2.0
"""Passive Windows mouse observation in a process independent of Houdini/Qt.

Reads GetAsyncKeyState/GetCursorPos only. It never injects input, imports Houdini,
opens a renderer or edits scene state. The parent owns all output/stop files.
"""

import argparse
import ctypes
import json
from pathlib import Path
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--stop", type=Path, required=True)
    parser.add_argument("--started", type=float, required=True)
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    args = parser.parse_args()
    assert 20 <= args.timeout <= 1210
    user = ctypes.windll.user32
    kernel = ctypes.windll.kernel32
    open_process = kernel.OpenProcess
    open_process.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    open_process.restype = ctypes.c_void_p
    wait = kernel.WaitForSingleObject
    wait.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    wait.restype = ctypes.c_ulong
    close = kernel.CloseHandle
    close.argtypes = [ctypes.c_void_p]
    close.restype = ctypes.c_int
    parent = open_process(0x00100000, False, args.parent_pid)
    assert parent, "Cannot observe parent process lifetime"
    key = user.GetAsyncKeyState
    key.argtypes = [ctypes.c_int]
    key.restype = ctypes.c_short

    class Point(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    cursor = user.GetCursorPos
    cursor.argtypes = [ctypes.POINTER(Point)]
    cursor.restype = ctypes.c_int
    previous_buttons = []
    previous_cursor = None
    burst = 0
    previous = None
    args.events.parent.mkdir(parents=True, exist_ok=True)
    with args.events.open("x", encoding="utf-8", buffering=1) as stream:
        while time.monotonic() - args.started < args.timeout and not args.stop.exists() and wait(parent, 0) == 258:
            buttons = [name for code, name in ((1, "left"), (4, "middle"), (2, "right")) if key(code) & 0x8000]
            point = Point()
            assert cursor(ctypes.byref(point)), "GetCursorPos failed"
            position = [point.x, point.y]
            if buttons and not previous_buttons:
                burst += 1
            if previous is None or buttons != previous_buttons or (buttons and position != previous_cursor):
                previous = dict(
                    at=time.monotonic() - args.started, input_burst=burst, mouse_buttons=buttons, cursor=position
                )
                encoded = json.dumps(previous)
                stream.write(encoded + "\n")
                # The JSONL is canonical. The tiny state file is a best-effort
                # current snapshot; readers tolerate an in-progress rewrite.
                args.state.write_text(encoded, encoding="utf-8")
            previous_buttons = buttons
            previous_cursor = position
            time.sleep(0.005)
    close(parent)


if __name__ == "__main__":
    main()
