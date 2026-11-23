# SPDX-License-Identifier: Apache-2.0
"""File > Import > Blender (.blend) listing: worker reply checking, host command, in-Blender listing with fakes."""

import json
import os
from collections import deque
from types import SimpleNamespace

import pytest
from cycles_blender import blend_list_contents, worker_server
from cycles_houdini.blender import blend_worker

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


def reply(**overrides):
    value = {
        "ok": True,
        "active_scene": "Scene",
        "scenes": [
            {
                "name": "Scene",
                "view_layers": ["ViewLayer"],
                "active_view_layer": "ViewLayer",
                "collections": ["Props"],
                "objects": ["Root"],
            }
        ],
        "collections": [
            {
                "name": "Props",
                "parent": None,
                "children": ["Child"],
                "objects": ["Rock"],
                "excluded": False,
                "hide_render": False,
                "library": None,
            },
            {
                "name": "Child",
                "parent": "Props",
                "children": [],
                "objects": ["Scatter"],
                "excluded": True,
                "hide_render": True,
                "library": None,
            },
        ],
        "objects": [
            {
                "name": "Rock",
                "type": "MESH",
                "parent": None,
                "collections": ["Props"],
                "has_geometry_nodes": False,
                "material_slots": ["Red", ""],
                "hide_render": False,
            },
            {
                "name": "Scatter",
                "type": "MESH",
                "parent": "Rock",
                "collections": ["Child"],
                "has_geometry_nodes": True,
                "material_slots": [],
                "hide_render": False,
            },
            {
                "name": "Root",
                "type": "EMPTY",
                "parent": None,
                "collections": [],
                "has_geometry_nodes": False,
                "material_slots": [],
                "hide_render": False,
            },
        ],
        "frame_range": [10, 20],
        "fps": 25,
    }
    value.update(overrides)
    return value


# ---------------------------------------------------------------- checked_listing


def test_checked_listing_normalizes_contract_and_defaults_extensions():
    result = blend_worker.checked_listing(reply())
    assert result["active_scene"] == "Scene" and result["frame_range"] == [10, 20] and result["fps"] == 25.0
    props, child = result["collections"]
    assert props["parents"] == [] and props["in_scene"] is True and props["parent"] is None
    assert child["parents"] == ["Props"] and child["excluded"] is True and child["hide_render"] is True
    rock, scatter, root = result["objects"]
    assert rock["material_slots"] == ["Red", ""] and rock["instance_collection"] is None and rock["library"] is None
    assert scatter["parent"] == "Rock" and scatter["has_geometry_nodes"] is True
    assert result["scenes"][0]["objects"] == ["Root"]
    assert "signature" not in result
    json.dumps(result)


def test_checked_listing_drops_dangling_references_and_duplicates():
    data = reply()
    data["collections"][0]["children"] += ["Ghost", "Child"]
    data["collections"][0]["objects"].append("Missing")
    data["collections"][1]["parent"] = "Ghost"
    data["objects"][0]["collections"].append("Ghost")
    data["objects"][1]["parent"] = "Ghost"
    data["objects"].append(dict(data["objects"][0], type="CURVE"))  # same name and library: dropped
    data["objects"].append(dict(data["objects"][0], library="//lib.blend"))  # linked twin: kept
    data["scenes"][0]["collections"].append("Ghost")
    result = blend_worker.checked_listing(data)
    props, child = result["collections"]
    assert props["children"] == ["Child"] and props["objects"] == ["Rock"]
    assert child["parent"] is None and child["parents"] == []
    assert [o["name"] for o in result["objects"]] == ["Rock", "Scatter", "Root", "Rock"]
    assert result["objects"][0]["type"] == "MESH" and result["objects"][3]["library"] == "//lib.blend"
    assert result["objects"][0]["collections"] == ["Props"] and result["objects"][1]["parent"] is None
    assert result["scenes"][0]["collections"] == ["Props"]


def test_checked_listing_parent_joins_parents_and_fallbacks():
    data = reply(frame_range=[30, "x"], fps=float("nan"), active_scene="Nope")
    data["collections"][1]["parents"] = ["Props"]
    data["collections"].append(
        {"name": "Shared", "parent": "Child", "parents": ["Props"], "children": [], "objects": [], "excluded": False}
    )
    data["scenes"][0]["active_view_layer"] = "Missing"
    result = blend_worker.checked_listing(data)
    assert result["collections"][2]["parents"] == ["Child", "Props"] and result["collections"][2]["parent"] == "Child"
    assert result["frame_range"] == [1, 1] and result["fps"] == 24.0
    assert result["active_scene"] == "Scene" and result["scenes"][0]["active_view_layer"] == "ViewLayer"
    assert blend_worker.checked_listing(reply(frame_range=[20, 5]))["frame_range"] == [5, 20]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(collections={}),
        lambda d: d["collections"][0].update(name=""),
        lambda d: d["collections"][0].update(name="a\0b"),
        lambda d: d["collections"][0].update(excluded="yes"),
        lambda d: d["objects"][0].update(type=3),
        lambda d: d["objects"][0].update(material_slots=[1]),
        lambda d: d["objects"][0].update(collections="Props"),
        lambda d: d["scenes"].append("Scene"),
        lambda d: d["objects"].append([]),
        lambda d: d.update(objects=[{"name": "x", "type": "MESH"}] * 200001),
    ],
)
def test_checked_listing_rejects_malformed(mutate):
    data = reply()
    mutate(data)
    with pytest.raises(ValueError):
        blend_worker.checked_listing(data)
    with pytest.raises(ValueError):
        blend_worker.checked_listing([data])


# ---------------------------------------------------------------- host command over a fake pipe


class _Endpoint:
    def __init__(self):
        self.frames = deque()

    def send_bytes(self, data):
        self.frames.append(bytes(data))

    def recv_bytes(self, limit):
        return self.frames.popleft()


class _HostConnection:
    def __init__(self, log):
        self.inbox = _Endpoint()
        self.log = log

    def send_bytes(self, data):
        command = json.loads(bytes(data))
        self.log.append(command)
        worker_server._reply_asset_command(self.inbox, command)

    def recv_bytes(self, limit):
        return self.inbox.recv_bytes(limit)


@pytest.fixture
def fake_worker(monkeypatch):
    state = SimpleNamespace(commands=[], launched=[], closed=0)

    class Worker:
        def __init__(self, source, executable, source_config, timeout, cancelled=None):
            state.launched.append((source, executable, timeout, cancelled))
            self.connection = _HostConnection(state.commands)
            self.logs = deque(maxlen=8)

        def _bounded(self, operation, timeout, cancelled=None):
            return operation()

        def close(self):
            state.closed += 1

    monkeypatch.setattr(blend_worker, "_Worker", Worker)
    return state


def test_list_blend_contents_runs_one_disposable_read_only_command(fake_worker, monkeypatch, tmp_path):
    source = tmp_path / "scene.blend"
    source.write_bytes(b"BLENDER")
    before = source.read_bytes()
    seen = []

    def list_file(path):
        seen.append(path)
        return reply()

    monkeypatch.setattr(blend_list_contents, "list_file", list_file)
    cancel = object()
    result = blend_worker.list_blend_contents(source, "blender", timeout=30, cancelled=cancel)
    assert fake_worker.commands == [{"command": "list_blend_contents", "source": str(source.resolve())}]
    assert fake_worker.launched == [(str(source.resolve()), "blender", 30, cancel)] and fake_worker.closed == 1
    assert seen == [str(source.resolve())]
    assert result["source"] == str(source.resolve()) and len(result["signature"]) == 4
    assert [c["name"] for c in result["collections"]] == ["Props", "Child"]
    assert source.read_bytes() == before


def test_list_blend_contents_reports_failures(fake_worker, monkeypatch, tmp_path):
    source = tmp_path / "scene.blend"
    source.write_bytes(b"BLENDER")

    def broken(path):
        raise RuntimeError("cannot read file")

    monkeypatch.setattr(blend_list_contents, "list_file", broken)
    with pytest.raises(RuntimeError, match="listing failed in Blender: cannot read file"):
        blend_worker.list_blend_contents(source, "blender")

    def changing(path):
        source.write_bytes(b"BLENDER changed")
        os.utime(source, ns=(5, 5))
        return reply()

    monkeypatch.setattr(blend_list_contents, "list_file", changing)
    with pytest.raises(RuntimeError, match="changed during listing"):
        blend_worker.list_blend_contents(source, "blender")

    monkeypatch.setattr(blend_list_contents, "list_file", lambda path: {"scenes": "bad"})
    with pytest.raises(ValueError, match="Malformed Blender listing"):
        blend_worker.list_blend_contents(source, "blender")
    assert fake_worker.closed == 3


def test_list_blend_contents_validates_before_launch(monkeypatch, tmp_path):
    monkeypatch.setattr(blend_worker, "_Worker", lambda *a, **k: pytest.fail("launched Blender"))
    text = tmp_path / "scene.txt"
    text.write_text("x")
    for source in (tmp_path / "missing.blend", text):
        with pytest.raises(ValueError, match="existing .blend"):
            blend_worker.list_blend_contents(source, "blender")
    for timeout in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="timeout"):
            blend_worker.list_blend_contents(tmp_path / "a.blend", "blender", timeout=timeout)


def test_worker_dispatches_listing_with_the_asset_commands():
    assert "list_blend_contents" in blend_worker._ASSET_COMMANDS
    inbox = _Endpoint()
    worker_server._reply_asset_command(inbox, {"command": "list_blend_contents", "source": 3})
    assert json.loads(inbox.frames.popleft()) == {"ok": False, "error": "Invalid Blender listing request"}


# ---------------------------------------------------------------- in-Blender listing with fake bpy


class ID:
    _next = 1

    def __init__(self, name, **values):
        self.name = name
        self.library = None
        self.__dict__.update(values)
        ID._next += 1
        self._pointer = ID._next

    def as_pointer(self):
        return self._pointer


def layer(collection, *children, exclude=False):
    return SimpleNamespace(collection=collection, children=list(children), exclude=exclude)


def fake_bpy():
    master = ID("Scene Collection", is_embedded_data=True, children=[], objects=[])
    props = ID("Props", children=[], objects=[], hide_render=False)
    child = ID("Child", children=[], objects=[], hide_render=True)
    shared = ID("Shared", children=[], objects=[], hide_render=False)
    orphan = ID("Orphan", children=[], objects=[], hide_render=False)
    props.children = [child, shared]
    child.children = [shared]
    master.children = [props, shared]
    red = ID("Red")
    gn = SimpleNamespace(type="NODES")
    rock = ID("Rock", type="MESH", parent=None, modifiers=[SimpleNamespace(type="SUBSURF")], hide_render=False)
    rock.material_slots = [SimpleNamespace(material=red), SimpleNamespace(material=None)]
    rock.users_collection = [props]
    scatter = ID("Scatter", type="MESH", parent=rock, modifiers=[gn], material_slots=[], hide_render=True)
    scatter.users_collection = [shared]
    empty = ID("Empty", type="EMPTY", parent=None, modifiers=[], material_slots=[], hide_render=False)
    empty.instance_type, empty.instance_collection = "COLLECTION", shared
    empty.users_collection = [master]
    props.objects, shared.objects, master.objects = [rock], [scatter], [empty]
    # Child is excluded under Props; Shared is reached excluded through Child but included
    # directly under the scene collection, so it counts as included.
    tree = layer(
        master,
        layer(props, layer(child, layer(shared), exclude=True), layer(shared)),
        layer(shared),
    )
    view_layer = ID("ViewLayer", layer_collection=tree)
    other_layer = ID("Other", layer_collection=layer(master))
    render = SimpleNamespace(fps=30, fps_base=1.001)
    scene = ID(
        "Scene", collection=master, view_layers=[view_layer, other_layer], frame_start=3, frame_end=9, render=render
    )
    data = SimpleNamespace(collections=[props, child, shared, orphan], objects=[rock, scatter, empty], scenes=[scene])
    return SimpleNamespace(data=data, context=SimpleNamespace(scene=scene, view_layer=view_layer))


def test_list_contents_reads_tree_exclusion_and_objects():
    result = blend_worker.checked_listing(blend_list_contents.list_contents(fake_bpy()))
    collections = {entry["name"]: entry for entry in result["collections"]}
    assert collections["Child"]["excluded"] is True and collections["Child"]["hide_render"] is True
    assert collections["Shared"]["excluded"] is False and collections["Props"]["excluded"] is False
    assert collections["Shared"]["parents"] == ["Props", "Child"]
    assert collections["Orphan"]["in_scene"] is False and collections["Orphan"]["excluded"] is False
    objects = {entry["name"]: entry for entry in result["objects"]}
    assert objects["Rock"]["material_slots"] == ["Red", ""] and objects["Rock"]["has_geometry_nodes"] is False
    assert objects["Scatter"]["has_geometry_nodes"] is True and objects["Scatter"]["parent"] == "Rock"
    assert objects["Scatter"]["hide_render"] is True
    assert objects["Empty"]["collections"] == [] and objects["Empty"]["instance_collection"] == "Shared"
    [scene] = result["scenes"]
    assert scene["view_layers"] == ["ViewLayer", "Other"] and scene["active_view_layer"] == "ViewLayer"
    assert scene["collections"] == ["Props", "Shared"] and scene["objects"] == ["Empty"]
    assert result["frame_range"] == [3, 9] and result["fps"] == pytest.approx(30 / 1.001)


def test_excluded_parent_propagates_to_children():
    bpy = fake_bpy()
    tree = bpy.context.view_layer.layer_collection
    tree.children = [tree.children[0]]  # Shared only under Props/Child now
    tree.children[0].exclude = True  # Props excluded: every descendant too
    result = blend_list_contents.list_contents(bpy)
    assert all(entry["excluded"] for entry in result["collections"] if entry["in_scene"])
