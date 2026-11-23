# SPDX-License-Identifier: Apache-2.0
"""Open the test scene and actual render panel in a newly launched Houdini UI."""

import os
from pathlib import Path
import time
import traceback

import hou

ready_at = time.monotonic() + 0.5
panel_tab = None
panel = None
opened = False


def open_scene():
    global panel_tab, panel, opened
    root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
    destination = (
        Path(os.environ.get("CYCLES_HOUDINI_TEST_ARTIFACTS", root / "artifacts")) / "examples/sop-fx-courtyard"
    )
    logs = Path(os.environ.get("CYCLES_HOUDINI_EXAMPLE_LOG_DIR", destination))
    logs.mkdir(parents=True, exist_ok=True)
    try:
        scene = Path(
            os.environ.get("CYCLES_HOUDINI_EXAMPLE_SCENE", str(destination / "sop-fx-courtyard-current.hiplc"))
        )
        # Houdini paths use forward slashes. A backslash path leaves $HIPNAME as
        # the whole path, so crash recovery files could not be saved.
        hou.hipFile.load(scene.as_posix(), suppress_save_prompt=True)
        hou.putenv("OCIO", os.environ.get("CYCLES_HOUDINI_EXAMPLE_OCIO", str(scene.parent / "showcase.ocio")))
        hou.Color.reloadOCIO()
        from cycles_houdini import registry, device_selection

        rop = hou.node("/out/cycles_sop_courtyard")
        device_selection.selected_devices(rop, hou.time())
        panel_tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel)
        panel_tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        panel = panel_tab.activeInterfaceRootWidget()
        panel.window().resize(1200, 820)
        panel.set_rop(rop)
        panel.render()
        network = hou.ui.paneTabOfType(hou.paneTabType.NetworkEditor)
        if network:
            network.setPwd(hou.node("/obj/fx_courtyard"))
        rop.setCurrent(True, clear_all_selected=True)
        # Keep one persistent artist session; matching native IPR can attach
        # through Houdini's Render View when the artist opens it.
        assert panel.entry is not None, panel.info.text()
        assert len(registry.entries()) == 1
        (logs / "last-launch.log").write_text(
            "Opened the SOP scene and Cycles Render panel successfully.\n", encoding="utf-8"
        )
        opened = True
    except Exception:
        text = traceback.format_exc()
        (logs / "last-launch.log").write_text(text, encoding="utf-8")
        hou.ui.displayMessage("Could not open the native Cycles example.\n" + text, severity=hou.severityType.Error)


def wait_for_ui():
    if time.monotonic() >= ready_at:
        hou.ui.removeEventLoopCallback(wait_for_ui)
        hou.ui.postEventCallback(open_scene)


hou.ui.addEventLoopCallback(wait_for_ui)
