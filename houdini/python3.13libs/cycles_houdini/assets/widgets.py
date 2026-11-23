# SPDX-License-Identifier: Apache-2.0
"""Asset browser widgets: thumbnail grid, catalog tree, filter proxy, details.

Qt only; no HOM. Thumbnails are decoded into QImages on one background
thread (QImage is reentrant) and converted to QPixmaps on the GUI thread
when painted. Colors come from the shared Cycles theme (Houdini theme roles,
Qt palette outside Houdini); see viewer.panel_theme.
"""

import os
import queue
import sys
import threading
from collections import OrderedDict

from PySide6 import QtCore, QtGui, QtWidgets

from ..viewer.viewer_theme import _blend, contrasting

RecordRole = QtCore.Qt.UserRole + 1
KeyRole = QtCore.Qt.UserRole + 2
TypeRole = QtCore.Qt.UserRole + 3
SortRole = QtCore.Qt.UserRole + 4

TYPE_LABELS = {
    "ALL": "All Types",
    "MATERIAL": "Materials",
    "OBJECT": "Objects",
    "COLLECTION": "Collections",
    "WORLD": "Worlds",
    "NODETREE": "Node Groups",
    "ACTION": "Actions",
    "BRUSH": "Brushes",
    "SCREEN": "Workspaces",
}
SINGULAR = {
    "MATERIAL": "Material",
    "OBJECT": "Object",
    "COLLECTION": "Collection",
    "WORLD": "World",
    "NODETREE": "Node Group",
}

# 24x24 stroke glyphs for assets without a stored preview.
_GLYPHS = {
    "MATERIAL": '<circle cx="12" cy="12" r="8.5"/><path d="M7.5 9.5a5 5 0 0 1 4-3"/>',
    "OBJECT": '<path d="m12 3 8 4.5v9L12 21l-8-4.5v-9z"/><path d="m4 7.5 8 4.5 8-4.5M12 12v9"/>',
    "COLLECTION": '<path d="M3 8h18v12H3zM5 5h14M7 2.5h10"/>',
    "WORLD": '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c3 3.2 3 14.8 0 18M12 3c-3 3.2-3 14.8 0 18"/>',
    "NODETREE": '<rect x="3" y="4" width="7" height="6" rx="1"/><rect x="14" y="14" width="7" height="6" rx="1"/>'
    '<path d="M10 7h2.5a2 2 0 0 1 2 2v3.5a2 2 0 0 0 2 2"/>',
}
_GENERIC_GLYPH = '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/>'


# ---------------------------------------------------------------- theme


def theme_colors(widget=None):
    """The shared Cycles theme: Houdini's theme roles inside Houdini, the Qt palette elsewhere.

    Same roles and same source as the render viewer (viewer_theme.read_theme);
    a panel that applied its chrome stores them on itself, so children reuse them.
    """
    from ..viewer.viewer_theme import colors, read_theme

    return dict(colors(widget) if widget is not None else read_theme())


# Selected and hovered items (grid thumbnails, catalog tree, library table, the importer's tree).
# Houdini 22 does not fill a selected entry with its bright accent (the `highlight` theme role,
# orange in the default theme, which menus and the render viewer use): its theme style draws a
# selected tab as `checkedSurface` blended half-way into `surfaceHighest` with a border
# `lighter(110)` (hutil.qt.pluto.parts.drawTabShape), and a hovered one as a faint veil; the
# Asset Gallery view outlines a selected item. The viewer theme's `active` is `checkedSurface`
# and `menu` is `surfaceHighest` (legacy scheme: ButtonPressedGradLow / MenuBG), so the asset
# browser derives its own tokens from them and leaves `selection` to the render viewer.
ITEM_SELECTED_ROLES = ("menu", "active")
ITEM_HOVER_VEIL = 0.06


def item_colors(theme, base="background"):
    """Asset-browser item tokens from the shared theme: item_selected (neutral selected surface),
    item_selected_border (thin outline), item_selected_hover, item_selected_text (the normal
    text colour, kept readable) and item_hover (a faint veil of the text colour over ``base``,
    quieter than the selection)."""
    surface, accent = (QtGui.QColor(theme[role]) for role in ITEM_SELECTED_ROLES)
    selected = _blend(surface, accent, 0.5)
    return dict(
        item_selected=selected,
        item_selected_border=selected.lighter(110),
        item_selected_hover=selected.lighter(108),
        item_selected_text=contrasting(QtGui.QColor(theme["text"]), selected, 4.5),
        item_hover=_blend(QtGui.QColor(theme[base]), QtGui.QColor(theme["text"]), ITEM_HOVER_VEIL),
    )


def asset_style(theme):
    """Asset-browser rules in the render viewer's vocabulary (12px-font pixels, scaled with the chrome).

    Shapes, radii and roles match viewer_layout.redesign_style: fields and
    buttons use $field/$button with a 1px $border and 4px radius, the thumbnail
    area uses $canvas like the render canvas, toolbars use $toolbar.
    """
    scope = 'QWidget[cycles_asset_panel="true"]'
    rules = """
    $scope QWidget[cycles_statusbar="true"] { background: $toolbar; border-top: 1px solid $divider; }
    $scope QLabel[cycles_caption="true"] { color: $muted; }
    $scope QLabel[cycles_banner="true"] { background: $toolbar; border-bottom: 1px solid $divider; padding: 4px 8px; }
    $scope QSplitter::handle { background: $divider; }
    $scope QListView[cycles_asset_grid="true"], $scope QWidget[cycles_asset_canvas="true"] {
        background: $canvas; border: none; }
    $scope QTreeView[cycles_asset_tree="true"] { background: $background; border: none; color: $text;
        selection-background-color: $item_selected; selection-color: $item_selected_text; }
    $scope QTreeView[cycles_asset_tree="true"]::item { padding: 2px 0px; }
    $scope QTreeView[cycles_asset_tree="true"]::item:hover { background: $item_hover; }
    $scope QTreeView[cycles_asset_tree="true"]::item:selected { background: $item_selected; color: $item_selected_text; }
    $scope QTreeView[cycles_asset_tree="true"]::item:selected:hover { background: $item_selected_hover; }
    $scope QWidget[cycles_asset_details="true"], $scope QWidget[cycles_asset_details="true"] QScrollArea,
    $scope QWidget[cycles_asset_details="true"] QScrollArea > QWidget > QWidget { background: $background; border: none; }
    $scope QPushButton, $scope QToolButton[cycles_action="true"] { background: $button; color: $text;
        border: 1px solid $border; border-radius: 4px; padding: 0px 8px; min-height: 22px; }
    $scope QPushButton:hover:enabled, $scope QToolButton[cycles_action="true"]:hover:enabled {
        background: $hover; border-color: $hover_border; }
    $scope QPushButton:pressed:enabled { background: $pressed; border-color: $active_border; color: $active_text; }
    $scope QPushButton[cycles_primary="true"] { font-weight: bold; }
    $scope QPushButton:disabled { background: $toolbar; color: $disabled; }
    $scope QSlider::groove:horizontal { height: 4px; background: $border; border-radius: 2px; }
    $scope QSlider::handle:horizontal { background: $thumb; width: 12px; height: 12px; margin: -4px 0px; border-radius: 6px; }
    $scope QSlider::handle:horizontal:disabled { background: $disabled; }
    $scope QTableView { background: $field; color: $text; gridline-color: $divider; border: 1px solid $border;
        selection-background-color: $item_selected; selection-color: $item_selected_text; }
    $scope QHeaderView::section { background: $toolbar; color: $muted; border: none;
        border-right: 1px solid $divider; padding: 2px 6px; }
    """
    from string import Template

    values = {key: value.name() for key, value in theme.items()}
    values.update({key: value.name() for key, value in item_colors(theme).items()})
    values["scope"] = scope
    return Template(rules).substitute(values)


def ui_scale():
    """Houdini's global UI scale when running in Houdini, else 1 (kept for callers outside the panel)."""
    host = sys.modules.get("hou")
    try:
        value = float(host.ui.globalScaleFactor()) if host is not None and host.isUIAvailable() else 1.0
    except Exception:
        value = 1.0
    return value if 0.25 <= value <= 8 else 1.0


def glyph_pixmap(id_type, size, color):
    from ..viewer.viewer_icons import rasterize

    body = _GLYPHS.get(id_type, _GENERIC_GLYPH)
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
        f'stroke="{QtGui.QColor(color).name()}" stroke-width="1.4" stroke-linecap="round" '
        f'stroke-linejoin="round">{body}</svg>'
    )
    return rasterize(svg, int(size))


# ---------------------------------------------------------------- pure filters


def search_words(text):
    return [word for word in (text or "").casefold().split() if word]


def matches_search(record, words):
    """Every word must occur in the name or in one of the tags (case-insensitive)."""
    if not words:
        return True
    haystack = [record.get("name", "").casefold()]
    haystack += [tag.casefold() for tag in (record.get("meta") or {}).get("tags", []) if isinstance(tag, str)]
    return all(any(word in text for text in haystack) for word in words)


def matches_type(record, id_type):
    return not id_type or id_type == "ALL" or record.get("id_type") == id_type


def catalog_id(record):
    return ((record.get("meta") or {}).get("catalog_id") or "").strip().lower()


def matches_catalog(record, spec):
    """spec: ("all",) | ("catalog", uuids) | ("unassigned", known_uuids)."""
    if not spec or spec[0] == "all":
        return True
    uuid = catalog_id(record)
    if spec[0] == "catalog":
        return bool(uuid) and uuid in spec[1]
    if spec[0] == "unassigned":
        return not uuid or uuid not in spec[1]
    return True


def subtree_uuids(node):
    result = set()
    stack = [node]
    while stack:
        item = stack.pop()
        result.update(uuid.strip().lower() for uuid in getattr(item, "uuids", ()) or ())
        stack.extend(getattr(item, "children", ()) or ())
    return result


def catalog_path(record, tree):
    uuid = catalog_id(record)
    node = tree.node_for_uuid(uuid) if uuid and tree is not None else None
    if node is not None:
        return node.path
    simple = (record.get("ch") or {}).get("catalog_simple_name") or ""
    if uuid:
        return (simple + " " if simple else "") + "(catalog not in this library's catalog file)"
    return "Unassigned"


def linked_text(linked):
    """Details line for a material linked in the scene (Link mode)."""
    if not linked:
        return ""
    text = "Linked as " + linked.get("path", "")
    if linked.get("stale"):
        text += "; the library file changed since it was linked: Reload from Library"
    return text


def importability_text(record):
    """One line for the details panel and tooltips."""
    from .importers import unsupported_reason

    reason = unsupported_reason(record)
    if reason:
        return reason
    ch = record.get("ch") or {}
    state = (ch.get("importable") or {}).get("state", "unknown")
    detail = (ch.get("importable") or {}).get("reason", "")
    id_type = record.get("id_type")
    if id_type in ("MATERIAL", "WORLD"):
        if state == "no":
            return "Not importable: " + (detail or "unsupported Blender nodes")
        if state == "yes":
            return "Imported successfully before"
        if id_type == "WORLD":
            return "Not imported yet; a simple HDRI world becomes an Environment Light, others a World Material"
        return "Not imported yet; unsupported Blender nodes are reported on import" + (
            " (" + detail + ")" if detail else ""
        )
    object_type = ch.get("object_type")
    if id_type == "OBJECT" and object_type and object_type != "MESH":
        return "Referenced as mesh only; this object is a " + object_type.lower() + " and may render nothing"
    if id_type == "COLLECTION":
        return "Referenced; only mesh objects in the collection render"
    return "Referenced (meshes only)"


# ---------------------------------------------------------------- thumbnails


class ThumbnailLoader(QtCore.QObject):
    """Decode thumbnails off the GUI thread; LRU of QImages and QPixmaps."""

    loaded = QtCore.Signal(str)

    def __init__(self, parent=None, capacity=384, max_size=256):
        super().__init__(parent)
        self.capacity = capacity
        self.max_size = max_size
        self._images = OrderedDict()
        self._pixmaps = OrderedDict()
        self._failed = set()
        self._pending = set()
        self._requests = []
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._results = queue.SimpleQueue()
        self._closed = False
        self._thread = None
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self.drain)

    def pixmap(self, path):
        """GUI thread. A cached QPixmap, or None after queueing a background load."""
        if not path or path in self._failed or self._closed:
            return None
        pixmap = self._pixmaps.get(path)
        if pixmap is not None:
            self._pixmaps.move_to_end(path)
            return pixmap
        image = self._images.get(path)
        if image is not None:
            self._images.move_to_end(path)
            pixmap = QtGui.QPixmap.fromImage(image)
            self._pixmaps[path] = pixmap
            while len(self._pixmaps) > self.capacity:
                self._pixmaps.popitem(last=False)
            return pixmap
        self.request(path)
        return None

    def request(self, path):
        if path in self._pending or self._closed:
            return
        self._pending.add(path)
        with self._wake:
            # Newest requests first: the rows currently being painted.
            self._requests.append(path)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="cycles-asset-thumbnails", daemon=True)
                self._thread.start()
            self._wake.notify()
        if not self._timer.isActive():
            self._timer.start()

    def _run(self):
        while True:
            with self._wake:
                while not self._requests and not self._closed:
                    self._wake.wait()
                if self._closed:
                    return
                path = self._requests.pop()
            image = QtGui.QImage()
            try:
                if os.path.isfile(path) and image.load(path):
                    if max(image.width(), image.height()) > self.max_size:
                        image = image.scaled(
                            self.max_size, self.max_size, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation
                        )
                    image = image.convertToFormat(QtGui.QImage.Format_ARGB32_Premultiplied)
            except Exception:
                image = QtGui.QImage()
            self._results.put((path, image))

    def drain(self):
        """GUI thread: accept decoded images and announce them."""
        done = []
        while True:
            try:
                path, image = self._results.get_nowait()
            except queue.Empty:
                break
            self._pending.discard(path)
            if image.isNull():
                self._failed.add(path)
            else:
                self._images[path] = image
                while len(self._images) > self.capacity:
                    self._images.popitem(last=False)
            done.append(path)
        if not self._pending:
            self._timer.stop()
        for path in done:
            self.loaded.emit(path)
        return len(done)

    def forget(self, path=None):
        """Drop cached entries (all, or one path) so changed files reload."""
        if path is None:
            self._images.clear()
            self._pixmaps.clear()
            self._failed.clear()
        else:
            self._images.pop(path, None)
            self._pixmaps.pop(path, None)
            self._failed.discard(path)

    def close(self):
        self._timer.stop()
        with self._wake:
            self._closed = True
            self._requests.clear()
            self._wake.notify_all()
        self._images.clear()
        self._pixmaps.clear()


def thumbnail_path(record):
    thumb = record.get("thumbnail")
    return thumb.get("url") or "" if isinstance(thumb, dict) else ""


# ---------------------------------------------------------------- model


def record_at(index):
    """The model's own record dict for a (proxy) index; never a QVariant copy."""
    while index.isValid():
        model = index.model()
        if isinstance(model, QtCore.QSortFilterProxyModel):
            index = model.mapToSource(index)
            continue
        record = getattr(model, "record", None)
        return record(index.row()) if callable(record) else None
    return None


class AssetModel(QtCore.QAbstractListModel):
    def __init__(self, parent=None, loader=None):
        super().__init__(parent)
        self._records = []
        self._rows_by_thumb = {}
        self._rows_by_key = {}
        self.loader = loader or ThumbnailLoader(self)
        self.loader.loaded.connect(self._thumbnail_loaded)

    def rowCount(self, parent=None):
        return 0 if parent is not None and parent.isValid() else len(self._records)

    def set_records(self, records):
        self.beginResetModel()
        self._records = list(records or ())
        self._rows_by_thumb.clear()
        self._rows_by_key.clear()
        for row, record in enumerate(self._records):
            path = thumbnail_path(record)
            if path:
                self._rows_by_thumb.setdefault(path, []).append(row)
            self._rows_by_key[(record.get("ch") or {}).get("key", "")] = row
        self.endResetModel()

    def records(self):
        return list(self._records)

    def record(self, row):
        return self._records[row] if 0 <= row < len(self._records) else None

    def row_for_key(self, key):
        return self._rows_by_key.get(key, -1)

    def update_importability(self, key, state, reason):
        row = self.row_for_key(key)
        record = self.record(row)
        if record is None:
            return False
        record.setdefault("ch", {})["importable"] = {"state": state, "reason": reason}
        index = self.index(row)
        self.dataChanged.emit(index, index)
        return True

    def thumbnail(self, row):
        record = self.record(row)
        path = thumbnail_path(record) if record else ""
        return self.loader.pixmap(path) if path else None

    def _thumbnail_loaded(self, path):
        for row in self._rows_by_thumb.get(path, ()):
            index = self.index(row)
            self.dataChanged.emit(index, index, [QtCore.Qt.DecorationRole])

    def data(self, index, role=QtCore.Qt.DisplayRole):
        record = self.record(index.row()) if index.isValid() else None
        if record is None:
            return None
        if role == QtCore.Qt.DisplayRole:
            return record.get("name", "")
        if role == SortRole:
            return record.get("name", "").casefold()
        if role == RecordRole:
            return record
        if role == KeyRole:
            return (record.get("ch") or {}).get("key", "")
        if role == TypeRole:
            return record.get("id_type", "")
        if role == QtCore.Qt.DecorationRole:
            return self.thumbnail(index.row())
        if role == QtCore.Qt.ToolTipRole:
            lines = [record.get("name", ""), SINGULAR.get(record.get("id_type"), record.get("id_type", "").title())]
            lines.append(importability_text(record))
            tags = (record.get("meta") or {}).get("tags") or []
            if tags:
                lines.append("Tags: " + ", ".join(tags))
            return "\n".join(lines)
        return None


class AssetFilterProxy(QtCore.QSortFilterProxyModel):
    """Search (name + tags, AND of words), type and catalog filters; sorted by name."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._words = []
        self._type = "ALL"
        self._catalog = ("all",)
        self.setSortRole(SortRole)
        self.setDynamicSortFilter(True)
        self.sort(0)

    def _refilter(self, name, value):
        # Qt 6.10 deprecates invalidateFilter(); older Qt (Houdini) lacks endFilterChange().
        modern = hasattr(self, "endFilterChange") and hasattr(self, "beginFilterChange")
        if modern:
            self.beginFilterChange()
        setattr(self, name, value)
        if modern:
            self.endFilterChange()
        else:
            self.invalidateFilter()

    def set_search(self, text):
        self._refilter("_words", search_words(text))

    def set_type(self, id_type):
        self._refilter("_type", id_type or "ALL")

    def set_catalog(self, spec):
        self._refilter("_catalog", spec or ("all",))

    def filterAcceptsRow(self, row, parent):
        record = self.sourceModel().record(row)
        return (
            record is not None
            and matches_type(record, self._type)
            and matches_catalog(record, self._catalog)
            and matches_search(record, self._words)
        )

    def record(self, row):
        index = self.index(row, 0)
        return record_at(index)


# ---------------------------------------------------------------- grid


class AssetDelegate(QtWidgets.QStyledItemDelegate):
    def __init__(self, view):
        super().__init__(view)
        self.view = view
        self._glyphs = {}

    def _placeholder(self, id_type, size, color):
        key = (id_type, size, color.rgba())
        if key not in self._glyphs:
            if len(self._glyphs) > 64:
                self._glyphs.clear()
            self._glyphs[key] = glyph_pixmap(id_type, size, color)
        return self._glyphs[key]

    def sizeHint(self, option, index):
        return self.view.cell_size()

    def paint(self, painter, option, index):
        record = record_at(index)
        if record is None:
            return
        theme = self.view.theme()
        painter.save()
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        painter.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, True)
        rect = option.rect.adjusted(2, 2, -2, -2)
        selected = bool(option.state & QtWidgets.QStyle.State_Selected)
        hovered = bool(option.state & QtWidgets.QStyle.State_MouseOver)
        radius = max(2.0, self.view.thumbnail_size() * 0.04)
        items = item_colors(theme, "canvas")
        if selected or hovered:
            # A neutral selected surface with a thin outline; hover only a faint veil (item_colors).
            if selected:
                fill = items["item_selected_hover" if hovered else "item_selected"]
                painter.setPen(QtGui.QPen(items["item_selected_border"], 1))
            else:
                fill = items["item_hover"]
                painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(fill)
            painter.drawRoundedRect(QtCore.QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        size = self.view.thumbnail_size()
        line = option.fontMetrics.lineSpacing()
        square = QtCore.QRect(rect.x() + (rect.width() - size) // 2, rect.y() + 3, size, size)
        from .importers import IMPORTABLE_TYPES

        unsupported = record.get("id_type") not in IMPORTABLE_TYPES
        if unsupported:
            painter.setOpacity(0.55)
        pixmap = index.data(QtCore.Qt.DecorationRole)
        if isinstance(pixmap, QtGui.QPixmap) and not pixmap.isNull():
            scaled = pixmap.size().scaled(square.size(), QtCore.Qt.KeepAspectRatio)
            target = QtCore.QRect(QtCore.QPoint(0, 0), scaled)
            target.moveCenter(square.center())
            painter.drawPixmap(target, pixmap)
        else:
            painter.setPen(QtCore.Qt.NoPen)
            # Placeholder tile: a themed field on the canvas, glyph tinted like the viewer's icons.
            painter.setBrush(theme["field"])
            painter.drawRoundedRect(QtCore.QRectF(square), radius, radius)
            glyph = max(12, size // 2)
            ratio = painter.device().devicePixelRatioF() if painter.device() else 1.0
            tint = contrasting(theme["muted"], theme["field"], 3.0)
            source = self._placeholder(record.get("id_type"), round(glyph * ratio), tint)
            target = QtCore.QRect(0, 0, glyph, glyph)
            target.moveCenter(square.center())
            painter.drawPixmap(target, source)
        painter.setOpacity(1.0)
        painter.save()
        self._paint_badge(painter, record, square, theme, hovered)
        painter.restore()  # the badge's bold font must not leak into the label
        painter.setFont(option.font)
        text_rect = QtCore.QRect(rect.x() + 2, square.bottom() + 3, rect.width() - 4, line)
        painter.setPen(items["item_selected_text"] if selected else theme["text"])
        name = option.fontMetrics.elidedText(record.get("name", ""), QtCore.Qt.ElideRight, text_rect.width())
        painter.drawText(text_rect, QtCore.Qt.AlignHCenter | QtCore.Qt.AlignVCenter, name)
        painter.restore()

    def _paint_badge(self, painter, record, square, theme, hovered):
        from .importers import IMPORTABLE_TYPES

        ch = record.get("ch") or {}
        state = (ch.get("importable") or {}).get("state", "unknown")
        id_type = record.get("id_type")
        mark = None
        if id_type not in IMPORTABLE_TYPES or state == "no":
            mark, color = "!", theme["warning"]
        elif id_type in ("MATERIAL", "WORLD"):
            mark, color = (chr(0x2713), theme["progress"]) if state == "yes" else ("?", theme["muted"])
        elif id_type == "OBJECT" and ch.get("object_type") not in (None, "MESH"):
            mark, color = "!", theme["warning"]
        font = QtGui.QFont(painter.font())
        if mark:
            # Theme colours, kept legible on the thumbnail canvas of any theme.
            color = contrasting(QtGui.QColor(color), theme["canvas"], 3.0)
            diameter = max(12, square.width() // 6)
            badge = QtCore.QRectF(square.right() - diameter - 3, square.top() + 3, diameter, diameter)
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(badge)
            font.setPixelSize(max(8, int(diameter * 0.7)))
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(contrasting(theme["text"], color, 4.5))
            painter.drawText(badge, QtCore.Qt.AlignCenter, mark)
        if hovered and id_type in ("OBJECT", "COLLECTION") and square.width() >= 64:
            font.setBold(False)
            font.setPixelSize(max(8, square.width() // 10))
            painter.setFont(font)
            metrics = QtGui.QFontMetrics(font)
            hint = QtCore.QRectF(
                square.left(), square.bottom() - metrics.height() - 2, square.width(), metrics.height()
            )
            painter.setPen(QtCore.Qt.NoPen)
            shade = QtGui.QColor(theme["background"])
            shade.setAlphaF(0.75)
            painter.setBrush(shade)
            painter.drawRect(hint)
            painter.setPen(theme["muted"])
            painter.drawText(hint, QtCore.Qt.AlignCenter, "meshes only")


class AssetGridView(QtWidgets.QListView):
    """Icon-mode list with uniform cells; drag is delegated to a callback."""

    activatedRecord = QtCore.Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._size = 112
        self._theme = None
        self.drag_handler = None
        self.setProperty("cycles_asset_grid", True)
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.setViewMode(QtWidgets.QListView.IconMode)
        self.setMovement(QtWidgets.QListView.Static)
        self.setResizeMode(QtWidgets.QListView.Adjust)
        self.setWrapping(True)
        self.setUniformItemSizes(True)
        self.setLayoutMode(QtWidgets.QListView.Batched)
        self.setBatchSize(200)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.setMouseTracking(True)
        self.setDragEnabled(False)
        self.setDragDropMode(QtWidgets.QAbstractItemView.NoDragDrop)
        self.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
        self.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.setItemDelegate(AssetDelegate(self))
        self.doubleClicked.connect(self._double_clicked)
        self.set_thumbnail_size(self._size)

    def theme(self):
        if self._theme is None:
            self._theme = theme_colors(self)
        return self._theme

    def set_theme(self, theme):
        self._theme = theme
        self.viewport().update()

    def thumbnail_size(self):
        return self._size

    def cell_size(self):
        line = self.fontMetrics().lineSpacing()
        return QtCore.QSize(self._size + 12, self._size + line + 12)

    def set_thumbnail_size(self, size):
        self._size = int(size)
        self.setGridSize(self.cell_size())
        self.setIconSize(QtCore.QSize(self._size, self._size))
        self.doItemsLayout()

    def set_drag_enabled(self, enabled):
        self.setDragEnabled(bool(enabled))
        self.setDragDropMode(
            QtWidgets.QAbstractItemView.DragOnly if enabled else QtWidgets.QAbstractItemView.NoDragDrop
        )

    def current_record(self):
        index = self.currentIndex()
        return record_at(index)

    def startDrag(self, actions):
        record = self.current_record()
        if self.drag_handler is not None and record is not None:
            pixmap = self.currentIndex().data(QtCore.Qt.DecorationRole)
            self.setProperty("cycles_drag_pixmap", pixmap if isinstance(pixmap, QtGui.QPixmap) else None)
            self.drag_handler(self, record)

    def _double_clicked(self, index):
        record = record_at(index)
        if record is not None:
            self.activatedRecord.emit(record)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QtCore.QEvent.FontChange, QtCore.QEvent.StyleChange):
            self.set_thumbnail_size(self._size)
        if event.type() == QtCore.QEvent.PaletteChange:
            self._theme = None


# ---------------------------------------------------------------- catalog tree

PathRole = QtCore.Qt.UserRole + 10
SpecRole = QtCore.Qt.UserRole + 11


class CatalogTreeWidget(QtWidgets.QTreeWidget):
    """'All', the library's catalog hierarchy, and 'Unassigned'."""

    filterChanged = QtCore.Signal(object)

    ALL = "\0all"
    UNASSIGNED = "\0unassigned"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setColumnCount(2)
        self.setHeaderHidden(True)
        self.setRootIsDecorated(True)
        self.setUniformRowHeights(True)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.header().setStretchLastSection(False)
        self.header().setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        self.header().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        self.setProperty("cycles_asset_tree", True)
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        self._theme = None
        self._known = frozenset()
        self.itemSelectionChanged.connect(self._selection_changed)

    def set_theme(self, theme):
        """Count column in the theme's secondary text colour."""
        self._theme = theme
        brush = QtGui.QBrush(theme["muted"])
        iterator = QtWidgets.QTreeWidgetItemIterator(self)
        while iterator.value() is not None:
            iterator.value().setForeground(1, brush)
            iterator += 1

    def populate(self, tree, records):
        """Rebuild, keeping the selected catalog path when it still exists."""
        current = self.current_path()
        records = list(records or ())
        root = getattr(tree, "root", None)
        self._known = frozenset(subtree_uuids(root)) if root is not None else frozenset()
        ids = [catalog_id(record) for record in records]
        blocker = QtCore.QSignalBlocker(self)
        self.clear()
        everything = self._item(None, "All", self.ALL, ("all",), len(records))
        if root is not None:
            for child in root.children:
                self._add(everything, child, ids)
        unassigned = sum(1 for uuid in ids if not uuid or uuid not in self._known)
        self._item(None, "Unassigned", self.UNASSIGNED, ("unassigned", self._known), unassigned)
        everything.setExpanded(True)
        del blocker
        self.select_path(current or self.ALL)

    def _add(self, parent, node, ids):
        uuids = frozenset(subtree_uuids(node))
        count = sum(1 for uuid in ids if uuid in uuids)
        item = self._item(parent, node.name or node.path.rsplit("/", 1)[-1], node.path, ("catalog", uuids), count)
        item.setToolTip(0, node.path)
        for child in node.children:
            self._add(item, child, ids)
        return item

    def _item(self, parent, text, path, spec, count):
        item = QtWidgets.QTreeWidgetItem([text, str(count) if count else ""])
        item.setData(0, PathRole, path)
        item.setData(0, SpecRole, spec)
        item.setTextAlignment(1, QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        muted = (self._theme or {}).get("muted")
        item.setForeground(
            1, QtGui.QBrush(muted) if muted is not None else self.palette().brush(QtGui.QPalette.PlaceholderText)
        )
        if parent is None:
            self.addTopLevelItem(item)
        else:
            parent.addChild(item)
        return item

    def current_path(self):
        items = self.selectedItems()
        return items[0].data(0, PathRole) if items else None

    def current_spec(self):
        items = self.selectedItems()
        return items[0].data(0, SpecRole) if items else ("all",)

    def select_path(self, path):
        iterator = QtWidgets.QTreeWidgetItemIterator(self)
        target = None
        while iterator.value() is not None:
            item = iterator.value()
            if item.data(0, PathRole) == path:
                target = item
                break
            iterator += 1
        if target is None:
            target = self.topLevelItem(0)
        if target is not None:
            parent = target.parent()
            while parent is not None:
                parent.setExpanded(True)
                parent = parent.parent()
            self.setCurrentItem(target)
            target.setSelected(True)
        self._selection_changed()

    def _selection_changed(self):
        self.filterChanged.emit(self.current_spec())


# ---------------------------------------------------------------- details


class DetailsPanel(QtWidgets.QWidget):
    importRequested = QtCore.Signal(object)
    assignRequested = QtCore.Signal(object)
    openRequested = QtCore.Signal(object)
    reloadRequested = QtCore.Signal(object)

    FIELDS = (
        ("name", "Name"),
        ("type", "Type"),
        ("file", "Source File"),
        ("catalog", "Catalog"),
        ("description", "Description"),
        ("author", "Author"),
        ("license", "License"),
        ("copyright", "Copyright"),
        ("tags", "Tags"),
        ("importable", "Import"),
        ("linked", "In Scene"),
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self._record = None
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        self.preview = QtWidgets.QLabel()
        self.preview.setAlignment(QtCore.Qt.AlignCenter)
        self.preview.setMinimumHeight(64)
        layout.addWidget(self.preview)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        body = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(body)
        form.setContentsMargins(0, 0, 0, 0)
        form.setRowWrapPolicy(QtWidgets.QFormLayout.WrapLongRows)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
        self.labels = {}
        for key, title in self.FIELDS:
            value = QtWidgets.QLabel()
            value.setWordWrap(True)
            value.setTextFormat(QtCore.Qt.PlainText)
            value.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            value.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
            caption = QtWidgets.QLabel(title)
            caption.setProperty("cycles_caption", True)
            form.addRow(caption, value)
            self.labels[key] = value
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)
        self.setProperty("cycles_asset_details", True)
        self.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        self.import_button = QtWidgets.QPushButton("Import")
        self.import_button.setProperty("cycles_primary", True)
        self.assign_button = QtWidgets.QPushButton("Assign to Selected Objects")
        self.open_button = QtWidgets.QPushButton("Open Blend File")
        self.reload_button = QtWidgets.QPushButton("Reload from Library")
        self.reload_button.setToolTip("Re-export the linked material from its .blend file (one undo step)")
        for button, signal in (
            (self.import_button, self.importRequested),
            (self.assign_button, self.assignRequested),
            (self.reload_button, self.reloadRequested),
            (self.open_button, self.openRequested),
        ):
            button.clicked.connect(lambda _=False, s=signal: self._record is not None and s.emit(self._record))
            layout.addWidget(button)
        self.set_record(None)

    def record(self):
        return self._record

    def set_theme(self, theme, scale=1.0):
        """Chrome colours come from the panel stylesheet; only scaled metrics are set here."""
        self.preview.setMinimumHeight(round(64 * scale))
        layout = self.layout()
        margin = round(8 * scale)
        layout.setContentsMargins(margin, margin, margin, margin)
        layout.setSpacing(round(4 * scale))

    def set_record(self, record, *, tree=None, pixmap=None, linked=None):
        """`linked`: {"path", "stale"} when the material is linked in the scene, else None."""
        from .importers import unsupported_reason

        self._record = record
        if record is None:
            for label in self.labels.values():
                label.clear()
            self.labels["name"].setText("Select an asset")
            self.preview.clear()
            for button in (self.import_button, self.assign_button, self.open_button):
                button.setEnabled(False)
            self.assign_button.setVisible(False)
            self.reload_button.setVisible(False)
            return
        meta = record.get("meta") or {}
        ch = record.get("ch") or {}
        values = {
            "name": record.get("name", ""),
            "type": SINGULAR.get(record.get("id_type"), record.get("id_type", "").title())
            + (" (" + ch["object_type"].title() + ")" if ch.get("object_type") else ""),
            "file": (record.get("files") or [""])[0],
            "catalog": catalog_path(record, tree),
            "description": meta.get("description", ""),
            "author": meta.get("author", ""),
            "license": meta.get("license", ""),
            "copyright": meta.get("copyright", ""),
            "tags": ", ".join(meta.get("tags") or []),
            "importable": importability_text(record),
            "linked": linked_text(linked),
        }
        for key, value in values.items():
            self.labels[key].setText(value)
        self.labels["file"].setToolTip(ch.get("source", ""))
        if isinstance(pixmap, QtGui.QPixmap) and not pixmap.isNull():
            width = max(64, self.width() - 16)
            self.preview.setPixmap(
                pixmap.scaled(width, min(width, 256), QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
            )
        else:
            self.preview.clear()
        importable = not unsupported_reason(record)
        self.import_button.setEnabled(importable)
        self.import_button.setToolTip(unsupported_reason(record))
        is_material = record.get("id_type") == "MATERIAL"
        self.assign_button.setVisible(is_material)
        self.assign_button.setEnabled(is_material)
        self.reload_button.setVisible(bool(linked))
        self.reload_button.setEnabled(bool(linked))
        self.reload_button.setText(
            "Reload from Library" + (" (source changed)" if linked and linked.get("stale") else "")
        )
        self.open_button.setEnabled(bool(ch.get("source")))
