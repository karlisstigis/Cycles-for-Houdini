# SPDX-License-Identifier: Apache-2.0
"""Debounced, viewport-sized IPR buffers; display references never follow the buffer."""

import math
import weakref
import hou
from PySide6 import QtCore
from ..rop.output import scaled_dimensions


class ResolutionLink:
    owners = weakref.WeakValueDictionary()

    def __init__(self, panel):
        self.panel = panel
        self.manual_keep = False
        self.reference = None
        self.target = None
        self.key = None
        self.setting = None
        self.last_error = None
        self.keep_tooltip = panel.keep_image_size.toolTip()
        self.timer = QtCore.QTimer(panel)
        self.timer.setSingleShot(True)
        self.timer.setInterval(300)
        self.timer.timeout.connect(self.apply)
        panel.resolution_link.toggled.connect(self.toggle)
        panel.canvas.viewChanged.connect(self.changed)
        panel.snapshot_menu.currentIndexChanged.connect(self.history_changed)

    def restoring(self):
        return bool(getattr(getattr(self.panel, "_viewer_state", None), "restoring", False))

    def sync_reference(self):
        p = self.panel
        key = p._rop_identity
        if self.key != key:
            self.release()
            self.key = key
        reference = tuple(float(p._rop.evalParm(n)) for n in ("resx", "resy", "pixel_aspect")) if p._rop else None
        if reference and any(not math.isfinite(n) or n <= 0 for n in reference):
            reference = None
        setting = p._rop.evalParm("ipr_resolution_percent") if p._rop else None
        if reference != self.reference or setting != self.setting:
            self.target = None
        self.setting = setting
        self.reference = reference
        if p.resolution_link.isChecked():
            if key is not None:
                other = self.owners.get(key)
                if other is not None and other is not self:
                    other.panel.resolution_link.setChecked(False)
                self.owners[key] = self
            p.canvas.set_linked_reference(None if p.snapshot_menu.currentIndex() else reference)
            self.changed()

    def toggle(self, enabled):
        p = self.panel
        self.timer.stop()
        self.target = None
        if enabled:
            self.manual_keep = p.keep_image_size.isChecked()
            p.keep_image_size.setChecked(True)
            self.sync_reference()
        else:
            self.release()
            p.canvas.set_linked_reference(None)
            p.keep_image_size.setChecked(self.manual_keep)
        p.keep_image_size.setEnabled(not enabled)
        p.keep_image_size.setToolTip(
            "Display size is held while resolution and zoom are linked. Unlink to change the size lock."
            if enabled
            else self.keep_tooltip
        )
        p.update_render_controls()

    def changed(self):
        p = self.panel
        if p._disposed or p.snapshot_menu.currentIndex() or not p.resolution_link.isChecked() or not self.reference:
            return
        percent = p.canvas.zoom_percent()
        size = scaled_dimensions(self.reference[:2], percent)
        # Frame delivery also emits viewChanged. Only a real footprint change
        # can schedule work; stable reference dimensions prevent Fit feedback.
        if self.target and self.target[1] == size:
            return
        self.target = (percent, size)
        self.last_error = None
        if not self.restoring():
            self.timer.start()

    def apply(self):
        p = self.panel
        if p._disposed or self.restoring() or not p.resolution_link.isChecked() or not p._rop or not self.target:
            return
        if p.canvas._navigation is not None:
            self.timer.start()
            return
        if p._final_view is not None or p.snapshot_menu.currentIndex():
            return
        percent, size = self.target
        if not math.isfinite(percent) or not 0 < percent <= 400:
            p.notify("Linked resolution is outside the supported 0–400% viewer range. Reduce display zoom.")
            return
        try:
            parm = p._rop.parm("ipr_resolution_percent")
            current = scaled_dimensions(self.reference[:2], parm.eval())
            if current == size:
                return
            parm.set(percent)
            p.request_update()
        except (hou.Error, ValueError) as error:
            if str(error) != self.last_error:
                p.notify(str(error))
                self.last_error = str(error)
            return
        # A deliberate view-size change also works with scene Auto disabled.
        # Stop, Pause, snapshots and final rendering never implicitly resume.
        if (
            p._render_mode
            and p.entry
            and not p._stopped
            and not p.entry.paused
            and not p.entry.final_paused
            and not p.snapshot_menu.currentIndex()
        ):
            p.queue_render()

    def release(self):
        if self.owners.get(self.key) is self:
            self.owners.pop(self.key, None)

    def close(self):
        self.timer.stop()
        self.release()

    def history_changed(self, index):
        self.timer.stop()
        if self.panel.resolution_link.isChecked():
            self.panel.canvas.set_linked_reference(None if index else self.reference)
        if index == 0:
            self.target = None
            self.changed()
