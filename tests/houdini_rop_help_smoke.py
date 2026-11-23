# SPDX-License-Identifier: Apache-2.0
"""Check ROP F1 help, tooltip coverage and saved per-node About migration.

Run with the staged Houdini runtime. Does not cook geometry or render images.
"""

from support.paths import artifacts_root

import json
import os
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

import hou
from cycles_houdini import device_selection, rop_help

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
out = artifacts_root() / "rop-help"
out.mkdir(parents=True, exist_ok=True)
rop = hou.node("/out").createNode("cycles_ipr", "rop_help_check")
definition = rop.type().definition()
page = definition.sections()["Help"].contents()
assert "= Cycles Render =" in page and "@parameters" in page
for route in ("objects", "camera", "viewer", "shaders", "licenses"):
    assert "|/cycles/" + route + "]" in page, route
assert rop_help.ATTRIBUTION in page
assert rop_help.NVIDIA_LICENSE_URL in page and rop_help.CYCLES_URL in page
# Houdini's native/base ROP fields discard template help during HDA
# normalization. Their effective tooltip is supplied by the embedded node help.
# Match the libraries used by the SideFX help server in standalone hython.
bundled = Path(hou.getenv("HFS")) / f"python{sys.version_info.major}{sys.version_info.minor}/lib/site-packages-forced"
if bundled.is_dir():
    sys.path.append(str(bundled))

from houdinihelp import api as help_api

help_api.initialize()
help_path = help_api.components_to_path("Driver", None, None, "cycles_ipr", None, snap_version=False)
help_pages = help_api.get_pages()
assert help_pages.exists(help_path), help_path
effective_tooltips = {}


def check(node):
    group = node.parmTemplateGroup()
    about = group.findFolder(("About",))
    assert about is not None
    names = [p.name() for p in about.parmTemplates()]
    actions = ("ch_about_notices", "ch_about_integration_license", "ch_about_nvidia_license", "ch_about_cycles")
    expected = [
        "ch_about_text",
        "ch_about_renderer",
        "ch_about_dlss",
        "ch_about_technology",
        "ch_about_availability",
        "ch_about_trademarks",
        "ch_about_spacer",
    ]
    for action in actions:
        expected.extend((action + "_left", action, action + "_right"))
    assert names == expected, names
    # A separately appended one-tab folder set exists in HOM but is not shown
    # beside Rendering/Devices/Scripts in the parameter pane. Check actual ownership.
    about_parm = node.parm("ch_about_notices")
    rendering_set = node.parm("samples").containingFolderSetParmTuples()[0]
    devices_parm = node.parm("device_mode")
    scripts_parm = node.parm("tprerender")
    assert about_parm.containingFolderSetParmTuples() == (rendering_set,)
    assert devices_parm.containingFolderSetParmTuples()[0] == rendering_set
    assert scripts_parm.containingFolderSetParmTuples()[0] == rendering_set
    assert scripts_parm.containingFolderIndices()[0] == devices_parm.containingFolderIndices()[0] + 1
    assert about_parm.containingFolderIndices()[0] == scripts_parm.containingFolderIndices()[0] + 1
    checked = []

    def walk(entries, hidden=False):
        for parm in entries:
            hidden_here = hidden or parm.isHidden()
            if hidden_here:
                continue
            if isinstance(parm, hou.FolderParmTemplate):
                walk(parm.parmTemplates(), hidden_here)
            elif parm.type() not in (hou.parmTemplateType.Label, hou.parmTemplateType.Separator):
                physical_device = parm.tags().get("cycles_gpu_physical_id")
                parsed = (
                    None
                    if physical_device
                    else help_api.getParmTooltipFromPath(help_path, parm.name(), parm.label(), False, help_pages)
                )
                text = (parm.help() or parsed or "").strip()
                assert len(text.split()) >= 5, (parm.name(), text)
                assert text != parm.label() + ".", parm.name()
                # Device checkboxes are runtime-authored, not definition help.
                if not physical_device:
                    assert "    #id: " + parm.name() + "\n" in page, parm.name()
                    assert parsed, ("Missing effective node-help tooltip", parm.name())
                    effective_tooltips[parm.name()] = parsed
                checked.append(parm.name())

    walk(group.entries())
    return checked


checked = check(rop)
assert effective_tooltips["denoise_quality"] != effective_tooltips["ipr_denoise_quality"]
assert effective_tooltips["max_bounce"] != effective_tooltips["world_max_bounces"]
assert "Interactive viewer setting." in effective_tooltips["ipr_denoise_quality"]
assert (
    help_api.getParmTooltipFromPath(help_path, "pass_type1", "Type", False, help_pages)
    == effective_tooltips["pass_type#"]
)
# Simulate a HIP whose physical-device list froze the pre-update UI. Refresh
# must add the About tab and current help without resetting authored channels.
rop.parm("samples").set(321)
rop.parm("noise_threshold").setExpression(".01+$F*.001", hou.exprLanguage.Hscript)
group = rop.parmTemplateGroup()
group.remove(group.findIndicesForFolder(("About",)))
old = group.find("samples")
old.setHelp("")
group.replace("samples", old)
group.append(hou.FloatParmTemplate("artist_help_spare", "Artist Spare", 1, default_value=(7,)))
rop.setParmTemplateGroup(group)
rop.parm("artist_help_spare").set(3)
device_selection.refresh(rop)
assert rop.parm("samples").eval() == 321
assert rop.parm("noise_threshold").expression() == ".01+$F*.001"
assert rop.parm("artist_help_spare").eval() == 3
group = rop.parmTemplateGroup()
group.remove("artist_help_spare")
rop.setParmTemplateGroup(group)
assert check(rop) == checked
path = out / "rop-help.hiplc"
hou.hipFile.save(str(path))
hou.hipFile.clear(suppress_save_prompt=True)
hou.hipFile.load(str(path), suppress_save_prompt=True)
rop = hou.node("/out/rop_help_check")
assert check(rop) == checked
assert rop.parm("samples").eval() == 321
links = {target: rop_help.link_target(target) for target in ("notices", "integration", "nvidia", "cycles")}
for target in ("notices", "integration", "nvidia"):
    parsed = urlparse(links[target])
    assert parsed.scheme == "file", (target, links[target])
    assert Path(unquote(parsed.path).lstrip("/")).is_file(), links[target]
(out / "houdini-rop-help-smoke.json").write_text(
    json.dumps(
        dict(
            result="passed",
            visible_controls=len(checked),
            links=links,
            help_path=help_path,
            effective_tooltips=effective_tooltips,
            checks=[
                "embedded node help with internal parameter anchors",
                "meaningful tooltips for every visible control and pass row",
                "text-only About attribution and installed license links",
                "stored per-node layout refresh and HIP reload preserve values, expressions and spares",
            ],
        ),
        indent=2,
    )
)
print(json.dumps(dict(result="passed", visible_controls=len(checked), links=links)))
