# SPDX-License-Identifier: Apache-2.0
"""File > Import > Blender (.blend) dialog with a fake backend and converter (offscreen Qt, no hou)."""

import json
import os
import sys
import threading
import time
import types

import pytest

pytestmark = [pytest.mark.pure, pytest.mark.cpu]

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6 import QtCore

from cycles_houdini.blender import blend_import_dialog as dialog_module
from cycles_houdini.blender import blend_worker


@pytest.fixture(scope="module")
def app():
    yield QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(autouse=True)
def no_hou(monkeypatch):
    # The dialog reaches HOM only through its backend; an accidental import fails loudly.
    monkeypatch.setitem(sys.modules, "hou", None)
    monkeypatch.setattr(dialog_module, "_dialog", None)


def listing():
    return blend_worker.checked_listing(
        {
            "active_scene": "Scene",
            "scenes": [
                {
                    "name": "Scene",
                    "view_layers": ["ViewLayer"],
                    "active_view_layer": "ViewLayer",
                    "collections": ["Props", "Shared"],
                    "objects": ["Root Empty"],
                }
            ],
            "collections": [
                {"name": "Props", "parent": None, "children": ["Shared", "Disabled", "Hidden"], "objects": ["Rock"]},
                {"name": "Shared", "parent": "Props", "children": [], "objects": ["Pebble"]},
                {"name": "Disabled", "parent": "Props", "children": [], "objects": ["Scatter"], "excluded": True},
                {"name": "Hidden", "parent": "Props", "children": [], "objects": [], "hide_render": True},
                {"name": "Unlinked", "parent": None, "children": [], "objects": [], "in_scene": False},
            ],
            "objects": [
                {"name": "Rock", "type": "MESH", "collections": ["Props"], "material_slots": ["Red"]},
                {"name": "Pebble", "type": "MESH", "parent": "Rock", "collections": ["Shared"]},
                {"name": "Scatter", "type": "MESH", "collections": ["Disabled"], "has_geometry_nodes": True},
                {"name": "Root Empty", "type": "EMPTY", "collections": []},
            ],
            "frame_range": [5, 48],
            "fps": 30,
        }
    )


class Node:
    def __init__(self, path):
        self._path = path

    def path(self):
        return self._path


class FakeBackend:
    def __init__(self, tmp_path, *, listing_value=None, blender=True, enabled=True):
        self.tmp_path = tmp_path
        self.listing_value = listing_value or listing()
        self.blender = blender
        self._enabled = enabled
        self.prepared = []
        self.built = []
        self.selected = []
        self.messages = []
        self.prepare_error = None
        self.build_error = None
        self.prepare_gate = None
        self.threads = {}

    def enabled(self):
        return self._enabled

    def settings_path(self):
        return self.tmp_path / "blend_import_settings.json"

    def blender_snapshot(self):
        self.threads["snapshot"] = threading.current_thread()
        return ("", "", "")

    def resolve_blender(self, snapshot):
        if not self.blender:
            raise FileNotFoundError(dialog_module.BLENDER_MISSING)
        return "/opt/blender/blender"

    def list_contents(self, source, executable, cancelled):
        self.threads["list"] = threading.current_thread()
        return dict(self.listing_value, source=source)

    def select_file(self, start, title):
        return str(self.tmp_path / "scene.blend")

    def select_directory(self, start, title):
        return "C:\\assets\\blender"

    def prepare(self, source, selection, kwargs, cancelled, report):
        self.threads["prepare"] = threading.current_thread()
        self.prepared.append((source, selection, kwargs))
        report("Exporting Rock")
        if self.prepare_gate is not None:
            self.prepare_gate.wait(5)
        if self.prepare_error is not None:
            raise self.prepare_error
        return {"payload": len(self.prepared)}

    def build(self, payload):
        self.threads["build"] = threading.current_thread()
        if self.build_error is not None:
            raise self.build_error
        self.built.append(payload)
        return [Node("/obj/Props"), Node("/obj/Root_Empty")]

    def select_nodes(self, nodes):
        self.selected = list(nodes)

    def main_window(self):
        return None

    def message(self, text):
        self.messages.append(text)


def drain(app, dialog, predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        dialog.poll()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def opened(app, tmp_path, monkeypatch=None, **kwargs):
    if monkeypatch is not None:
        monkeypatch.delenv("CYCLES_HOUDINI_BLEND_IMPORT_SETTINGS_FILE", raising=False)
    source = tmp_path / "scene.blend"
    source.write_bytes(b"BLENDER")
    backend = FakeBackend(tmp_path, **kwargs)
    dialog = dialog_module.BlendImportDialog(backend=backend)
    dialog.load_file(str(source))
    assert drain(app, dialog, lambda: dialog._listing is not None or "not found" in dialog.status.text().lower())
    return dialog, backend, source


def item(dialog, kind, name):
    return [
        i
        for i in dialog._items()
        if (i.data(0, dialog_module.KIND_ROLE), i.data(0, dialog_module.NAME_ROLE)) == (kind, name)
    ]


# ---------------------------------------------------------------- pure mapping


def test_converter_arguments_maps_every_option():
    options = {
        "mode": "convert",
        "save_location": "files",
        "structure": "hierarchy",
        "materials": "append",
        "frames": "range",
        "frame_start": 5,
        "frame_end": 48,
        "frame_step": 2,
        "include_disabled": True,
        "apply_subdivision": True,
        "asset_root": "C:\\work\\geo\\blender",
    }
    source, selection, kwargs = dialog_module.converter_arguments(
        "C:\\scenes\\tree.blend", [("collection", "Props"), ("object", "Rock")], options, "/opt/blender"
    )
    assert source == "C:/scenes/tree.blend" and selection == [("collection", "Props"), ("object", "Rock")]
    assert kwargs == {
        "storage": "files",
        "structure": "hierarchy",
        "materials": "append",
        "frames": (5, 48, 2),
        "include_disabled": True,
        "apply_subdivision": True,
        "nested_instances": True,
        "asset_root": "C:/work/geo/blender",
        "executable": "/opt/blender",
    }
    # Every Mode / Save Location combination; frame ranges only with Save to Disk.
    for mode, location, storage in (
        ("link", "files", "reference"),
        ("link", "embedded", "reference"),
        ("convert", "embedded", "embedded"),
    ):
        _, _, other = dialog_module.converter_arguments(
            "a.blend", [("object", "Rock")], dict(options, mode=mode, save_location=location)
        )
        assert other["frames"] is None and other["storage"] == storage
        # Link + Hierarchy is not offered: Hierarchy implies Convert & Append.
        assert other["structure"] == ("packed" if storage == "reference" else "hierarchy")
    # The removed Merged structure is refused with the converter's explanation.
    from cycles_houdini.blender.blend_convert import MERGED_REMOVED

    with pytest.raises(ValueError, match="Merged structure was removed"):
        dialog_module.converter_arguments("a.blend", [("object", "R")], dict(options, structure="merged"))
    assert "Load As" in MERGED_REMOVED
    # Scripts of earlier builds may still pass the converter storage directly.
    legacy = {k: v for k, v in options.items() if k not in ("mode", "save_location")}
    for storage in ("reference", "embedded", "files"):
        _, _, other = dialog_module.converter_arguments("a.blend", [("object", "R")], dict(legacy, storage=storage))
        assert other["storage"] == storage
    _, _, current = dialog_module.converter_arguments("a.blend", [("object", "R")], dict(options, frames="current"))
    assert current["frames"] is None
    _, _, defaults = dialog_module.converter_arguments("a.blend", [("object", "R")], {})
    assert (defaults["storage"], defaults["structure"], defaults["materials"]) == ("files", "packed", "link")
    assert defaults["asset_root"] == "$HIP/assets_blender" and "texture_location" not in defaults
    assert defaults["apply_subdivision"] is False  # a final Subsurf stays render-time by default
    assert defaults["nested_instances"] is True
    for bad in (
        dict(options, mode="usd"),
        dict(options, save_location="cloud"),
        dict(legacy, storage="usd"),
        dict(options, structure="flat"),
        dict(options, materials="copy"),
        dict(options, frame_end=1),
        dict(options, frame_step=0),
    ):
        with pytest.raises(ValueError):
            dialog_module.converter_arguments("a.blend", [("object", "R")], bad)
    with pytest.raises(ValueError, match="Select at least one"):
        dialog_module.converter_arguments("a.blend", [], options)
    with pytest.raises(ValueError):
        dialog_module.converter_arguments("a.blend", [("material", "Red")], options)


def test_labels_match_the_contract_values():
    assert [(v, label) for v, label, _ in dialog_module.MODE_CHOICES] == [
        ("link", "Link"),
        ("convert", "Convert & Append"),
    ]
    assert [(v, label) for v, label, _ in dialog_module.SAVE_LOCATION_CHOICES] == [
        ("files", "Save to Disk as .bgeo.sc"),
        ("embedded", "Stash in .hip"),
    ]
    assert [(v, label) for v, label, _ in dialog_module.STRUCTURE_CHOICES] == [
        ("packed", "Without hierarchy"),
        ("hierarchy", "Hierarchy"),
    ]
    assert [(v, label) for v, label, _ in dialog_module.MATERIAL_CHOICES] == [
        ("link", "Link (shared, read-only)"),
        ("append", "Append (editable copy)"),
        ("none", "None"),
    ]
    assert dialog_module.DEFAULTS["mode"] == "convert" and dialog_module.DEFAULTS["save_location"] == "files"
    assert dialog_module.DEFAULTS["structure"] == "packed"
    assert dialog_module.DEFAULT_ASSET_ROOT == "$HIP/assets_blender"
    assert not hasattr(dialog_module, "TEXTURE_CHOICES") and "texture_location" not in dialog_module.DEFAULTS


def test_accepted_kwargs_and_backend_prepare_use_the_converter_lazily(monkeypatch, tmp_path):
    calls = []
    fake = types.ModuleType("cycles_houdini.blender.blend_convert")

    def prepare_import(source, selection, *, storage="files", frames=None, executable=None, report=None):
        calls.append((source, selection, storage, frames, executable, report))
        report("working")
        return "payload"

    fake.prepare_import = prepare_import
    fake.build_import = lambda payload: [payload]
    monkeypatch.setitem(sys.modules, "cycles_houdini.blender.blend_convert", fake)
    backend = dialog_module.HoudiniBackend()
    messages = []
    kwargs = {"storage": "embedded", "frames": None, "executable": "b", "structure": "packed"}
    event = threading.Event()
    assert (
        backend.prepare("a.blend", [("object", "R")], kwargs, event, lambda t, error=False: messages.append(t))
        == "payload"
    )
    # Keywords the converter does not accept (structure, cancelled) are not passed.
    assert calls[0][:5] == ("a.blend", [("object", "R")], "embedded", None, "b") and messages == ["working"]
    assert backend.build("payload") == ["payload"]
    assert dialog_module.accepted_kwargs(lambda **kw: kw, {"x": 1}) == {"x": 1}


def test_scene_tree_and_implied_selection_pure():
    data = listing()
    nodes = dialog_module.scene_tree(data)
    assert [(n["kind"], n["name"]) for n in nodes] == [
        ("collection", "Props"),
        ("collection", "Shared"),
        ("object", "Root Empty"),
        ("group", "Not in the scene"),
    ]
    props = nodes[0]
    assert [(n["kind"], n["name"]) for n in props["children"]] == [
        ("collection", "Shared"),
        ("collection", "Disabled"),
        ("collection", "Hidden"),
        ("object", "Rock"),
    ]
    assert [n["name"] for n in nodes[3]["children"]] == ["Unlinked"]
    selection = [("collection", "Props"), ("collection", "Shared"), ("object", "Scatter"), ("object", "Root Empty")]
    assert dialog_module.selection_without_implied(data, selection, False) == [
        ("collection", "Props"),
        ("object", "Scatter"),
        ("object", "Root Empty"),
    ]
    assert dialog_module.selection_without_implied(data, selection, True) == [
        ("collection", "Props"),
        ("object", "Root Empty"),
    ]


# ---------------------------------------------------------------- dialog


def test_listing_runs_in_background_and_fills_tree(app, tmp_path, monkeypatch):
    dialog, backend, source = opened(app, tmp_path, monkeypatch)
    try:
        assert backend.threads["list"] is not threading.main_thread()
        assert backend.threads["snapshot"] is threading.main_thread()
        assert dialog._executable == "/opt/blender/blender"
        assert "5 collections, 4 objects" in dialog.status.text() and "1 disabled" in dialog.status.text()
        assert (dialog.start_spin.value(), dialog.end_spin.value()) == (5, 48)
        [scatter] = item(dialog, "object", "Scatter")
        assert scatter.text(1) == "Mesh" and "Geometry Nodes" in scatter.text(2)
        [rock] = item(dialog, "object", "Rock")
        assert "Red" in rock.toolTip(0)
        assert "Child of Rock" in item(dialog, "object", "Pebble")[0].text(2)
        assert item(dialog, "object", "Root Empty")[0].text(1) == "Empty"
        assert "Hidden in renders" in item(dialog, "collection", "Hidden")[0].text(2)
        # Default: the scene's top-level collections and its loose objects, without implied duplicates.
        assert dialog.selection() == [("collection", "Props"), ("object", "Root Empty")]
        assert dialog.import_button.isEnabled()
    finally:
        dialog.dispose()


def test_disabled_collections_are_greyed_and_follow_include_disabled(app, tmp_path, monkeypatch):
    dialog, backend, _ = opened(app, tmp_path, monkeypatch)
    try:
        [disabled] = item(dialog, "collection", "Disabled")
        assert disabled.data(0, dialog_module.EXCLUDED_ROLE) is True
        assert dialog_module.DISABLED_NOTE in disabled.text(2)
        assert disabled.toolTip(0) == dialog_module.DISABLED_TOOLTIP
        assert disabled.foreground(0).color() == dialog.palette().color(
            dialog.palette().ColorGroup.Disabled, dialog.palette().ColorRole.Text
        )
        # Props is checked: its included children are implied (checked, disabled); Disabled is not.
        [shared_under_props] = [i for i in item(dialog, "collection", "Shared") if i.parent() is not None]
        assert not (shared_under_props.flags() & QtCore.Qt.ItemIsEnabled)
        assert shared_under_props.checkState(0) == QtCore.Qt.Checked
        assert disabled.flags() & QtCore.Qt.ItemIsEnabled and disabled.checkState(0) == QtCore.Qt.Unchecked
        dialog.include_disabled_check.setChecked(True)
        assert not (disabled.flags() & QtCore.Qt.ItemIsEnabled) and disabled.checkState(0) == QtCore.Qt.Checked
        dialog.include_disabled_check.setChecked(False)
        assert disabled.flags() & QtCore.Qt.ItemIsEnabled and disabled.checkState(0) == QtCore.Qt.Unchecked
    finally:
        dialog.dispose()


def test_tree_checks_select_collections_as_units_and_objects_individually(app, tmp_path, monkeypatch):
    dialog, backend, _ = opened(app, tmp_path, monkeypatch)
    try:
        dialog.clear_checks()
        assert dialog.selection() == [] and not dialog.import_button.isEnabled()
        dialog.set_checked("object", "Rock")
        dialog.set_checked("object", "Scatter")
        assert dialog.selection() == [("object", "Scatter"), ("object", "Rock")]
        # A collection with two parents is one unit: checking one occurrence checks both.
        dialog.set_checked("collection", "Shared")
        assert all(i.checkState(0) == QtCore.Qt.Checked for i in item(dialog, "collection", "Shared"))
        assert ("collection", "Shared") in dialog.selection()
        # Checking Props makes Shared and Rock implied: the selection keeps only Props and the excluded child's object.
        dialog.set_checked("collection", "Props")
        assert dialog.selection() == [("collection", "Props"), ("object", "Scatter")]
        [rock] = item(dialog, "object", "Rock")
        assert not (rock.flags() & QtCore.Qt.ItemIsEnabled)
        dialog.set_checked("collection", "Props", False)
        assert rock.flags() & QtCore.Qt.ItemIsEnabled and rock.checkState(0) == QtCore.Qt.Checked
        # Tree order; Pebble is implied by Shared.
        assert dialog.selection() == [("collection", "Shared"), ("object", "Scatter"), ("object", "Rock")]
        # Group rows are not selectable items.
        group = [i for i in dialog._items() if i.data(0, dialog_module.KIND_ROLE) == "group"][0]
        assert not (group.flags() & QtCore.Qt.ItemIsUserCheckable)
    finally:
        dialog.dispose()


def _form_label(dialog, field):
    label = dialog._form.labelForField(field)
    return label.text() if label is not None else None


def test_controls_follow_mode_and_save_location(app, tmp_path, monkeypatch):
    dialog, backend, _ = opened(app, tmp_path, monkeypatch)
    try:
        assert not hasattr(dialog, "texture_combo") and not hasattr(dialog, "storage_combo")
        assert _form_label(dialog, dialog.mode_combo) == "Mode"
        assert _form_label(dialog, dialog.save_location_combo) == "Save Location"
        assert _form_label(dialog, dialog.asset_root_widget) == "Asset folder"
        assert dialog.asset_root_edit.text() == "$HIP/assets_blender"

        def choose(mode, location=None):
            dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData(mode))
            if location:
                dialog.save_location_combo.setCurrentIndex(dialog.save_location_combo.findData(location))

        def shown(widget):
            label = dialog._form.labelForField(widget)
            visible = widget.isVisibleTo(dialog)
            assert label is None or label.isVisibleTo(dialog) == visible  # rows hide with their label
            return visible

        converted = (
            dialog.save_location_combo,
            dialog.structure_combo,
            dialog.materials_combo,
            dialog.apply_subdivision_check,
            dialog.nested_instances_check,
            dialog.asset_root_widget,
        )
        choose("convert", "files")
        dialog.frames_combo.setCurrentIndex(dialog.frames_combo.findData("range"))
        assert all(shown(w) for w in converted) and shown(dialog.frames_combo)
        assert shown(dialog.range_widget) and shown(dialog.sequence_note)
        assert dialog.import_arguments()[2]["frames"] == (5, 48, 1)
        assert dialog.import_arguments()[2]["storage"] == "files"
        # Stash in .hip: one frame, so the Frames row is hidden (not greyed).
        choose("convert", "embedded")
        assert all(shown(w) for w in converted)
        assert not shown(dialog.frames_combo) and not shown(dialog.range_widget) and not shown(dialog.sequence_note)
        assert dialog.import_arguments()[2]["frames"] is None
        assert dialog.import_arguments()[2]["storage"] == "embedded"
        # Link: only the selection and Include Disabled Collections apply.
        choose("link")
        assert not any(shown(w) for w in converted) and not shown(dialog.frames_combo)
        assert shown(dialog.include_disabled_check)
        assert dialog.import_arguments()[2]["storage"] == "reference"
        assert dialog.import_arguments()[2]["frames"] is None
        # Nothing is greyed out: hidden controls stay enabled for the next mode.
        assert all(w.isEnabled() for w in (*converted, dialog.frames_combo))
        choose("convert", "files")
        assert dialog.frames_combo.currentData() == "range"  # the sequence choice survives
        assert dialog.save_location_combo.currentData() == "files"
    finally:
        dialog.dispose()


def test_import_two_phase_reports_nodes_and_persists_settings(app, tmp_path, monkeypatch):
    dialog, backend, source = opened(app, tmp_path, monkeypatch)
    try:
        dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData("convert"))
        dialog.save_location_combo.setCurrentIndex(dialog.save_location_combo.findData("embedded"))
        dialog.structure_combo.setCurrentIndex(dialog.structure_combo.findData("hierarchy"))
        dialog.materials_combo.setCurrentIndex(dialog.materials_combo.findData("none"))
        dialog.include_disabled_check.setChecked(True)
        dialog.browse_asset_root()
        assert dialog.asset_root_edit.text() == "C:/assets/blender"
        backend.prepare_gate = threading.Event()
        dialog.start_import()
        assert dialog._importing and dialog.progress.isVisibleTo(dialog) and dialog.cancel_button.isVisibleTo(dialog)
        assert not dialog.import_button.isEnabled()
        assert drain(app, dialog, lambda: dialog.status.text() == "Exporting Rock")
        backend.prepare_gate.set()
        assert drain(app, dialog, lambda: not dialog._importing)
        assert backend.threads["prepare"] is not threading.main_thread()
        assert backend.threads["build"] is threading.main_thread()
        [(src, selection, kwargs)] = backend.prepared
        assert src == str(source).replace("\\", "/")
        assert selection == [("collection", "Props"), ("object", "Root Empty")]
        assert kwargs == {
            "storage": "embedded",
            "structure": "hierarchy",
            "materials": "none",
            "frames": None,
            "include_disabled": True,
            "apply_subdivision": False,
            "nested_instances": True,
            "asset_root": "C:/assets/blender",
            "executable": "/opt/blender/blender",
        }
        assert backend.built == [{"payload": 1}]
        assert [n.path() for n in backend.selected] == ["/obj/Props", "/obj/Root_Empty"]
        assert "Imported 2 nodes: /obj/Props, /obj/Root_Empty" in dialog.status.text()
        assert dialog.import_button.isEnabled() and not dialog.progress.isVisibleTo(dialog)
        saved = json.loads((tmp_path / "blend_import_settings.json").read_text(encoding="utf-8"))["settings"]
        assert saved["mode"] == "convert" and saved["save_location"] == "embedded"
        assert saved["structure"] == "hierarchy" and saved["materials"] == "none"
        assert saved["include_disabled"] is True and saved["asset_root"] == "C:/assets/blender"
        assert saved["last_directory"] == str(tmp_path).replace("\\", "/")
        assert "storage" not in saved and "texture_location" not in saved
    finally:
        dialog.dispose()
    again = dialog_module.BlendImportDialog(backend=FakeBackend(tmp_path))
    try:
        assert again.mode_combo.currentData() == "convert"
        assert again.save_location_combo.currentData() == "embedded"
        assert again.structure_combo.currentData() == "hierarchy"
        assert again.materials_combo.currentData() == "none"
        assert again.include_disabled_check.isChecked()
        assert again.asset_root_edit.text() == "C:/assets/blender"
    finally:
        again.dispose()


def test_import_errors_and_cancel(app, tmp_path, monkeypatch):
    dialog, backend, _ = opened(app, tmp_path, monkeypatch)
    try:
        backend.prepare_error = RuntimeError("Blender crashed\nBlender diagnostics:\nlong log")
        dialog.start_import()
        assert drain(app, dialog, lambda: not dialog._importing)
        assert dialog.status.text() == "Import failed: Blender crashed" and dialog.status.property("cycles_warning")
        backend.prepare_error = None
        backend.build_error = RuntimeError("cannot create node")
        dialog.start_import()
        assert drain(app, dialog, lambda: not dialog._importing)
        assert dialog.status.text() == "Import failed: cannot create node"
        backend.build_error = None
        backend.prepare_gate = threading.Event()
        built = len(backend.built)
        dialog.start_import()
        dialog.cancel_work()
        backend.prepare_gate.set()
        assert drain(app, dialog, lambda: not dialog._importing)
        assert dialog.status.text() == "Import cancelled." and len(backend.built) == built
    finally:
        dialog.dispose()


def test_blender_missing_and_bad_files(app, tmp_path, monkeypatch):
    dialog, backend, source = opened(app, tmp_path, monkeypatch, blender=False)
    try:
        assert dialog._listing is None and dialog.status.text() == dialog_module.BLENDER_MISSING
        assert not dialog.import_button.isEnabled()
        dialog.load_file(str(tmp_path / "missing.blend"))
        assert dialog.status.text().startswith("File not found")
        text = tmp_path / "notes.txt"
        text.write_text("x")
        dialog.load_file(str(text))
        assert dialog.status.text() == "Choose a Blender .blend file."
        backend.blender = True
        dialog.browse()
        assert drain(app, dialog, lambda: dialog._listing is not None)
        assert dialog.file_edit.text() == str(source).replace("\\", "/")
    finally:
        dialog.dispose()


def test_open_dialog_single_instance_and_bridge_gate(app, tmp_path, monkeypatch):
    disabled = FakeBackend(tmp_path, enabled=False)
    assert dialog_module.open_dialog(backend=disabled) is None
    assert disabled.messages == [dialog_module.BRIDGE_DISABLED]
    backend = FakeBackend(tmp_path)
    first = dialog_module.open_dialog(backend=backend)
    try:
        assert first is not None and first.isVisible()
        assert dialog_module.open_dialog(backend=backend) is first
    finally:
        first.close()
    assert dialog_module._dialog is None


def test_settings_tolerant_read(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    assert dialog_module.load_settings(path) == dialog_module.normalize_settings({})
    path.write_text("{not json", encoding="utf-8")
    assert dialog_module.load_settings(path)["mode"] == "convert"
    path.write_text(
        json.dumps({"settings": {"mode": "bogus", "save_location": "cloud", "frame_step": 0, "asset_root": ""}}),
        encoding="utf-8",
    )
    loaded = dialog_module.load_settings(path)
    assert (loaded["mode"], loaded["save_location"], loaded["frame_step"]) == ("convert", "files", 1)
    assert loaded["asset_root"] == dialog_module.DEFAULT_ASSET_ROOT
    monkeypatch.setenv("CYCLES_HOUDINI_BLEND_IMPORT_SETTINGS_FILE", str(tmp_path / "explicit.json"))
    assert dialog_module.settings_path() == tmp_path / "explicit.json"
    monkeypatch.delenv("CYCLES_HOUDINI_BLEND_IMPORT_SETTINGS_FILE")
    monkeypatch.setenv("CYCLES_HOUDINI_ASSET_BROWSER_SETTINGS_FILE", str(tmp_path / "browser" / "asset.json"))
    assert dialog_module.settings_path() == tmp_path / "browser" / dialog_module.SETTINGS_FILENAME
    saved = dialog_module.save_settings({"mode": "link", "frames": "range", "frame_step": 4}, path)
    assert saved["mode"] == "link" and dialog_module.load_settings(path)["frame_step"] == 4


@pytest.mark.parametrize(
    "old, mode, location",
    [
        ({"storage": "reference"}, "link", "files"),
        ({"storage": "files"}, "convert", "files"),
        ({"storage": "embedded"}, "convert", "embedded"),
        ({"storage": "bogus"}, "convert", "files"),
        ({"storage": "embedded", "mode": "link"}, "link", "embedded"),  # a new key wins over the old one
    ],
)
def test_old_dialog_settings_normalise_to_mode_and_save_location(tmp_path, old, mode, location):
    path = tmp_path / "settings.json"
    values = dict(old, texture_location="cache", asset_root="$HIP/geo/assets_blender/", structure="merged")
    path.write_text(json.dumps({"version": 1, "settings": values}), encoding="utf-8")
    loaded = dialog_module.load_settings(path)
    assert (loaded["mode"], loaded["save_location"]) == (mode, location)
    assert loaded["asset_root"] == "$HIP/assets_blender"  # the old default folder moves with the layout
    assert loaded["structure"] == "packed"  # the removed Merged becomes Without hierarchy
    assert "texture_location" not in loaded and "storage" not in loaded
    assert (
        dialog_module.normalize_settings({"asset_root": "$HIP/geo/blender_assets"})["asset_root"]
        == "$HIP/assets_blender"
    )
    assert dialog_module.normalize_settings({"asset_root": "D:/my/assets"})["asset_root"] == "D:/my/assets"


class SharedBackend(FakeBackend):
    def __init__(self, tmp_path, shared=(), answer=True, **kwargs):
        super().__init__(tmp_path, **kwargs)
        self.shared, self.answer = list(shared), answer
        self.shared_calls, self.questions = [], []

    def shared_scenes(self, source, selection, kwargs):
        self.threads["shared"] = threading.current_thread()
        self.shared_calls.append((source, selection, dict(kwargs)))
        return self.shared

    def ask_replace(self, question):
        self.questions.append(question)
        return self.answer


SHARED = [{"kind": "collection", "name": "Props", "asset": "Props", "scenes": ["E:/shots/shot_v01.hip"]}]


def _shared_dialog(app, tmp_path, monkeypatch, **kwargs):
    monkeypatch.delenv("CYCLES_HOUDINI_BLEND_IMPORT_SETTINGS_FILE", raising=False)
    source = tmp_path / "scene.blend"
    source.write_bytes(b"BLENDER")
    backend = SharedBackend(tmp_path, **kwargs)
    dialog = dialog_module.BlendImportDialog(backend=backend)
    dialog.load_file(str(source))
    assert drain(app, dialog, lambda: dialog._listing is not None)
    dialog.set_checked("collection", "Props")
    return dialog, backend


def test_dialog_asks_before_replacing_files_of_other_scenes(app, tmp_path, monkeypatch):
    dialog, backend = _shared_dialog(app, tmp_path, monkeypatch, shared=SHARED, answer=False)
    try:
        dialog.start_import()  # Cancel: nothing starts
        assert backend.questions == ['"Props" is also used by: shot_v01.hip. Replacing it changes that scene too.']
        assert not dialog._importing and not backend.prepared and "cancelled" in dialog.status.text()
        assert backend.threads["shared"] is threading.main_thread()  # asked on the GUI thread
        (_, selection, kwargs) = backend.shared_calls[-1]
        assert kwargs["storage"] == "files" and kwargs["asset_root"] == "$HIP/assets_blender"
        backend.answer = True  # Replace: the prepare runs with on_shared="replace"
        dialog.start_import()
        assert drain(app, dialog, lambda: not dialog._importing and backend.prepared)
        assert backend.prepared[-1][2]["on_shared"] == "replace" and len(backend.questions) == 2
    finally:
        dialog.dispose()


@pytest.mark.parametrize("mode, location", [("link", "files"), ("convert", "embedded")])
def test_dialog_does_not_ask_when_no_files_are_replaced(app, tmp_path, monkeypatch, mode, location):
    dialog, backend = _shared_dialog(app, tmp_path, monkeypatch, shared=SHARED, answer=False)
    try:
        dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData(mode))
        dialog.save_location_combo.setCurrentIndex(dialog.save_location_combo.findData(location))
        dialog.start_import()
        assert drain(app, dialog, lambda: not dialog._importing and backend.prepared)
        assert not backend.shared_calls and not backend.questions
        assert "on_shared" not in backend.prepared[-1][2]
    finally:
        dialog.dispose()


def test_dialog_without_shared_scenes_imports_without_asking(app, tmp_path, monkeypatch):
    dialog, backend = _shared_dialog(app, tmp_path, monkeypatch, shared=[], answer=False)
    try:
        dialog.start_import()
        assert drain(app, dialog, lambda: not dialog._importing and backend.prepared)
        assert backend.shared_calls and not backend.questions and "on_shared" not in backend.prepared[-1][2]
    finally:
        dialog.dispose()


def test_dialog_shows_the_converter_build_report(app, tmp_path, monkeypatch):
    dialog, backend = _shared_dialog(app, tmp_path, monkeypatch)
    in_place = "Textures stay in place: 3 texture file(s) are read from where the .blend points."

    def build(payload):
        backend.built.append(payload)
        payload["build_messages"] = [(in_place, False)]
        return [Node("/obj/Props")]

    backend.build = build
    backend.prepare = lambda source, selection, kwargs, cancelled, report: {"payload": 1}
    try:
        dialog.start_import()
        assert drain(app, dialog, lambda: not dialog._importing and backend.built)
        assert "Imported 1 node: /obj/Props" in dialog.status.text() and in_place in dialog.status.text()
        assert in_place in dialog.status.toolTip()
    finally:
        dialog.dispose()


def test_houdini_backend_shared_scenes_and_question(monkeypatch):
    calls = []
    fake = types.ModuleType("cycles_houdini.blender.blend_convert")
    fake.shared_scenes = lambda source, selection, **options: calls.append((source, selection, options)) or SHARED
    monkeypatch.setitem(sys.modules, "cycles_houdini.blender.blend_convert", fake)
    backend = dialog_module.HoudiniBackend()
    kwargs = {"asset_root": "$HIP/assets_blender", "structure": "packed", "storage": "files"}
    assert backend.shared_scenes("a.blend", [("object", "R")], kwargs) == SHARED
    assert calls == [("a.blend", [("object", "R")], {"asset_root": "$HIP/assets_blender", "structure": "packed"})]
    answers = []
    hou = types.SimpleNamespace(
        ui=types.SimpleNamespace(displayMessage=lambda text, **options: answers.append((text, options)) or 0),
        severityType=types.SimpleNamespace(Warning="warning"),
    )
    monkeypatch.setitem(sys.modules, "hou", hou)
    assert backend.ask_replace("question") is True
    assert answers[0][1]["buttons"] == ("Replace", "Cancel") and answers[0][1]["close_choice"] == 1


# ---------------------------------------------------------------- Edit Selection (Blender Hierarchy object)


def test_link_hides_structure_and_imports_without_hierarchy(app, tmp_path, monkeypatch):
    dialog, backend, _ = opened(app, tmp_path, monkeypatch)
    try:
        dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData("convert"))
        dialog.structure_combo.setCurrentIndex(dialog.structure_combo.findData("hierarchy"))
        assert dialog.structure_combo.isVisibleTo(dialog)
        dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData("link"))
        # Hidden, not greyed out: Link + Hierarchy is not offered.
        assert not dialog.structure_combo.isVisibleTo(dialog)
        assert dialog.import_arguments()[2]["structure"] == "packed"
        assert dialog.import_arguments()[2]["storage"] == "reference"
    finally:
        dialog.dispose()


def test_edit_selection_reads_the_file_in_the_background_and_returns_the_choice(app, tmp_path):
    source = tmp_path / "scene.blend"
    source.write_bytes(b"BLENDER")
    backend = FakeBackend(tmp_path)
    gate = threading.Event()
    listed = backend.list_contents

    def slow_list(*args):
        gate.wait(5)
        return listed(*args)

    backend.list_contents = slow_list
    seen = {}

    def run(dialog):
        # Busy while Blender reads the file: OK is disabled, Cancel is available.
        seen["busy"] = dialog.progress.isVisibleTo(dialog) and not dialog.ok_button.isEnabled()
        assert dialog.cancel_button.isEnabled()
        gate.set()
        assert drain(app, dialog, lambda: dialog._listing is not None)
        assert backend.threads["list"] is not threading.main_thread()
        # The current selection is checked, nothing else; a name gone from the file is reported.
        seen["initial"] = dialog.selection()
        seen["missing"] = list(dialog.missing)
        assert "Gone" in dialog.status.text()
        dialog.set_checked("object", "Root Empty")
        return QtWidgets.QDialog.Accepted

    chosen = dialog_module.edit_selection(
        str(source), [("object", "Rock"), ("collection", "Gone")], backend=backend, run=run
    )
    assert seen["busy"]
    assert seen["initial"] == [("object", "Rock")] and seen["missing"] == [("collection", "Gone")]
    assert chosen == [("object", "Rock"), ("object", "Root Empty")]
    # Cancel returns None.
    cancelled = dialog_module.edit_selection(
        str(source), [("object", "Rock")], backend=FakeBackend(tmp_path), run=lambda dialog: QtWidgets.QDialog.Rejected
    )
    assert cancelled is None
    with pytest.raises(ValueError, match="bridge is disabled"):
        dialog_module.edit_selection(str(source), [], backend=FakeBackend(tmp_path, enabled=False), run=lambda d: 1)


def test_edit_selection_include_disabled_and_missing_file(app, tmp_path):
    source = tmp_path / "scene.blend"
    source.write_bytes(b"BLENDER")

    def run(dialog):
        assert drain(app, dialog, lambda: dialog._listing is not None)
        dialog.set_checked("collection", "Props")
        return QtWidgets.QDialog.Accepted

    # With Include Disabled Collections on, Props implies its disabled child and the child's object.
    chosen = dialog_module.edit_selection(
        str(source), [("object", "Scatter")], include_disabled=True, backend=FakeBackend(tmp_path), run=run
    )
    assert chosen == [("collection", "Props")]
    statuses = []

    def run_missing(dialog):
        statuses.append((dialog.status.text(), dialog.ok_button.isEnabled()))
        return QtWidgets.QDialog.Accepted

    assert (
        dialog_module.edit_selection(
            str(tmp_path / "gone.blend"), [("object", "Rock")], backend=FakeBackend(tmp_path), run=run_missing
        )
        is None
    )
    assert statuses[0][0].startswith("File not found") and statuses[0][1] is False
