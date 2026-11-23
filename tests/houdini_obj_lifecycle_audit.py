# SPDX-License-Identifier: Apache-2.0
"""Live OBJ viewer: equal-area resize, AOV replacement/removal and Stop/restart."""

from support.paths import artifacts_root

import argparse
import json
import os
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import hou
from PySide6 import QtWidgets
from cycles_houdini import passes, registry
from cycles_houdini.device_selection import set_devices
from cycles_houdini.panel import RenderPanel

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
args = parser.parse_args()
dest = artifacts_root() / "obj-audit-20261028"
dest.mkdir(parents=True, exist_ok=True)
report = dict(result="running", device=args.device, checks=[])
app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
obj = hou.node("/obj").createNode("geo", "lifecycle_subject", run_init_scripts=False)
sop = obj.createNode("box")
camera = hou.node("/obj").createNode("cam", "lifecycle_camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=4))
rop = hou.node("/out").createNode("cycles_ipr", "lifecycle_render")
rop.setParms(
    dict(
        source_mode="sop",
        soppath=sop.path(),
        camera=camera.path(),
        resx=96,
        resy=64,
        resolution_percent=100,
        ipr_resolution_percent=100,
        viewer_interactive=1,
        ipr_samples=1048576,
        ipr_adaptive_sampling=0,
        ipr_use_denoise=0,
        allowmotionblur=0,
        material_kind="emission",
    )
)
set_devices(rop, args.device)
index = passes.add(rop, "aov_color", source="AuditTint", enable=True)
panel = RenderPanel()
panel.auto.setChecked(False)
panel.set_rop(rop)
panel.show()


def until(label, predicate, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        panel.poll()
        if panel.entry:
            status = panel.entry.session.status()
            assert not panel.entry.error and status["state"] != "failed", (label, panel.entry.error, status)
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError((label, panel.info.text(), panel.entry.session.status()))


def displayed():
    if panel.entry is None:
        return False
    status = panel.entry.session.status()
    info = panel.entry.session.frame_info()
    return bool(
        info
        and info.get("current_scene")
        and not status.get("pending_update")
        and not status.get("capture_pending")
        and panel._live_generation == status["generation"]
        and panel._live_frame_metadata.get("render_revision") == status["render_revision"]
        and not panel._settings_dirty
    )


def render(label):
    panel.render()
    until(label, displayed)
    assert panel.entry.session.status()["state"] == "rendering"
    report["checks"].append(label)


try:
    render("Initial live accumulation")
    entry = panel.entry
    initial_session = entry.session
    initial_pid = initial_session.status()["renderer_pid"]
    # A different aspect ratio with exactly the same number of pixels must
    # recreate both backend and viewer dimensions, not reuse a byte-size match.
    rop.setParms(dict(resx=64, resy=96))
    render("Equal-area resize while accumulating")
    assert panel.entry is entry and entry.session is not initial_session
    assert entry.session.status()["renderer_pid"] != initial_pid
    assert panel.canvas.full_size == (64, 96), panel.canvas.full_size
    assert (panel._live_image.width(), panel._live_image.height()) == (64, 96)
    until("Old session retires", lambda: initial_session.status()["state"] == "closed")
    pid = entry.session.status()["renderer_pid"]

    aov = next(
        panel.pass_menu.itemData(i)
        for i in range(panel.pass_menu.count())
        if "AuditTint" in panel.pass_menu.itemData(i)
    )
    panel.pass_menu.setCurrentIndex(panel.pass_menu.findData(aov))
    until("Selected AOV arrives", lambda: aov in (entry.session.frame_info() or {}).get("passes", []))
    rop.parm("pass_type" + str(index)).set("aov_value")
    render("Selected custom AOV changes from color to scalar during accumulation")
    until("Scalar AOV arrives", lambda: aov in (entry.session.frame_info() or {}).get("passes", []))
    assert len(entry.session.frame_info()["pass_metadata"][aov]["channels"]) == 1
    rop.parm("pass_count").removeMultiParmInstance(index - 1)
    render("Selected custom AOV removed during accumulation")
    assert panel.pass_menu.findData(aov) == -1
    assert panel.pass_menu.currentData() == "combined"
    until("Beauty replaces removed AOV", lambda: "combined" in (entry.session.frame_info() or {}).get("passes", []))
    assert entry.session.status()["renderer_pid"] == pid
    panel.stop_button.click()
    until("Stop cancels accumulation", lambda: entry.session.status()["state"] not in ("rendering", "paused"))
    render("Restart after Stop")
    assert entry.session.status()["renderer_pid"] == pid
    report["result"] = "passed"
finally:
    panel.dispose()
    panel.close()
    registry.close_all()
    (dest / "obj-lifecycle.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
