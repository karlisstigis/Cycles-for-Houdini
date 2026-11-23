# SPDX-License-Identifier: Apache-2.0
"""Native ROP MPlay sequence delivery and floating IPR window reuse."""

import json
import os
from pathlib import Path
import time
import traceback

import hou
import OpenImageIO as oiio
from PySide6 import QtCore, QtWidgets
import cycles_houdini as ch
from cycles_houdini import device_selection, registry
from cycles_houdini.viewer.panel import RenderPanel
from cycles_houdini.rop import render_actions, rop_layout
from support.paths import artifacts_root

path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
output_root = artifacts_root() / "mp-actions" / str(os.getpid())
output_root.mkdir(parents=True, exist_ok=True)
report = {"result": "running", "device": os.environ["CYCLES_HOUDINI_GUI_DEVICE"]}
geo = hou.node("/obj").createNode("geo", "mplay_subject", run_init_scripts=False)
geo.createNode("sphere").setParms({"type": 2})
geo.parm("tx").setExpression("($F-111)*0.8")
mat = ch.create_material(name="mplay_emission", shader="emission")
# The default this test was written for (new nodes now take Blender's).
mat.node("surface_shader").setParms(dict(strength=10.0))
mat.node("surface_shader").parmTuple("color").set((0.2, 0.4, 0.8))
geo.parm("shop_materialpath").set(mat.path())
cam = hou.node("/obj").createNode("cam", "mplay_camera")
cam.setParms(dict(tz=5, projection=1, orthowidth=4))
rop = hou.node("/out").createNode("cycles_ipr", "mplay_sequence")
rop.setParms(
    dict(
        vobject=geo.path(),
        camera=cam.path(),
        resx=128,
        resy=96,
        samples=1,
        ipr_samples=2,
        use_denoise=0,
        ipr_use_denoise=0,
        render_threads=2,
        allowmotionblur=0,
        trange=1,
        persistent_data=1,
    )
)
device_selection.set_devices(rop, [report["device"]])
for parm in rop.parmTuple("f"):
    parm.deleteAllKeyframes()
rop.parmTuple("f").set((110, 112, 1))
picture = output_root / "mplay-output.$F4.exr"
rop.parm("picture").set(str(picture))
rop.parm("output_existing").set("error")
# Disk output policies apply to MPlay too; a genuine failure must still raise.
existing = output_root / "mplay-output.0110.exr"
existing.write_bytes(b"artist output must stay unchanged")
events = []
rop.addRenderEventCallback(lambda node, event, at_time: events.append((event.name(), hou.timeToFrame(at_time))))
names = [p.name() for p in rop.parmTemplateGroup().entries()]
assert names[:4] == ["ch_ipr", "execute", "ch_render_mplay", "ch_controls"], names[:6]
# Simulate a saved per-node layout predating these buttons.
group = rop.parmTemplateGroup()
group.remove("ch_ipr")
group.remove("ch_render_mplay")
rop.setParmTemplateGroup(group)
rop_layout.ensure_ipr_settings_layout(rop)
assert not rop_layout.ensure_ipr_settings_layout(rop)
assert rop.parm("picture").unexpandedString() == str(picture)
assert [p.name() for p in rop.parmTemplateGroup().entries()][:4] == names[:4], {
    "names": [p.name() for p in rop.parmTemplateGroup().entries()],
    "ipr": str(rop.parmTemplateGroup().find("ch_ipr")),
    "execute_indices": rop.parmTemplateGroup().findIndices("execute"),
    "default_ipr": str(rop.type().definition().parmTemplateGroup().find("ch_ipr")),
}
timer = QtCore.QTimer(hou.qt.mainWindow())
timer.setInterval(50)
phase = "mplay"
deadline = time.monotonic() + 120
pane = panel = None


class WindowShows(QtCore.QObject):
    def __init__(self):
        super().__init__()
        self.records = []

    def eventFilter(self, watched, event):
        if event.type() == QtCore.QEvent.Type.Show and isinstance(watched, QtWidgets.QWidget) and watched.isWindow():
            self.records.append(
                (
                    watched,
                    watched.testAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen),
                    bool(watched.findChildren(RenderPanel)),
                )
            )
        return False


shows = WindowShows()
QtWidgets.QApplication.instance().installEventFilter(shows)


def finish(error=None):
    timer.stop()
    QtWidgets.QApplication.instance().removeEventFilter(shows)
    if panel is not None:
        panel.dispose()
    if pane is not None:
        pane.close()
    registry.close_all()
    report.update(result="failed" if error else "passed", error=error)
    path.write_text(json.dumps(report, indent=2))
    hou.ui.postEventCallback(lambda: hou.exit(1 if error else 0, suppress_save_prompt=True))


def tick():
    global phase, pane, panel
    try:
        if time.monotonic() > deadline:
            raise TimeoutError(phase)
        if phase == "mplay":
            phase = "rendering"
            try:
                render_actions.render_mplay(rop)
            except hou.OperationFailed:
                pass
            else:
                raise AssertionError("MPlay hid a genuine existing-output error")
            assert existing.read_bytes() == b"artist output must stay unchanged"
            events.clear()
            rop.parm("output_existing").set("overwrite")
            started = time.monotonic()
            rop.parm("ch_render_mplay").pressButton()
            summary = rop.cachedUserData("cycles_houdini.final_sequence")
            assert summary["completed_frames"] == 3 and not summary["error"], summary
            assert summary["last_outputs"] and not summary["session_alive"], summary
            assert [round(frame) for event, frame in events if event == "PostFrame"] == [110, 111, 112], events
            for frame in (110, 111, 112):
                filename = output_root / f"mplay-output.{frame:04d}.exr"
                image = oiio.ImageInput.open(str(filename))
                assert image is not None, filename
                assert (image.spec().width, image.spec().height) == (128, 96)
                image.close()
            report.update(
                sequence=summary,
                events=events,
                sequence_seconds=time.monotonic() - started,
                private_mplay_label=render_actions.MPlayOutput(rop).label,
                saved_layout_migrated=True,
                outputs_saved=True,
                real_output_errors_preserved=True,
            )
            rop.parm("ch_ipr").pressButton()
            pane = render_actions.open_ipr(rop)
            panel = pane.panel
            assert isinstance(panel, RenderPanel)
            visible_shows = [
                has_viewer for widget, offscreen, has_viewer in shows.records if widget == pane and not offscreen
            ]
            assert visible_shows and all(visible_shows), visible_shows
            report["window_shown_only_with_viewer_attached"] = True
            matches = [
                widget
                for widget in QtWidgets.QApplication.topLevelWidgets()
                if widget.windowTitle() == "Cycles Render View" and widget.isVisible()
            ]
            assert len(matches) == 1, matches
            phase = "ipr"
        elif phase == "ipr":
            entry = panel.entry
            if entry is None:
                return
            status = entry.session.status()
            if (
                status["state"] != "complete"
                or panel.canvas.image.isNull()
                or panel.canvas.generation != status["generation"]
            ):
                return
            before = status["generation"]
            again = render_actions.open_ipr(rop)
            assert again == pane
            assert entry.session.status()["generation"] == before
            report.update(ipr_window_reused=True, ipr_generation=before)
            panel.grab().save(str(path.with_suffix(".png")))
            pane.close()
            assert panel._disposed
            pane = panel = None
            phase = "closed"
        elif phase == "closed":
            if render_actions._ipr_window is not None:
                return
            pane = render_actions.open_ipr(rop)
            panel = pane.panel
            phase = "reopened"
        elif phase == "reopened":
            if (
                panel.entry is None
                or panel.entry.session.status()["state"] != "complete"
                or panel.canvas.image.isNull()
            ):
                return
            report["ipr_close_reopen"] = True
            finish()
    except BaseException:
        finish(traceback.format_exc())


timer.timeout.connect(tick)
timer.start()
