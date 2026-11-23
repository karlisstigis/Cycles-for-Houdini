# SPDX-License-Identifier: Apache-2.0
"""Headless SOP/menu integration checks; synthetic metadata never launches Blender.

Run with the built package on HOUDINI_PATH/PYTHONPATH, e.g. hython this.py --out DIR.
"""

import argparse
import json
import sys
from pathlib import Path
import threading
import time

import hou
from cycles_houdini.blender import blend_catalogue as catalogue, blend_worker
from cycles_houdini.blender.blend_targets import resolve_targets
from cycles_houdini.core import blender_bridge

parser = argparse.ArgumentParser()
parser.add_argument("--out", required=True, type=Path)
parser.add_argument("--legacy-hip", type=Path, help="HIP saved by the previous production runtime")
parser.add_argument("--legacy-defaults-hip", type=Path, help="Previous runtime HIP with omitted default expressions")
args = parser.parse_args()
args.out.mkdir(parents=True, exist_ok=True)
hou.hipFile.setName(str(args.out / "catalogue ünicode.hip"))
source = args.out / "source 片 ü.blend"
override = args.out / "override ü.blend"
source.write_bytes(b"Synthetic metadata fixture")
override.write_bytes(b"Synthetic override fixture")
objects = ["Cube", "Rock Large", "Rock Small", "Object 片 ü", 'Quote " and slash \\']
collections = ["Collection", "集合 ü"]
calls = []
main_thread = threading.get_ident()
original_query = blend_worker.catalogue_many
original_discover = blender_bridge._discover


def query(sources, executable, **kwargs):
    assert threading.get_ident() != main_thread
    calls.append(tuple(sources))
    return [
        dict(source=str(path), signature=blend_worker._signature(path), objects=objects, collections=collections)
        for path in sources
    ]


def pump(job):
    deadline = time.monotonic() + 10
    while not job["done"].is_set():
        if time.monotonic() > deadline:
            raise TimeoutError("Catalogue test did not complete")
        catalogue.drain_catalogues()
        time.sleep(0.005)
    catalogue.drain_catalogues()
    assert not job["error"], job["error"]


blend_worker.catalogue_many = query
blender_bridge._discover = lambda *args: str(Path(sys.executable))
try:
    geo = hou.node("/obj").createNode("geo", "reference_ui", run_init_scripts=False)
    current = geo.createNode("cycles::blender_reference", "simplified_reference")
    frame_template = current.parm("ref_frame1").parmTemplate()
    assert frame_template.defaultValue() == (1.0,)
    assert frame_template.minValue() == 1 and frame_template.maxValue() == 100
    assert not frame_template.minIsStrict() and not frame_template.maxIsStrict()
    for sample in (-12.75, 0.25, 500.125):
        current.parm("ref_frame1").set(sample)
        assert current.evalParm("ref_frame1") == sample
    current.parm("ref_frame1").set(1)
    # Creation hooks can be bypassed by scripts and interactive asset tools.
    bare = geo.createNode("cycles::blender_reference", "without_creation_hook", run_init_scripts=False)
    bare.parm("references").set(2)
    for i in (1, 2):
        assert bare.evalParm(f"ref_frame{i}") == 1
        assert not bare.parm(f"ref_frame{i}").keyframes()
    bare.destroy()
    assert current.evalParm("multiple_references") == 1
    assert current.evalParm("references") == 1
    assert current.evalParm("ref_frame1") == 1
    assert not current.parm("ref_frame1").keyframes()
    current.parm("references").set(2)
    catalogue.initialize_rows(current)
    assert current.evalParm("ref_frame2") == 1
    assert not current.parm("ref_frame2").keyframes()
    current.parm("ref_frame_initialized2").set(0)
    current.parm("ref_frame2").set(7)
    catalogue.initialize_rows(current)
    assert current.evalParm("ref_frame2") == 7
    current.parm("ref_enabled2").set(0)
    current.parm("ref_frame_initialized2").set(0)
    current.parm("ref_frame2").setExpression("$F + 2", hou.exprLanguage.Hscript)
    catalogue.initialize_rows(current)
    assert current.parm("ref_frame2").expression() == "$F + 2"
    current.parm("references").set(1)
    for token in ("target", "target_kind", "frame", "multiple_references", "refresh_targets", "ref_name1", "ref_file1"):
        assert current.parm(token).parmTemplate().isHidden(), token
    assert current.parm("refresh_bounds").parmTemplate().label() == "Refresh"
    assert current.evalParm("viewport_shape") == "bounds"
    assert current.parm("viewport_shape").menuItems() == ("bounds", "hull")
    assert current.parm("viewport_shape").menuLabels() == ("Bounding Box", "Convex Hull")
    assert current.evalParm("ref_hull_per_member1") == 0
    disabled = current.parm("ref_include_disabled_collections1")
    assert disabled.parmTemplate().label() == "Include Disabled Collections"
    assert current.evalParm("ref_include_disabled_collections1") == 0
    current.updateParmStates()
    assert current.parm("ref_hull_per_member1").isHidden()
    assert disabled.isHidden()
    current.parm("ref_target_kind1").set("collection")
    current.updateParmStates()
    assert not current.parm("ref_hull_per_member1").isHidden()
    assert not disabled.isHidden()
    current.parm("ref_target_kind1").set("object")
    for token in ("file", "references", "ref_enabled1", "ref_target_kind1", "ref_target1", "ref_frame1"):
        assert not current.parm(token).parmTemplate().isHidden(), token
    # Explicit legacy mode exercises parameter tokens retained for older HIPs.
    node = geo.createNode("cycles::blender_reference")
    node.parm("multiple_references").set(0)
    node.parm("references").set(0)
    node.parm("references_initialized").set(0)
    node.parm("frame").deleteAllKeyframes()
    node.setParms(dict(file=str(source), target="Cube", frame=1.25))
    # HOM does not expose PRM_CHOICELIST_APPEND: native APPEND is reported Normal.
    # The actual append action is exercised through the real chooser GUI gate.
    reported_menu_type = str(node.parm("target").parmTemplate().menuType())
    assert node.parm("target").menuItems() == ()
    pump(catalogue.request_catalogues(node))
    assert [resolve_targets(value, objects)[0][0] for value in node.parm("target").menuItems()] == objects
    assert list(node.parm("target").menuLabels()) == [value.replace(" ", "_") for value in objects]
    node.parm("target_kind").set("collection")
    assert [resolve_targets(value, collections)[0][0] for value in node.parm("target").menuItems()] == collections
    # Merely reading menus/cooking, including a typed missing value, never queries.
    node.parm("target").set('Typed Missing 片 "name"')
    before = len(calls)
    node.parm("target").menuItems()
    node.cook(force=True)
    assert node.geometry().findPrimAttrib("name") is not None
    assert node.evalParm("target") == 'Typed Missing 片 "name"'
    assert len(calls) == before
    node.parm("target").setExpression('"集合 ü"', hou.exprLanguage.Python)
    node.parm("frame").setExpression("hou.frame() + .25", hou.exprLanguage.Python)
    node.parm("multiple_references").set(1)
    catalogue.initialize_rows(node)
    assert node.parm("ref_target1").expression() == node.parm("target").expression()
    assert node.parm("ref_frame1").expression() == node.parm("frame").expression()
    assert node.evalParm("ref_target_kind1") == "collection"
    first_name = "first_piece"
    node.parm("ref_name1").set(first_name)
    # Duplicate prototypes have independent packed prims and independent points.
    node.parm("references").set(3)
    catalogue.initialize_rows(node)
    for i in (2, 3):
        node.setParms({f"ref_target{i}": "Cube", f"ref_target_kind{i}": "object", f"ref_name{i}": f"legacy_piece_{i}"})
    node.parm("ref_file3").set(str(override))
    pump(catalogue.request_catalogues(node))
    assert [resolve_targets(value, objects)[0][0] for value in node.parm("ref_target3").menuItems()] == objects
    geometry = node.geometry()
    assert len(geometry.prims()) == len(geometry.points()) == 3
    assert geometry.prim(2).intrinsicValue("cycles_blender_reference") == str(override) + "#object=Cube"
    assert len(set(geometry.primStringAttribValues("name"))) == 3
    assert len({primitive.points()[0].number() for primitive in geometry.prims()}) == 3
    node.setParms(dict(ref_enabled3=0, ref_file3="missing-file.blend"))
    assert len(node.geometry().prims()) == 2
    active = catalogue.reference_rows(node)
    assert len(active) == 2 and all(row["file"] == str(source) for row in active)
    node.parm("ref_name2").set(first_name)
    try:
        node.cook(force=True)
    except hou.OperationFailed:
        pass
    assert not node.errors(), node.errors()
    assert node.warnings() and any(word in " ".join(node.warnings()).lower() for word in ("duplicate", "shared")), (
        node.warnings()
    )
    node.parm("ref_name2").set("my_unique_片_name")
    node.cook(force=True)
    assert not node.errors()
    node.parm("multiple_references").set(0)
    catalogue.initialize_rows(node)
    assert len(node.geometry().prims()) == 1 and node.evalParm("references") == 3
    node.parm("target").deleteAllKeyframes()
    node.parm("target").set("Changed single selection")
    node.parm("multiple_references").set(1)
    catalogue.initialize_rows(node)
    assert node.evalParm("ref_name1") == first_name
    assert node.parm("ref_target1").expression() == '"集合 ü"'
    # Reorder moves name with its row; a name is never based on the live index.
    group = node.parm("references")
    name_before = node.evalParm("ref_name2")
    group.moveMultiParmInstances([1, 0])
    assert node.evalParm("ref_name1") == name_before
    group.removeMultiParmInstance(1)
    assert node.evalParm("references") == 2
    # Multi-name selectors retain exact Blender identities behind piece aliases.
    current.setParms(dict(file=str(source), ref_target1="Cube Rock*"))
    current.parm("ref_frame1").deleteAllKeyframes()
    current.parm("ref_frame1").set(1.25)
    pump(catalogue.request_catalogues(current))
    current.cook(force=True)
    selected_geometry = current.geometry()
    assert list(selected_geometry.primStringAttribValues("name")) == ["Cube", "Rock_Large", "Rock_Small"]
    assert [primitive.intrinsicValue("cycles_blend_target") for primitive in selected_geometry.prims()] == [
        "Cube",
        "Rock Large",
        "Rock Small",
    ]
    assert current.parm("ref_target1").evalAsString() == "Cube Rock*"
    current.parm("ref_target1").set("Rock_Large")
    current.cook(force=True)
    assert current.geometry().prim(0).intrinsicValue("cycles_blend_target") == "Rock Large"
    current_path = current.path()
    node_path = node.path()
    hip = args.out / "catalogue ünicode.hip"
    hou.hipFile.save(str(hip))
    count = len(calls)
    catalogue.invalidate()
    catalogue._cache.clear()
    hou.hipFile.load(str(hip), suppress_save_prompt=True)
    restored = hou.node(node_path)
    restored.parm("ref_target1").menuItems()
    restored.cook(force=True)
    reopened_current = hou.node(current_path)
    reopened_current.cook(force=True)
    assert reopened_current.geometry().prim(0).intrinsicValue("cycles_blend_target") == "Rock Large"
    assert len(calls) == count, "HIP reopen launched a catalogue query"
    assert [resolve_targets(value, objects)[0][0] for value in restored.parm("ref_target1").menuItems()] == objects
    if args.legacy_hip:
        # This fixture must come from the previous runtime, not this test's save.
        count_before_legacy_load = len(calls)
        hou.hipFile.load(str(args.legacy_hip.resolve()), suppress_save_prompt=True)
        single = hou.node("/obj/legacy_references/single")
        multiple = hou.node("/obj/legacy_references/multiple")
        assert single is not None and multiple is not None
        assert single.parm("frame").expression() == "$F + 0.25"
        assert single.parm("target").unexpandedString() == "Wine Bottle"
        assert single.parm("file").unexpandedString() == "$HIP/assets/legacy.blend"
        single.cook(force=True)
        assert len(single.geometry().prims()) == 1
        assert single.geometry().prim(0).intrinsicValue("cycles_blend_target") == "Wine Bottle"
        for i, target in ((1, "Bottle A"), (2, "Bottle B")):
            assert multiple.parm(f"ref_target{i}").unexpandedString() == target
            assert multiple.parm(f"ref_frame{i}").expression() == f"$F + {i}"
            assert multiple.evalParm(f"ref_name{i}") == f"authored_piece_{i}"
        assert multiple.parm("ref_file2").unexpandedString() == "$HIP/assets/override.blend"
        multiple.cook(force=True)
        assert list(multiple.geometry().primStringAttribValues("name")) == ["authored_piece_1", "authored_piece_2"]
        assert [p.intrinsicValue("cycles_blend_target") for p in multiple.geometry().prims()] == [
            "Bottle A",
            "Bottle B",
        ]
        assert len(calls) == count_before_legacy_load, "Old-runtime HIP reopen launched Blender"
    if args.legacy_defaults_hip:
        before_defaults_load = len(calls)
        hou.hipFile.load(str(args.legacy_defaults_hip.resolve()), suppress_save_prompt=True)
        old_single = hou.node("/obj/legacy_defaults/single_default")
        old_multi = hou.node("/obj/legacy_defaults/multi_default")
        dormant = hou.node("/obj/legacy_defaults/single_with_dormant")
        # Omitted defaults adopt the actual new default; explicit expressions
        # in the previous fixture above retain their complete channel state.
        assert old_single.evalParm("frame") == 1
        assert old_single.evalParm("ref_frame1") == 1
        for i in (1, 2):
            assert old_multi.evalParm(f"ref_frame{i}") == 1
            assert old_multi.evalParm(f"ref_name{i}") == f"piece_{i}"
        assert dormant.evalParm("references") == 2
        assert dormant.evalParm("ref_target1") == "Active Single"
        assert dormant.evalParm("ref_target2") == "Dormant Bottle"
        assert dormant.evalParm("ref_name2") == "dormant_piece"
        assert dormant.parm("ref_file2").unexpandedString() == "$HIP/assets/dormant.blend"
        assert not dormant.evalParm("ref_enabled2")
        migration = json.loads(dormant.parm("saved_reference_migration").unexpandedString())
        assert migration["disabled_channels"][0]["row"] == 2
        assert "$F > 2" in migration["disabled_channels"][0]["code"]
        for restored_node in (old_single, old_multi, dormant):
            restored_node.cook(force=True)
            assert not restored_node.errors(), restored_node.errors()
        assert len(dormant.geometry().prims()) == 1
        assert dormant.geometry().prim(0).intrinsicValue("cycles_blend_target") == "Active Single"
        assert len(calls) == before_defaults_load, "Omitted-default HIP reopen launched Blender"
    report = dict(
        result="passed",
        metadata_batches=count,
        unicode=True,
        expressions=True,
        actual_dynamic_menu=True,
        reported_menu_type=reported_menu_type,
        simplified_ui=True,
        multi_name_wildcards=True,
        normalized_exact_binding=True,
        disabled_rows_and_duplicate_warnings=True,
        multiple_references=True,
        hip_reopen_no_blender=True,
        old_runtime_hip=bool(args.legacy_hip),
        old_runtime_defaults_hip=bool(args.legacy_defaults_hip),
    )
    (args.out / "reference-ui.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)
finally:
    catalogue.invalidate()
    blend_worker.catalogue_many = original_query
    blender_bridge._discover = original_discover
