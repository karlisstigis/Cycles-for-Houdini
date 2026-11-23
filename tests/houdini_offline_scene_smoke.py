# SPDX-License-Identifier: Apache-2.0
"""Disposable three-process production archive round trip, with no ROP/Viewer.

Run prepare/recover with the isolated runtime, missing without the package,
and scoped_missing/empty with only the package's source hook on HOUDINI_PATH.
Each stage must be a separate hython process using isolated prefs.
"""

import argparse
import hashlib
import json
from pathlib import Path
import time

import hou

parser = argparse.ArgumentParser()
parser.add_argument("mode", choices=("prepare", "missing", "scoped_missing", "empty", "recover"))
parser.add_argument("--output", required=True, type=Path)
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=True)
original = args.output / "original.hiplc"
resaved = args.output / "offline-twice.hiplc"
block = "cycles.offline.graphs"


def load(path):
    try:
        hou.hipFile.load(str(path), suppress_save_prompt=True)
    except hou.LoadWarning:
        pass
    except hou.OperationFailed as error:
        # Native network types without their plugin can emit Bad parent errors
        # while loading their children. Accept only this known offline fixture
        # failure, and then independently verify every placeholder and archive.
        message = str(error)
        errors = [line.strip() for line in message.splitlines() if line.strip().startswith("Error:")]
        if (
            args.mode not in ("missing", "scoped_missing")
            or "Bad node type found: cycles::" not in message
            or not errors
            or any("Bad parent found (parent is not a network)" not in line for line in errors)
        ):
            raise
        assert all(hou.node("/mat/offline_" + name) is not None for name in ("surface", "volume", "light", "world"))
        assert block in hou.node("/obj").dataBlockKeys("")


def digest():
    return hashlib.sha256(hou.node("/obj").dataBlock(block)).hexdigest()


def graph(root):
    nodes = (root, *root.allSubChildren())
    return [
        dict(
            path=root.relativePathTo(n),
            type=n.type().name(),
            parms={p.name(): dict(raw=p.rawValue(), keys=[k.asCode() for k in p.keyframes()]) for p in n.parms()},
            links=[[c.inputIndex(), root.relativePathTo(c.inputNode()), c.outputIndex()] for c in n.inputConnections()],
        )
        for n in sorted(nodes, key=lambda n: n.path())
    ]


if args.mode == "prepare":
    import cycles_houdini as ch
    import cycles_houdini_offline as offline

    assert offline._installed, "package ready.py must register save protection without a ROP/Viewer"
    mat = ch.create_material(name="offline_surface")
    shader = mat.node("surface_shader")
    key = hou.Keyframe()
    key.setFrame(1)
    key.setValue(0.314159)
    shader.parm("roughness").setKeyframe(key)
    shader.parm("metallic").setExpression("0.125 + $F * 0.001")
    ch.create_material(name="offline_volume", shader="principled_volume")
    ch.create_light_material(name="offline_light")
    ch.create_world_material(name="offline_world")
    obj = hou.node("/obj").createNode("geo", "unrelated_edit")
    obj.parm("shop_materialpath").set("/mat/offline_surface")
    expected = {n.name(): graph(n) for n in hou.node("/mat").children()}
    (args.output / "expected.json").write_text(json.dumps(expected, indent=2))
    before = offline.statistics()
    for index in range(10):
        obj.parm("tx").set(index)
    assert offline.statistics() == before, "transforms must do zero serialization"
    start = time.perf_counter()
    hou.hipFile.save(str(original), save_to_recent_files=False)
    elapsed = time.perf_counter() - start
    assert len(offline.inspect()) == 4, offline.inspect()
    result = dict(
        result="passed",
        archive=digest(),
        statistics=offline.statistics(),
        save_seconds=elapsed,
        hip_bytes=original.stat().st_size,
    )
elif args.mode == "empty":
    import cycles_houdini_offline as offline
    from unittest.mock import patch

    assert offline._installed, "package ready.py did not register before an ordinary save"
    with patch.object(offline, "capture", side_effect=AssertionError("unrelated save invoked Cycles capture")):
        hou.hipFile.save(str(args.output / "empty.hiplc"), save_to_recent_files=False)
    assert block not in hou.node("/obj").dataBlockKeys("")
    result = dict(result="passed", ordinary_save_unaffected=True)
elif args.mode in ("missing", "scoped_missing"):
    assert hou.nodeType(hou.vopNodeTypeCategory(), "cycles::material::1") is None
    if args.mode == "scoped_missing":
        import cycles_houdini_offline as offline

        assert offline._installed, "package ready.py must protect an offline material-only HIP"
    load(original)
    before = digest()
    prepared = json.loads((args.output / "prepare.json").read_text())
    assert before == prepared["archive"]
    hou.node("/obj/unrelated_edit").parm("tx").set(9.125)
    hou.hipFile.save(str(resaved), save_to_recent_files=False)
    load(resaved)
    hou.hipFile.save(str(resaved), save_to_recent_files=False)
    assert digest() == before, "offline saves must retain exact archive bytes"
    result = dict(
        result="passed",
        archive=digest(),
        placeholders=[
            dict(path=n.path(), type=n.type().name(), marker=n.userData("cycles.offline.id"))
            for n in hou.node("/mat").children()
        ],
    )
else:
    import cycles_houdini
    import cycles_houdini_offline as offline

    assert offline._installed, "package ready.py must register save protection before HIP load"
    load(resaved)
    expected = json.loads((args.output / "expected.json").read_text())
    before = digest()
    original_nodes = {n.path(): n.sessionId() for n in hou.node("/mat").children()}
    assignment = hou.node("/obj/unrelated_edit").parm("shop_materialpath").rawValue()
    statuses = offline.inspect()
    # Saves record native reduced definitions of the compiled shader nodes, so
    # Houdini reloads the plugin-absent saves as the real operators: every graph
    # is healthy and identical to what was authored. (Before those definitions,
    # graphs came back damaged and waited for explicit review.)
    assert len(statuses) == 4, statuses
    assert all(r["status"] == "healthy graph structure (archive available)" for r in statuses), statuses
    for name, authored in expected.items():
        assert graph(hou.node("/mat/" + name)) == authored, name
    # The archive still yields an exact review copy of each graph.
    for record in statuses:
        review = hou.node("/obj").createNode("matnet", "review_" + record["id"][:8])
        copy = offline.recover_copy(record["id"], review)
        actual = graph(copy)
        (args.output / (copy.name() + "-recovered.json")).write_text(json.dumps(actual, indent=2))
        assert actual == expected[copy.name()], copy.path()
    assert original_nodes == {n.path(): n.sessionId() for n in hou.node("/mat").children()}
    assert hou.node("/obj/unrelated_edit").evalParm("tx") == 9.125
    assert hou.node("/obj/unrelated_edit").parm("shop_materialpath").rawValue() == assignment
    # Saving after review must not archive the recovery copies. The graphs are
    # healthy, so this first save of the session recaptures their records (the
    # block bytes may change); it must still hold exactly the four originals.
    archived = sorted((r["id"], r["path"]) for r in statuses)
    hou.hipFile.save(str(args.output / "review-copy.hiplc"), save_to_recent_files=False)
    after = offline.inspect()
    assert sorted((r["id"], r["path"]) for r in after) == archived, after
    assert all(r["status"] == "healthy graph structure (archive available)" for r in after), after
    # Nothing changed since: the next save reuses the archive byte for byte.
    first = digest()
    hou.hipFile.save(str(args.output / "review-copy.hiplc"), save_to_recent_files=False)
    assert digest() == first
    # Unknown/corrupt archives survive ordinary subsequent saves byte-for-byte.
    hou.node("/obj").setDataBlock(block, b"unknown future archive format")
    hou.hipFile.save(str(args.output / "corrupt-preserved.hiplc"), save_to_recent_files=False)
    assert hou.node("/obj").dataBlock(block) == b"unknown future archive format"
    result = dict(
        result="passed", archive=before, copied=len(statuses), statistics=offline.statistics(), originals_untouched=True
    )
(args.output / (args.mode + ".json")).write_text(json.dumps(result, indent=2))
print(json.dumps(result, indent=2))
