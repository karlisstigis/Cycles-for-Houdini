# SPDX-License-Identifier: Apache-2.0
"""Measure the same native viewport geometry edits without a renderer."""

from support.paths import artifacts_root

import json, os, time, traceback
from pathlib import Path
import hou
from PySide6 import QtCore

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
hou.hipFile.load(
    str(artifacts_root() / "examples/sop-fx-courtyard/sop-fx-courtyard-current.hiplc"), suppress_save_prompt=True
)
viewer = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.SceneViewer)
viewer.curViewport().setCamera(hou.node("/obj/courtyard_camera"))
hero = hou.node("/obj/fx_courtyard/TWISTED_TURBINE")
viewer.setPwd(hero.parent())
hero.setCurrent(True, clear_all_selected=True)
out = hou.node("/obj/fx_courtyard/RENDER_COURTYARD")
report = dict(
    result="running", edits=[], heartbeat_gaps=[], scope="Native viewport with no Cycles session or image display"
)
last = time.monotonic()
started = last
index = 0


def beat():
    global last
    now = time.monotonic()
    report["heartbeat_gaps"].append(now - last)
    last = now


timer = QtCore.QTimer(hou.qt.mainWindow())
timer.setInterval(16)
timer.timeout.connect(beat)
timer.start()


def tick():
    global index
    if time.monotonic() - started < 2:
        return
    try:
        if index < 20:
            begin = time.monotonic()
            hero.parm("twist").set(2.6 + index * 0.02)
            if index % 5 == 0:
                hero.parm("blades").set(9 + (index // 5) % 2)
            out.geometry()
            report["edits"].append(time.monotonic() - begin)
            index += 1
            return
        report["result"] = "passed"
    except Exception:
        report["result"] = "failed"
        report["error"] = traceback.format_exc()
    hou.ui.removeEventLoopCallback(tick)
    timer.stop()
    values = sorted(report["heartbeat_gaps"])
    report["heartbeat_summary"] = dict(max_seconds=max(values), p95_seconds=values[int(0.95 * (len(values) - 1))])
    destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
    viewer.close()
    hou.exit(0 if report["result"] == "passed" else 1, suppress_save_prompt=True)


hou.ui.addEventLoopCallback(tick)
