# SPDX-License-Identifier: Apache-2.0
"""Dockable Blender asset browser panel (Python Panel root widget).

Host-thread rule: HOM only on the GUI thread. Indexing runs in
`indexer.IndexJob` threads and is drained by a QTimer here; Blender discovery
and launches run in short background tasks whose results are also delivered
by that timer. The library folder is never written to.
"""

import copy
import os
import queue
import subprocess
import sys
import threading

from PySide6 import QtCore, QtGui, QtWidgets

from ..viewer.viewer_chrome import ViewerCombo, ViewerToolButton, cluster, dimensions
from ..viewer.viewer_toolbar import ToolbarRow
from . import settings as settings_module
from .widgets import (
    TYPE_LABELS,
    AssetFilterProxy,
    AssetGridView,
    AssetModel,
    CatalogTreeWidget,
    DetailsPanel,
    asset_style,
    record_at,
)

# Property scoping the shared Cycles chrome (viewer_layout.scoped_style) to this panel.
PANEL_SCOPE = "cycles_asset_panel"

BLENDER_MISSING = (
    "Blender not found: showing cached thumbnails and catalogs. Install Blender or set "
    "CYCLES_HOUDINI_BLENDER to its executable, then Refresh."
)
LISTING_MISSING = (
    "This library has no Blender asset listing yet. The Listing provider only reads an existing "
    "_asset-library-meta.json: switch the provider to Blender, or generate a listing (this writes into the "
    "library folder)."
)
GENERATE_LISTING_WARNING = (
    "Generate a Blender asset listing for '{name}'?\n\n"
    "This WRITES into the library folder {path}:\n"
    "  - _asset-library-meta.json\n"
    "  - a _v1/ folder\n"
    "  - a <file>_thumbnails/ folder beside every .blend file\n\n"
    "It runs Blender's official 'asset_listing generate' command and needs Blender 5.2 or newer. "
    "It may take several minutes and cannot be cancelled."
)
NO_LIBRARIES = "No libraries yet: add a folder or import from Blender."
DISABLED = (
    "The Blender asset browser is not enabled in this installation. Run Cycles Setup again and select "
    "the 'Asset browser' component of the Blender bridge."
)
PROVIDER_CHOICES = (
    ("worker", "Blender", "Read .blend files with Blender in the background (default). Results are cached."),
    (
        "listing",
        "Listing",
        "Read the library's existing Blender asset listing (_asset-library-meta.json). Never launches Blender.",
    ),
)


def _blender_ocio():
    """CYCLES_HOUDINI_BLENDER_OCIO (Houdini's variable when set there), or None."""
    try:
        import hou

        value = hou.getenv("CYCLES_HOUDINI_BLENDER_OCIO")
    except Exception:
        value = None
    return value or os.environ.get("CYCLES_HOUDINI_BLENDER_OCIO") or None


class CoreBackend:
    """Default bindings to the core, worker and importer modules (imported lazily)."""

    def _assets(self, name):
        import importlib

        return importlib.import_module(__package__ + "." + name)

    def enabled(self):
        import importlib

        package = sys.modules.get(__package__) or importlib.import_module(__package__)
        check = getattr(package, "enabled", None)
        if callable(check):
            return bool(check())
        value = os.environ.get("CYCLES_HOUDINI_ASSET_BROWSER")
        return value is None or value.strip().lower() not in ("0", "off", "false", "disabled")

    def settings_path(self):
        return settings_module.settings_path()

    def read_libraries(self):
        libraries = self._assets("libraries")
        read = getattr(libraries, "read_registry", None)
        if callable(read):
            return read()
        return libraries.load_libraries(), ""

    def save_libraries(self, items):
        self._assets("libraries").save_libraries(items)

    def new_library(self, name, path):
        return self._assets("libraries").Library(name=name, path=path)

    def merge_libraries(self, existing, dumped):
        return self._assets("libraries").merge_blender_libraries(existing, dumped)

    def make_provider(self, library, kind):
        return self._assets("providers").make_provider(library, kind)

    def make_job(self, provider):
        return self._assets("indexer").IndexJob(provider)

    def import_asset(self, record, **options):
        return self._assets("importers").import_asset(record, **options)

    def assign_material(self, record, **options):
        return self._assets("importers").assign_material(record, **options)

    def material_snapshot(self, record, **options):
        """Host thread (HOM): resolved asset root, Blender discovery inputs, OCIO config."""
        return self._assets("importers").material_snapshot(record, **options)

    def prepare_material(self, snapshot, cancelled):
        """Background thread, no HOM: Blender export; packed images to <blend>/tex."""
        return self._assets("importers").prepare_material(snapshot, cancelled)

    def append_snapshot(self, record, **options):
        """Host thread (HOM): converter snapshot for appending an object or collection."""
        return self._assets("importers").append_snapshot(record, **options)

    def prepare_append(self, snapshot, cancelled):
        """Background thread, no HOM: Blender export and geometry conversion."""
        return self._assets("importers").prepare_append(snapshot, cancelled)

    def world_snapshot(self, record, **options):
        """Host thread (HOM): as material_snapshot, for a world."""
        return self._assets("importers").world_snapshot(record, **options)

    def prepare_world(self, snapshot, cancelled):
        """Background thread, no HOM: Blender exports the world; packed HDRIs to <blend>/tex."""
        return self._assets("importers").prepare_world(snapshot, cancelled)

    def existing_environment(self, record, mode):
        """HOM: active environment lighting to name in the Replace / Keep existing question."""
        return self._assets("importers").existing_environment(record, mode)

    def find_linked_world(self, record):
        return self._assets("importers").find_linked_world(record)

    def prepare_reload(self, snapshot, cancelled):
        """Background thread, no HOM: re-export for reload_snapshot (material or world)."""
        return self._assets("importers").prepare_linked(snapshot, cancelled)

    def shared_scenes(self, snapshot):
        """Host thread: other scenes a Convert & Append re-import would change (blend-import.md: Import record)."""
        return self._assets("importers").append_shared_scenes(snapshot)

    def is_content_failure(self, error):
        return self._assets("importers").is_content_failure(error)

    def record_importability(self, record, cache, state, reason):
        self._assets("importers").record_importability(record, cache, state, reason)

    def find_imported_material(self, record, mode="append"):
        return self._assets("importers").find_imported_material(record, mode)

    def linked_status(self, record):
        """HOM, cheap: {"path", "stale"} for the record's linked node, or None."""
        return self._assets("importers").linked_status(record)

    def reload_snapshot(self, path):
        return self._assets("importers").reload_snapshot(path)

    def rebuild_linked(self, path, prepared, report=None):
        return self._assets("importers").rebuild_linked([(path, prepared)], report)

    def selected_object_paths(self):
        return [node.path() for node in self._assets("importers").selected_objects()]

    def blender_snapshot(self):
        """Host thread: configuration only (may read hou env); no filesystem discovery."""
        from ..core import blender_bridge

        if not blender_bridge.enabled():
            raise ValueError("The Blender bridge is disabled for this installation (Cycles Setup).")
        return (
            blender_bridge._configured_executable(),
            os.environ.get("PATH", ""),
            os.environ.get("ProgramFiles", "C:/Program Files"),
        )

    def resolve_blender(self, snapshot):
        """Background thread: filesystem discovery from the host snapshot."""
        from ..core import blender_bridge

        executable = blender_bridge._discover(*snapshot)
        if not executable or not os.path.isfile(executable):
            raise FileNotFoundError(BLENDER_MISSING)
        return executable

    def dump_preferences(self, executable):
        from ..blender import blend_worker

        return blend_worker.dump_asset_library_preferences(executable, timeout=60)

    def launch_blender(self, executable, source):
        from ..blender import blend_worker

        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        # Blender's own OCIO configuration (blend_worker.blender_environment): a file saved
        # under Houdini's would lose Blender's colour space names.
        subprocess.Popen(
            [executable, source],
            env=blend_worker.blender_environment(_blender_ocio()),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=flags,
        )
        return "Opened " + os.path.basename(source) + " in Blender"


# Header import mode tooltips (labels: importers.IMPORT_MODES).
MODE_TOOLTIPS = {
    "link": "Materials and worlds: one shared, read-only node. Objects and collections: a Cycles "
    "Blender File node in Link mode that reads the .blend.",
    "append": "Materials and worlds: an editable copy. Objects and collections: a Cycles Blender File "
    "node in Converted mode (Options > Save Location).",
}


def _report_collector():
    """(lines, report): report(text, warning) appends (text, warning) to lines."""
    lines = []

    def report(text, warning=False):
        lines.append((str(text), bool(warning)))

    return lines, report


class _Pending:
    """Stand-in material for a drop whose import finishes in the background."""

    def __init__(self, name):
        self._name = name

    def path(self):
        return self._name + " (importing in the background)"


def _owner(widget):
    while isinstance(widget, QtWidgets.QWidget):
        if getattr(widget, "_cycles_asset_browser", False):
            return widget
        widget = widget.parentWidget()
    return None


class PanelShortcuts(QtCore.QObject):
    """T / N toggle the side panels while focus is inside the panel (not in a text field)."""

    def __init__(self, panel):
        super().__init__(panel)
        self.panel = panel
        self.app = QtWidgets.QApplication.instance()
        self.app.installEventFilter(self)

    def close(self):
        if self.app is not None:
            self.app.removeEventFilter(self)
            self.app = None

    def eventFilter(self, watched, event):
        kind = event.type()
        if kind not in (QtCore.QEvent.ShortcutOverride, QtCore.QEvent.KeyPress):
            return False
        if not isinstance(event, QtGui.QKeyEvent) or self.panel._disposed:
            return False
        if event.key() not in (QtCore.Qt.Key_T, QtCore.Qt.Key_N) or event.modifiers() != QtCore.Qt.NoModifier:
            return False
        from ..viewer.viewer_shortcuts import editing

        focus = self.app.focusWidget() if self.app is not None else None
        target = focus or (watched if isinstance(watched, QtWidgets.QWidget) else None)
        if _owner(target) is not self.panel or editing(target):
            return False
        # Claim the key before Houdini's hotkeys or the list's keyboard search.
        if kind == QtCore.QEvent.KeyPress and not event.isAutoRepeat():
            if event.key() == QtCore.Qt.Key_T:
                self.panel.toggle_left()
            else:
                self.panel.toggle_right()
        event.accept()
        return True


class LibrariesDialog(QtWidgets.QDialog):
    """Small modal editor: add, remove, rename, enable libraries; import Blender's list."""

    def __init__(self, panel, libraries):
        super().__init__(panel)
        self.panel = panel
        self.setWindowTitle("Asset Libraries")
        self.setModal(True)
        self.libraries = [copy.copy(library) for library in libraries]
        layout = QtWidgets.QVBoxLayout(self)
        self.table = QtWidgets.QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Enabled", "Name", "Folder"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.Interactive)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.Stretch)
        layout.addWidget(self.table, 1)
        row = QtWidgets.QHBoxLayout()
        self.add_button = QtWidgets.QPushButton("Add Folder...")
        self.remove_button = QtWidgets.QPushButton("Remove")
        self.import_button = QtWidgets.QPushButton("Import from Blender Preferences")
        self.import_button.setToolTip(
            "Read the asset libraries configured in Blender's Preferences (runs Blender in the background)."
        )
        for button in (self.add_button, self.remove_button, self.import_button):
            row.addWidget(button)
        row.addStretch(1)
        layout.addLayout(row)
        self.message = QtWidgets.QLabel()
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.add_button.clicked.connect(self.add_folder)
        self.remove_button.clicked.connect(self.remove_selected)
        self.import_button.clicked.connect(self.import_blender)
        self._fill()
        em = self.fontMetrics().averageCharWidth()
        self.resize(em * 90, self.fontMetrics().lineSpacing() * 18)

    def _fill(self):
        self.table.setRowCount(0)
        for library in self.libraries:
            row = self.table.rowCount()
            self.table.insertRow(row)
            enabled = QtWidgets.QTableWidgetItem()
            enabled.setFlags(QtCore.Qt.ItemIsUserCheckable | QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsSelectable)
            enabled.setCheckState(QtCore.Qt.Checked if library.enabled else QtCore.Qt.Unchecked)
            name = QtWidgets.QTableWidgetItem(library.name)
            path = QtWidgets.QTableWidgetItem(library.path)
            path.setFlags(QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsSelectable)
            path.setToolTip(library.path)
            for column, item in enumerate((enabled, name, path)):
                self.table.setItem(row, column, item)

    def _collect(self):
        for row, library in enumerate(self.libraries):
            library.enabled = self.table.item(row, 0).checkState() == QtCore.Qt.Checked
            name = self.table.item(row, 1).text().strip()
            library.name = name or os.path.basename(os.path.normpath(library.path)) or library.path

    def add_folder(self, path=None):
        if not path:
            path = QtWidgets.QFileDialog.getExistingDirectory(self, "Add Blender Asset Library Folder")
        if not path:
            return
        self._collect()
        key = os.path.normcase(os.path.abspath(path))
        if any(os.path.normcase(os.path.abspath(lib.path)) == key for lib in self.libraries):
            self.message.setText("That folder is already a library.")
            return
        name = os.path.basename(os.path.normpath(path)) or path
        self.libraries.append(self.panel.backend.new_library(name, path))
        self._fill()

    def remove_selected(self):
        self._collect()
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()}, reverse=True)
        for row in rows:
            del self.libraries[row]
        self._fill()

    def import_blender(self):
        self._collect()
        self.import_button.setEnabled(False)
        self.message.setText("Reading Blender preferences...")

        def done(dumped, error):
            if not self.isVisible() and self.result() != 0:
                return
            self.import_button.setEnabled(True)
            if error:
                self.message.setText(error)
                return
            before = len(self.libraries)
            self.libraries = self.panel.backend.merge_libraries(self.libraries, dumped)
            self._fill()
            added = len(self.libraries) - before
            self.message.setText(
                f"Added {added} librar{'y' if added == 1 else 'ies'} from Blender."
                if added
                else "No new libraries in Blender's preferences."
            )

        self.panel.read_blender_preferences(done)

    def accept(self):
        self._collect()
        super().accept()


def _glyph_label(name, tooltip):
    """A 14px tinted glyph leading a toolbar cluster, as in the render viewer."""
    label = QtWidgets.QLabel()
    label.setProperty("cycles_glyph", name)
    label.setAlignment(QtCore.Qt.AlignCenter)
    label.setToolTip(tooltip)
    dimensions(label, 14, 14)
    return label


class AssetBrowserPanel(QtWidgets.QWidget):
    def __init__(self, parent=None, *, backend=None):
        super().__init__(parent)
        self._cycles_asset_browser = True
        self.setProperty("cycles_asset_browser", True)
        self._disposed = False
        self.backend = backend or CoreBackend()
        self._libraries = []
        self._library = None
        self._provider = None
        self._job = None
        self._tree = None
        self._blender_missing = False
        self._indexing = False
        self._results = queue.SimpleQueue()
        self._tasks = 0
        self._material_tasks = {}
        self._listing_running = False
        self._generation = 0
        self._shortcuts = None
        self.timer = None
        self.model = None
        self._chrome = None
        self._theme = None
        self._status_error = False
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        try:
            enabled = self.backend.enabled()
        except Exception:
            enabled = True
        self.enabled = enabled
        if not enabled:
            self.settings = settings_module.normalize({})
            message = QtWidgets.QLabel(DISABLED)
            message.setWordWrap(True)
            message.setAlignment(QtCore.Qt.AlignCenter)
            layout.addWidget(message, 1)
            return
        self._settings_path = self.backend.settings_path()
        self.settings = settings_module.load_settings(self._settings_path)
        self._build()
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.poll)
        self._save_timer = QtCore.QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(600)
        self._save_timer.timeout.connect(self.save_settings)
        self._shortcuts = PanelShortcuts(self)
        from ..viewer import panel_theme

        self._chrome = panel_theme.install(self, PANEL_SCOPE, extra_style=asset_style, on_theme=self._theme_applied)
        self._chrome.capture_layouts()
        self.apply_theme()
        self.reload_libraries()

    # ------------------------------------------------------------ layout

    def _build(self):
        layout = self.layout()
        self.library_combo = ViewerCombo()
        self.library_combo.setToolTip("Asset library")
        self.library_combo.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToContents)
        self.provider_combo = ViewerCombo()
        for kind, label, tip in PROVIDER_CHOICES:
            self.provider_combo.addItem(label, kind)
            self.provider_combo.setItemData(self.provider_combo.count() - 1, tip, QtCore.Qt.ToolTipRole)
        self.provider_combo.setToolTip("How this library is read")
        self.mode_combo = ViewerCombo()
        model = QtGui.QStandardItemModel(self.mode_combo)
        from .importers import IMPORT_MODES

        for mode, label in IMPORT_MODES.items():
            item = QtGui.QStandardItem(label)
            item.setData(mode, QtCore.Qt.UserRole)
            item.setToolTip(MODE_TOOLTIPS.get(mode, ""))
            model.appendRow(item)
        self.mode_combo.setModel(model)
        self.mode_combo.setToolTip("Import mode")
        self.type_combo = ViewerCombo()
        self.type_combo.setToolTip("Show one asset type")
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Search names and tags")
        self.search.setClearButtonEnabled(True)
        self.size_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.size_slider.setRange(settings_module.THUMBNAIL_MIN, settings_module.THUMBNAIL_MAX)
        self.size_slider.setToolTip("Thumbnail size")
        self.refresh_button = ViewerToolButton()
        self.refresh_button.setAccessibleName("Refresh")
        self.refresh_button.setToolTip("Re-read changed .blend files and catalogs")
        self.refresh_button.setToolButtonStyle(QtCore.Qt.ToolButtonIconOnly)
        self.refresh_button.setProperty("cycles_glyph", "BUTTONS_reload")
        self.refresh_button.setAttribute(QtCore.Qt.WA_Hover, True)
        # Icon-only like Refresh: the name moves to the tooltip and the accessible name.
        self.options_button = ViewerToolButton()
        self.options_button.setAccessibleName("Options")
        self.options_button.setToolTip("Options: reference viewport shape, save location, drag and drop")
        self.options_button.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.options_button.setMenu(self._options_menu())
        self.libraries_button = ViewerToolButton()
        self.libraries_button.setAccessibleName("Libraries...")
        self.libraries_button.setToolTip("Libraries...: add, remove, rename or disable asset libraries")
        # Same chrome as the render viewer: one 24px toolbar row of themed fields and buttons.
        for widget, width in (
            (self.library_combo, 140),
            (self.provider_combo, 84),
            (self.mode_combo, 128),
            (self.type_combo, 104),
            (self.size_slider, 96),
        ):
            dimensions(widget, width, 24 if isinstance(widget, QtWidgets.QComboBox) else 20)
        dimensions(self.search, height=24)
        self.search.setMinimumWidth(120)
        self.libraries_button.setProperty("cycles_glyph", "BUTTONS_folder")
        self.libraries_button.setToolButtonStyle(QtCore.Qt.ToolButtonIconOnly)
        self.libraries_button.setAttribute(QtCore.Qt.WA_Hover, True)
        self.options_button.setProperty("cycles_glyph", "IMAGE_settings")
        self.options_button.setToolButtonStyle(QtCore.Qt.ToolButtonIconOnly)
        self.options_button.setAttribute(QtCore.Qt.WA_Hover, True)
        # The render viewer's toolbar vocabulary: dropdown clusters led by a tinted glyph.
        self.header = ToolbarRow()
        self.header.setProperty("cycles_toolbar", True)
        self.header.group(
            cluster(_glyph_label("BUTTONS_folder", "Asset library"), self.library_combo),
            cluster(_glyph_label("IMAGE_view", "How this library is read"), self.provider_combo),
        )
        self.header.group(
            cluster(_glyph_label("BUTTONS_resolution_link", "Import mode"), self.mode_combo),
            cluster(_glyph_label("IMAGE_look", "Asset type"), self.type_combo),
        )
        self.header.group(self.search, stretch=True)
        self.header.group(self.size_slider, self.refresh_button)
        self.header.group(self.options_button, self.libraries_button)
        layout.addWidget(self.header)

        self.banner = QtWidgets.QLabel()
        self.banner.setWordWrap(True)
        self.banner.setVisible(False)
        self.banner.setProperty("cycles_warning", True)
        self.banner.setProperty("cycles_banner", True)
        layout.addWidget(self.banner)

        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(1)
        self.catalogs = CatalogTreeWidget()
        self.model = AssetModel(self)
        self.proxy = AssetFilterProxy(self)
        self.proxy.setSourceModel(self.model)
        self.grid = AssetGridView()
        self.grid.setModel(self.proxy)
        self.grid.drag_handler = self._begin_drag
        self.empty = QtWidgets.QWidget()
        self.empty.setProperty("cycles_asset_canvas", True)
        self.empty.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        empty_layout = QtWidgets.QVBoxLayout(self.empty)
        empty_layout.addStretch(1)
        self.empty_label = QtWidgets.QLabel(NO_LIBRARIES)
        self.empty_label.setAlignment(QtCore.Qt.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.empty_label.setProperty("cycles_caption", True)
        empty_layout.addWidget(self.empty_label)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addStretch(1)
        self.empty_add = QtWidgets.QPushButton("Add Library Folder...")
        self.empty_import = QtWidgets.QPushButton("Import from Blender Preferences")
        self.empty_generate = QtWidgets.QPushButton("Generate Blender Listing...")
        self.empty_generate.setToolTip("Writes _asset-library-meta.json, _v1/ and thumbnail folders into the library")
        self.empty_generate.setVisible(False)
        buttons.addWidget(self.empty_add)
        buttons.addWidget(self.empty_import)
        buttons.addWidget(self.empty_generate)
        buttons.addStretch(1)
        empty_layout.addLayout(buttons)
        empty_layout.addStretch(2)
        self.center = QtWidgets.QStackedWidget()
        self.center.addWidget(self.grid)
        self.center.addWidget(self.empty)
        self.details = DetailsPanel()
        self.splitter.addWidget(self.catalogs)
        self.splitter.addWidget(self.center)
        self.splitter.addWidget(self.details)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 0)
        em = self.fontMetrics().averageCharWidth()
        sizes = self.settings["splitter_sizes"] or [em * 24, em * 80, em * 32]
        self.splitter.setSizes(sizes)
        layout.addWidget(self.splitter, 1)

        self.status_bar = QtWidgets.QWidget()
        self.status_bar.setProperty("cycles_statusbar", True)
        self.status_bar.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        status = QtWidgets.QHBoxLayout(self.status_bar)
        status.setContentsMargins(6, 0, 6, 0)
        status.setSpacing(10)
        dimensions(self.status_bar, height=22)
        self.status = QtWidgets.QLabel()
        self.status.setTextFormat(QtCore.Qt.PlainText)
        self.status.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        self.progress = QtWidgets.QProgressBar()
        dimensions(self.progress, 90, 4)  # The render viewer's status progress rail.
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        self.cancel_button = ViewerToolButton()
        self.cancel_button.setText("Cancel")
        dimensions(self.cancel_button, height=20)
        self.cancel_button.setVisible(False)
        status.addWidget(self.status, 1)
        status.addWidget(self.progress)
        status.addWidget(self.cancel_button)
        layout.addWidget(self.status_bar)

        # Initial values from settings, then connections.
        self.size_slider.setValue(self.settings["thumbnail_size"])
        self.grid.set_thumbnail_size(round(self.settings["thumbnail_size"] * self._chrome_scale()))
        self._set_mode_combo(self.settings["import_mode"])
        self.catalogs.setVisible(self.settings["left_visible"])
        self.details.setVisible(self.settings["right_visible"])
        self.grid.set_drag_enabled(self.drag_active())

        self.library_combo.currentIndexChanged.connect(self._library_changed)
        self.provider_combo.currentIndexChanged.connect(self._provider_changed)
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        self.type_combo.currentIndexChanged.connect(self._type_changed)
        self.search.textChanged.connect(self.proxy.set_search)
        self.size_slider.valueChanged.connect(self._size_changed)
        self.refresh_button.clicked.connect(lambda: self.start_scan())
        self.libraries_button.clicked.connect(self.edit_libraries)
        self.cancel_button.clicked.connect(self.cancel_work)
        self.empty_generate.clicked.connect(self.generate_listing)
        self.empty_add.clicked.connect(self.add_library_folder)
        self.empty_import.clicked.connect(self.import_blender_libraries)
        self.catalogs.filterChanged.connect(self.proxy.set_catalog)
        self.grid.selectionModel().currentChanged.connect(lambda *_: self._selection_changed())
        self.proxy.modelReset.connect(self._selection_changed)
        self.proxy.layoutChanged.connect(self._selection_changed)
        self.grid.activatedRecord.connect(self.import_record)
        self.grid.customContextMenuRequested.connect(self._context_menu)
        self.details.importRequested.connect(self.import_record)
        self.details.assignRequested.connect(self.assign_record)
        self.details.openRequested.connect(self.open_blend_file)
        self.details.reloadRequested.connect(self.reload_linked)
        self.splitter.splitterMoved.connect(lambda *_: self._schedule_save())

    def _options_menu(self):
        menu = QtWidgets.QMenu(self)
        shapes = menu.addMenu("Imported Reference Viewport")
        self._shape_group = QtGui.QActionGroup(self)
        for value, label in (("hull", "Convex Hull"), ("bounds", "Bounding Box")):
            action = shapes.addAction(label)
            action.setCheckable(True)
            action.setData(value)
            action.setChecked(self.settings["reference_shape"] == value)
            self._shape_group.addAction(action)
        self._shape_group.triggered.connect(lambda action: self._set_setting("reference_shape", action.data()))
        # Convert & Append of objects and collections; the setting keeps its internal name.
        appended = menu.addMenu("Save Location")
        appended.setToolTipsVisible(True)
        self._append_group = QtGui.QActionGroup(self)
        for value, tip in (
            ("files", "Converted objects and collections are written to $HIP/assets_blender and read from disk."),
            ("embedded", "Converted objects and collections are kept inside the .hip file (one frame)."),
        ):
            action = appended.addAction(settings_module.SAVE_LOCATION_LABELS[value])
            action.setToolTip(tip)
            action.setCheckable(True)
            action.setData(value)
            action.setChecked(self.settings["append_storage"] == value)
            self._append_group.addAction(action)
        self._append_group.triggered.connect(lambda action: self._set_setting("append_storage", action.data()))
        drag = menu.addMenu("Drag and Drop (Experimental)")
        self._drag_group = QtGui.QActionGroup(self)
        current = self.settings["drag_route"] if self.settings["drag_enabled"] else "off"
        for value, label in (
            ("off", "Off"),
            ("experimental", "Experimental: act on the pane under the cursor"),
            ("experimental_unaccepted", "Experimental: only when no target accepted the drop"),
        ):
            action = drag.addAction(label)
            action.setCheckable(True)
            action.setData(value)
            action.setChecked(current == value)
            self._drag_group.addAction(action)
        drag.setToolTipsVisible(True)
        drag.setToolTip("Untested in Houdini 22. Network Editor: import at the cursor; Scene Viewer: assign materials.")
        self._drag_group.triggered.connect(lambda action: self.set_drag_route(action.data()))
        menu.addSeparator()
        self.listing_action = menu.addAction("Generate Blender Listing...")
        self.listing_action.setToolTip(
            "Run Blender's listing generator for the Listing provider. Writes into the library folder."
        )
        self.listing_action.triggered.connect(lambda: self.generate_listing())
        menu.setToolTipsVisible(True)
        menu.addSeparator()
        self.left_action = menu.addAction("Catalogs (T)")
        self.left_action.setCheckable(True)
        self.left_action.setChecked(self.settings["left_visible"])
        self.left_action.triggered.connect(lambda checked: self.set_left_visible(checked))
        self.right_action = menu.addAction("Details (N)")
        self.right_action.setCheckable(True)
        self.right_action.setChecked(self.settings["right_visible"])
        self.right_action.triggered.connect(lambda checked: self.set_right_visible(checked))
        return menu

    def _chrome_scale(self):
        chrome = getattr(self, "_chrome", None)
        return chrome.scale if chrome is not None and chrome.theme is not None else 1.0

    def apply_theme(self, force=True):
        """Houdini theme roles, monitor scale and the shared Cycles chrome (see viewer.panel_theme)."""
        if getattr(self, "_chrome", None) is not None:
            self._chrome.update(force=force)

    def _theme_applied(self, theme, scale):
        self._theme = theme
        self.grid.set_theme(theme)
        self.grid.set_thumbnail_size(round(self.settings["thumbnail_size"] * scale))
        self.catalogs.set_theme(theme)
        self.details.set_theme(theme, scale)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QtCore.QEvent.PaletteChange and self.enabled and not self._disposed:
            self.apply_theme(force=False)

    def showEvent(self, event):
        super().showEvent(event)
        if self.enabled and not self._disposed:
            self.apply_theme(force=False)

    # ------------------------------------------------------------ settings

    def _set_setting(self, name, value):
        self.settings[name] = value
        self._schedule_save()

    def _schedule_save(self):
        if not self._disposed and self.enabled:
            self._save_timer.start()

    def save_settings(self):
        if not self.enabled:
            return
        if self.catalogs.isVisible() and self.details.isVisible():
            sizes = self.splitter.sizes()
            if len(sizes) == 3 and all(sizes):
                self.settings["splitter_sizes"] = sizes
        try:
            self.settings = settings_module.save_settings(self.settings, self._settings_path)
        except OSError as error:
            self.notify("Could not save asset browser settings: " + str(error), error=True)

    def drag_active(self):
        return bool(self.settings["drag_enabled"]) and self.settings["drag_route"] != "off"

    def set_drag_route(self, route):
        self.settings["drag_route"] = route
        self.settings["drag_enabled"] = route != "off"
        self.grid.set_drag_enabled(self.drag_active())
        self._schedule_save()

    def toggle_left(self):
        self.set_left_visible(not self.catalogs.isVisibleTo(self))

    def toggle_right(self):
        self.set_right_visible(not self.details.isVisibleTo(self))

    def set_left_visible(self, visible):
        self.catalogs.setVisible(bool(visible))
        self.left_action.setChecked(bool(visible))
        self._set_setting("left_visible", bool(visible))

    def set_right_visible(self, visible):
        self.details.setVisible(bool(visible))
        self.right_action.setChecked(bool(visible))
        self._set_setting("right_visible", bool(visible))

    def _size_changed(self, value):
        self.grid.set_thumbnail_size(round(value * self._chrome_scale()))
        self._set_setting("thumbnail_size", int(value))

    def _set_mode_combo(self, mode):
        for row in range(self.mode_combo.count()):
            if self.mode_combo.itemData(row, QtCore.Qt.UserRole) == mode:
                blocker = QtCore.QSignalBlocker(self.mode_combo)
                self.mode_combo.setCurrentIndex(row)
                del blocker

    def _mode_changed(self, row):
        mode = self.mode_combo.itemData(row, QtCore.Qt.UserRole)
        if mode:
            self._set_setting("import_mode", mode)
        self._selection_changed()

    # ------------------------------------------------------------ libraries

    def reload_libraries(self, select=None):
        try:
            self._libraries, error = self.backend.read_libraries()
        except Exception as exc:
            self._libraries, error = [], "Cannot read the asset library registry: " + str(exc)
        if error:
            self.notify(error, error=True)
        select = select or self.settings["last_library"]
        blocker = QtCore.QSignalBlocker(self.library_combo)
        self.library_combo.clear()
        current = -1
        for index, library in enumerate(self._libraries):
            if not library.enabled:
                continue
            self.library_combo.addItem(library.name, index)
            self.library_combo.setItemData(self.library_combo.count() - 1, library.path, QtCore.Qt.ToolTipRole)
            if library.name == select:
                current = self.library_combo.count() - 1
        del blocker
        if self.library_combo.count() == 0:
            self._set_library(None)
            return
        self.library_combo.setCurrentIndex(max(0, current))
        self._library_changed(self.library_combo.currentIndex())

    def _library_changed(self, row):
        index = self.library_combo.itemData(row)
        library = self._libraries[index] if isinstance(index, int) and 0 <= index < len(self._libraries) else None
        self._set_library(library)

    def _set_library(self, library):
        self.cancel_scan()
        # Results of work started for the previous library are ignored on arrival.
        self._generation += 1
        for event in self._material_tasks.values():
            event.set()
        self.listing_action.setEnabled(library is not None)
        self._library = library
        self._provider = None
        self._tree = None
        self._blender_missing = False
        self.banner.setVisible(False)
        if library is None:
            self.model.set_records([])
            self.catalogs.populate(None, [])
            self._update_types([])
            self._update_empty()
            for widget in (self.provider_combo, self.refresh_button):
                widget.setEnabled(False)
            if not self._libraries:
                self.notify(NO_LIBRARIES)
            else:
                self.notify("All libraries are disabled; enable one under Libraries...")
            return
        for widget in (self.provider_combo, self.refresh_button):
            widget.setEnabled(True)
        if self.settings["last_library"] != library.name:
            self._set_setting("last_library", library.name)
        blocker = QtCore.QSignalBlocker(self.provider_combo)
        self.provider_combo.setCurrentIndex(max(0, self.provider_combo.findData(library.provider)))
        del blocker
        try:
            self._provider = self.backend.make_provider(library, library.provider)
        except Exception as exc:
            self.notify("Cannot open library " + library.name + ": " + str(exc), error=True)
            self.model.set_records([])
            self._update_empty()
            return
        self.show_cached()
        self.start_scan()

    def show_cached(self):
        """Cheap: cached records and the catalog file, never Blender."""
        records, tree = [], None
        try:
            records = list(self._provider.items())
        except Exception as exc:
            self.notify("Cannot read the asset cache: " + str(exc), error=True)
        try:
            tree = self._provider.catalogs()
        except Exception as exc:
            self.notify("Cannot read catalogs: " + str(exc), error=True)
        self._set_records(records, tree)

    def _set_records(self, records, tree):
        selected = self.grid.current_record()
        key = (selected.get("ch") or {}).get("key") if selected else None
        self._tree = tree
        self.model.set_records(records)
        self.catalogs.populate(tree, records)
        warnings = list(getattr(tree, "warnings", None) or [])
        if warnings:
            self.catalogs.setToolTip("\n".join(warnings))
        self._update_types(records)
        if key:
            self.select_key(key)
        self._update_empty()
        self._selection_changed()

    def select_key(self, key):
        row = self.model.row_for_key(key)
        if row < 0:
            return False
        index = self.proxy.mapFromSource(self.model.index(row))
        if not index.isValid():
            return False
        self.grid.setCurrentIndex(index)
        return True

    def _update_types(self, records):
        present = sorted({record.get("id_type", "") for record in records} - {""})
        wanted = self.type_combo.currentData() or self.settings["type_filter"]
        blocker = QtCore.QSignalBlocker(self.type_combo)
        self.type_combo.clear()
        self.type_combo.addItem(TYPE_LABELS["ALL"], "ALL")
        for id_type in present:
            self.type_combo.addItem(TYPE_LABELS.get(id_type, id_type.title()), id_type)
        if wanted not in present and wanted != "ALL":
            # Keep a remembered filter visible even if this library lacks the type.
            self.type_combo.addItem(TYPE_LABELS.get(wanted, wanted.title()), wanted)
        self.type_combo.setCurrentIndex(max(0, self.type_combo.findData(wanted)))
        del blocker
        self.proxy.set_type(self.type_combo.currentData())

    def _type_changed(self, row):
        id_type = self.type_combo.itemData(row) or "ALL"
        self.proxy.set_type(id_type)
        self._set_setting("type_filter", id_type)

    def _provider_changed(self, row):
        kind = self.provider_combo.itemData(row)
        if self._library is None or not kind or kind == self._library.provider:
            return
        self._library.provider = kind
        try:
            self.backend.save_libraries(self._libraries)
        except Exception as exc:
            self.notify("Could not save the library registry: " + str(exc), error=True)
        self._set_library(self._library)

    def _update_empty(self):
        if self._library is None:
            self.empty_label.setText(
                NO_LIBRARIES if not self._libraries else "All libraries are disabled; enable one under Libraries..."
            )
            self.empty_add.setVisible(True)
            self.empty_import.setVisible(True)
            self.empty_generate.setVisible(False)
            self.center.setCurrentWidget(self.empty)
            return
        if self.model.rowCount() == 0:
            listing_missing = self._listing_missing()
            self.empty_generate.setVisible(listing_missing and not self._listing_running)
            if self._indexing:
                text = "Reading library..."
            elif listing_missing:
                text = LISTING_MISSING
            elif self._blender_missing:
                text = BLENDER_MISSING
            else:
                text = "No assets found in this library. In Blender, use Mark as Asset and save the file, then Refresh."
            self.empty_label.setText(text)
            self.empty_add.setVisible(False)
            self.empty_import.setVisible(False)
            self.center.setCurrentWidget(self.empty)
        else:
            self.center.setCurrentWidget(self.grid)

    def edit_libraries(self):
        dialog = LibrariesDialog(self, self._libraries)
        if dialog.exec() != QtWidgets.QDialog.Accepted or self._disposed:
            return
        self._store_libraries(dialog.libraries)

    def _store_libraries(self, libraries):
        current = self._library.name if self._library is not None else None
        try:
            self.backend.save_libraries(libraries)
        except Exception as exc:
            self.notify("Could not save the library registry: " + str(exc), error=True)
            return
        self.reload_libraries(select=current)

    def add_library_folder(self, path=None):
        if not path:
            path = QtWidgets.QFileDialog.getExistingDirectory(self, "Add Blender Asset Library Folder")
        if not path:
            return
        name = os.path.basename(os.path.normpath(path)) or path
        libraries = list(self._libraries) + [self.backend.new_library(name, path)]
        self.settings["last_library"] = name
        self._store_libraries(libraries)

    def import_blender_libraries(self):
        def done(dumped, error):
            if error:
                self.notify(error, error=True)
                return
            merged = self.backend.merge_libraries(list(self._libraries), dumped)
            added = len(merged) - len(self._libraries)
            self._store_libraries(merged)
            self.notify(
                f"Added {added} libraries from Blender's preferences."
                if added
                else "No new libraries in Blender's preferences."
            )

        self.notify("Reading Blender preferences...")
        self.read_blender_preferences(done)

    def read_blender_preferences(self, callback):
        """Resolve Blender (host snapshot, background discovery) and dump its library list."""
        try:
            snapshot = self.backend.blender_snapshot()
        except Exception as exc:
            callback(None, str(exc))
            return
        backend = self.backend

        def work():
            return backend.dump_preferences(backend.resolve_blender(snapshot))

        self.run_task(work, callback)

    # ------------------------------------------------------------ indexing

    def start_scan(self):
        if self._provider is None or self._disposed:
            return
        self.cancel_scan()
        try:
            self._job = self.backend.make_job(self._provider)
            self._job.start()
        except Exception as exc:
            self._job = None
            self.notify("Cannot start indexing: " + str(exc), error=True)
            return
        self._indexing = True
        self.progress.setRange(0, 0)
        self._update_busy()
        self.notify("Reading " + self._library.name + "...")
        self._update_empty()
        self.timer.start()

    def cancel_scan(self):
        job, self._job = self._job, None
        if job is not None:
            try:
                job.cancel()
            except Exception:
                pass
        self._indexing = False
        if self.model is not None:
            self._update_busy()

    def cancel_work(self):
        """Cancel indexing and every material export in flight."""
        self.cancel_scan()
        for event in self._material_tasks.values():
            event.set()
        if self._material_tasks:
            self.notify("Cancelling material import...")

    def _update_busy(self):
        if self.model is None:
            return
        busy = self._indexing or bool(self._material_tasks) or self._listing_running
        if busy and not self._indexing:
            self.progress.setRange(0, 0)
        self.progress.setVisible(busy)
        self.cancel_button.setVisible(self._indexing or bool(self._material_tasks))

    def _listing_missing(self):
        provider = self._provider
        if provider is None or getattr(provider, "kind", None) != "listing":
            return False
        check = getattr(provider, "has_listing", None)
        try:
            return callable(check) and not check()
        except OSError:
            return True

    def poll(self):
        if self._disposed:
            return
        if self._job is not None:
            try:
                state = self._job.poll()
            except Exception as exc:
                state = {"state": "failed", "error": str(exc)}
            if state:
                self._job_state(state)
        while True:
            try:
                callback, value, error = self._results.get_nowait()
            except queue.Empty:
                break
            self._tasks -= 1
            try:
                callback(value, error)
            except Exception as exc:
                self.notify(str(exc), error=True)
        if self._job is None and self._tasks <= 0:
            self.timer.stop()

    def _job_state(self, state):
        kind = state.get("state")
        if kind == "running":
            done, total = state.get("progress") or (0, 0)
            if total:
                self.progress.setRange(0, int(total))
                self.progress.setValue(int(done))
                self.status.setText(f"Reading {self._library.name}: {done} / {total} files")
            return
        self._job = None
        self._indexing = False
        self._update_busy()
        if kind == "done":
            self._apply_result(state.get("result"))
        elif kind == "cancelled":
            self.notify("Indexing cancelled.")
            self._update_empty()
        else:
            self.notify("Indexing failed: " + (state.get("error") or "unknown error"), error=True)
            self._update_empty()

    def _apply_result(self, result):
        if result is None:
            self._update_empty()
            return
        if getattr(result, "scanned", 0):
            self.model.loader.forget()
        tree = self._tree
        try:
            tree = self._provider.catalogs()
        except Exception:
            pass
        self._blender_missing = bool(getattr(result, "blender_missing", False))
        records = list(getattr(result, "records", None) or [])
        self._set_records(records, tree)
        self.banner.setText(BLENDER_MISSING)
        self.banner.setVisible(self._blender_missing)
        errors = list(getattr(result, "errors", None) or [])
        parts = [f"{len(records)} assets"]
        scanned, reused = getattr(result, "scanned", 0), getattr(result, "reused", 0)
        if scanned or reused:
            parts.append(f"{scanned} files read, {reused} cached")
        if errors:
            parts.append(f"{len(errors)} file errors")
        message = getattr(result, "message", "") or ""
        if message:
            parts.append(message)
        detail = "\n".join(self._error_line(item) for item in errors[:50])
        self.notify("; ".join(parts), error=bool(errors), tooltip=detail or None)

    @staticmethod
    def _error_line(item):
        if isinstance(item, dict):
            source = item.get("source") or item.get("file") or ""
            text = item.get("error") or item.get("message") or ""
            return (os.path.basename(source) + ": " if source else "") + str(text)
        return str(item)

    def run_task(self, work, callback):
        """Run `work()` on a thread (no HOM there); `callback(value, error)` runs on the GUI thread."""
        results = self._results

        def run():
            try:
                value, error = work(), None
            except Exception as exc:
                value, error = None, str(exc) or type(exc).__name__
            results.put((callback, value, error))

        self._tasks += 1
        threading.Thread(target=run, name="cycles-asset-browser-task", daemon=True).start()
        if self.timer is not None:
            self.timer.start()

    # ------------------------------------------------------------ selection and actions

    def notify(self, text, *, error=False, tooltip=None):
        if self.model is None:
            return
        self.status.setText(" ".join((text or "").splitlines()))
        self.status.setToolTip(tooltip or text or "")
        # Warning colour comes from the shared chrome rule QLabel[cycles_warning="true"] ($warning).
        self._status_error = bool(error)
        if bool(self.status.property("cycles_warning")) != self._status_error:
            self.status.setProperty("cycles_warning", self._status_error)
            self.status.style().unpolish(self.status)
            self.status.style().polish(self.status)

    def current_record(self):
        return self.grid.current_record()

    def _effective_mode(self, record):
        from .importers import effective_mode

        return effective_mode(record.get("id_type"), self.settings["import_mode"]) if record else None

    def _selection_changed(self, *_):
        if self.model is None or self._disposed:
            return
        from .importers import APPEND_OBJECTS_TOOLTIP, IMPORT_MODES, valid_modes

        record = self.current_record()
        id_type = record.get("id_type") if record else None
        valid = valid_modes(id_type) if record else tuple(IMPORT_MODES)
        model = self.mode_combo.model()
        for row in range(model.rowCount()):
            item = model.item(row)
            mode = item.data(QtCore.Qt.UserRole)
            item.setEnabled(mode in valid)
            tip = MODE_TOOLTIPS.get(mode, "")
            if mode not in valid and record is not None:
                if mode == "append" and id_type in ("OBJECT", "COLLECTION"):
                    tip = APPEND_OBJECTS_TOOLTIP
                else:
                    tip = "Not available for this asset type."
            item.setToolTip(tip)
        pixmap = linked = None
        if record is not None:
            row = self.model.row_for_key((record.get("ch") or {}).get("key", ""))
            pixmap = self.model.thumbnail(row) if row >= 0 else None
            if record.get("id_type") in ("MATERIAL", "WORLD"):
                try:
                    linked = self.backend.linked_status(record)
                except Exception:
                    linked = None
        self.details.set_record(record, tree=self._tree, pixmap=pixmap, linked=linked)

    def _cache(self):
        return getattr(self._provider, "cache", None)

    def _import_options(self):
        return dict(
            cache=self._cache(),
            library_root=self._library.path if self._library is not None else None,
        )

    def _notify_report(self, text, lines, notice=""):
        """Status line for a finished import: `text`, the notice, then the report lines
        (warnings colour the status but are not failures); all of it in the tooltip."""
        parts = [text.rstrip(".") + "."] + ([notice] if notice else []) + [line for line, _ in lines]
        self.notify(" ".join(parts), error=any(warning for _, warning in lines), tooltip="\n".join(parts))

    def ask_replace_shared(self, question):
        """GUI thread: Replace (True) or Cancel (False) when a re-import changes other scenes.

        Tests replace this method; background work never asks."""
        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Warning)
        box.setWindowTitle("Blender asset used by other scenes")
        box.setText(question)
        replace = box.addButton("Replace", QtWidgets.QMessageBox.AcceptRole)
        cancel = box.addButton("Cancel", QtWidgets.QMessageBox.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() is replace

    def ask_existing_environment(self, names):
        """GUI thread, before a world import starts (blend-import.md: Worlds): "replace", "keep", or None (Cancel).

        Tests replace this method; background work never asks."""
        from .importers import ENVIRONMENT_EXPLANATION, environment_question

        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Question)
        box.setWindowTitle("Import Blender world")
        box.setText(environment_question(names))
        box.setInformativeText(ENVIRONMENT_EXPLANATION)
        replace = box.addButton("Replace", QtWidgets.QMessageBox.AcceptRole)
        keep = box.addButton("Keep existing", QtWidgets.QMessageBox.ActionRole)
        cancel = box.addButton("Cancel", QtWidgets.QMessageBox.RejectRole)
        box.setDefaultButton(replace)
        box.setEscapeButton(cancel)
        box.exec()
        return {replace: "replace", keep: "keep"}.get(box.clickedButton())

    def _refresh_importability(self, record):
        ch = record.get("ch") or {}
        state = ch.get("importable") or {}
        self.model.update_importability(ch.get("key", ""), state.get("state", "unknown"), state.get("reason", ""))
        if self.current_record() is record:
            self._selection_changed()

    def _busy(self, text):
        self.notify(text)
        self.status.repaint()
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)

    def import_record(self, record=None):
        """Linked objects/collections import synchronously; materials, worlds and Convert &
        Append export in the background.

        Returns the created nodes, or None (the import completes later, or was cancelled).
        """
        record = record or self.current_record()
        if record is None:
            return None
        from .importers import unsupported_reason

        reason = unsupported_reason(record)
        mode = self._effective_mode(record)
        if reason or mode is None:
            self.notify(reason or "This asset cannot be imported.", error=True)
            return None
        if record.get("id_type") == "MATERIAL":
            if mode == "link":
                # Link reuses the one shared node at once; no Blender export.
                try:
                    existing = self.backend.find_imported_material(record, "link")
                except Exception:
                    existing = None
                if existing is not None:
                    try:
                        nodes = self.backend.import_asset(record, mode="link", **self._import_options())
                    except Exception as exc:
                        self.notify("Import failed: " + (str(exc) or type(exc).__name__), error=True)
                        return None
                    self.notify(record.get("name", "") + " is already linked" + self._paths_suffix(nodes))
                    return nodes
            self.start_material(record, mode=mode)
            return None
        if record.get("id_type") == "WORLD":
            nodes = self.start_world(record, mode=mode)
            return nodes if isinstance(nodes, list) else None
        if mode == "append":
            # Objects/collections: Blender exports and converts in the background.
            self.start_append(record)
            return None
        self._busy("Importing " + record.get("name", "") + "...")
        try:
            nodes = self.backend.import_asset(
                record, mode=mode, viewport_shape=self.settings["reference_shape"], **self._import_options()
            )
        except Exception as exc:
            self.notify("Import failed: " + (str(exc) or type(exc).__name__), error=True)
            nodes = None
        else:
            self.notify("Imported " + record.get("name", "") + self._paths_suffix(nodes))
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        self._refresh_importability(record)
        return nodes

    @staticmethod
    def _paths_suffix(nodes):
        paths = ", ".join(node.path() if hasattr(node, "path") else str(node) for node in nodes or ())
        return " as " + paths if paths else ""

    def assign_record(self, record=None):
        """Assign to the selected objects. Reuses an imported material at once; otherwise the
        export runs in the background and import + assignment happen as one undo step later."""
        record = record or self.current_record()
        if record is None or record.get("id_type") != "MATERIAL":
            return None
        try:
            mode = self._effective_mode(record)
            paths = self.backend.selected_object_paths()
            existing = self.backend.find_imported_material(record, mode) if paths else None
        except Exception as exc:
            self.notify("Assign failed: " + (str(exc) or type(exc).__name__), error=True)
            return None
        if not paths:
            self.notify("Select one or more objects (OBJ nodes) to assign the material to.", error=True)
            return None
        if existing is None:
            self.start_material(record, assign_nodes=paths, mode=mode)
            return None
        try:
            material, nodes = self.backend.assign_material(record, nodes=paths, mode=mode, **self._import_options())
        except Exception as exc:
            self.notify("Assign failed: " + (str(exc) or type(exc).__name__), error=True)
            return None
        self.notify(f"Assigned {material.path()} to {len(nodes)} object(s)")
        self._refresh_importability(record)
        return material, nodes

    def material_busy(self, record):
        return (record.get("ch") or {}).get("key", "") in self._material_tasks

    def start_material(self, record, *, assign_nodes=None, network=None, position=None, mode=None):
        """Host snapshot now, Blender export on a thread, HOM build on the GUI thread in one undo group."""
        key = (record.get("ch") or {}).get("key", "")
        name = record.get("name", "")
        if key in self._material_tasks:
            self.notify(name + " is already being imported.")
            return False
        mode = mode or self._effective_mode(record) or "append"
        options = self._import_options()
        cache = options["cache"]
        try:
            snapshot = self.backend.material_snapshot(record, library_root=options["library_root"])
        except Exception as exc:
            self._material_failed(record, exc, cache)
            return False
        cancel = threading.Event()
        generation = self._generation
        self._material_tasks[key] = cancel
        backend = self.backend
        notice = snapshot.get("notice", "") if isinstance(snapshot, dict) else ""

        def work():
            # Exceptions travel as values so the GUI thread can classify them.
            try:
                return "ok", backend.prepare_material(snapshot, cancel)
            except Exception as exc:
                return "error", exc

        def done(value, error):
            if self._material_tasks.get(key) is cancel:
                del self._material_tasks[key]
            if self._disposed:
                return
            self._update_busy()
            if generation != self._generation:
                return
            if cancel.is_set():
                self.notify("Import of " + name + " cancelled.")
                return
            if error:
                self.notify("Import failed: " + error, error=True)
                return
            kind, payload = value
            if kind == "error":
                self._material_failed(record, payload, cache)
                return
            lines, report = _report_collector()
            try:
                if assign_nodes is not None:
                    material, nodes = backend.assign_material(
                        record, nodes=assign_nodes, prepared=payload, mode=mode, report=report, **options
                    )
                    text = f"Assigned {material.path()} to {len(nodes)} object(s)"
                else:
                    nodes = backend.import_asset(
                        record,
                        mode=mode,
                        network=network,
                        position=position,
                        prepared=payload,
                        report=report,
                        **options,
                    )
                    text = "Imported " + name + self._paths_suffix(nodes)
            except Exception as exc:
                self._material_failed(record, exc, cache)
                return
            self._notify_report(text, lines, notice)
            self._refresh_importability(record)

        self.notify("Importing " + name + ": exporting from Blender..." + (" " + notice if notice else ""))
        self.run_task(work, done)
        self._update_busy()
        return True

    def start_world(self, record, *, mode=None):
        """Import a world (blend-import.md: Worlds). The Replace / Keep existing / Cancel question comes
        first, on the GUI thread; Link reuses an existing shared node at once (returns its
        nodes); otherwise Blender exports on a thread and the build runs on the GUI thread in
        one undo group (returns True). False: cancelled or failed before starting."""
        key = "world|" + (record.get("ch") or {}).get("key", "")
        name = record.get("name", "")
        if key in self._material_tasks:
            self.notify(name + " is already being imported.")
            return False
        mode = mode or self._effective_mode(record) or "link"
        options = self._import_options()
        cache = options["cache"]
        backend = self.backend
        snapshot = None
        try:
            reuse = mode == "link" and backend.find_linked_world(record) is not None
            if not reuse:
                snapshot = backend.world_snapshot(record, library_root=options["library_root"])
            names = backend.existing_environment(record, mode)
        except Exception as exc:
            self._material_failed(record, exc, cache)
            return False
        choice = "replace"
        if names:
            choice = self.ask_existing_environment(list(names))
            if choice is None:
                self.notify("Import of " + name + " cancelled; nothing was changed.")
                return False
        if reuse:
            lines, report = _report_collector()
            try:
                nodes = backend.import_asset(record, mode="link", existing_environment=choice, report=report, **options)
            except Exception as exc:
                self._material_failed(record, exc, cache)
                return False
            self._notify_report(name + " is already linked" + self._paths_suffix(nodes), lines)
            return nodes
        cancel = threading.Event()
        generation = self._generation
        self._material_tasks[key] = cancel
        notice = snapshot.get("notice", "") if isinstance(snapshot, dict) else ""

        def work():
            try:
                return "ok", backend.prepare_world(snapshot, cancel)
            except Exception as exc:
                return "error", exc

        def done(value, error):
            if self._material_tasks.get(key) is cancel:
                del self._material_tasks[key]
            if self._disposed:
                return
            self._update_busy()
            if generation != self._generation:
                return
            if cancel.is_set():
                self.notify("Import of " + name + " cancelled.")
                return
            if error:
                self.notify("Import failed: " + error, error=True)
                return
            kind, payload = value
            if kind == "error":
                self._material_failed(record, payload, cache)
                return
            lines, report = _report_collector()
            try:
                nodes = backend.import_asset(
                    record, mode=mode, prepared=payload, existing_environment=choice, report=report, **options
                )
            except Exception as exc:
                self._material_failed(record, exc, cache)
                return
            self._notify_report("Imported " + name + self._paths_suffix(nodes), lines, notice)
            self._refresh_importability(record)

        self.notify("Importing " + name + ": exporting from Blender..." + (" " + notice if notice else ""))
        self.run_task(work, done)
        self._update_busy()
        return True

    def start_append(self, record, *, network=None, position=None):
        """Append an object/collection: host snapshot, Blender export + conversion on a thread,
        geometry files and nodes on the GUI thread in one undo group."""
        key = "append|" + (record.get("ch") or {}).get("key", "")
        name = record.get("name", "")
        if key in self._material_tasks:
            self.notify(name + " is already being imported.")
            return False
        options = self._import_options()
        try:
            snapshot = self.backend.append_snapshot(
                record,
                library_root=options["library_root"],
                network=network,
                position=position,
                storage=self.settings["append_storage"],
            )
            # A re-import that changes files another scene uses asks first (blend-import.md: Import record), here on the
            # GUI thread, before the background prepare starts.
            check = getattr(self.backend, "shared_scenes", None)
            shared = check(snapshot) if callable(check) else []
        except Exception as exc:
            self.notify("Import failed: " + (str(exc) or type(exc).__name__), error=True)
            return False
        if shared:
            from .importers import shared_question

            if not self.ask_replace_shared(shared_question(shared)):
                self.notify("Import of " + name + " cancelled; nothing was changed.")
                return False
            snapshot["on_shared"] = "replace"
        cancel = threading.Event()
        generation = self._generation
        self._material_tasks[key] = cancel
        backend = self.backend
        notices = snapshot.get("notices", ()) if isinstance(snapshot, dict) else ()
        notice = " ".join(notices)

        def work():
            try:
                return "ok", backend.prepare_append(snapshot, cancel)
            except Exception as exc:
                return "error", exc

        def done(value, error):
            if self._material_tasks.get(key) is cancel:
                del self._material_tasks[key]
            if self._disposed:
                return
            self._update_busy()
            if generation != self._generation:
                return
            if cancel.is_set():
                self.notify("Import of " + name + " cancelled.")
                return
            kind, payload = value if value else ("error", error)
            if error or kind == "error":
                self.notify("Import failed: " + (error or str(payload) or type(payload).__name__), error=True)
                return
            lines, report = _report_collector()
            try:
                nodes = backend.import_asset(record, mode="append", prepared=payload, report=report, **options)
            except Exception as exc:
                self.notify("Import failed: " + (str(exc) or type(exc).__name__), error=True)
                return
            # The snapshot's notices (untitled scene, sequences) come back in the build report: once is enough.
            lines = [line for line in lines if line[0] not in notices]
            self._notify_report("Imported " + name + self._paths_suffix(nodes), lines, notice)

        self.notify("Importing " + name + ": converting in Blender..." + (" " + notice if notice else ""))
        self.run_task(work, done)
        self._update_busy()
        return True

    def reload_linked(self, record=None):
        """Reload the record's linked material or world: export in the background, swap in one undo group."""
        record = record or self.current_record()
        if record is None:
            return False
        try:
            linked = self.backend.linked_status(record)
            snapshot = self.backend.reload_snapshot(linked["path"]) if linked else None
        except Exception as exc:
            self.notify("Reload failed: " + (str(exc) or type(exc).__name__), error=True)
            return False
        if snapshot is None:
            self.notify(record.get("name", "") + " is not linked in this scene.", error=True)
            return False
        key = "reload|" + linked["path"]
        if key in self._material_tasks:
            self.notify(record.get("name", "") + " is already being reloaded.")
            return False
        cancel = threading.Event()
        generation = self._generation
        self._material_tasks[key] = cancel
        backend = self.backend

        def work():
            try:
                return "ok", backend.prepare_reload(snapshot, cancel)
            except Exception as exc:
                return "error", exc

        def done(value, error):
            if self._material_tasks.get(key) is cancel:
                del self._material_tasks[key]
            if self._disposed:
                return
            self._update_busy()
            if generation != self._generation or cancel.is_set():
                if cancel.is_set():
                    self.notify("Reload of " + record.get("name", "") + " cancelled.")
                return
            payload = value[1] if value else None
            if error or value[0] == "error":
                self.notify("Reload failed: " + (error or str(payload) or type(payload).__name__), error=True)
                return
            lines, report = _report_collector()
            try:
                nodes = backend.rebuild_linked(linked["path"], payload, report)
            except Exception as exc:
                self.notify("Reload failed: " + (str(exc) or type(exc).__name__), error=True)
                return
            self._notify_report("Reloaded " + record.get("name", "") + self._paths_suffix(nodes), lines)
            self._selection_changed()

        self.notify("Reloading " + record.get("name", "") + " from the library...")
        self.run_task(work, done)
        self._update_busy()
        return True

    def _material_failed(self, record, error, cache):
        reason = str(error) or type(error).__name__
        try:
            content = self.backend.is_content_failure(error)
        except Exception:
            content = False
        if content:
            # The exporter's verdict about this material: badge it until the file changes.
            try:
                self.backend.record_importability(record, cache, "no", reason)
            except Exception:
                pass
        self.notify("Import failed: " + reason, error=True)
        self._refresh_importability(record)

    def confirm_generate_listing(self, library):
        answer = QtWidgets.QMessageBox.warning(
            self,
            "Generate Blender Listing",
            GENERATE_LISTING_WARNING.format(name=library.name, path=library.path),
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.Cancel,
            QtWidgets.QMessageBox.Cancel,
        )
        return answer == QtWidgets.QMessageBox.Yes

    def generate_listing(self):
        """Option B: run Blender's listing generator INTO the library, after confirmation."""
        library = self._library
        if library is None or self._listing_running:
            return False
        if not self.confirm_generate_listing(library):
            return False
        try:
            snapshot = self.backend.blender_snapshot()
            provider = self.backend.make_provider(library, "listing")  # host thread
        except Exception as exc:
            self.notify(str(exc), error=True)
            return False
        backend = self.backend
        generation = self._generation

        def work():
            provider.generate_listing(backend.resolve_blender(snapshot))
            return "Blender listing generated for " + library.name

        def done(value, error):
            self._listing_running = False
            if self._disposed:
                return
            self._update_busy()
            if error:
                self.notify("Listing generation failed: " + error, error=True)
                self._update_empty()
                return
            if generation != self._generation or self._library is not library:
                self.notify(value)
                return
            if library.provider == "listing":
                self.notify(value)
                self._set_library(library)
            else:
                self.notify(value + "; switch the provider to Listing to use it.")

        self._listing_running = True
        self.notify("Generating the Blender listing for " + library.name + "...")
        self.run_task(work, done)
        self._update_busy()
        self._update_empty()
        return True

    def open_blend_file(self, record=None):
        record = record or self.current_record()
        source = ((record or {}).get("ch") or {}).get("source")
        if not source:
            return
        try:
            snapshot = self.backend.blender_snapshot()
        except Exception as exc:
            self.notify(str(exc), error=True)
            return
        backend = self.backend

        def work():
            if not os.path.isfile(source):
                raise FileNotFoundError("Source .blend file not found: " + source)
            return backend.launch_blender(backend.resolve_blender(snapshot), source)

        self.notify("Opening " + os.path.basename(source) + " in Blender...")
        self.run_task(work, lambda value, error: self.notify(error or value or "", error=bool(error)))

    def _context_menu(self, position):
        index = self.grid.indexAt(position)
        record = record_at(index)
        if record is None:
            return
        self.grid.setCurrentIndex(index)
        from .importers import unsupported_reason

        menu = QtWidgets.QMenu(self)
        action = menu.addAction("Import", lambda: self.import_record(record))
        action.setEnabled(not unsupported_reason(record))
        if record.get("id_type") == "MATERIAL":
            menu.addAction("Assign to Selected Objects", lambda: self.assign_record(record))
        menu.addAction("Open Blend File", lambda: self.open_blend_file(record))
        menu.addSeparator()
        clipboard = QtWidgets.QApplication.clipboard()
        menu.addAction("Copy Name", lambda: clipboard.setText(record.get("name", "")))
        menu.addAction("Copy Source Path", lambda: clipboard.setText((record.get("ch") or {}).get("source", "")))
        menu.exec(self.grid.viewport().mapToGlobal(position))

    def _begin_drag(self, view, record):
        if not self.drag_active():
            return None
        from . import drag

        def report(text, error):
            if error:
                self.notify("Drop failed (experimental): " + error, error=True)
            elif text:
                self.notify(text + " (experimental drop)")
            self._refresh_importability(record)

        backend, options = self.backend, self._import_options()
        shape = self.settings["reference_shape"]

        def importer(record, *, mode, network=None, position=None):
            if record.get("id_type") == "WORLD":
                # Worlds land in /obj or /mat wherever they are dropped.
                nodes = self.start_world(record, mode=self._effective_mode(record))
                return nodes if isinstance(nodes, list) else []
            if record.get("id_type") == "MATERIAL":
                path = network.path() if hasattr(network, "path") else network
                mode = self._effective_mode(record)
                if mode == "link" and backend.find_imported_material(record, "link") is not None:
                    return backend.import_asset(record, mode="link", **options)
                self.start_material(record, network=path, position=position, mode=mode)
                return []
            if self._effective_mode(record) == "append":
                # As for materials, the header's import mode decides for drops too.
                path = network.path() if hasattr(network, "path") else network
                self.start_append(record, network=path, position=position)
                return []
            return backend.import_asset(
                record, mode=mode, network=network, position=position, viewport_shape=shape, **options
            )

        def assign(record, nodes):
            paths = [node.path() for node in nodes]
            mode = self._effective_mode(record)
            if backend.find_imported_material(record, mode) is not None:
                return backend.assign_material(record, nodes=paths, mode=mode, **options)
            self.start_material(record, assign_nodes=paths, mode=mode)
            return _Pending(record.get("name", "")), nodes

        return drag.begin_drag(
            view, record, self.settings["drag_route"], on_result=report, importer=importer, assign=assign
        )

    # ------------------------------------------------------------ teardown

    def dispose(self):
        if self._disposed:
            return
        if self.enabled and self.model is not None:
            self._save_timer.stop()
            self.save_settings()
        self._disposed = True
        if self.timer is not None:
            self.timer.stop()
        if self._chrome is not None:
            self._chrome.timer.stop()
        self.cancel_scan()
        for event in self._material_tasks.values():
            event.set()
        self._material_tasks.clear()
        if self._shortcuts is not None:
            self._shortcuts.close()
        if self.model is not None:
            self.model.loader.close()
            self.model.set_records([])
        self._provider = None
        self._library = None
        self._tree = None

    def closeEvent(self, event):
        self.dispose()
        super().closeEvent(event)
