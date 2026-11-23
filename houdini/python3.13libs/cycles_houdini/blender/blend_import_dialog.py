# SPDX-License-Identifier: Apache-2.0
"""File > Import > Blender (.blend): pick collections/objects of a .blend and import them.

Host-thread rule: HOM only on the GUI thread, through `HoudiniBackend`. The
Blender listing and the converter's `prepare_import` run on background threads;
their results come back through a queue drained by a QTimer. `build_import`
runs on the GUI thread. The converter (`blend_convert`, contract in
docs/dev/blend-import.md) is imported lazily. The .blend file is only read.
"""

import importlib
import inspect
import json
import os
import queue
import tempfile
import threading
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

# Houdini's File > Import submenu (Houdini 22.0 $HFS/houdini/MainMenuCommon.xml: subMenu id="import_files")
# and the item the Blender entry follows. houdini/MainMenuCommon.xml must use the same ids
# (tests/pytest_cases/test_blend_import_dialog.py checks that).
FILE_IMPORT_MENU_ID = "import_files"
FILE_IMPORT_INSERT_AFTER = "h.import_gltf"

# Property scoping the shared Cycles chrome (viewer_layout.scoped_style) to this dialog.
DIALOG_SCOPE = "cycles_blend_import"
TITLE = "Import Blender (.blend)"
DEFAULT_ASSET_ROOT = "$HIP/assets_blender"
# The stored defaults of earlier builds; read as DEFAULT_ASSET_ROOT.
OBSOLETE_ASSET_ROOTS = ("$HIP/geo/assets_blender", "$HIP/geo/blender_assets")
SETTINGS_FILENAME = "blend_import_settings.json"
SETTINGS_VERSION = 1
_MAX_SETTINGS_BYTES = 256 * 1024

# Mode and Save Location (blend-import.md: Importer dialog) map to the converter's storage: Link -> "reference";
# Convert & Append -> the Save Location, "files" or "embedded".
MODE_CHOICES = (
    (
        "link",
        "Link",
        "Cycles Blender File nodes that read the .blend while rendering. Stays in sync with the file; needs Blender.",
    ),
    (
        "convert",
        "Convert & Append",
        "Convert to Houdini geometry; Update on the node converts again. No Blender needed to render.",
    ),
)
SAVE_LOCATION_CHOICES = (
    (
        "files",
        "Save to Disk as .bgeo.sc",
        "Write .bgeo.sc files to <asset folder>/<blend>/geo/<asset>/, loaded with File nodes.",
    ),
    (
        "embedded",
        "Stash in .hip",
        "Keep the converted geometry inside the .hip file (Stash SOP). One frame; makes the .hip larger.",
    ),
)
STORAGES = ("reference", "embedded", "files")
# Stored "storage" values of earlier builds and their (mode, save location).
_LEGACY_STORAGE = {"reference": ("link", None), "files": ("convert", "files"), "embedded": ("convert", "embedded")}
# Structure (blend-import.md: Importer dialog): "packed" creates one Cycles Blender File node per selected item (in its
# own geo object); "hierarchy" one Cycles Blender Hierarchy object. Hierarchy needs Convert & Append:
# with Mode Link the Structure row is hidden and the import is "packed".
STRUCTURE_CHOICES = (
    (
        "packed",
        "Without hierarchy",
        "One geometry object per selected item, holding a Cycles Blender File node. Blender objects "
        "are packed primitives with name and path; set Load As to Unpacked on the node to unpack them.",
    ),
    (
        "hierarchy",
        "Hierarchy",
        "One Cycles Blender Hierarchy object holding one Houdini object per Blender object, parented as "
        "in Blender. Convert & Append only.",
    ),
)
# Stored or scripted Structure values of earlier builds.
_LEGACY_STRUCTURE = {"merged": "packed"}
MATERIAL_CHOICES = (
    ("link", "Link (shared, read-only)", "Use shared translated materials that follow the .blend file."),
    ("append", "Append (editable copy)", "Translate the materials into editable copies in this scene."),
    ("none", "None", "Import geometry only; keep the Blender material names as attributes."),
)
FRAME_CHOICES = (
    ("current", "Current frame", "Import the scene's current frame."),
    ("range", "Frame range", "Write one file per frame (Save to Disk only)."),
)
SEQUENCE_NOTE = "Frame ranges write one file per frame: heavy on disk and slow to import."
DISABLED_NOTE = "Disabled in the view layer"
DISABLED_TOOLTIP = (
    "This collection is excluded from the active view layer in Blender. Inside a selected parent it is "
    "skipped unless Include Disabled Collections is on; selecting it directly imports it."
)
BRIDGE_DISABLED = (
    "The Blender bridge is disabled for this installation. Run Cycles Setup again and select "
    "'Blender .blend asset bridge' to import .blend files."
)
BLENDER_MISSING = "Blender not found. Install Blender or set CYCLES_HOUDINI_BLENDER to its executable, then try again."

OBJECT_TYPE_LABELS = {
    "MESH": "Mesh",
    "CURVES": "Hair Curves",
    "CURVE": "Curve",
    "SURFACE": "Surface",
    "META": "Metaball",
    "FONT": "Text",
    "POINTCLOUD": "Point Cloud",
    "VOLUME": "Volume",
    "GREASEPENCIL": "Grease Pencil",
    "GPENCIL": "Grease Pencil",
    "ARMATURE": "Armature",
    "LATTICE": "Lattice",
    "EMPTY": "Empty",
    "LIGHT": "Light",
    "LIGHT_PROBE": "Light Probe",
    "CAMERA": "Camera",
    "SPEAKER": "Speaker",
}

DEFAULTS = {
    "mode": "convert",
    "save_location": "files",
    "structure": "packed",
    "materials": "link",
    "frames": "current",
    "frame_step": 1,
    "include_disabled": False,
    "apply_subdivision": False,
    "nested_instances": True,
    "asset_root": DEFAULT_ASSET_ROOT,
    "last_directory": "",
}

KIND_ROLE = QtCore.Qt.UserRole + 1
NAME_ROLE = QtCore.Qt.UserRole + 2
EXCLUDED_ROLE = QtCore.Qt.UserRole + 3
OWN_STATE_ROLE = QtCore.Qt.UserRole + 4


def _values(choices):
    return tuple(choice[0] for choice in choices)


def _host_path(path):
    """Plugin convention: forward slashes, also on Windows."""
    return str(path).replace("\\", "/")


# ---------------------------------------------------------------- settings (pure)


def settings_path():
    """CYCLES_HOUDINI_BLEND_IMPORT_SETTINGS_FILE, else beside the asset browser settings."""
    explicit = os.environ.get("CYCLES_HOUDINI_BLEND_IMPORT_SETTINGS_FILE", "").strip()
    if explicit:
        return Path(os.path.expanduser(explicit))
    from ..assets import settings as browser_settings

    return Path(browser_settings.settings_path()).with_name(SETTINGS_FILENAME)


def storage_for(mode, save_location):
    """The converter's storage for a Mode and Save Location ("reference", "files", "embedded")."""
    if mode == "link":
        return "reference"
    if mode != "convert":
        raise ValueError("Unknown import mode: " + repr(mode))
    if save_location not in _values(SAVE_LOCATION_CHOICES):
        raise ValueError("Unknown save location: " + repr(save_location))
    return save_location


def normalize_settings(values):
    """Complete settings dict; every invalid field takes its default.

    Settings of earlier builds load without error: ``storage`` becomes Mode and Save
    Location, the removed Structure ``merged`` becomes ``packed`` (Without hierarchy), the
    old default asset folders (``$HIP/geo/assets_blender``, ``$HIP/geo/blender_assets``) become ``$HIP/assets_blender``,
    ``texture_location`` and unknown keys are dropped.
    """
    values = values if isinstance(values, dict) else {}
    if values.get("structure") in _LEGACY_STRUCTURE:
        values = dict(values, structure=_LEGACY_STRUCTURE[values["structure"]])
    result = dict(DEFAULTS)
    legacy_mode, legacy_location = _LEGACY_STORAGE.get(values.get("storage"), (None, None))
    if legacy_mode:
        result["mode"] = legacy_mode
    if legacy_location:
        result["save_location"] = legacy_location
    for name, choices in (
        ("mode", MODE_CHOICES),
        ("save_location", SAVE_LOCATION_CHOICES),
        ("structure", STRUCTURE_CHOICES),
        ("materials", MATERIAL_CHOICES),
        ("frames", FRAME_CHOICES),
    ):
        if values.get(name) in _values(choices):
            result[name] = values[name]
    step = values.get("frame_step")
    if type(step) is int and 1 <= step <= 10000:
        result["frame_step"] = step
    for name in ("include_disabled", "apply_subdivision", "nested_instances"):
        if isinstance(values.get(name), bool):
            result[name] = values[name]
    for name in ("asset_root", "last_directory"):
        value = values.get(name)
        if isinstance(value, str) and len(value) < 4096 and "\0" not in value:
            result[name] = value
    if not result["asset_root"].strip() or result["asset_root"].strip().rstrip("/\\") in OBSOLETE_ASSET_ROOTS:
        result["asset_root"] = DEFAULT_ASSET_ROOT
    return result


def load_settings(path=None):
    path = Path(path) if path is not None else settings_path()
    try:
        if path.stat().st_size > _MAX_SETTINGS_BYTES:
            return normalize_settings({})
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return normalize_settings({})
    if not isinstance(data, dict):
        return normalize_settings({})
    return normalize_settings(data.get("settings") if isinstance(data.get("settings"), dict) else data)


def save_settings(values, path=None):
    """Atomic write (temp file + os.replace) of the normalized settings."""
    path = Path(path) if path is not None else settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": SETTINGS_VERSION, "settings": normalize_settings(values)}
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return payload["settings"]


# ---------------------------------------------------------------- options -> converter (pure)


def converter_arguments(source, selection, options, executable=None):
    """Map dialog options to blend_convert.import_blend/prepare_import arguments.

    Options carry ``mode`` ("link" | "convert") and ``save_location`` ("files" |
    "embedded"), or a converter ``storage`` directly (scripts of earlier builds).
    Returns (source, selection, kwargs). `frames` is a (start, end, step) tuple
    only for storage "files" (Save to Disk) with a frame range; otherwise None
    (current frame). Link imports are always "packed" (Hierarchy implies Convert &
    Append); the removed "merged" is refused with the converter's explanation.
    """
    if "mode" in options:
        storage = storage_for(options["mode"], options.get("save_location", DEFAULTS["save_location"]))
    else:
        storage = options.get("storage", storage_for(DEFAULTS["mode"], DEFAULTS["save_location"]))
    if storage not in STORAGES:
        raise ValueError("Unknown import storage: " + repr(storage))
    structure = options.get("structure", DEFAULTS["structure"])
    if structure == "merged":
        from .blend_convert import MERGED_REMOVED

        raise ValueError(MERGED_REMOVED)
    if structure not in _values(STRUCTURE_CHOICES):
        raise ValueError("Unknown import structure: " + repr(structure))
    if storage == "reference":
        structure = "packed"  # Link + Hierarchy is not offered
    materials = options.get("materials", DEFAULTS["materials"])
    if materials not in _values(MATERIAL_CHOICES):
        raise ValueError("Unknown material mode: " + repr(materials))
    if not selection:
        raise ValueError("Select at least one collection or object to import.")
    for kind, name in selection:
        if kind not in ("collection", "object") or not isinstance(name, str) or not name:
            raise ValueError("Invalid import selection: " + repr((kind, name)))
    frames = None
    if storage == "files" and options.get("frames") == "range":
        start, end, step = (options.get(key) for key in ("frame_start", "frame_end", "frame_step"))
        if any(type(value) is not int for value in (start, end, step)) or step < 1 or end < start:
            raise ValueError("The frame range needs a start, an end not before the start, and a step of at least 1.")
        frames = (start, end, step)
    asset_root = str(options.get("asset_root") or "").strip() or DEFAULT_ASSET_ROOT
    kwargs = {
        "storage": storage,
        "structure": structure,
        "materials": materials,
        "frames": frames,
        "include_disabled": bool(options.get("include_disabled", False)),
        "apply_subdivision": bool(options.get("apply_subdivision", False)),
        "nested_instances": bool(options.get("nested_instances", True)),
        "asset_root": _host_path(asset_root),
        "executable": executable,
    }
    return _host_path(source), [(kind, name) for kind, name in selection], kwargs


def accepted_kwargs(function, kwargs):
    """Only the keyword arguments `function` accepts (all of them with **kwargs)."""
    try:
        parameters = inspect.signature(function).parameters.values()
    except (TypeError, ValueError):
        return dict(kwargs)
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters):
        return dict(kwargs)
    names = {
        parameter.name
        for parameter in parameters
        if parameter.kind in (inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    }
    return {key: value for key, value in kwargs.items() if key in names}


# ---------------------------------------------------------------- listing -> tree (pure)


def scene_tree(listing, scene_name=None):
    """Nested display rows for a checked listing (blend_worker.checked_listing).

    Returns a list of nodes {"kind": "collection"|"object"|"group", "name", "data", "children"}:
    the scene's top-level collections (recursively) and the objects linked
    directly to the scene, then a group of collections that are not in the
    scene. A collection with several parents appears under each of them.
    """
    collections = {entry["name"]: entry for entry in listing.get("collections", ())}
    objects = {entry["name"]: entry for entry in listing.get("objects", ())}
    scenes = {entry["name"]: entry for entry in listing.get("scenes", ())}
    scene = scenes.get(scene_name or listing.get("active_scene")) or next(iter(scenes.values()), None)

    def collection_node(name, ancestors):
        entry = collections[name]
        children = []
        for child in entry["children"]:
            if child in collections and child not in ancestors:
                children.append(collection_node(child, ancestors | {name}))
        children.extend(
            {"kind": "object", "name": obj, "data": objects[obj], "children": []}
            for obj in entry["objects"]
            if obj in objects
        )
        return {"kind": "collection", "name": name, "data": entry, "children": children}

    nodes = []
    shown = set()
    if scene is not None:
        for name in scene.get("collections", ()):
            if name in collections:
                nodes.append(collection_node(name, frozenset()))
        nodes.extend(
            {"kind": "object", "name": obj, "data": objects[obj], "children": []}
            for obj in scene.get("objects", ())
            if obj in objects
        )

    def mark(node):
        if node["kind"] == "collection":
            shown.add(node["name"])
        for child in node["children"]:
            mark(child)

    for node in nodes:
        mark(node)
    others = [
        name
        for name, entry in collections.items()
        if name not in shown and not any(p in collections for p in entry["parents"])
    ]
    if others:
        nodes.append(
            {
                "kind": "group",
                "name": "Not in the scene",
                "data": None,
                "children": [collection_node(name, frozenset()) for name in others],
            }
        )
    return nodes


def implied_names(listing, selection, include_disabled):
    """Collections and objects a selection of collections already imports (as (kind, name) keys)."""
    collections = {entry["name"]: entry for entry in listing.get("collections", ())}
    result = set()
    pending = [name for kind, name in selection if kind == "collection" and name in collections]
    seen = set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        entry = collections[name]
        for obj in entry["objects"]:
            result.add(("object", obj))
        for child in entry["children"]:
            if child in collections and (include_disabled or not collections[child]["excluded"]):
                result.add(("collection", child))
                pending.append(child)
    return result


def selection_without_implied(listing, selection, include_disabled):
    """Drop items a selected collection already imports, keeping order and removing duplicates."""
    selection = list(dict.fromkeys(selection))
    implied = implied_names(listing, selection, include_disabled)
    return [item for item in selection if item not in implied]


# ---------------------------------------------------------------- HOM backend


class HoudiniBackend:
    """Every HOM, Blender and converter call of the dialog (GUI thread unless noted)."""

    def _hou(self):
        import hou

        return hou

    def enabled(self):
        from ..core import blender_bridge

        return blender_bridge.enabled()

    def settings_path(self):
        return settings_path()

    def blender_snapshot(self):
        """GUI thread: configuration only (may read hou env); no filesystem discovery."""
        from ..core import blender_bridge

        if not blender_bridge.enabled():
            raise ValueError(BRIDGE_DISABLED)
        return (
            blender_bridge._configured_executable(),
            os.environ.get("PATH", ""),
            os.environ.get("ProgramFiles", "C:/Program Files"),
        )

    def resolve_blender(self, snapshot):
        """Background thread: filesystem discovery from the GUI-thread snapshot."""
        from ..core import blender_bridge

        executable = blender_bridge._discover(*snapshot)
        if not executable or not os.path.isfile(executable):
            raise FileNotFoundError(BLENDER_MISSING)
        return executable

    def list_contents(self, source, executable, cancelled):
        """Background thread."""
        from . import blend_worker

        return blend_worker.list_blend_contents(source, executable, timeout=120, cancelled=cancelled)

    def expand(self, path):
        return self._hou().text.expandString(path)

    def select_file(self, start, title):
        hou = self._hou()
        chosen = hou.ui.selectFile(
            start_directory=start or None,
            title=title,
            pattern="*.blend",
            chooser_mode=hou.fileChooserMode.Read,
        )
        return hou.text.expandString(chosen) if chosen else ""

    def select_directory(self, start, title):
        hou = self._hou()
        return hou.ui.selectFile(
            start_directory=start or None,
            title=title,
            file_type=hou.fileType.Directory,
            chooser_mode=hou.fileChooserMode.Read,
        )

    def converter(self):
        return importlib.import_module(__package__ + ".blend_convert")

    def prepare(self, source, selection, kwargs, cancelled, report):
        """Background thread: the converter's Blender phase."""
        convert = self.converter()
        options = dict(kwargs, cancelled=cancelled, report=report)
        return convert.prepare_import(source, selection, **accepted_kwargs(convert.prepare_import, options))

    def build(self, payload):
        """GUI thread: the converter's Houdini phase (one undo group inside the converter)."""
        return self.converter().build_import(payload)

    def shared_scenes(self, source, selection, kwargs):
        """GUI thread, before the background prepare: other scenes a re-import would change."""
        return self.converter().shared_scenes(
            source, selection, asset_root=kwargs.get("asset_root"), structure=kwargs.get("structure")
        )

    def ask_replace(self, question):
        """GUI thread: Replace (True) or Cancel (False)."""
        hou = self._hou()
        choice = hou.ui.displayMessage(
            question,
            buttons=("Replace", "Cancel"),
            default_choice=1,
            close_choice=1,
            severity=hou.severityType.Warning,
            title=TITLE,
        )
        return choice == 0

    def select_nodes(self, nodes):
        hou = self._hou()
        try:
            hou.clearAllSelected()
            for node in nodes:
                node.setSelected(True)
        except Exception:
            pass

    def main_window(self):
        return self._hou().qt.mainWindow()

    def message(self, text):
        hou = self._hou()
        hou.ui.displayMessage(text, severity=hou.severityType.Warning, title=TITLE)


# ---------------------------------------------------------------- dialog


def dialog_style(theme):
    """Asset-browser rules (same vocabulary and shapes) re-scoped to this dialog, plus tree notes."""
    from ..assets.widgets import asset_style

    rules = asset_style(theme).replace('[cycles_asset_panel="true"]', f'[{DIALOG_SCOPE}="true"]')
    scope = f'QWidget[{DIALOG_SCOPE}="true"]'
    muted = theme["muted"].name() if "muted" in theme else "gray"
    background = theme["background"].name() if "background" in theme else "transparent"
    text = theme["text"].name() if "text" in theme else "white"
    return rules + (
        f'\n{scope} QLabel[cycles_note="true"] {{ color: {muted}; }}'
        f"\n{scope} QTreeWidget {{ background: {background}; color: {text}; }}"
        f"\n{scope} QGroupBox {{ color: {muted}; }}"
    )


class _SelectionTree:
    """The collection/object tree with check boxes, shared by the importer and the Blender
    Hierarchy object's Edit Selection dialog. The host class provides ``_listing``,
    ``_updating``, ``_theme``, ``_chrome``, ``_include_disabled()`` and ``_update_enabled()``."""

    def _build_tree(self):
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setProperty("cycles_asset_tree", True)
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["Name", "Type", "Notes"])
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        header = self.tree.header()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.Interactive)
        self.tree.itemChanged.connect(self._item_changed)
        self.tree.itemExpanded.connect(lambda *_: self.tree.resizeColumnToContents(0))
        return self.tree

    def _refresh_icons(self):
        if self._theme is None:
            return
        try:
            from ..assets.widgets import glyph_pixmap
        except Exception:
            return
        size = max(12, round(14 * (self._chrome.scale if self._chrome is not None else 1.0)))
        color = self._theme.get("text", QtGui.QColor("white"))
        muted = self._theme.get("muted", color)
        icons = {}
        for item in self._items():
            kind = item.data(0, KIND_ROLE)
            if kind not in ("collection", "object"):
                continue
            key = (kind, bool(item.data(0, EXCLUDED_ROLE)))
            if key not in icons:
                pixmap = glyph_pixmap(
                    "COLLECTION" if kind == "collection" else "OBJECT", size, muted if key[1] else color
                )
                icons[key] = QtGui.QIcon(pixmap)
            item.setIcon(0, icons[key])

    # ------------------------------------------------------------ tree

    def _items(self):
        iterator = QtWidgets.QTreeWidgetItemIterator(self.tree)
        while iterator.value():
            yield iterator.value()
            iterator += 1

    def _fill_tree(self, listing):
        self._updating = True
        self.tree.blockSignals(True)
        try:
            self.tree.clear()
            scene = next(
                (entry for entry in listing.get("scenes", ()) if entry["name"] == listing.get("active_scene")), None
            )
            top = set(scene.get("collections", ())) if scene else set()
            muted = self.palette().color(QtGui.QPalette.Disabled, QtGui.QPalette.Text)

            def add(parent, node):
                item = QtWidgets.QTreeWidgetItem(parent)
                kind, data = node["kind"], node["data"]
                item.setText(0, node["name"])
                item.setData(0, KIND_ROLE, kind)
                item.setData(0, NAME_ROLE, node["name"])
                notes, tip = [], node["name"]
                if kind == "group":
                    item.setFlags(QtCore.Qt.ItemIsEnabled)
                    item.setText(2, "Collections not linked to the scene")
                elif kind == "collection":
                    item.setFlags(QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsSelectable | QtCore.Qt.ItemIsUserCheckable)
                    item.setText(1, "Collection")
                    if data["excluded"]:
                        notes.append(DISABLED_NOTE)
                        tip = DISABLED_TOOLTIP
                        item.setData(0, EXCLUDED_ROLE, True)
                        for column in range(3):
                            item.setForeground(column, muted)
                    if data["hide_render"]:
                        notes.append("Hidden in renders")
                    if data.get("library"):
                        notes.append("Linked from " + os.path.basename(data["library"]))
                    default = node["name"] in top and not data["excluded"] and parent is self.tree
                    state = QtCore.Qt.Checked if default else QtCore.Qt.Unchecked
                    item.setCheckState(0, state)
                    item.setData(0, OWN_STATE_ROLE, int(state == QtCore.Qt.Checked))
                else:
                    item.setFlags(QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsSelectable | QtCore.Qt.ItemIsUserCheckable)
                    label = OBJECT_TYPE_LABELS.get(data["type"], data["type"].replace("_", " ").title())
                    item.setText(1, label)
                    if data["has_geometry_nodes"]:
                        notes.append("Geometry Nodes")
                    if data.get("instance_collection"):
                        notes.append("Instances " + data["instance_collection"])
                    if data["parent"]:
                        notes.append("Child of " + data["parent"])
                    if data["hide_render"]:
                        notes.append("Hidden in renders")
                    slots = [slot for slot in data["material_slots"] if slot]
                    if slots:
                        tip += "\nMaterials: " + ", ".join(slots)
                    default = parent is self.tree
                    state = QtCore.Qt.Checked if default else QtCore.Qt.Unchecked
                    item.setCheckState(0, state)
                    item.setData(0, OWN_STATE_ROLE, int(state == QtCore.Qt.Checked))
                item.setText(2, ", ".join(notes) if notes else item.text(2))
                item.setToolTip(0, tip)
                item.setToolTip(2, item.text(2))
                for child in node["children"]:
                    add(item, child)
                return item

            for node in scene_tree(listing):
                add(self.tree, node)
            # A collection under several parents is one selectable unit: checked anywhere, checked everywhere.
            checked = {
                (item.data(0, KIND_ROLE), item.data(0, NAME_ROLE))
                for item in self._items()
                if item.data(0, OWN_STATE_ROLE)
            }
            for item in self._items():
                if (item.data(0, KIND_ROLE), item.data(0, NAME_ROLE)) in checked:
                    item.setData(0, OWN_STATE_ROLE, 1)
                    item.setCheckState(0, QtCore.Qt.Checked)
            self.tree.expandToDepth(1)
            for item in self._items():
                if item.data(0, KIND_ROLE) == "group":
                    item.setExpanded(False)
            self.tree.resizeColumnToContents(0)
            self.tree.resizeColumnToContents(1)
        finally:
            self.tree.blockSignals(False)
            self._updating = False
        self._refresh_implied()
        self._refresh_icons()

    def _item_changed(self, item, column):
        if self._updating or column != 0 or not (item.flags() & QtCore.Qt.ItemIsUserCheckable):
            return
        if not (item.flags() & QtCore.Qt.ItemIsEnabled):
            return
        checked = item.checkState(0) == QtCore.Qt.Checked
        key = (item.data(0, KIND_ROLE), item.data(0, NAME_ROLE))
        self._updating = True
        self.tree.blockSignals(True)
        try:
            # Every occurrence of the same collection/object shares one state.
            for other in self._items():
                if (other.data(0, KIND_ROLE), other.data(0, NAME_ROLE)) == key:
                    other.setData(0, OWN_STATE_ROLE, int(checked))
                    if other is not item:
                        other.setCheckState(0, QtCore.Qt.Checked if checked else QtCore.Qt.Unchecked)
        finally:
            self.tree.blockSignals(False)
            self._updating = False
        self._refresh_implied()
        self._update_enabled()

    def _own_selection(self):
        result = []
        for item in self._items():
            kind = item.data(0, KIND_ROLE)
            if kind in ("collection", "object") and item.data(0, OWN_STATE_ROLE):
                result.append((kind, item.data(0, NAME_ROLE)))
        return list(dict.fromkeys(result))

    def _refresh_implied(self):
        """Items a checked collection already imports show checked and disabled."""
        if self._listing is None:
            return
        include = self._include_disabled()
        implied = implied_names(self._listing, self._own_selection(), include)
        self._updating = True
        self.tree.blockSignals(True)
        try:
            for item in self._items():
                kind = item.data(0, KIND_ROLE)
                if kind not in ("collection", "object"):
                    continue
                key = (kind, item.data(0, NAME_ROLE))
                flags = item.flags()
                if key in implied:
                    item.setFlags(flags & ~QtCore.Qt.ItemIsEnabled)
                    item.setCheckState(0, QtCore.Qt.Checked)
                else:
                    item.setFlags(flags | QtCore.Qt.ItemIsEnabled)
                    item.setCheckState(0, QtCore.Qt.Checked if item.data(0, OWN_STATE_ROLE) else QtCore.Qt.Unchecked)
        finally:
            self.tree.blockSignals(False)
            self._updating = False

    def selection(self):
        """The converter selection: [("collection"|"object", name)], without implied items."""
        if self._listing is None:
            return []
        return selection_without_implied(self._listing, self._own_selection(), self._include_disabled())

    def set_checked(self, kind, name, checked=True):
        """Check or uncheck every occurrence of an item (as a click would)."""
        for item in self._items():
            if (item.data(0, KIND_ROLE), item.data(0, NAME_ROLE)) == (kind, name):
                item.setCheckState(0, QtCore.Qt.Checked if checked else QtCore.Qt.Unchecked)
                return True
        return False

    def apply_selection(self, selection):
        """Check exactly the items of ``selection`` (as clicks would); returns those not in the tree."""
        wanted = {tuple(item) for item in selection}
        found = set()
        self._updating = True
        self.tree.blockSignals(True)
        try:
            for item in list(self._items()):
                key = (item.data(0, KIND_ROLE), item.data(0, NAME_ROLE))
                if key[0] in ("collection", "object"):
                    item.setData(0, OWN_STATE_ROLE, int(key in wanted))
                    if key in wanted:
                        found.add(key)
        finally:
            self.tree.blockSignals(False)
            self._updating = False
        self._refresh_implied()
        self._update_enabled()
        return [item for item in selection if tuple(item) not in found]

    def clear_checks(self):
        self._updating = True
        self.tree.blockSignals(True)
        try:
            for item in list(self._items()):
                if item.data(0, KIND_ROLE) in ("collection", "object"):
                    item.setData(0, OWN_STATE_ROLE, 0)
        finally:
            self.tree.blockSignals(False)
            self._updating = False
        self._refresh_implied()
        self._update_enabled()


class BlendImportDialog(_SelectionTree, QtWidgets.QDialog):
    def __init__(self, parent=None, *, backend=None):
        super().__init__(parent)
        self.backend = backend or HoudiniBackend()
        self.setWindowTitle(TITLE)
        self.setObjectName("cycles_blend_import_dialog")
        self.setModal(False)
        self._disposed = False
        self._results = queue.SimpleQueue()
        self._tasks = 0
        self._generation = 0
        self._listing = None
        self._source = ""
        self._executable = None
        self._list_cancel = None
        self._import_cancel = None
        self._importing = False
        self._updating = False
        self._theme = None
        self._chrome = None
        self.created = []
        self._settings_path = self.backend.settings_path()
        self.settings = load_settings(self._settings_path)
        self._build()
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.poll)
        self._apply_settings()
        self._update_enabled()
        try:
            from ..viewer import panel_theme

            self._chrome = panel_theme.install(
                self, DIALOG_SCOPE, extra_style=dialog_style, on_theme=self._theme_applied
            )
            self._chrome.capture_layouts()
            self._chrome.update(force=True)
        except Exception:
            # Theming must never prevent importing; Qt defaults remain usable.
            self._chrome = None
        em = self.fontMetrics().averageCharWidth()
        self.resize(em * 110, self.fontMetrics().lineSpacing() * 34)

    # ------------------------------------------------------------ layout

    def _combo(self, choices):
        combo = QtWidgets.QComboBox()
        for choice in choices:
            combo.addItem(choice[1], choice[0])
            if len(choice) > 2:
                combo.setItemData(combo.count() - 1, choice[2], QtCore.Qt.ToolTipRole)
        return combo

    def _build(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        file_row = QtWidgets.QHBoxLayout()
        file_row.addWidget(QtWidgets.QLabel("Blender file"))
        self.file_edit = QtWidgets.QLineEdit()
        self.file_edit.setPlaceholderText("Choose a .blend file")
        self.file_edit.returnPressed.connect(lambda: self.load_file(self.file_edit.text()))
        file_row.addWidget(self.file_edit, 1)
        self.browse_button = QtWidgets.QPushButton("Browse...")
        self.browse_button.clicked.connect(self.browse)
        file_row.addWidget(self.browse_button)
        self.reload_button = QtWidgets.QPushButton("Reload")
        self.reload_button.setToolTip("Read the .blend file again")
        self.reload_button.clicked.connect(lambda: self.load_file(self.file_edit.text()))
        file_row.addWidget(self.reload_button)
        layout.addLayout(file_row)

        body = QtWidgets.QHBoxLayout()
        body.setSpacing(10)
        body.addWidget(self._build_tree(), 3)

        options = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(options)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(6)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
        self._form = form
        self.mode_combo = self._combo(MODE_CHOICES)
        self.mode_combo.setToolTip("Link: Blender File nodes that read the .blend. Convert & Append: Houdini geometry.")
        form.addRow("Mode", self.mode_combo)
        self.save_location_combo = self._combo(SAVE_LOCATION_CHOICES)
        self.save_location_combo.setToolTip("Where the converted geometry is kept")
        form.addRow("Save Location", self.save_location_combo)
        self.structure_combo = self._combo(STRUCTURE_CHOICES)
        self.structure_combo.setToolTip(
            "Without hierarchy: a Blender File node per selected item. Hierarchy: one Blender Hierarchy "
            "object with Blender's parenting."
        )
        form.addRow("Structure", self.structure_combo)
        self.materials_combo = self._combo(MATERIAL_CHOICES)
        form.addRow("Materials", self.materials_combo)
        self.frames_combo = self._combo(FRAME_CHOICES)
        form.addRow("Frames", self.frames_combo)
        range_row = QtWidgets.QHBoxLayout()
        range_row.setSpacing(4)
        self.start_spin, self.end_spin, self.step_spin = (QtWidgets.QSpinBox() for _ in range(3))
        for spin, label in ((self.start_spin, "Start"), (self.end_spin, "End"), (self.step_spin, "Step")):
            spin.setRange(-1000000, 1000000) if spin is not self.step_spin else spin.setRange(1, 10000)
            spin.setToolTip(label + " frame" if spin is not self.step_spin else "Write every Nth frame")
            range_row.addWidget(QtWidgets.QLabel(label))
            range_row.addWidget(spin, 1)
        self.range_widget = QtWidgets.QWidget()
        self.range_widget.setLayout(range_row)
        range_row.setContentsMargins(0, 0, 0, 0)
        form.addRow("", self.range_widget)
        self.sequence_note = QtWidgets.QLabel(SEQUENCE_NOTE)
        self.sequence_note.setProperty("cycles_note", True)
        self.sequence_note.setWordWrap(True)
        form.addRow("", self.sequence_note)
        self.include_disabled_check = QtWidgets.QCheckBox("Include Disabled Collections")
        self.include_disabled_check.setToolTip(
            "Also import child collections that are excluded from the active view layer in Blender."
        )
        form.addRow("", self.include_disabled_check)
        self.apply_subdivision_check = QtWidgets.QCheckBox("Apply Subdivision")
        self.apply_subdivision_check.setToolTip(
            "Off: an object whose last modifier is Subdivision Surface is imported as its control cage, "
            "and Cycles subdivides it at render time (levels and creases on the object's Cycles tab). "
            "On: the subdivided mesh is baked into the geometry."
        )
        form.addRow("", self.apply_subdivision_check)
        self.nested_instances_check = QtWidgets.QCheckBox("Keep Nested Instances")
        self.nested_instances_check.setToolTip(
            "On: geometry nodes instances of instances stay packed inside packed, as in Blender's "
            "instance tree (one prototype per instanced geometry). Off: every instance is a top-level "
            "packed primitive (still sharing its geometry)."
        )
        form.addRow("", self.nested_instances_check)
        folder_row = QtWidgets.QHBoxLayout()
        folder_row.setContentsMargins(0, 0, 0, 0)
        self.asset_root_edit = QtWidgets.QLineEdit()
        self.asset_root_edit.setToolTip(
            "Folder for converted files: <folder>/<blend>/geo/<asset>/ and extracted packed textures in "
            "<folder>/<blend>/tex/. Other textures stay where the .blend points."
        )
        folder_row.addWidget(self.asset_root_edit, 1)
        self.asset_root_button = QtWidgets.QPushButton("...")
        self.asset_root_button.setToolTip("Choose the asset folder")
        self.asset_root_button.clicked.connect(self.browse_asset_root)
        folder_row.addWidget(self.asset_root_button)
        self.asset_root_widget = QtWidgets.QWidget()
        self.asset_root_widget.setLayout(folder_row)
        form.addRow("Asset folder", self.asset_root_widget)
        body.addWidget(options, 2)
        layout.addLayout(body, 1)

        status_row = QtWidgets.QHBoxLayout()
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setFixedWidth(90)
        self.progress.setVisible(False)
        status_row.addWidget(self.progress)
        self.status = QtWidgets.QLabel("Choose a .blend file to see its collections and objects.")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        status_row.addWidget(self.status, 1)
        self.cancel_button = QtWidgets.QPushButton("Cancel")
        self.cancel_button.setVisible(False)
        self.cancel_button.clicked.connect(self.cancel_work)
        status_row.addWidget(self.cancel_button)
        self.import_button = QtWidgets.QPushButton("Import")
        self.import_button.setProperty("cycles_primary", True)
        self.import_button.setDefault(True)
        self.import_button.clicked.connect(self.start_import)
        status_row.addWidget(self.import_button)
        self.close_button = QtWidgets.QPushButton("Close")
        self.close_button.clicked.connect(self.close)
        status_row.addWidget(self.close_button)
        layout.addLayout(status_row)

        for combo in (
            self.mode_combo,
            self.save_location_combo,
            self.structure_combo,
            self.materials_combo,
            self.frames_combo,
        ):
            combo.currentIndexChanged.connect(self._options_changed)
        self.include_disabled_check.toggled.connect(self._include_disabled_changed)
        self.apply_subdivision_check.toggled.connect(self._options_changed)
        self.nested_instances_check.toggled.connect(self._options_changed)
        for spin in (self.start_spin, self.end_spin, self.step_spin):
            spin.valueChanged.connect(self._options_changed)
        self.asset_root_edit.editingFinished.connect(self._options_changed)

    def _theme_applied(self, theme, scale):
        self._theme = theme
        self._refresh_icons()

    # ------------------------------------------------------------ options

    def _set_combo(self, combo, value):
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _apply_settings(self):
        self._updating = True
        try:
            values = self.settings
            self._set_combo(self.mode_combo, values["mode"])
            self._set_combo(self.save_location_combo, values["save_location"])
            self._set_combo(self.structure_combo, values["structure"])
            self._set_combo(self.materials_combo, values["materials"])
            self._set_combo(self.frames_combo, values["frames"])
            self.step_spin.setValue(values["frame_step"])
            self.include_disabled_check.setChecked(values["include_disabled"])
            self.apply_subdivision_check.setChecked(values["apply_subdivision"])
            self.nested_instances_check.setChecked(values["nested_instances"])
            self.asset_root_edit.setText(values["asset_root"])
        finally:
            self._updating = False

    def options(self):
        """Current option values (the converter mapping is converter_arguments)."""
        return {
            "mode": self.mode_combo.currentData(),
            "save_location": self.save_location_combo.currentData(),
            "structure": self.structure_combo.currentData(),
            "materials": self.materials_combo.currentData(),
            "frames": self.frames_combo.currentData(),
            "frame_start": self.start_spin.value(),
            "frame_end": self.end_spin.value(),
            "frame_step": self.step_spin.value(),
            "include_disabled": self.include_disabled_check.isChecked(),
            "apply_subdivision": self.apply_subdivision_check.isChecked(),
            "nested_instances": self.nested_instances_check.isChecked(),
            "asset_root": self.asset_root_edit.text().strip() or DEFAULT_ASSET_ROOT,
        }

    def _options_changed(self, *_):
        if self._updating:
            return
        self._update_enabled()

    def _include_disabled(self):
        return self.include_disabled_check.isChecked()

    def _include_disabled_changed(self, *_):
        if self._updating:
            return
        self._refresh_implied()
        self._update_enabled()

    def _show_row(self, field, visible):
        """Show or hide one form row, its label included (controls that do not apply are hidden)."""
        label = self._form.labelForField(field)
        if label is not None:
            label.setVisible(visible)
        field.setVisible(visible)

    def storage(self):
        """The converter storage of the current Mode and Save Location."""
        return storage_for(self.mode_combo.currentData(), self.save_location_combo.currentData())

    def _update_enabled(self):
        storage = self.storage()
        converted = storage != "reference"
        files = storage == "files"
        busy = self._importing or self._tasks > 0
        # Link: Blender File nodes in Link mode keep Blender's structure, modifiers and materials;
        # nothing is written, so only the selection and Include Disabled Collections apply.
        # Structure is hidden with it: Hierarchy implies Convert & Append.
        for field in (
            self.save_location_combo,
            self.structure_combo,
            self.materials_combo,
            self.apply_subdivision_check,
            self.nested_instances_check,
            self.asset_root_widget,
        ):
            self._show_row(field, converted)
        # Sequences exist only on disk; Stash in .hip imports the current frame
        # (converter_arguments), and the stored choice stays for the next Save to Disk import.
        self._show_row(self.frames_combo, files)
        ranged = files and self.frames_combo.currentData() == "range"
        self._show_row(self.range_widget, ranged)
        self._show_row(self.sequence_note, ranged)
        self.import_button.setEnabled(not busy and self._listing is not None and bool(self.selection()))
        self.browse_button.setEnabled(not self._importing)
        self.reload_button.setEnabled(not self._importing and bool(self.file_edit.text().strip()))

    def _prefill_frames(self, listing):
        start, end = listing.get("frame_range") or [1, 1]
        self._updating = True
        try:
            self.start_spin.setValue(int(start))
            self.end_spin.setValue(int(end))
        finally:
            self._updating = False

    # ------------------------------------------------------------ settings

    def save_settings(self):
        values = dict(self.options())
        values["last_directory"] = self.settings.get("last_directory", "")
        try:
            self.settings = save_settings(values, self._settings_path)
        except OSError as error:
            self.notify("Could not save the import settings: " + str(error), error=True)

    # ------------------------------------------------------------ status and tasks

    def notify(self, text, *, error=False):
        self.status.setText(text or "")
        self.status.setToolTip(text or "")
        if bool(self.status.property("cycles_warning")) != bool(error):
            self.status.setProperty("cycles_warning", bool(error))
            self.status.style().unpolish(self.status)
            self.status.style().polish(self.status)

    def _busy(self, active, cancellable=True):
        self.progress.setVisible(active)
        self.cancel_button.setVisible(active and cancellable)
        self._update_enabled()

    def run_task(self, work, callback):
        """Run work() on a thread (no HOM there); callback(value, error) runs on the GUI thread."""
        results = self._results

        def run():
            try:
                value, error = work(), None
            except Exception as exc:
                value, error = None, exc
            results.put(("done", callback, value, error))

        self._tasks += 1
        threading.Thread(target=run, name="cycles-blend-import-task", daemon=True).start()
        self.timer.start()

    def _report(self, generation):
        results = self._results

        def report(text, error=False):
            results.put(("report", generation, str(text), bool(error)))

        return report

    def poll(self):
        while True:
            try:
                message = self._results.get_nowait()
            except queue.Empty:
                break
            if message[0] == "report":
                _, generation, text, error = message
                if not self._disposed and generation == self._generation and self._importing:
                    self.notify(text, error=error)
                continue
            _, callback, value, error = message
            self._tasks -= 1
            if self._disposed:
                continue
            try:
                callback(value, error)
            except Exception as exc:
                self.notify(str(exc) or type(exc).__name__, error=True)
        if self._tasks <= 0:
            self._tasks = 0
            self.timer.stop()
            if not self._disposed:
                self._update_enabled()

    def cancel_work(self):
        for event in (self._list_cancel, self._import_cancel):
            if event is not None:
                event.set()
        self.notify("Cancelling...")

    # ------------------------------------------------------------ file and listing

    def browse(self):
        start = self.settings.get("last_directory") or ""
        current = self.file_edit.text().strip()
        if current:
            start = os.path.dirname(current) or start
        try:
            chosen = self.backend.select_file(start, "Import Blender (.blend)")
        except Exception as exc:
            self.notify(str(exc), error=True)
            return
        if chosen:
            self.load_file(chosen)

    def browse_asset_root(self):
        try:
            chosen = self.backend.select_directory(self.asset_root_edit.text().strip(), "Asset folder")
        except Exception as exc:
            self.notify(str(exc), error=True)
            return
        if chosen:
            self.asset_root_edit.setText(_host_path(chosen).rstrip("/"))
            self._options_changed()

    def load_file(self, path):
        """Start a background listing of path (GUI thread)."""
        path = _host_path((path or "").strip())
        if not path:
            return
        if self._importing:
            self.notify("Wait for the import to finish before choosing another file.", error=True)
            return
        self.file_edit.setText(path)
        if self._list_cancel is not None:
            self._list_cancel.set()
        self._generation += 1
        generation = self._generation
        self._listing = None
        self._source = path
        self.tree.clear()
        if not path.lower().endswith(".blend"):
            self.notify("Choose a Blender .blend file.", error=True)
            self._update_enabled()
            return
        if not os.path.isfile(path):
            self.notify("File not found: " + path, error=True)
            self._update_enabled()
            return
        self.settings["last_directory"] = _host_path(os.path.dirname(path))
        try:
            snapshot = self.backend.blender_snapshot()
        except Exception as exc:
            self.notify(str(exc), error=True)
            self._update_enabled()
            return
        backend = self.backend
        cancel = threading.Event()
        self._list_cancel = cancel

        def work():
            executable = backend.resolve_blender(snapshot)
            return executable, backend.list_contents(path, executable, cancel)

        def done(value, error):
            if generation != self._generation:
                return
            self._list_cancel = None
            self._busy(False)
            if cancel.is_set():
                self.notify("Reading the file was cancelled.")
                return
            if error is not None:
                self.notify(self._error_text(error), error=True)
                return
            self._executable, listing = value
            self.set_listing(listing)

        self.notify("Reading " + os.path.basename(path) + " with Blender...")
        self.run_task(work, done)
        self._busy(True)

    @staticmethod
    def _error_text(error):
        if isinstance(error, FileNotFoundError) and str(error) == BLENDER_MISSING:
            return BLENDER_MISSING
        text = str(error) or type(error).__name__
        # Blender diagnostics follow the message; keep the status line to the message.
        return text.split("\nBlender diagnostics:", 1)[0]

    def set_listing(self, listing):
        """Fill the tree from a checked listing (GUI thread)."""
        self._listing = listing
        self._prefill_frames(listing)
        self._fill_tree(listing)
        collections, objects = len(listing.get("collections", ())), len(listing.get("objects", ()))
        excluded = sum(1 for entry in listing.get("collections", ()) if entry.get("excluded"))
        text = f"{collections} collections, {objects} objects"
        if excluded:
            text += f"; {excluded} disabled in the view layer"
        start, end = listing.get("frame_range") or [1, 1]
        text += f"; frames {start}-{end} at {listing.get('fps', 24.0):g} fps"
        self.notify(text)
        self._update_enabled()

    # ------------------------------------------------------------ import

    def import_arguments(self):
        return converter_arguments(self._source, self.selection(), self.options(), self._executable)

    def _confirm_shared(self, source, selection, kwargs):
        """Shared scenes, on the GUI thread before the background prepare: when Save to Disk would
        replace files another scene uses, ask Replace / Cancel. Updates kwargs["on_shared"];
        returns False when the import must not start."""
        check = getattr(self.backend, "shared_scenes", None)
        if kwargs.get("storage") != "files" or not callable(check):
            return True
        try:
            shared = check(source, selection, kwargs)
        except Exception as exc:
            self.notify("Import failed: " + self._error_text(exc), error=True)
            return False
        if not shared:
            return True
        from ..assets.importers import shared_question

        if not self.backend.ask_replace(shared_question(shared)):
            self.notify("Import cancelled; nothing was changed.")
            return False
        kwargs["on_shared"] = "replace"
        return True

    def start_import(self):
        if self._importing or self._listing is None:
            return
        try:
            source, selection, kwargs = self.import_arguments()
        except ValueError as exc:
            self.notify(str(exc), error=True)
            return
        if not self._confirm_shared(source, selection, kwargs):
            return
        self.save_settings()
        self._generation += 1
        generation = self._generation
        cancel = threading.Event()
        self._import_cancel = cancel
        self._importing = True
        backend = self.backend
        report = self._report(generation)

        def work():
            return backend.prepare(source, selection, kwargs, cancel, report)

        def done(payload, error):
            self._import_cancel = None
            if cancel.is_set():
                self._importing = False
                self._busy(False)
                self.notify("Import cancelled.")
                return
            if error is not None:
                self._importing = False
                self._busy(False)
                self.notify("Import failed: " + self._error_text(error), error=True)
                return
            self.notify("Building Houdini nodes...")
            self.cancel_button.setVisible(False)
            try:
                nodes = list(backend.build(payload) or [])
            except Exception as exc:
                self.notify("Import failed: " + (str(exc) or type(exc).__name__), error=True)
                return
            finally:
                self._importing = False
                self._busy(False)
            self.created = nodes
            backend.select_nodes(nodes)
            paths = [node.path() if hasattr(node, "path") else str(node) for node in nodes]
            # The converter's build report (missing textures, textures in place) stays visible.
            built = payload.get("build_messages", ()) if isinstance(payload, dict) else ()
            notes = [str(text) for text, _ in built]
            if paths:
                shown = ", ".join(paths[:4]) + (f" and {len(paths) - 4} more" if len(paths) > 4 else "")
                text = f"Imported {len(paths)} node{'s' if len(paths) != 1 else ''}: {shown}"
                self.notify(" ".join([text + "."] + notes) if notes else text, error=any(e for _, e in built))
                self.status.setToolTip("\n".join(paths + notes))
            else:
                self.notify("The import created no nodes.", error=True)

        count = len(selection)
        self.notify(f"Importing {count} item{'s' if count != 1 else ''} with Blender...")
        self.run_task(work, done)
        self._busy(True)

    # ------------------------------------------------------------ lifetime

    def dispose(self):
        if self._disposed:
            return
        for event in (self._list_cancel, self._import_cancel):
            if event is not None:
                event.set()
        if not self._importing:
            self.save_settings()
        self._disposed = True
        self.timer.stop()
        chrome_timer = getattr(self._chrome, "timer", None)
        if chrome_timer is not None:
            chrome_timer.stop()

    def closeEvent(self, event):
        self.dispose()
        global _dialog
        if _dialog is self:
            _dialog = None
        super().closeEvent(event)

    def reject(self):
        self.close()


SELECTION_TITLE = "Blender Hierarchy Selection"


class SelectionDialog(_SelectionTree, QtWidgets.QDialog):
    """Edit... next to the Blender Hierarchy object's Selection: the importer's tree for one .blend.

    Modal. The listing is read with Blender on a background thread (busy state, Cancel works
    meanwhile); OK returns the checked items without the ones a checked collection implies."""

    def __init__(self, source, selection, parent=None, *, backend=None, include_disabled=False):
        super().__init__(parent)
        self.backend = backend or HoudiniBackend()
        self.setWindowTitle(SELECTION_TITLE)
        self.setObjectName("cycles_blend_selection_dialog")
        self.setModal(True)
        self._source = _host_path(source)
        self._initial = [tuple(item) for item in selection or ()]
        self._listing = None
        self._updating = False
        self._theme = None
        self._chrome = None
        self._include = bool(include_disabled)
        self._results = queue.SimpleQueue()
        self._cancel = threading.Event()
        self._reading = False
        self._disposed = False
        self.missing = []

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        file_label = QtWidgets.QLabel(self._source)
        file_label.setProperty("cycles_note", True)
        file_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        layout.addWidget(file_label)
        layout.addWidget(self._build_tree(), 1)
        status_row = QtWidgets.QHBoxLayout()
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setFixedWidth(90)
        status_row.addWidget(self.progress)
        self.status = QtWidgets.QLabel("")
        self.status.setWordWrap(True)
        status_row.addWidget(self.status, 1)
        self.ok_button = QtWidgets.QPushButton("OK")
        self.ok_button.setProperty("cycles_primary", True)
        self.ok_button.setDefault(True)
        self.ok_button.clicked.connect(self.accept)
        status_row.addWidget(self.ok_button)
        self.cancel_button = QtWidgets.QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        status_row.addWidget(self.cancel_button)
        layout.addLayout(status_row)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.poll)
        try:
            from ..viewer import panel_theme

            self._chrome = panel_theme.install(
                self, DIALOG_SCOPE, extra_style=dialog_style, on_theme=lambda theme, scale: self._themed(theme)
            )
            self._chrome.capture_layouts()
            self._chrome.update(force=True)
        except Exception:
            self._chrome = None
        em = self.fontMetrics().averageCharWidth()
        self.resize(em * 80, self.fontMetrics().lineSpacing() * 30)
        self._update_enabled()

    def _themed(self, theme):
        self._theme = theme
        self._refresh_icons()

    def _include_disabled(self):
        return self._include

    def _update_enabled(self):
        self.progress.setVisible(self._reading)
        self.ok_button.setEnabled(not self._reading and self._listing is not None and bool(self.selection()))

    def notify(self, text, *, error=False):
        self.status.setText(text or "")
        self.status.setToolTip(text or "")
        self.status.setProperty("cycles_warning", bool(error))

    def load(self):
        """Read the .blend's collections and objects with Blender in the background."""
        if not self._source or not os.path.isfile(self._source):
            self.notify("File not found: " + (self._source or "(no Blender File)"), error=True)
            return
        try:
            snapshot = self.backend.blender_snapshot()
        except Exception as exc:
            self.notify(str(exc) or type(exc).__name__, error=True)
            return
        backend, source, cancel, results = self.backend, self._source, self._cancel, self._results

        def run():
            try:
                executable = backend.resolve_blender(snapshot)
                value, error = backend.list_contents(source, executable, cancel), None
            except Exception as exc:
                value, error = None, exc
            results.put((value, error))

        self._reading = True
        self.notify("Reading " + os.path.basename(source) + " with Blender...")
        threading.Thread(target=run, name="cycles-blend-selection", daemon=True).start()
        self.timer.start()
        self._update_enabled()

    def poll(self):
        try:
            value, error = self._results.get_nowait()
        except queue.Empty:
            return
        self.timer.stop()
        self._reading = False
        if self._disposed or self._cancel.is_set():
            return
        if error is not None:
            self.notify(BlendImportDialog._error_text(error), error=True)
            self._update_enabled()
            return
        self.set_listing(value)

    def set_listing(self, listing):
        self._listing = listing
        self._fill_tree(listing)
        self.missing = self.apply_selection(self._initial)
        if self.missing:
            names = ", ".join(f"{kind} {name}" for kind, name in self.missing)
            self.notify("Not in the file any more (dropped on OK): " + names, error=True)
        else:
            self.notify("Check the collections and objects the Blender Hierarchy object imports.")
        self._update_enabled()

    def dispose(self):
        self._disposed = True
        self._cancel.set()
        self.timer.stop()
        chrome_timer = getattr(self._chrome, "timer", None)
        if chrome_timer is not None:
            chrome_timer.stop()

    def done(self, result):
        self._cancel.set()
        super().done(result)


def edit_selection(source, selection, *, include_disabled=False, parent=None, backend=None, run=None):
    """Modal selection tree for ``source`` (the Blender Hierarchy object's Edit...).

    Returns the chosen [(kind, name)], or None when cancelled or nothing could be read.
    ``run(dialog)`` replaces ``dialog.exec()`` (tests). Raises ValueError when the Blender
    bridge is disabled.
    """
    backend = backend or HoudiniBackend()
    try:
        enabled = backend.enabled()
    except Exception:
        enabled = True
    if not enabled:
        raise ValueError(BRIDGE_DISABLED)
    if parent is None:
        try:
            parent = backend.main_window()
        except Exception:
            parent = None
    dialog = SelectionDialog(source, selection, parent, backend=backend, include_disabled=include_disabled)
    try:
        dialog.load()
        result = run(dialog) if run is not None else dialog.exec()
        accepted = int(getattr(result, "value", result)) == int(getattr(QtWidgets.QDialog.Accepted, "value", 1))
        chosen = dialog.selection() if accepted else None
    finally:
        dialog.dispose()
        dialog.deleteLater()
    return chosen or None


_dialog = None


def open_dialog(path=None, *, backend=None, parent=None):
    """Show the importer (one instance; focus it when already open). Returns the dialog or None."""
    global _dialog
    backend = backend or HoudiniBackend()
    try:
        enabled = backend.enabled()
    except Exception:
        enabled = True
    if not enabled:
        backend.message(BRIDGE_DISABLED)
        return None
    dialog = _dialog
    if dialog is None or dialog._disposed:
        if parent is None:
            try:
                parent = backend.main_window()
            except Exception:
                parent = None
        dialog = BlendImportDialog(parent, backend=backend)
        dialog.setAttribute(QtCore.Qt.WA_DeleteOnClose, True)
        _dialog = dialog
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    if path:
        dialog.load_file(path)
    return dialog


def import_blend_menu(kwargs=None):
    """Main menu and shelf entry point."""
    return open_dialog()


__all__ = [
    "FILE_IMPORT_INSERT_AFTER",
    "FILE_IMPORT_MENU_ID",
    "BlendImportDialog",
    "HoudiniBackend",
    "SelectionDialog",
    "converter_arguments",
    "edit_selection",
    "load_settings",
    "normalize_settings",
    "open_dialog",
    "save_settings",
    "scene_tree",
    "selection_without_implied",
]
