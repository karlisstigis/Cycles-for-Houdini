# SPDX-License-Identifier: Apache-2.0
"""HIP-local viewer preferences; never store images or a running session."""

import json
import math
import hou
from PySide6 import QtCore, QtGui

KEY = "cycles_viewer_state_v1"


class ViewerState:
    def __init__(self, panel):
        self.panel = panel
        self.restoring = True
        self.dirty = False
        self.pending_pass = None
        self.defaults = self.capture()
        self.timer = QtCore.QTimer(panel)
        self.timer.setSingleShot(True)
        self.timer.setInterval(150)
        self.timer.timeout.connect(self.save)
        for signal in (
            panel.auto.toggled,
            panel.channels.currentIndexChanged,
            panel.channel_alpha.toggled,
            panel.checker.toggled,
            panel.background_button.toggled,
            panel.background_button.colorChanged,
            panel.display.currentTextChanged,
            panel.view.currentTextChanged,
            panel.exposure.valueChanged,
            panel.gamma.valueChanged,
            panel.keep_image_size.toggled,
            panel.resolution_link.toggled,
            panel.save_format.currentIndexChanged,
            panel.canvas.viewChanged,
        ):
            signal.connect(self.changed)
        panel.pass_menu.activated.connect(self.pass_changed)
        hou.hipFile.addEventCallback(self.scene_event)
        self.restore()

    def capture(self):
        p = self.panel
        canvas = p.canvas
        return dict(
            version=1,
            rop=p._rop.path() if p._rop else "",
            auto=p.auto.isChecked(),
            channel=p.channels.currentData(),
            use_alpha=p.channel_alpha.isChecked(),
            checker=p.checker.isChecked(),
            background=p.background_button.isChecked(),
            color=canvas.background_color.name(),
            exposure=p.exposure.value(),
            gamma=p.gamma.value(),
            display=p.display.currentText(),
            view=p.view.currentText(),
            render_pass=self.pending_pass or p.pass_menu.currentData(),
            save_format=p.save_format.currentData(),
            keep_size=p.keep_image_size.isChecked(),
            zoom_mode=canvas.view_mode,
            resolution_linked=p.resolution_link.isChecked(),
            link_keep_size=p._resolution_linker.manual_keep,
            zoom=canvas._scale,
            pan=[canvas._pan.x(), canvas._pan.y()],
        )

    def changed(self, *_):
        if not self.restoring:
            self.dirty = True
            self.timer.start()

    def pass_changed(self, *_):
        self.pending_pass = None
        self.changed()

    def save(self, *, force=False):
        if self.restoring or self.panel._disposed or not (self.dirty or force):
            return
        try:
            root = hou.node("/obj")
            data = json.dumps(self.capture(), sort_keys=True, separators=(",", ":"), allow_nan=False)
            if root and root.userData(KEY) != data:
                root.setUserData(KEY, data)
            self.dirty = False
        except (hou.Error, ValueError, TypeError):
            pass

    def allow_follow(self):
        # Houdini reports a context node on panel creation; a saved explicit ROP
        # must win over that implicit selection. The viewer dropdown remains active.
        return not (self.panel._rop and self.saved_rop())

    def saved_rop(self):
        try:
            state = json.loads(hou.node("/obj").userData(KEY) or "{}")
            node = hou.node(state.get("rop", "")) if isinstance(state, dict) else None
            return node if node and node.type().name() == "cycles_ipr" else None
        except (hou.Error, ValueError, TypeError):
            return None

    def scene_event(self, event_type, **_kwargs):
        if event_type in (hou.hipFileEventType.BeforeLoad, hou.hipFileEventType.BeforeClear):
            self.timer.stop()
            self.restoring = True
            self.dirty = False
            self.pending_pass = None
            p = self.panel
            p._resolution_linker.close()
            p.resolution_link.setChecked(False)
            if p._observer:
                p._observer.close()
                p._observer = None
            p._detach()
            p._rop = None
            p._rop_identity = None
            p.rop_path.set_path("")
            p.camera.set_path("")
            p._live_image = None
            p._snapshots.clear()
            p.canvas.composites.clear()
            p.snapshot_menu.blockSignals(True)
            p.snapshot_menu.clear()
            p.snapshot_menu.addItem("Live")
            p.snapshot_menu.blockSignals(False)
            p.canvas.image = QtGui.QImage()
            p.canvas.full_size = (0, 0)
            p.canvas.update()
            p.update_render_controls()
        elif event_type in (hou.hipFileEventType.AfterLoad, hou.hipFileEventType.AfterClear):
            self.restore()
        elif event_type == hou.hipFileEventType.BeforeSave:
            self.timer.stop()
            self.save()

    def restore(self):
        p = self.panel
        self.restoring = True
        try:
            root = hou.node("/obj")
            try:
                value = json.loads(root.userData(KEY) or "{}") if root else {}
                if not isinstance(value, dict) or value.get("version") != 1:
                    value = {}
            except (ValueError, TypeError):
                value = {}
            state = dict(self.defaults)
            state.update(value)
            node = hou.node(state["rop"]) if isinstance(state.get("rop"), str) and state["rop"] else None
            if node and node.type().name() == "cycles_ipr":
                p.set_rop(node)
            p.auto.setChecked(bool(state["auto"]))
            index = p.channels.findData(state["channel"])
            p.channels.setCurrentIndex(index if index >= 0 else 0)
            p.channel_alpha.setChecked(bool(state["use_alpha"]))
            p.background_button.set_color(QtGui.QColor(state["color"]))
            p.background_button.setChecked(bool(state["background"]))
            p.checker.setChecked(bool(state["checker"]))
            for widget, key in ((p.exposure, "exposure"), (p.gamma, "gamma")):
                number = state[key]
                if isinstance(number, (int, float)) and math.isfinite(number):
                    widget.setValue(number)
            if p.display.findText(state["display"]) >= 0:
                p.display.setCurrentText(state["display"])
            if p.view.findText(state["view"]) >= 0:
                p.view.setCurrentText(state["view"])
            index = p.save_format.findData(state["save_format"])
            p.save_format.setCurrentIndex(index if index >= 0 else 0)
            p.keep_image_size.setChecked(bool(state["keep_size"]))
            if state["zoom_mode"] in ("fit", "actual", "manual"):
                p.canvas.view_mode = state["zoom_mode"]
            if isinstance(state["zoom"], (int, float)) and math.isfinite(state["zoom"]) and state["zoom"] > 0:
                p.canvas._scale = state["zoom"]
            pan = state["pan"]
            if (
                isinstance(pan, list)
                and len(pan) == 2
                and all(isinstance(v, (int, float)) and math.isfinite(v) for v in pan)
            ):
                p.canvas._pan = QtCore.QPointF(*pan)
            p.resolution_link.setChecked(bool(state.get("resolution_linked", False)))
            if p.resolution_link.isChecked():
                p._resolution_linker.manual_keep = bool(state.get("link_keep_size", False))
            p.canvas.viewChanged.emit()
            p.canvas.update()
            self.pending_pass = state.get("render_pass") or None
            if self.pending_pass == p.pass_menu.currentData():
                self.pending_pass = None
        finally:
            self.dirty = False
            self.restoring = False
            if p.resolution_link.isChecked():
                p._resolution_linker.changed()
                p._resolution_linker.timer.start()

    def restore_pass(self, enabled, selected):
        if self.pending_pass and self.pending_pass in enabled:
            index = self.panel.pass_menu.findData(self.pending_pass)
            if index >= 0:
                self.panel.pass_menu.blockSignals(True)
                self.panel.pass_menu.setCurrentIndex(index)
                self.panel.pass_menu.blockSignals(False)
                selected = self.pending_pass
                self.pending_pass = None
        return selected

    def close(self):
        self.timer.stop()
        self.save()
        hou.hipFile.removeEventCallback(self.scene_event)
