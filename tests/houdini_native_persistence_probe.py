# SPDX-License-Identifier: Apache-2.0
"""Probe Houdini's own operator fallback persistence, using only disposable HIPs."""

import argparse
import json
from pathlib import Path
import hou

parser = argparse.ArgumentParser()
parser.add_argument("mode", choices=("prepare", "missing", "reload"))
parser.add_argument("--directory", type=Path, required=True)
args, _ = parser.parse_known_args()
args.directory.mkdir(parents=True, exist_ok=True)


def describe():
    return {
        n.path(): dict(
            type=n.type().name(),
            network=n.isNetwork(),
            parms={p.name(): p.rawValue() for p in n.parms()},
            inputs=[[c.inputIndex(), c.inputNode().path(), c.outputIndex()] for c in n.inputConnections()],
        )
        for n in hou.node("/mat").allSubChildren()
    }


if args.mode == "prepare":
    import cycles_houdini as ch

    mat = ch.create_material(name="native_saved_surface")
    mat.node("surface_shader").parm("roughness").set(0.314159)
    mat.node("surface_shader").parm("metallic").setExpression(".125+$F*.001")
    ch.create_world_material(name="native_saved_world")
    ch.create_light_material(name="native_saved_light")
    ch.create_material(name="native_saved_volume", shader="principled_volume")
    expected = describe()
    rows = []
    seen = set()
    for n in hou.node("/mat").allSubChildren():
        if n.type().name() in seen:
            continue
        seen.add(n.type().name())
        ds = n.type().parmTemplateGroup().asDialogScript(full_info=True)
        ports = []
        for direction, names in [("input", n.inputNames()), ("output", n.outputNames())]:
            ports.extend(direction + " float " + json.dumps(name) + " " + json.dumps(name) + "\n" for name in names)
        close = ds.rfind("}")
        ds = ds[:close] + "".join(ports) + ds[close:]
        rows.append(
            dict(
                type=n.type().name(),
                **ch._native.scene_definition_fallback(n.path(), ds, len(n.inputNames()), len(n.outputNames())),
            )
        )
    assert describe() == expected, "Registering fallback definitions changed the live graph"
    (args.directory / "expected.json").write_text(json.dumps(expected, indent=2))
    hou.hipFile.save((args.directory / "native.hiplc").as_posix(), save_to_recent_files=False)
    report = dict(result="passed", rows=rows, live_graph_unchanged=True)
else:
    source = args.directory / ("native.hiplc" if args.mode == "missing" else "offline.hiplc")
    warning = None
    try:
        hou.hipFile.load(source.as_posix(), suppress_save_prompt=True)
    except hou.Error as error:
        warning = str(error)
    actual = describe()
    report = dict(result="observed", warning=warning, nodes=actual)
    if args.mode == "missing":
        hou.hipFile.save((args.directory / "offline.hiplc").as_posix(), save_to_recent_files=False)
        report["saved"] = True
    else:
        expected = json.loads((args.directory / "expected.json").read_text())
        report["exact_graph"] = actual == expected
        report["result"] = "passed" if actual == expected else "failed"
(args.directory / (args.mode + ".json")).write_text(json.dumps(report, indent=2))
print(json.dumps(report))
if report["result"] == "failed":
    raise AssertionError("Native graph did not survive the plugin-absent save")
