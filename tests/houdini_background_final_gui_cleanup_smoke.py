# SPDX-License-Identifier: Apache-2.0
"""A failed GUI fixture must terminate its owned child and close Houdini without crashing."""

import json
from pathlib import Path
import runpy
import time
from PySide6 import QtCore

loaded = runpy.run_path(str(Path(__file__).with_name("houdini_background_final_gui_smoke.py")))
workflow = loaded["workflow"]
workflow.timer.stop()
original_quit = workflow.quit
expected = "Intentional GUI fixture abort while the background child is rendering"


def quit_after_expected_failure():
    if workflow.report["error"] == expected:
        workflow.report.update(
            result="passed",
            error=None,
            expected_fixture_error=expected,
            scope=__doc__,
            aborted_child_pid=workflow.job.pid,
        )
        workflow.path.write_text(json.dumps(workflow.report, indent=2), encoding="utf-8")
    original_quit()


workflow.quit = quit_after_expected_failure
workflow.tick()  # Launch through the same native ROP action.
began = time.monotonic()
monitor = QtCore.QTimer(loaded["hou"].qt.mainWindow())


def observe():
    try:
        assert time.monotonic() - began < 100
        status = workflow.job.status()
        assert status["state"] not in ("failed", "cancelled", "completed"), status
        if status["state"] == "rendering":
            monitor.stop()
            workflow.finish(expected)
    except BaseException:
        import traceback

        monitor.stop()
        workflow.finish(traceback.format_exc())


monitor.timeout.connect(observe)
monitor.start(50)
