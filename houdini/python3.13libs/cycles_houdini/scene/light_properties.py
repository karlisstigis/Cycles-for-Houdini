# SPDX-License-Identifier: Apache-2.0
"""Optional native-Houdini light properties, also authored into Cycles Light."""

import hou
import math
from functools import lru_cache
from .object_properties import _toggle, _float, _integer, _condition, _hide, _section, install_tabs, properties_folder

LIGHT_TYPE = "cycles::light::1"


def _document(folder):
    """Fill renderer-specific hover help on the HDA and native spare tabs."""
    from ..materials.shader_help import LIGHT_PARAMETERS

    children = []
    for parm in folder.parmTemplates():
        if isinstance(parm, hou.FolderParmTemplate):
            parm = _document(parm)
        elif not parm.help() and parm.name() in LIGHT_PARAMETERS:
            parm.setHelp(LIGHT_PARAMETERS[parm.name()])
        children.append(parm)
    folder.setParmTemplates(children)
    return folder


def area_size(node, now=None):
    """Full emitter dimensions shared by scene extraction and viewport controls."""
    now = hou.time() if now is None else now
    if not is_cycles_light(node):
        return node.parmTuple("areasize").evalAtTime(now)
    x = node.parm("areasizex").evalAtTime(now)
    shape = node.parm("cycles_light_area_shape").evalAtTime(now)
    return (x, x if shape in ("square", "disk") else node.parm("areasizey").evalAtTime(now))


@lru_cache(maxsize=128)
def _temperature_color(kelvin, color_identity):
    from .. import _native

    return _native.blackbody_temperature_color(kelvin)


def emission_color(node, now=None):
    """Light tint multiplied by Blender/Cycles blackbody RGB, before power."""
    now = hou.time() if now is None else now
    color = node.parmTuple("cycles_light_color" if is_cycles_light(node) else "light_color")
    rgb = color.evalAtTime(now) if color else (1.0, 1.0, 1.0)
    enabled = node.parm("cycles_light_use_temperature")
    if enabled and enabled.evalAtTime(now):
        from .. import color_policy

        kelvin = node.parm("cycles_light_temperature").evalAtTime(now)
        if not math.isfinite(kelvin) or not 800 <= kelvin <= 20000:
            raise ValueError("Light Temperature must be finite and between 800 and 20000 K: " + node.path())
        tint = _temperature_color(kelvin, color_policy()["configuration_id"])
        rgb = tuple(a * b for a, b in zip(rgb, tint))
    return rgb


def light_type(node, now=None):
    """Read the editable Cycles type or a stock light's native type."""
    now = hou.time() if now is None else now
    parm = node.parm("cycles_light_type" if is_cycles_light(node) else "light_type")
    return parm.evalAsStringAtFrame(hou.timeToFrame(now)) if parm else ""


def viewport_light_type(node):
    kind = light_type(node)
    if kind == "spot":
        return "point"
    if kind == "grid" and node.evalParm("cycles_light_area_shape") in ("disk", "ellipse"):
        return "disk"
    return kind


def temperature_template():
    temperature = _float(
        "light_temperature",
        "Temperature",
        6500,
        800,
        20000,
        "Blackbody temperature in Kelvin, converted to the render working space with the Cycles Blackbody approximation.",
    )
    temperature.setMaxIsStrict(True)
    return [
        _toggle(
            "light_use_temperature",
            "Use Temperature",
            False,
            "Multiply Color by blackbody RGB, matching Blender. Leave Color white for the temperature color alone.",
        ),
        _condition(temperature, "{ cycles_light_use_temperature == 0 }"),
    ]


def emission_template():
    material = hou.StringParmTemplate(
        "cycles_light_material",
        "Emission Material",
        1,
        default_value=("",),
        string_type=hou.stringParmType.NodeReference,
        tags={"opfilter": "!!VOP!!", "oprelative": "."},
        help="Cycles Light Material with Emission connected to Light Output. Its emission multiplies the light color, temperature and power. Overrides the texture/profile shortcuts below; broken assignments retain the base light and report a warning.",
    )
    material.setJoinWithNext(True)
    create = hou.ButtonParmTemplate(
        "cycles_light_material_create",
        "+",
        help="Create and assign a Cycles Light Material in /mat, named after this light.",
        script_callback="from cycles_houdini.scene.light_materials import action; action(kwargs)",
        script_callback_language=hou.scriptLanguage.Python,
    )
    create.setConditional(hou.parmCondType.HideWhen, '{ cycles_light_material != "" }')
    create.setJoinWithNext(True)
    edit = hou.ButtonParmTemplate(
        "cycles_light_material_open",
        "Open",
        help="Select and frame the assigned light material in the material network.",
        script_callback="from cycles_houdini.scene.light_materials import action; action(kwargs)",
        script_callback_language=hou.scriptLanguage.Python,
    )
    edit.setConditional(hou.parmCondType.HideWhen, '{ cycles_light_material == "" }')
    texture = hou.StringParmTemplate(
        "cycles_light_texture",
        "Color Texture",
        1,
        default_value=("",),
        string_type=hou.stringParmType.FileReference,
        help="File or owned op: image. Area lights map the full width/height to 0..1. Point, Spot and Sun use equirectangular local outgoing directions. Multiplies Color and Power. Missing inputs disable this modulation, preserving the light.",
    )
    space = hou.StringParmTemplate(
        "cycles_light_texture_colorspace",
        "Input Color Space",
        1,
        default_value=("auto",),
        menu_type=hou.menuType.StringReplace,
        item_generator_script="import cycles_houdini as ch\nitems=['auto','Auto from Houdini and file','data','Data','scene_linear','Render working space']\nfor name in ch.color_policy()['texture_spaces']:\n    if name not in ('auto','data','scene_linear'): items.extend([name,name])\nreturn items",
        item_generator_script_language=hou.scriptLanguage.Python,
        help="Follows the active Houdini OCIO configuration. Invalid interpretations disable only this color modulation and report a warning.",
    )
    ies = hou.StringParmTemplate(
        "cycles_light_ies",
        "IES Profile",
        1,
        default_value=("",),
        string_type=hou.stringParmType.FileReference,
        help="IES photometry, with its forward axis along local -Z. Multiplies emission using Cycles D65 candela-to-radiometric conversion. Power 1 preserves the file-derived scale; other powers scale it. The profile is not renormalized to total power. TILT=NONE only; unusable profiles retain the light without this modulation. File-only repairs are observed during IPR.",
    )
    return _section(
        "light_emission",
        "Emission",
        [
            material,
            create,
            edit,
            _condition(texture, '{ cycles_light_material != "" }'),
            _condition(space, '{ cycles_light_material != "" }'),
            _condition(ies, '{ cycles_light_material != "" }'),
        ],
    )


PORTAL_HELP = (
    "Blender Light > Portal. The light emits nothing and guides environment sampling through its shape, which "
    "reduces noise in interiors lit through windows. Place it in the opening with its light direction pointing "
    "into the room. Needs an environment; Color, Power, Spread and the light's own sampling settings are ignored."
)


def _portal_hidden(parm, condition):
    """Hide a control a portal ignores; keeps any existing hide condition."""
    current = parm.conditionals().get(hou.parmCondType.HideWhen, "")
    parm.setConditional(hou.parmCondType.HideWhen, (current + " " + condition).strip())
    return parm


def is_cycles_light(node):
    return node.type().category() == hou.objNodeTypeCategory() and node.type().name() == LIGHT_TYPE


def supported(node):
    return node.type().category() == hou.objNodeTypeCategory() and (
        is_cycles_light(node) or node.type().name().split("::")[0] in ("hlight", "envlight")
    )


def template(environment=False):
    from .linking import parameter_template as linking_template

    sampling = [_toggle("light_use_mis", "Multiple Importance Sampling", True)]
    shape = [
        _float(
            "light_radius",
            "Point / Spot Radius",
            0,
            0,
            1,
            "Radius in scene units, independent of object scale. A zero-radius point or spot cannot be seen as a surface.",
        ),
        _toggle("light_is_sphere", "Spherical Point / Spot", True),
        _float("light_spread", "Area Spread", 180, 0, 180, "Degrees; 180 emits over the full hemisphere."),
        _float(
            "light_spot_angle",
            "Spot Angle",
            45,
            0.001,
            180,
            "Cycles Light Spot: full cone angle in degrees. Standard Houdini lights use Cone Angle, Cone Delta and Cone Rolloff in their native Light tab.",
        ),
        _float(
            "light_spot_smooth",
            "Spot Blend",
            0,
            0,
            1,
            "Cycles Light Spot blend. Standard Houdini lights use their native cone controls.",
        ),
    ]
    shape[0].setMaxIsStrict(False)
    portal = "{ light_type == grid cycles_light_is_portal == 1 } { light_type == disk cycles_light_is_portal == 1 }"
    if not environment:
        shape.append(
            _hide(_toggle("light_is_portal", "Portal", False, PORTAL_HELP), "{ light_type != grid light_type != disk }")
        )
    for parm in shape:
        if parm.name() == "cycles_light_is_portal":
            continue
        if parm.name() in ("cycles_light_radius", "cycles_light_is_sphere"):
            _condition(parm, "{ light_type != point light_type != spot }")
        elif parm.name() == "cycles_light_spread":
            _portal_hidden(_condition(parm, "{ light_type != grid light_type != disk }"), portal)
        else:
            _condition(parm, "{ light_type != spot }")
    if environment:
        space = hou.StringParmTemplate(
            "cycles_environment_colorspace",
            "Input Color Space",
            1,
            default_value=("auto",),
            help="Auto follows Houdini OCIO and file metadata. Data bypasses conversion; scene_linear means the render working space.",
            menu_type=hou.menuType.StringReplace,
            item_generator_script="import cycles_houdini as ch\nitems=['auto','Auto from Houdini and file','data','Data','scene_linear','Render working space']\nfor name in ch.color_policy()['texture_spaces']:\n    if name not in ('auto','data','scene_linear'): items.extend([name,name])\nreturn items",
            item_generator_script_language=hou.scriptLanguage.Python,
        )
        resolution = _integer(
            "environment_map_resolution",
            "Map Resolution",
            0,
            0,
            16384,
            "0 means Auto; a manual width must be 16..16384. This changes environment importance sampling, not the source image resolution. The preset menu offers valid values; custom widths may be entered.",
        )
        values = ("0", "16", "32", "64", "128", "256", "512", "1024", "2048", "4096", "8192", "16384")
        resolution.setMenuItems(values)
        resolution.setMenuLabels(("Auto",) + values[1:])
        resolution.setMenuUseToken(True)
        resolution.setMenuType(hou.menuType.StringReplace)
        # A continuous slider crosses invalid 1..15 widths between Auto and
        # the supported manual range. Use a numeric entry with valid presets.
        resolution.setTags({"sidefx::slider": "none"})
        shape = [space]
        sampling.append(_condition(resolution, "{ cycles_light_use_mis == 0 }"))
    visibility = [
        _condition(
            _toggle("light_visibility_" + name, label, name != "camera"), "{ cycles_light_visibility_override == 0 }"
        )
        for name, label in (
            ("camera", "Camera"),
            ("diffuse", "Diffuse"),
            ("glossy", "Glossy"),
            ("transmission", "Transmission"),
            ("volume", "Volume Scatter"),
        )
    ]
    entries = [
        hou.StringParmTemplate(
            "cycles_lightgroup",
            "Light Group",
            1,
            default_value=("",),
            help="Contribution from this light belongs to this group. Enable its RGB output under Image Output / Passes / Light Groups on the Cycles ROP. Empty means ungrouped; unregistered groups still illuminate Beauty. Names are case sensitive ASCII identifiers.",
        ),
        _toggle(
            "light_shadow_catcher",
            "Shadow Catcher Reference",
            True,
            "Include this light in the real/reference lighting used to calculate the Shadow Catcher pass. Leave on for real lights. Turn off for added virtual lighting; ordinary Beauty lighting is unchanged.",
        ),
        _section("light_shape", "Environment" if environment else "Shape", shape, expanded=True),
        *([] if environment else [*temperature_template(), emission_template()]),
        *([] if environment else [linking_template()]),
        _section(
            "light_sampling",
            "Sampling",
            sampling
            + [
                _integer("light_max_bounces", "Max Bounces", 1024, 0, 1024),
                _toggle(
                    "light_use_caustics",
                    "Shadow Caustics",
                    False,
                    "Include this light in shadow-caustic evaluation. Requires a refractive object with Cast Shadow Caustics and a receiver with Receive Shadow Caustics enabled in their Cycles properties.",
                ),
                _toggle("light_cast_shadow", "Cast Shadow", True, "Also honors the native Shadow Type control."),
            ],
        ),
        _toggle(
            "light_visibility_override",
            "Override Ray Visibility",
            help="Off follows native Render Light Geometry and normal light contributions.",
        ),
        _section("light_visibility", "Ray Visibility", visibility, "light_visibility_override"),
    ]
    if not environment:
        for parm in entries:
            if parm.name() != "cycles_light_shape":
                _portal_hidden(parm, portal)
    return _document(
        hou.FolderParmTemplate(
            "cycles_light_properties",
            "Cycles Light",
            entries,
            folder_type=hou.folderType.Tabs,
            tags={
                "cycles_light_properties_version": "14",
                "cycles_environment_properties": "1" if environment else "0",
            },
        )
    )


def install(node):
    if not supported(node):
        return False
    if is_cycles_light(node):
        # The full light interface belongs to the asset, not a spare tab.
        return False
    group = node.parmTemplateGroup()
    environment = node.type().name().split("::")[0] == "envlight"
    folder = properties_folder(group, "lights")
    if folder and group.find("cycles_light_visibility_override"):
        # Reinstalling adds controls introduced since (Portal); values are kept by name.
        if environment or group.find("cycles_light_is_portal"):
            return False
    elif any(parm.name().startswith("cycles_") for parm in group.entriesWithoutFolders()):
        raise ValueError("Cannot install Cycles light properties over conflicting parameters: " + node.path())
    group = install_tabs(group, template(environment))
    node.setParmTemplateGroup(group)
    return True


def custom_template():
    """One native tab, with panels following Blender's Cycles light layout.

    The same controls drive scene translation and the native viewport light.
    """
    from .linking import parameter_template as linking_template

    def real(name, label, default, minimum=0, maximum=10, help=""):
        return hou.FloatParmTemplate(
            name, label, 1, default_value=(default,), min=minimum, max=maximum, min_is_strict=True, help=help
        )

    def menu(name, label, items, default):
        return hou.StringParmTemplate(
            name,
            label,
            1,
            default_value=(default,),
            menu_items=tuple(token for token, _ in items),
            menu_labels=tuple(label for _, label in items),
        )

    def hide(parm, condition):
        parm.setConditional(hou.parmCondType.HideWhen, condition)
        return parm

    color = hou.FloatParmTemplate(
        "cycles_light_color", "Color", 3, default_value=(1, 1, 1), naming_scheme=hou.parmNamingScheme.RGBA
    )
    color.setTags({"colortype": "color", "color_space": "scene_linear"})
    kind = menu(
        "cycles_light_type", "Type", [("point", "Point"), ("spot", "Spot"), ("grid", "Area"), ("sun", "Sun")], "grid"
    )
    kind.setHelp(
        "One Cycles Light object supports Point, Spot, Area and Sun. Use Sun with Angle zero for an ideal distant source."
    )
    radius = hide(
        _float(
            "light_radius",
            "Radius",
            0,
            0,
            1,
            "Point/spot radius in scene units, independent of object scale. Zero gives a punctual source with no visible surface.",
        ),
        "{ cycles_light_type != point cycles_light_type != spot }",
    )
    radius.setMaxIsStrict(False)
    soft = hide(
        _toggle(
            "light_is_sphere",
            "Soft Falloff",
            True,
            "Use a spherical point/spot source; off uses the disk approximation, matching Cycles soft-falloff semantics.",
        ),
        "{ cycles_light_type != point cycles_light_type != spot }",
    )
    area_hide = "{ cycles_light_type != grid }"
    shape = hide(
        menu(
            "cycles_light_area_shape",
            "Shape",
            [("square", "Square"), ("rectangle", "Rectangle"), ("disk", "Disk"), ("ellipse", "Ellipse")],
            "square",
        ),
        area_hide,
    )
    size = hide(
        real(
            "areasizex",
            "Size",
            1,
            0.0001,
            10,
            "Full emitter width or diameter in scene units. Square and Disk use this value for both axes.",
        ),
        area_hide,
    )
    size_y = hide(
        real(
            "areasizey",
            "Size Y",
            1,
            0.0001,
            10,
            "Full emitter height or vertical diameter in scene units. Used by Rectangle and Ellipse.",
        ),
        "{ cycles_light_type != grid } { cycles_light_area_shape == square } { cycles_light_area_shape == disk }",
    )
    angle = hide(
        real(
            "vm_envangle", "Angle", 0.526, 0, 180, "Sun angular diameter in degrees. Zero is an ideal distant source."
        ),
        "{ cycles_light_type != sun }",
    )
    normalize = hou.ToggleParmTemplate(
        "normalizearea",
        "Normalize",
        default_value=True,
        help="Keep total power constant as point/spot radius, area size or Sun angle changes. Off interprets Power/Strength as radiance. Normalized Sun Strength is irradiance.",
    )
    portal = "{ cycles_light_type == grid cycles_light_is_portal == 1 }"
    is_portal = hide(_toggle("light_is_portal", "Portal", False, PORTAL_HELP), area_hide)
    basic = _section(
        "light_basic",
        "Light",
        [
            hou.ToggleParmTemplate("light_enable", "Enable", default_value=True),
            kind,
            is_portal,
            color,
            *temperature_template(),
            real(
                "light_intensity",
                "Power / Strength",
                1000,
                0,
                10000,
                "Point, Spot and Area: power in watts when Normalize is on. Sun: strength in watts per square metre.",
            ),
            real(
                "light_exposure",
                "Exposure",
                0,
                -20,
                20,
                "Light exposure in stops; multiplies power by 2 raised to Exposure.",
            ),
            normalize,
            radius,
            soft,
            shape,
            size,
            size_y,
            angle,
        ],
        expanded=True,
    )
    beam = _section(
        "light_beam",
        "Beam Shape",
        [
            hide(
                _float("light_spot_angle", "Angle", 45, 0.001, 180, "Full spot cone angle in degrees."),
                "{ cycles_light_type != spot }",
            ),
            hide(
                _float("light_spot_smooth", "Blend", 0.15, 0, 1, "Blend from the full cone to its smooth inner cone."),
                "{ cycles_light_type != spot }",
            ),
            hide(
                _float(
                    "light_spread",
                    "Spread",
                    180,
                    0,
                    180,
                    "Area-light beam angle in degrees. 180 emits over the full hemisphere.",
                ),
                area_hide,
            ),
            hide(hou.ToggleParmTemplate("singlesided", "Single Sided", default_value=True), area_hide),
            hide(_condition(hou.ToggleParmTemplate("reverse", "Reverse Direction"), "{ singlesided == 0 }"), area_hide),
        ],
        expanded=True,
    )
    hide(beam, "{ cycles_light_type != spot cycles_light_type != grid }")
    settings = _section(
        "light_sampling",
        "Settings",
        [
            _integer("light_max_bounces", "Max Bounces", 1024, 0, 1024),
            _toggle("light_cast_shadow", "Cast Shadow", True),
            _toggle("light_use_mis", "Multiple Importance Sampling", True),
            _toggle(
                "light_use_caustics",
                "Shadow Caustics",
                False,
                "Include this light in shadow-caustic evaluation. Requires a refractive object with Cast Shadow Caustics and a receiver with Receive Shadow Caustics enabled in their Cycles properties.",
            ),
        ],
    )
    visibility = _section(
        "light_visibility",
        "Ray Visibility",
        [
            _toggle("light_visibility_" + name, label, name != "camera")
            for name, label in (
                ("camera", "Camera"),
                ("diffuse", "Diffuse"),
                ("glossy", "Glossy"),
                ("transmission", "Transmission"),
                ("volume", "Volume Scatter"),
            )
        ],
    )
    groups = _section(
        "light_groups",
        "Light Groups",
        [
            hou.StringParmTemplate(
                "cycles_lightgroup",
                "Light Group",
                1,
                default_value=("",),
                help="Enable this RGB group output under Image Output / Passes / Light Groups on the Cycles ROP.",
            ),
            _toggle(
                "light_shadow_catcher",
                "Shadow Catcher Reference",
                True,
                "Include this real/reference light when calculating the Shadow Catcher pass. Off is for additional virtual lights.",
            ),
        ],
    )
    # A portal keeps only its shape, size and direction (see Light::is_portal in the engine).
    ignored = ("cycles_light_color", "cycles_light_use_temperature", "cycles_light_temperature", "light_intensity")
    ignored += ("light_exposure", "normalizearea", "cycles_light_spread", "singlesided")
    for folder in (basic, beam):
        children = []
        for parm in folder.parmTemplates():
            if parm.name() in ignored:
                _portal_hidden(parm, portal)
            elif parm.name() == "reverse":
                _condition(parm, "{ singlesided == 0 cycles_light_is_portal == 0 }")
            children.append(parm)
        folder.setParmTemplates(children)
    tabs = [basic, beam] + [
        _portal_hidden(folder, portal)
        for folder in (emission_template(), linking_template(), settings, visibility, groups)
    ]
    return _document(
        hou.FolderParmTemplate(
            "cycles_light_properties",
            "Cycles Light",
            tabs,
            folder_type=hou.folderType.Tabs,
            tags={"cycles_light_properties_version": "14"},
        )
    )
