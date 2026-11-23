# SPDX-License-Identifier: Apache-2.0
"""Scene notifications, editor state, and observer ownership across ROP deletion."""

import pass_fixtures as pass_fixture

import argparse
import json
import traceback

import hou

from cycles_houdini import native_scene, passes
from environment_recovery_fixture import Fixture

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
f = Fixture(parser.parse_args().device)
events = []
report = dict(result="running", checks=[])
observer = native_scene.SceneObserver(
    f.rop,
    lambda: events.append("scene"),
    camera_callback=lambda: events.append("camera"),
    object_pose_callback=lambda: events.append("pose"),
)
before_cooks = f.grid.cookCount()


def change(name, callback, expected):
    events.clear()
    callback()
    assert (expected in events) if expected else not events, (name, expected, events)
    assert f.grid.cookCount() == before_cooks, "Observer notification cooked SOP geometry"
    report["checks"].append(dict(name=name, events=list(events)))


def verify_fingerprint(node):
    names = ("t", "r", "s", "p", "pr", "scale", "xOrd", "rOrd")
    cached = observer._non_transform_fingerprint(node, names)
    # The oracle has no cache or callbacks and cannot repair the live observer's layout.
    assert cached == object.__new__(type(observer))._non_transform_fingerprint(node, names)


def spare_tuple_fingerprints():
    node = f.geo
    middle, renamed = "observer_middle", "observer_renamed"
    node.addSpareParmTuple(hou.FloatParmTemplate(middle, "Middle", 1))
    node.addSpareParmTuple(hou.FloatParmTemplate("observer_tail", "Tail", 1))
    observer.remember_parameters([node])
    layout = node.parmTuples()
    edge = len(layout), layout[-1].name()
    kinds = getattr(observer, "_parm_kinds", {}).get(node.sessionId())
    change("Object transform with spare tuples remains pose-only", lambda: node.parm("tx").set(0.31), "pose")
    assert events == ["pose"]
    assert getattr(observer, "_parm_kinds", {}).get(node.sessionId()) is kinds

    def replace(old, template):
        group = node.parmTemplateGroup()
        group.replace(old, template)
        node.setParmTemplateGroup(group)

    def verify_layout():
        tuples = node.parmTuples()
        assert (len(tuples), tuples[-1].name()) == edge
        verify_fingerprint(node)

    edits = (
        ("Same-count spare tuple rename", lambda: replace(middle, hou.FloatParmTemplate(renamed, "Renamed", 1))),
        ("Same-name spare tuple replacement", lambda: replace(renamed, hou.StringParmTemplate(renamed, "Text", 1))),
        ("Spare string changes to backtick expression", lambda: node.parm(renamed).set("`$F`")),
    )
    for name, edit in edits:
        with hou.undos.group(name):
            change(name, edit, "scene")
        verify_layout()
    if hou.isUIAvailable():
        for name, _ in reversed(edits):
            assert hou.undos.undoLabels()[0] == name
            change("Undo " + name, lambda: hou.undos.performUndo(), "scene")
            assert hou.undos.redoLabels()[0] == name
            verify_layout()
        assert node.parm(middle) is not None and node.parm(renamed) is None


def balanced_multiparm_fingerprint():
    node = f.geo
    a, b, tail = "observer_a", "observer_b", "observer_tail_count"
    for name in (a, b, tail):
        node.addSpareParmTuple(
            hou.FolderParmTemplate(
                name,
                name,
                (hou.FloatParmTemplate(name + "_item#", "Item", 1),),
                folder_type=hou.folderType.MultiparmBlock,
            )
        )
    node.parm(a).set(1)
    node.parm(tail).set(1)
    observer.remember_parameters([node])
    before = tuple(item.name() for item in node.parmTuples())

    def move_instance():
        node.parm(a).set(0)
        node.parm(b).set(1)
        # HOM appends new instances at the end of parmTuples(), even if their
        # block precedes this one. Restore the tail tuple before sampling.
        node.parm(tail).set(0)
        node.parm(tail).set(1)

    change("Balance two multiparm edits without changing tuple count or tail", move_instance, "scene")
    after = tuple(item.name() for item in node.parmTuples())
    assert (len(before), before[-1]) == (len(after), after[-1]), (before, after)
    assert set(before) - set(after) == {a + "_item1"} and set(after) - set(before) == {b + "_item1"}
    verify_fingerprint(node)
    change(
        "Unnamed notification after balanced multiparm edits takes the scene route",
        lambda: observer._callback(node=node, event_type=hou.nodeEventType.ParmTupleChanged, parm_tuple=None),
        "scene",
    )


try:
    # Houdini's scene root reports noneditable even though it is not inside a
    # locked asset. COP dependency discovery includes that root.
    for path in ("/", "/obj", "/img", "/out", "/mat"):
        node = hou.node(path)
        assert native_scene._observable_source_node(node) == node, path
    report["checks"].append(dict(name="Scene root and manager COP dependencies retain their observer identities"))
    change(
        "Native light contribution count is a scene edit",
        lambda: pass_fixture.parm(f.env, "light_contrib").set(1),
        "scene",
    )
    change(
        "Removing native contribution entries is a scene edit",
        lambda: pass_fixture.parm(f.env, "light_contrib").set(0),
        "scene",
    )
    for kind in ("aov_color", "lightgroup"):
        parm = f.rop.parm("pass_count")
        assert parm.parmTemplate().type() == hou.parmTemplateType.Folder and parm.isMultiParmParent()
        before = parm.eval()
        change(
            "ROP pass_count adds " + kind,
            lambda kind=kind: passes.add(f.rop, kind, source="observer_" + kind),
            "scene",
        )
        assert parm.eval() == before + 1
        change(
            "ROP pass_count removes " + kind,
            lambda p=parm, index=before: p.removeMultiParmInstance(index),
            "scene",
        )
        assert parm.eval() == before
    # Native count styles all use FolderParmTemplate, just like UI-only headers.
    for name in ("MultiparmBlock", "ScrollingMultiparmBlock", "TabbedMultiparmBlock"):
        folder = hou.FolderParmTemplate(
            "observer_" + name,
            "Observer " + name,
            parm_templates=(hou.FloatParmTemplate("value_" + name + "#", "Value", 1),),
            folder_type=getattr(hou.folderType, name),
        )
        f.geo.addSpareParmTuple(folder)
        observer.refresh()
        parm = pass_fixture.parm(f.geo, folder.name())
        assert parm.isMultiParmParent()
        change(name + " count edit", lambda p=parm: p.set(1), "scene")
        change(name + " inserted instance", lambda p=parm: p.insertMultiParmInstance(0), "scene")
        change(name + " removed instance", lambda p=parm: p.removeMultiParmInstance(0), "scene")
        parm.set(0)
    tabs = pass_fixture.parm(f.rop, "world_source").containingFolderSetParmTuples()[0]
    original = tabs.eval()
    change("Native tab selection is editor state", lambda: tabs.set((0 if original[0] else 1,)), None)
    change("Native tab restoration is editor state", lambda: tabs.set(original), None)
    # Native collapsible headers have no scene-data parameter in this ROP.
    # Their expansion is UI state; the real folder-set selection above checks
    # host editor notifications without inventing a hidden compatibility parm.
    persistent = pass_fixture.parm(f.rop, "persistent_data")
    original = persistent.eval()
    change("Final Persistent Data remains outside IPR scene changes", lambda: persistent.set(not original), None)
    # Cook-driven private parameter rewrites must not requeue an unchanged
    # scene. Keep the public asset boundary and upstream/editable nodes live.
    asset = f.geo.createNode("subnet", "observer_boundary")
    asset.addSpareParmTuple(hou.FloatParmTemplate("public_value", "Public Value", 1))
    inside = asset.createNode("xform", "private_transform")
    inside.setInput(0, asset.indirectInputs()[0])
    inside.setDisplayFlag(True)
    inside.setRenderFlag(True)
    asset = asset.createDigitalAsset(
        name="cycles_test_observer_boundary", save_as_embedded=True, min_num_inputs=1, max_num_inputs=1
    )
    asset.matchCurrentDefinition()
    asset.setInput(0, f.grid)
    asset.setDisplayFlag(True)
    asset.setRenderFlag(True)
    inside = asset.node("private_transform")
    assert asset.isLockedHDA() and not inside.isEditableInsideLockedHDA()
    observer.refresh()
    watched_nodes = {node for node, _ in observer.nodes}
    assert asset in watched_nodes and inside not in watched_nodes and f.grid in watched_nodes
    change(
        "Locked asset public parameters remain observed",
        lambda: pass_fixture.parm(asset, "public_value").set(1),
        "scene",
    )
    change("Locked asset upstream geometry remains observed", lambda: f.grid.parmTuple("size").set((3, 2)), "scene")
    asset.allowEditingOfContents()
    observer.refresh()
    inside = asset.node("private_transform")
    assert inside.isEditableInsideLockedHDA() and inside in {node for node, _ in observer.nodes}
    change(
        "Unlocked asset internal parameter edits remain observed",
        lambda: pass_fixture.parm(inside, "tx").set(0.25),
        "scene",
    )
    asset.matchCurrentDefinition()
    observer.refresh()
    assert asset.node("private_transform") not in {node for node, _ in observer.nodes}
    change(
        "Relocked asset public parameters remain observed",
        lambda: pass_fixture.parm(asset, "public_value").set(2),
        "scene",
    )
    asset.destroy()
    f.grid.setDisplayFlag(True)
    f.grid.setRenderFlag(True)
    observer.refresh()
    change(
        "Camera transform retains its lightweight notification",
        lambda: pass_fixture.parm(f.camera, "tx").set(0.25),
        "camera",
    )
    change("Object transform retains its pose notification", lambda: pass_fixture.parm(f.geo, "tx").set(0.25), "pose")
    spare_tuple_fingerprints()
    balanced_multiparm_fingerprint()
    change("Deleting a source keeps the live ROP observing repairs", lambda: f.env.destroy(), "scene")
    assert not observer._closed and observer.nodes
    change("Recreating a source remains observed", lambda: f.scene.createNode("envlight", "environment"), "scene")
    second_events = []
    second = native_scene.SceneObserver(f.rop, lambda: second_events.append("scene"))
    watched = tuple(observer.nodes) + tuple(second.nodes)
    late_callbacks = (observer._callback, second._callback)
    f.rop.destroy()
    assert observer._closed and second._closed and not observer.nodes and not second.nodes
    events.clear()
    second_events.clear()
    # A callback already dispatched by Houdini can arrive after its ROP or
    # source was deleted. It must neither compare stale HOM handles nor rearm.
    for callback in late_callbacks:
        callback(node=f.geo, event_type=hou.nodeEventType.ParmTupleChanged, parm_tuple=f.geo.parmTuple("t"))
        callback(node=f.rop, event_type=hou.nodeEventType.BeingDeleted)
    pass_fixture.parm(f.geo, "tx").set(0.5)
    for node, _ in watched:
        try:
            callbacks = node.eventCallbacks()
        except hou.ObjectWasDeleted:
            continue
        assert not any(callback in late_callbacks for _, callback in callbacks)
    assert not events and not second_events
    observer.selection_changed()
    observer.refresh_material_bindings(None)
    observer.refresh()
    observer.close()
    observer.close()
    second.close()
    second.close()
    assert observer._closed and not observer.nodes
    report["checks"].append(
        dict(name="Both ROP observers detach before source deletion, late events are inert, and close is repeatable")
    )
    report.update(result="passed", geometry_cooks=f.grid.cookCount() - before_cooks)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    observer.close()
    (f.dest / "observer-report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result="passed", checks=len(report["checks"]), geometry_cooks=report["geometry_cooks"])))
