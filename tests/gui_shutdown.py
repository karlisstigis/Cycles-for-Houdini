# SPDX-License-Identifier: Apache-2.0
"""Finish native GUI tests only after asynchronous renderer ownership retires."""

import json
from pathlib import Path
import time
import traceback

import hou
from PySide6 import QtCore

_retirements = set()


class _Shutdown:
    def __init__(self, path, report, sessions, code, actions):
        self.path, self.report = Path(path), report
        self.sessions, self.code = list(sessions), code
        self.started, self.finished = time.monotonic(), False
        self.timer = QtCore.QTimer(hou.qt.mainWindow())
        self.timer.setInterval(20)
        self.timer.timeout.connect(self.poll)
        _retirements.add(self)
        for action in actions:
            try:
                action()
            except (hou.ObjectWasDeleted, hou.OperationFailed):
                pass
            except Exception:
                self.fail_cleanup()
        self.report["result"] = "retiring"
        self.save()
        self.timer.start()

    def save(self):
        self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")

    def fail_cleanup(self):
        self.code = 1
        self.report.setdefault("cleanup_errors", []).append(traceback.format_exc())

    def poll(self):
        if self.finished:
            return
        try:
            states = [session.status() for session in self.sessions]
            self.report["resource_retirement"] = states
            pending = [state for state in states if state["state"] != "closed"]
            if pending:
                if time.monotonic() - self.started < 20:
                    return
                raise TimeoutError("Renderer retirement did not finish: " + str(pending))
            for state in states:
                assert state["device_allocation_bytes"] == state["owned_geometry_bytes"] == 0, state
                if "engine_shader_resources" in state:
                    assert state["engine_shader_resources"] == 0, state
        except Exception:
            self.fail_cleanup()
        self.finished = True
        self.timer.stop()
        self.report["result"] = "passed" if self.code == 0 else "failed"
        self.save()
        _retirements.discard(self)
        hou.ui.postEventCallback(lambda: hou.exit(self.code, suppress_save_prompt=True))


def retire_and_exit(path, report, sessions, code, close_actions=()):
    return _Shutdown(path, report, sessions, code, close_actions)
