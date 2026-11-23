# SPDX-License-Identifier: Apache-2.0
"""Names both sides of the Blender bridge agree on; needs no Blender.

object_attribute_field (moved unchanged from the Blender exporter, blend_export.py) and
TextureNames, the file-naming rules of exported textures (moved unchanged from the Blender
material exporter's _Images, blend_material_export.py, which now derives from it). The
exporter (blender/cycles_blender, GPL-3.0-or-later, runs inside Blender) writes these names;
the Houdini side reads and checks them.
"""

from pathlib import Path
import re


def object_attribute_field(kind, name, alpha=False):
    """The field name (before Houdini's attribute-name encoding) that carries a Blender Attribute
    node of type OBJECT or INSTANCER to Cycles: one 3-component colour field, plus one for its
    alpha. blender_assets fills it per object; blend_materials points the Attribute node at it."""
    return "__cycles_blender_" + kind.lower() + "_" + name + ("__alpha" if alpha else "")


class TextureNames:
    """File names of exported textures: one portable name component per image (Windows-safe,
    no Houdini variables), with a <UDIM> token for tiled images. The Blender material
    exporter's _Images derives from this class; Houdini checks exported names with it."""

    TOKEN = "<UDIM>"
    MAX_NAME = 120
    # Windows device names, also with superscript digits; matched against the part before the first dot.
    RESERVED = re.compile(
        r"(?i)(con|prn|aux|nul|conin\$|conout\$|com[0-9\u00b9\u00b2\u00b3]|lpt[0-9\u00b9\u00b2\u00b3])"
    )
    # Separators, drive colons, Windows-invalid and control characters, and Houdini's
    # string expansion ($VAR, `expr`), so a texture parm never expands a file name.
    _UNSAFE = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*$`]')

    @classmethod
    def safe_name(cls, text, fallback="texture", token=False):
        """One portable file-name component: no separators, drive letters, `..`, Windows
        device names or invalid characters, nor Houdini variables. `token` keeps <UDIM>."""
        text = re.split(r"[\\/]", str(text))[-1]
        parts = text.split(cls.TOKEN) if token else [text]
        text = cls.TOKEN.join(cls._UNSAFE.sub("_", part) for part in parts)
        text = text.lstrip(" .").rstrip(" .")
        if not text.replace(cls.TOKEN, "").strip("._- "):
            text = fallback
        if cls.RESERVED.fullmatch(text.split(".", 1)[0].rstrip(" ")):
            text = "_" + text
        if len(text) > cls.MAX_NAME:
            cut = text.find(cls.TOKEN) if token else -1
            if cut < 0:
                suffix = Path(text).suffix
                cut = len(text) - len(suffix) if len(suffix) <= 16 else len(text)
            head, tail = text[:cut], text[cut:]
            if len(tail) > 48:
                tail = (cls.TOKEN if tail.startswith(cls.TOKEN) else "") + Path(tail).suffix[:16]
            head = head[: max(cls.MAX_NAME - len(tail), 1)]
            text = head + tail if tail else head.rstrip(" .")
        return text

    @classmethod
    def tile_name(cls, pattern, number):
        return pattern if number is None else pattern.replace(cls.TOKEN, str(number))
