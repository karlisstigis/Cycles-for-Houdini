# SPDX-License-Identifier: Apache-2.0
"""Asset browser panel against the real core modules (registry, cache, providers, indexer).

Offscreen Qt, no hou, no Blender: the listing provider reads the checked-in
Blender-generated listing fixture; the worker provider runs with Blender
missing and must keep the panel usable.
"""

import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.pure, pytest.mark.cpu]

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/asset_browser/listing_v1"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "hou", None)
    monkeypatch.setenv("CYCLES_HOUDINI_ASSET_LIBRARIES_FILE", str(tmp_path / "prefs/asset_libraries.json"))
    monkeypatch.setenv("CYCLES_HOUDINI_ASSET_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("CYCLES_HOUDINI_BLENDER", str(tmp_path / "no-blender/blender.exe"))
    monkeypatch.delenv("CYCLES_HOUDINI_ASSET_BROWSER_SETTINGS_FILE", raising=False)
    monkeypatch.delenv("CYCLES_HOUDINI_ASSET_BROWSER", raising=False)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return app, tmp_path


def wait(app, panel, predicate, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_listing_library_through_real_core(env):
    app, tmp_path = env
    from cycles_houdini.assets import libraries
    from cycles_houdini.assets.panel import AssetBrowserPanel

    root = tmp_path / "Lib One"
    shutil.copytree(FIXTURE, root)
    libraries.save_libraries([libraries.Library(name="Lib One", path=str(root), provider="listing")])
    panel = AssetBrowserPanel()
    panel.show()
    assert panel.library_combo.currentText() == "Lib One"
    assert panel.provider_combo.currentData() == "listing"
    assert wait(app, panel, lambda: panel._job is None and panel.model.rowCount() == 2)
    names = sorted(r["name"] for r in panel.model.records())
    assert names == ["Metal - Galvanized", "Tri Prop"]
    everything = panel.catalogs.topLevelItem(0)
    assert everything.text(0) == "All" and everything.childCount() >= 1
    panel.catalogs.select_path("Materials/Metal")
    assert [panel.proxy.index(r, 0).data() for r in range(panel.proxy.rowCount())] == ["Metal - Galvanized"]
    panel.catalogs.select_path("Materials")
    assert panel.proxy.rowCount() >= 1  # parent includes its children
    panel.catalogs.select_path(panel.catalogs.ALL)
    panel.search.setText("SHINY metal")
    assert panel.proxy.rowCount() == 1
    assert panel.select_key(panel.proxy.record(0)["ch"]["key"])
    assert panel.details.labels["file"].text() == "materials/metal.blend"
    assert panel.details.labels["license"].text() == "CC0"
    # Settings land beside the registry.
    panel.dispose()
    settings = json.loads((tmp_path / "prefs/asset_browser_settings.json").read_text())
    assert settings["settings"]["last_library"] == "Lib One"


def test_worker_library_with_blender_missing(env):
    app, tmp_path = env
    from cycles_houdini.assets import libraries
    from cycles_houdini.assets.panel import BLENDER_MISSING, AssetBrowserPanel

    root = tmp_path / "blends"
    root.mkdir()
    (root / "a.blend").write_bytes(b"BLENDER-v405")
    libraries.save_libraries([libraries.Library(name="Blends", path=str(root))])
    panel = AssetBrowserPanel()
    assert wait(app, panel, lambda: panel._job is None)
    assert panel.banner.text() == BLENDER_MISSING and not panel.banner.isHidden()
    assert panel.center.currentWidget() is panel.empty
    assert "Blender" in panel.empty_label.text()
    # Switching provider is stored in the registry.
    panel.provider_combo.setCurrentIndex(panel.provider_combo.findData("listing"))
    assert libraries.load_libraries()[0].provider == "listing"
    assert wait(app, panel, lambda: panel._job is None)
    panel.dispose()


def test_empty_registry_real_core(env):
    app, tmp_path = env
    from cycles_houdini.assets.panel import NO_LIBRARIES, AssetBrowserPanel

    panel = AssetBrowserPanel()
    assert panel.empty_label.text() == NO_LIBRARIES and panel.center.currentWidget() is panel.empty
    panel.add_library_folder(str(tmp_path / "New Lib"))
    assert panel.library_combo.currentText() == "New Lib"
    assert wait(app, panel, lambda: panel._job is None)
    panel.dispose()
