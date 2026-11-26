# SPDX-License-Identifier: Apache-2.0
"""Native OBJ render properties. Installation never cooks or edits SOP geometry."""

import math
import hou

FOLDER = "cycles_object"


def _condition(parm, expression):
    parm.setConditional(hou.parmCondType.DisableWhen, expression)
    return parm


def _toggle(name, label, default=False, help=""):
    return hou.ToggleParmTemplate("cycles_" + name, label, default_value=default, help=help)


def _menu(name, label, items, default, help=""):
    return hou.StringParmTemplate(
        "cycles_" + name,
        label,
        1,
        default_value=(default,),
        menu_items=tuple(value for value, _ in items),
        menu_labels=tuple(label for _, label in items),
        help=help,
    )


def _integer(name, label, default, minimum, maximum, help=""):
    return hou.IntParmTemplate(
        "cycles_" + name,
        label,
        1,
        default_value=(default,),
        min=minimum,
        max=maximum,
        min_is_strict=True,
        max_is_strict=True,
        help=help,
    )


def _float(name, label, default, minimum, maximum, help=""):
    return hou.FloatParmTemplate(
        "cycles_" + name, label, 1, default_value=(default,), min=minimum, max=maximum, min_is_strict=True, help=help
    )


def _hide(parm, expression):
    parm.setConditional(hou.parmCondType.HideWhen, expression)
    return parm


# ROP and object "Deformation" choices. The tokens are the saved values: "transform" and
# "deformation" keep the names of the former Transform Only and Deformation choices.
DEFORMATION_CHOICES = [
    ("transform", "Off"),
    ("velocity", "Velocity"),
    ("acceleration", "Acceleration"),
    ("deformation", "Geometry Samples"),
    ("velocity_if_available", "Velocity if Available"),
]
DEFORMATION_HELP = (
    "Off: no deformation blur. "
    "Velocity: moves points by v (units per second) and turns packed primitives and instances by w "
    "(radians per second) of this frame; nothing else is cooked. "
    "Acceleration: like Velocity, plus accel (units per second squared) for curved paths, using all Samples. "
    "Geometry Samples: cooks the geometry at each shutter sample. "
    "Velocity if Available: Velocity for objects whose points have v (or w), Geometry Samples for the rest."
)


def _section(name, label, parameters, toggle=None, expanded=False):
    tags = {"group_default": "1"} if expanded else {}
    if toggle:
        tags["sidefx::header_toggle"] = "cycles_" + toggle
    return hou.FolderParmTemplate(
        "cycles_" + name, label, parameters, folder_type=hou.folderType.Collapsible, tags=tags
    )


def template(*, point_instancing=True):
    from .linking import parameter_template as linking_template

    """Opt-in properties for every rendering component inside one SOP object."""
    subdivision = [
        _toggle(
            "subdivision_ipr",
            "Enable in IPR",
            True,
            "Subdivide this object in interactive renders. Turn off to render its original polygons in IPR; disk renders still use the Render settings. Applies to uniform and adaptive subdivision, including packed instances.",
        ),
        _menu(
            "subdivision_type",
            "Type",
            [("catmull_clark", "Catmull-Clark"), ("linear", "Simple")],
            "catmull_clark",
            "Catmull-Clark smooths the surface. Simple divides the faces and preserves their shape before displacement.",
        ),
        _toggle(
            "subdivision_adaptive",
            "Adaptive Subdivision",
            help="Vary tessellation with the selected pixel or object-space edge length. Turn this off to use Levels IPR and Levels Render.",
        ),
        _toggle(
            "subdivision_cache_topology",
            "Cache Subdivision Topology",
            True,
            help="Reuse OpenSubdiv topology for adaptive Catmull-Clark subdivision during IPR. Tessellation and rendered detail still update normally. The ROP IPR Topology Cache Limit caps retained host memory.",
        ),
        _integer(
            "subdivision_ipr_level",
            "Levels IPR",
            1,
            0,
            12,
            "Uniform sampling of the Cycles limit surface. Zero disables subdivision during IPR.",
        ),
        _integer(
            "subdivision_render_level",
            "Levels Render",
            2,
            0,
            12,
            "Uniform sampling of the Cycles limit surface. Zero renders the original polygons.",
        ),
        _menu(
            "subdivision_space",
            "Adaptive Space",
            [("pixel", "Pixel"), ("object", "Object")],
            "pixel",
            "Pixel measures projected size through the dicing camera. Object measures edge length in SOP units.",
        ),
        _float(
            "subdivision_pixel_size",
            "Pixel Size",
            1,
            0.1,
            10,
            "Object adaptive pixel size, multiplied by the Render or Viewport dicing rate on the Cycles ROP.",
        ),
        _float(
            "subdivision_edge_length",
            "Edge Length",
            0.01,
            0.0001,
            1,
            "Target edge length in SOP object units, shared by packed instances with compatible rendering state.",
        ),
        _menu(
            "subdivision_fvar",
            "UV Smooth",
            [
                ("none", "Smooth All"),
                ("corners_only", "Keep Corners"),
                ("corners_plus1", "Keep Corners + Junctions"),
                ("corners_plus2", "Keep Corners + Junctions + Concave"),
                ("boundaries", "Keep Boundaries"),
                ("all", "Keep All / Linear"),
            ],
            "boundaries",
            "Choose which UV corners and boundaries stay fixed when interpolating UVs across subdivided faces.",
        ),
        _menu(
            "subdivision_boundary",
            "Boundary Smooth",
            [("edge_only", "Smooth Corners"), ("edge_and_corner", "Keep Corners"), ("none", "None (Closed Mesh Only)")],
            "edge_only",
            "Choose how open border edges and corners move as the mesh subdivides.",
        ),
        _toggle(
            "subdivision_use_creases",
            "Use Creases",
            True,
            "Use Houdini vertex/primitive creaseweight and point cornerweight. Sharpness is clamped to OpenSubdiv 10.",
        ),
    ]
    for parm in subdivision:
        disable = "{ cycles_subdivision_override == 0 } { cycles_subdivision_type == none }"
        if parm.name().endswith(("_ipr_level", "_render_level")):
            _hide(parm, "{ cycles_subdivision_adaptive == 1 }")
        elif parm.name().endswith("_space"):
            disable += " { cycles_subdivision_adaptive == 0 }"
        elif parm.name().endswith("_pixel_size"):
            disable += " { cycles_subdivision_adaptive == 0 } { cycles_subdivision_space != pixel }"
        elif parm.name().endswith("_edge_length"):
            disable += " { cycles_subdivision_adaptive == 0 } { cycles_subdivision_space != object }"
        elif parm.name() == "cycles_subdivision_cache_topology":
            disable = "{ cycles_subdivision_override == 0 } { cycles_subdivision_adaptive == 0 }"
        if parm.name() == "cycles_subdivision_ipr_level":
            disable += " { cycles_subdivision_ipr == 0 }"
        # Type must remain editable when it currently says Disabled.
        _condition(
            parm,
            "{ cycles_subdivision_override == 0 }"
            if parm.name() in ("cycles_subdivision_type", "cycles_subdivision_ipr")
            else disable,
        )
    ray_help = {
        "camera": "Show this object to primary camera rays.",
        "diffuse": "Allow rays scattered by diffuse surfaces to intersect this object.",
        "glossy": "Include the object in glossy and mirror reflections.",
        "transmission": "Allow rays transmitted through a surface to intersect this object.",
        "volume": "Allow rays scattered inside a volume to intersect this object.",
        "shadow": "Allow the object to block shadow rays between shading points and lights.",
        "raycast": "Allow the Raycast shader node to hit this object independently of its camera and lighting visibility.",
    }
    ray_visibility = [
        _condition(_toggle("visibility_" + name, label, True, ray_help[name]), "{ cycles_visibility_override == 0 }")
        for name, label in [
            ("camera", "Camera"),
            ("diffuse", "Diffuse"),
            ("glossy", "Glossy"),
            ("transmission", "Transmission"),
            ("volume", "Volume Scatter"),
            ("shadow", "Shadow"),
            ("raycast", "Raycast"),
        ]
    ]
    motion = [
        _menu(
            "motion_blur",
            "Motion Blur",
            [("inherit", "Use ROP"), ("off", "Disabled")],
            "inherit",
            "Disabled removes transform, packed-transform, deformation and velocity shutter sampling for this object.",
        ),
        _hide(
            _menu(
                "geometry_motion",
                "Deformation",
                [("inherit", "From ROP")] + DEFORMATION_CHOICES,
                "inherit",
                "How the shape itself blurs (camera and object transform blur always apply). " + DEFORMATION_HELP,
            ),
            "{ cycles_motion_blur == off }",
        ),
    ]
    primitive = {}
    for name, label, default in [
        ("point_radius", "Point Radius", 0.05),
        ("curve_width", "Curve Width", 0.001),
        ("volume_step_size", "Volume Step Size", 0),
    ]:
        primitive[name] = [
            _toggle(
                name + "_override",
                "Override " + label,
                help="Use the object value for " + label + " in place of the Cycles ROP setting.",
            ),
            _condition(
                _float(
                    name,
                    label,
                    default,
                    0,
                    1,
                    "Fallback when geometry has no explicit size attribute."
                    if name != "volume_step_size"
                    else "Object-space ray-march step. Zero derives it from grid resolution; the biased/velocity volume path uses this.",
                ),
                "{ cycles_" + name + "_override == 0 }",
            ),
        ]
        primitive[name][-1].setMaxIsStrict(False)
    shading = [
        hou.StringParmTemplate(
            "cycles_lightgroup",
            "Light Group",
            1,
            default_value=("",),
            help="Emission from this object and its packed children belongs to this group. Enable its RGB output under Image Output / Passes / Light Groups on the Cycles ROP. Empty means ungrouped; unregistered groups still illuminate Beauty. Names are case sensitive ASCII identifiers.",
        ),
        _integer(
            "pass_index",
            "Pass Index",
            0,
            0,
            32767,
            "Explicit Object Index for surface hits. All mesh, curve, point and packed components inherit it. Enable Object Index under Passes on the Cycles ROP. This pass is unfiltered; use Cryptomatte for anti-aliased masks.",
        ),
        hou.IntParmTemplate(
            "cycles_dielectric_priority",
            "Dielectric Priority",
            1,
            default_value=(0,),
            min=0,
            max=10,
            min_is_strict=False,
            max_is_strict=False,
            help="Priority for Nested Dielectrics (Cycles ROP, Light Paths). Where refractive objects overlap, the higher priority defines the medium; equal priorities form ordinary interfaces; negative values are allowed.\nModel containers and contents as closed meshes that overlap slightly; do not place faces exactly on top of each other.\nSupported: Glass BSDF, Refraction BSDF and Principled BSDF with transmission. A hand-built Glossy + Refraction mix (Fresnel or Layer Weight) is not nested-aware; use Glass or Principled.\nA textured IOR is taken where the ray enters. The Principled coat stays relative to air.\nPacked primitives can override it with a cycles_dielectric_priority attribute (Copy to Points with Pack and Instance: list it on the Attributes tab). With s@instance point instancing, instances take the priority of the Instance object, and a point attribute cycles_dielectric_priority overrides it.",
        ),
        _float(
            "ao_distance",
            "Fast GI AO Distance",
            0,
            0,
            10000,
            "Zero uses the ROP distance. Applies only when Fast GI is enabled.",
        ),
        _section(
            "mask",
            "Mask",
            [
                _toggle(
                    "shadow_catcher",
                    "Shadow Catcher",
                    help="Turn mesh, curve and sphere-point surfaces into shadow catchers; packed children inherit it. Volumes retain their ordinary rendering. Enable Shadow Catcher under Passes for an RGB shadow/reflection multiplier and foreground RGBA, or leave it off to approximate the shadow in Beauty.",
                ),
                _toggle(
                    "holdout",
                    "Holdout",
                    help="Mask this object in transparent-film output. Applies to all of its rendered components and packed children.",
                ),
            ],
            expanded=True,
        ),
        _section(
            "shadow_terminator",
            "Shadow Terminator",
            [
                _float(
                    "shadow_terminator_geometry",
                    "Geometry Offset",
                    0.1,
                    0,
                    1,
                    "Offset shadow rays near grazing angles to reduce faceted self-shadowing on smooth surfaces.",
                ),
                _float(
                    "shadow_terminator_shading",
                    "Shading Offset",
                    0,
                    0,
                    1,
                    "Shift the light-to-shadow transition on smooth shaded surfaces. Larger offsets can change the apparent shape of shadows.",
                ),
            ],
            expanded=True,
        ),
        _section(
            "caustics",
            "Caustics",
            [
                _toggle(
                    "caustics_caster",
                    "Cast Shadow Caustics",
                    help="Requires a refractive material, a receiver object and a light with Shadow Caustics enabled.",
                ),
                _toggle(
                    "caustics_receiver",
                    "Receive Shadow Caustics",
                    help="Requires a caster and a light with Shadow Caustics enabled.",
                ),
            ],
            expanded=True,
        ),
    ]
    return hou.FolderParmTemplate(
        FOLDER,
        "Cycles",
        [
            _section(
                "meshes",
                "Meshes",
                [
                    _toggle(
                        "render_mesh", "Render Meshes", True, "Include polygon surfaces from this object in the render."
                    ),
                    _toggle(
                        "subdivision_override",
                        "Subdivision",
                        help="Enable render-time subdivision on this object and its packed meshes. The ROP sets dicing multipliers and limits; it never enables subdivision on other objects. SOP geometry is unchanged.",
                    ),
                    _section("subdivision", "Subdivision", subdivision, "subdivision_override", True),
                ],
                expanded=True,
            ),
            _section(
                "instancing",
                "Instancing",
                [
                    _toggle(
                        "render_packed",
                        "Render Packed Instances",
                        True,
                        "Render packed primitives on this object. Point instances created by the instance attribute are controlled separately and continue to render. Packed children inherit the mesh, curve, point, volume and shading settings of this object.",
                    ),
                    _toggle(
                        "blender_subdivision",
                        "Subdivide Blender Meshes in Cycles",
                        True,
                        "Import an eligible final Subdivision Surface modifier as a control cage and subdivide at the Blender render level. Turn this off to use Blender's evaluated mesh and normals.",
                    ),
                ]
                + (
                    [
                        _menu(
                            "subnet_filter",
                            "Subnetwork Filtering",
                            [
                                ("all", "All Objects"),
                                ("display", "Displayed Objects"),
                                ("pattern", "Specify by a pattern"),
                            ],
                            "all",
                            "Select members of subnet prototypes referenced by the instance string point attribute. All Objects and Specify by a pattern allow the source subnet to be hidden in the viewport. Displayed Objects respects the source and parent display flags. This does not change whether the original objects render independently; use the ROP Exclude objects control for that.",
                        ),
                        _condition(
                            hou.StringParmTemplate(
                                "cycles_subnet_pattern",
                                "Subnetwork Pattern",
                                1,
                                default_value=("*",),
                                help="Object pattern relative to each referenced subnet, using Houdini object patterns. Used only with Specify by a pattern; source display flags do not filter the matches.",
                            ),
                            "{ cycles_subnet_filter != pattern }",
                        ),
                    ]
                    if point_instancing
                    else []
                ),
            ),
            _section(
                "curves",
                "Curves",
                [
                    _toggle(
                        "render_curves",
                        "Render Curves",
                        True,
                        "Include supported open curves as renderable strands. Use Shape and Curve Width to control their appearance.",
                    ),
                    _menu(
                        "curve_shape",
                        "Shape",
                        [("linear", "Linear 3D Curves"), ("thick", "3D Curves"), ("ribbon", "Rounded Ribbons")],
                        "ribbon",
                        "Rounded Ribbons (the Blender default curve shape) and 3D Curves use Cycles cardinal interpolation of the extracted SOP control keys; Linear follows the SOP segments exactly. None of them modify SOP geometry.",
                    ),
                    _toggle(
                        "curve_segments_override",
                        "Override Segments",
                        help="Use this object’s own Bezier/NURBS Segments instead of the ROP’s.",
                    ),
                    _condition(
                        _integer(
                            "curve_segments",
                            "Bezier/NURBS Segments",
                            16,
                            1,
                            256,
                            "Only for Houdini Bezier and NURBS curves: the number of straight pieces each span becomes when the curve is converted for rendering. Polygon curves, such as hair, are not affected; Curve Subdivisions refines how every curve renders. The count is fixed per span, so deforming curves keep matching samples for motion blur. SOP geometry is unchanged.",
                        ),
                        "{ cycles_curve_segments_override == 0 }",
                    ),
                    *primitive["curve_width"],
                ],
            ),
            _section(
                "points",
                "Points",
                [
                    _toggle(
                        "render_points",
                        "Render Points",
                        True,
                        "Render loose points and particle primitives using the selected point render mode.",
                    ),
                    _menu(
                        "point_render_mode",
                        "Render Mode",
                        [("auto", "Auto"), ("spheres", "Spheres"), ("gaussian_splats", "Gaussian Splats")],
                        "auto",
                        "Auto detects original 3D Gaussian splat coefficients or prepared Bake GSplats attributes. Spheres uses pscale as radius. Gaussian Splats requires valid opacity, anisotropic scale, orientation and radiance attributes. Packed children inherit this mode; point instances use their prototype object mode.",
                    ),
                    *primitive["point_radius"],
                ],
            ),
            _section(
                "volumes",
                "Volumes",
                [
                    _toggle(
                        "render_volume",
                        "Render VDB Volumes",
                        True,
                        "Include volume grids from this object when building the Cycles scene.",
                    ),
                    _toggle(
                        "volume_object_space",
                        "Density in Object Space",
                        help="Off measures absorption/scattering per world-space distance. On makes optical depth follow the object scale.",
                    ),
                    *primitive["volume_step_size"],
                ],
            ),
            _section("shading", "Shading", shading),
            linking_template(emission=True),
            _section(
                "cryptomatte",
                "Cryptomatte",
                [
                    hou.StringParmTemplate(
                        "cycles_cryptomatte_object",
                        "Object Name",
                        1,
                        default_value=("",),
                        help="Empty uses this OBJ path. All mesh, curve, point and volume components share this matte. Packed children append their stable name/id. Does not change geometry or materials.",
                    ),
                    hou.StringParmTemplate(
                        "cycles_cryptomatte_asset",
                        "Asset Name",
                        1,
                        default_value=("",),
                        help="Empty uses this OBJ path. A common name groups several OBJs and their packed children in one asset matte. Enable the required Cryptomatte outputs on the Cycles ROP.",
                    ),
                ],
            ),
            _toggle(
                "visibility_override",
                "Override Ray Visibility",
                help="These switches restrict Houdini render visibility and Phantom; they cannot make a host-hidden ray visible.",
            ),
            _section("visibility", "Ray Visibility", ray_visibility, "visibility_override"),
            _section("motion", "Motion Blur", motion),
        ],
        folder_type=hou.folderType.Tabs,
        tags={"cycles_properties_version": "23"},
    )


def properties_folder(group, kind="objects"):
    """Locate our tab by its tag; Houdini renames folders in native tab sets."""
    tag = "cycles_properties_version" if kind == "objects" else "cycles_light_properties_version"
    return next(
        (entry for entry in group.entries() if isinstance(entry, hou.FolderParmTemplate) and tag in entry.tags()), None
    )


def install_tabs(group, cycles_template):
    """Add one peer tab to the host's existing tab set."""
    owned_tags = {"cycles_properties_version", "cycles_light_properties_version"} & cycles_template.tags().keys()
    for index, entry in reversed(list(enumerate(group.entries()))):
        if entry.name() == cycles_template.name() or owned_tags & entry.tags().keys():
            group.remove((index,))
    # Canonical native linking controls stay in their existing host location.
    # When the host has not exposed them, our optional tab provides them.
    incoming = hou.ParmTemplateGroup([cycles_template])
    for name in ("lightmask", "lightcategories", "shadowcategories", "categories"):
        if group.find(name) is not None and incoming.find(name) is not None:
            incoming.remove(incoming.findIndices(name))
    cycles_template = incoming.entries()[0]
    entries = group.entries()
    # Hidden native parameters may follow the standard tab folders. Appending
    # after those would create another tab set, so insert inside the first set.
    last = None
    for index, entry in enumerate(entries):
        tab = isinstance(entry, hou.FolderParmTemplate) and entry.folderType() in (
            hou.folderType.Tabs,
            hou.folderType.RadioButtons,
        )
        if tab:
            last = index
        elif last is not None:
            break
    if last is not None:
        group.insertAfter((last,), cycles_template)
    else:
        group.append(cycles_template)
    return group


def remove_tabs(group):
    for index, entry in reversed(list(enumerate(group.entries()))):
        if (
            isinstance(entry, hou.FolderParmTemplate)
            and {"cycles_properties_version", "cycles_light_properties_version"} & entry.tags().keys()
        ):
            group.remove((index,))
    return group


def install(node):
    if node.type().category() != hou.objNodeTypeCategory() or node.childTypeCategory() != hou.sopNodeTypeCategory():
        return False
    from .instancing import is_instancer

    point_instancing = not is_instancer(node)
    group = node.parmTemplateGroup()
    folder = properties_folder(group)
    if folder and group.find("cycles_subdivision_override"):
        if (
            folder.tags().get("cycles_properties_version") == "23"
            and group.find("cycles_point_render_mode")
            and group.find("cycles_blender_subdivision")
            and group.find("cycles_subdivision_ipr")
            and group.find("cycles_subdivision_cache_topology")
            and (not point_instancing or group.find("cycles_subnet_filter"))
        ):
            return False
    elif any(parm.name().startswith("cycles_") for parm in group.entriesWithoutFolders()):
        raise ValueError("Cannot install Cycles object properties over existing conflicting parameters: " + node.path())
    group = install_tabs(group, template(point_instancing=point_instancing))
    node.setParmTemplateGroup(group)
    return True


def _value(node, name, now, default):
    parm = node.parm("cycles_" + name)
    return parm.evalAtTime(now) if parm else default


def subdivision_settings(node, now, inherited, *, interactive=True):
    if not _value(node, "subdivision_override", now, False):
        return dict(type="none")
    kind = _value(node, "subdivision_type", now, "catmull_clark")
    if interactive and not _value(node, "subdivision_ipr", now, True):
        kind = "none"
    adaptive = bool(_value(node, "subdivision_adaptive", now, False))
    level = (
        inherited["max_level"]
        if adaptive
        else _value(
            node, "subdivision_" + ("ipr" if interactive else "render") + "_level", now, 1 if interactive else 2
        )
    )
    space = _value(node, "subdivision_space", now, "pixel") if adaptive else "object"
    rate = 1
    if adaptive:
        rate = (
            _value(node, "subdivision_edge_length", now, 0.01)
            if space == "object"
            else _value(node, "subdivision_pixel_size", now, 1)
        ) * inherited["dicing_rate"]
        if space == "pixel":
            rate = max(0.1, rate)
    elif level == 0:
        kind = "none"
    return dict(
        type=kind,
        adaptive=adaptive,
        cache_topology=interactive
        and adaptive
        and kind == "catmull_clark"
        and bool(_value(node, "subdivision_cache_topology", now, True)),
        space=space,
        max_level=level,
        dicing_rate=rate,
        boundary=_value(node, "subdivision_boundary", now, "edge_only"),
        fvar=_value(node, "subdivision_fvar", now, "boundaries"),
        triangle_limit=inherited["triangle_limit"],
        use_creases=bool(_value(node, "subdivision_use_creases", now, True)),
    )


def override(node, name, now, inherited):
    result = _value(node, name, now, inherited) if _value(node, name + "_override", now, False) else inherited
    if not math.isfinite(result) or result < 0:
        raise ValueError("Cycles " + name + " must be finite and nonnegative: " + node.path())
    return result


def motion_enabled(node, now):
    choice = _value(node, "motion_blur", now, "inherit")
    if choice not in ("inherit", "off"):
        raise ValueError("Invalid Cycles object motion blur: " + node.path())
    return choice == "inherit"


def motion_mode(node, now, inherited):
    choice = _value(node, "geometry_motion", now, "inherit")
    return inherited if choice == "inherit" else choice


def component_settings(node, now):
    return dict(
        curve_shape=_value(node, "curve_shape", now, "ribbon"),
        point_render_mode=point_render_mode(node, now),
        render_types={
            kind: bool(_value(node, "render_" + kind, now, True))
            for kind in ("mesh", "points", "curves", "volume", "packed")
        },
        volume_object_space=bool(_value(node, "volume_object_space", now, False)),
        object_properties={
            name: bool(_value(node, name, now, False))
            for name in ("holdout", "caustics_caster", "caustics_receiver", "shadow_catcher")
        }
        | {
            name: float(_value(node, name, now, default))
            for name, default in [
                ("shadow_terminator_shading", 0),
                ("shadow_terminator_geometry", 0.1),
                ("ao_distance", 0),
            ]
        }
        | {
            "lightgroup": _value(node, "lightgroup", now, ""),
            "pass_index": _value(node, "pass_index", now, 0),
            "dielectric_priority": int(_value(node, "dielectric_priority", now, 0)),
            "linking_id": node.path(),
            "cryptomatte_object": _value(node, "cryptomatte_object", now, "") or node.path(),
            "cryptomatte_asset": _value(node, "cryptomatte_asset", now, "") or node.path(),
        },
    )


def point_render_mode(node, now):
    choice = _value(node, "point_render_mode", now, "auto")
    if choice not in ("auto", "spheres", "gaussian_splats"):
        raise ValueError("Invalid Cycles object point render mode: " + node.path())
    return choice


_scene_refresh_installed = False


def refresh_owned():
    root = hou.node("/obj")
    if root is None:
        return
    for node in root.recursiveGlob("*", filter=hou.nodeTypeFilter.Obj):
        if node.parm("cycles_subdivision_override") is not None:
            try:
                install(node)
            except (hou.Error, ValueError):
                pass  # Protected/foreign parameter layouts stay authored.
        elif node.parm("cycles_dof") is not None and any(
            folder.tags().get("cycles_camera_properties_version") for folder in node.parmTemplateGroup().entries()
        ):
            from . import camera_properties

            try:
                camera_properties.install(node)
            except (hou.Error, ValueError):
                pass


def install_scene_refresh():
    global _scene_refresh_installed
    if not _scene_refresh_installed:

        def after_load(event_type):
            if event_type == hou.hipFileEventType.AfterLoad:
                refresh_owned()

        hou.hipFile.addEventCallback(after_load)
        _scene_refresh_installed = True
    if not hou.hipFile.isLoadingHipFile():
        refresh_owned()
