# SPDX-License-Identifier: Apache-2.0
"""ROP help authoring and text-only About links; no renderer initialization."""

from pathlib import Path
import os

ATTRIBUTION = (
    "Cycles for Houdini integrates the Blender Cycles renderer. "
    "Optional DLSS Ray Reconstruction uses NVIDIA DLSS / NGX technology. "
    "Availability depends on the installed build and selected hardware. "
    "NVIDIA, DLSS and NGX are trademarks of NVIDIA Corporation."
)
CYCLES_URL = "https://www.cycles-renderer.org/"
NVIDIA_LICENSE_URL = "https://github.com/NVIDIA/DLSS/blob/v310.9.16/LICENSE.txt"

# These fill gaps in the inherited native ROP templates. Existing, more detailed
# integration help remains the source of truth, including candidate-specific help.
TOOLTIPS = {
    "execute": "Render the final frame range to Output Picture with Final Render sampling and denoising settings.",
    "renderdialog": "Open Houdini render controls for this ROP, including frame range and render scheduling.",
    "trange": "Choose the current frame or the explicit Start/End/Inc frame range for disk rendering.",
    "f": "First frame, last frame and frame increment for the selected Valid Frame Range mode.",
    "take": "Houdini take used for the render. The take can override scene and render parameters.",
    "tprerender": "Enable the pre-render script for this render operation.",
    "prerender": "Run once before the first frame, using the selected language.",
    "lprerender": "Language used to execute the pre-render script.",
    "tpreframe": "Enable the pre-frame script for each frame.",
    "preframe": "Run before each frame is rendered, using that frame's context.",
    "lpreframe": "Language used to execute the pre-frame script.",
    "tpostframe": "Enable the post-frame script for each frame.",
    "postframe": "Run after each frame is rendered, using that frame's context.",
    "lpostframe": "Language used to execute the post-frame script.",
    "tpostrender": "Enable the post-render script for this render operation.",
    "postrender": "Run once after the render operation, using the selected language.",
    "lpostrender": "Language used to execute the post-render script.",
    "picture": "Destination image. The extension selects the format: .exr (scene-linear, all passes), .png or .jpg/.jpeg (8-bit, the viewer's default display/view) or .tif/.tiff (32-bit float, scene-linear). PNG, JPEG and TIFF hold only the beauty image; passes must use separate EXR files. Use Houdini frame expressions such as $F4 for a sequence.",
    "camera": "Houdini camera object used for projection, framing, clipping and camera effects.",
    "resolution_percent": "Scale both base image dimensions for disk rendering. Viewer resolution follows IPR Resolution % when it is nonzero.",
    "pixel_aspect": "Width-to-height ratio of one pixel. 1 means square pixels; this also affects the camera framing.",
    "render_region": "Trace only the normalized rectangular region specified below.",
    "render_region_bounds": "Normalized left, right, bottom and top image edges, from 0 to 1. Left must be below right and bottom below top.",
    "samples": "Maximum path-tracing samples per pixel for Final Render. Adaptive Sampling can stop converged pixels earlier.",
    "seed": "Base random seed for path tracing. Change it to obtain a different noise realization without changing the intended image.",
    "denoise_quality": "OpenImageDenoise quality and speed tradeoff. High spends more work on reconstruction; Fast favors responsiveness.",
    "denoise_passes": "Guides supplied to the conventional denoiser: color alone, color with albedo, or color with albedo and normal.",
    "subset_offset": "First sample index of the final-render subset. Use non-overlapping ranges when externally combining sample subsets.",
    "subset_length": "Number of samples in the final-render subset, within the configured final sample budget.",
    "min_bounce": "Minimum light-path depth before probabilistic path termination. Higher values can increase render time.",
    "transparent_min_bounce": "Minimum transparent-surface depth before probabilistic path termination.",
    "max_bounce": "Maximum total light-path bounces. Individual diffuse, glossy, transmission and volume limits also apply.",
    "diffuse_bounce": "Maximum diffuse reflection bounces. Lower limits reduce indirect diffuse light and rendering cost.",
    "glossy_bounce": "Maximum glossy or specular reflection bounces, also limited by Total.",
    "transmission_bounce": "Maximum transmission bounces through refractive surfaces, also limited by Total.",
    "volume_bounce": "Maximum volume scattering bounces. Zero permits direct volume lighting without indirect volume scattering.",
    "transparent_max_bounce": "Maximum transparent-surface bounces. Increase for many overlapping alpha-transparent surfaces.",
    "filter_glossy": "Blur sharp glossy paths after diffuse scattering to reduce difficult indirect highlights. Higher values trade accuracy for less noise.",
    "caustics_reflective": "Include reflective caustic light paths. Disabling can reduce noise while removing that contribution.",
    "caustics_refractive": "Include refractive caustic light paths. Disabling can reduce noise while removing that contribution.",
    "fast_gi_method": "Replace indirect diffuse lighting with ambient occlusion after the bounce limit, or add an ambient occlusion approximation.",
    "ao_factor": "Strength of the ambient occlusion contribution used by Fast GI Approximation.",
    "ao_distance": "Scene-space distance used to search for occlusion in Fast GI and the Ambient Occlusion pass.",
    "ao_bounces": "Final-render bounce depth after which Fast GI replaces indirect diffuse lighting. Used by Replace GI after Bounces.",
    "allowmotionblur": "Sample camera, object and configured geometry motion over the camera shutter for motion blur.",
    "point_radius": "Fallback point/sphere radius in scene units when the source has no pscale attribute.",
    "curve_width": "Fallback full curve width in scene units when the source has no width attribute.",
    "volume_max_steps": "Maximum ray-marching steps per ray through volumes. Too few steps can truncate volume integration.",
    "vobject": "Geometry object selection relative to OBJ Scene Root. Supports Houdini node patterns, recursive subnet selection and @bundles.",
    "excludeobject": "Geometry objects excluded after normal and forced inclusion. Exclusion takes precedence.",
    "lights": "Light object selection relative to OBJ Scene Root. Supports Houdini node patterns and @bundles; lights must also be enabled.",
    "excludelights": "Light objects excluded after normal and forced inclusion. Exclusion takes precedence.",
    "world_sampling": "Choose automatic environment importance sampling, a manual importance-map resolution, or no environment importance sampling.",
    "world_max_bounces": "Maximum bounces through which the non-scene world contributes indirect light. Scene Environment Light uses its own settings.",
    "world_cast_shadow": "Allow the non-scene world light to cast shadows. Scene Environment Light uses its own settings.",
    "world_use_caustics": "Include the non-scene world in shadow-caustics sampling where supported by the scene and materials.",
    "material_kind": "Shader used by the fallback material when no applicable assignment or Material Override supplies a Cycles graph.",
    "basecolor": "Scene-linear color of the fallback surface material. Assigned or overriding Cycles materials use their own graph.",
    "color_attribute": "Optional geometry color attribute used by the fallback surface. Empty uses Material Color.",
    "roughness": "Surface roughness of the fallback shader. Larger values broaden reflections.",
    "metallic": "Metalness of the fallback Principled surface, from dielectric at 0 to metallic at 1.",
    "emission_strength": "Emission multiplier for the fallback emissive surface material.",
    "density": "Multiplier on the density grid in the fallback volume shader.",
    "anisotropy": "Scattering direction of the fallback volume. Positive favors forward scattering, negative favors backward scattering.",
    "volume_emission_strength": "Emission multiplier for the fallback volume shader.",
    "volume_emission_color": "Scene-linear emission tint of the fallback volume shader.",
    "emission_field": "Volume grid used to modulate fallback emission. Empty uses the density field.",
    "blackbody_intensity": "Strength of temperature-driven blackbody emission in the fallback volume shader.",
    "temperature_kelvin_scale": "Kelvin represented by one unit of the volume temperature field for fallback blackbody emission.",
    "pixel_filter": "Reconstruction filter used to combine samples into pixels. Wider, smoother filters reduce aliasing and soften detail.",
    "filter_width": "Reconstruction filter width in pixels. Larger values soften the image.",
    "mist_falloff": "Curve used to map distance between Mist Start and Mist Depth into the normalized Mist pass.",
    "use_texture_cache": "Use Cycles texture-cache files when available for texture loading and memory management.",
    "auto_texture_cache": "Generate missing or outdated Cycles texture-cache files automatically when rendering. This adds preprocessing time and disk usage. Generate and Clear manage the current ROP textures manually.",
    "device_mode": "Render on the CPU alone or on explicitly selected GPUs. GPU mode requires at least one checked device.",
    "compute_backend": "Backend shared by the selected GPUs. OptiX requires compatible NVIDIA hardware; CUDA uses the CUDA backend.",
    "device_refresh": "Rescan render devices, retaining saved selections by physical GPU identity. Unavailable selected devices remain visible.",
    "pass_add": "Choose a Cycles pass to add to the output list. Beauty is written through Output Picture.",
    "pass_count": "Ordered list of output passes. Add Pass creates an entry; disable or remove entries to omit their output.",
    "pass_type#": "Cycles pass to export. Choose an AOV or Light Group type to expose its source-name field.",
    "pass_dwa_level#": "Compression level for this pass when DWA-A or DWA-B is selected. Higher values produce smaller, lossier files.",
}


def about_templates():
    import hou

    # Short, separate native label rows fit ordinary parameter panes without
    # relying on HTML, rich text, or multiline-label sizing.
    entries = [
        hou.LabelParmTemplate(name, "", column_labels=(text,), is_label_hidden=True, help=ATTRIBUTION)
        for name, text in (
            ("ch_about_text", "Cycles for Houdini"),
            ("ch_about_renderer", "Integrates the Blender Cycles renderer."),
            ("ch_about_dlss", "Optional DLSS Ray Reconstruction"),
            ("ch_about_technology", "uses NVIDIA DLSS / NGX technology."),
            ("ch_about_availability", "Availability depends on build and selected hardware."),
            ("ch_about_trademarks", "NVIDIA, DLSS and NGX are trademarks of NVIDIA Corporation."),
        )
    ]
    entries.append(hou.LabelParmTemplate("ch_about_spacer", "", is_label_hidden=True))
    for name, text, target, help_text in (
        (
            "ch_about_notices",
            "Licenses and Notices…",
            "notices",
            "Open the locally installed third-party notice index and license texts.",
        ),
        (
            "ch_about_integration_license",
            "Cycles for Houdini License…",
            "integration",
            "Open the license for this Cycles for Houdini integration from the installed version.",
        ),
        (
            "ch_about_nvidia_license",
            "NVIDIA DLSS License…",
            "nvidia",
            "Open the locally installed NVIDIA DLSS / NGX SDK license; development builds can use the pinned source copy.",
        ),
        ("ch_about_cycles", "Blender Cycles…", "cycles", "Open the Blender Cycles renderer website."),
    ):
        # Matching stretch labels center each button in its own native row.
        entries.append(hou.LabelParmTemplate(name + "_left", "", is_label_hidden=True, join_with_next=True))
        entries.append(
            hou.ButtonParmTemplate(
                name,
                text,
                join_with_next=True,
                script_callback="from cycles_houdini.rop import rop_help; rop_help.open_link(" + repr(target) + ")",
                script_callback_language=hou.scriptLanguage.Python,
                help=help_text,
            )
        )
        entries.append(hou.LabelParmTemplate(name + "_right", "", is_label_hidden=True))
    return hou.FolderParmTemplate("ch_about", "About", entries, folder_type=hou.folderType.Tabs)


def link_target(target):
    """Prefer the immutable installed payload, then the development source."""
    if target == "cycles":
        return CYCLES_URL
    if target not in ("notices", "nvidia", "integration"):
        raise ValueError("Unknown Cycles About link: " + target)
    host = Path(os.environ.get("CYCLES_HOUDINI_HOST_ROOT", Path(__file__).resolve().parents[3]))
    source_value = os.environ.get("CYCLES_HOUDINI_SOURCE_ROOT")
    source = Path(source_value) if source_value else None
    relative = {
        "notices": "THIRD_PARTY_NOTICES.md",
        "nvidia": "notices/dlss-headers/LICENSE.txt",
        "integration": "LICENSE",
    }[target]
    candidates = [host.parent / relative]
    if source:
        candidates.append(source / ("packaging/notices/dlss-headers/LICENSE.txt" if target == "nvidia" else relative))
    for path in candidates:
        if path.is_file():
            return path.resolve().as_uri()
    # An installed runtime must include the notices. Do not silently open a
    # remote generic index that could hide an incomplete local installation.
    if target == "nvidia":
        return NVIDIA_LICENSE_URL
    raise FileNotFoundError("Cycles license or notices are missing beside the installed host directory: " + relative)


def open_link(target):
    import hou
    from PySide6 import QtCore, QtGui, QtWidgets

    url = QtCore.QUrl(link_target(target))
    if not url.isLocalFile():
        if not QtGui.QDesktopServices.openUrl(url):
            hou.ui.displayMessage("Could not open the website in your browser.", details=url.toString())
        return

    # LICENSE has no extension and Markdown file associations vary. Read the
    # installed document directly so all three links work on a clean machine.
    path = Path(url.toLocalFile())
    dialog = QtWidgets.QDialog(hou.qt.mainWindow())
    dialog.setAttribute(QtCore.Qt.WA_DeleteOnClose)
    dialog.setWindowTitle(
        {
            "notices": "Licenses and Notices",
            "integration": "Cycles for Houdini License",
            "nvidia": "NVIDIA DLSS License",
        }[target]
    )
    layout = QtWidgets.QVBoxLayout(dialog)
    reader = QtWidgets.QTextBrowser(dialog)
    if path.suffix.lower() == ".md":
        reader.setSource(url, QtGui.QTextDocument.ResourceType.MarkdownResource)
        # The installed index is beside notices/. In a checkout only that directory moves.
        if not (path.parent / "notices").is_dir() and (path.parent / "packaging/notices").is_dir():

            def open_source_notice(link):
                target = url.resolved(link)
                local = Path(target.toLocalFile())
                if target.isLocalFile() and local.is_relative_to(path.parent / "notices"):
                    target = QtCore.QUrl.fromLocalFile(str(path.parent / "packaging" / local.relative_to(path.parent)))
                reader.setSource(target)

            reader.setOpenLinks(False)
            reader.anchorClicked.connect(open_source_notice)
    else:
        reader.setPlainText(path.read_text(encoding="utf-8-sig"))
    layout.addWidget(reader)
    buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close, parent=dialog)
    buttons.rejected.connect(dialog.close)
    layout.addWidget(buttons)
    dialog.resize(750, 510)
    dialog.show()


def apply_tooltips(group):
    """Annotate all visible controls, including multiparm row templates."""
    import hou

    def visit(entries, hidden=False):
        result = []
        for parm in entries:
            hidden_here = hidden or parm.isHidden()
            if isinstance(parm, hou.FolderParmTemplate):
                parm.setParmTemplates(visit(parm.parmTemplates(), hidden_here))
            if not hidden_here and parm.type() not in (hou.parmTemplateType.Separator, hou.parmTemplateType.Label):
                text = TOOLTIPS.get(parm.name())
                if parm.name().startswith("world_visibility_"):
                    text = (
                        "Make the non-scene world visible to "
                        + parm.label().lower()
                        + " rays. Illumination and other ray visibility settings remain independent."
                    )
                if parm.name().startswith("ipr_") and text is None:
                    base = group.find(parm.name()[4:])
                    text = (TOOLTIPS.get(base.name()) or base.help()) if base else None
                    if text:
                        text = "Interactive viewer setting. " + text
                if text and (not parm.help() or parm.help().strip() == parm.label().strip() + "."):
                    parm.setHelp(text)
                if not isinstance(parm, hou.FolderParmTemplate) and not parm.help().strip():
                    raise RuntimeError("Missing Cycles ROP tooltip: " + parm.name())
            result.append(parm)
        return result

    return hou.ParmTemplateGroup(visit(group.entries()))


def help_page(group):
    """Generate standard Houdini wiki help with internal parameter anchors."""
    import hou

    dlss_guidance = (
        "DLSS Ray Reconstruction is optional and interactive only; compatible NVIDIA RTX hardware, "
        "an enabled build and an NGX driver runtime are required. The denoiser control reports unsupported requests."
        if group.find("ipr_dlss_quality") is not None
        else "This ROP definition does not expose DLSS controls. Conventional denoising uses OpenImageDenoise or OptiX as configured."
    )
    lines = [
        "= Cycles Render =",
        "#type: node",
        "#context: out",
        "",
        "Render Houdini scenes with Blender Cycles and write EXR, PNG, JPEG or TIFF images.",
        "",
        "== Getting started ==",
        "",
        "Select a Camera, choose geometry and lights on the Scene tab, and select CPU or explicit GPUs on Devices. "
        "Set Output Picture and the frame range, then choose Render to Disk. "
        "Use the Cycles Viewer for interactive rendering.",
        "",
        "Final disk renders use Final Render sampling and denoising. The interactive viewer uses IPR Settings; "
        "switching the viewer to Final uses the final sampling settings at the viewer resolution. "
        "Common Settings Resolution % controls disk and clapper output. In the Viewer, ROP % follows this value; "
        "a manual percentage independently scales the base Resolution. Linking resolution to zoom updates IPR Resolution % "
        "after navigation settles and holds the display size. These viewer choices apply in either sampling mode.",
        "",
        "== Sampling and output ==",
        "",
        "Adaptive Sampling stops converged pixels; Max Samples remains the upper limit. "
        "Denoising reconstructs Beauty from noisy color and optional guides. " + dlss_guidance,
        "",
        "Image Output controls pass selection, EXR precision/compression, separate files, multipart output and optional color conversion. "
        "Data passes and Cryptomatte remain raw. Display/view transforms belong to preview; Film Exposure changes rendered color.",
        "",
        "Controls opens background rendering and background-job actions. A background render snapshots the scene "
        "for a separate Houdini batch process; external assets must remain accessible.",
        "",
        "== Related guides ==",
        "",
        "* [Cycles object properties|/cycles/objects]",
        "* [Cycles camera properties|/cycles/camera]",
        "* [Cycles Viewer|/cycles/viewer]",
        "* [Shader nodes|/cycles/shaders]",
        "",
        "* [Licenses and notices|/cycles/licenses]",
        "",
        "== About and licenses ==",
        "",
        ATTRIBUTION,
        "",
        "The About tab opens the local notice index and NVIDIA SDK license from this installed version. "
        "Development hosts use the pinned source notices when no packaged copy is present.",
        "",
        "* [Blender Cycles|" + CYCLES_URL + "]",
        "* [NVIDIA DLSS SDK license|" + NVIDIA_LICENSE_URL + "]",
        "",
        "@parameters",
        "",
    ]

    def visit(entries, path=(), hidden=False):
        for parm in entries:
            hidden_here = hidden or parm.isHidden()
            if hidden_here:
                continue
            if isinstance(parm, hou.FolderParmTemplate):
                if parm.folderType() != hou.folderType.MultiparmBlock:
                    lines.extend(["== " + " / ".join((*path, parm.label())) + " ==", ""])
                elif parm.help():
                    parameter(parm)
                visit(parm.parmTemplates(), (*path, parm.label()), hidden_here)
            elif parm.type() not in (hou.parmTemplateType.Separator, hou.parmTemplateType.Label):
                parameter(parm)

    def parameter(parm):
        lines.extend(
            [parm.label() + ":", "    #id: " + parm.name(), "", "    " + parm.help().replace("\n", "\n    "), ""]
        )

    visit(group.entries())
    return "\n".join(lines)
