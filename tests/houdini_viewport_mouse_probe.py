# SPDX-License-Identifier: Apache-2.0
"""Manual/OS-pointer measurement fixture: never synthesizes transform edits."""

from support.paths import artifacts_root

import hashlib
import json
import os
from pathlib import Path
import time
import traceback
import ctypes
import hou
import numpy as np
from PySide6 import QtCore, QtWidgets
from cycles_houdini import device_selection, native_scene, object_scene, registry, _native
from cycles_houdini.panel import RenderPanel


class Probe:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.source = artifacts_root() / "examples/sop-fx-courtyard/Test.hiplc"
        self.digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        hou.hipFile.load(str(self.source), suppress_save_prompt=True, ignore_load_warnings=True)
        self.rop = hou.node("/out/Test_2")
        device_selection.set_devices(self.rop, [os.environ["CYCLES_HOUDINI_GUI_DEVICE"]])
        self.actor = hou.node("/obj/circle1")
        self.light = next(
            n
            for n in object_scene.selected(self.rop, hou.time(), lights=True)
            if not native_scene.world.is_environment(n)
        )
        self.viewer = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.SceneViewer)
        self.viewer.setPwd(hou.node("/obj"))
        self.viewer.curViewport().setCamera(hou.node(self.rop.evalParm("camera")))
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel)
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.started = time.monotonic()
        self.panel = None
        self.finishing = False
        self.gesture = None
        self.last_tick = time.perf_counter()
        self.last_save = 0
        self.restores = []
        self.mouse_state = ctypes.WinDLL("user32").GetAsyncKeyState
        self.mouse_state.argtypes = [ctypes.c_int]
        self.mouse_state.restype = ctypes.c_short
        self.last_edit = 0
        self.edits = []
        self.gaps = []
        self.calls = []
        self.report = dict(
            result="running",
            scope=__doc__,
            gestures=[],
            source=str(self.source),
            source_sha256=self.digest,
            runtime_root=os.environ["CYCLES_HOUDINI_HOST_ROOT"],
            edits=self.edits,
            gaps=self.gaps,
            calls=self.calls,
        )
        self.callback = self.edited
        for node in (self.actor, self.light):
            node.addEventCallback((hou.nodeEventType.ParmTupleChanged,), self.callback)
        for owner, name in (
            (RenderPanel, "poll"),
            (registry.Entry, "preview_object_poses"),
            (_native, "prepare_apply"),
            (native_scene.SceneObserver, "refresh"),
        ):
            original = getattr(owner, name)

            def timed(*args, _original=original, _key=owner.__name__ + "." + name, **kwargs):
                began = time.perf_counter()
                try:
                    return _original(*args, **kwargs)
                finally:
                    elapsed = (time.perf_counter() - began) * 1000
                    if elapsed > 5:
                        self.calls.append(dict(at=began - self.started, method=_key, ms=elapsed))
                    if self.gesture is not None:
                        self.gesture["calls"].setdefault(_key, []).append((time.perf_counter() - began) * 1000)

            setattr(owner, name, timed)
            self.restores.append((owner, name, original))
        self.timer = QtCore.QTimer()
        self.timer.setTimerType(QtCore.Qt.PreciseTimer)
        self.timer.setInterval(8)
        self.timer.timeout.connect(self.tick)
        self.timer.start()

    def edited(self, node, parm_tuple=None, **kwargs):
        if parm_tuple is not None and parm_tuple.name() not in ("t", "r"):
            return
        now = time.perf_counter()
        self.last_edit = now
        self.edits.append(
            dict(
                at=now - self.started,
                node=node.path(),
                values=self.values(),
                qt_down=QtWidgets.QApplication.mouseButtons() != QtCore.Qt.NoButton,
                os_down=bool(self.mouse_state(1) & 0x8000),
                ipr=bool(self.panel and self.panel._render_mode and not self.panel._stopped),
            )
        )

    def select(self, node):
        def apply():
            self.viewer.setCurrentState("select")
            node.setSelected(True, clear_all_selected=True)
            node.setCurrent(True)
            self.viewer.setIsCurrentTab()
            self.viewer.enterTranslateToolState()
            self.report["selected"] = node.path()
            self.save()

        hou.ui.postEventCallback(apply)

    def values(self):
        return {
            node.path(): dict(t=node.parmTuple("t").eval(), r=node.parmTuple("r").eval())
            for node in (self.actor, self.light)
        }

    def save(self):
        self.report["elapsed_seconds"] = time.monotonic() - self.started
        if self.panel and self.panel.entry:
            self.report["status"] = self.panel.entry.session.status()
            self.report["transforms"] = self.values()
            self.report["viewer_state"] = self.viewer.currentState()
        self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")

    def tick(self):
        if self.finishing:
            return
        now = time.perf_counter()
        gap = (now - self.last_tick) * 1000
        self.last_tick = now
        if now - self.last_edit < 1 or gap > 50:
            self.gaps.append(dict(at=now - self.started, ms=gap))
        try:
            if time.monotonic() - self.started > 900:
                raise TimeoutError("Pointer probe timed out")
            if self.panel is None:
                panel = self.tab.activeInterfaceRootWidget()
                viewport = hou.qt.floatingPanelWindow(self.viewer.floatingPanel())
                render = hou.qt.floatingPanelWindow(self.tab.floatingPanel())
                if panel is None or viewport is None or render is None:
                    return
                self.panel = panel
                self.viewport_window = viewport
                screen = QtWidgets.QApplication.primaryScreen().availableGeometry()
                half = screen.width() // 2
                viewport.setGeometry(screen.x(), screen.y() + 30, half - 10, min(950, screen.height() - 170))
                render.setGeometry(screen.x() + half, screen.y() + 30, half - 10, min(950, screen.height() - 170))
                viewport.setWindowTitle("Cycles pointer test - viewport")
                render.setWindowTitle("Cycles pointer test - render")
                viewport.show()
                render.show()
                self.controls = QtWidgets.QWidget()
                self.controls.setWindowTitle("Cycles pointer test - controls")
                row = QtWidgets.QHBoxLayout(self.controls)
                for label, callback in [
                    ("Select mesh", lambda: self.select(self.actor)),
                    ("Select light", lambda: self.select(self.light)),
                    ("Finish test", lambda: hou.ui.postEventCallback(self.finish)),
                ]:
                    button = QtWidgets.QPushButton(label)
                    button.clicked.connect(callback)
                    row.addWidget(button)
                self.controls.setGeometry(screen.x() + 30, screen.y() + screen.height() - 110, 450, 60)
                self.controls.show()
                panel.set_rop(self.rop)
                panel.render()
                self.select(self.actor)
                self.save()
                return
            down = bool(self.mouse_state(1) & 0x8000)
            if down and self.gesture is None:
                self.gesture = dict(
                    start=time.monotonic() - self.started,
                    started=now,
                    before=self.values(),
                    gaps=[],
                    calls={},
                    ipr=self.panel._render_mode and not self.panel._stopped,
                    state=self.viewer.currentState(),
                    selected=[n.path() for n in hou.selectedNodes()],
                )
            elif self.gesture is not None:
                self.gesture["gaps"].append(gap)
                if not down:
                    gesture = self.gesture
                    self.gesture = None
                    values = self.values()
                    if values != gesture["before"]:
                        gaps = gesture.pop("gaps")
                        gesture.update(
                            after=values,
                            duration=now - gesture.pop("started"),
                            p95_ms=float(np.quantile(gaps, 0.95)),
                            max_ms=max(gaps),
                            stalls_over_50ms=sum(x > 50 for x in gaps),
                        )
                        gesture["calls"] = {
                            key: dict(count=len(v), total_ms=sum(v), max_ms=max(v))
                            for key, v in gesture["calls"].items()
                        }
                        self.report["gestures"].append(gesture)
                        self.save()
            if now - self.last_save > 1 and self.gesture is None:
                self.last_save = now
                self.save()
        except Exception:
            self.finish(traceback.format_exc())

    def finish(self, error=None):
        if self.finishing:
            return
        self.finishing = True
        self.timer.stop()
        self.viewer.setCurrentState("select")
        for node in (self.actor, self.light):
            node.removeEventCallback((hou.nodeEventType.ParmTupleChanged,), self.callback)
        if self.panel:
            self.panel.dispose()
        registry.close_all()
        for owner, name, original in reversed(self.restores):
            setattr(owner, name, original)
        assert hashlib.sha256(self.source.read_bytes()).hexdigest() == self.digest
        self.report.update(result="failed" if error else "passed", error=error, saved_scene_unchanged=True)
        self.save()
        hou.ui.postEventCallback(lambda: hou.exit(1 if error else 0, suppress_save_prompt=True))


probe = Probe()
