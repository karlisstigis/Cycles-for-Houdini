# SPDX-License-Identifier: Apache-2.0
"""Reload from Library after Collect Textures, through importers.rebuild_linked (blend-import.md: Collect and relink).

All three rows of the table: original unchanged (copy kept), changed (re-collected),
missing (copy kept, warning). Uses the fake `hou` of test_assets_collect.
"""

import json
from pathlib import Path

import pytest
import test_assets_collect as collect_cases
from test_assets_collect import fake, write  # noqa: F401  (fixture)

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


@pytest.fixture
def scene(fake, monkeypatch):  # noqa: F811
    node = collect_cases.Node

    def destroy(self):
        self.destroyed = True
        self._parent.children_.remove(self)

    def set_name(self, name, unique_name=False):
        self._name = name

    monkeypatch.setattr(node, "destroy", destroy, raising=False)
    monkeypatch.setattr(node, "setName", set_name, raising=False)
    monkeypatch.setattr(node, "position", lambda self: getattr(self, "pos", (0.0, 0.0)), raising=False)
    monkeypatch.setattr(node, "setPosition", lambda self, p: setattr(self, "pos", tuple(p)), raising=False)
    monkeypatch.setattr(node, "setComment", lambda self, text: None, raising=False)
    return fake


def test_reload_keeps_collected_copies_for_all_three_rows(scene, monkeypatch):
    from cycles_houdini.assets import importers
    from cycles_houdini.assets.collect import collect_textures
    from cycles_houdini.blender import blend_library_materials
    from cycles_houdini.blender.blend_library_materials import LINK_USER_DATA, link_info

    same = write(scene.lib / "same.jpg", b"same")
    changed = write(scene.lib / "changed.jpg", b"before")
    gone = write(scene.lib / "gone.jpg", b"gone")
    old = collect_cases.material(scene, "steel", [same, changed, gone], linked=True)
    # A link written by an earlier build: its texture_root and library are read without error.
    info = dict(link_info(old), texture_root="$HIP/tex/assets_blender/Lib")
    old.setUserData(LINK_USER_DATA, json.dumps(info))
    collect_textures([old], show=False)
    tex = scene.hip / "assets_blender/metals/tex"
    collected = {i: f"$HIP/assets_blender/metals/tex/{p.name}" for i, p in enumerate((same, changed, gone))}
    assert {i: scene.hou.node(f"/mat/steel/tex{i}").parm("filename").value for i in range(3)} == collected

    changed.write_bytes(b"after, longer")  # row 2: the library file changed
    gone.unlink()  # row 3: the library file is missing

    def build(prepared, *, parent=None, diagnostics=None):
        # The rebuilt material points at the library again, as the exporter writes it.
        new = collect_cases.Node(scene.hou, parent, "steel_1", "cycles::material::1", scene.cats["vop"], {})
        for index, path in enumerate((same, changed, gone)):
            collect_cases.Node(
                scene.hou, new, f"tex{index}", "cycles::image_texture::1", scene.cats["vop"], {"filename": str(path)}
            )
        scene.hou.child_categories[new.path()] = scene.cats["vop"]
        diagnostics.append({"code": "material.missing_texture", "path": str(gone), "message": "gone.jpg not found"})
        return new

    monkeypatch.setattr(blend_library_materials, "build_library_material", build)
    lines = []
    [new] = importers.rebuild_linked(
        [("/mat/steel", {"name": "steel", "record": {}})], lambda text, warning: lines.append((text, warning))
    )
    assert new.path() == "/mat/steel" and old.destroyed
    for index in range(3):
        parm = scene.hou.node(f"/mat/steel/tex{index}").parm("filename")
        assert parm.value == collected[index] and parm.locked  # collected path kept, link still locked
    assert (tex / "same.jpg").read_bytes() == b"same"  # row 1: unchanged original, copy kept
    assert (tex / "changed.jpg").read_bytes() == b"after, longer"  # row 2: re-collected
    assert (tex / "gone.jpg").read_bytes() == b"gone"  # row 3: copy kept ...
    assert lines[0] == ("gone.jpg not found", True)
    assert len(lines) == 2 and lines[1][1] is True and "library texture not found" in lines[1][0]  # ... and warned
    kept = link_info(new)
    assert kept["source"] == info["source"] and "texture_root" not in kept and "library" not in kept
    stored = json.loads((scene.hip / "assets_blender/metals/cycles_import.json").read_text(encoding="utf-8"))
    record = next(image for image in stored["images"].values() if image.get("file") == "changed.jpg")
    assert record["sha256"] == __import__("hashlib").sha256(b"after, longer").hexdigest()


def test_reload_without_collected_paths_reports_nothing(scene, monkeypatch):
    from cycles_houdini.assets import importers
    from cycles_houdini.blender import blend_library_materials

    wood = write(scene.lib / "wood.jpg", b"wood")
    collect_cases.material(scene, "steel", [wood], linked=True)

    def build(prepared, *, parent=None, diagnostics=None):
        new = collect_cases.Node(scene.hou, parent, "steel_1", "cycles::material::1", scene.cats["vop"], {})
        collect_cases.Node(
            scene.hou, new, "tex0", "cycles::image_texture::1", scene.cats["vop"], {"filename": str(wood)}
        )
        return new

    monkeypatch.setattr(blend_library_materials, "build_library_material", build)
    lines = []
    importers.rebuild_linked([("/mat/steel", {"name": "steel"})], lambda *a: lines.append(a))
    assert scene.hou.node("/mat/steel/tex0").parm("filename").value == str(wood) and lines == []
    assert Path(scene.hip / "assets_blender").exists() is False
