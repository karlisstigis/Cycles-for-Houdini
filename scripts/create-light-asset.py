# SPDX-License-Identifier: Apache-2.0
"""Author the Cycles native OBJ light using public Houdini operators."""

import sys
import types
from pathlib import Path

import hou

root = Path(__file__).resolve().parent.parent
# Template authoring has no renderer dependency, including on the first build.
package = types.ModuleType("cycles_houdini")
package.__path__ = [str(root / "houdini/python3.13libs/cycles_houdini")]
sys.modules["cycles_houdini"] = package
from cycles_houdini import light_properties, shader_help


def add_native_controls(group):
    """Provide the controls Houdini uses to draw and shade the light."""
    hscript = hou.scriptLanguage.Hscript
    python = hou.scriptLanguage.Python

    def float_parm(name, default, expression="", language=hscript):
        return hou.FloatParmTemplate(
            name,
            name,
            1,
            default_value=(default,),
            default_expression=(expression,),
            default_expression_language=(language,),
        )

    controls = [
        float_parm("ogl_enablelight", 1, 'ch("light_enable")'),
        float_parm("light_contribprimary", 0, 'ch("cycles_light_visibility_camera")'),
        hou.StringParmTemplate(
            "light_type",
            "Native Light Type",
            1,
            default_value=("grid",),
            default_expression=(
                "from cycles_houdini.light_properties import viewport_light_type\n"
                "return viewport_light_type(hou.pwd())",
            ),
            default_expression_language=(python,),
        ),
        hou.FloatParmTemplate(
            "light_color",
            "Native Light Color",
            3,
            naming_scheme=hou.parmNamingScheme.RGBA,
            default_value=(1, 1, 1),
            default_expression=tuple(
                f"from cycles_houdini.light_properties import emission_color\nreturn emission_color(hou.pwd())[{index}]"
                for index in range(3)
            ),
            default_expression_language=(python,) * 3,
        ),
        hou.FloatParmTemplate(
            "areasize",
            "Native Area Size",
            2,
            naming_scheme=hou.parmNamingScheme.Base1,
            default_value=(1, 1),
            default_expression=(
                'ch("areasizex")',
                "from cycles_houdini.light_properties import area_size\nreturn area_size(hou.pwd())[1]",
            ),
            default_expression_language=(hscript, python),
        ),
        float_parm("coneenable", 0, 'int(hou.pwd().evalParm("cycles_light_type") == "spot")', python),
        float_parm(
            "coneangle",
            45,
            "from cycles_houdini.light_viewer_state import _inner_angle\n"
            "node = hou.pwd()\n"
            "return _inner_angle(node.evalParm('cycles_light_spot_angle'), node.evalParm('cycles_light_spot_smooth'))",
            python,
        ),
        float_parm("conedelta", 10, '(ch("cycles_light_spot_angle") - ch("coneangle")) * 0.5'),
        hou.MenuParmTemplate(
            "shadow_type",
            "Native Shadow Type",
            ("off", "raytrace"),
            ("Off", "Raytraced"),
            default_value=1,
            default_expression='ch("cycles_light_cast_shadow")',
            default_expression_language=hscript,
        ),
        hou.StringParmTemplate("light_wrangler", "Native Light Wrangler", 1, default_value=("HoudiniLight",)),
        hou.MenuParmTemplate(
            "atten_type",
            "Attenuation",
            ("none", "half", "physical"),
            ("None", "Half Distance", "Physical"),
            default_value=2,
        ),
        hou.MenuParmTemplate("projection2", "Projection", ("perspective", "ortho"), ("Perspective", "Orthographic")),
        hou.ToggleParmTemplate("light_conefov", "Use Viewing Cone", default_value=False),
    ]
    controls.extend(
        float_parm(name, default)
        for name, default in (
            ("near", 0.001),
            ("far", 10000),
            ("focus", 10),
            ("focal", 1),
            ("aperture", 0.828427),
            ("atten_dist", 10),
            ("atten_start", 0),
            ("coneroll", 1),
            ("shadow_intensity", 1),
            ("shadow_bias", 0.05),
            ("shadow_quality", 1),
            ("shadow_blur", 0),
            ("orthowidth", 2),
            ("iconscale", 1),
            ("l_dimmer", 1),
        )
    )
    controls.extend(
        hou.ToggleParmTemplate(name, label, default_value=True)
        for name, label in (
            ("selfshadow", "Self Shadows"),
            ("shadow_transparent", "Transparent Shadows"),
            ("diffuse", "Diffuse Contribution"),
            ("specular", "Specular Contribution"),
            ("light_contribdiff", "Diffuse Contribution"),
            ("light_contribspec", "Specular Contribution"),
        )
    )
    for parm in controls:
        if group.find(parm.name()) is None:
            parm.hide(True)
            group.append(parm)


def light_parameters(node):
    group = node.parmTemplateGroup()
    # Keep Houdini's transform controls and place light controls in the Cycles tab.
    for index, entry in enumerate(group.entries()):
        if not (isinstance(entry, hou.FolderParmTemplate) and entry.label() == "Transform"):
            entry.hide(True)
            group.replace((index,), entry)
    for name in ("categories", "shadowmask"):
        if group.find(name) is not None:
            group.remove(name)

    cycles = hou.ParmTemplateGroup([light_properties.custom_template()])
    shadow = cycles.find("cycles_shadow_objects")
    shadow.setName("shadowmask")
    shadow.setHelp(
        "Select which OBJ roots can block the light. This is the Shadow Mask edited by the Houdini Light Linker."
    )
    cycles.replace("cycles_shadow_objects", shadow)
    selection = cycles.find("shadowcategories")
    selection.setHelp("Optional native category expression intersected with this light's Shadow Casters.")
    cycles.replace("shadowcategories", selection)
    group = light_properties.install_tabs(group, cycles.entries()[0])
    add_native_controls(group)
    return group


def add_viewer_state(definition):
    # The native OBJ header adds its stock bindings. The Cycles state owns the
    # transform and light handles, so avoid binding those twice.
    script = definition.sections()["DialogScript"].contents()
    definition.addSection(
        "DialogScript", "\n".join(line for line in script.split("\n") if not line.lstrip().startswith("bindhandle "))
    )
    definition.addSection("DefaultState", "cycles_light_handles")
    definition.setExtraFileOption("ViewerStateModule/IsPython", True)
    definition.addSection(
        "ViewerStateModule",
        "from cycles_houdini.light_viewer_state import create_template\n\n"
        "def createViewerStateTemplate():\n    return create_template()\n",
    )
    definition.setExtraFileOption("OnInstall/IsPython", True)
    definition.addSection(
        "OnInstall",
        "import hou\n"
        "from cycles_houdini.light_viewer_state import STATE_NAME\n"
        "if hou.isUIAvailable() and not hou.ui.isRegisteredViewerState(STATE_NAME):\n"
        '    hou.ui.registerViewerState(kwargs["type"].hdaViewerStateModule().createViewerStateTemplate())\n',
    )
    definition.setExtraFileOption("OnUninstall/IsPython", True)
    definition.addSection(
        "OnUninstall",
        "import hou\n"
        "from cycles_houdini.light_viewer_state import STATE_NAME\n"
        "if hou.isUIAvailable() and hou.ui.isRegisteredViewerState(STATE_NAME):\n"
        "    hou.ui.unregisterViewerState(STATE_NAME)\n",
    )


path = Path(sys.argv[1])
path.parent.mkdir(parents=True, exist_ok=True)
# Suppress stock initialization so the asset contains only our authored guide.
node = hou.node("/obj").createNode("light", "cycles_light_asset", run_init_scripts=False, exact_type_name=True)
group = light_parameters(node)

guide = node.createNode("python", "cycles_light_guide")
guide.parm("python").set("from cycles_houdini.light_viewer_state import build_guide\nbuild_guide(hou.pwd())")
guide.setDisplayFlag(True)
guide.setRenderFlag(True)

node = node.createDigitalAsset(
    name=light_properties.LIGHT_TYPE,
    hda_file_name=str(path),
    description="Cycles Light",
    min_num_inputs=0,
    max_num_inputs=1,
)
definition = node.type().definition()
definition.setIcon("cycles_area_light")
definition.setParmTemplateGroup(group)
definition.setExtraInfo("subtype=light ")
add_viewer_state(definition)
help_card, _, missing_help = shader_help.light_help_card(group)
if missing_help:
    raise RuntimeError("Undocumented Cycles Light controls: " + repr(missing_help))
definition.addSection("Help", help_card)
definition.save(str(path), create_backup=False)
print("Created Cycles Light asset: " + str(path))
