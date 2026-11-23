# SPDX-License-Identifier: Apache-2.0
"""Protect a known-good existing HIP's saved ROP layout before offline saves."""

import argparse
import contextlib
import io
import json
from pathlib import Path
import textwrap

import hou

parser = argparse.ArgumentParser()
parser.add_argument("mode", choices=("prepare", "missing", "recover"))
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--source", type=Path)
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=True)
protected = args.output / "existing-protected.hiplc"
resaved = args.output / "existing-offline-twice.hiplc"


def load(path):
    try:
        hou.hipFile.load(str(path), suppress_save_prompt=True)
    except hou.LoadWarning:
        pass
    except hou.OperationFailed as error:
        message = str(error)
        errors = [line.strip() for line in message.splitlines() if line.strip().startswith("Error:")]
        if (
            args.mode != "missing"
            or "Bad node type found: cycles::" not in message
            or not errors
            or any("Bad parent found (parent is not a network)" not in line for line in errors)
        ):
            raise


def menu_items(parm):
    # Houdini's button mini-menu has a generator but Parm.menuItems() rejects
    # it as a non-menu parm. Execute the saved generator with its normal kwargs.
    code = parm.parmTemplate().itemGeneratorScript()
    kwargs = dict(node=parm.node(), parm=parm)
    try:
        expression = compile(code, "<saved-menu>", "eval")
    except SyntaxError:
        namespace = {}
        exec("def _menu(kwargs):\n" + textwrap.indent(code, "    "), namespace)
        return namespace["_menu"](kwargs)
    return eval(expression, dict(kwargs=kwargs))


if args.mode == "prepare":
    import cycles_houdini
    import cycles_houdini_offline as offline

    assert offline._installed
    assert args.source and args.source.is_file()
    load(args.source)
    rop = hou.node("/out/offline_test")
    assert rop.evalParm("samples") == 1234
    rop.parm("pass_count").set(1)
    rop.parm("pass_type1").set("normal")
    rop.parm("world_material").set("/mat/offline_test")
    for parm in rop.parmTemplateGroup().entriesWithoutFolders():
        if hasattr(parm, "itemGeneratorScript"):
            code = parm.itemGeneratorScript()
            if code and "from cycles_houdini" in code:
                assert "_cycles_import_error" in code, ("old saved menu remains unguarded", parm.name(), code)
    assert menu_items(rop.parm("res_menu")), "healthy builtin FBres menu must remain functional"
    hou.hipFile.save(str(protected), save_to_recent_files=False)
    assert offline.inspect(), "existing healthy graph needs its first protected archive"
    result = dict(result="passed", samples=rop.evalParm("samples"), archive=offline.inspect())
elif args.mode == "missing":
    assert hou.nodeType(hou.vopNodeTypeCategory(), "cycles::material::1") is None
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        load(protected)
        rop = hou.node("/out/offline_test")
        saved = {name: rop.parm(name).rawValue() for name in ("samples", "pass_type1", "world_material")}
        for _ in range(3):
            for name in ("pass_add", "pass_type1", "world_material"):
                menu_items(rop.parm(name))
        assert saved == {name: rop.parm(name).rawValue() for name in saved}
        assert rop.evalParm("samples") == 1234
        hou.hipFile.save(str(resaved), save_to_recent_files=False)
        load(resaved)
        hou.hipFile.save(str(resaved), save_to_recent_files=False)
    output = captured.getvalue()
    assert "ModuleNotFoundError" not in output, output
    (args.output / "offline-callbacks.log").write_text(output)
    result = dict(result="passed", saved=saved, notices=output.count("Cycles renderer unavailable:"))
else:
    import cycles_houdini
    import cycles_houdini_offline as offline

    load(resaved)
    rop = hou.node("/out/offline_test")
    assert rop.evalParm("samples") == 1234
    assert rop.parm("pass_type1").rawValue() == "normal"
    assert rop.parm("world_material").rawValue() == "/mat/offline_test"
    assert offline.inspect(), "archive must survive offline saves"
    result = dict(result="passed", samples=1234, pass_type="normal", world_material="/mat/offline_test")
(args.output / (args.mode + ".json")).write_text(json.dumps(result, indent=2))
print(json.dumps(result, indent=2))
