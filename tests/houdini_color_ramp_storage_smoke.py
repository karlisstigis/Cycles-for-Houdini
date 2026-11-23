# SPDX-License-Identifier: Apache-2.0
"""Inline Color (RGB) and Alpha (float) ramps: defaults, native stop edits, animation and references."""

from support.paths import artifacts_root
import json, os, traceback
from pathlib import Path
import hou
import cycles_houdini as ch

destination = artifacts_root() / "stage3/materials/color-ramp-storage"
destination.mkdir(parents=True, exist_ok=True)
report = dict(result="running", checks=[])
try:
    material = ch.create_material(name="rgba_storage")
    node = material.createNode("cycles::color_ramp::1")
    color, alpha = node.parm("ramp"), node.parm("alpha_ramp")
    assert (
        color.parmTemplate().parmType() == hou.rampParmType.Color
        and alpha.parmTemplate().parmType() == hou.rampParmType.Float
    )
    assert len(node.parmTuple("ramp1c")) == 3 and node.parm("ramp1ca") is None and node.parm("edit_ramp") is None
    report["defaults"] = dict(
        color=[list(map(list, color.evalAsRamp().values())), list(color.evalAsRamp().keys())],
        alpha=[list(alpha.evalAsRamp().values()), list(alpha.evalAsRamp().keys())],
        interpolation=node.parm("ramp_interpolation").evalAsString(),
    )
    assert (
        color.evalAsRamp().values() == ((0.0, 0.0, 0.0), (1.0, 1.0, 1.0))
        and alpha.evalAsRamp().values() == (1.0, 1.0)
        and alpha.evalAsRamp().keys() == (0.0, 1.0)
    ), report["defaults"]
    assert node.parm("ramp_interpolation").evalAsString() == "NATIVE", report["defaults"]
    report["checks"].append(
        "Defaults: native RGB Color ramp, opaque Alpha ramp, Native interpolation, no RGBA tuple or editor button"
    )
    ramp = hou.Ramp((hou.rampBasis.Linear,) * 3, (0.0, 0.4, 1.0), ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)))
    color.set(ramp)
    assert color.evalAsRamp().keys() == ramp.keys() and color.evalAsRamp().values() == ramp.values()
    alpha.set(hou.Ramp((hou.rampBasis.Linear,) * 3, (0.0, 0.3, 1.0), (0.1, 0.4, 0.9)))
    node.parm("alpha_ramp2value").setExpression(".2+.1*$F", hou.exprLanguage.Hscript)
    expression = node.parm("alpha_ramp2value").expression()
    alpha.insertMultiParmInstance(1)
    assert node.parm("alpha_ramp3value").expression() == expression
    assert abs(node.parm("alpha_ramp4value").eval() - 0.9) < 1e-6
    report["checks"].append("Native Alpha insertion carries stop value and animation")
    alpha.removeMultiParmInstance(1)
    assert node.parm("alpha_ramp2value").expression() == expression
    assert abs(node.parm("alpha_ramp3value").eval() - 0.9) < 1e-6
    report["checks"].append("Native Alpha removal restores stop value and animation")
    alpha.moveMultiParmInstances((1, 2))
    assert node.parm("alpha_ramp3value").expression() == expression
    alpha.moveMultiParmInstances((2, 1))
    assert node.parm("alpha_ramp2value").expression() == expression
    report["checks"].append("Native Alpha reordering carries animation")
    before = alpha.evalAsRamp().keys()
    color.insertMultiParmInstance(1)
    node.parm("ramp2pos").set(0.6)
    color.removeMultiParmInstance(1)
    assert alpha.evalAsRamp().keys() == before and node.parm("alpha_ramp2value").expression() == expression
    report["checks"].append("Color stop edits leave the independent Alpha stops unchanged")
    path = destination / "storage.hip"
    node_path = node.path()
    hou.hipFile.save(str(path))
    hou.hipFile.load(str(path), suppress_save_prompt=True)
    loaded = hou.node(node_path)
    assert loaded.parm("alpha_ramp2value").expression() == expression and len(loaded.parmTuple("ramp1c")) == 3
    report["checks"].append("Saved Color and Alpha stops and animation reload unchanged")
    target = ch.promote_parameter(loaded.parent(), loaded, "alpha_ramp2value", name="editable_alpha_stop")
    assert len(target) == 1 and target[0].expression() == expression
    assert loaded.parm("alpha_ramp2value").getReferencedParm() == target[0]
    target[0].setExpression(".3+.1*$F", hou.exprLanguage.Hscript)
    assert abs(loaded.parm("alpha_ramp2value").eval() - (0.3 + 0.1 * hou.frame())) < 1e-6
    stop = ch.promote_parameter(loaded.parent(), loaded, "ramp2c", name="editable_color_stop")
    assert len(stop) == 3
    report["checks"].append("Promoted Alpha and Color stops preserve animated references")
    from cycles_houdini import ramps

    shared = loaded.parent().createNode("cycles::color_ramp::1", "shared_rgba")
    shared.parm("ramp").setExpression('ch("../' + loaded.name() + '/ramp")', hou.exprLanguage.Hscript)
    shared.parm("alpha_ramp").setExpression('ch("../' + loaded.name() + '/alpha_ramp")', hou.exprLanguage.Hscript)
    shared_controls = ramps.ColorRampControls(shared)
    assert shared_controls.parm("ramp") == loaded.parm("ramp") and shared_controls.alpha_parm(
        "alpha_ramp2value"
    ) == loaded.parm("alpha_ramp2value")
    assert ramps.preview_sources(shared) == ramps.preview_sources(loaded)
    report["checks"].append("Shared Color and Alpha ramp references resolve their canonical stops")
    group = loaded.parent().parmTemplateGroup()
    group.append(hou.RampParmTemplate("rgb_only", "RGB Only", hou.rampParmType.Color))
    loaded.parent().setParmTemplateGroup(group)
    shared.parm("alpha_ramp").setExpression('ch("../rgb_only")', hou.exprLanguage.Hscript)
    color_source, alpha_source = ramps.preview_sources(shared)
    assert color_source[3] == 0 and alpha_source[3] == 7, (color_source[:8], alpha_source[:8])
    shared.parm("alpha_ramp").setExpression('ch("../' + loaded.name() + '/alpha_ramp")', hou.exprLanguage.Hscript)
    assert all(source[3] == 0 for source in ramps.preview_sources(shared))
    report["checks"].append("A color ramp as the Alpha source faults only Alpha; restoring a float ramp clears it")
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    (destination / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
