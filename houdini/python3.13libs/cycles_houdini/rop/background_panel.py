# SPDX-License-Identifier: Apache-2.0
"""Bounded background-job history with live progress and obvious Cancel controls."""

from pathlib import Path
import math
import time

import hou
from PySide6 import QtCore, QtWidgets
from . import background_render as bg

_window = None


class BackgroundRenders(QtWidgets.QDialog):
    def __init__(self):
        super().__init__(hou.qt.mainWindow())
        self.setWindowTitle("Cycles Background Renders")
        self.setObjectName("cycles_background_renders")
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(900, 340)
        self.before = self.older_than = None
        self.jobs = []
        self.values = {}
        self.root = None
        self.active_keys = ()
        layout = QtWidgets.QVBoxLayout(self)
        self.location = QtWidgets.QLabel()
        self.location.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.location)
        self.table = QtWidgets.QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(("State", "Scene / ROP", "Frame", "Progress", "Started"))
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self.selection_changed)
        layout.addWidget(self.table)
        self.message = QtWidgets.QLabel("No background renders in this project.")
        self.message.setWordWrap(True)
        self.message.setMinimumHeight(36)
        layout.addWidget(self.message)
        buttons = QtWidgets.QHBoxLayout()
        layout.addLayout(buttons)

        def button(label, callback):
            result = QtWidgets.QPushButton(label)
            result.clicked.connect(callback)
            buttons.addWidget(result)
            return result

        self.newest_button = button("Newest", self.newest)
        self.older_button = button("Older", self.older)
        self.refresh_button = button("Refresh", self.refresh)
        buttons.addStretch(1)
        self.folder_button = button("Open Folder", self.open_folder)
        self.log_button = button("Show Log", self.show_log)
        self.force_button = button("Force Stop", self.force_stop)
        self.cancel_button = button("Cancel Render", self.cancel)
        self.cancel_button.setToolTip("Stop the selected job. Completed images are preserved.")
        self.force_button.setToolTip("Terminate the selected owned process after requesting cancellation.")
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.update_jobs)
        self.timer.start(500)
        self.refresh()

    def selected(self):
        row = self.table.currentRow()
        return self.jobs[row] if 0 <= row < len(self.jobs) else None

    def refresh(self):
        selected = self.selected()
        selected_key = selected.key if selected else None
        root = bg._root()
        if self.root != root:
            self.before = None
        self.root = root
        page = bg._registry.page(self.root, before=self.before)
        self.jobs = list(page.jobs)
        self.older_than = page.older_than
        self.values = {job.key: job.status() for job in self.jobs}
        self.active_keys = tuple(job.key for job in bg.active_jobs())
        self.location.setText(str(self.root))
        self.table.setRowCount(len(self.jobs))
        for row, job in enumerate(self.jobs):
            value = self.values[job.key]
            cells = self.cells(value)
            for column, text in enumerate(cells):
                item = QtWidgets.QTableWidgetItem(text)
                item.setToolTip(value.get("scene", "") + "\n" + value["rop"] + "\n" + str(job.directory))
                self.table.setItem(row, column, item)
        self.newest_button.setEnabled(self.before is not None)
        self.older_button.setEnabled(self.older_than is not None)
        if self.jobs:
            row = next((i for i, job in enumerate(self.jobs) if job.key == selected_key), 0)
            self.table.selectRow(row)
        self.selection_changed()

    @staticmethod
    def cells(value):
        scene = Path(value.get("scene", "")).stem
        started = value.get("created")
        progress = value.get("progress", 0)
        if type(progress) not in (int, float) or not math.isfinite(progress):
            progress = 0
        return (
            value["state"].title(),
            (scene + " / " if scene else "") + value["rop"],
            str(value.get("frame", value.get("last_frame", "")) or ""),
            f"{100 * max(0, min(1, progress)):.1f}%",
            BackgroundRenders.started_text(started),
        )

    @staticmethod
    def started_text(started):
        try:
            return time.strftime("%d %b %H:%M", time.localtime(started)) if started else ""
        except (ValueError, OSError, OverflowError):
            return "Unknown"

    def update_jobs(self):
        # Scan bounded history only on explicit refresh/project/job-list changes.
        # Completed metadata is cached; the timer reads active reports only.
        if self.root != bg._root() or self.active_keys != tuple(job.key for job in bg.active_jobs()):
            self.refresh()
            return
        for row, job in enumerate(self.jobs):
            if self.values[job.key]["state"] in bg._terminal:
                continue
            value = self.values[job.key] = job.status()
            for column, text in enumerate(self.cells(value)):
                self.table.item(row, column).setText(text)
        self.selection_changed()

    def selection_changed(self):
        job = self.selected()
        value = self.values.get(job.key) if job else None
        active = bool(value and value["state"] not in bg._terminal)
        stopping = bool(active and (job.directory / "stop").is_file())
        self.cancel_button.setEnabled(active and not stopping)
        self.force_button.setVisible(bool(stopping and job.has_process_handle))
        self.folder_button.setEnabled(job is not None)
        self.log_button.setEnabled(bool(job and (job.directory / "render.log").is_file()))
        if value:
            self.message.setText(value.get("error") or value.get("message") or bg._job_label(value))
            self.message.setToolTip(str(job.directory))
        else:
            self.message.setText("No background renders in this project.")

    def cancel(self):
        job = self.selected()
        if job and self.values[job.key]["state"] not in bg._terminal:
            job.stop()
            self.update_jobs()

    def force_stop(self):
        job = self.selected()
        if job and (job.directory / "stop").is_file() and job.has_process_handle:
            job.force_stop()
            self.update_jobs()

    def open_folder(self):
        job = self.selected()
        if job:
            hou.ui.showInFileBrowser(str(job.directory))

    def show_log(self):
        job = self.selected()
        if job:
            hou.ui.showInFileBrowser(str(job.directory / "render.log"))

    def newest(self):
        self.before = None
        self.refresh()

    def older(self):
        if self.older_than is not None:
            self.before = self.older_than
            self.refresh()

    def closeEvent(self, event):
        self.timer.stop()
        super().closeEvent(event)


def _closed(*args):
    global _window
    _window = None


def show():
    global _window
    if _window is None:
        _window = BackgroundRenders()
        _window.destroyed.connect(_closed)
    else:
        _window.refresh()
    _window.show()
    _window.raise_()
    _window.activateWindow()
    return _window
