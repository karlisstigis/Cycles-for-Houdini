# SPDX-License-Identifier: Apache-2.0
"""Real GUI focus routing and CPU/GPU coverage display, without renderer updates."""

import json, os, time, traceback
from pathlib import Path
import hou
import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets, QtTest
from cycles_houdini import _native, registry, device_selection
from cycles_houdini.panel import RenderPanel
from cycles_houdini.gpu_display import Converter
from cycles_houdini.display_queue import DisplayQueue

app = QtWidgets.QApplication.instance()
report = dict(result="running", cases=[])
path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
geo = hou.node("/obj").createNode("geo", run_init_scripts=False)
box = geo.createNode("box")
box.setRenderFlag(True)
geo.parmTuple("r").set((13, 23, 7))
cam = hou.node("/obj").createNode("cam")
cam.parm("tz").set(5)
rop = hou.node("/out").createNode("cycles_ipr")
rop.setParms(
    dict(
        source_mode="sop",
        soppath=box.path(),
        camera=cam.path(),
        resx=96,
        resy=64,
        viewer_interactive=1,
        ipr_samples=16,
        ipr_adaptive_sampling=0,
        ipr_use_denoise=0,
        allowmotionblur=0,
        transparent_background=1,
        material_kind="emission",
        use_material_assignments=0,
    )
)
rop.parmTuple("basecolor").set((0.4, 0.2, 0.1))
device_selection.set_devices(rop, os.environ.get("CYCLES_HOUDINI_GUI_DEVICE", "CPU"))
p = RenderPanel()
p.resize(1400, 700)
p.set_rop(rop)
p.show()
p.activateWindow()
p.render()
queue = None
owned = None
baseline = None
original = None
submitted = False
index = 0
cases = [
    (backend, channel, alpha)
    for backend in ("opengl", "cpu")
    for channel in ("rgba", "rgb", "r", "g", "b", "alpha")
    for alpha in (False, True)
]
began = time.monotonic()
timer = QtCore.QTimer(hou.qt.mainWindow())
timer.setInterval(25)


def shortcuts():
    assert p.canvas.background_color == QtGui.QColor("black")
    assert p.channel_alpha.isChecked()

    # Application filters see host navigation events too. An unrelated release
    # must remain opaque, even if focus left us while Space was held.
    class HostRelease:
        def type(self):
            return QtCore.QEvent.KeyRelease

        def key(self):
            raise AssertionError("Read a foreign navigation key payload")

    foreign = QtWidgets.QWidget()
    assert not p._shortcuts.eventFilter(foreign, HostRelease())
    p._shortcuts.space = p.canvas._space_down = True
    assert not p._shortcuts.eventFilter(foreign, HostRelease())
    assert not p._shortcuts.space and not p.canvas._space_down
    foreign.deleteLater()
    p.activateWindow()
    app.processEvents()
    p.stop_button.setFocus()
    QtGui.QCursor.setPos(p.canvas.mapToGlobal(p.canvas.rect().center()))
    assert app.activeWindow() is p.window()
    for key, token in (
        (QtCore.Qt.Key_R, "r"),
        (QtCore.Qt.Key_G, "g"),
        (QtCore.Qt.Key_B, "b"),
        (QtCore.Qt.Key_A, "alpha"),
    ):
        QtTest.QTest.keyClick(p.stop_button, key)
        assert p.channels.currentData() == token, (key, p.channels.currentData())
        QtTest.QTest.keyClick(p.stop_button, key)
        assert p.channels.currentData() == "rgba"
    before = p.checker.isChecked()
    QtTest.QTest.keyClick(p.stop_button, QtCore.Qt.Key_T)
    assert p.checker.isChecked() != before
    QtTest.QTest.keyClick(p.stop_button, QtCore.Qt.Key_T)
    assert p.checker.isChecked() == before
    QtTest.QTest.keyPress(p.stop_button, QtCore.Qt.Key_Space)
    assert p._shortcuts.space and p.canvas._space_down
    QtTest.QTest.keyRelease(p.canvas, QtCore.Qt.Key_Space)
    assert not p._shortcuts.space and not p.canvas._space_down
    QtTest.QTest.keyPress(p.stop_button, QtCore.Qt.Key_Space)
    app.sendEvent(p.canvas, QtGui.QFocusEvent(QtCore.QEvent.FocusOut))
    assert not p._shortcuts.space and not p.canvas._space_down
    QtTest.QTest.keyRelease(p.canvas, QtCore.Qt.Key_Space)
    # Do not steal editing keys, even with the mouse over the image.
    editor = QtWidgets.QLineEdit(p)
    editor.show()
    editor.setFocus()
    app.processEvents()
    QtTest.QTest.keyClicks(editor, "rgbaft")
    assert editor.text() == "rgbaft" and p.channels.currentData() == "rgba"
    editor.hide()
    editor.deleteLater()
    p.stop_button.setFocus()
    QtTest.QTest.keyClick(p.stop_button, QtCore.Qt.Key_R)
    p.update_display_controls()
    assert p.channel_alpha.isEnabled()
    p.channel_alpha.click()
    assert not p.display_options()["use_alpha"]
    p.channel_alpha.click()
    assert p.display_options()["use_alpha"]
    QtTest.QTest.keyClick(p.stop_button, QtCore.Qt.Key_R)
    report["shortcuts"] = (
        "R/G/B/A toggle back to RGBA; T toggles; toolbar focus works before any image click; text editing unaffected"
    )


def finish(error=None):
    timer.stop()
    if queue:
        queue.close(wait=True)
    p.dispose()
    p.close()
    registry.close_all()
    report.update(result="failed" if error else "passed", error=error)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    hou.ui.postEventCallback(lambda: hou.exit(1 if error else 0, suppress_save_prompt=True))


def tick():
    global owned, baseline, original, queue, submitted, index
    try:
        if time.monotonic() - began > 100:
            raise TimeoutError(p.info.text())
        if owned is None:
            if not p.entry or p.entry.session.status()["state"] != "complete" or p.canvas.image.isNull():
                return
            original = p.entry.session.status()
            shortcuts()
            owned = p.entry.session.display_frame()
            baseline = (
                np.frombuffer(_native.display_image(owned[0], dict(channels="rgba"))["rgba"], np.uint8)
                .reshape(-1, 4)
                .copy()
            )
            assert np.any((baseline[:, 3] > 0) & (baseline[:, 3] < 255)), "Fixture requires fractional coverage"
        if index == len(cases):
            status = p.entry.session.status()
            for key in ("generation", "geometry_conversions"):
                assert status[key] == original[key], key
            finish()
            return
        backend, channel, alpha = cases[index]
        if queue is None:
            converter = Converter()
            if backend == "cpu":
                converter.failure = "Test CPU conversion"
            queue = DisplayQueue(converter)
        if not submitted:
            queue.request(index, *owned, dict(channels=channel, use_alpha=alpha, collect_diagnostics=True))
            submitted = True
            return
        ready = queue.take()
        if ready is None:
            return
        _, result, error = ready
        assert error is None, error
        actual = np.frombuffer(result["rgba"], np.uint8).reshape(-1, 4)
        expected = baseline.copy()
        if channel == "alpha":
            expected[:, :3] = baseline[:, 3, None]
            expected[:, 3] = 255
        else:
            if channel in ("r", "g", "b"):
                expected[:, :3] = baseline[:, {"r": 0, "g": 1, "b": 2}[channel], None]
            if channel != "rgba" and not alpha:
                expected[:, 3] = 255
        difference = int(np.abs(actual.astype(np.int16) - expected.astype(np.int16)).max())
        assert difference <= (2 if backend == "opengl" else 0), (backend, channel, alpha, difference)
        if backend == "opengl" and channel != "alpha":
            assert result["diagnostics"]["backend"] == "opengl", result["diagnostics"]
        report["cases"].append(dict(backend=backend, channel=channel, use_alpha=alpha, maximum_byte_error=difference))
        queue.close(wait=True)
        queue = None
        submitted = False
        index += 1
    except BaseException:
        finish(traceback.format_exc())


timer.timeout.connect(tick)
timer.start()
