# SPDX-License-Identifier: Apache-2.0
"""Asset importers against a minimal fake `hou` (parameter names, undo grouping, assignment)."""

import contextlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

pytestmark = [pytest.mark.pure, pytest.mark.cpu]

OBJ, SOP, VOP = object(), object(), object()


class Parm:
    def __init__(self):
        self.value = ""

    def set(self, value):
        self.value = value

    def eval(self):
        return self.value


class Node:
    def __init__(self, host, parent, type_name, name, category, child_category):
        self.host, self._parent, self.type_name, self.name = host, parent, type_name, name
        self.category, self.child_category = category, child_category
        self.parms, self.children_, self.user = {}, [], {}
        self.flags = set()

    def path(self):
        return (self._parent.path().rstrip("/") if self._parent else "") + "/" + self.name

    def parent(self):
        return self._parent

    def type(self):
        return SimpleNamespace(category=lambda: self.category, name=lambda: self.type_name)

    def childTypeCategory(self):
        return self.child_category

    def createNode(self, type_name, name, run_init_scripts=True):
        self.host.log.append(("create", type_name, name, self.host.depth))
        # A SOP digital asset (cycles::blender_file) has SOP children of its own.
        child = {"geo": (OBJ, SOP), "cycles::blender_file": (SOP, SOP)}.get(type_name, (SOP, None))
        node = Node(self.host, self, type_name, name, *child)
        if type_name == "cycles::blender_reference" and run_init_scripts:
            # The SOP's creation script (runCreateScript) enables one reference row.
            for parm_name in ("multiple_references", "references", "references_initialized", "ref_enabled1"):
                node.parm(parm_name).set(1)
        self.children_.append(node)
        if type_name == "cycles::blender_file":
            node.children_.append(Node(self.host, node, "cycles::blender_reference", "reference", SOP, None))
        return node

    def children(self):
        return list(self.children_)

    def parm(self, name):
        return self.parms.setdefault(name, Parm())

    def evalParm(self, name):
        return self.parm(name).eval()

    def setUserData(self, key, value):
        self.user[key] = value

    def userData(self, key):
        return self.user.get(key)

    def moveToGoodPosition(self):
        pass

    def setPosition(self, position):
        self.position = position

    def setCurrent(self, *args, **kwargs):
        pass

    def setDisplayFlag(self, value):
        self.flags.add("display")

    def setRenderFlag(self, value):
        self.flags.add("render")


@pytest.fixture
def fake(monkeypatch, tmp_path):
    hou = ModuleType("hou")
    hou.log, hou.depth = [], 0
    hou.objNodeTypeCategory = lambda: OBJ
    hou.sopNodeTypeCategory = lambda: SOP
    hou.vopNodeTypeCategory = lambda: VOP
    hou.Error = Exception

    @contextlib.contextmanager
    def group(label):
        hou.log.append(("undo", label))
        hou.depth += 1
        try:
            yield
        finally:
            hou.depth -= 1

    hou.undos = SimpleNamespace(group=group)
    root = Node(hou, None, "root", "", None, None)
    obj = Node(hou, root, "obj", "obj", None, OBJ)
    mat = Node(hou, root, "mat", "mat", None, VOP)

    def find(path, nodes=(obj, mat)):
        for node in nodes:
            if node.path() == path:
                return node
            found = find(path, node.children_)
            if found is not None:
                return found
        return None

    hou.node = find
    hou.selected = []
    hou.selectedNodes = lambda: list(hou.selected)
    hip = tmp_path / "shots"
    hip.mkdir()
    (hip / "scene.hip").write_bytes(b"hip")
    hou.scene = SimpleNamespace(new=False, path=str(hip / "scene.hip"), hip=str(hip))
    hou.hipFile = SimpleNamespace(isNewFile=lambda: hou.scene.new, path=lambda: hou.scene.path)
    hou.text = SimpleNamespace(expandString=lambda value: value.replace("$HIP", hou.scene.hip))
    hou.getenv = lambda name, default=None: {"CYCLES_HOUDINI_BLENDER_OCIO": "/ocio/blender.ocio"}.get(name, default)
    monkeypatch.setitem(sys.modules, "hou", hou)
    from cycles_houdini.blender import blend_catalogue

    calls = []
    monkeypatch.setattr(blend_catalogue, "initialize_rows", lambda node: calls.append(("init", node)))
    monkeypatch.setattr(blend_catalogue, "bind_targets", lambda node: calls.append(("bind", node)))
    monkeypatch.setattr(blend_catalogue, "refresh_node", lambda node: calls.append(("refresh", node)))
    blend = tmp_path / "lib" / "props.blend"
    blend.parent.mkdir()
    blend.write_bytes(b"BLENDER")
    return SimpleNamespace(hou=hou, obj=obj, mat=mat, calls=calls, blend=blend, tmp=tmp_path)


def make(fake, name, id_type):
    from cycles_houdini.assets.records import make_record

    return make_record(name, id_type, "props.blend", source=str(fake.blend), object_type="MESH")


def test_append_object_converts_through_blend_convert_in_one_undo_group(fake, monkeypatch):
    from cycles_houdini.assets import importers
    from cycles_houdini.blender import blend_convert

    calls = {}

    def snapshot(source, selection, **options):
        calls["snapshot"] = (source, selection, options)
        return {"snapshot": True}

    def build(payload, *, undo=True, report=None):
        calls["build"] = (payload, undo, fake.hou.depth)
        calls["report"] = report
        return [fake.obj.createNode("geo", "Chair_1")]

    monkeypatch.setattr(blend_convert, "import_snapshot", snapshot)
    monkeypatch.setattr(blend_convert, "prepare_import", lambda snap, cancelled=None: {"prepared": snap})
    monkeypatch.setattr(blend_convert, "build_import", build)
    record = make(fake, "Chair *1", "COLLECTION")

    def report(text, warning=False):
        pass

    nodes = importers.import_asset(record, mode="append", report=report)
    source, selection, options = calls["snapshot"]
    # Houdini convention: forward slashes in path parms, also on Windows (importers._source).
    assert source == str(fake.blend).replace("\\", "/") and selection == [("collection", "Chair *1")]
    assert (options["storage"], options["structure"], options["materials"]) == ("files", "packed", "append")
    # Textures stay in place: no texture location or library name reaches the converter.
    assert "library_name" not in options and "texture_location" not in options
    assert calls["report"] is report  # the converter's report reaches the caller
    # One undo group: the importer's; the converter's own group is disabled inside it.
    assert calls["build"] == ({"prepared": {"snapshot": True}}, False, 1)
    assert [entry for entry in fake.hou.log if entry[0] == "undo"] == [("undo", "Import Blender asset Chair *1")]
    assert nodes[0].userData(importers.ASSET_KEY_USER_DATA) == record["ch"]["key"]
    # A prepared payload (background export) skips the snapshot and Blender.
    calls.clear()
    importers.import_asset(record, mode="append", prepared={"items": []})
    assert "snapshot" not in calls and calls["build"][0] == {"items": []}


def test_append_storage_follows_the_browser_setting(fake, monkeypatch):
    from cycles_houdini.assets import importers, settings
    from cycles_houdini.blender import blend_convert

    seen = []
    monkeypatch.setattr(
        blend_convert, "import_snapshot", lambda source, selection, **options: seen.append(options["storage"])
    )
    stored = dict(settings.DEFAULTS, append_storage="embedded")
    monkeypatch.setattr(settings, "load_settings", lambda: stored)
    record = make(fake, "Chair *1", "COLLECTION")
    importers.append_snapshot(record)
    importers.append_snapshot(record, storage="files")
    assert seen == ["embedded", "files"]
    with pytest.raises(importers.AssetImportError, match="Append storage"):
        importers.append_snapshot(record, storage="reference")
    assert settings.normalize({"append_storage": "bogus"})["append_storage"] == "files"


def test_reference_import_sets_rows_in_one_undo_group(fake):
    from cycles_houdini.assets import importers

    record = make(fake, "Chair *1", "COLLECTION")
    nodes = importers.import_asset(record, mode="link")
    geo = nodes[0]
    assert geo.type_name == "geo" and geo.name == "Chair_1" and geo.parent() is fake.obj
    ref = nodes[1]
    assert ref.type_name == "cycles::blender_reference"
    values = {name: parm.value for name, parm in ref.parms.items()}
    assert values["file"] == str(fake.blend).replace("\\", "/")
    assert values["multiple_references"] == 1 and values["references"] == 1 and values["ref_enabled1"] == 1
    assert values["ref_target_kind1"] == "collection" and values["ref_target1"] == '"Chair *1"'
    # Hidden legacy channels stay untouched: a ref_name would bypass name resolution.
    assert not values.get("ref_name1") and "target" not in values and "target_kind" not in values
    assert [c[0] for c in fake.calls] == ["init", "bind", "refresh"]
    assert {"display", "render"} <= ref.flags
    undo = [entry for entry in fake.hou.log if entry[0] == "undo"]
    creates = [entry for entry in fake.hou.log if entry[0] == "create"]
    assert len(undo) == 1 and all(depth == 1 for *_, depth in creates)
    # Dropping into a SOP network creates the SOP directly there.
    sop_net = fake.obj.createNode("geo", "existing")
    created = importers.import_asset(make(fake, "Lamp", "OBJECT"), network=sop_net, position=(1, 2))
    assert created[0].parent() is sop_net and tuple(created[0].position) == (1, 2)
    assert created[0].type_name == "cycles::blender_reference" and len(created) == 1


def test_link_creates_a_blender_file_node_in_link_mode(fake):
    """With the node assets installed, Link of an object/collection creates a Cycles
    Blender File node in Link mode with the browser's viewport shape, in a new geo object or,
    dropped into a SOP network, directly there; one undo group either way."""
    from cycles_houdini.assets import importers
    from cycles_houdini.blender import blend_file_node

    installed = SimpleNamespace(name=lambda: blend_file_node.FILE_TYPE)
    fake.hou.nodeType = lambda category, name: installed if name == blend_file_node.FILE_TYPE else None
    record = make(fake, "Crate 01", "OBJECT")
    nodes = importers.import_asset(record, mode="link", viewport_shape="bounds")
    geo, node = nodes
    assert geo.type_name == "geo" and geo.parent() is fake.obj
    assert node.type_name == blend_file_node.FILE_TYPE and node.parent() is geo
    values = {name: parm.value for name, parm in node.parms.items()}
    assert values["mode"] == "link" and values["kind"] == "object" and values["target"] == '"Crate 01"'
    assert values["file"] == str(fake.blend).replace("\\", "/") and values["viewport_shape"] == "bounds"
    assert {"display", "render"} <= node.flags
    record_data = blend_file_node.record(node)
    assert record_data["storage"] is None and record_data["signature"]
    assert all(n.userData(importers.ASSET_KEY_USER_DATA) == record["ch"]["key"] for n in nodes)
    assert [entry for entry in fake.hou.log if entry[0] == "undo"] == [("undo", "Import Blender asset Crate 01")]
    assert not [n for n in geo.children() if n.type_name == "cycles::blender_reference"]
    # Dropped into a SOP network: the node is created there, at the drop position.
    sop_net = fake.obj.createNode("geo", "existing")
    created = importers.import_asset(record, mode="link", network=sop_net, position=(3, 4))
    assert len(created) == 1 and created[0].parent() is sop_net
    assert created[0].type_name == blend_file_node.FILE_TYPE and tuple(created[0].position) == (3, 4)
    assert created[0].name == "Crate_01"


def test_material_import_failure_marks_cache_and_assign(fake, monkeypatch):
    from cycles_houdini.assets import importers
    from cycles_houdini.blender import blend_library_materials
    from cycles_houdini.blender.blend_materials import BlendMaterialError

    cache = SimpleNamespace(marks=[], set_importability=lambda *a: cache.marks.append(a))
    seen = {}

    # Without `prepared` the import runs the panel's steps synchronously: snapshot, prepare, build.
    def fake_prepare(snapshot, cancelled=None, timeout=300):
        seen.update(snapshot=snapshot)
        if snapshot["name"] == "Bad":
            raise BlendMaterialError("Unsupported Blender node: Wave Texture")
        return {"name": snapshot["name"], "record": {}, "asset_root_text": snapshot["asset_root_text"]}

    def fake_build(prepared, *, parent=None, diagnostics=None):
        seen.update(parent=parent)
        return fake.mat.createNode("cycles_material", prepared["name"])

    monkeypatch.setattr(importers, "prepare_material", fake_prepare)
    monkeypatch.setattr(blend_library_materials, "build_library_material", fake_build)
    bad = make(fake, "Bad", "MATERIAL")
    with pytest.raises(BlendMaterialError):
        importers.import_asset(bad, mode="append", cache=cache, executable="/opt/blender")
    assert cache.marks == [(bad["ch"]["key"], "no", "Unsupported Blender node: Wave Texture")]
    assert bad["ch"]["importable"]["state"] == "no"
    snapshot = seen["snapshot"]
    assert snapshot["asset_root_text"] == "$HIP/assets_blender" and snapshot["executable"] == "/opt/blender"
    assert snapshot["root"] == str((fake.tmp / "shots/assets_blender").resolve()) and "texture_root" not in snapshot

    good = make(fake, "Steel", "MATERIAL")
    target = fake.obj.createNode("geo", "target")
    with pytest.raises(importers.AssetImportError, match="Select"):
        importers.assign_material(good, [])
    fake.hou.selected = [target.createNode("box", "box1")]  # SOP selection walks up to its object
    material, objects = importers.assign_material(good, cache=cache, library_root=str(fake.tmp / "other"))
    assert objects == [target] and target.parm("shop_materialpath").value == material.path() == "/mat/Steel"
    assert cache.marks[-1] == (good["ch"]["key"], "yes", "") and seen["parent"] is None
    with pytest.raises(blend_library_materials.LibraryMaterialUnavailable, match="outside the asset library"):
        # The asset root may not lie inside the library (blend-import.md: Where imports write): checked before Blender
        # starts.
        importers.assign_material(make(fake, "Gold", "MATERIAL"), [target], library_root=str(fake.tmp))
    again, _ = importers.assign_material(good, [target])
    assert again is material  # reused, not re-imported
    assert len(fake.mat.children()) == 1


def test_untitled_scene_falls_back_to_cache(fake, monkeypatch):
    from cycles_houdini.assets import importers

    monkeypatch.setenv("CYCLES_HOUDINI_ASSET_CACHE", str(fake.tmp / "cache"))
    text, root, notice = importers.asset_root(str(fake.blend))
    assert (text, root, notice) == ("$HIP/assets_blender", str((fake.tmp / "shots/assets_blender").resolve()), "")
    fake.hou.scene.new = True  # File > New: $HIP is only the start-up folder
    cache = (fake.tmp / "cache/assets_blender").as_posix()
    text, root, notice = importers.asset_root(str(fake.blend))
    assert text == cache and root == str(Path(cache).resolve()) and "not saved" in notice and cache in notice
    fake.hou.scene.new = False
    fake.hou.scene.path = str(fake.tmp / "missing/untitled.hip")  # never written to disk
    assert importers.asset_root(str(fake.blend))[0] == cache
    assert not hasattr(importers, "effective_texture_location") and not hasattr(importers, "texture_root")


def test_snapshot_prepare_build_and_classification(fake, monkeypatch):
    from cycles_houdini.assets import importers
    from cycles_houdini.blender import blend_library_materials as library
    from cycles_houdini.blender import blend_worker
    from cycles_houdini.blender.blend_materials import BlendMaterialError
    from cycles_houdini.core import blender_bridge

    blender = fake.tmp / "blender"
    blender.write_bytes(b"")
    monkeypatch.setenv("CYCLES_HOUDINI_BLENDER", str(blender))
    record = make(fake, "Steel", "MATERIAL")
    snapshot = importers.material_snapshot(record)
    assert snapshot["asset_root_text"] == "$HIP/assets_blender"
    assert snapshot["root"] == str((fake.tmp / "shots/assets_blender").resolve())
    assert snapshot["source_config"] == "/ocio/blender.ocio" and snapshot["notice"] == ""
    with pytest.raises(library.LibraryMaterialUnavailable, match="outside the asset library"):
        importers.material_snapshot(record, library_root=str(fake.tmp))

    calls = []
    outcome = {}

    def export(source, names, output_dir, executable, timeout=300, cancelled=None, source_config=None, **options):
        calls.append((executable, source_config, cancelled, options.get("textures")))
        if "raise" in outcome:
            raise outcome["raise"]
        if "error" in outcome:
            return {"records": [], "errors": {names[0]: outcome["error"]}}
        return {"records": [{"key": "k1", "name": names[0], "nodes": [], "outputs": {}}], "errors": {}}

    monkeypatch.setattr(blend_worker, "export_library_materials", export)
    monkeypatch.setattr(blender_bridge, "_discover", lambda *a: str(blender))
    prepared = importers.prepare_material(snapshot)  # pure: hou is not touched here
    assert calls[-1][:2] == (str(blender), "/ocio/blender.ocio") and calls[-1][3] == "in_place"
    assert prepared["record"]["key"] == "k1" and prepared["asset_root_text"] == "$HIP/assets_blender"
    assert prepared["blend_folder"] == str(Path(snapshot["root"]) / "props")

    content = importers.is_content_failure
    outcome.update(error="Unsupported connected Blender node type: ShaderNodeScript")
    with pytest.raises(BlendMaterialError) as error:
        importers.prepare_material(snapshot)
    assert content(error.value)
    del outcome["error"]
    outcome["raise"] = TimeoutError("Blender evaluation exceeded 300.0 seconds")
    with pytest.raises(library.LibraryMaterialUnavailable) as error:
        importers.prepare_material(snapshot)
    assert not content(error.value) and isinstance(error.value, BlendMaterialError)
    cancelled = __import__("threading").Event()
    cancelled.set()
    with pytest.raises(library.LibraryMaterialUnavailable, match="cancelled"):
        importers.prepare_material(snapshot, cancelled)
    monkeypatch.setattr(blender_bridge, "_discover", lambda *a: None)
    with pytest.raises(library.LibraryMaterialUnavailable, match="Blender not found"):
        importers.prepare_material(snapshot)

    # Build on the host: one undo group; a transport failure never marks the cache.
    built = []

    def build(prepared, *, parent=None, diagnostics=None):
        built.append((prepared["name"], prepared["asset_root_text"], fake.hou.depth, diagnostics))
        if prepared.get("fail"):
            raise prepared["fail"]
        return fake.mat.createNode("cycles_material", prepared["name"])

    monkeypatch.setattr(library, "build_library_material", build)
    cache = SimpleNamespace(marks=[], set_importability=lambda *a: cache.marks.append(a))
    target = fake.obj.createNode("geo", "target")
    material, objects = importers.assign_material(record, ["/obj/target"], cache=cache, prepared=prepared)
    assert built == [("Steel", "$HIP/assets_blender", 1, [])] and objects == [target]
    assert target.parm("shop_materialpath").value == material.path() and cache.marks[-1][1] == "yes"
    broken = dict(prepared, fail=library.LibraryMaterialUnavailable("Could not finish importing Steel: disk"))
    other = make(fake, "Other", "MATERIAL")
    with pytest.raises(library.LibraryMaterialUnavailable):
        importers.import_asset(other, mode="append", cache=cache, prepared=broken)
    assert len(cache.marks) == 1 and other["ch"]["importable"]["state"] == "unknown"
    broken["fail"] = BlendMaterialError("Unsupported Blender node: Wave Texture")
    with pytest.raises(BlendMaterialError):
        importers.import_asset(other, mode="append", cache=cache, prepared=broken)
    assert cache.marks[-1][1:] == ("no", "Unsupported Blender node: Wave Texture")


def test_target_token_matches_the_reference_name_menu():
    from cycles_houdini.assets import importers

    assert importers.target_token("Crate_01") == "Crate_01"
    assert importers.target_token("Crate 01") == "Crate_01"
    assert importers.target_token("Chair *1") == '"Chair *1"'
    assert importers.target_token('Say "hi"') == '"Say \\"hi\\""'


def test_reference_import_uses_underscore_alias_without_quotes(fake):
    from cycles_houdini.assets import importers

    nodes = importers.import_asset(make(fake, "Crate 01", "OBJECT"), mode="link", viewport_shape="hull")
    values = {name: parm.value for name, parm in nodes[1].parms.items()}
    assert values["ref_target1"] == "Crate_01" and values["ref_target_kind1"] == "object"
    assert not values.get("ref_name1")
    # The hull is requested before the single refresh, so it is evaluated with the bounds.
    assert values["viewport_shape"] == "hull"


def test_reference_viewport_setting_defaults_to_hull():
    from cycles_houdini.assets import settings

    assert settings.normalize({})["reference_shape"] == "hull"
    assert settings.normalize({"reference_shape": "bounds"})["reference_shape"] == "bounds"
    assert settings.normalize({"reference_shape": "sphere"})["reference_shape"] == "hull"


def test_paths_use_forward_slashes_like_houdini(monkeypatch):
    from types import SimpleNamespace

    from cycles_houdini.assets import importers, libraries, records
    from cycles_houdini.blender import blend_library_materials

    assert libraries.Library(name="L", path="E:\\Assets\\Lib").path == "E:/Assets/Lib"
    record = records.make_record(
        "Crate", "OBJECT", "props\\crate.blend", source="E:\\Assets\\Lib\\props\\crate.blend", thumbnail="C:\\t\\a.png"
    )
    assert record["ch"]["source"] == "E:/Assets/Lib/props/crate.blend" and record["files"] == ["props/crate.blend"]
    assert record["thumbnail"]["url"] == "C:/t/a.png"
    # Records cached before this rule are normalised when they reach a parm.
    monkeypatch.setattr(importers.os.path, "isfile", lambda path: True)
    assert importers._source({"ch": {"source": "E:\\Assets\\crate.blend"}}) == "E:/Assets/crate.blend"

    class TextureParm:
        def __init__(self, value):
            self.value = value

        def unexpandedString(self):
            return self.value

        def set(self, value):
            self.value = value

    parm = TextureParm("C:\\cache\\tex\\abc\\wood.png")
    child = SimpleNamespace(
        parm=lambda name: parm, type=lambda: SimpleNamespace(name=lambda: "cycles::image_texture::1")
    )
    blend_library_materials.forward_slash_texture_paths(SimpleNamespace(allSubChildren=lambda: [child]))
    assert parm.value == "C:/cache/tex/abc/wood.png"
