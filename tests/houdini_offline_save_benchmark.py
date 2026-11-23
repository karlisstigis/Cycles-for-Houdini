# SPDX-License-Identifier: Apache-2.0
"""Secondary archive save timings and complete authored-state change probes.

The baseline replays the former healthy-root capture path (temporary CPIO file,
full metadata traversal, ZIP validation and complete block recompression).
Both paths run through ordinary HIP BeforeSave, including fallback republish.
"""

import argparse
import hashlib
import json
import math
from pathlib import Path
import statistics
import tempfile
import time
import traceback

import hou
import cycles_houdini as ch
import cycles_houdini_offline as offline

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
parser.add_argument("--report", type=Path, required=True)
parser.add_argument("--roots", type=int, default=16)
parser.add_argument("--nodes-per-root", type=int, default=24)
parser.add_argument("--repeats", type=int, default=5)
parser.add_argument("--skip-timings", action="store_true")
args = parser.parse_args()
args.report.parent.mkdir(parents=True, exist_ok=True)
directory = args.report.parent / (args.report.stem + "-scenes")
directory.mkdir(exist_ok=True)
report = dict(
    result="running",
    houdini=hou.applicationVersionString(),
    scope=__doc__,
    roots=args.roots,
    nodes_per_root=args.nodes_per_root,
    timings={},
    probes=[],
    ui_float_absolute_tolerance=1e-10,
)
original_capture = offline.capture


def reference_capture():
    # Synthetic scene deliberately contains unique healthy roots only. Ownership
    # and damaged/review-root behavior are exercised by the offline unit suite.
    offline._prepare_scene_definitions()
    offline._archive_cache = None
    records, payloads = offline._read()
    for node in offline._roots():
        identity = node.userData(offline.MARKER)
        assert identity and not offline._has_damaged_descendants(node, records.get(identity, {}))
        with tempfile.TemporaryDirectory(prefix="cycles-baseline-") as temporary:
            path = Path(temporary) / "graph.cpio"
            node.parent().saveItemsToFile((node,), str(path))
            payload = path.read_bytes()
        records[identity] = dict(
            id=identity,
            path=node.path(),
            type=node.type().name(),
            sha256=hashlib.sha256(payload).hexdigest(),
            nodes=offline._graph_nodes(node),
            connections=offline._graph_connections(node),
        )
        payloads[identity] = payload
    offline._write(records, payloads)


def save(path):
    began = time.perf_counter()
    hou.hipFile.save(str(path), save_to_recent_files=False)
    return time.perf_counter() - began


def measured(label, changed=False):
    rows = {}
    # Alternate order across scenarios; perform warmup to avoid comparing cold
    # native helpers, different payload formats or newly allocated identities.
    implementations = [("optimized", original_capture), ("baseline", reference_capture)]
    if changed:
        implementations.reverse()
    for name, capture in implementations:
        offline.capture = capture
        save(directory / (label + "-" + name + ".hiplc"))
        values = []
        before = offline.statistics()
        for index in range(args.repeats):
            # Cross the native packet clock boundary between ordinary saves.
            # Sleeping is outside the measured wall time of each actual save.
            time.sleep(1.05)
            if changed:
                roots[0].node("value_000").parm("value").set(0.25 + index * 0.001)
            values.append(save(directory / (label + "-" + name + ".hiplc")))
        after = offline.statistics()
        rows[name] = dict(
            seconds=values,
            median_seconds=statistics.median(values),
            statistics={key: after[key] - before[key] for key in after if key in before},
        )
        if name == "optimized":
            rows[name]["exact_byte_reuse_hit_rate"] = (
                rows[name]["statistics"]["unchanged_roots"] / rows[name]["statistics"]["serialized_roots"]
            )
            rows[name]["temporary_files"] = rows[name]["statistics"]["temporary_serializations"]
            rows[name]["temporary_directories"] = rows[name]["statistics"]["temporary_directories"]
            assert rows[name]["statistics"]["temporary_directories"] == args.repeats
            assert rows[name]["statistics"]["temporary_serializations"] == len(roots) * args.repeats
            assert (rows[name]["statistics"]["unchanged_roots"] + rows[name]["statistics"]["refreshed_roots"]) == len(
                roots
            ) * args.repeats, rows[name]
            if report["stable_native_bytes_across_seconds"]:
                assert rows[name]["statistics"]["unchanged_roots"] >= (len(roots) - int(changed)) * args.repeats, rows[
                    name
                ]
        else:
            rows[name]["temporary_files"] = len(roots) * args.repeats
            rows[name]["temporary_directories"] = len(roots) * args.repeats
    rows["median_speedup"] = rows["baseline"]["median_seconds"] / rows["optimized"]["median_seconds"]
    report["timings"][label] = rows
    offline.capture = original_capture


def authored_state(root):
    """Independent restored-value oracle; never compare licensed envelopes."""
    nodes = {}
    for node in (root, *root.allSubChildren(recurse_in_locked_nodes=False)):
        node.updateParmStates()
        nodes[root.relativePathTo(node)] = dict(
            type=node.type().name(),
            parms={
                parm.name(): dict(
                    raw=parm.rawValue(),
                    keys=[key.asCode() for key in parm.keyframes()],
                    locked=parm.isLocked(),
                    disabled=parm.isDisabled(),
                    hidden=parm.isHidden(),
                    at_frames=[repr(parm.evalAtFrame(frame)) for frame in (1, 4, 12)],
                )
                for parm in node.parms()
            },
            templates=node.parmTemplateGroup().asDialogScript(full_info=True),
            userdata={key: value for key, value in node.userDataDict().items() if key != "cycles.offline.review"},
            blocks={
                key: dict(type=node.dataBlockType(key), value=node.dataBlock(key).hex())
                for key in node.dataBlockKeys("")
            },
            comment=node.comment(),
            position=list(node.position()),
            color=list(node.color().rgb()),
            display_comment=node.isGenericFlagSet(hou.nodeFlag.DisplayComment),
            notes=sorted(
                (
                    dict(
                        name=note.name(),
                        text=note.text(),
                        position=list(note.position()),
                        size=list(note.size()),
                        color=list(note.color().rgb()),
                    )
                    for note in (node.stickyNotes() if node.isNetwork() else ())
                ),
                key=lambda row: row["name"],
            ),
            boxes=sorted(
                (
                    dict(
                        name=box.name(),
                        comment=box.comment(),
                        position=list(box.position()),
                        size=list(box.size()),
                        color=list(box.color().rgb()),
                        items=sorted(item.name() for item in box.items()),
                    )
                    for box in (node.networkBoxes() if node.isNetwork() else ())
                ),
                key=lambda row: row["name"],
            ),
        )
    return dict(nodes=nodes, connections=offline._graph_connections(root))


def same_authored_state(left, right, path=()):
    # Houdini's editor-coordinate text roundtrip can differ by a few ulps.
    # Only UI vectors use a tolerance; parameters, keys, wires and data remain
    # strict. This comparison is a test oracle, never a production cache key.
    if (
        path
        and path[-1] in ("position", "size", "color")
        and isinstance(left, list)
        and isinstance(right, list)
        and all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in (*left, *right))
    ):
        return len(left) == len(right) and all(
            math.isfinite(a) and math.isfinite(b) and abs(a - b) <= 1e-10 for a, b in zip(left, right)
        )
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            same_authored_state(left[key], right[key], (*path, key)) for key in left
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            same_authored_state(a, b, (*path, index)) for index, (a, b) in enumerate(zip(left, right))
        )
    return left == right


def probe(name, change):
    original_capture()
    marker = roots[0].userData(offline.MARKER)
    previous = offline._read()[1][marker]
    change()
    expected = authored_state(roots[0])
    original_capture()
    payload = offline._read()[1][marker]
    assert payload != previous, "Authored edit was missed: " + name
    review = hou.node("/obj").createNode("matnet", "probe_review")
    try:
        restored = offline.recover_copy(marker, review)
        observed = authored_state(restored)
        if not same_authored_state(observed, expected):
            (directory / "failed-probe-expected.json").write_text(
                json.dumps(expected, indent=2, sort_keys=True), encoding="utf-8"
            )
            (directory / "failed-probe-observed.json").write_text(
                json.dumps(observed, indent=2, sort_keys=True), encoding="utf-8"
            )
            raise AssertionError("Restored authored state differs after " + name)
    finally:
        review.destroy()
    report["probes"].append(
        dict(
            name=name,
            previous_sha256=hashlib.sha256(previous).hexdigest(),
            edited_sha256=hashlib.sha256(payload).hexdigest(),
            restored_authored_state="strict authored values; UI vectors within 1e-10",
        )
    )


try:
    assert ch.capabilities()["image_iris"], "Image Iris must remain enabled"
    report["runtime"] = dict(python=ch.__file__, native=ch._native.__file__, image_iris=ch.capabilities()["image_iris"])
    hou.hipFile.clear(suppress_save_prompt=True)
    roots = []
    for index in range(args.roots):
        root = ch.create_material(name="archive_benchmark_%03d" % index)
        for child in range(args.nodes_per_root):
            node = root.createNode("cycles::value::1", "value_%03d" % child)
            node.parm("value").set((child + 1) * 0.01)
        roots.append(root)
    original_capture()
    first = offline._serialize_graph(roots[0])
    time.sleep(1.05)
    second = offline._serialize_graph(roots[0])
    report["stable_native_bytes_across_seconds"] = second == first
    if second != first:
        (directory / "unchanged-native-first.cpio").write_bytes(first)
        (directory / "unchanged-native-second.cpio").write_bytes(second)
        report["native_byte_comparison_limit"] = (
            "Native stream varies for an unchanged graph; only exact-equality reuse is permitted. Complete capture remains required when bytes differ. No licensed envelope is normalized."
        )
    if not args.skip_timings:
        measured("unchanged")
        measured("one-edited-root", changed=True)

    root = roots[0]
    node = root.node("value_000")
    control = hou.node("/obj").createNode("null", "archive_external_control")
    probe(
        "parameter expression",
        lambda: node.parm("value").setExpression(
            'ch("/obj/archive_external_control/tx") + $F*.01', hou.exprLanguage.Hscript
        ),
    )
    # Native archives retain channel definitions, not evaluated dependency
    # values. A changed external dependency must be saved by HIP itself.
    prior = offline._serialize_graph(root)
    control.parm("tx").set(0.375)
    after = offline._serialize_graph(root)
    report["external_dependency_changes_native_payload"] = after != prior
    probe("animation", lambda: node.parm("value").setKeyframe(hou.Keyframe(0.5, hou.frameToTime(12))))
    probe("node position", lambda: node.setPosition(node.position() + hou.Vector2((1.25, 0.5))))
    probe("node comment", lambda: node.setComment("Artist authored note"))
    probe("persistent user data", lambda: node.setUserData("artist_metadata", "retained"))
    probe("binary data block", lambda: node.setDataBlock("artist_binary", b"\x00\xffpreserve me"))
    probe("node flags", lambda: node.setGenericFlag(hou.nodeFlag.DisplayComment, True))
    probe("rewiring", lambda: root.node("surface_shader").setNamedInput("roughness", root.node("value_001"), "value"))
    group = root.parmTemplateGroup()
    group.append(hou.FloatParmTemplate("artist_spare", "Artist spare", 1))
    probe("parameter template", lambda: root.setParmTemplateGroup(group))
    note = root.createStickyNote("artist_note")
    probe("sticky note text", lambda: note.setText("Preserve text and layout"))
    box = root.createNetworkBox("artist_box")
    box.addItem(node)
    probe("network box comment", lambda: box.setComment("Preserve box membership"))

    # Explicit recovery uses the actual supported native payload and checks state
    # that a parameter-value-only cache would lose.
    original_capture()
    identity = root.userData(offline.MARKER)
    review = hou.node("/obj").createNode("matnet", "archive_review")
    recovered = offline.recover_copy(identity, review)
    restored = recovered.node(node.name())
    assert restored.userData("artist_metadata") == "retained"
    assert restored.dataBlock("artist_binary") == b"\x00\xffpreserve me"
    assert restored.comment() == node.comment() and restored.position() == node.position()
    assert restored.parm("value").keyframes() == node.parm("value").keyframes()
    assert next(item for item in recovered.stickyNotes() if item.name() == "artist_note").text() == note.text()
    assert next(item for item in recovered.networkBoxes() if item.name() == "artist_box").comment() == box.comment()
    report["explicit_native_archive_recovery"] = "passed"

    # Removal must invalidate the exact authored-state proof just as addition
    # does; an evaluated-value or positive-keys-only snapshot would miss these.
    probe("remove animation and expression", lambda: node.parm("value").deleteAllKeyframes())
    probe("remove persistent user data", lambda: node.destroyUserData("artist_metadata"))
    probe("remove binary data block", lambda: node.removeDataBlock("artist_binary"))
    probe("clear comment", lambda: node.setComment(""))
    probe("clear authored flag", lambda: node.setGenericFlag(hou.nodeFlag.DisplayComment, False))
    surface = root.node("surface_shader")
    probe("disconnect authored wire", lambda: surface.setInput(surface.inputIndex("roughness"), None))
    removed_group = root.parmTemplateGroup()
    removed_group.remove("artist_spare")
    probe("remove spare parameter", lambda: root.setParmTemplateGroup(removed_group))
    probe("remove sticky note", note.destroy)
    probe("remove network box", box.destroy)
    removal_review = hou.node("/obj").createNode("matnet", "archive_removal_review")
    removal_copy = offline.recover_copy(identity, removal_review)
    removal_node = removal_copy.node(node.name())
    assert removal_node.userData("artist_metadata") is None
    try:
        removal_node.dataBlock("artist_binary")
    except ValueError:
        pass
    else:
        raise AssertionError("Explicit recovery resurrected a removed binary block")
    assert not removal_node.comment() and not removal_node.parm("value").keyframes()
    assert not removal_node.isGenericFlagSet(hou.nodeFlag.DisplayComment)
    assert removal_copy.parm("artist_spare") is None
    assert not removal_copy.stickyNotes() and not removal_copy.networkBoxes()
    report["explicit_property_removal_recovery"] = "passed"

    # A byte-identical validated cache never licenses reuse of an externally
    # damaged block. Ordinary saves retain those unknown bytes verbatim.
    carrier = hou.node("/obj")
    good_block = carrier.dataBlock(offline.BLOCK)
    good_payload = offline._read()[1][identity]
    damaged_block = b"externally replaced unknown archive, retain verbatim\x00\xff"
    carrier.setDataBlock(offline.BLOCK, damaged_block)
    node.setComment("Artist edit while independent archive is unreadable")
    save(directory / "externally-damaged.hiplc")
    assert carrier.dataBlock(offline.BLOCK) == damaged_block
    carrier.setDataBlock(offline.BLOCK, good_block)
    original_capture()
    assert offline._read()[1][identity] != good_payload
    report["externally_damaged_archive_preserved"] = "passed"

    # Import a separately saved network fragment with an external expression.
    imported = ch.create_world_material(name="archive_fragment_root")
    fragment = directory / "fragment.cpio"
    imported.parent().saveItemsToFile((imported,), str(fragment))
    imported.destroy()
    hou.node("/mat").loadItemsFromFile(str(fragment))
    imported = hou.node("/mat/archive_fragment_root")
    roots.append(imported)
    merged = directory / "merge-source.hiplc"
    save(merged)
    root_paths = [root.path() for root in roots]
    hou.hipFile.clear(suppress_save_prompt=True)
    hou.hipFile.merge(str(merged), node_pattern="*", overwrite_on_conflict=False)
    roots = [hou.node(path) for path in root_paths]
    save(directory / "save-as-after-merge.hiplc")
    if not args.skip_timings:
        measured("save-as-after-merge")
    report["timings_skipped"] = args.skip_timings
    assert hou.node("/obj/archive_external_control").evalParm("tx") == 0.375
    assert hou.node("/mat/archive_benchmark_000/value_000").userData("artist_metadata") is None
    assert (
        hou.node("/mat/archive_benchmark_000/value_000").comment()
        == "Artist edit while independent archive is unreadable"
    )
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    offline.capture = original_capture
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(dict(result=report["result"], report=str(args.report))))
