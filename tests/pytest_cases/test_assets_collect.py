# SPDX-License-Identifier: Apache-2.0
"""Collect Textures and Reload after a collect (blend-import.md: Collect and relink), plus the hou layer of
Collect, Relink and Move into Project against a small fake `hou`."""

import contextlib
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


def entry(node, path, owner=None, parm="filename", raw=None):
    from cycles_houdini.assets.scene_paths import PathEntry

    return PathEntry(node, parm, raw or str(path), str(path), owner)


def blender(path="/mat/steel", source=None):
    from cycles_houdini.assets.scene_paths import Owner

    return Owner(path, path.rsplit("/", 1)[-1], True, None if source is None else str(source))


def plain(path="/mat/steel"):
    from cycles_houdini.assets.scene_paths import Owner

    return Owner(path, path.rsplit("/", 1)[-1])


@pytest.fixture
def scene(tmp_path):
    hip, lib = tmp_path / "shot", tmp_path / "lib"
    hip.mkdir()
    lib.mkdir()
    return SimpleNamespace(hip=hip, lib=lib, blend=lib / "metals.blend", tmp=tmp_path)


def write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def collect(entries, hip):
    from cycles_houdini.assets.collect import collect_entries

    return collect_entries(entries, str(hip))


def names(folder):
    return sorted(p.name for p in Path(folder).iterdir() if p.name != "cycles_import.json")


# ------------------------------------------------------------------ destination


def test_destination_blend_other_and_unknown(scene):
    from cycles_houdini.assets.collect import destination

    hip = scene.hip
    dest = destination(blender(source=scene.blend), hip)
    assert (
        dest.folder == hip / "assets_blender" / "metals" / "tex" and dest.blend_dir == hip / "assets_blender" / "metals"
    )
    dest = destination(plain("/mat/My Steel$1"), hip)
    assert dest.folder == hip / "tex" / "My Steel_1" and dest.blend_dir is None
    dest = destination(blender(source=None), hip)
    assert dest.folder == hip / "assets_blender" / "_unknown_source" / "tex" and dest.unknown_source


def test_two_blends_with_one_stem_get_two_folders_in_one_run(scene):
    a = write(scene.lib / "a" / "metals.blend", b"B")
    b = write(scene.lib / "b" / "metals.blend", b"B")
    wood = write(scene.lib / "wood.jpg", b"wood")
    result = collect(
        [entry("/mat/x/t", wood, blender("/mat/x", a)), entry("/mat/y/t", wood, blender("/mat/y", b))], scene.hip
    )
    texts = sorted(text for _, _, text in result.rewrites)
    assert texts == ["$HIP/assets_blender/metals/tex/wood.jpg", "$HIP/assets_blender/metals__b/tex/wood.jpg"]
    assert len(result.report.copied) == 2  # one copy per .blend


# ------------------------------------------------------------------ one copy per texture


def test_two_materials_sharing_a_texture_together_and_in_separate_runs(scene):
    wood = write(scene.lib / "wood.jpg", b"wood")
    first = [entry("/mat/a/t", wood, blender("/mat/a", scene.blend))]
    second = [entry("/mat/b/t", wood, blender("/mat/b", scene.blend))]
    together = collect(first + second, scene.hip)
    assert [r[2] for r in together.rewrites] == ["$HIP/assets_blender/metals/tex/wood.jpg"] * 2
    assert len(together.report.copied) == 1 and together.report.bytes == 4
    again = collect(second, scene.hip)  # the unselected one collected later lands on the copy
    assert again.report.copied == [] and len(again.report.reused) == 1
    assert names(scene.hip / "assets_blender/metals/tex") == ["wood.jpg"]


def test_same_name_different_content_gets_suffix_and_same_content_is_reused(scene):
    one = write(scene.lib / "one" / "wood.jpg", b"wood one")
    two = write(scene.lib / "two" / "wood.jpg", b"wood two")
    same = write(scene.lib / "three" / "wood.jpg", b"wood one")
    owner = blender("/mat/a", scene.blend)
    result = collect(
        [entry("/mat/a/t1", one, owner), entry("/mat/a/t2", two, owner), entry("/mat/a/t3", same, owner)], scene.hip
    )
    assert [r[2].rsplit("/", 1)[1] for r in result.rewrites] == ["wood.jpg", "wood_2.jpg", "wood.jpg"]
    tex = scene.hip / "assets_blender/metals/tex"
    assert (tex / "wood_2.jpg").read_bytes() == b"wood two" and len(result.report.reused) == 1
    record = json.loads((scene.hip / "assets_blender/metals/cycles_import.json").read_text(encoding="utf-8"))
    by_original = {r["original"]: r for r in record["images"].values()}
    assert by_original[str(two).replace("\\", "/")]["file"] == "wood_2.jpg"
    assert by_original[str(same).replace("\\", "/")]["file"] == "wood.jpg"
    assert all(r["packed"] is False and len(r["sha256"]) == 64 for r in by_original.values())


def test_other_materials_go_to_hip_tex_and_never_overwrite(scene):
    wood = write(scene.lib / "wood.jpg", b"new wood")
    write(scene.hip / "tex" / "steel" / "wood.jpg", b"someone else's wood")
    result = collect([entry("/mat/steel/t", wood, plain("/mat/steel"))], scene.hip)
    assert result.rewrites == [("/mat/steel/t", "filename", "$HIP/tex/steel/wood_2.jpg")]
    assert (scene.hip / "tex/steel/wood.jpg").read_bytes() == b"someone else's wood"
    assert not (scene.hip / "assets_blender").exists()


def test_lost_import_record_goes_to_unknown_source(scene):
    wood = write(scene.lib / "wood.jpg", b"wood")
    result = collect([entry("/mat/lost/t", wood, blender("/mat/lost", None))], scene.hip)
    assert result.rewrites[0][2] == "$HIP/assets_blender/_unknown_source/tex/wood.jpg"
    assert result.report.unknown_source == ["/mat/lost"] and "_unknown_source" in result.report.text()


def test_udim_and_sequence_sets_copy_every_tile_and_keep_the_token(scene):
    for tile in (1001, 1002, 1011):
        write(scene.lib / f"wood.{tile}.exr", b"w%d" % tile)
    for frame in (1, 2, 3):
        write(scene.lib / f"fire.{frame:04d}.png", b"f%d" % frame)
    write(scene.lib / "mari_1001.tif", b"m")
    owner = blender("/mat/a", scene.blend)
    result = collect(
        [
            entry("/mat/a/u", scene.lib / "wood.<UDIM>.exr", owner),
            entry("/mat/a/f", scene.lib / "fire.$F4.png", owner),
            entry("/mat/a/m", scene.lib / "mari_%(UDIM)d.tif", owner),
        ],
        scene.hip,
    )
    prefix = "$HIP/assets_blender/metals/tex/"
    assert [r[2] for r in result.rewrites] == [
        prefix + "wood.<UDIM>.exr",
        prefix + "fire.$F4.png",
        prefix + "mari_%(UDIM)d.tif",
    ]
    tex = scene.hip / "assets_blender/metals/tex"
    assert names(tex) == [
        "fire.0001.png",
        "fire.0002.png",
        "fire.0003.png",
        "mari_1001.tif",
        "wood.1001.exr",
        "wood.1002.exr",
        "wood.1011.exr",
    ]
    assert len(result.report.copied) == 7


def test_udim_set_with_a_different_tile_gets_a_suffix_for_every_tile(scene):
    for tile in (1001, 1002):
        write(scene.lib / "a" / f"wood.{tile}.exr", b"a%d" % tile)
        write(scene.lib / "b" / f"wood.{tile}.exr", b"a%d" % tile if tile == 1001 else b"different")
    owner = blender("/mat/a", scene.blend)
    result = collect(
        [
            entry("/mat/a/1", scene.lib / "a/wood.<UDIM>.exr", owner),
            entry("/mat/a/2", scene.lib / "b/wood.<UDIM>.exr", owner),
        ],
        scene.hip,
    )
    assert [r[2].rsplit("/", 1)[1] for r in result.rewrites] == ["wood.<UDIM>.exr", "wood_2.<UDIM>.exr"]
    assert names(scene.hip / "assets_blender/metals/tex") == [
        "wood.1001.exr",
        "wood.1002.exr",
        "wood_2.1001.exr",
        "wood_2.1002.exr",
    ]


def test_in_project_skipped_missing_and_unsupported_reported(scene):
    inside = write(scene.hip / "tex" / "mine.jpg", b"x")
    owner = blender("/mat/a", scene.blend)
    result = collect(
        [
            entry("/mat/a/1", inside, owner),
            entry("/mat/a/2", scene.lib / "gone.jpg", owner),
            entry("/mat/a/3", scene.lib / "gone.<UDIM>.exr", owner),
            entry("/mat/a/4", scene.lib / "$F4" / "x.<UDIM>.exr", owner),
        ],
        scene.hip,
    )
    report = result.report
    assert result.rewrites == [] and report.skipped == [str(inside)]
    assert report.missing == [str(scene.lib / "gone.jpg"), str(scene.lib / "gone.<UDIM>.exr")]
    assert report.unsupported == [str(scene.lib / "$F4" / "x.<UDIM>.exr")]
    text = report.text()
    assert "Copied 0 file(s)" in text and "skipped 1" in text and "2 source file(s) not found" in text
    assert str(scene.lib / "gone.jpg") in text


def test_without_the_json_equal_copies_are_reused_and_unknown_blend_folders_left_alone(scene):
    wood = write(scene.lib / "wood.jpg", b"wood")
    owner = plain("/mat/a")
    collect([entry("/mat/a/t", wood, owner)], scene.hip)
    again = collect([entry("/mat/b/t", wood, owner)], scene.hip)  # $HIP/tex has no JSON: content decides
    assert again.report.copied == [] and again.rewrites[0][2] == "$HIP/tex/a/wood.jpg"
    # A blend folder whose JSON is gone belongs to an unknown .blend (blend_assets_layout rule):
    # it is never written; the next collect makes <stem>__<parent folder>.
    owner = blender("/mat/a", scene.blend)
    collect([entry("/mat/a/t", wood, owner)], scene.hip)
    (scene.hip / "assets_blender/metals/cycles_import.json").unlink()
    third = collect([entry("/mat/a/t", wood, owner)], scene.hip)
    assert third.rewrites[0][2] == "$HIP/assets_blender/metals__lib/tex/wood.jpg"
    assert names(scene.hip / "assets_blender/metals/tex") == ["wood.jpg"]


def test_recorded_copy_is_refreshed_when_its_original_changed(scene):
    wood = write(scene.lib / "wood.jpg", b"wood v1")
    owner = blender("/mat/a", scene.blend)
    collect([entry("/mat/a/t", wood, owner)], scene.hip)
    wood.write_bytes(b"wood v2 longer")
    again = collect([entry("/mat/a/t", wood, owner)], scene.hip)
    assert (scene.hip / "assets_blender/metals/tex/wood.jpg").read_bytes() == b"wood v2 longer"
    assert names(scene.hip / "assets_blender/metals/tex") == ["wood.jpg"] and len(again.report.copied) == 1


def test_report_text_counts_and_linked_note():
    from cycles_houdini.assets.collect import CollectReport

    report = CollectReport(copied=["a", "b"], reused=["c"], skipped=["d"], bytes=2048, rewritten=4, linked_assets=3)
    report.in_cache = ["/obj/geo/file1/file: /cache/assets_blender/x/geo/x/x.bgeo.sc"]
    text = report.text()
    assert (
        text.splitlines()[0]
        == "Copied 2 file(s) (2.0 KB), reused 1, skipped 1 already in the project; 4 parameter(s) rewritten."
    )
    assert (
        "3 linked assets still read their .blend at render time. Switch them to Converted to make them portable."
        in text
    )
    assert "1 path(s) still in the user cache" in text


# ------------------------------------------------------------------ reload after collect (5.2)


@pytest.mark.parametrize(
    "exists, current, recorded, action",
    [
        (True, "a", "a", "keep"),
        (True, "b", "a", "recollect"),
        (False, None, "a", "keep_missing"),
        (True, "a", None, "recollect"),
    ],
)
def test_after_reload_action_table(exists, current, recorded, action):
    from cycles_houdini.assets.collect import after_reload_action

    assert after_reload_action(exists, current, recorded) == action


def test_reapply_collected_keep_recollect_and_missing(scene):
    from cycles_houdini.assets import texture_files
    from cycles_houdini.assets.collect import ReloadItem, reapply_collected

    tex = scene.hip / "assets_blender/metals/tex"
    same = write(scene.lib / "same.jpg", b"same")
    changed = write(scene.lib / "changed.jpg", b"changed now")
    write(tex / "same.jpg", b"same")
    write(tex / "changed.jpg", b"changed before")
    write(tex / "gone.jpg", b"gone")
    recorded = {str(same): texture_files.file_sha256(same), str(changed): "old"}
    items = [
        ReloadItem("/mat/a/1", "filename", str(same), str(tex / "same.jpg"), "$HIP/assets_blender/metals/tex/same.jpg"),
        ReloadItem(
            "/mat/a/2", "filename", str(changed), str(tex / "changed.jpg"), "$HIP/assets_blender/metals/tex/changed.jpg"
        ),
        ReloadItem(
            "/mat/a/3",
            "filename",
            str(scene.lib / "gone.jpg"),
            str(tex / "gone.jpg"),
            "$HIP/assets_blender/metals/tex/gone.jpg",
        ),
    ]
    rewrites, warnings, refreshed = reapply_collected(items, record_sha=lambda original: recorded.get(original))
    assert [r[2] for r in rewrites] == [item.collected_text for item in items]  # every parm keeps its copy
    assert (tex / "changed.jpg").read_bytes() == b"changed now" and (tex / "same.jpg").read_bytes() == b"same"
    assert [r[1] for r in refreshed] == [str(changed).replace("\\", "/")]
    assert [(w["code"], w["node"]) for w in warnings] == [("material.library_texture_missing", "/mat/a/3")]
    assert (tex / "gone.jpg").read_bytes() == b"gone"


# ------------------------------------------------------------------ fake hou


class Parm:
    def __init__(self, node, name, value="", locked=False):
        self.node, self._name, self.value, self.locked = node, name, value, locked

    def name(self):
        return self._name

    def path(self):
        return self.node.path() + "/" + self._name

    def unexpandedString(self):
        return self.value

    def evalAsString(self):
        return self.node.hou.text.expandString(self.value)

    def isLocked(self):
        return self.locked

    def lock(self, on):
        self.node.hou.log.append(("lock", self.path(), on, self.node.hou.depth))
        self.locked = on

    def set(self, value):
        if self.locked:
            raise PermissionError("locked: " + self.path())
        self.node.hou.log.append(("set", self.path(), value, self.node.hou.depth))
        self.value = value


class NodeType:
    def __init__(self, hou, name, category):
        self.hou, self._name, self._category, self.nodes = hou, name, category, []

    def name(self):
        return self._name

    def category(self):
        return self._category

    def instances(self):
        return [n for n in self.nodes if not n.destroyed]


class Category:
    def __init__(self, name):
        self._name, self.types = name, {}

    def name(self):
        return self._name

    def nodeTypes(self):
        return dict(self.types)


class Node:
    def __init__(self, hou, parent, name, type_name, category, parms=None):
        self.hou, self._parent, self._name, self.destroyed = hou, parent, name, False
        self.children_, self.user = [], {}
        node_type = category.types.setdefault(type_name, NodeType(hou, type_name, category))
        node_type.nodes.append(self)
        self._type = node_type
        self._parms = {name: Parm(self, name, value) for name, value in (parms or {}).items()}
        if parent is not None:
            parent.children_.append(self)

    def path(self):
        return (self._parent.path().rstrip("/") if self._parent else "") + "/" + self._name

    def name(self):
        return self._name

    def parent(self):
        return self._parent

    def type(self):
        return self._type

    def childTypeCategory(self):
        return self.hou.child_categories.get(self.path())

    def children(self):
        return list(self.children_)

    def allSubChildren(self):
        return [d for c in self.children_ for d in [c, *c.allSubChildren()]]

    def parm(self, name):
        return self._parms.get(name)

    def parms(self):
        return list(self._parms.values())

    def userData(self, key):
        return self.user.get(key)

    def setUserData(self, key, value):
        self.user[key] = value


@pytest.fixture
def fake(monkeypatch, tmp_path):
    hou = ModuleType("hou")
    hou.log, hou.depth = [], 0
    cats = {kind: Category(kind) for kind in ("vop", "obj", "sop", "manager")}
    for kind in ("vop", "obj", "sop"):
        setattr(hou, kind + "NodeTypeCategory", lambda kind=kind: cats[kind])
    hip = tmp_path / "shot"
    hip.mkdir()
    (hip / "scene.hip").write_bytes(b"hip")
    cache = tmp_path / "cache"
    monkeypatch.setenv("CYCLES_HOUDINI_ASSET_CACHE", str(cache))
    hou.saved = True
    hou.hipFile = SimpleNamespace(isNewFile=lambda: not hou.saved, path=lambda: str(hip / "scene.hip"))
    hou.text = SimpleNamespace(expandString=lambda value: value.replace("$HIP", str(hip)).replace("$F4", "0001"))
    hou.getenv = lambda name, default=None: hou.env.get(name, default)
    hou.env = {}
    hou.isUIAvailable = lambda: False
    hou.child_categories = {}

    @contextlib.contextmanager
    def group(label):
        hou.log.append(("undo", label))
        hou.depth += 1
        try:
            yield
        finally:
            hou.depth -= 1

    hou.undos = SimpleNamespace(group=group)
    root = Node(hou, None, "", "root", cats["manager"])
    root._name = ""
    mat = Node(hou, root, "mat", "matnet", cats["manager"])
    obj = Node(hou, root, "obj", "objnet", cats["manager"])
    hou.child_categories.update({"/mat": cats["vop"], "/obj": cats["obj"]})

    def find(path):
        for node in [mat, obj, *mat.allSubChildren(), *obj.allSubChildren()]:
            if node.path() == path:
                return node
        return None

    hou.node = find
    hou.selected = []
    hou.selectedNodes = lambda: list(hou.selected)
    monkeypatch.setitem(sys.modules, "hou", hou)
    lib = tmp_path / "lib"
    lib.mkdir()
    return SimpleNamespace(
        hou=hou, mat=mat, obj=obj, cats=cats, hip=hip, cache=cache, lib=lib, blend=lib / "metals.blend"
    )


def material(fake, name, textures, *, linked=False):
    """A Cycles material in /mat with one image texture node per path."""
    from cycles_houdini.blender.blend_library_materials import mark_linked

    builder = Node(fake.hou, fake.mat, name, "cycles::material::1", fake.cats["vop"], {"surface": ""})
    for index, path in enumerate(textures):
        Node(fake.hou, builder, f"tex{index}", "cycles::image_texture::1", fake.cats["vop"], {"filename": str(path)})
    fake.hou.child_categories[builder.path()] = fake.cats["vop"]
    if linked:
        builder.setColor = lambda color: None
        builder.color = lambda: None
        builder.setComment = lambda text: None
        mark_linked(builder, {"source": str(fake.blend), "name": name, "library": "Lib", "library_root": str(fake.lib)})
    return builder


def test_collect_refuses_an_unsaved_scene(fake):
    from cycles_houdini.assets.collect import SAVE_FIRST, CollectError, collect_textures

    fake.hou.saved = False
    with pytest.raises(CollectError, match="Save the scene first"):
        collect_textures(None)
    assert SAVE_FIRST.startswith("Save the scene first")


def test_collect_textures_unlocks_linked_parms_for_the_write_in_one_undo_group(fake):
    from cycles_houdini.assets.collect import collect_textures
    from cycles_houdini.blender.blend_library_materials import LINK_USER_DATA, link_info

    wood = write(fake.lib / "wood.jpg", b"wood")
    hdri = write(fake.lib / "sky.exr", b"sky")
    linked = material(fake, "steel", [wood], linked=True)
    material(fake, "paint", [write(fake.lib / "paint.jpg", b"p")])
    light = Node(fake.hou, fake.obj, "envlight1", "envlight", fake.cats["obj"], {"env_map": str(hdri)})
    Node(fake.hou, fake.obj, "ref", "cycles::blender_reference", fake.cats["sop"], {"file": str(fake.blend)})
    cached = fake.cache / "assets_blender/x/geo/x/x.bgeo.sc"
    Node(fake.hou, fake.obj, "file1", "file", fake.cats["sop"], {"file": str(cached)})
    info = link_info(linked)
    fake.hou.log.clear()

    report = collect_textures(None, show=False)

    texture = fake.hou.node("/mat/steel/tex0").parm("filename")
    assert texture.value == "$HIP/assets_blender/metals/tex/wood.jpg" and texture.locked
    assert fake.hou.node("/mat/paint/tex0").parm("filename").value == "$HIP/tex/paint/paint.jpg"
    assert light.parm("env_map").value == "$HIP/tex/envlight1/sky.exr"
    assert link_info(linked) == info and linked.userData(LINK_USER_DATA)  # the link stays
    undo = [e for e in fake.hou.log if e[0] == "undo"]
    assert undo == [("undo", "Collect textures")]
    steps = [e[:3] for e in fake.hou.log if e[0] in ("lock", "set") and e[1] == texture.path()]
    assert steps == [
        ("lock", texture.path(), False),
        ("set", texture.path(), texture.value),
        ("lock", texture.path(), True),
    ]
    assert all(e[-1] == 1 for e in fake.hou.log if e[0] in ("lock", "set"))
    assert len(report.copied) == 3 and report.rewritten == 3 and report.linked_assets == 1
    assert report.in_cache == [f"/obj/file1/file: {cached}"]


def test_collect_selected_rewrites_only_the_selected_material(fake):
    from cycles_houdini.assets.collect import collect_textures

    wood = write(fake.lib / "wood.jpg", b"wood")
    first = material(fake, "first", [wood])
    material(fake, "second", [wood])
    collect_textures([first], show=False)
    assert fake.hou.node("/mat/first/tex0").parm("filename").value == "$HIP/tex/first/wood.jpg"
    assert fake.hou.node("/mat/second/tex0").parm("filename").value == str(wood)


def test_reapply_after_reload_restores_collected_paths_and_relocks(fake):
    from cycles_houdini.assets.collect import collect_textures, reapply_after_reload, texture_values

    wood = write(fake.lib / "wood.jpg", b"wood")
    gone = write(fake.lib / "gone.jpg", b"gone")
    old = material(fake, "steel", [wood, gone], linked=True)
    collect_textures([old], show=False)
    previous = texture_values(old)
    assert previous == {
        "/tex0/filename": "$HIP/assets_blender/metals/tex/wood.jpg",
        "/tex1/filename": "$HIP/assets_blender/metals/tex/gone.jpg",
    }
    gone.unlink()
    # Reload rebuilt the material from the library: paths point at the library again, locked.
    old._name = "steel_old"
    new = material(fake, "steel", [wood, gone], linked=True)
    warnings = reapply_after_reload(new, previous)
    for index in (0, 1):
        parm = fake.hou.node(f"/mat/steel/tex{index}").parm("filename")
        assert parm.value == previous[f"/tex{index}/filename"] and parm.locked
    assert [w["code"] for w in warnings] == ["material.library_texture_missing"]


def test_relink_menu_action_rewrites_found_paths(fake):
    from cycles_houdini.assets.relink import relink_blender_assets

    material(fake, "steel", [fake.lib / "old" / "wood.jpg", fake.lib / "old" / "bark.<UDIM>.exr"])
    moved = fake.lib.parent / "moved"
    write(moved / "deep" / "WOOD.jpg", b"w")
    write(moved / "bark.1001.exr", b"b")
    Node(
        fake.hou, fake.obj, "ref", "cycles::blender_reference", fake.cats["sop"], {"file": str(fake.lib / "old.blend")}
    )
    write(fake.hip / "assets_blender" / "old.blend", b"B")
    plan = relink_blender_assets(folder=str(moved))
    assert fake.hou.node("/mat/steel/tex0").parm("filename").value == (moved / "deep" / "WOOD.jpg").as_posix()
    assert fake.hou.node("/mat/steel/tex1").parm("filename").value == (moved / "bark.<UDIM>.exr").as_posix()
    assert len(plan.not_found) == 1 and plan.not_found[0].kind == "blend"
    assert [e for e in fake.hou.log if e[0] == "undo"] == [("undo", "Relink Blender assets")]


def test_move_imported_assets_into_project_rewrites_node_and_texture_paths(fake):
    from cycles_houdini.assets.move_project import move_imported_assets_into_project
    from cycles_houdini.blender import blend_assets_layout as layout

    blend_dir = fake.cache / "assets_blender" / "metals"
    write(blend_dir / "geo/tree/tree.bgeo.sc", b"geo")
    write(blend_dir / "tex/bark.jpg", b"bark")
    with layout.open_store(blend_dir) as store:
        store.set_source(str(fake.blend), None)
        store.save()
    Node(fake.hou, fake.obj, "file1", "file", fake.cats["sop"], {"file": str(blend_dir / "geo/tree/tree.bgeo.sc")})
    material(fake, "bark", [blend_dir / "tex/bark.jpg"])
    fake.hou.saved = False
    assert move_imported_assets_into_project(show=False) == (
        [],
        [("", "Save the scene first: the assets move into the folder of the .hip file.")],
    )
    fake.hou.saved = True
    results, failures = move_imported_assets_into_project(show=False)
    assert failures == [] and [r.merged for r in results] == [False]
    assert fake.hou.node("/obj/file1").parm("file").value == "$HIP/assets_blender/metals/geo/tree/tree.bgeo.sc"
    assert fake.hou.node("/mat/bark/tex0").parm("filename").value == "$HIP/assets_blender/metals/tex/bark.jpg"
    assert (fake.hip / "assets_blender/metals/geo/tree/tree.bgeo.sc").read_bytes() == b"geo"
    assert not blend_dir.exists()


def test_scan_reads_unexpanded_and_expanded_paths_and_skips_op_and_empty(fake):
    from cycles_houdini.assets import scene_paths

    material(fake, "steel", ["$HIP/tex/a.$F4.exr", "op:/img/comp1", ""])
    entries = scene_paths.all_texture_entries()
    assert [(e.node, e.raw, e.path) for e in entries] == [
        ("/mat/steel/tex0", "$HIP/tex/a.$F4.exr", str(fake.hip) + "/tex/a.$F4.exr")
    ]
    assert entries[0].owner.path == "/mat/steel" and not entries[0].owner.blender


def test_imported_worlds_collect_into_their_blend_folder(fake):
    """Environment Lights and World Materials imported from a Blender world (appended or linked)
    are Blender imports: their textures go to $HIP/assets_blender/<blend>/tex/."""
    from cycles_houdini.assets.collect import collect_textures
    from cycles_houdini.assets.scene_paths import WORLD_USER_DATA, owner_of
    from cycles_houdini.blender import blend_worlds
    from cycles_houdini.blender.blend_library_materials import mark_linked

    assert WORLD_USER_DATA == blend_worlds.WORLD_USER_DATA
    sky = write(fake.lib / "sky.blend", b"BLENDER")
    world = json.dumps({"source": sky.as_posix(), "name": "Sky", "route": "light"})
    hdri, clouds_hdri, linked_hdri = (write(fake.lib / name, name.encode()) for name in ("a.exr", "b.exr", "c.exr"))
    light = Node(fake.hou, fake.obj, "Sky", "envlight", fake.cats["obj"], {"env_map": str(hdri)})
    light.setUserData(WORLD_USER_DATA, world)
    clouds = Node(fake.hou, fake.mat, "Clouds", "cycles::world_material::1", fake.cats["vop"])
    clouds.setUserData(WORLD_USER_DATA, world.replace('"light"', '"material"'))
    texture = Node(
        fake.hou, clouds, "env", "cycles::environment_texture::1", fake.cats["vop"], {"filename": str(clouds_hdri)}
    )
    linked = Node(fake.hou, fake.obj, "Sky_linked", "envlight", fake.cats["obj"], {"env_map": str(linked_hdri)})
    linked.setColor = lambda color: None
    linked.color = lambda: None
    linked.setComment = lambda text: None
    linked.setUserData(WORLD_USER_DATA, world)
    mark_linked(linked, {"kind": "world", "route": "light", "source": str(sky), "name": "Sky"})
    other = Node(fake.hou, fake.obj, "envlight1", "envlight", fake.cats["obj"], {"env_map": str(hdri)})
    damaged = Node(fake.hou, fake.obj, "lost", "envlight", fake.cats["obj"], {"env_map": str(hdri)})
    damaged.setUserData(WORLD_USER_DATA, "{not json")
    assert owner_of(light).blender and owner_of(light).source == sky.as_posix()
    assert owner_of(texture).path == "/mat/Clouds" and owner_of(texture).blender
    assert not owner_of(other).blender
    assert owner_of(damaged).blender and owner_of(damaged).source is None  # lost record

    collect_textures([light, clouds, linked, other, damaged], show=False)

    assert light.parm("env_map").value == "$HIP/assets_blender/sky/tex/a.exr"
    assert texture.parm("filename").value == "$HIP/assets_blender/sky/tex/b.exr"
    assert linked.parm("env_map").value == "$HIP/assets_blender/sky/tex/c.exr" and linked.parm("env_map").locked
    assert other.parm("env_map").value == "$HIP/tex/envlight1/a.exr"
    assert damaged.parm("env_map").value == "$HIP/assets_blender/_unknown_source/tex/a.exr"


# ------------------------------------------------ Blender File nodes (blend-import.md: Collect and relink, Asset nodes)


def blender_file(fake, name, *, mode, source, converted=""):
    """A Cycles Blender File SOP in its own geo object with the inner reference and File SOP."""
    geo = Node(fake.hou, fake.obj, name, "geo", fake.cats["obj"])
    node = Node(fake.hou, geo, "blender_file", "cycles::blender_file", fake.cats["sop"], {"file": str(source)})
    node._parms["mode"] = Parm(node, "mode", mode)
    Node(fake.hou, node, "reference", "cycles::blender_reference", fake.cats["sop"], {"file": str(source)})
    Node(fake.hou, node, "converted_file", "file", fake.cats["sop"], {"file": converted})
    return node


def test_collect_report_counts_link_mode_blender_file_nodes(fake):
    """Collect report: Link-mode Blender File nodes and plain reference SOPs are counted once each (the
    reference inside a Blender File node is the node's own); Converted ones are portable."""
    from cycles_houdini.assets import scene_paths
    from cycles_houdini.assets.collect import LINKED_NOTE, collect_textures

    write(fake.blend, b"B")
    blender_file(fake, "linked", mode="link", source=fake.blend)
    blender_file(fake, "converted", mode="converted", source=fake.blend, converted="$HIP/x.bgeo.sc")
    old = Node(fake.hou, fake.obj, "old", "geo", fake.cats["obj"])
    Node(fake.hou, old, "ref", "cycles::blender_reference", fake.cats["sop"], {"file": str(fake.blend)})
    assert scene_paths.linked_asset_count() == 2
    report = collect_textures(show=False)
    assert report.linked_assets == 2
    assert LINKED_NOTE.format(count=2) in report.text()
    assert "Switch them to Converted to make them portable." in report.text()
    # The inner reference is not a .blend entry of its own.
    assert sorted(e.node for e in scene_paths.blend_entries()) == [
        "/obj/converted/blender_file",
        "/obj/linked/blender_file",
        "/obj/old/ref",
    ]


def test_relink_covers_blender_file_nodes_and_their_converted_file(fake, monkeypatch):
    """Relink: the .blend of both node types and the missing converted file of a Blender File
    node are relinked; the inner reference follows the node's new file (pushed after the write)."""
    from cycles_houdini.assets.relink import relink_blender_assets
    from cycles_houdini.blender import blend_file_node

    pushed = []
    monkeypatch.setattr(blend_file_node, "push_reference", lambda node: pushed.append(node.path()))
    gone = fake.lib / "old" / "props.blend"
    converted = fake.hip / "assets_blender" / "props" / "geo" / "Props" / "Props.bgeo.sc"
    node = blender_file(fake, "props", mode="converted", source=gone, converted="$HIP/old_geo/Props/Props.bgeo.sc")
    hierarchy = Node(fake.hou, fake.obj, "tree", "cycles::blender_hierarchy", fake.cats["obj"], {"file": str(gone)})
    moved = fake.lib.parent / "moved"
    write(moved / "props.blend", b"B")
    write(converted, b"geo")
    plan = relink_blender_assets(folder=str(moved))
    assert node.parm("file").value == (moved / "props.blend").as_posix()
    assert hierarchy.parm("file").value == (moved / "props.blend").as_posix()
    assert pushed == [node.path()]  # the hierarchy has no inner reference
    assert not [e for e in plan.not_found if e.kind == "blend"]
    # The converted file: a geometry path under assets_blender, matched by name.
    write(moved / "Props.bgeo.sc", b"geo")
    reader = [c for c in node.children() if c.name() == "converted_file"][0]
    reader.parm("file").set("$HIP/assets_blender/props/geo/Gone/Props.bgeo.sc")
    relink_blender_assets(folder=str(moved))
    assert reader.parm("file").value == (moved / "Props.bgeo.sc").as_posix()


def test_move_into_project_rewrites_the_inner_file_sop_of_a_converted_node(fake):
    """Move into Project: a converted Blender File node made in an untitled scene reads the cache; the move
    rewrites its inner File SOP (an Editable Node) to $HIP/assets_blender."""
    from cycles_houdini.assets.move_project import move_imported_assets_into_project
    from cycles_houdini.blender import blend_assets_layout as layout

    write(fake.blend, b"B")
    blend_dir = fake.cache / "assets_blender" / "metals"
    write(blend_dir / "geo/Props/Props.$F4.bgeo.sc".replace("$F4", "0001"), b"geo1")
    write(blend_dir / "geo/Props/Props.0002.bgeo.sc", b"geo2")
    with layout.open_store(blend_dir) as store:
        store.set_source(str(fake.blend), None)
        store.save()
    node = blender_file(
        fake,
        "props",
        mode="converted",
        source=fake.blend,
        converted=(blend_dir / "geo/Props/Props.$F4.bgeo.sc").as_posix(),
    )
    results, failures = move_imported_assets_into_project(show=False)
    assert failures == [] and len(results) == 1
    reader = [c for c in node.children() if c.name() == "converted_file"][0]
    assert reader.parm("file").value == "$HIP/assets_blender/metals/geo/Props/Props.$F4.bgeo.sc"
    assert (fake.hip / "assets_blender/metals/geo/Props/Props.0002.bgeo.sc").read_bytes() == b"geo2"
    assert node.parm("file").value == str(fake.blend)  # the .blend is not moved
