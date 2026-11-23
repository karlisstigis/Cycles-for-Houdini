# SPDX-License-Identifier: Apache-2.0
"""Serialize binary HDA options using the installed public HOM API."""

import sys
import hou


def render_parameters(definition):
    """Author the current native ROP controls and parameter layout.

    Sampling/Denoising, Film and Performance follow Cycles' Blender panels.
    Houdini scene selection and SOP defaults retain their host-specific meaning.
    Unsupported engine controls are deliberately absent.
    """
    original = definition.parmTemplateGroup()
    remaining = {parm.name(): parm for parm in original.entries()}
    # Subdivision is an object opt-in, with scene-wide adaptive dicing policy.
    for name in (
        "subdivision_type",
        "subdivision_space",
        "subdivision_rate",
        "subdivision_level",
        "subdivision_boundary",
        "subdivision_fvar",
    ):
        remaining.pop(name, None)
    for name, label, default in [("dicing_rate", "Dicing Rate Render", 1), ("ipr_dicing_rate", "Viewport", 8)]:
        remaining[name] = hou.FloatParmTemplate(
            name,
            label,
            1,
            default_value=(default,),
            min=0.1,
            max=1000,
            min_is_strict=True,
            max_is_strict=True,
            help="Multiplier for each enabled object adaptive Pixel Size or object-space Edge Length. Does not enable subdivision on objects.",
        )
    remaining["max_subdivisions"] = hou.IntParmTemplate(
        "max_subdivisions",
        "Max Subdivisions",
        1,
        default_value=(12,),
        min=0,
        max=16,
        min_is_strict=True,
        max_is_strict=True,
        help="Maximum adaptive subdivision depth. Does not change uniform object Levels IPR/Render. The Triangle Budget remains an allocation guard.",
    )
    remaining["offscreen_dicing_scale"] = hou.FloatParmTemplate(
        "offscreen_dicing_scale",
        "Offscreen Scale",
        1,
        default_value=(4,),
        min=1,
        max=25,
        min_is_strict=True,
        help="Cycles progressively increases micropolygon size outside the dicing-camera view. Higher values use less memory; lower values improve offscreen reflections and shadows. 1 disables this reduction.",
    )
    remaining["limit_subdivision"] = hou.ToggleParmTemplate(
        "limit_subdivision",
        "Limit Subdivision Triangles",
        default_value=False,
        help="Optional Cycles for Houdini memory guard, disabled by default. Exceeding the budget reports a render error instead of silently reducing subdivision. Not a Blender subdivision setting.",
    )
    remaining["subdivision_limit"] = hou.IntParmTemplate(
        "subdivision_limit",
        "Triangle Budget",
        1,
        default_value=(100000000,),
        min=1,
        max=100000000,
        min_is_strict=True,
        disable_when="{ limit_subdivision == 0 }",
        help="Optional per-mesh allocation ceiling. Has no effect unless Limit Subdivision Triangles is enabled. Internal indexing limits always apply.",
    )
    remaining["dicing_camera"] = hou.StringParmTemplate(
        "dicing_camera",
        "Dicing Camera",
        1,
        default_value=("",),
        string_type=hou.stringParmType.NodeReference,
        tags={"opfilter": "!!OBJ/CAMERA!!", "oprelative": "."},
        help="Optional camera used only for adaptive tessellation. Empty follows the render camera. A fixed camera can avoid crawling geometry when the render camera moves.",
    )
    remaining["apply_stylesheets"] = hou.StringParmTemplate(
        "apply_stylesheets",
        "Stylesheets",
        1,
        default_value=("",),
        item_generator_script="from cycles_houdini.stylesheets import menu\nreturn menu(kwargs)",
        item_generator_script_language=hou.scriptLanguage.Python,
        menu_type=hou.menuType.StringToggle,
        help="Scene stylesheets from Houdini's Material Stylesheets editor. Choose names or enter a space-separated pattern, for example look_* ^look_debug. Empty applies no scene stylesheet. Native manager order controls priority: the last selected stylesheet has highest priority, above object and geometry rules. Uses native material assignments; material overrides selected on this ROP replace these bindings.",
    )
    remaining["use_tiling"] = hou.ToggleParmTemplate(
        "use_tiling",
        "Tiling (Final Render)",
        default_value=True,
        help="Render final disk and MPlay images in tiles to reduce render-buffer memory. Interactive viewer rendering always uses a full-frame buffer.",
    )
    remaining["tile_size"] = hou.IntParmTemplate(
        "tile_size",
        "Tile Size",
        1,
        default_value=(2048,),
        min=1,
        max=8192,
        min_is_strict=True,
        max_is_strict=False,
        disable_when="{ use_tiling == 0 }",
        help="Final-render tile width and height in pixels. Smaller tiles reduce render-buffer memory and may increase render time. Cycles can round this size for device alignment.",
    )
    remaining["auto_texture_cache"].setConditional(hou.parmCondType.DisableWhen, "{ use_texture_cache == 0 }")
    for name, label, action, help_text in (
        (
            "texture_cache_generate",
            "Generate",
            "generate_from_rop",
            "Generate Cycles cache files for the textures used by this ROP at the current frame. Existing current cache files are reused.",
        ),
        (
            "texture_cache_clear",
            "Clear",
            "clear_from_rop",
            "Clear generated Cycles cache files belonging to textures used by this ROP at the current frame. Source textures are preserved.",
        ),
    ):
        remaining[name] = hou.ButtonParmTemplate(
            name,
            label,
            script_callback=f"from cycles_houdini import texture_cache; texture_cache.{action}(kwargs)",
            script_callback_language=hou.scriptLanguage.Python,
            help=help_text,
        )

    def take(names):
        return [remaining.pop(name) for name in names.split()]

    def section(name, label, names, expanded=False, toggle=None):
        tags = {"group_default": "1"} if expanded else {}
        if toggle:
            tags["sidefx::header_toggle"] = toggle
        return hou.FolderParmTemplate(name, label, take(names), folder_type=hou.folderType.Collapsible, tags=tags)

    def tab(name, label, contents):
        return hou.FolderParmTemplate(name, label, contents, folder_type=hou.folderType.Tabs)

    remaining["ipr_denoiser"].setItemGeneratorScript(
        "from cycles_houdini.dlss import denoiser_menu\nreturn denoiser_menu(kwargs['node'])"
    )
    remaining["ipr_denoiser"].setItemGeneratorScriptLanguage(hou.scriptLanguage.Python)
    remaining["background"].setConditional(hou.parmCondType.DisableWhen, "{ world_source == material }")
    remaining["background"].setHelp(
        "Used by Constant Color, and as the fallback when Scene Environment Light selects no environment. A Cycles World Material supplies its own background color."
    )
    group = hou.ParmTemplateGroup(take("execute executebackground renderdialog"))
    device_tab = tab(
        "ch_devices",
        "Devices",
        [
            hou.StringParmTemplate(
                "device_mode",
                "Render Device",
                1,
                default_value=("gpu",),
                menu_items=("cpu", "gpu"),
                menu_labels=("CPU", "GPU Compute"),
            ),
            hou.StringParmTemplate(
                "compute_backend",
                "GPU Backend",
                1,
                default_value=("OPTIX",),
                menu_items=("CUDA", "OPTIX"),
                menu_labels=("CUDA", "OptiX"),
                disable_when="{ device_mode != gpu }",
            ),
            hou.FolderParmTemplate(
                "device_gpu_list", "GPU Devices", folder_type=hou.folderType.Simple, tags={"cycles_device_list": "1"}
            ),
            hou.ButtonParmTemplate(
                "device_refresh",
                "Refresh Devices",
                script_callback="from cycles_houdini import device_selection; device_selection.refresh(kwargs['node'])",
                script_callback_language=hou.scriptLanguage.Python,
            ),
        ],
    )
    group.append(
        tab(
            "ch_rendering",
            "Rendering",
            [
                section(
                    "ch_sampling",
                    "Final Sampling",
                    "samples adaptive_sampling noise_threshold min_samples time_limit",
                    expanded=True,
                )
            ]
            + take("use_denoise")
            + [
                section(
                    "ch_denoising",
                    "Final Denoising",
                    "denoiser denoise_use_gpu denoise_passes denoise_quality denoise_prefilter denoise_start_sample",
                    expanded=True,
                    toggle="use_denoise",
                )
            ]
            + [
                section(
                    "ch_ipr_sampling",
                    "Interactive Sampling and Denoising",
                    "ipr_samples ipr_adaptive_sampling ipr_noise_threshold ipr_min_samples ipr_use_denoise ipr_denoiser ipr_dlss_quality ipr_denoise_use_gpu ipr_denoise_passes ipr_denoise_quality ipr_denoise_prefilter ipr_denoise_start_sample",
                ),
                section(
                    "ch_ipr_settings",
                    "IPR Settings",
                    "tessellation_update refresh_tessellation ipr_refit_meshes",
                    expanded=True,
                ),
                section(
                    "ch_light_sampling", "Lights", "light_tree light_threshold resampled_light_sampling use_guiding"
                ),
                section(
                    "ch_advanced_sampling",
                    "Advanced Sampling",
                    "sampling_pattern seed animate_seed deterministic_guiding min_bounce transparent_min_bounce sample_subset subset_offset subset_length",
                ),
            ],
        )
    )
    group.append(device_tab)
    group.append(
        tab(
            "ch_light_paths",
            "Light Paths",
            [
                section(
                    "ch_max_bounces",
                    "Max Bounces",
                    "max_bounce diffuse_bounce glossy_bounce transmission_bounce volume_bounce transparent_max_bounce",
                    expanded=True,
                ),
                section("ch_clamping", "Clamping", "clamp_direct clamp_indirect"),
                section("ch_caustics", "Caustics", "filter_glossy caustics_reflective caustics_refractive"),
            ]
            + take("nested_dielectrics fast_gi")
            + [
                section(
                    "ch_fast_gi",
                    "Fast GI Approximation",
                    "fast_gi_method ao_factor ao_distance ipr_ao_bounces ao_bounces",
                    toggle="fast_gi",
                )
            ],
        )
    )
    group.append(
        tab(
            "ch_scene",
            "Scene",
            take("camera dof_mode source_mode")
            + [
                section(
                    "ch_sources", "Geometry", "soppath objectroot vobject forceobject excludeobject apply_stylesheets"
                ),
                section("ch_lights", "Lights", "lights forcelights excludelights lightpath lightstrength"),
                hou.FolderParmTemplate(
                    "ch_environment",
                    "Environment",
                    [
                        hou.StringParmTemplate(
                            "world_source",
                            "World Source",
                            1,
                            default_value=("scene",),
                            menu_items=("scene", "color", "material"),
                            menu_labels=("Scene Environment Light", "Constant Color", "Cycles World Material"),
                            help="Scene uses one enabled native Environment Light selected by the Lights patterns, or the fallback color when none is selected. Choose a World Material to combine environment textures and procedural world shaders.",
                        ),
                        hou.StringParmTemplate(
                            "world_material",
                            "World Material",
                            1,
                            default_value=("",),
                            string_type=hou.stringParmType.NodeReference,
                            disable_when="{ world_source != material }",
                            tags={"opfilter": "!!VOP!!", "oprelative": "."},
                            help="Cycles World Material builder. Native typed subnets, promotion, animation and file textures use the same graph services as surface materials.",
                        ),
                        hou.StringParmTemplate(
                            "world_volume_sampling",
                            "Volume Sampling",
                            1,
                            default_value=("equiangular",),
                            menu_items=("distance", "equiangular", "multiple_importance"),
                            menu_labels=("Distance", "Equiangular", "Multiple Importance"),
                            disable_when="{ world_source != material }",
                            help="Sampling of a World Material volume. Equiangular suits lights inside the medium and is the Blender world default; Distance suits dense media; Multiple Importance combines both at higher cost.",
                        ),
                        hou.StringParmTemplate(
                            "world_sampling",
                            "Sampling",
                            1,
                            default_value=("auto",),
                            menu_items=("auto", "manual", "none"),
                            menu_labels=("Automatic", "Manual Resolution", "None"),
                            disable_when="{ world_source == scene }",
                        ),
                        hou.IntParmTemplate(
                            "world_map_resolution",
                            "Map Resolution",
                            1,
                            default_value=(1024,),
                            min=16,
                            max=16384,
                            min_is_strict=True,
                            max_is_strict=True,
                            disable_when="{ world_sampling != manual } { world_source == scene }",
                            help="Width of the environment importance map, not the source HDRI. Higher values cost preparation time and memory.",
                        ),
                        hou.IntParmTemplate(
                            "world_max_bounces",
                            "Max Bounces",
                            1,
                            default_value=(1024,),
                            min=0,
                            max=1024,
                            min_is_strict=True,
                            max_is_strict=True,
                            disable_when="{ world_source == scene }",
                        ),
                        hou.ToggleParmTemplate(
                            "world_cast_shadow",
                            "Cast Shadow",
                            default_value=True,
                            disable_when="{ world_source == scene }",
                        ),
                        hou.ToggleParmTemplate(
                            "world_use_caustics",
                            "Shadow Caustics",
                            default_value=False,
                            disable_when="{ world_source == scene }",
                        ),
                        hou.FolderParmTemplate(
                            "world_visibility",
                            "Ray Visibility",
                            [
                                hou.ToggleParmTemplate(
                                    "world_visibility_" + name,
                                    label,
                                    default_value=True,
                                    disable_when="{ world_source == scene }",
                                )
                                for name, label in (
                                    ("camera", "Camera"),
                                    ("diffuse", "Diffuse"),
                                    ("glossy", "Glossy"),
                                    ("transmission", "Transmission"),
                                    ("volume", "Volume Scatter"),
                                )
                            ],
                            folder_type=hou.folderType.Collapsible,
                        ),
                    ]
                    + take("background")
                    + [
                        hou.StringParmTemplate(
                            "background_lightgroup",
                            "Light Group",
                            1,
                            default_value=("",),
                            help="Group for the world background contribution. Enable its RGB output under Image Output / Passes / Light Groups. Empty means ungrouped; names are case sensitive ASCII identifiers.",
                        ),
                        hou.ToggleParmTemplate(
                            "background_shadow_catcher",
                            "Shadow Catcher Reference",
                            default_value=True,
                            help="Include the world in the real/reference lighting used for Shadow Catcher division. Leave on for the photographed environment. Ordinary Beauty lighting is unchanged.",
                        ),
                    ],
                    folder_type=hou.folderType.Collapsible,
                ),
            ],
        )
    )
    group.append(
        tab(
            "ch_geometry",
            "Geometry",
            [
                section(
                    "ch_subdivision",
                    "Subdivision",
                    "dicing_rate ipr_dicing_rate offscreen_dicing_scale max_subdivisions dicing_camera limit_subdivision subdivision_limit",
                ),
                section("ch_primitive_defaults", "Points and Curves", "point_radius curve_width curve_subdivisions"),
            ],
        )
    )
    group.append(tab("ch_motion", "Motion Blur", take("allowmotionblur shutteroffset motion_samples geometry_motion")))
    group.append(
        tab(
            "ch_volumes",
            "Volumes",
            take("volume_step_size volume_biased volume_step_rate ipr_volume_step_rate volume_max_steps"),
        )
    )
    group.append(
        tab(
            "ch_film",
            "Film",
            take(
                "film_exposure transparent_background transparent_glass transparent_roughness_threshold pixel_filter filter_width pass_alpha_threshold"
            ),
        )
    )
    for name in tuple(remaining):
        if name.startswith("pass_"):
            remaining.pop(name)
    group.append(tab("ch_passes", "Passes", take("mist_start mist_depth mist_falloff")))
    group.append(
        tab(
            "ch_materials",
            "Materials",
            take("use_material_assignments materialpath material_kind material_pass_index")
            + [
                section(
                    "ch_surface_fallback",
                    "Fallback Surface",
                    "basecolor color_attribute roughness metallic emission_strength",
                ),
                section(
                    "ch_volume_fallback",
                    "Fallback Volume",
                    "density anisotropy volume_emission_strength volume_emission_color emission_field blackbody_intensity temperature_kelvin_scale",
                ),
            ],
        )
    )
    # Resolution (res) is added with the artist output controls below.
    group.append(tab("ch_output", "Output", take("trange f take picture")))
    group.append(
        tab(
            "ch_performance",
            "Performance",
            [
                section("ch_final_memory", "Final Render Memory", "persistent_data use_tiling tile_size"),
                section("ch_threads", "Threads", "render_threads"),
                section(
                    "ch_texture_cache",
                    "Texture Cache",
                    "use_texture_cache auto_texture_cache texture_cache_generate texture_cache_clear",
                ),
            ],
        )
    )
    group.append(
        tab(
            "ch_scripts",
            "Scripts",
            take(
                "tprerender prerender lprerender tpreframe preframe lpreframe "
                "tpostframe postframe lpostframe tpostrender postrender lpostrender"
            ),
        )
    )
    if any(not parm.isHidden() for parm in remaining.values()):
        raise RuntimeError("Unassigned visible render parameters: " + ", ".join(remaining))
    for parm in remaining.values():
        group.append(parm)
    # Artist output controls. These are consumed by shared native/SOHO policy.
    additions = [
        hou.IntParmTemplate(
            "resolution_percent",
            "Resolution %",
            1,
            default_value=(100,),
            min=1,
            max=400,
            min_is_strict=True,
            max_is_strict=True,
        ),
        hou.FloatParmTemplate(
            "ipr_resolution_percent",
            "IPR Resolution %",
            1,
            default_value=(0,),
            min=0,
            max=400,
            min_is_strict=True,
            max_is_strict=True,
            help="Viewer resolution in both Interactive and Final sampling modes. 0 explicitly follows Common Settings Resolution %. Otherwise an independent percentage of Width and Height. Disk and clapper renders always use Common Settings Resolution %.",
        ),
        hou.FloatParmTemplate(
            "pixel_aspect", "Pixel Aspect", 1, default_value=(1,), min=0.01, max=10, min_is_strict=True
        ),
        hou.ToggleParmTemplate("render_region", "Render Region", default_value=False),
        hou.FloatParmTemplate(
            "render_region_bounds",
            "Left / Right / Bottom / Top",
            4,
            default_value=(0, 1, 0, 1),
            min=0,
            max=1,
            min_is_strict=True,
            max_is_strict=True,
            disable_when="{ render_region == 0 }",
        ),
        hou.ToggleParmTemplate(
            "crop_to_region",
            "Crop Output to Region",
            default_value=False,
            help="Save only the region as a new image at origin. Off retains its position and full-frame display window.",
        ),
        hou.ToggleParmTemplate(
            "output_separate_passes",
            "Passes to Separate Files",
            default_value=False,
            help="Write all enabled passes to separate EXRs, leaving Beauty in Output Picture. Overrides the individual Separate File toggles without changing them. Explicit per-pass paths still apply; matching paths combine passes into a shared file.",
        ),
        hou.ToggleParmTemplate(
            "output_multipart",
            "Multipart EXR",
            default_value=False,
            disable_when="{ output_separate_passes == 1 }",
            help="Write Beauty and every enabled pass into one EXR, with independent compression and precision per part. Cryptomatte numbered layers stay together in one part. Overrides individual Separate File flags and paths without changing them. Passes to Separate Files takes precedence.",
        ),
        hou.ToggleParmTemplate(
            "output_pass_folders",
            "Pass Folders",
            default_value=False,
            help="Put Output Picture in a beauty subfolder and automatically named separate files in subfolders named after their output layers. Explicit per-pass File paths are unchanged. Does not enable Separate File for any pass.",
        ),
        hou.StringParmTemplate(
            "output_precision",
            "Precision",
            1,
            default_value=("float",),
            menu_items=("half", "float"),
            menu_labels=("16-bit Half", "32-bit Float"),
            menu_type=hou.menuType.Normal,
            help="Precision for Beauty and passes set to Use Main Output. Every pass can choose its own precision, including in a shared multilayer EXR. Half is allowed for every pass, but reduces numeric accuracy and can corrupt Cryptomatte IDs.",
        ),
        hou.StringParmTemplate(
            "output_compression",
            "EXR Compression",
            1,
            default_value=("zip",),
            menu_items=("none", "dwab", "dwaa", "piz", "rle", "zips", "zip", "pxr24", "b44", "b44a"),
            menu_labels=(
                "No Compression",
                "DWA-B (Lossy, 256 Scanlines)",
                "DWA-A (Lossy, 32 Scanlines)",
                "PIZ Wavelet",
                "Run-Length Encoding",
                "ZIP (Single Scanline)",
                "ZIP (16-Scanline Blocks)",
                "PXR24 (Lossy Float)",
                "B44 (Lossy Half)",
                "B44A (Lossy Half, Flat Fields)",
            ),
            help="Compression for the main output (Beauty when passes use separate files). Separate files use their own per-pass codec, defaulting to ZIP. A pass set to Use Main Output follows this codec and DWA level exactly. ZIP, single-scanline ZIP, PIZ, RLE and None are lossless. Every codec is allowed on every pass. Lossy codecs can alter numeric values or corrupt Cryptomatte IDs; use ZIP or PIZ when exact values are required.",
        ),
        hou.FloatParmTemplate(
            "output_dwa_level",
            "DWA Compression Level",
            1,
            default_value=(45,),
            min=0,
            max=100,
            min_is_strict=True,
            disable_when="{ output_compression != dwaa output_compression != dwab }",
            help="Higher values reduce file size at the cost of color fidelity. Applies only to DWA-A and DWA-B.",
        ),
        hou.StringParmTemplate(
            "output_existing",
            "Existing Files",
            1,
            default_value=("overwrite",),
            menu_items=("overwrite", "skip", "error"),
            menu_labels=("Overwrite", "Skip Existing Frames", "Report Error"),
            help="Skip validates complete EXR headers, image windows, channels and multipart parts. Invalid existing outputs report an error. Error and Skip never replace files created concurrently.",
        ),
        hou.ToggleParmTemplate(
            "fail_on_missing_inputs",
            "Fail on Missing Render Inputs",
            default_value=False,
            help="Final disk and farm renders fail before publishing any output when authored inputs are missing or unusable. Default off preserves diagnostic renders. Strict Skip verifies current inputs by rendering; interactive rendering always supports live repair.",
        ),
    ]
    # Resolution follows Houdini's own render nodes: one integer vector with a
    # mini menu of the standard resolutions (FBres, overridable on HOUDINI_PATH).
    additions.append(
        hou.IntParmTemplate(
            "res",
            "Resolution",
            2,
            default_value=(640, 360),
            min=1,
            max=4096,
            min_is_strict=True,
            naming_scheme=hou.parmNamingScheme.XYZW,
            help="Base image width and height in pixels. Resolution % scales both for disk renders; the viewer uses IPR Resolution % when it is set.",
        )
    )
    resolution_menu = hou.ButtonParmTemplate(
        "res_menu",
        "Choose Resolution",
        script_callback="from cycles_houdini import rop_controls; rop_controls.apply_resolution_menu(kwargs)",
        script_callback_language=hou.scriptLanguage.Python,
        help="Houdini's standard resolutions. Sets width, height and pixel aspect; resolution percentages are unchanged.",
    )
    # A one-line Python menu script is evaluated as an expression (no return).
    resolution_menu.setItemGeneratorScript("__import__('toolutils').parseDialogScriptMenu('FBres', forhscript=False)")
    resolution_menu.setItemGeneratorScriptLanguage(hou.scriptLanguage.Python)
    resolution_menu.setMenuType(hou.menuType.Mini)
    additions.append(resolution_menu)
    additions.extend(
        [
            hou.IntParmTemplate(
                "curve_segments",
                "Bezier/NURBS Segments",
                1,
                default_value=(16,),
                min=1,
                max=256,
                min_is_strict=True,
                max_is_strict=True,
                help="Only for Houdini Bezier and NURBS curves: the number of straight pieces each span becomes when the curve is converted for rendering. Polygon curves, such as hair, are not affected; Curve Subdivisions refines how every curve renders. The count is fixed per span, so deforming curves keep matching samples for motion blur. SOP geometry is unchanged.",
            ),
            hou.ToggleParmTemplate(
                "output_color_override",
                "Override Color Space",
                default_value=False,
                help="Convert color passes when writing EXR. Does not change rendering, viewer transforms, data passes, denoising guides or Cryptomatte.",
            ),
            hou.StringParmTemplate(
                "output_color_space",
                "Color Space",
                1,
                default_value=("",),
                item_generator_script="from cycles_houdini import rop_controls\nreturn rop_controls.output_color_menu()",
                item_generator_script_language=hou.scriptLanguage.Python,
                menu_type=hou.menuType.StringReplace,
                disable_when="{ output_color_override == 0 }",
                help="Destination from Houdini active OCIO configuration. RGB is converted from the rendered scene-linear space; alpha is preserved. A changed configuration requires rendering a new frame before saving.",
            ),
        ]
    )
    for parm in additions:
        group.append(parm)
    labels = {
        "picture": "Output Picture",
        "subdivision_limit": "Triangle Budget",
        "point_radius": "Point Radius",
        "curve_width": "Curve Width",
        "motion_samples": "Samples",
        "shutteroffset": "Shutter Offset",
        "allowmotionblur": "Enable",
        "film_exposure": "Exposure",
        "time_limit": "Time Limit (s)",
        "denoise_quality": "Quality",
        "denoise_prefilter": "Prefilter",
        "denoise_use_gpu": "Use GPU",
        "denoise_passes": "Input Passes",
        "use_denoise": "Denoise",
    }
    for parm in group.entriesWithoutFolders():
        if parm.name().startswith("ipr_") and parm.name() not in labels:
            labels[parm.name()] = (
                parm.label().removeprefix("IPR ").replace("OIDN ", "").replace("Denoise Beauty", "Denoise")
            )
    for name, label in labels.items():
        parm = group.find(name)
        if parm:
            parm.setLabel(label)
            group.replace(name, parm)
    for name in ("source_mode", "soppath", "lightpath", "lightstrength"):
        parm = group.find(name)
        parm.hide(True)
        group.replace(name, parm)
    # Every default with a Blender equivalent is Blender 5.3's factory default, authored once in
    # the DialogScript (docs/user/rop-defaults-and-layout.md, "Defaults follow Blender").
    for name in ("samples", "min_samples", "ipr_samples", "ipr_min_samples"):
        parm = group.find(name)
        parm.setMaxValue(4096)
        parm.setMaxIsStrict(False)
        group.replace(name, parm)
    parm = group.find("sampling_pattern")
    parm.setHelp(
        "Automatic uses blue noise, optimized for first-sample quality during interactive rendering. This is the Blender default."
    )
    group.replace(parm.name(), parm)
    parm = group.find("adaptive_sampling")
    parm.setHelp("Stop sampling converged pixels. Lower thresholds require more work.")
    group.replace(parm.name(), parm)

    for name, world in [("world_material", True), ("materialpath", False)]:
        parm = group.find(name)
        parm.setItemGeneratorScript(
            "from cycles_houdini import rop_controls\nreturn rop_controls.material_menu(world=" + str(world) + ")"
        )
        parm.setItemGeneratorScriptLanguage(hou.scriptLanguage.Python)
        parm.setMenuType(hou.menuType.StringReplace)
        group.replace(name, parm)
    precision = group.find("output_precision")
    precision.setHelp(
        precision.help()
        + " EXR color channels remain scene-linear in the active Houdini OCIO working space (for example ACEScg or linear Rec.709). Display/view transforms are preview-only. Enable Override Color Space to convert color passes on output; data passes and Cryptomatte stay raw."
    )
    group.replace(precision.name(), precision)
    from pathlib import Path
    import runpy

    # The list is the sole artist/source schema. Do not retain hidden checkbox
    # or old custom-AOV multiparms as a second, diverging set of selections.
    for parm in runpy.run_path(str(Path(__file__).with_name("pass-schema.py")))["templates"]():
        group.append(parm)
    for name, label, action, help_text in (
        (
            "ch_ipr",
            "IPR",
            "open_ipr",
            "Open the Cycles Render View in a separate window and start IPR. Focus the existing window if already open.",
        ),
        (
            "ch_render_mplay",
            "Render to MPlay",
            "render_mplay",
            "Render this Cycles ROP and upstream Cycles ROPs to MPlay, and save each ROP's Output Picture. Other dependency types retain their own render behavior.",
        ),
    ):
        group.append(
            hou.ButtonParmTemplate(
                name,
                label,
                script_callback=f"from cycles_houdini.rop import render_actions; render_actions.{action}(kwargs['node'])",
                script_callback_language=hou.scriptLanguage.Python,
                help=help_text,
            )
        )
    layout = runpy.run_path(str(Path(__file__).with_name("rop-layout.py")))["apply"](group)
    from collections import Counter

    def leaves(entries):
        for p in entries:
            if isinstance(p, hou.FolderParmTemplate):
                if p.folderType() == hou.folderType.MultiparmBlock:
                    yield p.name()
                yield from leaves(p.parmTemplates())
            elif p.type() not in (hou.parmTemplateType.Label, hou.parmTemplateType.Separator):
                yield p.name()

    if Counter(leaves(group.entries())) != Counter(leaves(layout.entries())):
        before = Counter(leaves(group.entries()))
        after = Counter(leaves(layout.entries()))
        raise RuntimeError(
            "ROP layout changed the current parameter inventory: missing="
            + repr(before - after)
            + " extra="
            + repr(after - before)
        )
    # SOHO's reserved executebackground action does not dispatch this asset's
    # Python callback. Use a native authored button, as other custom ROP actions
    # do, and leave the inherited action out of the artist parameter layout.
    background = hou.ButtonParmTemplate(
        "ch_render_background",
        "Render to Disk in Background",
        script_callback="from cycles_houdini import background_render; background_render.launch(kwargs['node'])",
        script_callback_language=hou.scriptLanguage.Python,
        help="Snapshot this scene and render its final frame range in a separate Houdini batch process. The current scene remains editable. External assets are read by the batch process. A private job folder retains the scene snapshot, color configuration and logs; rendered images use Output Picture. Inspect the job folder using Background Renders.",
    )
    background.hide(True)
    layout.append(background)
    cancel = hou.ButtonParmTemplate(
        "ch_cancel_render",
        "Cancel Render",
        script_callback="from cycles_houdini import rop_controls; rop_controls.cancel_render(kwargs['node'])",
        script_callback_language=hou.scriptLanguage.Python,
        help="Cancel this ROP's current foreground render or its most recently launched background job. Completed frames are preserved. Use Background Renders to stop other jobs.",
    )
    cancel.hide(True)
    layout.append(cancel)
    jobs = hou.ButtonParmTemplate(
        "ch_background_jobs",
        "Background Renders",
        script_callback="from cycles_houdini import background_render; background_render.show_jobs()",
        script_callback_language=hou.scriptLanguage.Python,
        help="Inspect background progress and diagnostics, stop a job, or open its report folder.",
    )
    jobs.hide(True)
    layout.append(jobs)
    native_controls = layout.find("renderdialog")
    native_controls.hide(True)
    layout.replace("renderdialog", native_controls)
    controls = hou.ButtonParmTemplate(
        "ch_controls",
        "Controls…",
        script_callback="from cycles_houdini import rop_controls; rop_controls.show_controls(kwargs['node'])",
        script_callback_language=hou.scriptLanguage.Python,
        help="Render settings, background rendering and live background job cancellation.",
    )
    layout.insertAfter("ch_render_mplay", controls)
    layout.append(
        hou.ToggleParmTemplate(
            "viewer_interactive",
            "Interactive Viewer",
            default_value=True,
            is_hidden=True,
            help="Viewer quality selection: Interactive Render when enabled, Final Render when disabled. Disk rendering always uses Final Render.",
        )
    )
    # Dynamic menus are saved with the HIP and must remain harmless when the
    # whole integration is absent, including its top-level startup module.
    offline_scripts = runpy.run_path(str(Path(__file__).with_name("offline-scripts.py")))
    help_tools = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "houdini/python3.13libs/cycles_houdini/rop/rop_help.py")
    )
    # Tabs must be contiguous to belong to the same Houdini folder set.
    # Appending after hidden internal leaf parameters creates a separate set
    # whose sole About tab is not shown beside the artist-facing tabs.
    layout.insertAfter(layout.findIndicesForFolder(("Scripts",)), help_tools["about_templates"]())
    layout = help_tools["apply_tooltips"](layout)
    definition.setParmTemplateGroup(offline_scripts["guard_menus"](layout))
    # Embedded wiki help follows the definition, including F1 on individual
    # controls. Internal IDs keep repeated labels (Quality, Enable) unambiguous.
    definition.addSection("Help", help_tools["help_page"](layout))


for filename in sys.argv[1:]:
    hou.hda.installFile(filename)
    definitions = hou.hda.definitionsInFile(filename)
    if len(definitions) != 1:
        raise RuntimeError("Expected exactly one authored asset definition")
    definitions[0].setExtraFileOption("OnCreated/IsPython", True)
    definitions[0].setExtraFileOption("OnLoaded/IsPython", True)
    render_parameters(definitions[0])
