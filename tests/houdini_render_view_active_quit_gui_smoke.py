# SPDX-License-Identifier: Apache-2.0
"""Quit an isolated real Houdini Render View immediately after a large frame submission."""

import json
import math
import os
from pathlib import Path
import time
import traceback

import hou
import pass_fixtures
from cycles_houdini import registry
from cycles_houdini.device_selection import set_devices

receipt = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
width, height = 1920, 1080
geo = hou.node("/obj").createNode("geo", run_init_scripts=False)
box = geo.createNode("box")
box.setRenderFlag(True)
camera = hou.node("/obj").createNode("cam")
camera.parm("tz").set(5)
rop = hou.node("/out").createNode("cycles_ipr")
pass_fixtures.set_parameters(
    rop, dict(soppath=box.path(), camera=camera.path(), resx=width, resy=height, pass_depth=1, ipr_samples=4)
)
set_devices(rop, os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
viewer = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.IPRViewer)
viewer.setRopNode(rop)
viewer.setPreview(True)
viewer.setAutoUpdate(True)
viewer.startRender()
deadline = time.monotonic() + 75


def arm():
    try:
        if time.monotonic() > deadline:
            raise TimeoutError("No depth image from native Render View")
        entry = registry.get(f"rop:{rop.sessionId()}")
        if not entry or not entry.socket_port:
            return
        try:
            if "depth" not in viewer.planes() or not math.isclose(
                viewer.pixel("depth", width // 2, height // 2)[0], 4.5, abs_tol=0.001
            ):
                return
        except hou.OperationFailed:
            return
        session = entry.session
        previous = session.frame_info()["generation"]
        present = session.present_native

        def present_then_quit():
            image, _, _ = session._controller.frame()
            info = session.frame_info()
            present()
            if (
                not info
                or image is None
                or not session._controller.frame_is_current(image)
                or info["generation"] <= previous
            ):
                return
            assert (info["width"], info["height"]) == (width, height)
            assert {"combined", "depth"}.issubset(info["passes"])
            process = session._controller.backend.process
            assert process.poll() is None
            receipt.write_text(
                json.dumps(
                    dict(
                        result="passed",
                        size=[width, height],
                        passes=info["passes"],
                        generation=info["generation"],
                        worker_pid=process.pid,
                        quit_requested_unix=time.time(),
                    )
                ),
                encoding="utf-8",
            )
            hou.exit(0, suppress_save_prompt=True)

        hou.ui.removeEventLoopCallback(arm)
        session.present_native = present_then_quit
        camera.parm("tx").set(0.05)
    except Exception:
        receipt.write_text(json.dumps(dict(result="failed", error=traceback.format_exc())), encoding="utf-8")
        hou.exit(1, suppress_save_prompt=True)


hou.ui.addEventLoopCallback(arm)
