# SPDX-License-Identifier: Apache-2.0
"""Current and saved per-node IPR layouts retain their authored channels."""

import json
import os
from pathlib import Path
import hou
from cycles_houdini.rop_layout import ensure_ipr_settings_layout

out = Path(os.environ["CH_IPR_LAYOUT_REPORT"])
out.parent.mkdir(parents=True, exist_ok=True)
node = hou.node("/out").createNode("cycles_ipr", "ipr_layout")
names = (
    "ipr_resolution_percent",
    "ipr_adaptive_sampling",
    "ipr_noise_threshold",
    "ipr_samples",
    "ipr_min_samples",
    "ipr_use_denoise",
    "ipr_denoiser",
    "ipr_denoise_use_gpu",
    "ipr_denoise_passes",
    "ipr_denoise_quality",
    "ipr_denoise_prefilter",
    "ipr_denoise_start_sample",
)


def layout():
    top_level = [parm.name() for parm in node.parmTemplateGroup().entries()]
    assert top_level.index("take") == top_level.index("f") + 1
    for name in names:
        folders = node.parm(name).containingFolders()
        assert folders[:3] == ("Rendering", "IPR Settings", "Sampling"), (name, folders)
        if name.startswith("ipr_denoise") and name != "ipr_use_denoise":
            assert folders[-1] == "Denoising", (name, folders)
    assert node.parm("samples").containingFolders() == ("Rendering", "Sampling", "Final Render")
    assert node.parm("tessellation_update").containingFolders() == ("Rendering", "IPR Settings")
    assert node.parm("tessellation_update").parmTemplate().label() == "Adaptive Subdivision Update"
    assert node.parm("refresh_tessellation").containingFolders() == ("Rendering", "IPR Settings")
    cache = node.parm("ipr_topology_cache_mib")
    assert cache.containingFolders() == ("Rendering", "Geometry", "Subdivision")
    assert cache.parmTemplate().label() == "IPR Topology Cache Limit (MiB)"
    assert cache.parmTemplate().defaultValue() == (512,)
    assert "Zero disables and clears the cache" in cache.parmTemplate().help()
    # Refit Meshes While Interacting: IPR Settings, off by default.
    assert node.parm("ipr_refit_meshes").containingFolders() == ("Rendering", "IPR Settings")
    assert node.parm("ipr_refit_meshes").parmTemplate().label() == "Refit Meshes While Interacting"
    assert node.parm("ipr_refit_meshes").parmTemplate().defaultValue() is False


layout()
# Model the saved per-node layout from the immediately preceding build.
group = node.parmTemplateGroup()
take = group.find("take")
group.remove("take")
scene_indices = group.findIndicesForFolder(("Scene",))
scene = group.entryAtIndices(scene_indices)
scene.setParmTemplates((take, *scene.parmTemplates()))
group.replace(scene_indices, scene)
adaptive = group.find("tessellation_update")
adaptive.setLabel("Tessellation Update")
group.replace("tessellation_update", adaptive)
cache = group.find("ipr_topology_cache_mib")
group.remove("ipr_topology_cache_mib")
group.append(cache)
indices = group.findIndices("ipr_samples")[:-1]
section = group.entryAtIndices(indices)
group.remove(indices)
indices = group.findIndicesForFolder(("Rendering", "Sampling"))
sampling = group.entryAtIndices(indices)
section.setLabel("Interactive Render")
sampling.setParmTemplates((*sampling.parmTemplates(), section))
group.replace(indices, sampling)
group.append(hou.StringParmTemplate("artist_note", "Artist Note", 1, default_value=("keep this",)))
node.setParmTemplateGroup(group)
node.setParms(
    dict(
        samples=117,
        ipr_resolution_percent=63.5,
        ipr_use_denoise=0,
        ipr_denoiser="oidn",
        ipr_denoise_passes="rgb",
        ipr_denoise_quality="fast",
        ipr_denoise_prefilter="none",
        ipr_denoise_start_sample=19,
        tessellation_update=0,
        picture="$HIP/artist/custom.$F4.exr",
        artist_note="preserved custom value",
    )
)
node.parm("ipr_topology_cache_mib").setExpression('ch("samples") * 2', hou.exprLanguage.Hscript)
node.parm("ipr_samples").setExpression('ch("samples") * 2', hou.exprLanguage.Hscript)
node.parm("ipr_min_samples").setExpression('ch("min_samples")', hou.exprLanguage.Hscript)
hou.takes.rootTake().addChildTake("SavedLayoutTake")
node.parm("take").setExpression('"SavedLayoutTake"', hou.exprLanguage.Hscript)
for frame, value in ((1, 0.073), (9, 0.021)):
    key = hou.Keyframe()
    key.setFrame(frame)
    key.setValue(value)
    node.parm("ipr_noise_threshold").setKeyframe(key)


def channels():
    return {
        name: node.parm(name).asCode()
        for name in (
            *names,
            "samples",
            "min_samples",
            "tessellation_update",
            "ipr_topology_cache_mib",
            "artist_note",
            "picture",
            "take",
        )
    }


before = channels()
assert ensure_ipr_settings_layout(node)
layout()
assert channels() == before
script = node.parmTemplateGroup().asDialogScript()
assert not ensure_ipr_settings_layout(node) and node.parmTemplateGroup().asDialogScript() == script

# Save the previous layout and exercise the real OnLoaded update boundary.
node.setParmTemplateGroup(group)
assert node.parm("ipr_samples").containingFolders()[:2] == ("Rendering", "Sampling")
scene = out.with_suffix(".hiplc")
hou.hipFile.save(str(scene), save_to_recent_files=False)
hou.hipFile.load(str(scene), suppress_save_prompt=True, ignore_load_warnings=False)
node = hou.node("/out/ipr_layout")
layout()
assert channels() == before

# A familiar section label inside an artist folder is deliberate customization.
custom = node.parmTemplateGroup()
section_indices = custom.findIndices("ipr_samples")[:-1]
section = custom.entryAtIndices(section_indices)
section.setLabel("Interactive Render")
custom.remove(section_indices)
custom.append(hou.FolderParmTemplate("artist_render", "Artist Render", (section,)))
node.setParmTemplateGroup(custom)
custom_script = node.parmTemplateGroup().asDialogScript()
assert not ensure_ipr_settings_layout(node)
assert node.parmTemplateGroup().asDialogScript() == custom_script
assert channels() == before
assert node.parm("ipr_samples").containingFolders()[0] == "Artist Render"
# Restore the qualified default layout for the report below.
node.setParmTemplateGroup(group)
assert ensure_ipr_settings_layout(node)
layout()
out.write_text(
    json.dumps(
        dict(
            result="passed",
            runtime=__import__("cycles_houdini").__file__,
            checks=[
                "New ROP uses IPR Settings / Sampling with Denoising below",
                "Saved per-node layouts move once; custom parameters, values, expressions and keyframes survive",
                "HIP save/reload runs the OnLoaded update",
                "Final Render sampling and tessellation controls retain their locations",
                "The IPR topology cache budget appears in Subdivision with help and an authored channel survives migration",
            ],
            folders={name: node.parm(name).containingFolders() for name in (*names, "ipr_topology_cache_mib")},
        ),
        indent=2,
    )
)
print(json.dumps(dict(result="passed", report=str(out))))
