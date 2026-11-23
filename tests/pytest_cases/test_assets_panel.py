# SPDX-License-Identifier: Apache-2.0
"""Asset browser panel, widgets and settings with fake providers (offscreen Qt, no hou)."""

import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

pytestmark = [pytest.mark.pure, pytest.mark.cpu]

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from cycles_houdini.assets import settings as settings_module
from PySide6 import QtCore, QtGui

A, A1, B, LOST = (
    "aaaaaaaa-0000-0000-0000-000000000001",
    "aaaaaaaa-0000-0000-0000-000000000002",
    "bbbbbbbb-0000-0000-0000-000000000001",
    "cccccccc-0000-0000-0000-000000000009",
)


@pytest.fixture(scope="module")
def app():
    application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield application


@pytest.fixture(autouse=True)
def no_hou(monkeypatch):
    # These tests must never touch HOM; make an accidental import fail loudly.
    monkeypatch.setitem(sys.modules, "hou", None)


# ---------------------------------------------------------------- fakes


@dataclass
class Node:
    path: str
    name: str
    uuids: list = field(default_factory=list)
    simple_names: list = field(default_factory=list)
    children: list = field(default_factory=list)


class Tree:
    def __init__(self, root):
        self.root = root
        self.warnings = []
        self._by_uuid = {}
        stack = [root]
        while stack:
            node = stack.pop()
            for uuid in node.uuids:
                self._by_uuid[uuid] = node
            stack.extend(node.children)

    def node_for_uuid(self, uuid):
        return self._by_uuid.get(uuid)


def make_tree():
    return Tree(
        Node(
            "",
            "",
            children=[
                Node("Metals", "Metals", [A], children=[Node("Metals/Rusty", "Rusty", [A1])]),
                Node("Wood", "Wood", [B]),
            ],
        )
    )


def record(name, id_type="MATERIAL", catalog="", tags=(), thumb=None, object_type=None, state="unknown"):
    rel = "lib/" + name.lower().replace(" ", "_") + ".blend"
    return {
        "name": name,
        "id_type": id_type,
        "files": [rel],
        "thumbnail": {"url": thumb, "hash": ""} if thumb else None,
        "meta": {
            "catalog_id": catalog,
            "tags": list(tags),
            "author": "Ann",
            "description": "d",
            "license": "CC0",
            "copyright": "",
        },
        "ch": {
            "key": rel + "|" + id_type + "|" + name,
            "source": "/library/" + rel,
            "catalog_simple_name": "",
            "object_type": object_type,
            "importable": {"state": state, "reason": ""},
            "provider": "worker",
        },
    }


def sample_records():
    return [
        record("Galvanized Steel", catalog=A, tags=("metal", "Shiny")),
        record("Rust Patina", catalog=A1, tags=("metal", "old")),
        record("Oak Planks", catalog=B, tags=("wood",)),
        record("Chair", "OBJECT", catalog=B, object_type="MESH"),
        record("Furniture Set", "COLLECTION"),
        record("Lost Asset", "OBJECT", catalog=LOST, object_type="CAMERA"),
        record("Sky", "WORLD"),
    ]


class ContentError(ValueError):
    pass


class UnavailableError(ContentError):
    """Like LibraryMaterialUnavailable: a subclass that is NOT a content verdict."""


@dataclass
class Library:
    name: str
    path: str
    enabled: bool = True
    import_method: str = "APPEND"
    provider: str = "worker"


class FakeJob:
    def __init__(self, provider):
        self.provider = provider
        self.started = self.cancelled = False
        self.polls = 0

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def poll(self):
        self.polls += 1
        if self.cancelled:
            return {"state": "cancelled", "progress": (0, 0), "result": None, "error": ""}
        if self.polls < 2:
            return {"state": "running", "progress": (1, 3), "result": None, "error": ""}
        result = SimpleNamespace(
            records=self.provider.scan_records,
            errors=[{"source": "/library/bad.blend", "error": "boom"}],
            scanned=2,
            reused=1,
            blender_missing=self.provider.blender_missing,
            message="",
        )
        return {"state": "done", "progress": (3, 3), "result": result, "error": ""}


class FakeCache:
    def __init__(self):
        self.importability = {}

    def set_importability(self, key, state, reason):
        self.importability[key] = (state, reason)


class FakeProvider:
    def __init__(self, library, kind, cached, scan_records, blender_missing=False):
        self.library, self.kind = library, kind
        self.cached, self.scan_records = cached, scan_records
        self.blender_missing = blender_missing
        self.cache = FakeCache()

    def items(self):
        return list(self.cached)

    def catalogs(self):
        return make_tree()


class FakeBackend:
    def __init__(self, tmp_path, libraries=(), cached=(), scan=None, blender_missing=False, enabled=True):
        self.tmp_path = tmp_path
        self.libraries = list(libraries)
        self.cached = list(cached)
        self.scan = list(cached if scan is None else scan)
        self.blender_missing = blender_missing
        self._enabled = enabled
        self.saved = []
        self.providers = []
        self.jobs = []
        self.imports = []
        self.assigned = []
        self.snapshots = []
        self.append_snapshots = []
        self.prepared = []
        self.gate = None  # threading.Event holding prepare_material until set
        self.prepare_errors = {"Rust Patina": ContentError("Unsupported node: Wave")}
        self.selection = []
        self.existing = None
        self.linked_node = None
        self.linked = None
        self.find_modes = []
        self.reload_paths = []
        self.rebuilt = []
        self.rebuild_report = []  # (text, warning) lines rebuild_linked reports
        self.notice = ""
        self.world_snapshots = []
        self.environment = []  # names existing_environment returns
        self.environment_modes = []
        self.linked_world = None

    def enabled(self):
        return self._enabled

    def settings_path(self):
        return self.tmp_path / "settings.json"

    def read_libraries(self):
        return list(self.libraries), ""

    def save_libraries(self, items):
        self.libraries = list(items)
        self.saved.append(list(items))

    def new_library(self, name, path):
        return Library(name, path)

    def merge_libraries(self, existing, dumped):
        return list(existing) + [Library(d["name"], d["path"]) for d in dumped]

    def make_provider(self, library, kind):
        provider = FakeProvider(library, kind, self.cached, self.scan, self.blender_missing)
        self.providers.append(provider)
        return provider

    def make_job(self, provider):
        job = FakeJob(provider)
        self.jobs.append(job)
        return job

    def import_asset(self, record, **options):
        self.imports.append((record["name"], options))
        return [SimpleNamespace(path=lambda: "/obj/" + record["name"])]

    def assign_material(self, record, nodes=None, **options):
        self.assigned.append((record["name"], list(nodes), options))
        return SimpleNamespace(path=lambda: "/mat/" + record["name"]), list(nodes)

    def material_snapshot(self, record, **options):
        self.snapshots.append((record["name"], options))
        return {"notice": self.notice, "name": record["name"]}

    def world_snapshot(self, record, **options):
        self.world_snapshots.append((record["name"], options))
        return {"notice": self.notice, "name": record["name"], "kind": "world"}

    def prepare_world(self, snapshot, cancelled):
        return self.prepare_material(snapshot, cancelled)

    def existing_environment(self, record, mode):
        self.environment_modes.append(mode)
        return list(self.environment)

    def find_linked_world(self, record):
        return self.linked_world

    def append_snapshot(self, record, **options):
        self.append_snapshots.append((record["name"], options))
        return {"notices": [], "name": record["name"]}

    def prepare_append(self, snapshot, cancelled):
        # Background thread: no Qt, no hou.
        if cancelled.is_set():
            raise UnavailableError("Blender import cancelled")
        return {"items": [snapshot["name"]]}

    def prepare_material(self, snapshot, cancelled):
        # Background thread: no Qt, no hou.
        if self.gate is not None:
            self.gate.wait(5)
        if cancelled.is_set():
            raise UnavailableError("Material import cancelled")
        error = self.prepare_errors.get(snapshot["name"])
        if error is not None:
            raise error
        self.prepared.append(snapshot["name"])
        return {"name": snapshot["name"], "texture_root": "$HIP/tex"}

    def is_content_failure(self, error):
        return isinstance(error, ContentError) and not isinstance(error, UnavailableError)

    def record_importability(self, record, cache, state, reason):
        cache.set_importability(record["ch"]["key"], state, reason)
        record["ch"]["importable"] = {"state": state, "reason": reason}

    def find_imported_material(self, record, mode="append"):
        self.find_modes.append(mode)
        return self.existing if mode != "link" else self.linked_node

    def linked_status(self, record):
        return self.linked

    def reload_snapshot(self, path):
        self.reload_paths.append(path)
        return {"notice": "", "name": "reload:" + path}

    def prepare_reload(self, snapshot, cancelled):
        return self.prepare_material(snapshot, cancelled)

    def rebuild_linked(self, path, prepared, report=None):
        self.rebuilt.append((path, prepared))
        for line in self.rebuild_report:
            report(*line)
        return [SimpleNamespace(path=lambda: path)]

    def selected_object_paths(self):
        return list(self.selection)

    def blender_snapshot(self):
        return ("", "", "")

    def resolve_blender(self, snapshot):
        raise FileNotFoundError("Blender not found")

    def dump_preferences(self, executable):
        return [{"name": "Blender Lib", "path": "/blender/lib"}]


def drain(app, panel, predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        panel.poll()
        if predicate():
            return True
        time.sleep(0.01)
    return False


# ---------------------------------------------------------------- pure filters


def test_search_type_and_catalog_filters_pure():
    from cycles_houdini.assets import widgets

    records = sample_records()
    words = widgets.search_words("METAL shi")
    assert [r["name"] for r in records if widgets.matches_search(r, words)] == ["Galvanized Steel"]
    assert [r["name"] for r in records if widgets.matches_search(r, widgets.search_words("oak"))] == ["Oak Planks"]
    assert len([r for r in records if widgets.matches_search(r, [])]) == len(records)
    assert [r["name"] for r in records if widgets.matches_type(r, "COLLECTION")] == ["Furniture Set"]
    tree = make_tree()
    metals = frozenset(widgets.subtree_uuids(tree.root.children[0]))
    assert metals == {A, A1}
    assert [r["name"] for r in records if widgets.matches_catalog(r, ("catalog", metals))] == [
        "Galvanized Steel",
        "Rust Patina",
    ]
    known = frozenset(widgets.subtree_uuids(tree.root))
    unassigned = [r["name"] for r in records if widgets.matches_catalog(r, ("unassigned", known))]
    assert unassigned == ["Furniture Set", "Lost Asset", "Sky"]
    assert widgets.catalog_path(records[1], tree) == "Metals/Rusty"
    assert widgets.catalog_path(records[4], tree) == "Unassigned"


def test_importability_text_and_modes():
    from cycles_houdini.assets import importers, widgets

    assert importers.valid_modes("MATERIAL") == ("link", "append")
    assert importers.effective_mode("OBJECT", "append") == "append"
    assert importers.effective_mode("MATERIAL", "link") == "link"
    assert importers.effective_mode("MATERIAL", "append") == "append"
    assert importers.valid_modes("COLLECTION") == ("link", "append")
    assert importers.valid_modes("WORLD") == ("link", "append") and not importers.unsupported_reason(
        record("Sky", "WORLD")
    )
    assert importers.effective_mode("NODETREE", "link") is None
    assert "cannot be imported" in importers.unsupported_reason(record("Group", "NODETREE"))
    assert "worlds" in importers.unsupported_reason(record("Group", "NODETREE"))
    assert "Environment Light" in widgets.importability_text(record("Sky", "WORLD"))
    assert importers.node_name("3 Metal - Galvanized!") == "_3_Metal_Galvanized"
    assert importers.target_token('a "b" *') == '"a \\"b\\" *"'
    assert "camera" in widgets.importability_text(record("Cam", "OBJECT", object_type="CAMERA"))
    bad = record("Bad", state="no")
    bad["ch"]["importable"]["reason"] = "Unsupported node: Wave"
    assert widgets.importability_text(bad) == "Not importable: Unsupported node: Wave"


# ---------------------------------------------------------------- settings


def test_settings_round_trip_and_tolerant_read(tmp_path):
    path = tmp_path / "nested" / "settings.json"
    assert settings_module.load_settings(path) == settings_module.DEFAULTS
    values = dict(settings_module.DEFAULTS)
    values.update(
        thumbnail_size=180,
        import_mode="append",
        append_storage="embedded",
        drag_enabled=True,
        drag_route="experimental",
        last_library="Mine",
        splitter_sizes=[100, 400, 200],
        left_visible=False,
    )
    saved = settings_module.save_settings(values, path)
    assert settings_module.load_settings(path) == saved == values
    assert not list(path.parent.glob("*.tmp"))
    path.write_text("{not json", encoding="utf-8")
    assert settings_module.load_settings(path) == settings_module.DEFAULTS
    path.write_text(
        json.dumps(
            {
                "settings": {
                    "thumbnail_size": 9999,
                    "import_mode": "copy",
                    "drag_enabled": True,
                    "drag_route": "off",
                    "splitter_sizes": [1, 2],
                    "unknown": 1,
                }
            }
        ),
        encoding="utf-8",
    )
    loaded = settings_module.load_settings(path)
    assert loaded["thumbnail_size"] == settings_module.THUMBNAIL_MAX
    assert loaded["import_mode"] == "link"
    assert loaded["drag_enabled"] is False
    assert loaded["splitter_sizes"] == []
    assert "unknown" not in loaded


def test_settings_path_env_and_registry_sibling(tmp_path, monkeypatch):
    monkeypatch.setenv("CYCLES_HOUDINI_ASSET_BROWSER_SETTINGS_FILE", str(tmp_path / "x.json"))
    assert settings_module.settings_path() == tmp_path / "x.json"
    monkeypatch.delenv("CYCLES_HOUDINI_ASSET_BROWSER_SETTINGS_FILE")
    monkeypatch.setenv("CYCLES_HOUDINI_ASSET_LIBRARIES_FILE", str(tmp_path / "reg" / "libs.json"))
    assert settings_module.settings_path() == tmp_path / "reg" / settings_module.SETTINGS_FILENAME


# ---------------------------------------------------------------- model, proxy, tree


def test_model_proxy_and_catalog_tree(app, tmp_path):
    from cycles_houdini.assets import widgets

    image = QtGui.QImage(32, 16, QtGui.QImage.Format_ARGB32)
    image.fill(QtGui.QColor("red"))
    thumb = tmp_path / "thumb.png"
    assert image.save(str(thumb))
    records = sample_records()
    records[0]["thumbnail"] = {"url": str(thumb), "hash": ""}
    model = widgets.AssetModel()
    model.set_records(records)
    assert model.rowCount() == len(records)
    index = model.index(0)
    assert model.data(index) == "Galvanized Steel"
    assert model.data(index, widgets.TypeRole) == "MATERIAL"
    assert model.data(index, widgets.KeyRole).endswith("|MATERIAL|Galvanized Steel")
    assert "metal" in model.data(index, QtCore.Qt.ToolTipRole)
    # Thumbnail decode is lazy and off-thread; first request returns None.
    assert model.data(index, QtCore.Qt.DecorationRole) is None
    loaded = []
    model.loader.loaded.connect(loaded.append)
    end = time.monotonic() + 5
    while not loaded and time.monotonic() < end:
        app.processEvents()
        model.loader.drain()
        time.sleep(0.01)
    pixmap = model.data(index, QtCore.Qt.DecorationRole)
    assert isinstance(pixmap, QtGui.QPixmap) and pixmap.width() == 32
    assert model.data(model.index(1), QtCore.Qt.DecorationRole) is None  # no thumbnail -> placeholder

    proxy = widgets.AssetFilterProxy()
    proxy.setSourceModel(model)
    names = lambda: [proxy.index(r, 0).data() for r in range(proxy.rowCount())]
    assert names() == sorted((r["name"] for r in records), key=str.casefold)
    proxy.set_search("metal")
    assert names() == ["Galvanized Steel", "Rust Patina"]
    proxy.set_search("")
    proxy.set_type("OBJECT")
    assert names() == ["Chair", "Lost Asset"]
    proxy.set_type("ALL")

    tree = widgets.CatalogTreeWidget()
    specs = []
    tree.filterChanged.connect(specs.append)
    tree.populate(make_tree(), records)
    top = [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())]
    assert top == ["All", "Unassigned"]
    everything = tree.topLevelItem(0)
    assert everything.text(1) == str(len(records))
    assert [everything.child(i).text(0) for i in range(everything.childCount())] == ["Metals", "Wood"]
    assert everything.child(0).text(1) == "2"
    assert tree.topLevelItem(1).text(1) == "3"
    proxy.set_catalog(tree.current_spec())
    assert len(names()) == len(records)
    tree.select_path("Metals")
    proxy.set_catalog(specs[-1])
    assert names() == ["Galvanized Steel", "Rust Patina"]
    tree.select_path("Metals/Rusty")
    proxy.set_catalog(specs[-1])
    assert names() == ["Rust Patina"]
    tree.select_path(widgets.CatalogTreeWidget.UNASSIGNED)
    proxy.set_catalog(specs[-1])
    assert names() == ["Furniture Set", "Lost Asset", "Sky"]
    # Repopulating keeps the selected catalog.
    tree.select_path("Wood")
    tree.populate(make_tree(), records)
    assert tree.current_path() == "Wood"
    model.loader.close()


def test_grid_view_paints_offscreen(app):
    from cycles_houdini.assets import widgets

    model = widgets.AssetModel()
    model.set_records(sample_records())
    view = widgets.AssetGridView()
    view.setModel(model)
    view.set_thumbnail_size(64)
    assert view.gridSize().width() == 64 + 12
    view.resize(400, 300)
    view.show()
    app.processEvents()
    image = view.grab()
    assert not image.isNull()
    view.close()
    model.loader.close()


# ---------------------------------------------------------------- panel


def test_panel_disabled_shows_message(app, tmp_path):
    from cycles_houdini.assets.panel import DISABLED, AssetBrowserPanel

    panel = AssetBrowserPanel(backend=FakeBackend(tmp_path, enabled=False))
    labels = [label.text() for label in panel.findChildren(QtWidgets.QLabel)]
    assert DISABLED in labels
    panel.dispose()
    panel.dispose()


def test_panel_empty_registry(app, tmp_path):
    from cycles_houdini.assets.panel import NO_LIBRARIES, AssetBrowserPanel

    backend = FakeBackend(tmp_path)
    panel = AssetBrowserPanel(backend=backend)
    assert panel.center.currentWidget() is panel.empty
    assert panel.empty_label.text() == NO_LIBRARIES
    assert panel.empty_add.isVisibleTo(panel) and panel.empty_import.isVisibleTo(panel)
    assert not backend.jobs
    panel.add_library_folder(str(tmp_path / "MyLib"))
    assert backend.saved and backend.saved[-1][0].name == "MyLib"
    assert panel.library_combo.count() == 1 and backend.jobs
    panel.dispose()
    assert backend.jobs[-1].cancelled
    assert json.loads((tmp_path / "settings.json").read_text())["settings"]["last_library"] == "MyLib"


def test_panel_with_fake_provider(app, tmp_path):
    from cycles_houdini.assets.panel import BLENDER_MISSING, AssetBrowserPanel

    cached = sample_records()[:2]
    backend = FakeBackend(
        tmp_path,
        libraries=[Library("Off", "/off", enabled=False), Library("Main", "/library")],
        cached=cached,
        scan=sample_records(),
        blender_missing=True,
    )
    panel = AssetBrowserPanel(backend=backend)
    panel.resize(900, 500)
    panel.show()
    app.processEvents()
    assert panel.library_combo.count() == 1 and panel.library_combo.currentText() == "Main"
    # Cached records are shown immediately, before indexing finishes.
    assert panel.model.rowCount() == 2
    assert panel.center.currentWidget() is panel.grid
    assert panel.progress.isVisibleTo(panel)
    assert drain(app, panel, lambda: panel._job is None)
    assert panel.model.rowCount() == len(sample_records())
    assert panel.banner.isVisibleTo(panel) and panel.banner.text() == BLENDER_MISSING
    assert "1 file errors" in panel.status.text() and "bad.blend: boom" in panel.status.toolTip()
    assert not panel.timer.isActive()

    # Search, type filter and catalog filter through the real widgets.
    panel.search.setText("wood")
    assert panel.proxy.rowCount() == 1
    panel.search.setText("")
    panel.type_combo.setCurrentIndex(panel.type_combo.findData("MATERIAL"))
    assert panel.proxy.rowCount() == 3
    panel.type_combo.setCurrentIndex(panel.type_combo.findData("ALL"))
    panel.catalogs.select_path("Metals")
    assert panel.proxy.rowCount() == 2

    # Selecting a material restricts import modes to Append and fills details.
    assert panel.select_key(sample_records()[1]["ch"]["key"])
    assert panel.details.labels["catalog"].text() == "Metals/Rusty"
    model = panel.mode_combo.model()
    enabled = {model.item(r).data(QtCore.Qt.UserRole): model.item(r).isEnabled() for r in range(model.rowCount())}
    assert enabled == {"link": True, "append": True}  # materials: Link (shared, locked) or Append
    assert panel.details.assign_button.isVisibleTo(panel)
    assert panel.details.import_button.text() == "Import"  # the header dropdown alone picks the mode

    # A content failure in the background export marks importability and updates the badge.
    assert panel.import_record() is None  # materials finish asynchronously
    name, options = backend.snapshots[-1]
    assert name == "Rust Patina" and options == {"library_root": "/library"}  # textures stay in place
    assert drain(app, panel, lambda: not panel._material_tasks and "Import failed" in panel.status.text())
    assert backend.providers[-1].cache.importability[sample_records()[1]["ch"]["key"]][0] == "no"
    assert not backend.imports
    assert panel.current_record()["ch"]["importable"]["state"] == "no"
    assert panel.details.labels["importable"].text().startswith("Not importable")

    # Objects: Link (reference) or Append (converted geometry), both enabled.
    panel.catalogs.select_path("Wood")
    assert panel.select_key(sample_records()[3]["ch"]["key"])
    item = panel.mode_combo.model().item(panel.mode_combo.findData("append", QtCore.Qt.UserRole))
    assert item.isEnabled() and item.text() == "Convert & Append" and "Save Location" in item.toolTip()
    assert panel.import_record() and backend.imports[-1][1]["mode"] == "link"
    panel.dispose()


def test_panel_import_passes_effective_mode(app, tmp_path):
    from cycles_houdini.assets.panel import AssetBrowserPanel

    seen = []

    class Backend(FakeBackend):
        def import_asset(self, record, **options):
            seen.append((record["id_type"], options["mode"]))
            return []

    records = sample_records()
    panel = AssetBrowserPanel(backend=Backend(tmp_path, libraries=[Library("Main", "/library")], cached=records))
    panel._set_mode_combo("append")
    panel.settings["import_mode"] = "append"
    panel.backend.prepare_errors = {}
    for rec in records[:5]:
        panel.import_record(rec)
    assert seen == []  # Append converts objects/collections in the background, like materials
    assert [name for name, _ in panel.backend.append_snapshots] == [r["name"] for r in records[3:5]]
    assert drain(app, panel, lambda: len(seen) == 5)
    assert sorted(seen) == [
        ("COLLECTION", "append"),
        ("MATERIAL", "append"),
        ("MATERIAL", "append"),
        ("MATERIAL", "append"),
        ("OBJECT", "append"),
    ]
    panel.import_record(records[6])  # a world: exported in the background too
    assert drain(app, panel, lambda: len(seen) == 6) and seen[-1] == ("WORLD", "append")
    panel.import_record(record("Group", "NODETREE"))
    assert len(seen) == 6 and "cannot be imported" in panel.status.text()
    panel.dispose()


def test_panel_t_n_toggles_and_settings(app, tmp_path):
    from cycles_houdini.assets.panel import AssetBrowserPanel

    backend = FakeBackend(tmp_path, libraries=[Library("Main", "/library")], cached=sample_records())
    panel = AssetBrowserPanel(backend=backend)
    panel.show()
    app.processEvents()

    def key(widget, code):
        for kind in (QtCore.QEvent.ShortcutOverride, QtCore.QEvent.KeyPress):
            event = QtGui.QKeyEvent(kind, code, QtCore.Qt.NoModifier, "t" if code == QtCore.Qt.Key_T else "n")
            QtWidgets.QApplication.sendEvent(widget, event)

    panel.grid.setFocus()
    app.processEvents()
    assert panel.catalogs.isVisibleTo(panel) and panel.details.isVisibleTo(panel)
    key(panel.grid, QtCore.Qt.Key_T)
    assert not panel.catalogs.isVisibleTo(panel)
    key(panel.grid, QtCore.Qt.Key_N)
    assert not panel.details.isVisibleTo(panel)
    key(panel.grid, QtCore.Qt.Key_T)
    assert panel.catalogs.isVisibleTo(panel)
    # Typing in the search field never toggles panels.
    panel.search.setFocus()
    app.processEvents()
    key(panel.search, QtCore.Qt.Key_T)
    assert panel.catalogs.isVisibleTo(panel)
    # Keys aimed at a widget outside the panel are ignored.
    outside = QtWidgets.QLineEdit()
    other = QtWidgets.QListWidget()
    key(other, QtCore.Qt.Key_N)
    assert not panel.details.isVisibleTo(panel)
    panel.size_slider.setValue(150)
    panel.set_drag_route("experimental")
    assert panel.grid.dragEnabled()
    panel.dispose()
    saved = settings_module.load_settings(tmp_path / "settings.json")
    assert saved["thumbnail_size"] == 150 and saved["left_visible"] is True and saved["right_visible"] is False
    assert saved["drag_enabled"] is True and saved["drag_route"] == "experimental"
    reopened = AssetBrowserPanel(backend=backend)
    assert not reopened.details.isVisibleTo(reopened) and reopened.size_slider.value() == 150
    reopened.dispose()
    outside.deleteLater()
    other.deleteLater()


def test_panel_provider_switch_and_blender_preferences(app, tmp_path):
    from cycles_houdini.assets.panel import AssetBrowserPanel, LibrariesDialog

    backend = FakeBackend(tmp_path, libraries=[Library("Main", "/library")], cached=sample_records())
    panel = AssetBrowserPanel(backend=backend)
    panel.provider_combo.setCurrentIndex(panel.provider_combo.findData("listing"))
    assert backend.saved[-1][0].provider == "listing"
    assert backend.providers[-1].kind == "listing"

    class PrefsBackend(FakeBackend):
        def resolve_blender(self, snapshot):
            return "/bin/blender"

    prefs = PrefsBackend(tmp_path / "p", libraries=[Library("Main", "/library")])
    (tmp_path / "p").mkdir()
    other = AssetBrowserPanel(backend=prefs)
    other.import_blender_libraries()
    assert drain(app, other, lambda: len(prefs.saved) == 1)
    assert [lib.name for lib in prefs.libraries] == ["Main", "Blender Lib"]
    dialog = LibrariesDialog(other, prefs.libraries)
    dialog.add_folder(str(tmp_path / "Extra"))
    dialog.table.item(0, 1).setText("Renamed")
    dialog.table.item(1, 0).setCheckState(QtCore.Qt.Unchecked)
    dialog.table.selectRow(2)
    dialog.remove_selected()
    dialog.accept()
    assert [(lib.name, lib.enabled) for lib in dialog.libraries] == [("Renamed", True), ("Blender Lib", False)]
    # Open Blend File with Blender missing reports without blocking.
    other.open_blend_file(sample_records()[0])
    assert drain(app, other, lambda: "not found" in other.status.text())
    other.dispose()
    panel.dispose()


def test_drag_mime_and_routes(app, monkeypatch):
    from cycles_houdini.assets import drag

    rec = sample_records()[0]
    data = drag.make_mime_data(rec)
    assert data.hasUrls() and data.urls()[0].toLocalFile() == "/library/lib/galvanized_steel.blend"
    assert json.loads(bytes(data.data(drag.MIME_TYPE)).decode())["key"] == rec["ch"]["key"]
    assert drag.begin_drag(QtWidgets.QWidget(), rec, "off") is None

    editor, viewer = object(), object()
    pane = SimpleNamespace(type=lambda: editor, pwd=lambda: "NET", cursorPosition=lambda: (3, 4))
    fake = SimpleNamespace(
        ui=SimpleNamespace(paneTabUnderCursor=lambda: pane),
        paneTabType=SimpleNamespace(NetworkEditor=editor, SceneViewer=viewer),
    )
    monkeypatch.setitem(sys.modules, "hou", fake)
    calls = []

    def importer(record, **options):
        calls.append(options)
        return [SimpleNamespace(path=lambda: "/obj/x")]

    obj = sample_records()[3]
    assert drag.handle_drop(obj, importer=importer) == "Imported /obj/x"
    assert calls == [{"mode": "link", "network": "NET", "position": (3, 4)}]
    pane.type = lambda: viewer
    assert drag.handle_drop(obj, importer=importer) == ""  # objects are not dropped on viewports


def material_panel(tmp_path, **kwargs):
    from cycles_houdini.assets.panel import AssetBrowserPanel

    backend = FakeBackend(tmp_path, libraries=[Library("Main", "/library")], cached=sample_records(), **kwargs)
    backend.prepare_errors = {}
    panel = AssetBrowserPanel(backend=backend)
    app = QtWidgets.QApplication.instance()
    assert drain(app, panel, lambda: panel._job is None)  # finish the initial (fake) index scan
    return panel, backend


def test_material_import_async_one_build_and_duplicate_block(app, tmp_path):
    panel, backend = material_panel(tmp_path)
    backend.gate = threading.Event()
    backend.notice = "The scene is not saved yet: textures go to the shared cache."
    steel = sample_records()[0]
    assert panel.start_material(steel)
    assert "not saved yet" in panel.status.text()
    assert panel.progress.isVisibleTo(panel) and panel.cancel_button.isVisibleTo(panel)
    assert not panel.start_material(steel) and "already being imported" in panel.status.text()
    assert len(backend.snapshots) == 1  # the second request never reached the host snapshot
    assert not backend.imports  # the GUI thread is free while Blender exports
    backend.gate.set()
    assert drain(app, panel, lambda: backend.imports)
    name, options = backend.imports[0]
    assert name == "Galvanized Steel" and options["mode"] == "link" and options["prepared"]["name"] == name
    assert "Imported Galvanized Steel" in panel.status.text() and "not saved yet" in panel.status.text()
    assert not panel._material_tasks and not panel.cancel_button.isVisibleTo(panel)
    panel.dispose()


def test_material_cancel_and_transport_failure_do_not_mark(app, tmp_path):
    panel, backend = material_panel(tmp_path)
    backend.gate = threading.Event()
    rust = sample_records()[1]
    panel.start_material(rust)
    cache = backend.providers[-1].cache
    panel.cancel_button.click()
    backend.gate.set()
    assert drain(app, panel, lambda: not panel._material_tasks)
    assert "cancelled" in panel.status.text() and not backend.imports and not cache.importability
    backend.gate = None
    backend.prepare_errors = {rust["name"]: UnavailableError("Blender evaluation exceeded 300.0 seconds")}
    panel.start_material(rust)
    assert drain(app, panel, lambda: not panel._material_tasks)
    assert "exceeded" in panel.status.text() and not cache.importability
    assert rust["ch"]["importable"]["state"] == "unknown"
    panel.dispose()


def test_material_callback_after_dispose_or_library_change_is_ignored(app, tmp_path):
    panel, backend = material_panel(tmp_path)
    backend.gate = threading.Event()
    panel.start_material(sample_records()[0])
    panel.dispose()
    backend.gate.set()
    time.sleep(0.2)
    app.processEvents()
    panel.poll()  # disposed: no-op
    assert not backend.imports

    panel, backend = material_panel(tmp_path)
    backend.libraries.append(Library("Other", "/other"))
    panel.reload_libraries(select="Main")
    backend.gate = threading.Event()
    panel.start_material(sample_records()[0])
    panel.library_combo.setCurrentIndex(panel.library_combo.findText("Other"))
    backend.gate.set()
    assert drain(app, panel, lambda: not panel._material_tasks)
    assert not backend.imports
    panel.dispose()


def test_assign_reuses_or_imports_in_background(app, tmp_path):
    panel, backend = material_panel(tmp_path)
    steel = sample_records()[0]
    panel.settings["import_mode"] = "append"  # reuse an editable copy made before
    assert panel.assign_record(steel) is None and "Select one or more objects" in panel.status.text()
    backend.selection = ["/obj/box"]
    backend.existing = SimpleNamespace(path=lambda: "/mat/steel")
    assert panel.assign_record(steel) is not None and backend.assigned[-1][2].get("prepared") is None
    backend.existing = None
    assert panel.assign_record(steel) is None
    assert drain(app, panel, lambda: len(backend.assigned) == 2)
    _, nodes, options = backend.assigned[-1]
    assert nodes == ["/obj/box"] and options["prepared"]["name"] == "Galvanized Steel"
    assert options["mode"] == "append"
    panel.settings["import_mode"] = "link"  # link: only the shared linked node is reused
    backend.linked_node = SimpleNamespace(path=lambda: "/mat/linked")
    assert panel.assign_record(steel) is not None
    assert backend.find_modes[-1] == "link" and backend.assigned[-1][2]["mode"] == "link"
    panel.dispose()


def test_listing_missing_offers_generation(app, tmp_path):
    panel, backend = material_panel(tmp_path)
    generated = []

    class ListingProvider(FakeProvider):
        def has_listing(self):
            return bool(generated)

        def generate_listing(self, executable):
            generated.append(executable)

    backend.make_provider = lambda library, kind: ListingProvider(library, kind, [], [], False)
    backend.resolve_blender = lambda snapshot: "/bin/blender"
    panel.provider_combo.setCurrentIndex(panel.provider_combo.findData("listing"))
    assert drain(app, panel, lambda: panel._job is None)
    assert "no Blender asset listing" in panel.empty_label.text()
    assert panel.empty_generate.isVisibleTo(panel) and panel.listing_action.isEnabled()
    panel.confirm_generate_listing = lambda library: False
    assert not panel.generate_listing() and not generated
    panel.confirm_generate_listing = lambda library: True
    assert panel.generate_listing()
    assert drain(app, panel, lambda: generated and not panel._listing_running and panel._job is None)
    assert generated == ["/bin/blender"]
    assert not panel.empty_generate.isVisibleTo(panel)
    panel.dispose()


def test_link_mode_reuses_shared_node_and_reload(app, tmp_path):
    panel, backend = material_panel(tmp_path)
    steel = sample_records()[0]
    assert panel.settings["import_mode"] == "link"
    backend.linked_node = SimpleNamespace(path=lambda: "/mat/steel")
    nodes = panel.import_record(steel)  # already linked: reused at once, no export
    assert nodes and not backend.snapshots and backend.imports[-1][1]["mode"] == "link"
    assert "already linked" in panel.status.text()
    backend.linked_node = None
    panel.settings["import_mode"] = "append"
    assert panel.import_record(steel) is None and backend.snapshots  # append: always a new copy
    assert drain(app, panel, lambda: len(backend.imports) == 2)
    assert backend.imports[-1][1]["mode"] == "append"
    # A stale linked node shows in the details and reloads in the background.
    backend.linked = {"path": "/mat/steel", "stale": True}
    assert panel.select_key(steel["ch"]["key"])
    panel._selection_changed()
    assert "library file changed" in panel.details.labels["linked"].text()
    assert panel.details.reload_button.isVisibleTo(panel) and "source changed" in panel.details.reload_button.text()
    assert panel.reload_linked(steel)
    assert drain(app, panel, lambda: backend.rebuilt)
    assert backend.reload_paths == ["/mat/steel"] and backend.rebuilt[0][0] == "/mat/steel"
    assert "Reloaded" in panel.status.text()
    backend.linked = None
    panel._selection_changed()
    assert not panel.details.reload_button.isVisibleTo(panel)
    panel.dispose()


# ---------------- textures in place, shared scenes (blend-import.md: Import record, Textures in place, Importer dialog)


def _menu_titles(menu):
    return [action.text() for action in menu.actions() if not action.isSeparator()]


def test_options_menu_save_location_and_no_material_textures(app, tmp_path):
    panel, backend = material_panel(tmp_path)
    titles = _menu_titles(panel.options_button.menu())
    assert "Material Textures" not in titles and "Append Objects and Collections" not in titles
    assert "Save Location" in titles
    # Keep the action: under Houdini's PySide6 6.8.3 the wrapper QAction.menu() returns dies with it.
    save_action = next(a for a in panel.options_button.menu().actions() if a.text() == "Save Location")
    save = save_action.menu()
    labels = {action.data(): action.text() for action in save.actions()}
    assert labels == {"files": "Save to Disk as .bgeo.sc", "embedded": "Stash in .hip"}
    assert next(a for a in save.actions() if a.data() == "files").isChecked()  # the default
    assert "$HIP/assets_blender" in next(a for a in save.actions() if a.data() == "files").toolTip()
    assert not any("geo/assets_blender" in a.toolTip() + a.text() for a in save.actions())
    next(a for a in save.actions() if a.data() == "embedded").trigger()
    assert panel.settings["append_storage"] == "embedded"
    model = panel.mode_combo.model()
    assert [model.item(r).text() for r in range(model.rowCount())] == ["Link (reference)", "Convert & Append"]
    panel.dispose()


def test_header_options_and_libraries_buttons_are_icon_only(app, tmp_path):
    panel, backend = material_panel(tmp_path)
    for button, name, glyph in (
        (panel.options_button, "Options", "IMAGE_settings"),
        (panel.libraries_button, "Libraries...", "BUTTONS_folder"),
    ):
        assert button.toolButtonStyle() == QtCore.Qt.ToolButtonIconOnly
        assert button.text() == ""  # the name lives in the tooltip and the accessible name
        assert button.accessibleName() == name and button.toolTip().startswith(name)
        assert button.property("cycles_glyph") == glyph
    assert "add, remove, rename or disable" in panel.libraries_button.toolTip()
    assert panel.options_button.menu() is not None
    panel.dispose()


def test_header_groups_have_no_divider_lines(app, tmp_path):
    # Groups are separated by the row spacing only; no 1 px divider widgets or empty groups.
    panel, backend = material_panel(tmp_path)
    assert not [w for w in panel.header.findChildren(QtWidgets.QWidget) if w.property("cycles_separator")]
    for group, *_ in panel.header.groups:
        assert group.layout().count() > 0
    panel.dispose()


def _object_panel(tmp_path, shared):
    panel, backend = material_panel(tmp_path)
    calls = []

    def shared_scenes(snapshot):
        calls.append(snapshot)
        return shared

    backend.shared_scenes = shared_scenes
    panel.settings["import_mode"] = "append"
    return panel, backend, calls


def test_convert_and_append_asks_before_replacing_files_of_other_scenes(app, tmp_path):
    shared = [{"kind": "collection", "name": "Crate", "asset": "Crate", "scenes": ["E:/shots/shot_v01.hip"]}]
    panel, backend, calls = _object_panel(tmp_path, shared)
    record = sample_records()[3]
    questions = []

    # Cancel: nothing starts in the background, nothing is imported.
    panel.ask_replace_shared = lambda question: questions.append(question) or False
    assert panel.start_append(record) is False
    assert questions == ['"Crate" is also used by: shot_v01.hip. Replacing it changes that scene too.']
    assert "cancelled" in panel.status.text() and not panel._material_tasks and not backend.imports
    assert calls and calls[-1]["name"] == record["name"]  # asked with the host snapshot

    # Replace: the background prepare starts with on_shared="replace".
    panel.ask_replace_shared = lambda question: questions.append(question) or True
    assert panel.start_append(record) is True
    assert drain(app, panel, lambda: backend.imports)
    name, options = backend.imports[-1]
    assert name == record["name"] and options["prepared"] == {"items": [record["name"]]}
    assert calls[-1]["on_shared"] == "replace" and len(questions) == 2
    panel.dispose()


def test_convert_and_append_without_shared_scenes_does_not_ask(app, tmp_path):
    panel, backend, calls = _object_panel(tmp_path, [])
    panel.ask_replace_shared = lambda question: pytest.fail("asked without shared scenes")
    assert panel.start_append(sample_records()[3]) is True
    assert drain(app, panel, lambda: backend.imports)
    assert "on_shared" not in calls[-1]
    panel.dispose()


def test_import_report_lines_reach_the_status(app, tmp_path):
    from cycles_houdini.blender.blend_convert import TEXTURES_IN_PLACE

    panel, backend = material_panel(tmp_path)
    in_place = TEXTURES_IN_PLACE.format(count=2)

    def import_asset(record, **options):
        backend.imports.append((record["name"], options))
        options["report"]("bark.jpg not found", True)
        options["report"](in_place, False)
        return [SimpleNamespace(path=lambda: "/mat/" + record["name"])]

    backend.import_asset = import_asset
    backend.prepare_errors = {}
    panel.settings["import_mode"] = "append"
    steel = sample_records()[0]
    assert panel.import_record(steel) is None
    assert drain(app, panel, lambda: backend.imports)
    text = panel.status.text()
    assert text.startswith("Imported " + steel["name"]) and "bark.jpg not found" in text and in_place in text
    assert panel.status.property("cycles_warning") is True  # a warning, not a failure: the node exists
    assert "Import failed" not in text and in_place in panel.status.toolTip()

    # Reload: warnings from Reload after Collect are shown too.
    backend.linked = {"path": "/mat/steel", "stale": True}
    backend.rebuild_report = [("/mat/steel/tex0: library texture not found; the collected copy is kept: x", True)]
    assert panel.reload_linked(steel)
    assert drain(app, panel, lambda: backend.rebuilt)
    assert "Reloaded" in panel.status.text() and "collected copy is kept" in panel.status.text()
    panel.dispose()


def test_convert_and_append_reports_a_snapshot_notice_once(app, tmp_path):
    panel, backend, _ = _object_panel(tmp_path, [])
    notice = "The scene is not saved yet: Blender assets go to the shared cache C:/cache/assets_blender"
    backend.append_snapshot = lambda record, **options: {"notices": [notice], "name": record["name"]}

    def import_asset(record, **options):
        backend.imports.append((record["name"], options))
        options["report"](notice, False)  # the converter reports the snapshot's notices again
        options["report"]("Textures stay in place: 1 texture file(s) are read from where the .blend points.", False)
        return [SimpleNamespace(path=lambda: "/obj/" + record["name"])]

    backend.import_asset = import_asset
    assert panel.start_append(sample_records()[3])
    assert drain(app, panel, lambda: backend.imports)
    text = panel.status.text()
    assert text.count(notice) == 1 and "Textures stay in place" in text
    assert panel.status.property("cycles_warning") is not True
    panel.dispose()


# ---------------------------------------------------------------- worlds (blend-import.md: Worlds)


def test_world_import_runs_in_the_background_and_asks_about_existing_environment(app, tmp_path):
    panel, backend = material_panel(tmp_path)
    sky = sample_records()[6]
    questions = []
    panel.ask_existing_environment = lambda names: pytest.fail("asked with nothing to replace")
    assert panel.import_record(sky) is None  # nothing in the scene: no question
    assert drain(app, panel, lambda: backend.imports)
    name, options = backend.imports[-1]
    assert (name, options["mode"], options["existing_environment"]) == ("Sky", "link", "replace")
    assert options["prepared"]["name"] == "Sky" and backend.world_snapshots[-1][0] == "Sky"
    assert backend.environment_modes == ["link"] and "Imported Sky" in panel.status.text()

    # Cancel: nothing starts and nothing is imported.
    backend.environment = ["/obj/envlight1"]
    panel.ask_existing_environment = lambda names: questions.append(names) or None
    assert panel.start_world(sky) is False
    assert questions == [["/obj/envlight1"]] and len(backend.imports) == 1 and not panel._material_tasks
    assert "cancelled" in panel.status.text()

    # Keep existing (Convert & Append): the choice reaches the build.
    panel.ask_existing_environment = lambda names: "keep"
    panel.settings["import_mode"] = "append"
    assert panel.import_record(sky) is None
    assert drain(app, panel, lambda: len(backend.imports) == 2)
    assert (backend.imports[-1][1]["mode"], backend.imports[-1][1]["existing_environment"]) == ("append", "keep")
    panel.dispose()


def test_linked_world_is_reused_at_once_and_reloads(app, tmp_path):
    panel, backend = material_panel(tmp_path)
    sky = sample_records()[6]
    backend.linked_world = SimpleNamespace(path=lambda: "/obj/Sky")
    backend.environment = ["/obj/other"]
    panel.ask_existing_environment = lambda names: "replace"
    snapshots = len(backend.world_snapshots)
    nodes = panel.import_record(sky)
    assert nodes and len(backend.world_snapshots) == snapshots  # no export
    assert backend.imports[-1][1]["mode"] == "link" and backend.imports[-1][1]["existing_environment"] == "replace"
    assert "already linked" in panel.status.text()
    backend.linked = {"path": "/obj/Sky", "stale": True}
    assert panel.select_key(sky["ch"]["key"])
    panel._selection_changed()
    assert panel.details.reload_button.isVisibleTo(panel)
    assert not panel.details.assign_button.isVisibleTo(panel)  # never offered for worlds
    assert panel.reload_linked(sky)
    assert drain(app, panel, lambda: backend.rebuilt)
    assert backend.rebuilt[0][0] == "/obj/Sky" and "Reloaded Sky" in panel.status.text()
    panel.dispose()


def test_world_content_failure_marks_the_asset(app, tmp_path):
    panel, backend = material_panel(tmp_path)
    sky = sample_records()[6]
    backend.prepare_errors = {"Sky": ContentError("World 'Sky' cannot be imported: no World Output")}
    assert panel.start_world(sky) is True
    assert drain(app, panel, lambda: not panel._material_tasks)
    cache = backend.providers[-1].cache
    assert cache.importability[sky["ch"]["key"]][0] == "no" and "no World Output" in panel.status.text()
    assert not backend.imports
    panel.dispose()
