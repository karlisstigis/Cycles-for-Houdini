# SPDX-License-Identifier: Apache-2.0
"""Load and replace saved Cycles scenes with one live viewer; capture callback errors."""

from support.paths import artifacts_root

import hashlib
import faulthandler
from collections import deque
import json
import os
import sys
from pathlib import Path
import time
import traceback

import hou
from PySide6 import QtCore, QtWidgets
from cycles_houdini import device_selection, registry, queued, final_render, material_previews


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
        self.folder = artifacts_root() / "examples/sop-fx-courtyard"
        self.scenes = [self.folder / name for name in ("sop-fx-courtyard-adaptive.hiplc", "Test.hiplc")]
        self.scenes = [path for path in self.scenes if path.exists()]
        self.hashes = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in self.scenes}
        self.report = dict(result="running", scope=__doc__, events=[], callback_errors=[])
        self.exceptions = deque(maxlen=48)
        self.exception_file = self.path.with_suffix(".exceptions.jsonl").open("w")
        self.exception_keys = set()

        def trace(frame, event, arg):
            if event == "exception":
                kind, value, tb = arg
                if kind not in (StopIteration, GeneratorExit):
                    record = dict(
                        file=frame.f_code.co_filename,
                        function=frame.f_code.co_name,
                        traceback="".join(traceback.format_exception(kind, value, tb)),
                    )
                    self.exceptions.append(record)
                    key = (str(kind), str(value), frame.f_code.co_filename, frame.f_code.co_name)
                    if key not in self.exception_keys and len(self.exception_keys) < 256:
                        self.exception_keys.add(key)
                        self.exception_file.write(json.dumps(record) + "\n")
                        self.exception_file.flush()
            return trace

        def select_trace(frame, event, arg):
            filename = frame.f_code.co_filename
            if filename.startswith("<frozen ") or (
                "python313" in filename.lower() and "houdini" not in filename.lower()
            ):
                return
            frame.f_trace_lines = False
            return trace

        debugging = os.environ.get("CYCLES_HOUDINI_TRACE_CALLBACKS") == "1"
        if debugging:
            sys.settrace(select_trace)
        self.stacks = self.path.with_suffix(".stacks.log").open("w")
        faulthandler.dump_traceback_later(20, repeat=True, file=self.stacks)
        self.tab = self.panel = self.session = None
        self.index = 0
        self.stage = "load"
        self.finished = self.busy = False
        self.started = time.monotonic()
        # This test owns its Houdini process. Observe exceptions before HOM can
        # create modal error dialogs. Keep HOM callback identity unchanged.
        self.failed_callbacks = set()

        def observe(owner, name):
            callback = getattr(owner, name)

            def guarded(*args, **kwargs):
                if callback in self.failed_callbacks:
                    return
                try:
                    return callback(*args, **kwargs)
                except Exception:
                    self.failed_callbacks.add(callback)
                    self.report["callback_errors"].append(
                        dict(
                            callback=callback.__module__ + "." + callback.__qualname__, traceback=traceback.format_exc()
                        )
                    )
                    self.save()

            setattr(owner, name, guarded)

        for owner, name in (
            (
                (registry, "_poll_host"),
                (registry, "_scene_event"),
                (queued, "_poll_retirements"),
                (final_render, "_scene_event"),
                (final_render, "_node_event"),
                (material_previews.PreviewService, "tick"),
            )
            if debugging
            else ()
        ):
            observe(owner, name)
        self.timer = QtCore.QTimer(hou.qt.mainWindow())
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.save()

    def save(self):
        self.report.update(
            phase=self.stage, scene=str(self.scenes[self.index]) if self.index < len(self.scenes) else ""
        )
        self.path.write_text(json.dumps(self.report, indent=2))

    def load(self):
        try:
            hou.hipFile.load(str(self.scenes[self.index]), suppress_save_prompt=True, ignore_load_warnings=True)
            hou.putenv("OCIO", str(self.folder / "showcase.ocio"))
            hou.Color.reloadOCIO()
            self.rop = hou.node("/out/cycles_sop_courtyard")
            assert self.rop is not None
            device_selection.set_devices(self.rop, os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
            self.rop.setParms(dict(resx=320, resy=180, ipr_samples=4, ipr_use_denoise=0, render_threads=2))
            if self.tab is None:
                self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(850, 600))
                self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
            self.stage = "mount"
            self.save()
        except Exception:
            self.finish(traceback.format_exc())

    def tick(self):
        if self.finished or self.busy:
            return
        self.busy = True
        try:
            assert time.monotonic() - self.started < 150, self.stage
            assert not self.report["callback_errors"], self.report["callback_errors"]
            dialogs = [
                widget
                for widget in QtWidgets.QApplication.topLevelWidgets()
                if "Python Callback Error" in widget.windowTitle()
            ]
            if dialogs:
                self.report["interface_errors"] = self.tab.activeInterfaceScriptErrors() if self.tab else ""
                self.report["dialog_count"] = len(dialogs)
                self.report["dialog_widgets"] = [
                    dict(
                        type=type(child).__name__,
                        name=child.objectName(),
                        text=str(child.property("text") or child.property("plainText") or ""),
                    )
                    for widget in dialogs[:2]
                    for child in (widget, *widget.findChildren(QtWidgets.QWidget))
                ]
                raise AssertionError(f"{len(dialogs)} Python Callback Error dialogs")
            if self.stage == "load":
                self.stage = "loading"
                self.save()
                hou.ui.postEventCallback(self.load)
                return
            if self.stage == "mount":
                self.panel = self.tab.activeInterfaceRootWidget()
                if self.panel is None:
                    return
                self.panel.set_rop(self.rop)
                self.report["interface_errors"] = self.tab.activeInterfaceScriptErrors()
                self.save()
                self.panel.render()
                self.stage = "render"
                return
            if self.stage == "render":
                if self.panel.entry is None:
                    return
                self.session = self.panel.entry.session
                status = self.session.status()
                assert status["state"] != "failed", status
                if status["state"] != "complete" or status.get("capture_pending") or self.panel.canvas.image.isNull():
                    return
                self.report["events"].append(dict(scene=str(self.scenes[self.index]), status=status))
                self.index += 1
                if self.index < len(self.scenes):
                    self.stage = "load"
                    self.save()
                    return
                self.panel.dispose()
                registry.close_all()
                self.stage = "close"
                return
            if self.stage == "close":
                status = self.session.status()
                if status["state"] != "closed":
                    return
                assert status["owned_geometry_bytes"] == status["device_allocation_bytes"] == 0, status
                assert all(
                    hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest
                    for path, digest in self.hashes.items()
                )
                self.report["closed"] = status
                self.finish()
                return
        except Exception:
            self.finish(traceback.format_exc())
        finally:
            self.busy = False

    def finish(self, error=None):
        if self.finished:
            return
        self.finished = True
        self.timer.stop()
        sys.settrace(None)
        faulthandler.cancel_dump_traceback_later()
        self.stacks.close()
        self.exception_file.close()
        self.report.update(result="failed" if error else "passed", error=error, recent_exceptions=list(self.exceptions))
        self.save()
        try:
            if self.panel:
                self.panel.dispose()
            registry.close_all()
        except Exception:
            self.report["cleanup_error"] = traceback.format_exc()
            self.save()
        hou.ui.postEventCallback(lambda: hou.exit(1 if error else 0, suppress_save_prompt=True))


workflow = Workflow()
