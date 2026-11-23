# SPDX-License-Identifier: Apache-2.0
"""EXPERIMENTAL drag and drop from the asset browser (default off).

UNTESTED IN HOUDINI 22. No `scripts/externaldragdrop.py` hook is installed.
The drag is a plain Qt QDrag carrying a text/uri-list of the source .blend
file plus a private MIME type. After the drag finishes (QDrag.exec returns on
the GUI thread) the pane under the cursor decides what happens:

- Network Editor: import into its current network at the cursor position.
- Scene Viewer with a material: assign it to the object under the cursor.
- Anything else: nothing.

Every HOM call is guarded with hasattr/try and falls back to a plain import
(network editor) or to doing nothing (scene viewer).
"""

import json

MIME_TYPE = "application/x-cycles-houdini-blender-asset"
ROUTES = ("off", "experimental", "experimental_unaccepted")


def mime_payload(record):
    ch = record.get("ch") or {}
    return json.dumps(
        {
            "key": ch.get("key", ""),
            "source": ch.get("source", ""),
            "name": record.get("name", ""),
            "id_type": record.get("id_type", ""),
        },
        ensure_ascii=False,
    ).encode("utf-8")


def make_mime_data(record):
    from PySide6 import QtCore

    data = QtCore.QMimeData()
    source = (record.get("ch") or {}).get("source") or ""
    if source:
        data.setUrls([QtCore.QUrl.fromLocalFile(source)])
    data.setData(MIME_TYPE, QtCore.QByteArray(mime_payload(record)))
    data.setText(record.get("name", ""))
    return data


def _call(obj, name, *args):
    method = getattr(obj, name, None)
    if not callable(method):
        return None
    try:
        return method(*args)
    except Exception:
        return None


def _network_position(pane):
    position = _call(pane, "cursorPosition")
    if position is not None:
        return position
    # Fallback: map the global cursor into the editor (API shape unverified).
    from PySide6 import QtGui

    geometry = _call(pane, "qtScreenGeometry")
    cursor = QtGui.QCursor.pos()
    if geometry is not None and hasattr(pane, "posFromScreen"):
        try:
            import hou

            return pane.posFromScreen(hou.Vector2(cursor.x() - geometry.x(), geometry.bottom() - cursor.y()))
        except Exception:
            return None
    return None


def _object_under_cursor(pane):
    """Hit-test the Scene Viewer viewport under the cursor; None when unknown."""
    import hou
    from PySide6 import QtGui

    viewport = _call(pane, "curViewport")
    if viewport is None or not hasattr(viewport, "queryNodeAtPixel"):
        return None
    geometry = _call(pane, "qtScreenGeometry")
    cursor = QtGui.QCursor.pos()
    if geometry is None:
        return None
    # HOM viewport pixels have their origin at the lower left (assumed).
    x, y = cursor.x() - geometry.x(), geometry.bottom() - cursor.y()
    size = _call(viewport, "size")
    if size and len(size) == 4:
        x, y = x - size[0], y - size[1]
    node = _call(viewport, "queryNodeAtPixel", int(x), int(y))
    while node is not None and node.type().category() != hou.objNodeTypeCategory():
        node = node.parent()
    return node


def handle_drop(record, *, importer=None, assign=None):
    """Resolve the pane under the cursor and act. Returns a short status text."""
    import hou

    from . import importers

    importer = importer or importers.import_asset
    assign = assign or importers.assign_material
    ui = getattr(hou, "ui", None)
    pane = _call(ui, "paneTabUnderCursor")
    if pane is None:
        return ""
    pane_type = _call(pane, "type")
    types = getattr(hou, "paneTabType", None)
    if pane_type is not None and pane_type == getattr(types, "NetworkEditor", object()):
        network = _call(pane, "pwd")
        position = _network_position(pane)
        mode = importers.effective_mode(record.get("id_type"), "link")
        if mode is None:
            raise importers.AssetImportError(importers.unsupported_reason(record))
        try:
            nodes = importer(record, mode=mode, network=network, position=position)
        except importers.AssetImportError:
            raise
        except Exception:
            if network is None and position is None:
                raise
            # Plain import when the drop location could not be used.
            nodes = importer(record, mode=mode)
        return "Imported " + ", ".join(node.path() for node in nodes) if nodes else ""
    if pane_type is not None and pane_type == getattr(types, "SceneViewer", object()):
        if record.get("id_type") != "MATERIAL":
            return ""
        target = _object_under_cursor(pane)
        if target is None:
            return "No object under the cursor (experimental drop)"
        material, nodes = assign(record, [target])
        return "Assigned " + material.path() + " to " + ", ".join(node.path() for node in nodes)
    return ""


def begin_drag(widget, record, route="off", *, on_result=None, importer=None, assign=None):
    """Start an EXPERIMENTAL drag. Returns the Qt drop action, or None when disabled.

    `on_result(text, error)` reports the outcome; errors never propagate out of
    the drag so a failed drop cannot break the panel's event handling.
    """
    if route not in ROUTES or route == "off" or record is None:
        return None
    from PySide6 import QtCore, QtGui, QtWidgets

    drag = QtGui.QDrag(widget)
    drag.setMimeData(make_mime_data(record))
    icon = widget.property("cycles_drag_pixmap") if widget is not None else None
    if isinstance(icon, QtGui.QPixmap) and not icon.isNull():
        drag.setPixmap(icon)
    action = drag.exec(QtCore.Qt.CopyAction | QtCore.Qt.LinkAction, QtCore.Qt.CopyAction)
    if route == "experimental_unaccepted" and action != QtCore.Qt.IgnoreAction:
        if on_result:
            on_result("Drop handled by the target (experimental)", None)
        return action
    # Dropped back onto our own panel: nothing to do.
    under = QtWidgets.QApplication.widgetAt(QtGui.QCursor.pos())
    top = widget.window() if widget is not None else None
    if under is not None and top is not None and under.window() is top and _inside(under, widget):
        return action
    try:
        text = handle_drop(record, importer=importer, assign=assign)
        error = None
    except Exception as exc:  # reported, never raised out of the drag
        text, error = "", str(exc) or type(exc).__name__
    if on_result:
        on_result(text, error)
    return action


def _inside(child, ancestor):
    panel = ancestor
    while panel is not None and getattr(panel, "_cycles_asset_browser", None) is None:
        panel = panel.parentWidget()
    panel = panel or ancestor
    while child is not None:
        if child is panel:
            return True
        child = child.parentWidget()
    return False
