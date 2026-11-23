# SPDX-License-Identifier: Apache-2.0
"""Whole Color Ramp promotion (Color + Alpha ramps) retains animation, references, topology and HIP ownership."""

from support.paths import artifacts_root
import json, os, traceback
from pathlib import Path
import hou
import cycles_houdini as ch
from cycles_houdini.ramps import ColorRampControls, preview_sources

dest = artifacts_root() / "stage3/materials/rgba-promotion"
dest.mkdir(parents=True, exist_ok=True)
report = dict(result="running", checks=[])
try:
    material = ch.create_material(name="promoted_rgba")
    node = material.createNode("cycles::color_ramp::1", "rgba")
    node.parm("ramp").set(
        hou.Ramp((hou.rampBasis.Linear,) * 3, (0.0, 0.4, 1.0), ((0.8, 0.1, 0.3), (0.2, 0.9, 0.1), (0.1, 0.2, 0.8)))
    )
    node.parm("alpha_ramp").set(hou.Ramp((hou.rampBasis.Linear,) * 3, (0.0, 0.4, 1.0), (0.1, 0.5, 0.9)))
    node.parm("ramp_interpolation").set("CARDINAL")
    node.parm("alpha_ramp2value").setExpression(".2+.05*$F", hou.exprLanguage.Hscript)
    node.parm("ramp1cr").setExpression(".3+.02*$F", hou.exprLanguage.Hscript)
    node.parm("alpha_ramp3value").setExpression('ch("alpha_ramp2value")*.5', hou.exprLanguage.Hscript)
    node.parm("ramp3cb").setExpression('hou.parm("ramp1cr").eval()*.5', hou.exprLanguage.Python)
    before = [preview_sources(node, hou.frameToTime(f)) for f in (1, 2, 5)]
    promoted = ch.promote_parameter(material, node, "ramp", name="palette", label="Palette")
    controls = ColorRampControls(node)
    assert controls.parm("ramp") == promoted[0] == material.parm(
        "palette"
    ) and controls.alpha_storage() == material.parm("palette_alpha")
    assert node.parm("ramp").getReferencedParm() == promoted[0] and node.parm(
        "alpha_ramp"
    ).getReferencedParm() == material.parm("palette_alpha")
    assert (
        material.parm("palette").parmTemplate().parmType() == hou.rampParmType.Color
        and material.parm("palette_alpha").parmTemplate().parmType() == hou.rampParmType.Float
    )
    assert [preview_sources(node, hou.frameToTime(f)) for f in (1, 2, 5)] == before
    assert material.parm("palette_alpha2value").expression() == ".2+.05*$F"
    assert material.parm("palette_alpha3value").expression() == 'ch("palette_alpha2value")*.5'
    assert "palette1cr" in material.parm("palette3cb").expression()
    assert material.parm("palette_interpolation").eval() == "CARDINAL"
    report["checks"].append("Whole Color+Alpha, interpolation and time expressions preserve exact owned sources")
    alpha = material.parm("palette_alpha")
    alpha.insertMultiParmInstance(1)
    assert material.parm("palette_alpha3value").expression() == ".2+.05*$F"
    material.parm("palette_alpha2pos").set(0.2)
    material.parm("palette_alpha2value").set(0.3)
    alpha.moveMultiParmInstances((2, 3))
    assert material.parm("palette_alpha4value").expression() == ".2+.05*$F"
    alpha.moveMultiParmInstances((3, 2))
    alpha.removeMultiParmInstance(1)
    promoted[0].insertMultiParmInstance(1)
    material.parm("palette2pos").set(0.2)
    material.parmTuple("palette2c").set((0.2, 0.4, 0.7))
    promoted[0].removeMultiParmInstance(1)
    assert [preview_sources(node, hou.frameToTime(f)) for f in (1, 2, 5)] == before
    report["checks"].append("Promoted insertion, reordering and removal preserve Color and Alpha animation")
    material.setName("renamed_palette_material")
    node.setName("renamed_rgba")
    assert [preview_sources(node, hou.frameToTime(f)) for f in (1, 2, 5)] == before
    report["checks"].append("Native references survive owner and shader renaming")
    path = dest / "promoted.hiplc"
    source_path = node.path()
    hou.hipFile.save(str(path))
    hou.hipFile.load(str(path), suppress_save_prompt=True)
    node = hou.node(source_path)
    assert [preview_sources(node, hou.frameToTime(f)) for f in (1, 2, 5)] == before
    report["checks"].append("Promoted Color, Alpha and animation reload from HIP")
    outer = ch.create_material(name="outer_palette")
    nested = ch.create_subnet(outer, name="nested")
    r = nested.createNode("cycles::color_ramp::1", "rgba")
    r.parm("ramp_interpolation").set("EASE")
    r.parm("alpha_ramp").set(hou.Ramp((hou.rampBasis.Linear,) * 3, (0.0, 0.5, 1.0), (0.2, 0.6, 1.0)))
    r.parm("alpha_ramp2value").setExpression(".1*$F", hou.exprLanguage.Hscript)
    expected = [preview_sources(r, hou.frameToTime(f)) for f in (1, 2, 5)]
    ch.promote_parameter(nested, r, "ramp", name="inner_palette")
    ch.promote_parameter(outer, r, "ramp", name="outer_palette")
    assert ColorRampControls(r).parm("ramp") == outer.parm("outer_palette") and ColorRampControls(
        r
    ).alpha_storage() == outer.parm("outer_palette_alpha")
    assert [preview_sources(r, hou.frameToTime(f)) for f in (1, 2, 5)] == expected
    report["checks"].append("Nested promotion resolves the canonical Color and Alpha ramps")
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    (dest / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
