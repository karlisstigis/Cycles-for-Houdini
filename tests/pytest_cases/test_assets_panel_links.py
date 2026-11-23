# SPDX-License-Identifier: Apache-2.0
"""Linked asset materials (Link mode) against a minimal fake `hou`: shared node, locks, append, reload, menus."""

import contextlib
import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

pytestmark = [pytest.mark.pure, pytest.mark.cpu]

ROOT = Path(__file__).resolve().parents[2]
VOP = object()


class Parm:
    def __init__(self, node, name, locked=False):
        self.node, self.name, self.locked, self.value = node, name, locked, ""

    def path(self):
        return self.node.path() + "/" + self.name

    def isLocked(self):
        return self.locked

    def lock(self, on):
        self.node.host.log.append(("lock", self.path(), on, self.node.host.depth))
        self.locked = on

    def set(self, value):
        self.value = value


class Node:
    def __init__(self, host, parent, name):
        self.host, self._parent, self._name = host, parent, name
        self.children_, self.user, self._parms = [], {}, {}
        self.comment, self.flags, self.pos, self.destroyed = "", {}, (0.0, 0.0), False

    def path(self):
        return (self._parent.path().rstrip("/") if self._parent else "") + "/" + self._name

    def name(self):
        return self._name

    def setName(self, name, unique_name=False):
        if not unique_name and any(c is not self and c._name == name for c in self._parent.children_):
            raise ValueError("name in use: " + name)
        self._name = name

    def parent(self):
        return self._parent

    def childTypeCategory(self):
        return VOP

    def children(self):
        return [c for c in self.children_ if not c.destroyed]

    def allSubChildren(self):
        return [d for c in self.children() for d in [c, *c.allSubChildren()]]

    def add(self, name, parms=("a", "b"), locked=()):
        node = Node(self.host, self, name)
        for parm in parms:
            node._parms[parm] = Parm(node, parm, parm in locked)
        self.children_.append(node)
        return node

    def parms(self):
        return list(self._parms.values())

    def parm(self, name):
        return self._parms.setdefault(name, Parm(self, name))

    def userData(self, key):
        return self.user.get(key)

    def setUserData(self, key, value):
        self.user[key] = value

    def destroyUserData(self, key):
        self.user.pop(key, None)

    def setComment(self, text):
        self.comment = text

    def color(self):
        return SimpleNamespace(rgb=lambda: getattr(self, "rgb", (0.8, 0.8, 0.8)))

    def setColor(self, color):
        self.rgb = tuple(color)

    def setGenericFlag(self, flag, value):
        self.flags[flag] = value

    def position(self):
        return self.pos

    def setPosition(self, position):
        self.pos = tuple(position)

    def moveToGoodPosition(self):
        pass

    def setCurrent(self, *args, **kwargs):
        pass

    def destroy(self):
        self.host.log.append(("destroy", self.path(), self.host.depth))
        self.destroyed = True
        self._parent.children_.remove(self)

    def type(self):
        return SimpleNamespace(category=lambda: "obj", name=lambda: "geo")


@pytest.fixture
def fake(monkeypatch, tmp_path):
    hou = ModuleType("hou")
    hou.log, hou.depth = [], 0
    hou.vopNodeTypeCategory = lambda: VOP
    hou.objNodeTypeCategory = lambda: "obj"
    hou.Error = Exception
    hou.nodeFlag = SimpleNamespace(DisplayComment="display_comment")
    hou.isUIAvailable = lambda: False
    hou.Color = tuple

    @contextlib.contextmanager
    def group(label):
        hou.log.append(("undo", label))
        hou.depth += 1
        try:
            yield
        finally:
            hou.depth -= 1

    hou.undos = SimpleNamespace(group=group)
    root = Node(hou, None, "")
    mat = root.add("mat", parms=())
    obj = root.add("obj", parms=())

    def find(path):
        for node in [mat, obj, *mat.allSubChildren(), *obj.allSubChildren()]:
            if node.path() == path and not node.destroyed:
                return node
        return None

    hou.node = find
    hou.selected = []
    hou.selectedNodes = lambda: list(hou.selected)
    hip = tmp_path / "shots"
    hip.mkdir()
    (hip / "scene.hip").write_bytes(b"hip")
    hou.hipFile = SimpleNamespace(isNewFile=lambda: False, path=lambda: str(hip / "scene.hip"))
    hou.text = SimpleNamespace(expandString=lambda value: value.replace("$HIP", str(hip)))
    hou.getenv = lambda name, default=None: default
    monkeypatch.setitem(sys.modules, "hou", hou)
    from cycles_houdini.blender import blend_library_materials as library

    builds = []

    def build(prepared, *, parent=None, diagnostics=None):
        parent = parent or mat
        name = prepared["name"].replace(" ", "_")
        taken = {c.name() for c in parent.children()}
        unique, n = name, 1
        while unique in taken:
            unique, n = name + str(n), n + 1
        node = parent.add(unique, parms=("surface", "user_locked"), locked=("user_locked",))
        node.add("tex", parms=("filename",))
        builds.append((prepared["name"], prepared.get("asset_root_text"), hou.depth))
        return node

    monkeypatch.setattr(library, "build_library_material", build)
    blend = tmp_path / "lib" / "metals.blend"
    blend.parent.mkdir()
    blend.write_bytes(b"BLENDER")
    return SimpleNamespace(hou=hou, mat=mat, obj=obj, blend=blend, builds=builds, tmp=tmp_path)


def record(fake, name="Metal Steel"):
    from cycles_houdini.assets.records import make_record

    return make_record(name, "MATERIAL", "metals.blend", source=str(fake.blend))


def prepared(name="Metal Steel"):
    return {"name": name, "asset_root_text": "$HIP/assets_blender", "record": {}, "root": "/x", "source": ""}


def parm_states(node):
    from cycles_houdini.blender.blend_library_materials import _material_parms

    return {path: parm.locked for path, parm in _material_parms(node)}


def test_mode_rules():
    from cycles_houdini.assets import importers

    assert importers.valid_modes("MATERIAL") == ("link", "append")
    # Objects/collections: Link = reference SOP, Append = converted geometry files (blend_convert).
    assert importers.valid_modes("OBJECT") == importers.valid_modes("COLLECTION") == ("link", "append")
    assert importers.effective_mode("OBJECT", "append") == "append"
    assert importers.effective_mode("COLLECTION", "link") == "link"
    assert importers.effective_mode("MATERIAL", "append") == "append"


def test_link_reuses_one_locked_shared_node_and_append_copies(fake):
    from cycles_houdini.assets import importers
    from cycles_houdini.blender.blend_library_materials import LINK_USER_DATA, link_info

    rec = record(fake)
    [linked] = importers.import_asset(rec, mode="link", prepared=prepared())
    assert [entry for entry in fake.hou.log if entry[0] == "undo"] == [("undo", "Import Blender asset Metal Steel")]
    assert all(entry[-1] == 1 for entry in fake.hou.log if entry[0] == "lock")  # inside the one undo group
    info = json.loads(linked.userData(LINK_USER_DATA))
    assert info["source"] == str(fake.blend).replace("\\", "/") and info["name"] == "Metal Steel"
    assert "library" not in info and "texture_root" not in info  # textures stay in place
    assert info["signature"] is not None and info["locked_before"] == ["/user_locked"]
    assert all(parm_states(linked).values())  # builder and descendants greyed out
    assert linked.comment == "Linked from metals.blend: Metal Steel"
    assert linked.flags == {"display_comment": True}
    # Link again (import or assign): the same node, no second build.
    again = importers.import_asset(rec, mode="link")
    assert again == [linked] and len(fake.builds) == 1
    target = fake.obj.add("box", parms=("shop_materialpath",))
    material, _ = importers.assign_material(rec, ["/obj/box"], mode="link")
    assert material is linked and target.parm("shop_materialpath").value == linked.path()
    assert importers.find_imported_material(rec, "append") is None  # a linked node is no editable copy
    # Append: a new editable copy every time.
    [copy1] = importers.import_asset(rec, mode="append", prepared=prepared())
    [copy2] = importers.import_asset(rec, mode="append", prepared=prepared())
    assert len({linked.path(), copy1.path(), copy2.path()}) == 3
    assert link_info(copy1) is None and not parm_states(copy1)["/surface"]
    assert importers.all_linked_materials() == [linked]


def test_append_linked_is_exact_unlock_in_one_undo(fake):
    from cycles_houdini.assets import importers, menus
    from cycles_houdini.blender.blend_library_materials import LINK_USER_DATA

    rec = record(fake)
    [linked] = importers.import_asset(rec, mode="link", prepared=prepared())
    path = linked.path()
    assert linked.rgb == (0.45, 0.45, 0.45)  # linked materials are grey
    assert menus.has_linked({"node": linked}) and not menus.has_linked({"node": fake.obj})
    fake.hou.selected = [linked]
    assert menus.has_linked(None) and menus.any_linked_in_scene()
    fake.hou.log.clear()
    menus.append_selected({"node": fake.obj})  # the selection counts too
    undo = [entry for entry in fake.hou.log if entry[0] == "undo"]
    assert undo == [("undo", "Make linked assets editable")]
    assert linked.path() == path and LINK_USER_DATA not in linked.user and linked.comment == ""
    states = parm_states(linked)
    assert states.pop("/user_locked") is True  # locked before linking: stays locked
    assert not any(states.values())
    assert linked.flags == {"display_comment": False}
    assert linked.rgb == (0.8, 0.8, 0.8)  # original colour restored
    assert not menus.has_linked({"node": linked})
    with pytest.raises(importers.AssetImportError):
        importers.append_linked(linked)
    # The importer-level helper is its own undo group too.
    [other] = importers.import_asset(record(fake, "Gold"), mode="link", prepared=prepared("Gold"))
    fake.hou.log.clear()
    importers.append_linked(other.path())
    assert [entry[0] for entry in fake.hou.log if entry[0] == "undo"] == ["undo"]


def test_reload_keeps_path_position_and_relocks_in_one_undo(fake, monkeypatch):
    from cycles_houdini.assets import importers, menus
    from cycles_houdini.blender.blend_library_materials import link_info

    rec = record(fake)
    [linked] = importers.import_asset(rec, mode="link", prepared=prepared())
    linked.setPosition((3.0, -2.0))
    linked.setName("steel_custom")
    linked.setUserData(importers.ASSET_KEY_USER_DATA, "k")
    path = linked.path()
    old_signature = link_info(linked)["signature"]
    assert importers.linked_status(rec) == {"path": path, "stale": False}
    os.utime(fake.blend, ns=(10**18, 10**18))  # the library file changed
    fake.blend.write_bytes(b"BLENDER changed")
    assert importers.linked_status(rec)["stale"] is True
    fake.hou.log.clear()
    [new] = importers.rebuild_linked([(path, prepared())])
    assert new is not linked and linked.destroyed and new.path() == path and new.position() == (3.0, -2.0)
    assert [entry for entry in fake.hou.log if entry[0] == "undo"] == [("undo", "Reload linked material steel_custom")]
    assert all(entry[-1] == 1 for entry in fake.hou.log if entry[0] in ("lock", "destroy"))
    info = link_info(new)
    assert all(parm_states(new).values()) and info["signature"] != old_signature
    assert "texture_root" not in info and new.userData(importers.ASSET_KEY_USER_DATA) == "k"
    assert importers.linked_status(rec) == {"path": path, "stale": False}

    # Snapshot from the link data, then menu reload without a UI runs synchronously.
    snapshot = importers.reload_snapshot(new)
    assert snapshot["source"] == str(fake.blend).replace("\\", "/") and snapshot["path"] == path
    assert snapshot["asset_root_text"] == "$HIP/assets_blender"
    monkeypatch.setattr(importers, "prepare_material", lambda s, cancelled=None, timeout=300: prepared())
    fake.hou.selected = [new]
    reloaded = menus.reload_selected(None)
    assert [node.path() for node in reloaded] == [path] and link_info(reloaded[0])
    assert [node.path() for node in importers.reload_all_linked()] == [path]


def test_menu_xml_files_split_between_host_and_addon():
    """Host menus (blend-import.md): the node right-click and network editor Cycles menus are host menus; the add-on
    keeps only what needs the asset browser (Cycles > Asset Browser)."""
    from cycles_houdini.assets import menus

    addon = ROOT / "houdini/addons/asset_browser"
    host_root = ROOT / "houdini"
    for name in ("OPmenu.xml", "NetworkViewMenu.xml"):
        raw = (host_root / name).read_bytes()
        assert raw.count(b"\r\n") == raw.count(b"\n")  # host files are CRLF, like MainMenuCommon.xml
        assert not (addon / name).exists()
    op = ET.parse(host_root / "OPmenu.xml").getroot()
    sub = op.find("menu/subMenu")
    assert sub.get("id") == "cycles_asset_link_menu" and sub.find("label").text == "Cycles"
    assert ".node_menu(kwargs)" in sub.find("context/expression").text
    items = {item.get("id"): item for item in sub.findall("scriptItem")}
    assert [item.get("id") for item in sub.findall("scriptItem")] == [
        "cycles_convert_material",
        "cycles_node_keep_current_graph",
        "cycles_node_collect_textures",
        "cycles_asset_append_linked",
        "cycles_asset_reload_linked",
    ]
    labels = {key: item.find("label").text for key, item in items.items()}
    assert labels == {
        "cycles_convert_material": "Convert to Cycles Material",
        "cycles_node_keep_current_graph": "Keep Current Graph",
        "cycles_node_collect_textures": "Collect Textures",
        "cycles_asset_append_linked": "Make Editable",
        "cycles_asset_reload_linked": "Reload from Library",
    }
    contexts = {key: item.find("context/expression").text.strip() for key, item in items.items()}
    # Saved-scene protection: shown only on a material waiting for review, even without the bridge.
    assert ".needs_review(kwargs)" in sub.find("context/expression").text
    assert '__import__("cycles_houdini_offline").needs_review(kwargs)' == contexts["cycles_node_keep_current_graph"]
    assert (
        "cycles_houdini_offline.keep_current(kwargs)" in items["cycles_node_keep_current_graph"].find("scriptCode").text
    )
    assert ".node_can_collect(kwargs)" in contexts["cycles_node_collect_textures"]
    assert ".has_linked(kwargs)" in contexts["cycles_asset_append_linked"]
    assert ".has_linked(kwargs)" in contexts["cycles_asset_reload_linked"]
    # Houdini evaluates a one-line menu <expression> as a Python expression: no `return`.
    for path in (host_root / "OPmenu.xml", host_root / "NetworkViewMenu.xml", addon / "MainMenuCommon.xml"):
        for node in ET.parse(path).getroot().iter("expression"):
            text = node.text.strip()
            assert "\n" not in text and "return" not in text
            compile(text, path.name, "eval")
    assert "menus.collect_selected(kwargs)" in items["cycles_node_collect_textures"].find("scriptCode").text
    assert "menus.append_selected(kwargs)" in items["cycles_asset_append_linked"].find("scriptCode").text
    assert "menus.reload_selected(kwargs)" in items["cycles_asset_reload_linked"].find("scriptCode").text

    # The network editor's Cycles menu: in every network editor (Houdini 22 ignores a context on a
    # menu-bar entry); each item checks the Blender bridge and the network kind itself.
    network = ET.parse(host_root / "NetworkViewMenu.xml").getroot().find("menuBar/subMenu")
    assert network.get("id") == "cycles_network_view_menu" and network.find("label").text == "Cycles"
    assert network.find("modifyItem/insertBefore").text == menus.HELP_MENU_ID
    assert network.find("context") is None
    items = network.findall("scriptItem")
    expected = [
        (
            "cycles_network_collect_selected",
            "Collect Textures for Selected",
            "network_can_collect_selected",
            "collect_selected",
        ),
        ("cycles_network_collect_all", "Collect Textures for All", "in_collect_network", "collect_all"),
        ("cycles_network_update_all_blender", "Update All Blender Files", "can_update_all", "update_all"),
        ("cycles_network_relink", "Relink Blender Assets...", "in_asset_network", "relink_assets"),
        (
            "cycles_network_move_into_project",
            "Move Imported Assets into Project",
            "in_asset_network",
            "move_into_project",
        ),
        ("cycles_network_append_linked", "Make Selected Editable", "network_has_linked", "append_selected"),
        ("cycles_network_reload_selected", "Reload Selected from Library", "network_has_linked", "reload_selected"),
        ("cycles_network_reload_all", "Reload All Linked Assets", "network_any_linked", "reload_all"),
    ]
    assert [item.get("id") for item in items] == [row[0] for row in expected]
    from cycles_houdini.blender import blend_file_node

    for item, (key, label, context, action) in zip(items, expected):
        # Update All Blender Files belongs to the Blender File node's module; the rest to menus.
        module, prefix = (
            (blend_file_node, "blend_file_node") if key == "cycles_network_update_all_blender" else (menus, "menus")
        )
        assert item.find("label").text == label
        assert f".{context}(kwargs)" in item.find("context/expression").text
        assert f"{prefix}.{action}(kwargs)" in item.find("scriptCode").text
        assert callable(getattr(module, context)) and callable(getattr(module, action))
    update_all = items[[row[0] for row in expected].index("cycles_network_update_all_blender")]
    assert '"cycles_houdini.blender.blend_file_node"' in update_all.find("context/expression").text

    # Main menu: the host defines Cycles > Render View (and the Blender importer, gated on the
    # bridge component); the add-on merges Asset Browser into it. Houdini 22 evaluates main-menu
    # contexts once at startup, so only the static bridge component check filters an item.
    host = ET.parse(ROOT / "houdini/MainMenuCommon.xml").getroot().find("menuBar/subMenu")
    added = ET.parse(addon / "MainMenuCommon.xml").getroot().find("menuBar/subMenu")
    for menu in (host, added):
        assert menu.get("id") == "cycles_main_menu" and menu.find("modifyItem/insertBefore").text == menus.HELP_MENU_ID
    assert not list(added.iter("context"))
    assert [item.get("id") for item in host.findall("scriptItem")] == [
        "cycles_main_render_view",
        "cycles_main_import_blend",
    ]
    assert [item.get("id") for item in host.findall("scriptItem") if item.find("context") is not None] == [
        "cycles_main_import_blend"
    ]
    assert "menu_actions.open_render_view()" in host.find("scriptItem/scriptCode").text
    assert [item.get("id") for item in added.findall("scriptItem")] == ["cycles_main_asset_browser"]
    host_text = (ROOT / "houdini/MainMenuCommon.xml").read_text(encoding="utf-8")
    assert "cycles_houdini.assets" not in host_text and "cycles_asset_link_menu" not in host_text


class Editor:
    def __init__(self, pwd):
        self._pwd = pwd

    def pwd(self):
        return self._pwd


def test_network_menu_shows_in_material_networks_and_sees_the_shown_material(fake):
    from cycles_houdini.assets import importers, menus

    fake.hou.vopNodeTypeCategory = lambda: "vop"
    fake.mat.childTypeCategory = lambda: "vop"
    fake.obj.childTypeCategory = lambda: "obj"
    assert menus.in_material_network({"editor": Editor(fake.mat)})
    assert not menus.in_material_network({"editor": Editor(fake.obj)})
    assert not menus.in_material_network({}) and not menus.in_material_network(None)

    [linked] = importers.import_asset(record(fake), mode="link", prepared=prepared())
    linked.childTypeCategory = lambda: "vop"  # a material builder holds VOPs
    assert not menus.has_linked({"editor": Editor(fake.mat)})  # nothing selected
    assert menus.has_linked({"editor": Editor(linked)})  # inside the linked material's network
    assert menus.network_has_linked({"editor": Editor(linked)}) and menus.network_any_linked(
        {"editor": Editor(fake.mat)}
    )
    fake.hou.selected = [linked]
    assert not menus.network_has_linked({"editor": Editor(fake.obj)})  # only inside material networks
    assert not menus.network_any_linked({"editor": Editor(fake.obj)})


def test_cycles_menus_follow_the_blender_bridge_and_the_network_kind(fake):
    from cycles_houdini.assets import importers, menus

    fake.hou.vopNodeTypeCategory = lambda: "vop"
    fake.hou.sopNodeTypeCategory = lambda: "sop"
    fake.mat.childTypeCategory = lambda: "vop"
    fake.obj.childTypeCategory = lambda: "obj"
    geo = fake.obj.add("geo1", parms=())
    geo.childTypeCategory = lambda: "sop"
    [linked] = importers.import_asset(record(fake), mode="link", prepared=prepared())
    linked.childTypeCategory = lambda: "vop"
    linked.type = lambda: SimpleNamespace(category=lambda: "vop", name=lambda: "cycles::material::1")
    mat, obj, sop = ({"editor": Editor(node)} for node in (fake.mat, fake.obj, geo))

    assert menus.bridge_enabled()
    assert menus.in_collect_network(mat) and menus.in_collect_network(obj) and not menus.in_collect_network(sop)
    assert menus.in_asset_network(mat) and menus.in_asset_network(obj) and menus.in_asset_network(sop)
    assert not menus.in_asset_network({}) and not menus.network_can_collect_selected(mat)  # nothing selected
    assert menus.node_can_collect({"node": linked}) and menus.node_menu({"node": linked})
    assert not menus.node_can_collect({"node": geo}) and not menus.node_menu({"node": geo})
    fake.hou.selected = [linked]
    assert menus.network_can_collect_selected(mat) and not menus.network_can_collect_selected(sop)
    assert menus.collect_candidates({"node": geo}) == [linked] and menus.network_has_linked(mat)

    # Blender bridge component disabled: every Cycles network and node menu item is off.
    fake.hou.getenv = lambda name, default=None: "0" if name == "CYCLES_HOUDINI_BLENDER_BRIDGE" else default
    assert not menus.bridge_enabled()
    for check in (menus.in_collect_network, menus.in_asset_network, menus.network_can_collect_selected):
        assert not check(mat) and not check(obj)
    assert not menus.network_has_linked(mat) and not menus.network_any_linked(mat)
    assert not menus.node_menu({"node": linked}) and not menus.node_can_collect({"node": linked})


def test_library_materials_drop_the_prototype_marker_so_the_ipr_tracks_edits(fake):
    """blend_materials marks its output as a hidden prototype; the scene observer skips marked
    nodes, so edits to an imported material never reached the IPR. Building and reusing release it."""
    from cycles_houdini.assets import importers
    from cycles_houdini.blender import blend_library_materials as library

    old = fake.mat.add("Metal_Steel", parms=("surface",))
    inner = old.add("tex", parms=("filename",))
    for node in (old, inner):
        node.setUserData(library.PROTOTYPE_MARKER, "1")
    old.setUserData("cycles_blender_library_source", str(fake.blend))
    old.setUserData("cycles_blender_library_material", "Metal Steel")

    assert importers.find_imported_material(record(fake)) is old
    assert old.userData(library.PROTOTYPE_MARKER) is None and inner.userData(library.PROTOTYPE_MARKER) is None
    assert library.release_prototype_marker(old) is old  # idempotent


def test_generated_ancestry_is_shared_only_within_the_current_walk(fake, monkeypatch):
    from cycles_houdini.scene.instancing import generated_nodes

    marker = "cycles_external_blend"
    prototype = fake.obj.add("prototype", parms=())
    ordinary = fake.obj.add("ordinary", parms=())
    first = prototype.add("first", parms=())
    second = prototype.add("second", parms=())
    sibling = ordinary.add("source", parms=())
    candidates = {prototype, ordinary, first, second, sibling}
    marker_reads = []
    user_data = prototype.userData

    def read_marker(key):
        marker_reads.append(key)
        return user_data(key)

    monkeypatch.setattr(prototype, "userData", read_marker)
    prototype.setUserData(marker, "1")
    assert generated_nodes(candidates) == {prototype, first, second}
    assert marker_reads == [marker]

    prototype.destroyUserData(marker)
    assert generated_nodes(candidates) == set()

    prototype.setUserData(marker, "1")
    prototype.children_.remove(first)
    ordinary.children_.append(first)
    first._parent = ordinary
    assert generated_nodes(candidates) == {prototype, second}


def test_build_library_material_releases_the_marker_before_naming_keys():
    import ast

    source = (ROOT / "houdini/python3.13libs/cycles_houdini/blender/blend_library_materials.py").read_text(
        encoding="utf-8"
    )
    function = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == "build_library_material"
    )
    calls = [n.func.id for n in ast.walk(function) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert "release_prototype_marker" in calls
