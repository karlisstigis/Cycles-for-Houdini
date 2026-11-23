# SPDX-License-Identifier: Apache-2.0
"""Asset library registry: tolerant load, atomic save, Blender preference merge."""

import json
import os

import pytest

from cycles_houdini import assets
from cycles_houdini.assets import libraries
from cycles_houdini.assets.libraries import Library

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


def test_registry_path_resolution(monkeypatch, tmp_path):
    monkeypatch.delenv("CYCLES_HOUDINI_ASSET_LIBRARIES_FILE", raising=False)
    monkeypatch.setenv("HOUDINI_USER_PREF_DIR", str(tmp_path / "prefs"))
    assert libraries.registry_path() == tmp_path / "prefs/cycles_houdini/asset_libraries.json"
    monkeypatch.setenv("HOUDINI_USER_PREF_DIR", "C:/x/houdini__HVER__")
    assert libraries.registry_path().parts[-2:] == (".cycles-houdini", "asset_libraries.json")
    monkeypatch.setenv("CYCLES_HOUDINI_ASSET_LIBRARIES_FILE", str(tmp_path / "r.json"))
    assert libraries.registry_path() == tmp_path / "r.json"


def test_round_trip_and_defaults(tmp_path):
    path = tmp_path / "sub/libs.json"
    libs = [Library("A", str(tmp_path / "a")), Library("B", str(tmp_path / "b"), False, "LINK", "listing")]
    libraries.save_libraries(libs, path)
    assert json.loads(path.read_text())["version"] == 1
    assert libraries.load_libraries(path) == libs
    assert [p.name for p in path.parent.iterdir()] == ["libs.json"]  # no temp files left


def test_tolerant_entries(tmp_path):
    path = tmp_path / "libs.json"
    entries = [
        {"path": str(tmp_path / "lib one"), "enabled": "yes", "import_method": "bogus", "provider": "nope"},
        {"name": "dup", "path": str(tmp_path / "lib one")},
        {"name": "no path"},
        "junk",
    ]
    path.write_text(json.dumps({"version": 1, "libraries": entries}))
    (library,) = libraries.load_libraries(path)
    assert library == Library("lib one", str(tmp_path / "lib one"))


def test_missing_and_corrupt_registry(tmp_path):
    path = tmp_path / "libs.json"
    assert libraries.read_registry(path) == ([], "")
    path.write_text("{not json")
    libs, error = libraries.read_registry(path)
    assert libs == [] and "Cannot read" in error
    assert libraries.load_libraries(path) == []
    assert path.read_text() == "{not json"  # loading never rewrites
    libraries.save_libraries([Library("A", "/a")], path)
    backups = [p for p in tmp_path.iterdir() if ".corrupt-" in p.name]
    assert len(backups) == 1 and backups[0].read_text() == "{not json"
    assert libraries.load_libraries(path)[0].name == "A"


def test_unknown_version_is_an_error(tmp_path):
    path = tmp_path / "libs.json"
    path.write_text(json.dumps({"version": 9, "libraries": []}))
    assert "version" in libraries.read_registry(path)[1]


def test_merge_blender_libraries_dedupes_by_path(tmp_path):
    a = str(tmp_path / "A")
    existing = [Library("Mine", a, enabled=False, provider="listing")]
    dumped = [
        {"name": "Blender A", "path": os.path.join(a, "."), "import_method": "LINK", "enabled": True},
        {"name": "Mine", "path": str(tmp_path / "B"), "import_method": "APPEND_REUSE", "enabled": True},
        {"name": "", "path": ""},
    ]
    merged = libraries.merge_blender_libraries(existing, dumped)
    assert merged[0] == existing[0]
    assert len(merged) == 2
    assert merged[1].name == "Mine (2)" and merged[1].import_method == "APPEND_REUSE"


@pytest.mark.parametrize(
    "value,expected", [(None, True), ("1", True), ("0", False), ("Off", False), ("disabled", False)]
)
def test_enabled(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("CYCLES_HOUDINI_ASSET_BROWSER", raising=False)
    else:
        monkeypatch.setenv("CYCLES_HOUDINI_ASSET_BROWSER", value)
    assert assets.enabled() is expected
