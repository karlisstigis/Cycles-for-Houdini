# SPDX-License-Identifier: Apache-2.0
"""Texture colour spaces: Houdini Principled and MaterialX names -> Cycles Image Texture input space.

Cycles offers ``auto`` (Houdini OCIO file rules, metadata, format), ``data``,
``scene_linear`` (the render working space) and the active OCIO names. Each
mapping lists candidates; the host picks the first one the active
configuration knows, so the ACES configuration's full names win and the
MaterialX short aliases remain as a fallback.
"""

from .plan import ColorSpace

SRGB = ("sRGB Encoded Rec.709 (sRGB)", "sRGB - Texture", "srgb_tx", "srgb_texture", "sRGB")

# MaterialX colour space names (the menu of mtlximage File Color Space).
MATERIALX = {
    "srgb_texture": SRGB,
    "srgb_tx": SRGB,
    "lin_rec709": ("Linear Rec.709 (sRGB)", "lin_rec709"),
    "acescg": ("ACEScg", "acescg"),
    "lin_ap1": ("ACEScg", "lin_ap1"),
    "srgb_displayp3": ("sRGB Encoded P3-D65", "srgb_displayp3"),
    "lin_displayp3": ("Linear P3-D65", "lin_displayp3"),
    "g22_ap1": ("Gamma 2.2 Encoded AP1", "g22_ap1"),
    "g22_rec709": ("Gamma 2.2 Encoded Rec.709", "g22_rec709"),
    "g18_rec709": ("Gamma 1.8 Encoded Rec.709", "g18_rec709"),
    "rec709_display": ("Rec.1886 Rec.709 - Display", "rec709_display"),
    "raw": ("data",),
}


def houdini(value, data, plan=None, label=""):
    """Principled Shader 'Source Color Space' / 'Texture Color Space'.

    ``data`` is True for textures that hold values rather than colours
    (single channels, normals, bump and displacement): Houdini's ``linear``
    reads those unchanged, which is Cycles ``data``.
    """
    text = str(value or "auto").strip()
    key = text.lower()
    if key == "auto":
        return ColorSpace(("auto",))
    if key in ("raw", "data"):
        return ColorSpace(("data",))
    if key == "linear":
        return ColorSpace(("data",) if data else ("scene_linear",))
    if key == "srgb":
        return ColorSpace(SRGB)
    if key == "rec709":
        if plan is not None:
            plan.approx(label, "the Rec. 709 camera curve is not a Cycles input space; Auto is used")
        return ColorSpace(("auto",))
    return ColorSpace((text,))


def materialx(value, color):
    """mtlximage File Color Space. MaterialX applies colour spaces to colour images only."""
    if not color:
        return ColorSpace(("data",))
    text = str(value or "").strip()
    if not text:
        return ColorSpace(("auto",))
    return ColorSpace(MATERIALX.get(text.lower(), (text,)))


def resolve(space, available):
    """The first candidate the Cycles Image Texture menu offers, else the first candidate."""
    names = set(available or ())
    lowered = {name.lower(): name for name in names}
    for candidate in space.candidates:
        if candidate in names:
            return candidate
        if candidate.lower() in lowered:
            return lowered[candidate.lower()]
    return space.candidates[0]
