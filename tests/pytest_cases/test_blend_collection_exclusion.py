# SPDX-License-Identifier: Apache-2.0
"""Collection references honour the source file's view-layer exclusion of children."""

import itertools
import sys
from types import SimpleNamespace

import pytest

from cycles_blender import blend_export as export

pytestmark = [pytest.mark.pure, pytest.mark.cpu]

_pointers = itertools.count(1)


class FakeID:
    def __init__(self, name):
        self.name = self.name_full = name
        self.library = None
        self._pointer = next(_pointers)

    def as_pointer(self):
        return self._pointer


class Links(list):
    def link(self, item):
        if any(existing is item for existing in self):
            raise RuntimeError("already linked: " + item.name)
        self.append(item)

    def unlink(self, item):
        self.remove(item)


class Collection(FakeID):
    def __init__(self, name, *objects, children=()):
        super().__init__(name)
        self.objects = Links(objects)
        self.children = Links(children)
        self.instance_offset = (0.0, 0.0, 0.0)
        self.hide_render = False
        self.hide_viewport = False

    @property
    def all_objects(self):
        result = list(self.objects)
        for child in self.children:
            result.extend(obj for obj in child.all_objects if obj not in result)
        return result


class Object(FakeID):
    def __init__(self, name, data=None):
        super().__init__(name)
        self.data = data
        self.instance_type = "NONE"
        self.instance_collection = None


class LayerCollection:
    """Blender-like layer tree: excluding a parent also flags all its children."""

    def __init__(self, collection, excluded, parent_excluded=False):
        self.collection = collection
        self.exclude = parent_excluded or collection.name in excluded
        self.children = [LayerCollection(child, excluded, self.exclude) for child in collection.children]
        self._pointer = next(_pointers)

    def as_pointer(self):
        return self._pointer


class ViewLayer(FakeID):
    def __init__(self, name, master, excluded=()):
        super().__init__(name)
        self.layer_collection = LayerCollection(master, set(excluded))


class Scene(FakeID):
    def __init__(self, name, *children, layers=(("ViewLayer", ()),)):
        super().__init__(name)
        self.collection = Collection("Scene Collection", children=children)
        self.view_layers = [ViewLayer(layer, self.collection, excluded) for layer, excluded in layers]


class DataBlocks(list):
    def __init__(self, factory, items=()):
        super().__init__(items)
        self.factory = factory

    def new(self, name, *args):
        item = self.factory(name, *args)
        self.append(item)
        return item

    def get(self, name):
        return next((item for item in self if item.name == name), None)

    def remove(self, item, do_unlink=True):
        list.remove(self, item)


def fake_bpy(collections, scenes, *, active_layer=None):
    scenes = list(scenes)
    return SimpleNamespace(
        data=SimpleNamespace(
            collections=DataBlocks(Collection, collections),
            objects=DataBlocks(Object),
            scenes=scenes,
        ),
        context=SimpleNamespace(
            scene=scenes[0] if scenes else None,
            view_layer=active_layer or (scenes[0].view_layers[0] if scenes else None),
            window=None,
        ),
    )


def rig_asset():
    body, gear = Object("Body"), Object("Gear")
    shared, widget, bone_shape = Object("Shared"), Object("WGT_root"), Object("WGT_bone")
    widgets = Collection("wdg_widgets", widget, shared, children=[Collection("wdg_nested", bone_shape)])
    parts = Collection("Parts", gear, shared)
    asset = Collection("Alarm Clock", body, children=[widgets, parts])
    asset.instance_offset = (0.0, 0.0, 1.5)
    return asset, widgets, parts


def select(bpy, scene, name="Alarm Clock", **options):
    skipped = []
    request = dict(selector_type="COLLECTION", selector=name, **options)
    export._select(bpy, request, scene, skipped)
    instance = scene.collection.objects[-1]
    assert instance.instance_type == "COLLECTION"
    return instance.instance_collection, skipped


def test_excluded_child_is_skipped_through_a_temporary_copy_keeping_offset_and_originals():
    asset, widgets, parts = rig_asset()
    scene = Scene("Scene", asset, layers=[("ViewLayer", ["wdg_widgets"])])
    bpy = fake_bpy([asset, widgets, parts, *widgets.children], [scene])
    evaluated, skipped = select(bpy, scene)
    assert evaluated is not asset
    assert evaluated.instance_offset == (0.0, 0.0, 1.5)
    assert [obj.name for obj in evaluated.objects] == ["Body"]
    # The untouched child is linked as-is rather than copied.
    assert list(evaluated.children) == [parts]
    assert sorted(obj.name for obj in evaluated.all_objects) == ["Body", "Gear", "Shared"]
    assert skipped == [dict(name="wdg_widgets", parent="Alarm Clock", scene="Scene", view_layer="ViewLayer")]
    # Source data keeps its membership; only new collections were added.
    assert [child.name for child in asset.children] == ["wdg_widgets", "Parts"]
    assert [obj.name for obj in widgets.objects] == ["WGT_root", "Shared"]
    assert [collection.name for collection in bpy.data.collections][-1].startswith("__CH_EXPORT_FILTER__")


def test_include_option_and_unexcluded_files_evaluate_the_original_collection():
    asset, widgets, parts = rig_asset()
    excluded = Scene("Scene", asset, layers=[("ViewLayer", ["wdg_widgets"])])
    bpy = fake_bpy([asset, widgets, parts], [excluded])
    evaluated, skipped = select(bpy, excluded, include_excluded_collections=True)
    assert evaluated is asset and skipped == []
    included = Scene("Included", asset)
    bpy = fake_bpy([asset, widgets, parts], [included])
    evaluated, skipped = select(bpy, included)
    assert evaluated is asset and skipped == []
    assert len(bpy.data.collections) == 3


def test_nested_exclusion_copies_only_the_affected_branch_and_keeps_render_hiding():
    deep = Object("Deep")
    hidden = Collection("Hidden", deep)
    inner = Collection("Inner", Object("InnerMesh"), children=[hidden])
    inner.hide_render = True
    sibling = Collection("Sibling", Object("SiblingMesh"))
    root = Collection("Root", children=[inner, sibling])
    scene = Scene("Scene", root, layers=[("ViewLayer", ["Hidden"])])
    bpy = fake_bpy([root, inner, sibling, hidden], [scene])
    evaluated, skipped = select(bpy, scene, name="Root")
    copied_inner, linked_sibling = evaluated.children
    assert linked_sibling is sibling
    assert copied_inner is not inner and copied_inner.hide_render is True
    assert [obj.name for obj in copied_inner.objects] == ["InnerMesh"]
    assert list(copied_inner.children) == []
    assert skipped == [dict(name="Hidden", parent="Inner", scene="Scene", view_layer="ViewLayer")]


def test_object_reachable_through_an_included_child_stays():
    asset, widgets, parts = rig_asset()
    scene = Scene("Scene", asset, layers=[("ViewLayer", ["wdg_widgets"])])
    bpy = fake_bpy([asset, widgets, parts], [scene])
    evaluated, _ = select(bpy, scene)
    names = [obj.name for obj in evaluated.all_objects]
    assert "Shared" in names and "WGT_root" not in names and "WGT_bone" not in names


def test_collection_outside_every_scene_or_only_excluded_keeps_all_children():
    asset, widgets, parts = rig_asset()
    unlinked = Scene("Scene")
    bpy = fake_bpy([asset, widgets, parts], [unlinked])
    assert select(bpy, unlinked) == (asset, [])
    # An excluded asset collection flags all of its children too, so those flags
    # cannot describe the author's choice for an individual child.
    excluded = Scene("Scene", asset, layers=[("ViewLayer", ["Alarm Clock"])])
    bpy = fake_bpy([asset, widgets, parts], [excluded])
    assert all(layer.exclude for layer in excluded.view_layers[0].layer_collection.children[0].children)
    assert select(bpy, excluded) == (asset, [])


def test_active_view_layer_wins_and_other_layers_or_scenes_are_fallbacks():
    asset, widgets, parts = rig_asset()
    scene = Scene("Scene", asset, layers=[("Lookdev", []), ("Rig", ["wdg_widgets"]), ("Asset Hidden", ["Alarm Clock"])])
    bpy = fake_bpy([asset, widgets, parts], [scene], active_layer=scene.view_layers[1])
    evaluated, skipped = select(bpy, scene)
    assert evaluated is not asset and skipped[0]["view_layer"] == "Rig"
    bpy = fake_bpy([asset, widgets, parts], [scene], active_layer=scene.view_layers[0])
    assert select(bpy, scene) == (asset, [])
    # The asset is excluded in the active layer, so the next layer decides.
    bpy = fake_bpy([asset, widgets, parts], [scene], active_layer=scene.view_layers[2])
    assert select(bpy, scene) == (asset, [])
    # The evaluated scene does not contain the asset; its own scene decides.
    other = Scene("Library", asset, layers=[("ViewLayer", ["wdg_widgets"])])
    empty = Scene("Shot")
    bpy = fake_bpy([asset, widgets, parts], [empty, other])
    evaluated, skipped = select(bpy, empty)
    assert evaluated is not asset and skipped[0]["scene"] == "Library"


def test_session_removes_temporary_collections_on_reselection_and_close(monkeypatch):
    asset, widgets, parts = rig_asset()
    scene = Scene("Scene", asset, layers=[("ViewLayer", ["wdg_widgets"])])
    scene.camera, scene.frame_current, scene.frame_subframe = None, 1, 0.0
    scene.render = SimpleNamespace(engine="CYCLES")
    scene.frame_set = lambda frame, subframe=0.0: None
    bpy = fake_bpy([asset, widgets, parts], [scene])
    bpy.data.cameras = DataBlocks(Object)
    bpy.types = SimpleNamespace(RenderEngine=object)
    registered = []
    bpy.utils = SimpleNamespace(register_class=registered.append, unregister_class=registered.remove)
    monkeypatch.setitem(sys.modules, "bpy", bpy)
    session = export._EvaluationSession()
    request = dict(selector_type="COLLECTION", selector="Alarm Clock")
    session.select(request, scene)
    assert [skipped["name"] for skipped in session.selection_skipped] == ["wdg_widgets"]
    assert len(session.selection_collections) == 1
    # Reusing the selection keeps it; switching the option replaces it.
    assert session.select(dict(request), scene) is session.selection
    session.select(dict(request, include_excluded_collections=True), scene)
    assert session.selection_skipped == [] and session.selection_collections == []
    assert [collection.name for collection in bpy.data.collections] == ["Alarm Clock", "wdg_widgets", "Parts"]
    session.select(request, scene)
    session.close()
    assert [collection.name for collection in bpy.data.collections] == ["Alarm Clock", "wdg_widgets", "Parts"]
    assert list(bpy.data.objects) == [] and list(scene.collection.objects) == [] and registered == []
