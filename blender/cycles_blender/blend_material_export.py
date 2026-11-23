# SPDX-License-Identifier: GPL-3.0-or-later
# Blender component of Cycles for Houdini (see blender/README.md). This file is one of the scripts
# that run inside Blender and use its Python API, so it is published under the GNU General Public
# License, version 3 or later (blender/LICENSE). The Houdini integration (Apache-2.0) never imports
# it; the two sides exchange only files, command-line arguments and pipe messages.
"""Blender-only, dependency-owning material extraction (no Houdini imports).

This module is loaded by file path in Blender's isolated Python process. Node
groups are expanded through their actual interface links, retaining defaults;
only branches connected to the active Cycles material output are exported.
"""

from pathlib import Path
import hashlib
import math
import re

from cycles_houdini.blender.blend_names import TextureNames


class BlendMaterialExportError(ValueError):
    pass


_STANDARD_COLOR_IDS = {
    "Linear Rec.709": "lin_rec709_scene",
    "Linear Rec.709 (sRGB)": "lin_rec709_scene",
    "Linear Rec.709 (sRGB) - Scene-referred": "lin_rec709_scene",
    "sRGB": "srgb_rec709_display",
    "sRGB - Texture": "srgb_rec709_display",
    "sRGB Texture": "srgb_rec709_display",
    "sRGB - Display": "srgb_rec709_display",
    "ACEScg": "lin_ap1_scene",
    "ACES2065-1": "lin_ap0_scene",
}


def _color_identity(space):
    # Older Blender distributions embed OCIO without interop identifiers.
    identity = space.getInteropID() if hasattr(space, "getInteropID") else ""
    return identity or _STANDARD_COLOR_IDS.get(space.getName(), "")


def image_colorspace(image):
    """The color space Blender renders the image with.

    A file saved under another OCIO configuration can name a space this one lacks; RNA
    then reads "" until the image loads, and loading applies Blender's own fallback
    (the default byte or float space). Load it to read what Blender actually uses.
    """
    settings = image.colorspace_settings
    if not settings.name:
        try:
            image.size[:]  # loads the image; Blender resolves the colour space on load
        except Exception:
            pass
    if settings.name:
        return settings.name
    import PyOpenColorIO as ocio

    config = ocio.GetCurrentConfig()
    for role in ("default_float" if image.is_float else "default_byte", ocio.ROLE_DEFAULT):
        space = config.getColorSpace(role)
        if space is not None:
            return space.getName()
    return ""


def material_key(material):
    if material is None:
        return "blender-material-default-surface"
    library = getattr(getattr(material, "library", None), "filepath", "")
    return "blender-material-" + hashlib.sha256((library + "\0" + material.name_full).encode()).hexdigest()[:24]


def _plain(value):
    if isinstance(value, (str, bool, int)) or value is None:
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise BlendMaterialExportError("Non-finite shader parameter")
        return value
    return [_plain(v) for v in value]


def _socket(socket, index):
    result = dict(
        index=index, identifier=socket.identifier, name=socket.name, type=socket.type, enabled=bool(socket.enabled)
    )
    if hasattr(socket, "default_value"):
        result["value"] = _plain(socket.default_value)
    return result


# RNA property types carried as node settings; pointers (images, objects, ramps, curves,
# texture mappings) have their own records below.
_SETTING_TYPES = frozenset(("BOOLEAN", "INT", "FLOAT", "STRING", "ENUM"))


def node_settings(node):
    """Names of the node's own RNA settings (not those every node has), in RNA order."""
    rna = getattr(node, "bl_rna", None)
    if rna is None:  # not a Blender node (test doubles)
        return []
    import bpy

    common = {p.identifier for p in bpy.types.ShaderNode.bl_rna.properties}
    return [
        p.identifier
        for p in rna.properties
        if p.identifier not in common and p.identifier != "rna_type" and p.type in _SETTING_TYPES
    ]


def _properties(node):
    # RNA settings, unlike sockets, select shader algorithms or coordinate
    # domains. Every one is carried (derived from the node's RNA, not a list of
    # names), so the receiving adapter maps each, or rejects an unsupported one by
    # name, instead of silently keeping the Houdini node's own default.
    result = {}
    for name in node_settings(node):
        try:
            value = getattr(node, name)
            result[name] = sorted(value) if isinstance(value, (set, frozenset)) else _plain(value)
        except (TypeError, ValueError):
            raise BlendMaterialExportError("Unsupported shader property: " + node.name + "." + name)
    if getattr(node, "object", None) is not None:
        result["object_reference"] = node.object.name_full
    if hasattr(node, "texture_mapping"):
        mapping = node.texture_mapping
        identity = (
            not any(mapping.translation)
            and not any(mapping.rotation)
            and tuple(mapping.scale) == (1.0, 1.0, 1.0)
            and (mapping.mapping_x, mapping.mapping_y, mapping.mapping_z) == ("X", "Y", "Z")
            and mapping.vector_type == "POINT"
            and not mapping.use_min
            and not mapping.use_max
            and mapping.mapping == "FLAT"
        )
        if not identity:
            raise BlendMaterialExportError(
                "Nonidentity internal texture mapping requires an explicit Mapping node: " + node.name
            )
    if hasattr(node, "color_mapping"):
        mapping = node.color_mapping
        if (
            mapping.use_color_ramp
            or mapping.brightness != 1
            or mapping.contrast != 1
            or mapping.saturation != 1
            or mapping.blend_factor != 0
        ):
            raise BlendMaterialExportError(
                "Internal texture color mapping requires explicit shader nodes: " + node.name
            )
    if hasattr(node, "color_ramp"):
        ramp = node.color_ramp
        result["color_ramp"] = dict(
            interpolation=ramp.interpolation,
            color_mode=ramp.color_mode,
            hue_interpolation=ramp.hue_interpolation,
            elements=[dict(position=float(e.position), color=list(e.color)) for e in ramp.elements],
            table=[list(ramp.evaluate(i / 256)) for i in range(257)],
            interpolate=ramp.interpolation != "CONSTANT",
        )
    if node.bl_idname in ("ShaderNodeFloatCurve", "ShaderNodeRGBCurve", "ShaderNodeVectorCurve"):
        # Blender CurveMapping cannot be replaced by a different spline basis.
        # Preserve its exact points/handles for a capable receiving adapter.
        mapping = node.mapping
        mapping.initialize()
        mapping.update()
        # Match Cycles' public exporter: sample every authored channel's range,
        # compose Combined before RGB, and leave black/white controls dormant.
        import struct

        def f32(v):
            return struct.unpack("f", struct.pack("f", float(v)))[0]

        curves = list(mapping.curves)
        minimum = min(float(p.location.x) for curve in curves for p in curve.points)
        maximum = max(float(p.location.x) for curve in curves for p in curve.points)
        span = f32(maximum - minimum)
        table = []
        for i in range(257):
            x = f32(minimum + f32(f32(i / 256) * span))
            common = mapping.evaluate(curves[3], x) if len(curves) == 4 else x
            table.append([mapping.evaluate(curves[0 if len(curves) == 1 else c], common) for c in range(3)])
        result["curve_mapping"] = dict(
            minimum=minimum,
            maximum=maximum,
            table=table,
            extend=mapping.extend,
            use_clip=mapping.use_clip,
            clip_min_x=mapping.clip_min_x,
            clip_min_y=mapping.clip_min_y,
            clip_max_x=mapping.clip_max_x,
            clip_max_y=mapping.clip_max_y,
            black_level=list(mapping.black_level),
            white_level=list(mapping.white_level),
            curves=[
                [dict(location=list(p.location), handle_type=p.handle_type) for p in curve.points]
                for curve in mapping.curves
            ],
        )
    return result


class _Images(TextureNames):
    """Record every image a material uses; `textures` picks what happens to its files.

    "copy" (default; the reference render, blender_assets._share_textures): every image
    is copied to <directory>/textures under its real file name. The visible name is the
    basename of the image's file path or, for a packed image without one, its datablock
    name plus the format's extension. Tiled images keep their pattern with a <UDIM>
    token. Within one export two different payloads never share a name: a later one
    gets _2, _3, ... before the extension (or the UDIM token, for all its tiles alike);
    identical bytes share one file. Identity remains the content sha256 recorded per
    dependency.

    "in_place" (library material import, the converter, worlds): images on disk stay
    where they are and are never read here; only packed images are written to
    <directory>/textures, exactly as in "copy".

    Image record (node["image"]), "copy":
        filename        "textures/<name>" (relative to <directory>; <UDIM> pattern if tiled)
        source_path     resolved absolute source path (native separators)
        source_kind     "FILE" | "TILED"
        colorspace, is_data, alpha_mode, color_interop_id
        tiles           [tile numbers] (Blender's tiles; [] unless TILED)
        dependencies    [{filename: "textures/<tile name>", source_path, sha256, packed}]
    "in_place" adds, for every image:
        textures        "in_place"
        packed          True: the copy-mode record above (relative filename, sha256 per
                        dependency; the host places the files and rewrites the names).
                        False: filename is the resolved absolute path with forward
                        slashes (a <UDIM> pattern if tiled), no bytes were read and no
                        dependency has a sha256.
        missing         True when the file (every tile, if tiled) is absent from disk
    and, for every dependency, `missing` (that tile's file is absent). Not packed:
    dependencies are one per Blender tile, each {filename: absolute forward-slash path,
    source_path: native absolute path, packed: False, missing}; `tiles` lists the tiles
    whose files exist (Blender's tiles when none does).
    """

    MODES = ("copy", "in_place")

    # Image.file_format -> extension, for names that carry no usable image extension.
    FORMAT_SUFFIX = {
        "BMP": ".bmp",
        "IRIS": ".rgb",
        "PNG": ".png",
        "JPEG": ".jpg",
        "JPEG2000": ".jp2",
        "TARGA": ".tga",
        "TARGA_RAW": ".tga",
        "CINEON": ".cin",
        "DPX": ".dpx",
        "OPEN_EXR_MULTILAYER": ".exr",
        "OPEN_EXR": ".exr",
        "HDR": ".hdr",
        "TIFF": ".tif",
        "WEBP": ".webp",
    }
    IMAGE_SUFFIXES = frozenset(
        (*FORMAT_SUFFIX.values(), ".jpeg", ".tiff", ".sgi", ".bw", ".rgba", ".j2c", ".psd", ".tx", ".dds", ".avif")
    )
    _TILE = re.compile(r"(?<!\d)1\d{3}(?!\d)")

    def __init__(self, directory, textures="copy"):
        if textures not in self.MODES:
            raise BlendMaterialExportError("Unknown texture mode: " + repr(textures))
        self.textures = textures
        self.root = Path(directory)
        self.cache = {}
        self.dependencies = []
        self.names = {}  # casefolded relative name -> sha256 this export placed there

    @classmethod
    def tagged(cls, name, tag):
        """`name` with `_tag` before its UDIM token (all tiles alike), else before its extension."""
        if cls.TOKEN in name:
            head, tail = name.split(cls.TOKEN, 1)
            stem, separator = re.fullmatch(r"(.*?)([._-]?)", head, re.DOTALL).groups()
            return stem + "_" + tag + separator + cls.TOKEN + tail
        path = Path(name)
        return path.stem + "_" + tag + path.suffix

    def file_name(self, image, tiled):
        """Visible name (a <UDIM> pattern for tiled images) before disambiguation."""
        raw = re.split(r"[\\/]", (image.filepath or "").strip())[-1]
        from_path = raw not in ("", ".", "..")
        if not from_path:
            raw = image.name
        if tiled and self.TOKEN not in raw:
            # A path naming one tile (wall.1001.exr): its last tile number becomes the token.
            numbers = list(self._TILE.finditer(raw))
            if numbers:
                raw = raw[: numbers[-1].start()] + self.TOKEN + raw[numbers[-1].end() :]
            elif from_path or Path(raw).suffix.lower() in self.IMAGE_SUFFIXES:
                raw = Path(raw).stem + "." + self.TOKEN + Path(raw).suffix
            else:
                raw = raw + "." + self.TOKEN  # a datablock name such as Baked.001
        fallback = "texture." + self.TOKEN if tiled else "texture"
        name = self.safe_name(raw, fallback, token=tiled)
        suffix = Path(name.replace(self.TOKEN, "1001")).suffix
        if from_path:
            usable = re.fullmatch(r"\.(?=[0-9]*[A-Za-z])[A-Za-z0-9]{1,10}", suffix)
        else:
            usable = suffix.lower() in self.IMAGE_SUFFIXES
        if not usable:
            extension = self.FORMAT_SUFFIX.get(getattr(image, "file_format", ""), "")
            if not extension:
                raise BlendMaterialExportError("Cannot identify image file format: " + image.name)
            name += extension
        return name

    @classmethod
    def tile_path(cls, original, number):
        """Source file of one tile: substitute <UDIM>, else the last tile number of the basename."""
        if number is None:
            return original
        if cls.TOKEN in original:
            return original.replace(cls.TOKEN, str(number))
        path = Path(original)
        numbers = list(cls._TILE.finditer(path.name))
        if not numbers:
            return original
        found = numbers[-1]
        return str(path.with_name(path.name[: found.start()] + str(number) + path.name[found.end() :]))

    def _available(self, relative, digest):
        known = self.names.get(relative.casefold())
        if known is not None:
            return known == digest
        path = self.root / relative
        if not path.exists():
            return True
        # Another export may share this staging folder: never replace its payload.
        return path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == digest

    def claim(self, name, digests):
        """First of name, name_2, ... whose every tile is unused or already holds the same bytes.
        `digests` is [(tile number or None, sha256)]."""
        for index in range(1, 10000):
            candidate = name if index == 1 else self.tagged(name, str(index))
            relatives = ["textures/" + self.tile_name(candidate, number) for number, _ in digests]
            if all(self._available(r, d) for r, (_, d) in zip(relatives, digests, strict=True)):
                for relative, (_, digest) in zip(relatives, digests, strict=True):
                    self.names[relative.casefold()] = digest
                return candidate
        raise BlendMaterialExportError("Too many different images named " + name)

    def export(self, image):
        import bpy

        identity = image.as_pointer()
        if identity in self.cache:
            return self.cache[identity]
        source = image.source
        if source not in ("FILE", "TILED"):
            raise BlendMaterialExportError("Image source " + source + " requires an explicit conversion: " + image.name)
        if image.use_multiview:
            raise BlendMaterialExportError("Multiview images require an explicit view selection: " + image.name)
        if self.textures == "in_place" and not self.is_packed(image):
            result = self._in_place(image)
            self.cache[identity] = result
            return result
        original = bpy.path.abspath(image.filepath, library=image.library)
        original = str(Path(original).resolve())
        packed = list(getattr(image, "packed_files", ()))
        tiles = [int(t.number) for t in image.tiles] if source == "TILED" else []
        slots = tiles or [None]
        name = self.file_name(image, bool(tiles))
        folder = self.root / "textures"
        folder.mkdir(parents=True, exist_ok=True)
        import os

        pending = []  # (number, source path, sha256, packed, temporary file)
        try:
            for number in slots:
                source_path = self.tile_path(original, number)
                packed_entry = None
                for entry in packed:
                    entry_path = getattr(entry, "filepath", "")
                    if (
                        number is None
                        or getattr(entry, "tile_number", None) == number
                        or str(number) in Path(entry_path).stem
                    ):
                        packed_entry = entry
                        break
                blob = getattr(packed_entry, "packed_file", None)
                if blob is None and number is None:
                    blob = getattr(image, "packed_file", None)
                data = bytes(blob.data) if blob is not None else Path(source_path).read_bytes()
                temporary = folder / (".pending-" + os.urandom(8).hex())
                temporary.write_bytes(data)
                pending.append((number, source_path, hashlib.sha256(data).hexdigest(), blob is not None, temporary))
                del data
            pattern = self.claim(name, [(number, digest) for number, _, digest, _, _ in pending])
            records = []
            for number, source_path, digest, is_packed, temporary in pending:
                relative = "textures/" + self.tile_name(pattern, number)
                destination = self.root / relative
                if destination.is_file():
                    temporary.unlink()  # claim() verified these bytes already
                else:
                    os.replace(temporary, destination)
                records.append(dict(filename=relative, source_path=source_path, sha256=digest, packed=is_packed))
        finally:
            for *_, temporary in pending:
                if temporary.exists():
                    temporary.unlink()
        if self.textures == "in_place":
            for record in records:
                record["missing"] = False
        self.dependencies.extend(records)
        result = dict(
            filename="textures/" + pattern,
            source_path=original,
            source_kind=source,
            colorspace=image_colorspace(image),
            is_data=bool(image.colorspace_settings.is_data),
            alpha_mode=image.alpha_mode,
            tiles=tiles,
            dependencies=records,
        )
        if self.textures == "in_place":
            result.update(textures="in_place", packed=True, missing=False)
        import PyOpenColorIO as ocio

        space = ocio.GetCurrentConfig().getColorSpace(result["colorspace"])
        result["color_interop_id"] = _color_identity(space) if space is not None else ""
        self.cache[identity] = result
        return result

    @staticmethod
    def is_packed(image):
        return getattr(image, "packed_file", None) is not None or bool(list(getattr(image, "packed_files", ())))

    @classmethod
    def token_path(cls, original):
        """A tiled image's path with <UDIM>: kept if present, else replacing the basename's last tile number."""
        if cls.TOKEN in original:
            return original
        head, separator, name = original.replace("\\", "/").rpartition("/")
        numbers = list(cls._TILE.finditer(name))
        if not numbers:
            return original
        found = numbers[-1]
        return head + separator + name[: found.start()] + cls.TOKEN + name[found.end() :]

    def _in_place(self, image):
        """Record an unpacked FILE/TILED image where it is on disk: its path, never its bytes."""
        import bpy
        import os

        original = ""  # no file path at all: missing, with nothing to point at
        if (image.filepath or "").strip():
            original = os.path.normpath(os.path.abspath(bpy.path.abspath(image.filepath, library=image.library)))
        tiles = [int(t.number) for t in image.tiles] if image.source == "TILED" else []
        records, present = [], []
        for number in tiles or [None]:
            path = self.tile_path(original, number) if original else ""
            exists = bool(path) and os.path.isfile(path)
            if exists and number is not None:
                present.append(number)
            records.append(dict(filename=path.replace("\\", "/"), source_path=path, packed=False, missing=not exists))
        self.dependencies.extend(records)
        pattern = self.token_path(original) if tiles else original
        result = dict(
            filename=pattern.replace("\\", "/"),
            source_path=original,
            source_kind=image.source,
            # Blender loads the file itself when the stored name is unknown; a missing
            # file falls back to the default role (image_colorspace).
            colorspace=image_colorspace(image),
            is_data=bool(image.colorspace_settings.is_data),
            alpha_mode=image.alpha_mode,
            tiles=present or tiles,
            dependencies=records,
            textures="in_place",
            packed=False,
            missing=all(record["missing"] for record in records),
        )
        import PyOpenColorIO as ocio

        space = ocio.GetCurrentConfig().getColorSpace(result["colorspace"])
        result["color_interop_id"] = _color_identity(space) if space is not None else ""
        return result


# Node layout. Positions are Blender node-editor view units (about pixels at UI
# scale 1; y grows up) of a node's top-left corner. Houdini maps them onto the
# rebuilt VOP network (blend_materials._Layout); records without them keep
# Houdini's automatic layout.


def _absolute(node):
    """Top-left corner of `node` in its own tree's view space, frame parenting resolved."""
    absolute = getattr(node, "location_absolute", None)  # Blender 4.4+
    if absolute is not None:
        return float(absolute[0]), float(absolute[1])
    # Older Blender: `location` is relative to the parent frame, frames nest.
    x, y = float(node.location[0]), float(node.location[1])
    parent = getattr(node, "parent", None)
    while parent is not None:
        x, y = x + float(parent.location[0]), y + float(parent.location[1])
        parent = getattr(parent, "parent", None)
    return x, y


def _height(node):
    """Estimated drawn height. `Node.dimensions` is zero until an editor draws the
    node (always in background exports) and carries the UI scale, so estimate."""
    if getattr(node, "hide", False):
        return 30.0

    def visible(sockets):
        return sum(
            1
            for s in sockets
            if getattr(s, "enabled", True) and not getattr(s, "hide", False) and not getattr(s, "is_unavailable", False)
        )

    return 40.0 + 22.0 * (visible(node.outputs) + visible(node.inputs))


def _frames_of(node):
    parent = getattr(node, "parent", None)
    while parent is not None:
        yield parent
        parent = getattr(parent, "parent", None)


def _id_order(identity):
    return (1, 0) if identity == "output" else (0, int(identity[1:]))


class _Graph:
    def __init__(self, images):
        self.images = images
        self.nodes = []
        self.emitted = {}
        self.active = set()
        # node id -> (group context, Blender node), for the layout pass.
        self.origins = {}

    def layout(self, output):
        """Attach flattened Blender-space layout to the emitted nodes; return record keys.

        Rules (view units, top-left corners, y up):
        * A node keeps its absolute position in its own tree (`location_absolute`,
          or `location` summed up the frame `parent` chain before Blender 4.4).
        * A flattened group's emitted internals keep their relative layout as one
          block (nested groups expanded first). The block's bounding box is moved
          so its right edge sits on the group node's right edge and its vertical
          centre on the group node's vertical centre.
        * Room for the block: every node (or expanded block) of the containing
          tree lying entirely left of the group node (right edge <= its x) moves
          left by (block width - group node width), so upstream columns keep
          their order. Shifts of several groups add up; nodes sharing the group
          node's column or right of it do not move.
        * A frame's rect edges move with the same shifts (it stretches around an
          expanded group). Frames inside a group nest in the innermost frame that
          holds the group node. Only frames holding an exported node are kept.
        """
        positions, _, frames = self._place((), {} if output is None else {"output": output})
        by_id = {item["id"]: item for item in self.nodes}
        for identity, (x, y) in positions.items():
            if identity == "output":
                continue
            item, node = by_id[identity], self.origins[identity][1]
            item["layout"] = [x, y]
            item["width"] = float(node.width)
            dimensions = getattr(node, "dimensions", None)
            if dimensions is not None and dimensions[0] > 0 and dimensions[1] > 0:
                item["dimensions"] = [float(dimensions[0]), float(dimensions[1])]
        result = {}
        if output is not None:
            x, y = positions["output"]
            result["output_layout"] = dict(name=output.name, layout=[x, y], width=float(output.width))
        if frames:
            ids = {id(frame): "f" + str(i) for i, frame in enumerate(frames)}
            result["frames"] = [
                dict(
                    id=ids[id(frame)],
                    name=frame["name"],
                    label=frame["label"],
                    color=frame["color"],
                    parent=ids[id(frame["parent"])] if frame["parent"] is not None else None,
                    nodes=sorted(set(frame["nodes"]), key=_id_order),
                    rect=[float(v) for v in frame["rect"]],
                )
                for frame in frames
            ]
        return result

    def _place(self, context, extra):
        """Lay out one tree with its expanded groups.

        Returns (positions, box, frames): id -> (x, y) in this tree's space, the
        content bounding box (left, bottom, right, top) and every frame record
        inside (rects in this tree's space, `parent` None when outermost here).
        """
        keys, depth = tuple(n.as_pointer() for n in context), len(context)
        items, groups = dict(extra), {}
        for identity, (where, node) in self.origins.items():
            if tuple(n.as_pointer() for n in where[:depth]) != keys:
                continue
            if len(where) == depth:
                items[identity] = node
            else:
                groups.setdefault(where[depth].as_pointer(), where[depth])
        expanded = {pointer: (group, *self._place(context + (group,), {})) for pointer, group in groups.items()}
        origin = {pointer: _absolute(group)[0] for pointer, group in groups.items()}
        grow = {p: max(0.0, box[2] - box[0] - float(g.width)) for p, (g, _, box, _) in expanded.items()}

        def shift(x, right=None):
            # A node moves when it lies entirely left of the group node (a node
            # sharing the group node's column stays); a frame edge when left of it.
            edge = x if right is None else right
            return x - sum(d for p, d in grow.items() if (edge < origin[p] if right is None else edge <= origin[p]))

        positions, rects, frames, members = {}, [], [], []
        for identity, node in items.items():
            x, y = _absolute(node)
            left = shift(x, x + float(node.width))
            positions[identity] = (left, y)
            rects.append((left, y - _height(node), left + float(node.width), y))
            members.append((node, (identity,)))
        for group, inner, box, inner_frames in expanded.values():
            x, y = _absolute(group)
            dx = shift(x, x + float(group.width)) + float(group.width) - box[2]
            dy = y - _height(group) / 2 - (box[1] + box[3]) / 2
            for identity, (ix, iy) in inner.items():
                positions[identity] = (ix + dx, iy + dy)
            rects.append((box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy))
            for frame in inner_frames:
                left, bottom, right, top = frame["rect"]
                frame["rect"] = (left + dx, bottom + dy, right + dx, top + dy)
            members.append((group, tuple(inner)))
        local = {}
        for node, ids in members:
            for frame in _frames_of(node):
                record = local.get(frame.as_pointer())
                if record is None:
                    x, y = _absolute(frame)
                    width, height = float(frame.width), float(frame.height)
                    record = local[frame.as_pointer()] = dict(
                        name=frame.name,
                        label=frame.label or frame.name,
                        color=[float(c) for c in frame.color][:3] if frame.use_custom_color else None,
                        nodes=[],
                        rect=(shift(x), y - height, shift(x + width), y),
                        source=frame,
                    )
                record["nodes"].extend(ids)
        for record in local.values():
            parent = getattr(record.pop("source"), "parent", None)
            record["parent"] = local.get(parent.as_pointer()) if parent is not None else None
        for group, _, _, inner_frames in expanded.values():
            holder = getattr(group, "parent", None)
            holder = local.get(holder.as_pointer()) if holder is not None else None
            for frame in inner_frames:
                if frame["parent"] is None:
                    frame["parent"] = holder
            frames.extend(inner_frames)
        frames = list(local.values()) + frames
        box = (
            (min(r[0] for r in rects), min(r[1] for r in rects), max(r[2] for r in rects), max(r[3] for r in rects))
            if rects
            else None
        )
        return positions, box, frames

    def input(self, socket, context):
        links = [link for link in socket.links if link.is_valid and not getattr(link, "is_muted", False)]
        if not links:
            return (
                dict(value=_plain(socket.default_value), value_type=socket.type)
                if hasattr(socket, "default_value")
                else {}
            )
        if len(links) != 1:
            raise BlendMaterialExportError("Multiple shader links into " + socket.node.name + "/" + socket.name)
        return self.output(links[0].from_socket, context)

    def output(self, socket, context):
        node = socket.node
        portal = tuple(n.as_pointer() for n in context), node.as_pointer(), socket.identifier
        if portal in self.active:
            raise BlendMaterialExportError("Cyclic Blender material graph: " + node.name)
        self.active.add(portal)
        try:
            if node.bl_idname == "NodeReroute":
                return self.input(node.inputs[0], context)
            if node.mute:
                for link in node.internal_links:
                    if link.to_socket == socket:
                        return self.input(link.from_socket, context)
                raise BlendMaterialExportError("Muted node has no bypass for " + node.name + "/" + socket.name)
            if node.bl_idname == "NodeGroupInput":
                if not context:
                    raise BlendMaterialExportError("Group input outside a shader group")
                group = context[-1]
                target = next((s for s in group.inputs if s.identifier == socket.identifier), None)
                if target is None:
                    raise BlendMaterialExportError("Unresolved shader group input: " + socket.identifier)
                value = self.input(target, context[:-1])
                if "source" not in value:
                    value["explicit_default"] = True
                return value
            if node.bl_idname == "ShaderNodeGroup":
                if len(context) >= 32 or node.node_tree is None:
                    raise BlendMaterialExportError("Shader group nesting is invalid: " + node.name)
                outputs = [n for n in node.node_tree.nodes if n.bl_idname == "NodeGroupOutput" and n.is_active_output]
                if len(outputs) != 1:
                    raise BlendMaterialExportError("Shader group needs one active output: " + node.name)
                target = next((s for s in outputs[0].inputs if s.identifier == socket.identifier), None)
                if target is None:
                    raise BlendMaterialExportError("Unresolved shader group output: " + socket.identifier)
                value = self.input(target, context + (node,))
                if "source" not in value:
                    value["explicit_default"] = True
                return value
            key = tuple(n.as_pointer() for n in context), node.as_pointer()
            if key not in self.emitted:
                identity = "n" + str(len(self.emitted))
                self.emitted[key] = identity
                self.origins[identity] = context, node
                item = dict(
                    id=identity,
                    name="/".join([n.name for n in context] + [node.name]),
                    type=node.bl_idname,
                    properties=_properties(node),
                    inputs=[],
                    outputs=[
                        _socket(s, i) for i, s in enumerate(node.outputs) if not getattr(s, "is_unavailable", False)
                    ],
                    location=list(node.location),
                )
                if hasattr(node, "image"):
                    if node.image is None:
                        raise BlendMaterialExportError("Image Texture has no image: " + node.name)
                    item["image"] = self.images.export(node.image)
                    user = node.image_user
                    if node.image.source not in ("FILE", "TILED") or user.use_auto_refresh:
                        raise BlendMaterialExportError(
                            "Animated image user requires a sampled image adapter: " + node.name
                        )
                self.nodes.append(item)
                for i, source in enumerate(node.inputs):
                    if getattr(source, "is_unavailable", False):
                        continue
                    value = _socket(source, i)
                    value.update(self.input(source, context))
                    item["inputs"].append(value)
            return dict(
                source=dict(
                    node=self.emitted[key],
                    socket=socket.identifier,
                    name=socket.name,
                    index=list(node.outputs).index(socket),
                )
            )
        finally:
            self.active.remove(portal)


def _material_settings(material):
    """Material > Settings as Blender's Cycles sync reads them (shader.cpp sync_materials).

    Only the settings the pinned Cycles Shader has; Blender 5.3 has no
    Homogeneous volume setting. Defaults are Blender's and the engine's.
    """
    cycles = getattr(material, "cycles", None)
    return dict(
        displacement_method=getattr(material, "displacement_method", getattr(cycles, "displacement_method", "BUMP")),
        emission_sampling=str(getattr(cycles, "emission_sampling", "AUTO")),
        use_transparent_shadow=bool(getattr(material, "use_transparent_shadow", True)),
        use_bump_map_correction=bool(getattr(cycles, "use_bump_map_correction", True)),
        volume_sampling=str(getattr(cycles, "volume_sampling", "MULTIPLE_IMPORTANCE")),
        volume_interpolation=str(getattr(cycles, "volume_interpolation", "LINEAR")),
        volume_step_rate=float(getattr(cycles, "volume_step_rate", 1.0)),
    )


def _material_record(material, images, color):
    """One owned material record; raises BlendMaterialExportError when unsupported."""
    if material is None:
        # Blender's matching Cycles default_surface is a default Principled
        # BSDF, independent of the Houdini ROP's artist fallback material.
        import bpy

        temporary = bpy.data.materials.new("__cycles_houdini_default_surface__")
        try:
            temporary.use_nodes = True
            shader = next(n for n in temporary.node_tree.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled")
            graph = _Graph(images)
            terminal = graph.output(shader.outputs[0], ())
            return dict(
                key=material_key(None),
                name="Blender default surface",
                nodes=graph.nodes,
                outputs=dict(surface=terminal),
                settings=dict(displacement_method="BUMP"),
                color_management=color,
            )
        finally:
            bpy.data.materials.remove(temporary)
    if material.node_tree is None:
        # BlenderSync uses a Diffuse BSDF with the material RGB and default
        # zero roughness when no node tree exists (viewport metallic and
        # roughness controls do not replace that Cycles fallback).
        inputs = [
            dict(
                index=0,
                identifier="Color",
                name="Color",
                type="RGBA",
                enabled=True,
                value=list(material.diffuse_color),
                value_type="RGBA",
            ),
            dict(
                index=1,
                identifier="Roughness",
                name="Roughness",
                type="VALUE",
                enabled=True,
                value=0.0,
                value_type="VALUE",
            ),
        ]
        node = dict(
            id="n0",
            name=material.name_full,
            type="ShaderNodeBsdfDiffuse",
            properties={},
            inputs=inputs,
            outputs=[dict(index=0, identifier="BSDF", name="BSDF", type="SHADER", enabled=True)],
            location=[0.0, 0.0],
        )
        return dict(
            key=material_key(material),
            name=material.name_full,
            nodes=[node],
            outputs=dict(surface=dict(source=dict(node="n0", socket="BSDF", name="BSDF", index=0))),
            # BlenderSync applies the material's settings without a node tree too.
            settings=dict(_material_settings(material), displacement_method="BUMP"),
            color_management=color,
        )
    outputs = [
        n
        for n in material.node_tree.nodes
        if n.bl_idname == "ShaderNodeOutputMaterial" and n.is_active_output and n.target in ("ALL", "CYCLES")
    ]
    cycles = [n for n in outputs if n.target == "CYCLES"]
    outputs = cycles or outputs
    if len(outputs) != 1:
        raise BlendMaterialExportError("Material needs one active Cycles output: " + material.name_full)
    graph = _Graph(images)
    terminals = {}
    for name in ("Surface", "Volume", "Displacement"):
        socket = outputs[0].inputs.get(name)
        if socket is not None and socket.is_linked:
            terminals[name.lower()] = graph.input(socket, ())
    if not terminals:
        raise BlendMaterialExportError("Material output is unconnected: " + material.name_full)
    settings = _material_settings(material)
    return dict(
        key=material_key(material),
        name=material.name_full,
        nodes=graph.nodes,
        outputs=terminals,
        settings=settings,
        color_management=color,
        # output_layout (Material Output position) and optional frames.
        **graph.layout(outputs[0]),
    )


def export_materials(materials, output_dir, *, tolerant=False, textures="copy"):
    """Return owned material records, exact texture copies and diagnostics.

    Any unsupported material is reported and raises; a partially exported
    bundle must never be mistaken for a faithful import. tolerant (geometry
    references): an unsupported material instead becomes a record carrying
    only `unsupported` (the reason), so its geometry still exports and Houdini
    renders those faces with a fallback material. textures: "copy" (every image
    copied below output_dir) or "in_place" (only packed images are written; files on
    disk are recorded by absolute path, see _Images).
    """
    images = _Images(output_dir, textures)
    import PyOpenColorIO as ocio

    config = ocio.GetCurrentConfig()
    working = config.getColorSpace(ocio.ROLE_SCENE_LINEAR)
    if working is None:
        raise BlendMaterialExportError("Blender OCIO configuration has no scene_linear role")
    color = dict(colorspace=working.getName(), color_interop_id=_color_identity(working))
    records = []
    seen = set()
    for material in materials:
        key = material_key(material)
        if key in seen:
            continue
        seen.add(key)
        try:
            records.append(_material_record(material, images, color))
        except Exception as error:
            if not tolerant:
                raise
            records.append(
                dict(
                    key=key,
                    name=material.name_full if material is not None else "Blender default surface",
                    unsupported=str(error) or type(error).__name__,
                    nodes=[],
                    outputs={},
                    settings=dict(displacement_method="BUMP"),
                    color_management=color,
                )
            )
    return dict(materials=records, dependencies=images.dependencies, diagnostics=[], color_management=color)
