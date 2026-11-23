# SPDX-License-Identifier: Apache-2.0
"""Asset browser importers after the textures-in-place switch (blend-import.md: Import record, Textures in place,
Importer dialog).

The import report (missing textures as warnings, the Collect Textures line), the UI names,
settings of earlier builds and the shared-scene question.
"""

import json

import pytest
from test_assets_panel_importers import fake, make  # noqa: F401  (fixture)

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


# ------------------------------------------------------------------ import report


def _in_place(path, missing=False):
    return {"filename": str(path), "packed": False, "missing": missing, "tiles": [], "dependencies": []}


def test_material_report_warns_missing_textures_and_names_collect(fake, monkeypatch):  # noqa: F811
    from cycles_houdini.assets import importers
    from cycles_houdini.blender import blend_convert, blend_library_materials

    def build(prepared, *, parent=None, diagnostics=None):
        diagnostics.append({"code": "material.missing_texture", "path": "/lib/gone.png", "message": "gone.png missing"})
        diagnostics.append({"code": "material.other", "message": "not a texture warning"})
        return fake.mat.createNode("cycles_material", prepared["name"])

    monkeypatch.setattr(blend_library_materials, "build_library_material", build)
    shared = _in_place("/lib/bark.jpg")
    record = {
        "key": "k",
        "nodes": [
            {"id": "a", "image": shared},
            {"id": "b", "image": shared},  # one image dict shared by two nodes counts once
            {"id": "c", "image": _in_place("/lib/gone.png", missing=True)},
            {"id": "d", "image": {"filename": "packed.png", "packed": True}},  # extracted to tex/: not in place
        ],
    }
    prepared = {"name": "Steel", "record": record, "asset_root_text": "$HIP/assets_blender"}
    lines = []
    nodes = importers.import_asset(
        make(fake, "Steel", "MATERIAL"),
        mode="append",
        prepared=prepared,
        report=lambda text, warning=False: lines.append((text, warning)),
    )
    assert [node.path() for node in nodes] == ["/mat/Steel"]  # a missing texture never fails the import
    assert lines == [
        ("gone.png missing", True),
        (blend_convert.TEXTURES_IN_PLACE.format(count=2), False),
    ]
    assert "Cycles > Collect Textures copies them into the project" in lines[1][0]
    assert lines[1][0].startswith("Textures stay in place: 2 texture file(s)")
    # Nothing in place, nothing missing: no report lines.
    assert importers.material_report({"record": {"nodes": []}}, []) == []


def test_untitled_notice_is_reported_by_the_synchronous_material_import(fake, monkeypatch, tmp_path):  # noqa: F811
    from cycles_houdini.assets import importers
    from cycles_houdini.blender import blend_library_materials

    monkeypatch.setenv("CYCLES_HOUDINI_ASSET_CACHE", str(tmp_path / "cache"))
    fake.hou.scene.new = True
    seen = {}

    def prepare(snapshot, cancelled=None, timeout=300):
        seen.update(snapshot)
        return {"name": snapshot["name"], "record": {}, "notice": snapshot["notice"]}

    monkeypatch.setattr(importers, "prepare_material", prepare)
    monkeypatch.setattr(
        blend_library_materials,
        "build_library_material",
        lambda prepared, *, parent=None, diagnostics=None: fake.mat.createNode("cycles_material", prepared["name"]),
    )
    lines = []
    importers.import_asset(make(fake, "Steel", "MATERIAL"), mode="append", report=lambda *a: lines.append(a))
    cache = (tmp_path / "cache/assets_blender").as_posix()
    assert seen["asset_root_text"] == cache
    assert lines == [("The scene is not saved yet: Blender assets go to the shared cache " + cache, False)]


# ------------------------------------------------------------------ UI names and settings


def test_ui_names_follow_the_plan_table():
    from cycles_houdini.assets import importers, settings

    assert importers.IMPORT_MODES == {"link": "Link (reference)", "append": "Convert & Append"}
    assert "Convert & Append" in importers.APPEND_OBJECTS_TOOLTIP
    assert settings.SAVE_LOCATION_LABELS == {"files": "Save to Disk as .bgeo.sc", "embedded": "Stash in .hip"}
    assert settings.DEFAULTS["append_storage"] == "files"  # Save to Disk is the default
    assert "texture_location" not in settings.DEFAULTS and not hasattr(settings, "TEXTURE_LOCATIONS")


def test_settings_of_earlier_builds_load_and_drop_texture_location(tmp_path):
    from cycles_houdini.assets import settings

    path = tmp_path / "asset_browser_settings.json"
    old = {
        "version": 1,
        "settings": {"import_mode": "append", "texture_location": "cache", "append_storage": "embedded"},
    }
    path.write_text(json.dumps(old), encoding="utf-8")
    loaded = settings.load_settings(path)
    assert loaded["import_mode"] == "append" and loaded["append_storage"] == "embedded"
    assert "texture_location" not in loaded
    settings.save_settings(loaded, path)
    assert "texture_location" not in json.loads(path.read_text(encoding="utf-8"))["settings"]
    assert settings.normalize({"texture_location": "bogus"}) == settings.DEFAULTS


# ------------------------------------------------------------------ shared scenes (blend-import.md: Import record)


def test_shared_question_and_append_shared_scenes(monkeypatch):
    from cycles_houdini.assets import importers
    from cycles_houdini.blender import blend_convert

    shared = [
        {"kind": "collection", "name": "Tree", "asset": "Tree", "scenes": ["E:/shots/shot_v01.hip"]},
        {"kind": "object", "name": "Rock", "asset": "Rock_2", "scenes": ["E:/shots/a.hip", "C:\\work\\b.hip"]},
    ]
    assert importers.shared_question(shared) == (
        '"Tree" is also used by: shot_v01.hip. Replacing it changes that scene too.\n'
        '"Rock_2" is also used by: a.hip, b.hip. Replacing it changes that scene too.'
    )
    calls = []
    monkeypatch.setattr(blend_convert, "shared_scenes", lambda snapshot: calls.append(snapshot) or shared)
    assert importers.append_shared_scenes({"storage": "embedded"}) == []  # Stash in .hip replaces no files
    assert calls == []
    assert importers.append_shared_scenes({"storage": "files", "source": "x"}) == shared
    assert calls == [{"storage": "files", "source": "x"}]
