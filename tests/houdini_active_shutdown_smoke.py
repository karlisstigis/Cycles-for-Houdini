# SPDX-License-Identifier: Apache-2.0
"""Exit Houdini with one native viewer and an active capture/render."""

from cycles_houdini.device_selection import set_devices as set_rop_devices
import atexit
import json
import os
from pathlib import Path
import time
import traceback

import hou

report = dict(result="starting", host_pid=os.getpid(), device=os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
entry = None


def save():
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")


def final_check():
    try:
        assert entry is not None
        status = entry.session.status()
        assert status["state"] == "closed" and status["device_allocation_bytes"] == 0, status
        report.update(
            result="passed", interpreter_shutdown="renderer closed before module teardown", final_status=status
        )
    except Exception:
        report.update(result="failed", error=traceback.format_exc())
    save()


# The integration's atexit callback is registered later and runs before this
# independent check. No host UI traversal is performed during interpreter exit.
atexit.register(final_check)
from cycles_houdini import registry, native_scene, _native

geometry = hou.node("/obj").createNode("geo", "active_shutdown_geo", run_init_scripts=False)
box = geometry.createNode("box")
box.setRenderFlag(True)
camera = hou.node("/obj").createNode("cam", "active_shutdown_camera")
camera.parm("tz").set(5)
rop = hou.node("/out").createNode("cycles_ipr", "active_shutdown_rop")
set_rop_devices(rop, report["device"])
for name, value in dict(soppath=box.path(), camera=camera.path(), resx=320, resy=180, samples=65536).items():
    rop.parm(name).set(value)
viewer = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.IPRViewer)
viewer.setRopNode(rop)
viewer.setPreview(True)
viewer.setAutoUpdate(True)
viewer.startRender()
started = time.monotonic()


def tick():
    global entry
    try:
        entry = registry.get(f"rop:{rop.sessionId()}")
        if time.monotonic() - started > 25:
            raise TimeoutError("Active shutdown test did not receive a progressive native render")
        if entry is None or not entry.helper_pid:
            return
        frame = entry.session.frame_info()
        if not frame:
            return
        assert not frame["complete"]
        original = _native.prewarm_apply

        def held_scan(job):
            time.sleep(0.2)
            return original(job)

        _native.prewarm_apply = held_scan
        box.parm("sizex").set(1.2)
        entry.apply(native_scene.capture(rop), hou.time())
        _native.prewarm_apply = original
        assert entry.views == 0 and entry.session.status()["capture_pending"]
        report.update(result="pending shutdown", active_status=entry.session.status(), render_viewers=["native"])
        save()
        hou.ui.removeEventLoopCallback(tick)
        hou.ui.postEventCallback(exit_test)
    except Exception:
        report.update(result="failed", error=traceback.format_exc())
        save()
        hou.ui.removeEventLoopCallback(tick)
        registry.close_all()
        hou.ui.postEventCallback(exit_test)


def exit_test():
    try:
        hou.exit(0 if report["result"] == "pending shutdown" else 1, suppress_save_prompt=True)
    except SystemExit:
        pass


hou.ui.addEventLoopCallback(tick)
save()
