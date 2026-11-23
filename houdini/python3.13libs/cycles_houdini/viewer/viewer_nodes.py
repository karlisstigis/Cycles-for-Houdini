# SPDX-License-Identifier: Apache-2.0
"""Scene node dropdowns, refreshed on demand without cooking or polling."""

import hou
from PySide6 import QtCore
from .viewer_chrome import ViewerCombo


class NodeMenu(ViewerCombo):
    nodeSelected = QtCore.Signal(object)

    def __init__(self, category, type_name, placeholder):
        super().__init__()
        self.category, self.type_name, self.placeholder = category, type_name, placeholder
        self.activated.connect(self._selected)
        self.currentIndexChanged.connect(self._update_tooltip)
        self.view().setMouseTracking(True)
        self.refresh()

    def _update_tooltip(self, *_):
        self.setToolTip(self.currentData() or self.placeholder)

    def _label(self, path):
        return path.rsplit("/", 1)[-1] if path else self.placeholder

    def refresh(self):
        path = self.currentData()
        # Follow renames using identity, and rebuild only when the menu opens.
        identity = self.currentData(QtCore.Qt.UserRole + 1)
        node = hou.nodeBySessionId(identity) if identity is not None else None
        if node is not None:
            path = node.path()
        node_type = hou.nodeType(self.category, self.type_name)
        nodes = (
            sorted(node_type.instances(), key=lambda n: (n.name().casefold(), n.path().casefold())) if node_type else []
        )
        self.blockSignals(True)
        try:
            self.clear()
            for node in nodes:
                self.addItem(node.name(), node.path())
                self.setItemData(self.count() - 1, node.sessionId(), QtCore.Qt.UserRole + 1)
                self.setItemData(self.count() - 1, node.path(), QtCore.Qt.ToolTipRole)
            index = self.findData(path) if path else -1
            if index < 0:
                self.insertItem(0, self._label(path), path)
                self.setItemData(0, path, QtCore.Qt.ToolTipRole)
                self.model().item(0).setEnabled(False)
                index = 0
            self.setCurrentIndex(index)
        finally:
            self.blockSignals(False)
        self._update_tooltip()

    def set_path(self, path):
        self.blockSignals(True)
        try:
            index = self.findData(path)
            if index < 0:
                self.addItem(self._label(path), path)
                index = self.count() - 1
            self.setItemText(index, self._label(path))
            self.setItemData(index, path, QtCore.Qt.ToolTipRole)
            self.setCurrentIndex(index)
            node = hou.node(path) if path else None
            self.setItemData(index, node.sessionId() if node else None, QtCore.Qt.UserRole + 1)
        finally:
            self.blockSignals(False)
        self._update_tooltip()

    def _selected(self, index):
        path = self.itemData(index)
        node = hou.node(path) if path else None
        if node is not None:
            self.nodeSelected.emit(node)

    def showPopup(self):
        self.refresh()
        super().showPopup()
