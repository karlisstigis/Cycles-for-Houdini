# SPDX-License-Identifier: Apache-2.0
"""Current native pass multiparm; no shadow checkbox/older AOV schemas."""

import hou


def templates():
    menu = "from cycles_houdini import passes\nreturn passes.menu()"
    button = hou.ButtonParmTemplate(
        "pass_add",
        "Add Pass",
        script_callback="from cycles_houdini import passes; passes.add_from_menu(kwargs)",
        script_callback_language=hou.scriptLanguage.Python,
    )
    button.setItemGeneratorScript(menu)
    button.setItemGeneratorScriptLanguage(hou.scriptLanguage.Python)
    button.setMenuType(hou.menuType.StringReplace)
    kind = hou.StringParmTemplate(
        "pass_type#",
        "Type",
        1,
        default_value=("depth",),
        item_generator_script=menu,
        item_generator_script_language=hou.scriptLanguage.Python,
        menu_type=hou.menuType.Normal,
    )
    codecs = ("inherit", "none", "dwab", "dwaa", "piz", "rle", "zips", "zip", "pxr24", "b44", "b44a")
    labels = (
        "Use Main Output",
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
    )
    shared = "{ pass_separate# == 0 output_separate_passes == 0 output_multipart == 0 }"
    path_disabled = (
        "{ output_multipart == 1 output_separate_passes == 0 } { pass_separate# == 0 output_separate_passes == 0 }"
    )
    output = hou.FolderParmTemplate(
        "pass_output#",
        "Output",
        [
            hou.ToggleParmTemplate(
                "pass_separate#",
                "Separate File",
                default_value=False,
                disable_when="{ output_separate_passes == 1 } { output_multipart == 1 }",
                help="Off includes this pass in Output Picture. On writes it only to its separate EXR. Passes to Separate Files overrides this toggle for all enabled passes. Matching separate paths combine passes into one multilayer file.",
            ),
            hou.StringParmTemplate(
                "pass_path#",
                "File",
                1,
                default_value=("",),
                string_type=hou.stringParmType.FileReference,
                disable_when=path_disabled,
                help="EXR path, with normal Houdini frame expressions. Empty inserts the layer name before the frame number (or the extension for a static output), inside a layer-named subfolder when Pass Folders is on. Multipart EXR uses Output Picture instead of this path. Explicit paths override Pass Folders. Matching single-part files must use the same compression.",
            ),
            hou.StringParmTemplate(
                "pass_precision#",
                "Precision",
                1,
                default_value=("float",),
                menu_items=("inherit", "half", "float"),
                menu_labels=("Use Main Output", "16-bit Half", "32-bit Float"),
                menu_type=hou.menuType.Normal,
                help="Channel precision for this pass, in a separate file or a shared multilayer EXR. Defaults to 32-bit Float for every pass type. Use Main Output follows the main precision exactly. Half is allowed for every pass, but reduces numeric accuracy and can corrupt Cryptomatte IDs.",
            ),
            hou.StringParmTemplate(
                "pass_compression#",
                "EXR Compression",
                1,
                default_value=("zip",),
                menu_items=codecs,
                menu_labels=labels,
                menu_type=hou.menuType.Normal,
                disable_when=shared,
                help="Compression for this separate file or multipart EXR part; defaults to ZIP for every pass type. Use Main Output follows the main codec and DWA level exactly. No codec is substituted based on pass type. Lossy codecs can alter numeric values or corrupt Cryptomatte IDs; use ZIP or PIZ for exact data. Shared single-part files require matching settings.",
            ),
            hou.FloatParmTemplate(
                "pass_dwa_level#",
                "DWA Compression Level",
                1,
                default_value=(45,),
                min=0,
                max=100,
                min_is_strict=True,
                disable_when=shared + " { pass_compression# != dwaa pass_compression# != dwab }",
            ),
        ],
        folder_type=hou.folderType.Collapsible,
    )
    rows = hou.FolderParmTemplate(
        "pass_count",
        "Passes",
        [
            hou.ToggleParmTemplate(
                "pass_enable#",
                "Enabled",
                default_value=True,
                disable_when="{ pass_type# == motion allowmotionblur == 1 }",
                help="Motion Vectors require shutter Motion Blur off.",
            ),
            kind,
            hou.StringParmTemplate(
                "pass_name#",
                "Layer Name",
                1,
                default_value=("",),
                help="Empty uses the Cycles pass name. Custom names contain ASCII letters, digits and underscores. Cryptomatte names apply to the entire set of numbered layers and its manifest.",
            ),
            hou.StringParmTemplate(
                "pass_source#",
                "Source Name",
                1,
                default_value=("aov_#",),
                conditionals={
                    hou.parmCondType.HideWhen: "{ pass_type# != aov_color pass_type# != aov_value pass_type# != lightgroup }"
                },
                help="Name on the shader AOV Output, or Light Group on the lights/emissive objects. Independent of the output Layer Name.",
            ),
            hou.IntParmTemplate(
                "pass_levels#",
                "Levels",
                1,
                default_value=(6,),
                min=2,
                max=16,
                min_is_strict=True,
                max_is_strict=True,
                conditionals={
                    hou.parmCondType.HideWhen: "{ pass_type# != crypto_object pass_type# != crypto_material pass_type# != crypto_asset }"
                },
                help="Cryptomatte ID/coverage pairs per pixel. Odd values round up to a complete RGBA layer.",
            ),
            output,
        ],
        folder_type=hou.folderType.MultiparmBlock,
        default_value=0,
    )
    return button, rows
