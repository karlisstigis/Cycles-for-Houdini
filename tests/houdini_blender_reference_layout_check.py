# SPDX-License-Identifier: Apache-2.0
"""Mount the simplified reference editor on the primary monitor for visual QA.

Run with scripts/run-gui-test.ps1; inspect and close the isolated Houdini window
when done. This fixture deliberately does not invoke menu choices or callbacks.
"""

import json
import os
from pathlib import Path

import hou
from PySide6 import QtCore, QtGui

from cycles_houdini.blender import blend_catalogue, blend_worker

report_path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
verdict_path = report_path.with_suffix(".verdict.json")
geo = hou.node("/obj").createNode("geo", "reference_layout_check", run_init_scripts=False)
node = geo.createNode("cycles::blender_reference")
real_blend = os.environ.get("CYCLES_HOUDINI_REFERENCE_REAL_BLEND", "")
if real_blend:
    real_blend = str(Path(real_blend).resolve())
    assert Path(real_blend).is_file(), real_blend
node.parm("file").set(real_blend or "$HIP/assets/example.blend")
node.parm("references").set(2)
blend_catalogue.initialize_rows(node)
node.setParms(
    dict(
        ref_target1="Smooth_Red" if real_blend else "Wine_Bottle",
        ref_target2="Shared_Pieces" if real_blend else "Tableware",
        ref_target_kind2="collection",
        ref_enabled2=0,
    )
)
# Supply a cached catalogue for genuine chooser interaction, without Blender.
source = blend_catalogue._path(node.evalParm("file"))
record = dict(
    source=source,
    signature=blend_worker._signature(source) if real_blend else [1, 2, 3, 4],
    objects=["Smooth Red", "Smooth Blue"] if real_blend else ["Wine Bottle", "Glass Tall", "Glass Short"],
    collections=["Shared Pieces"] if real_blend else ["Tableware"],
)
node.parm("saved_catalogue").set(json.dumps(dict(schema_version=1, files={source: record})))
node.parm("catalogue_status").set("")
node.setSelected(True, clear_all_selected=True)
pane = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm, size=(720, 690))
pane.setCurrentNode(node)
pane.setPin(True)
window = pane.qtParentWindow()
window.setWindowTitle("Cycles Blender Reference - simplified layout QA")


def contain_editor():
    screen = QtGui.QGuiApplication.primaryScreen()
    area = screen.availableGeometry()
    handle = window.windowHandle()
    if handle is not None:
        handle.setScreen(screen)
    window.showNormal()
    window.setGeometry(area.left() + 80, area.top() + 100, min(720, area.width() - 160), min(690, area.height() - 180))
    window.raise_()
    window.activateWindow()
    report_path.write_text(
        json.dumps(
            dict(
                result="ready",
                node=node.path(),
                screen=screen.name(),
                screen_geometry=list(screen.geometry().getRect()),
                available_geometry=list(area.getRect()),
                device_pixel_ratio=screen.devicePixelRatio(),
                verdict_file=str(verdict_path),
                primary_monitor=True,
                geometry=list(window.frameGeometry().getRect()),
                synthetic_catalogue=not bool(real_blend),
                real_blend=real_blend,
                chooser_test_scope="Append Smooth_Blue to Smooth_Red using real source"
                if real_blend
                else "Open chooser and Escape; this fixture has no evaluated Blender source",
                pending_visual_checks=[
                    "File and Refresh share a row",
                    "enabled and Object / Collection share a row",
                    "Name chooser appends underscore names",
                    "Source Frame shows default 1 for both rows",
                    "Viewport menu offers Bounding Box and Convex Hull",
                    "Hull per member appears below Source Frame for the collection row",
                    "legacy override fields and status controls are hidden",
                ],
            ),
            indent=2,
        ),
        encoding="utf-8",
    )


QtCore.QTimer.singleShot(800, contain_editor)


def accept_visual_verdict():
    if not verdict_path.is_file():
        return
    try:
        verdict = json.loads(verdict_path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError):
        return
    if verdict.get("result") not in ("passed", "failed"):
        return
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report.update(result=verdict["result"], visual_verdict=verdict)
    try:
        geometry = node.geometry()
        primitives = geometry.prims()
        report["final_state"] = dict(
            selection=node.evalParm("ref_target1"),
            viewport_shape=node.evalParm("viewport_shape"),
            source_frames=[node.evalParm("ref_frame1"), node.evalParm("ref_frame2")],
            targets=[p.intrinsicValue("cycles_blend_target") for p in primitives],
            piece_names=list(geometry.primStringAttribValues("name")),
            hull_points=[p.intrinsicValue("cycles_blender_viewport_hull_points") for p in primitives],
            hull_triangles=[p.intrinsicValue("cycles_blender_viewport_hull_triangles") for p in primitives],
            bounds_known=[p.intrinsicValue("cycles_blender_bounds_known") for p in primitives],
            bounds_status=[p.intrinsicValue("cycles_blender_bounds_status") for p in primitives],
            bounds=[p.intrinsicValue("cycles_blender_bounds") for p in primitives],
            errors=node.errors(),
            warnings=node.warnings(),
            catalogue_status=node.evalParm("catalogue_status"),
        )
    except Exception as error:
        report["final_state_error"] = str(error)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    hou.ui.removeEventLoopCallback(accept_visual_verdict)
    hou.exit(0 if verdict["result"] == "passed" else 1, suppress_save_prompt=True)


hou.ui.addEventLoopCallback(accept_visual_verdict)
