# SPDX-License-Identifier: Apache-2.0
"""Current ROP layout, device ownership, settings, animation and self round-trip."""

from support.paths import artifacts_root
import pass_fixtures as pass_fixture

import argparse, json, os
from collections import Counter
from pathlib import Path
import hou
from cycles_houdini import native_scene, device_selection, rop_controls, materials, output

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
out = artifacts_root() / "rop-layout"
out.mkdir(parents=True, exist_ok=True)
expected = ["Rendering", "Scene", "Image Output", "Performance", "Devices", "Scripts", "About"]
checks = []
rop = hou.node("/out").createNode("cycles_ipr", "current_layout")
assert rop.parm("picture").unexpandedString() == "$HIP/render/$HIPNAME.$OS.$F4.exr"
assert rop.type().minNumInputs() == 0 and rop.type().maxNumInputs() == 9999
assert rop.numOrderedInputs() == 0
device_selection.set_devices(rop, device)


def locations(node):
    result = {}
    names = []

    def walk(entries, path=()):
        for p in entries:
            if isinstance(p, hou.FolderParmTemplate):
                assert p.parmTemplates() or p.folderType() == hou.folderType.MultiparmBlock, (
                    "Empty current folder",
                    p.name(),
                )
                if p.folderType() == hou.folderType.MultiparmBlock:
                    result[p.name()] = path + (p.label(),)
                walk(p.parmTemplates(), path + (p.label(),))
            else:
                names.append(p.name())
                result[p.name()] = path

    walk(node.parmTemplateGroup().entries())
    assert all(v == 1 for v in Counter(names).values()), "Duplicate current controls"
    return result


def check_layout(node):
    tabs = [
        p.label()
        for p in node.parmTemplateGroup().entries()
        if isinstance(p, hou.FolderParmTemplate) and p.folderType() == hou.folderType.Tabs and not p.isHidden()
    ]
    assert tabs == expected, tabs
    for name in (
        "device",
        "device_legacy_seen",
        "device_selection_version",
        "ipr_sampling_override",
        "ipr_denoise_override",
        "width",
        "height",
        "resolution_preset",
    ):
        assert pass_fixture.parm(node, name) is None, name
    paths = locations(node)
    for name, path in [
        ("denoise_start_sample", ("Rendering", "Sampling", "Final Render", "Denoising")),
        ("ipr_denoise_start_sample", ("Rendering", "IPR Settings", "Sampling", "Denoising")),
    ]:
        assert paths[name] == path, (name, paths[name])
        assert pass_fixture.parm(node, name).parmTemplate().label() == "Start Sample"
    assert paths["f"] == () and paths["trange"] == () and paths["take"] == ()
    top_level = [p.name() for p in node.parmTemplateGroup().entries()]
    assert top_level[:4] == ["ch_ipr", "execute", "ch_render_mplay", "ch_controls"], top_level[:4]
    assert top_level.index("take") == top_level.index("f") + 1
    for name in ("camera", "picture", "res", "res_menu", "resolution_percent"):
        assert paths[name] == ("Common Settings",), (name, paths[name])
    for name, tab, panel in [
        ("dof_mode", "Rendering", "Camera Effects"),
        ("allowmotionblur", "Rendering", "Camera Effects"),
        ("transparent_glass", "Image Output", "Transparent"),
        ("vobject", "Scene", "Objects"),
        ("materialpath", "Scene", "Materials"),
        ("background", "Scene", "Environment"),
        ("noise_threshold", "Rendering", "Sampling"),
        ("ipr_samples", "Rendering", "IPR Settings"),
        ("samples", "Rendering", "Final Render"),
        ("device_mode", "Devices", "Devices"),
        ("world_volume_sampling", "Scene", "Environment"),
    ]:
        assert paths[name][0] == tab and panel in paths[name], (name, paths[name])
    assert paths["nested_dielectrics"] == ("Rendering", "Light Paths"), paths["nested_dielectrics"]
    assert paths["tessellation_update"] == ("Rendering", "IPR Settings"), paths["tessellation_update"]
    assert paths["refresh_tessellation"] == ("Rendering", "IPR Settings"), paths["refresh_tessellation"]
    assert not node.parm("tessellation_update").parmTemplate().isHidden()
    light_paths = [p.name() for p in node.parmTemplateGroup().findFolder(("Rendering", "Light Paths")).parmTemplates()]
    assert light_paths.index("nested_dielectrics") + 1 == light_paths.index("fast_gi"), light_paths
    assert pass_fixture.parm(node, "nested_dielectrics").parmTemplate().label() == "Nested Dielectrics"
    assert device_selection.gpu_parameters(node)
    resolution = node.parmTemplateGroup().find("res")
    assert (
        resolution.numComponents() == 2
        and resolution.joinsWithNext()
        and resolution.namingScheme() == hou.parmNamingScheme.XYZW
    )
    menu = node.parmTemplateGroup().find("res_menu")
    assert menu.type() == hou.parmTemplateType.Button and menu.menuType() == hou.menuType.Mini and menu.isLabelHidden()
    group = node.parmTemplateGroup()
    # Houdini renames folder-set templates when a per-node device list is
    # rebuilt. Folder labels describe their stable location in the live UI.
    for folder_path, toggle, children in (
        (("Performance", "Tiling (Final Render)"), "use_tiling", ("tile_size",)),
        (
            ("Performance", "Texture Cache"),
            "use_texture_cache",
            ("auto_texture_cache", "texture_cache_generate", "texture_cache_clear"),
        ),
        (
            ("Image Output", "Film", "Transparent"),
            "transparent_background",
            ("transparent_glass", "transparent_roughness_threshold"),
        ),
    ):
        folder = group.findFolder(folder_path)
        assert folder is not None, folder_path
        assert folder.folderType() == hou.folderType.Collapsible
        assert folder.tags().get("sidefx::header_toggle") == toggle
        assert tuple(parm.name() for parm in folder.parmTemplates()) == children
    assert paths["use_tiling"] == paths["use_texture_cache"] == ("Performance",)
    assert paths["transparent_background"] == ("Image Output", "Film")
    assert group.find("texture_cache_generate").joinsWithNext()
    for name in ("texture_cache_generate", "texture_cache_clear"):
        assert group.find(name).type() == hou.parmTemplateType.Button
        assert "from cycles_houdini import texture_cache" in group.find(name).scriptCallback()
    return paths


assert pass_fixture.evaluate(rop, "samples") == 4096 and pass_fixture.evaluate(rop, "sampling_pattern") == "auto"
assert pass_fixture.evaluate(rop, "adaptive_sampling") == pass_fixture.evaluate(rop, "use_denoise") == 1
assert pass_fixture.evaluate(rop, "ipr_samples") == 1024
assert pass_fixture.evaluate(rop, "nested_dielectrics") == 0  # Off renders native Cycles
assert native_scene.rendering_configuration(rop, hou.time())["nested_dielectrics"] is False
assert pass_fixture.evaluate(rop, "world_volume_sampling") == "equiangular"  # Blender's world default
assert pass_fixture.evaluate(rop, "use_tiling") == 1
assert pass_fixture.evaluate(rop, "tile_size") == 2048
for state in (0, 1):
    pass_fixture.parm(rop, "use_tiling").set(state)
    pass_fixture.parm(rop, "use_texture_cache").set(state)
    pass_fixture.parm(rop, "transparent_background").set(state)
    pass_fixture.parm(rop, "transparent_glass").set(state)
    rop.updateParmStates()
    for name in ("tile_size", "auto_texture_cache", "transparent_glass", "transparent_roughness_threshold"):
        assert pass_fixture.parm(rop, name).isDisabled() == (not state), (name, state)
pass_fixture.parm(rop, "transparent_background").set(1)
pass_fixture.parm(rop, "transparent_glass").set(0)
rop.updateParmStates()
assert not pass_fixture.parm(rop, "transparent_glass").isDisabled()
assert pass_fixture.parm(rop, "transparent_roughness_threshold").isDisabled()
pass_fixture.parm(rop, "transparent_background").set(0)


def environment_order():
    return [p.name() for p in rop.parmTemplateGroup().findFolder(("Scene", "Environment")).parmTemplates()]


environment = environment_order()
assert (
    environment.index("background") + 1
    == environment.index("world_volume_sampling")
    == environment.index("world_sampling") - 1
), environment
# A ROP saved before a definition change stores its own layout (the GPU list
# makes it per-node), where new controls would land at the end. Loading
# rebuilds the layout from the definition: simulate a stale stored layout.
stale = rop.parmTemplateGroup()
moved = stale.find("world_volume_sampling")
stale.remove("world_volume_sampling")
stale.append(moved)
spare = hou.FloatParmTemplate("artist_spare", "Artist Spare", 1, default_value=(7,))
stale.append(spare)
rop.setParmTemplateGroup(stale)
pass_fixture.parm(rop, "artist_spare").set(3)
assert "world_volume_sampling" not in environment_order()
device_selection.refresh(rop)
assert environment_order() == environment, environment_order()
assert pass_fixture.evaluate(rop, "artist_spare") == 3, "Artist spare parameter lost"
stale = rop.parmTemplateGroup()
stale.remove("artist_spare")
rop.setParmTemplateGroup(stale)
checks.append(
    "A stored per-node layout is rebuilt from the definition; new controls appear in place and spare parameters survive"
)
check_layout(rop)
checks.append("Current controls have one owner and no compatibility fields or empty folders")
pass_fixture.parm(rop, "resolution_percent").set(50)
# The mini menu offers Houdini's standard resolutions (FBres) and sets both
# components and the pixel aspect in one step.
# Evaluate the ROP's own menu script as Houdini does: a one-line Python menu
# script is an expression. (hou.Parm.menuItems() does not serve button menus.)
script = pass_fixture.parm(rop, "res_menu").parmTemplate().itemGeneratorScript()
assert "\n" not in script.strip() and not script.lstrip().startswith("return"), script
items = list(eval(script, {"hou": hou, "kwargs": dict(node=rop, parm=pass_fixture.parm(rop, "res_menu"))}))[0::2]
assert (
    not rop.parmTuple("res").isHidden() and not pass_fixture.parm(rop, "res_menu").isHidden()
)  # res is a 2-component tuple
tokens = [item for item in items if item != "_separator_"]  # Parm.menuItems() returns tokens
assert "1920 1080 1" in tokens and "1280 720 1" in tokens, tokens
for token in tokens:
    width, height, aspect = token.split()
    rop_controls.apply_resolution_menu(dict(node=rop, script_value=token))
    assert (
        rop.parmTuple("res").eval() == (int(width), int(height))
        and abs(pass_fixture.evaluate(rop, "pixel_aspect") - float(aspect)) < 1e-6
    )
    assert output.dimensions(rop, hou.time(), interactive=False) == (int(width) // 2, int(height) // 2)
pass_fixture.set_parameters(rop, dict(resx=333, resy=222, pixel_aspect=1))
assert output.dimensions(rop, hou.time(), interactive=False) == (166, 111)
checks.append("Resolution is one integer vector with the standard resolution menu; percentages scale it")
world = materials.create_world_material(name="picker_world")
surface = materials.create_material(name="picker_surface")
assert world.path() in pass_fixture.parm(rop, "world_material").menuItems()
assert surface.path() in pass_fixture.parm(rop, "materialpath").menuItems()
assert world.path() in rop_controls.material_menu(True) and surface.path() not in rop_controls.material_menu(True)
assert surface.path() in rop_controls.material_menu(False) and world.path() not in rop_controls.material_menu(False)
camera = hou.node("/obj").createNode("cam", "picker_camera")
pass_fixture.set_parameters(rop, dict(camera=camera.path(), world_source="material", world_material=world.path()))
native_scene.capture(rop)
checks.append("World and surface pickers resolve current materials")
for interactive in (False, True):
    pass_fixture.parm(rop, "viewer_interactive").set(interactive)
    before = native_scene.denoising_configuration(rop, hou.time(), interactive=False)
    native_scene.set_interactive_denoising(rop, False)
    assert not native_scene.denoising_configuration(rop, hou.time())["enabled"]
    if interactive:
        assert native_scene.denoising_configuration(rop, hou.time(), interactive=False) == before
    native_scene.set_interactive_denoising(rop, True)
    assert native_scene.denoising_configuration(rop, hou.time())["enabled"]
pass_fixture.set_parameters(
    rop, dict(use_denoise=1, denoise_start_sample=64, ipr_use_denoise=1, ipr_denoise_start_sample=10)
)
rop.updateParmStates()
assert not pass_fixture.parm(rop, "ipr_denoise_start_sample").isDisabled()
pass_fixture.parm(rop, "viewer_interactive").set(1)
assert native_scene.denoising_configuration(rop, hou.time())["start_sample"] == 10
assert native_scene.denoising_configuration(rop, hou.time(), interactive=False)["start_sample"] == 64
pass_fixture.parm(rop, "viewer_interactive").set(0)
assert native_scene.denoising_configuration(rop, hou.time())["start_sample"] == 64
pass_fixture.parm(rop, "viewer_interactive").set(1)
checks.append("Viewer quality and denoising use exactly the selected current settings")
pass_fixture.parm(rop, "noise_threshold").setExpression(".01+$F*.001", hou.exprLanguage.Hscript)
pass_fixture.set_parameters(
    rop,
    dict(
        samples=123,
        resx=720,
        use_tiling=0,
        tile_size=37,
        aov_count=1,
        aov_name1="artist_mask",
        lightgroup_count=1,
        lightgroup_name1="key",
    ),
)
selection = device_selection.selected_devices(rop, hou.time())
path = out / "current-layout.hiplc"
hou.hipFile.save(str(path))
hou.hipFile.clear(suppress_save_prompt=True)
hou.hipFile.load(str(path), suppress_save_prompt=True)
rop = hou.node("/out/current_layout")
paths = check_layout(rop)
assert pass_fixture.evaluate(rop, "samples") == 123 and pass_fixture.evaluate(rop, "resx") == 720
assert pass_fixture.evaluate(rop, "use_tiling") == 0 and pass_fixture.evaluate(rop, "tile_size") == 37
assert pass_fixture.parm(rop, "noise_threshold").expression() == ".01+$F*.001"
assert (
    pass_fixture.evaluate(rop, "aov_name1") == "artist_mask" and pass_fixture.evaluate(rop, "lightgroup_name1") == "key"
)
assert device_selection.selected_devices(rop, hou.time()) == selection
checks.append("Current HIP reload preserves animation, AOVs, light groups and device selection without migration")
for fixture in ("release-existing.hiplc", "legacy-rgba-color-ramps.hiplc"):
    hou.hipFile.load(str(root / "tests/fixtures" / fixture), suppress_save_prompt=True, ignore_load_warnings=True)
    rops = [node for node in hou.node("/out").allSubChildren() if node.type().name() == "cycles_ipr"]
    assert not rops, (fixture, [node.path() for node in rops])
checks.append("Both tracked legacy HIP fixtures load and contain no authored Cycles ROP object patterns to migrate")
report = dict(result="passed", tabs=expected, locations=paths, checks=checks, device=device, scope=__doc__)
(out / "houdini-rop-layout-smoke.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result="passed", checks=len(checks))))
