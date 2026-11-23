# SPDX-License-Identifier: Apache-2.0
"""Cycles Blender File / Blender Hierarchy nodes: pure rules and the creation API (fake `hou`).

Pure: derived paths, the five status texts and their precedence, the parameters visible in
each state against the hide-when table the asset generator writes, the options fingerprint,
selection text and target names. Fake hou (test_blend_convert's network plus the two asset
types): what blend_convert.build_import creates for Link, Converted (files, stash, parts) and
Hierarchy, the fallback to stock networks, Merged refused, status, the inner switch
expressions, Update All, Open in Blender and hiding the native reference node.
Update itself: test_blend_update.py.
"""

import itertools
import json
import os
from types import SimpleNamespace

import pytest

import test_blend_convert as cases

pytestmark = [pytest.mark.pure, pytest.mark.cpu]
fake = cases.fake  # fake hou network, scene and .blend (fixture)


# ---------------------------------------------------------------- pure


def test_part_tokens_and_paths():
    from cycles_houdini.blender import blend_file_node as fn

    assert [fn.part_suffix(t) for t in ("", "base", "subdiv", "subdiv_2")] == ["", "", "_subdiv", "_subdiv_2"]
    assert fn.part_token("", 1) == "" and fn.part_token("", 2) == "base" and fn.part_token("_subdiv", 2) == "subdiv"
    text = "$HIP/assets_blender/props/geo/Props/Props.bgeo.sc"
    assert fn.part_path(text, "base", "subdiv") == "$HIP/assets_blender/props/geo/Props/Props_subdiv.bgeo.sc"
    assert fn.part_path(text.replace(".bgeo", "_subdiv.bgeo"), "subdiv", "base") == text
    assert fn.part_path("x/Props.$F4.bgeo.sc", "", "subdiv") == "x/Props_subdiv.$F4.bgeo.sc"
    assert fn.part_path("x/Props.bgeo.sc", "subdiv", "base") == ""  # not that part's file
    assert fn.frames_value("range", (1, 5, 2)) == (1, 5, 2)
    assert fn.frames_value("range", (1, 5, 2), stash=True) is None and fn.frames_value("single", (1, 5, 2)) is None


def test_derived_paths(tmp_path):
    from cycles_houdini.blender import blend_file_node as fn

    hip = tmp_path / "shot"
    root = hip / "assets_blender"
    source = tmp_path / "lib" / "props.blend"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"BLENDER")
    one = fn.derived_paths(root, str(source), "collection", "Big Tree", "", None, root_text="$HIP/assets_blender")
    assert one["asset_folder"] == root / "props" / "geo" / "Big Tree"
    assert one["files"] == [root / "props" / "geo" / "Big Tree" / "Big Tree.bgeo.sc"]
    assert one["text"] == "$HIP/assets_blender/props/geo/Big Tree/Big Tree.bgeo.sc"
    seq = fn.derived_paths(root, str(source), "object", "a/b", "subdiv", (1, 3, 1))
    assert seq["pattern"] == "a_b_subdiv.$F4.bgeo.sc"
    assert [p.name for p in seq["files"]] == [f"a_b_subdiv.000{i}.bgeo.sc" for i in (1, 2, 3)]
    assert seq["text"] == (root / "props" / "geo" / "a_b" / "a_b_subdiv.$F4.bgeo.sc").as_posix()

    class Names:  # an ImportStore's recorded name wins over the plain rule
        def asset_name(self, original, name):
            assert original == "object:Tree"
            return "Tree_2"

    assert fn.derived_paths(root, str(source), "object", "Tree", names=Names())["pattern"] == "Tree_2.bgeo.sc"


def test_status_texts_and_precedence():
    from cycles_houdini.blender import blend_file_node as fn

    s = fn.status
    assert s("converted", True, "a", "a", True, False) == fn.UP_TO_DATE == "Up to date"
    assert s("converted", True, "a", "b", True, False) == fn.SOURCE_CHANGED == "Source changed"
    assert s("converted", True, "a", "a", False, False) == fn.SOURCE_MISSING == "Source missing"
    assert s("converted", False, None, "a", True, False) == fn.NOT_CONVERTED == "Not converted. Press Update."
    assert s("converted", True, "a", "a", True, True) == fn.OPTIONS_CHANGED == "Options changed. Press Update."
    # Precedence: missing > not converted > source changed > options changed.
    assert s("converted", False, "a", "b", False, True) == fn.SOURCE_MISSING
    assert s("converted", False, "a", "b", True, True) == fn.NOT_CONVERTED
    assert s("converted", True, "a", "b", True, True) == fn.SOURCE_CHANGED
    # Link reads the .blend live; an unknown signature never claims a change.
    assert s("link", False, "a", "b", True, True) == fn.UP_TO_DATE
    assert s("link", False, None, None, False, False) == fn.SOURCE_MISSING
    assert s("converted", True, None, "b", True, False) == fn.UP_TO_DATE
    assert set(fn.STATUSES) == {
        fn.UP_TO_DATE,
        fn.SOURCE_CHANGED,
        fn.SOURCE_MISSING,
        fn.NOT_CONVERTED,
        fn.OPTIONS_CHANGED,
    }


def _states():
    for mode, load_as, stash, frames_mode, kind, parts, missing in itertools.product(
        ("link", "converted"),
        ("packed", "unpacked"),
        (0, 1),
        ("single", "range"),
        ("object", "collection"),
        (1, 2),
        (False, True),
    ):
        yield dict(
            mode=mode,
            load_as=load_as,
            stash=stash,
            frames_mode=frames_mode,
            kind=kind,
            parts=parts,
            source_missing=missing,
        )


def _parm_values(state):
    """The parameter values a state shows to Houdini's hide-when conditions."""
    return dict(state, part="base" if state["parts"] > 1 else "", file="" if state["source_missing"] else "x.blend")


def test_hide_when_table_matches_visible_parms_in_every_state():
    from cycles_houdini.blender import blend_file_node as fn

    tokens = [name for name in fn.FILE_PARMS if name != fn.DATA_PARM]
    assert set(fn.HIDE_WHEN) <= set(tokens)
    for state in _states():
        visible = fn.visible_parms(state)
        values = _parm_values(state)
        for token in tokens:
            hidden = fn.condition_holds(fn.HIDE_WHEN.get(token, ""), values)
            assert hidden == (token not in visible), (token, state)
    for state in (
        {"stash": 0, "frames_mode": "range", "source_missing": False},
        {"stash": 1, "frames_mode": "single", "source_missing": True},
    ):
        visible = fn.hierarchy_visible_parms(state)
        values = dict(state, file="" if state["source_missing"] else "x.blend")
        for token in fn.HIERARCHY_PARMS:
            assert fn.condition_holds(fn.HIERARCHY_HIDE_WHEN.get(token, ""), values) == (token not in visible)


def test_only_what_works_is_visible():
    from cycles_houdini.blender import blend_file_node as fn

    converted_only = {
        "load_as",
        "unpack_iterations",
        "stash",
        "frames_mode",
        "frame_range",
        "apply_subdivision",
        "nested_instances",
        "part",
        "update",
        "open_blender",
        "files",
    }
    for state in _states():
        visible = fn.visible_parms(state)
        assert {"file", "kind", "target", "mode", "status"} <= visible
        assert fn.DATA_PARM not in visible
        if state["mode"] == "link":
            assert not visible & converted_only and set(fn.LINK_PARMS) <= visible
        else:
            assert not visible & set(fn.LINK_PARMS)
            assert ("unpack_iterations" in visible) == (state["load_as"] == "unpacked")
            if state["stash"]:  # one frame, no files
                assert not visible & {"frames_mode", "frame_range", "files"}
            assert ("frame_range" in visible) == (not state["stash"] and state["frames_mode"] == "range")
            assert ("part" in visible) == (state["parts"] > 1)
            assert ("update" in visible) == (not state["source_missing"])
        assert ("include_disabled" in visible) == (state["kind"] == "collection")


def test_options_fingerprint():
    from cycles_houdini.blender import blend_file_node as fn

    base = dict(
        kind="collection",
        target="Props",
        stash=False,
        frames=None,
        apply_subdivision=False,
        nested_instances=True,
        include_disabled=False,
        source="/a.blend",
    )
    same = fn.options_fingerprint(base)
    assert fn.options_fingerprint(dict(base, source="/moved/a.blend", part="subdiv")) == same  # not options
    assert fn.options_fingerprint(dict(base, stash=1)) == fn.options_fingerprint(dict(base, stash=True))
    for key, value in (
        ("frames", (1, 2, 1)),
        ("stash", True),
        ("apply_subdivision", True),
        ("target", "Tree"),
        ("nested_instances", False),
        ("include_disabled", True),
    ):
        assert fn.options_fingerprint(dict(base, **{key: value})) != same, key


def test_selection_text_and_targets():
    from cycles_houdini.blender import blend_file_node as fn

    selection = [("collection", "Props"), ("object", "Big Tree"), ("object", 'say "hi"')]
    text = fn.format_selection(selection)
    assert text.startswith('collection:Props object:"Big Tree"')
    assert fn.parse_selection(text) == selection
    for bad in ("Props", "light:Lamp", "object:"):
        with pytest.raises(fn.NodeError):
            fn.parse_selection(bad)
    assert fn.target_token("Props") == "Props" and fn.target_token("Big Tree") == '"Big Tree"'
    assert fn.resolve_target('"Big Tree"') == "Big Tree"
    assert fn.resolve_target("Big_Tree", ["Big Tree", "Rock"]) == "Big Tree"
    assert fn.resolve_target("Crate", []) == "Crate"  # names unknown: the token as typed
    assert fn.resolve_target("Big_Tree", ["Other"], {"Big_Tree": "Other"}) == "Other"
    for bad in ("A B", "^A", "Tr*"):
        with pytest.raises(fn.NodeError):
            fn.resolve_target(bad, ["Tree", "Trunk"])


def test_source_signature(tmp_path):
    from cycles_houdini.blender import blend_file_node as fn

    path = tmp_path / "a.blend"
    assert fn.source_signature(str(path)) is None and fn.source_signature("") is None
    path.write_bytes(b"x")
    first = fn.source_signature(str(path))
    os.utime(path, ns=(1, 10**9))
    path.write_bytes(b"xy")
    assert first and fn.source_signature(str(path)) != first


# ---------------------------------------------------------------- fake hou with the asset types


class FakeType:
    def __init__(self, root, name):
        self.root, self._name, self.hidden = root, name, False

    def name(self):
        return self._name

    def instances(self):
        return [node for node in self.root.allSubChildren() if node.type_name == self._name]

    def setHidden(self, on):
        self.hidden = on


@pytest.fixture
def assets(fake, monkeypatch):
    """test_blend_convert's fake plus cycles::blender_file / cycles::blender_hierarchy."""
    from cycles_houdini.blender import blend_catalogue
    from cycles_houdini.blender import blend_file_node as fn

    hou, root = fake.hou, fake.obj.parent()
    names = (fn.FILE_TYPE, fn.HIERARCHY_TYPE, fn.NATIVE_REFERENCE_TYPE, "file")
    types = {name: FakeType(root, name) for name in names}
    state = SimpleNamespace(installed=True, types=types, refreshed=[])
    sop = SimpleNamespace(nodeTypes=lambda: types, nodeType=lambda name: types.get(name))
    hou.sopNodeTypeCategory = lambda: sop
    hou.objNodeTypeCategory = lambda: "obj"
    hou.nodeType = lambda category, name: types.get(name) if state.installed and name in names[:2] else None
    hou.isUIAvailable = lambda: False
    original = cases.Node.createNode

    def create(self, type_name, name, run_init_scripts=True):
        node = original(self, type_name, name, run_init_scripts)
        if type_name == fn.FILE_TYPE:
            for child, kind in (
                (fn.REFERENCE_NODE, fn.NATIVE_REFERENCE_TYPE),
                (fn.FILE_NODE, "file"),
                (fn.STASH_NODE, "stash"),
            ):
                original(node, kind, child)
            for parm, value in dict(
                mode="link",
                load_as="packed",
                unpack_iterations=1,
                nested_instances=1,
                frames_mode="single",
                stash=0,
                part="",
            ).items():
                node.parm(parm).value = value
        return node

    def outputs(self):
        return [n for n in self._parent.children_ if self in n.inputs] if self._parent else []

    def set_input(self, index, node):
        while len(self.inputs) <= index:
            self.inputs.append(None)
        self.inputs[index] = node

    def destroy(self):
        self._parent.children_.remove(self)

    monkeypatch.setattr(cases.Node, "createNode", create)
    monkeypatch.setattr(cases.Node, "outputs", outputs, raising=False)
    monkeypatch.setattr(cases.Node, "setInput", set_input, raising=False)
    monkeypatch.setattr(cases.Node, "destroy", destroy, raising=False)
    monkeypatch.setattr(cases.Node, "parmTransform", lambda self: self.transform, raising=False)
    monkeypatch.setattr(cases.Node, "position", lambda self: self.pos or (0, 0), raising=False)
    monkeypatch.setattr(blend_catalogue, "refresh_node", lambda node, force=True: state.refreshed.append(node.path()))
    fake.state = state
    return fake


def blender_files(fake):
    from cycles_houdini.blender import blend_file_node as fn

    return fake.state.types[fn.FILE_TYPE].instances()


def test_link_creates_a_blender_file_node_in_link_mode(assets):
    from cycles_houdini.blender import blend_convert
    from cycles_houdini.blender import blend_file_node as fn

    snap = cases.snapshot(
        assets,
        storage="reference",
        include_disabled=True,
        selection=[("collection", "Props"), ("object", "Lamp 1")],
        viewport_shape="hull",
    )
    payload = blend_convert.prepare_import(snap, evaluate=None)
    nodes = blend_convert.build_import(payload)
    assert [n.name() for n in nodes] == ["Props", "Lamp_1"] and all(n.type_name == "geo" for n in nodes)
    props, lamp = (n.node("blender_file") for n in nodes)
    assert props.type_name == fn.FILE_TYPE and {"display", "render"} <= props.flags
    assert props.evalParm("mode") == "link" and props.evalParm("kind") == "collection"
    assert props.evalParm("target") == "Props" and props.evalParm("include_disabled") == 1
    assert lamp.evalParm("target") == '"Lamp 1"' and lamp.evalParm("include_disabled") == 0
    assert props.evalParm("viewport_shape") == "hull"
    reference = props.node(fn.REFERENCE_NODE)
    assert reference.evalParm("file") == str(assets.blend).replace("\\", "/")
    assert reference.evalParm("ref_target1") == "Props" and reference.evalParm("ref_target_kind1") == "collection"
    assert assets.state.refreshed == [reference.path(), lamp.node(fn.REFERENCE_NODE).path()]
    data = fn.record(props)
    assert data["storage"] is None and data["signature"] and data["materials"] == "link"
    assert nodes[0].userData("cycles_blender_import") is None  # the old record is not written
    assert fn.source_index(props) == 0 and fn.node_status(props) == fn.UP_TO_DATE


def test_drop_into_a_sop_network_creates_the_node_there(assets, monkeypatch):
    """A browser drop into a SOP network: Link and Convert & Append create the Blender File node
    in that network; a further subdivision part gets its own geo object (Cycles subdivides per
    object); a hierarchy goes to the object network around it."""
    from cycles_houdini.blender import blend_convert
    from cycles_houdini.blender import blend_file_node as fn

    sop = assets.hou.sopNodeTypeCategory()
    obj = assets.hou.objNodeTypeCategory()

    def category(self):
        if self.path() == "/obj" or self.type_name == fn.HIERARCHY_TYPE:
            return obj
        return sop if self.type_name == "geo" else None

    monkeypatch.setattr(cases.Node, "childTypeCategory", category, raising=False)
    target = assets.obj.createNode("geo", "mine")
    snap = cases.snapshot(
        assets, storage="reference", selection=[("object", "Tree")], parent=target.path(), position=(2, 3)
    )
    [node] = blend_convert.build_import(blend_convert.prepare_import(snap, evaluate=None))
    assert node.type_name == fn.FILE_TYPE and node.parent() is target and node.name() == "Tree"
    assert node.evalParm("mode") == "link" and {"display", "render"} <= node.flags
    assert [c.name() for c in assets.obj.children()] == ["mine"]

    nodes, _, messages = cases.run(
        assets, "packed", parent=target.path(), manifest_for=lambda request: cases.subdivided_scene()
    )
    first, second = nodes
    assert first.type_name == fn.FILE_TYPE and first.parent() is target and first.evalParm("part") == "base"
    assert first.evalParm("mode") == "converted" and fn.record(first)["storage"] == "files"
    assert second.type_name == "geo" and second.parent() is assets.obj
    assert second.node("blender_file").evalParm("part") == "subdiv"
    assert second.evalParm("cycles_subdivision_render_level") == 2
    assert target.evalParm("cycles_subdivision_override") in (None, "")  # the artist's object is left alone

    [hierarchy], _, _ = cases.run(assets, "hierarchy", materials="none", parent=target.path())
    assert hierarchy.type_name == fn.HIERARCHY_TYPE and hierarchy.parent() is assets.obj


def test_converted_files_and_stash_nodes(assets):
    from cycles_houdini.blender import blend_file_node as fn

    nodes, _, _ = cases.run(assets, "packed")
    [container] = nodes
    node = container.node("blender_file")
    assert node.type_name == fn.FILE_TYPE and container.node("file1") is None and container.node("material1") is None
    assert node.evalParm("mode") == "converted" and node.evalParm("stash") == 0 and node.evalParm("part") == ""
    reader = node.node(fn.FILE_NODE)
    assert reader.evalParm("file") == "$HIP/assets_blender/props/geo/Props/Props.bgeo.sc"
    assert any(e[:3] == ("press", reader.path(), "reload") for e in assets.hou.log)
    data = fn.record(node)
    assert data["storage"] == "files" and data["materials"] == "link" and data["options"] == fn.fingerprint_of(node)
    assert container.userData("cycles_blender_import") is None
    assert fn.source_index(node) == 1 and fn.unpack_index(node) == 0
    node.parm("load_as").set("unpacked")
    assert fn.unpack_index(node) == 1
    assert fn.node_status(node) == fn.UP_TO_DATE
    assert fn.files_text(node) == "$HIP/assets_blender/props/geo/Props"
    content = cases.file_text(assets.assets / "geo" / "Props" / "Props.bgeo.sc")["content"]
    assert content["attribs"]["prim:name"] and content["attribs"]["prim:path"][0] == "/Props/Sub/Child"

    stashed, _, _ = cases.run(assets, "packed", storage="embedded")
    node = stashed[0].node("blender_file")
    assert node.evalParm("stash") == 1 and isinstance(node.evalParm(fn.DATA_PARM), cases.FakeGeometry)
    assert fn.record(node)["storage"] == "stash" and fn.source_index(node) == 2
    assert fn.files_text(node) == "Not converted"


def test_frame_range_and_parts(assets):
    from cycles_houdini.blender import blend_file_node as fn

    def manifest_for(request):
        return cases.scene_manifest(frame=request["frame"], offset=request["frame"])

    nodes, _, _ = cases.run(assets, "packed", frames=(1, 3, 1), manifest_for=manifest_for)
    node = nodes[0].node("blender_file")
    assert node.evalParm("frames_mode") == "range"
    assert [node.evalParm(f"frame_range{i}") for i in (1, 2, 3)] == [1, 3, 1]
    assert node.node(fn.FILE_NODE).evalParm("file").endswith("/Props.$F4.bgeo.sc")
    nodes, _, _ = cases.run(assets, "packed", manifest_for=lambda request: cases.subdivided_scene())
    plain, smooth = (n.node("blender_file") for n in nodes)
    assert (plain.evalParm("part"), smooth.evalParm("part")) == ("base", "subdiv")
    assert smooth.node(fn.FILE_NODE).evalParm("file").endswith("/Props_subdiv.bgeo.sc")
    assert nodes[1].evalParm("cycles_subdivision_render_level") == 2
    assert fn.record(smooth)["part"] == "subdiv"


def test_packed_top_level_polygons_carry_material_paths():
    """A packed file needs no Material SOP: any unpacked top-level polygon is bound in the file."""
    import numpy as np

    from cycles_houdini.blender import blend_convert as bc

    record, arrays = cases.mesh_record()
    geo = bc.mesh_geo(
        record, arrays, ["kRed", "kDefault", "kBlue"], {"kRed": "Red Paint", "kBlue": "Blue"}, bc.Report()
    )
    frames = [{"geo": geo, "children": {}, "parts": [{"subdivision": None, "geo": geo}]}]
    [(bound, _)] = bc._part_frames(frames, {"subdivision": None}, {"kRed": "/mat/Red", "kBlue": "/mat/Blue"})
    assert bound.attribs["shop_materialpath"].values == ["/mat/Red", "/mat/Blue", ""]
    assert np.array_equal(bound.P, geo.P) and "shop_materialpath" not in geo.attribs


def test_hierarchy_creates_one_hierarchy_object(assets):
    from cycles_houdini.blender import blend_file_node as fn

    nodes, _, _ = cases.run(assets, "hierarchy", materials="none")
    [hierarchy] = nodes
    assert hierarchy.type_name == fn.HIERARCHY_TYPE and hierarchy.name() == "Props" and hierarchy.parent() is assets.obj
    assert [c.name() for c in assets.obj.children()] == ["Props"]  # nothing else in /obj
    children = {c.name(): c for c in hierarchy.children()}
    assert set(children) == {"Props", "Parent", "Child", "Tree"}  # collection null + objects
    assert children["Props"].userData(fn.COLLECTION_USER_DATA) == "Props"
    assert children["Child"].inputs == [children["Parent"]] and children["Tree"].inputs == [children["Props"]]
    assert children["Tree"].userData(fn.OBJECT_USER_DATA) == "Tree"
    assert children["Tree"].node("file1").userData(fn.SOURCE_USER_DATA) == "1"
    assert hierarchy.evalParm("selection") == "collection:Props" and hierarchy.evalParm("stash") == 0
    data = fn.record(hierarchy)
    assert data["storage"] == "files" and set(data["transforms"]) == {"Parent", "Child", "Tree"}
    assert data["transforms"]["Child"][12:15] == [1.0, 0.0, 0.0]
    assert fn.node_status(hierarchy) == fn.UP_TO_DATE
    assert hierarchy.userData("cycles_blender_import") is None


def test_fallback_to_stock_networks_when_the_assets_are_missing(assets):
    assets.state.installed = False
    nodes, _, messages = cases.run(assets, "packed")
    assert nodes[0].node("file1") is not None and nodes[0].node("blender_file") is None
    assert any("cycles::blender_file asset is not installed" in text for text, _ in messages)
    assert json.loads(nodes[0].userData("cycles_blender_import"))["structure"] == "packed"


def test_merged_is_refused_with_a_clear_message(fake):
    from cycles_houdini.blender import blend_convert

    with pytest.raises(blend_convert.BlendImportError, match="Load As to Unpacked"):
        cases.snapshot(fake, structure="merged")
    assert blend_convert.STRUCTURES == ("hierarchy", "packed")


def test_status_follows_options_source_and_mode(assets):
    from cycles_houdini.blender import blend_file_node as fn

    nodes, _, _ = cases.run(assets, "packed")
    node = nodes[0].node("blender_file")
    assert fn.status_text(node) == fn.UP_TO_DATE
    node.parm("apply_subdivision").set(1)
    assert fn.node_status(node) == fn.OPTIONS_CHANGED
    node.parm("apply_subdivision").set(0)
    fn.write_record(node, signature="older")
    assert fn.node_status(node) == fn.SOURCE_CHANGED
    node.parm("mode").set("link")
    assert fn.node_status(node) == fn.UP_TO_DATE and fn.source_index(node) == 0  # nothing deleted
    node.parm("mode").set("converted")
    node.setUserData(fn.USER_DATA, "")  # never converted (e.g. created in Link mode)
    assert fn.node_status(node) == fn.NOT_CONVERTED and fn.source_index(node) == 0  # shows the Link result
    node.parm("file").set(str(assets.tmp / "gone.blend"))
    assert fn.node_status(node) == fn.SOURCE_MISSING


def test_update_all_presses_update_only_on_changed_sources(assets, monkeypatch):
    from cycles_houdini.blender import blend_file_node as fn

    first, _, _ = cases.run(assets, "packed")
    second, _, _ = cases.run(assets, "hierarchy", materials="none")
    link = cases.snapshot(assets, storage="reference")
    from cycles_houdini.blender import blend_convert

    blend_convert.build_import(blend_convert.prepare_import(link))
    pressed = []
    monkeypatch.setattr(fn, "start_update", lambda nodes, report=None: pressed.append([n.path() for n in nodes]))
    assert fn.update_all() is None and pressed == []  # nothing changed
    assert not fn.can_update_all()
    fn.write_record(first[0].node("blender_file"), signature="older")
    fn.write_record(second[0], signature="older")
    assert fn.can_update_all()
    fn.update_all()
    assert pressed == [[first[0].node("blender_file").path(), second[0].path()]]


def test_open_blender_is_detached(assets, monkeypatch):
    from cycles_houdini.blender import blend_file_node as fn

    nodes, _, _ = cases.run(assets, "packed")
    node = nodes[0].node("blender_file")
    monkeypatch.setattr(fn, "blender_executable", lambda: "/opt/blender")
    # Blender's own environment: Houdini's OCIO and Python variables are not passed on.
    monkeypatch.setenv("OCIO", "/houdini/aces.ocio")
    monkeypatch.setenv("PYTHONPATH", "/houdini/python")
    monkeypatch.setenv("CYCLES_HOUDINI_BLENDER_OCIO", "/blender/config.ocio")
    assets.hou.getenv = lambda name, default=None: None
    calls = []
    fn.open_blender(node, popen=lambda command, **options: calls.append((command, options)))
    [(command, options)] = calls
    assert command == ["/opt/blender", str(assets.blend).replace("\\", "/")]
    assert options["stdin"] is not None and (options.get("start_new_session") or options.get("creationflags"))
    assert options["env"]["OCIO"] == "/blender/config.ocio" and "PYTHONPATH" not in options["env"]
    monkeypatch.delenv("CYCLES_HOUDINI_BLENDER_OCIO")
    calls.clear()
    fn.open_blender(node, popen=lambda command, **options: calls.append((command, options)))
    assert "OCIO" not in calls[0][1]["env"]
    node.parm("file").set(str(assets.tmp / "gone.blend"))
    with pytest.raises(fn.NodeError, match="Source missing"):
        fn.open_blender(node, popen=lambda *a, **k: None)


def test_callbacks_push_to_the_inner_reference(assets, monkeypatch):
    from cycles_houdini.blender import blend_catalogue
    from cycles_houdini.blender import blend_file_node as fn

    calls = []
    for name in ("parameters_changed", "selection_changed", "viewport_changed"):
        monkeypatch.setattr(blend_catalogue, name, lambda node, name=name: calls.append((name, node.name())))
    nodes, _, _ = cases.run(assets, "packed")
    node = nodes[0].node("blender_file")
    reference = node.node(fn.REFERENCE_NODE)
    node.parm("target").set("Tree")
    fn.on_parm(dict(node=node, parm_name="target"))
    assert reference.evalParm("ref_target1") == "Tree" and calls[-1] == ("selection_changed", "reference")
    node.parm("file").set("/moved/props.blend")
    fn.on_parm(dict(node=node, parm_name="file"))
    assert reference.evalParm("file") == "/moved/props.blend" and calls[-1][0] == "parameters_changed"
    fn.on_parm(dict(node=node, parm_name="shape_frame"))
    assert calls[-1][0] == "viewport_changed"
    # Part: the File SOP follows to the other part's file of the same asset.
    reader = node.node(fn.FILE_NODE)
    node.parm("part").set("subdiv")
    fn.on_parm(dict(node=node, parm_name="part"))
    assert reader.evalParm("file").endswith("/Props_subdiv.bgeo.sc")
    assert fn.target_menu(dict(node=node)) == []  # no catalogue read yet


def test_hide_native_reference(assets):
    from cycles_houdini.blender import blend_file_node as fn

    native = assets.state.types[fn.NATIVE_REFERENCE_TYPE]
    assets.state.installed = False
    assert fn.hide_native_reference() is False and not native.hidden  # no replacement installed
    assets.state.installed = True
    assert fn.hide_native_reference() is True and native.hidden


def test_status_label_never_raises(assets, monkeypatch):
    from cycles_houdini.blender import blend_file_node as fn

    monkeypatch.setattr(fn, "node_status", lambda node: (_ for _ in ()).throw(RuntimeError("boom")))
    assets.hou.pwd = lambda: None
    assert fn.status_text().startswith("Status unavailable")
    assert fn.source_index(object()) == 0 and fn.unpack_index(object()) == 0


def test_hierarchy_edit_selection_uses_the_importer_tree(assets):
    """Edit... calls blend_import_dialog.edit_selection(source, selection, include_disabled=) and
    writes the chosen items; Cancel (None) and an unchanged choice leave the parameter alone."""
    from cycles_houdini.blender import blend_file_node as fn
    from cycles_houdini.blender import blend_import_dialog

    [hierarchy], _, _ = cases.run(assets, "hierarchy", materials="none")
    hierarchy.parm("include_disabled").set(1)
    calls = []

    def editor(source, selection, *, include_disabled=False):
        calls.append((source, selection, include_disabled))
        return [("collection", "Props"), ("object", "Big Tree")]

    assert fn.edit_selection(hierarchy, editor=editor) == [("collection", "Props"), ("object", "Big Tree")]
    assert calls == [(str(assets.blend).replace("\\", "/"), [("collection", "Props")], True)]
    assert hierarchy.evalParm("selection") == 'collection:Props object:"Big Tree"'
    assert fn.node_status(hierarchy) == fn.OPTIONS_CHANGED
    assert fn.edit_selection(hierarchy, editor=lambda *a, **k: None) is None
    assert (
        fn.edit_selection(hierarchy, editor=lambda *a, **k: fn.parse_selection(hierarchy.evalParm("selection"))) is None
    )
    assert hierarchy.evalParm("selection") == 'collection:Props object:"Big Tree"'
    # The default editor is the importer's dialog function (signature checked, not opened here).
    import inspect

    parameters = inspect.signature(blend_import_dialog.edit_selection).parameters
    assert list(parameters)[:2] == ["source", "selection"] and "include_disabled" in parameters


class _Sop:
    def __init__(self, name, *inputs):
        self.name, self._inputs = name, list(inputs)

    def path(self):
        return "/obj/geo/" + self.name

    def inputs(self):
        return list(self._inputs)


def test_subdivision_checks_are_pure():
    from cycles_houdini.blender import blend_file_node as fn

    setting = {"type": "catmull-clark", "render_level": 2}
    on = {"cycles_subdivision_override": 1, "cycles_subdivision_type": "catmull-clark"}
    assert fn.subdivision_matches(setting, dict(on, cycles_subdivision_render_level=2))
    assert not fn.subdivision_matches(setting, dict(on, cycles_subdivision_render_level=1))
    assert not fn.subdivision_matches(setting, dict(on, cycles_subdivision_override=0))
    assert fn.subdivision_matches({}, {"cycles_subdivision_override": 1})  # earlier builds: only "on"
    part = _Sop("blender_file")
    other = _Sop("box")
    assert fn.upstream_of(part, part) == (True, False)
    assert fn.upstream_of(_Sop("OUT", _Sop("xform", part)), part) == (True, False)
    assert fn.upstream_of(_Sop("OUT", _Sop("merge", part, other)), part) == (True, True)
    assert fn.upstream_of(_Sop("OUT", other), part) == (False, False)  # not rendered: no warning
    looped = _Sop("loop")
    looped._inputs = [looped, part]
    assert fn.upstream_of(looped, part) == (True, True)


def test_subdivided_part_warns_in_the_status_and_the_update_report(assets, monkeypatch):
    """Subdivision warning: a subdivided part whose object does not subdivide it, or merges it with other
    geometry, shows a warning naming the fix after the Status text; its own object does not."""
    from cycles_houdini.blender import blend_file_node as fn

    nodes, _, _ = cases.run(assets, "packed", manifest_for=lambda request: cases.subdivided_scene())
    plain, smooth = (n.node("blender_file") for n in nodes)
    assert fn.record(smooth)["subdivision"]["render_level"] == 2 and fn.record(plain)["subdivision"] is None
    render = {}
    monkeypatch.setattr(cases.Node, "renderNode", lambda self: render.get(self.path()), raising=False)
    monkeypatch.setattr(cases.Node, "inputs", lambda self: [], raising=False)
    render[nodes[1].path()] = smooth
    render[nodes[0].path()] = plain
    assert fn.subdivision_problem(smooth) == "" and fn.status_text(smooth) == fn.UP_TO_DATE
    assert fn.subdivision_problem(plain) == ""
    # The object's Cycles subdivision no longer matches the part.
    nodes[1].parm("cycles_subdivision_render_level").set(1)
    warning = fn.subdivision_problem(smooth)
    assert warning.startswith("Warning:") and "Apply Subdivision" in warning and "own object" in warning
    assert fn.status_text(smooth) == fn.UP_TO_DATE + ". " + warning
    nodes[1].parm("cycles_subdivision_render_level").set(2)
    # Merged with other geometry before the render node.
    render[nodes[1].path()] = _Sop("merge", smooth, _Sop("box"))
    assert "merged with other geometry" in fn.subdivision_problem(smooth)
    # Link mode never warns.
    smooth.parm("mode").set("link")
    assert fn.subdivision_problem(smooth) == ""


def test_network_categories_compare_by_value(assets, monkeypatch):
    """Defect found live: HOM returns a new NodeTypeCategory wrapper per call, so the drop target
    test must compare categories with ==, never `is` (a SOP drop landed in /obj)."""
    from cycles_houdini.blender import blend_convert

    class Category:
        def __init__(self, name):
            self.name = name

        def __eq__(self, other):
            return isinstance(other, Category) and other.name == self.name

        __hash__ = None

    monkeypatch.setattr(assets.hou, "sopNodeTypeCategory", lambda: Category("sop"))
    monkeypatch.setattr(assets.hou, "objNodeTypeCategory", lambda: Category("obj"))

    def category(self):
        if self.path() == "/obj":
            return Category("obj")
        return Category("sop") if self.type_name == "geo" else None

    monkeypatch.setattr(cases.Node, "childTypeCategory", category, raising=False)
    target = assets.obj.createNode("geo", "mine")
    assert blend_convert.is_sop_network(assets.hou, target)
    assert not blend_convert.is_sop_network(assets.hou, assets.obj)
    assert blend_convert.object_network(assets.hou, target) is assets.obj
    snapshot = {"parent": target.path()}
    assert blend_convert._parent_network(assets.hou, snapshot, sop=True) is target
    assert blend_convert._parent_network(assets.hou, snapshot) is assets.obj
