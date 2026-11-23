# SPDX-License-Identifier: Apache-2.0
"""Whole-packed Edit vs Transform primitive/point behavior, without Blender."""

import argparse
import json
from pathlib import Path
import hou

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--out", type=Path, required=True)
args = parser.parse_args()
args.out.mkdir(parents=True, exist_ok=True)
obj = hou.node("/obj").createNode("geo", "reference_edit_fixture", run_init_scripts=False)
reference = obj.createNode("cycles::blender_reference", "reference")
reference.setParms(dict(file="$HIP/missing.blend", ref_target1="Cube"))
box = obj.createNode("box")
stock = obj.createNode("pack")
stock.setInput(0, box)
stock.parm("pivot").set("origin")
transforms = dict(tx=3.0, ty=-1.2, tz=2.1, rx=11.0, ry=40.0, rz=-15.0, sx=-2.0, sy=3.0, sz=0.5, px=0.7, py=-0.2, pz=1.1)
checks = []


def matrix(node):
    geometry = node.geometry()
    assert len(geometry.prims()) == 1 and len(geometry.points()) == 1
    return tuple(geometry.prim(0).intrinsicValue("packedfulltransform"))


def close(a, b):
    return all(abs(x - y) < 1e-5 for x, y in zip(a, b))


for group in ("prims", "points"):
    results = {}
    for label, source in (("reference", reference), ("stock", stock)):
        for kind in ("edit", "xform"):
            node = obj.createNode(kind, label + "_" + kind + "_" + group)
            node.setInput(0, source)
            node.setParms(dict(group="0", grouptype=group, **transforms))
            assert not node.errors()
            results[label, kind] = matrix(node)
    assert close(results["reference", "edit"], results["reference", "xform"]), results
    assert close(results["reference", "edit"], results["stock", "edit"]), results
    checks.append(group + " matches stock packed Edit and Transform")
    if group == "prims":
        assert not close(results["reference", "edit"][:12], tuple(hou.Matrix4(1).asTuple())[:12])
    else:
        # Point edits transform the packed pivot P; this is Houdini's standard
        # point selection behavior. Whole geometry rotation/scale uses prims.
        assert close(results["reference", "edit"][:12], tuple(hou.Matrix4(1).asTuple())[:12])
edited = obj.node("reference_edit_prims")
before_edit = matrix(edited)
with hou.undos.group("Whole packed reference edit"):
    edited.parm("tx").set(8.25)
after_edit = matrix(edited)
assert not close(before_edit, after_edit)
hou.undos.performUndo()
assert close(matrix(edited), before_edit)
hou.undos.performRedo()
assert close(matrix(edited), after_edit)
hou.undos.performUndo()
assert close(matrix(edited), before_edit)
checks.append("whole packed Edit undo and redo preserve transforms")
scene = args.out.resolve() / "edit-transform.hip"
snapshots = {node.path(): matrix(node) for node in obj.children() if node.type().name() in ("edit", "xform")}
hou.hipFile.save(str(scene))
hou.hipFile.load(str(scene), suppress_save_prompt=True)
for path, before in snapshots.items():
    assert close(matrix(hou.node(path)), before), path
checks.append("signed nonuniform transforms and pivot survive HIP reload")
# Exercise the adapter's custom-reference identity branch with real HDK data.
# One tiny CPU session uses a resident authored mesh, so Blender is never started.
import cycles_houdini as ch
from blend_direct_fixture import mesh, prototype

identity_obj = hou.node("/obj").createNode("geo", "packed_identity_policy", run_init_scripts=False)
custom = identity_obj.createNode("cycles::blender_reference", "custom")
custom.setParms(dict(file="$HIP/identity-fixture.blend", ref_target1="Cube"))
custom.parm("ref_frame1").deleteAllKeyframes()
custom.parm("ref_frame1").set(1.25)
ordinary_box = identity_obj.createNode("box")
ordinary = identity_obj.createNode("pack")
ordinary.setInput(0, ordinary_box)
ordinary.parm("pivot").set("origin")
left = identity_obj.createNode("xform")
right = identity_obj.createNode("xform")
left.parm("tx").setExpression("$T*4-.75", hou.exprLanguage.Hscript)
right.parm("tx").setExpression("$T*4+.75", hou.exprLanguage.Hscript)
joined = identity_obj.createNode("merge")
joined.setInput(0, left)
joined.setInput(1, right)
authored = identity_obj.createNode("python", "piece_names_and_ids")
authored.setInput(0, joined)
member = prototype("identity/shared", mesh("identity/shared", size=(0.4, 0.4, 0.4)))
reference_token = "$HIP/identity-fixture.blend#object=Cube"
identity_rows = []
with ch.Session(
    device="CPU", width=8, height=8, samples=1, threads=1, execution="worker", denoising={"enabled": False}
) as session:
    for label, source, id_kind, duplicate, warning in (
        ("custom_name_only", custom, None, False, False),
        ("custom_integer_ids", custom, "integer", False, False),
        ("custom_string_ids", custom, "string", False, False),
        ("custom_duplicate_integer_ids", custom, "integer", True, True),
        ("custom_duplicate_string_ids", custom, "string", True, True),
        ("ordinary_duplicate_names", ordinary, None, False, True),
    ):
        left.setInput(0, source)
        right.setInput(0, source)
        code = [
            "import hou",
            "g=hou.pwd().geometry()",
            "name=g.findPrimAttrib('name') or g.addAttrib(hou.attribType.Prim,'name','')",
        ]
        if id_kind:
            code.append("identity=g.addAttrib(hou.attribType.Prim,'id'," + ("''" if id_kind == "string" else "0") + ")")
        code += ["for i,p in enumerate(g.prims()):", " p.setAttribValue(name,'repeated_piece')"]
        if id_kind:
            code.append(
                " p.setAttribValue(identity,"
                + (
                    "'same'"
                    if duplicate and id_kind == "string"
                    else "7"
                    if duplicate
                    else "str(i)"
                    if id_kind == "string"
                    else "i"
                )
                + ")"
            )
        authored.parm("python").set("\n".join(code))
        try:
            authored.cook(force=True)
        except hou.OperationFailed:
            raise AssertionError((label, authored.errors(), authored.parm("python").evalAsString())) from None
        assert not authored.errors(), (label, authored.errors())
        request = ch.geometry_request(identity_obj, authored, material="surface", motion="deformation", at_time=0)
        if source == custom:
            request["packed_blender"] = dict(
                mode="fast",
                instancer_path=identity_obj.path(),
                fallback=-1,
                prototypes=[
                    dict(
                        path=reference_token,
                        node_id=0,
                        source_reference=reference_token,
                        source_frame=1.25,
                        direct_instances=member["instances"],
                    )
                ],
            )
        update = dict(
            geometry=[request],
            materials=[dict(key="surface", kind="emission", color=(0.5, 0.2, 0.1))],
            motion=dict(enabled=True, fps=24, shutter_frames=0.5, offset=0, samples=3),
        )
        session.wait(session.apply(update, at_time=0), timeout=60)
        status = session.status()
        assert not status["error"] and status["object_resources"] == 2, (label, status)
        codes = [issue["code"] for issue in status["source_diagnostics"]]
        assert ("motion.packed_unavailable" in codes) == warning, (label, status["source_diagnostics"])
        identity_rows.append(dict(case=label, expected_ambiguity=warning, diagnostics=status["source_diagnostics"]))
(args.out / "identity-policy.json").write_text(json.dumps(identity_rows, indent=2), encoding="utf-8")
checks.append("custom piece names use standard IDs/index; duplicate IDs and ordinary name ambiguity remain guarded")
print(json.dumps(dict(checks=checks, scene=str(scene))))
