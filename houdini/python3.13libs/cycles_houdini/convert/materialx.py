# SPDX-License-Identifier: Apache-2.0
"""MaterialX Standard Surface and OpenPBR Surface networks -> Cycles Principled BSDF plan.

Pure Python. The input is a snapshot of the shader network (see
``host.snapshot_materialx``)::

    {"name": "/mat/brick", "surface": "<id>", "displacement": "<id>" | None,
     "nodes": {"<id>": {"type": "mtlximage", "name": "...", "parms": {...},
                        "inputs": {"<input>": ["<source id>", "<output>"]}}}}

Upstream nodes with a direct Cycles counterpart are rebuilt; anything else
leaves the surface input at the surface's own value and is reported.
"""

from . import spaces
from .plan import ADDRESS, WHITE, Plan, Socket, absorption, as_color, close, is_constant

SURFACES = ("mtlxstandard_surface", "mtlxopen_pbr_surface")

STANDARD_LABELS = {
    "base": "Base",
    "base_color": "Base Color",
    "diffuse_roughness": "Diffuse Roughness",
    "metalness": "Metalness",
    "specular": "Specular",
    "specular_color": "Specular Color",
    "specular_roughness": "Specular Roughness",
    "specular_IOR": "Specular IOR",
    "specular_anisotropy": "Specular Anisotropy",
    "specular_rotation": "Specular Rotation",
    "transmission": "Transmission",
    "transmission_color": "Transmission Color",
    "transmission_depth": "Transmission Depth",
    "transmission_scatter": "Transmission Scatter",
    "transmission_scatter_anisotropy": "Transmission Scatter Anisotropy",
    "transmission_dispersion": "Transmission Dispersion",
    "transmission_extra_roughness": "Transmission Extra Roughness",
    "subsurface": "Subsurface",
    "subsurface_color": "Subsurface Color",
    "subsurface_radius": "Subsurface Radius",
    "subsurface_scale": "Subsurface Scale",
    "subsurface_anisotropy": "Subsurface Anisotropy",
    "sheen": "Sheen",
    "sheen_color": "Sheen Color",
    "sheen_roughness": "Sheen Roughness",
    "coat": "Coat",
    "coat_color": "Coat Color",
    "coat_roughness": "Coat Roughness",
    "coat_anisotropy": "Coat Anisotropy",
    "coat_rotation": "Coat Rotation",
    "coat_IOR": "Coat IOR",
    "coat_normal": "Coat Normal",
    "coat_affect_color": "Coat Affect Color",
    "coat_affect_roughness": "Coat Affect Roughness",
    "thin_film_thickness": "Thin Film Thickness",
    "thin_film_IOR": "Thin Film IOR",
    "emission": "Emission",
    "emission_color": "Emission Color",
    "opacity": "Opacity",
    "thin_walled": "Thin Walled",
    "normal": "Normal",
    "tangent": "Tangent",
}


def _short(type_name):
    """'mtlximage::2.0' and 'ns::mtlximage' -> 'mtlximage'."""
    parts = str(type_name).split("::")
    return next((part for part in parts if part.startswith("mtlx") or part in ("null", "dot")), parts[0])


def _label(name):
    return STANDARD_LABELS.get(name) or name.replace("_", " ").title()


def _sig_kind(signature, default="float"):
    signature = str(signature or "default")
    if signature == "default":
        return default
    if signature.startswith("color"):
        return "color"
    if signature.startswith("vector"):
        return "vector"
    return "float"


class _Network:
    def __init__(self, graph, plan):
        self.graph, self.plan = graph, plan
        self.nodes = graph.get("nodes", {})
        self.done = {}

    # -- values ------------------------------------------------------------------
    def node(self, ident):
        return self.nodes[ident]

    def parm(self, ident, name, default=None):
        """Value of a MaterialX input parameter, honouring the node's signature suffix."""
        node = self.node(ident)
        parms = node.get("parms", {})
        signature = parms.get("signature", "default")
        for candidate in (name + "_" + str(signature), name):
            if signature != "default" and candidate in parms:
                return parms[candidate]
        return parms.get(name, default)

    def connected(self, ident, name):
        return name in self.node(ident).get("inputs", {})

    def value(self, ident, name, label, default=None):
        """Constant or Socket for an input of ``ident``; unsupported upstream keeps the constant."""
        constant = self.parm(ident, name, default)
        if isinstance(constant, list):
            constant = tuple(constant)
        source = self.node(ident).get("inputs", {}).get(name)
        if not source:
            return constant
        result = self.upstream(source[0], source[1] if len(source) > 1 else "out", label)
        return constant if result is None else result

    def upstream(self, ident, output, label):
        key = (ident, output)
        if key not in self.done:
            self.done[key] = None  # cycles end here
            self.done[key] = self.convert(ident, output, label)
        return self.done[key]

    # -- node conversion ----------------------------------------------------------------
    def convert(self, ident, output, label):
        node = self.node(ident)
        kind = _short(node.get("type", ""))
        handler = getattr(self, "node_" + kind[4:] if kind.startswith("mtlx") else "node_" + kind, None)
        if handler is None:
            self.plan.skip(
                label,
                "'" + node.get("name", ident) + "' (" + kind + ") has no Cycles counterpart; the input keeps its value",
            )
            return None
        return handler(ident, output, label)

    def node_null(self, ident, output, label):
        inputs = self.node(ident).get("inputs", {})
        if not inputs:
            return None
        source = next(iter(inputs.values()))
        return self.upstream(source[0], source[1] if len(source) > 1 else "out", label)

    node_dot = node_null

    def node_constant(self, ident, output, label):
        value = self.parm(ident, "value", 0.0)
        return tuple(value) if isinstance(value, list) else value

    def coordinates(self, ident, label):
        """Texture coordinates of an image node: None (default UV) or a vector Socket."""
        source = self.node(ident).get("inputs", {}).get("texcoord")
        if not source:
            return None
        upstream = self.node(source[0])
        kind = _short(upstream.get("type", ""))
        if kind == "mtlxtexcoord":
            index = int(upstream.get("parms", {}).get("index", 0))
            if index == 0:
                return None
            key = self.plan.add("uvmap", "uv" + str(index + 1), attribute="uv" + str(index + 1))
            return Socket(key, "UV", "vector")
        if kind == "mtlxgeompropvalue":
            attribute = str(upstream.get("parms", {}).get("geomprop", ""))
            if attribute in ("", "uv", "st"):
                return None
            key = self.plan.add("uvmap", attribute or "uv_map", attribute=attribute)
            return Socket(key, "UV", "vector")
        self.plan.skip(
            label, "texture coordinates from '" + upstream.get("name", source[0]) + "' are not carried; UV is used"
        )
        return None

    def image_node(self, ident, label, tiled):
        node = self.node(ident)
        parms = node.get("parms", {})
        signature = str(parms.get("signature", "color3"))
        color = signature.startswith("color")
        filename = parms.get("file", "")
        if not filename:
            self.plan.skip(label, "'" + node.get("name", ident) + "' has no file")
            return None
        space = spaces.materialx(parms.get("filecolorspace", ""), color)
        vector = self.coordinates(ident, label)
        if tiled:
            tiling = tuple(parms.get("uvtiling", (1.0, 1.0)))
            offset = tuple(parms.get("uvoffset", (0.0, 0.0)))
            image_size = tuple(parms.get("realworldimagesize", (1.0, 1.0)))
            tile_size = tuple(parms.get("realworldtilesize", (1.0, 1.0)))
            factor = [t / i if i else 1.0 for t, i in zip(tile_size, image_size)]
            scale = (tiling[0] * factor[0], tiling[1] * factor[1], 1.0)
            location = (-offset[0] * factor[0], -offset[1] * factor[1], 0.0)
            if not (close(scale, (1.0, 1.0, 1.0)) and close(location, (0.0, 0.0, 0.0))):
                if vector is None:
                    coordinates = self.plan.add("texture_coordinate", "texture_coordinates")
                    vector = Socket(coordinates, "UV", "vector")
                mapping = self.plan.add(
                    "mapping",
                    node.get("name", "tiling") + "_tiling",
                    mapping_type="point",
                    location=location,
                    scale=scale,
                )
                self.plan.set(mapping, "vector", vector)
                vector = Socket(mapping, "vector", "vector")
            extension = "periodic"
        else:
            u, v = parms.get("uaddressmode", "periodic"), parms.get("vaddressmode", "periodic")
            if u != v:
                self.plan.approx(label, "different U and V address modes; Cycles uses U's (" + str(u) + ") for both")
            extension = ADDRESS.get(u, "periodic")
        interpolation = {"closest": "closest", "linear": "linear", "cubic": "cubic"}.get(
            parms.get("filtertype", "linear"), "linear"
        )
        if parms.get("layer", ""):
            self.plan.skip(label, "image layer '" + str(parms["layer"]) + "' is not carried")
        if parms.get("framerange", ""):
            self.plan.skip(label, "frame range and frame offset are not carried; use $F in the file name")
        defaults = [value for key, value in parms.items() if key.startswith("default")]
        if any(any(v for v in (d if isinstance(d, (list, tuple)) else (d,))) for d in defaults):
            self.plan.skip(label, "the default colour for missing files is not carried (Cycles shows magenta)")
        key = self.plan.image(filename, space, extension, interpolation, vector, node.get("name", "texture"))
        return key, signature

    def _image(self, ident, output, label, tiled):
        made = self.image_node(ident, label, tiled)
        if made is None:
            return None
        key, signature = made
        kind = _sig_kind(signature, "color")
        if kind == "float":
            # MaterialX reads the first channel of a single-value image.
            return self.plan.channel(Socket(key, "color"), "r", self.node(ident).get("name", "image") + "_r")
        return Socket(key, "color", "vector" if kind == "vector" else "color")

    def node_image(self, ident, output, label):
        return self._image(ident, output, label, False)

    def node_tiledimage(self, ident, output, label):
        return self._image(ident, output, label, True)

    def node_normalmap(self, ident, output, label):
        color = self.value(ident, "in", label, (0.5, 0.5, 1.0))
        scale = self.parm(ident, "scale", 1.0)
        if isinstance(scale, (list, tuple)):
            scale = scale[0]
        # Base explicitly: the node's own default is Blender's (Displaced).
        key = self.plan.add(
            "normal_map",
            self.node(ident).get("name", "normal_map"),
            space="tangent",
            strength=float(scale),
            base="original",
        )
        self.plan.set(key, "color", color)
        if float(scale) != 1.0:
            self.plan.approx(label, "MaterialX scales the normal's slope; Cycles Strength blends towards the map")
        for name in ("normal", "tangent", "bitangent"):
            if self.connected(ident, name):
                self.plan.skip(label, "the normal map's " + name + " input is not carried")
        return Socket(key, "normal", "vector")

    def node_bump(self, ident, output, label):
        height = self.value(ident, "height", label, 0.0)
        key = self.plan.add(
            "bump", self.node(ident).get("name", "bump"), strength=1.0, distance=float(self.parm(ident, "scale", 1.0))
        )
        self.plan.set(key, "height", height)
        normal = self.value(ident, "normal", label, None)
        if isinstance(normal, Socket):
            self.plan.set(key, "normal", normal)
        return Socket(key, "normal", "vector")

    def _binary(self, ident, label, operation, default2):
        a = self.value(ident, "in1", label, 0.0)
        b = self.value(ident, "in2", label, default2)
        plan, name = self.plan, self.node(ident).get("name", operation)
        return getattr(plan, operation)(a, b, name)

    def node_multiply(self, ident, output, label):
        return self._binary(ident, label, "multiply", 1.0)

    def node_add(self, ident, output, label):
        return self._binary(ident, label, "add_values", 0.0)

    def node_subtract(self, ident, output, label):
        return self._binary(ident, label, "subtract", 0.0)

    def node_divide(self, ident, output, label):
        return self._binary(ident, label, "divide", 1.0)

    def node_power(self, ident, output, label):
        return self._binary(ident, label, "power", 1.0)

    def node_mix(self, ident, output, label):
        fg = self.value(ident, "fg", label, 0.0)
        bg = self.value(ident, "bg", label, 0.0)
        fac = self.value(ident, "mix", label, 0.0)
        if isinstance(fac, tuple):
            self.plan.approx(label, "a per-channel mix amount is averaged")
            fac = sum(fac) / len(fac)
        return self.plan.lerp(bg, fg, fac, self.node(ident).get("name", "mix"))

    def node_invert(self, ident, output, label):
        value = self.value(ident, "in", label, 0.0)
        amount = self.value(ident, "amount", label, 1.0)
        return self.plan.subtract(amount, value, self.node(ident).get("name", "invert"))

    def node_clamp(self, ident, output, label):
        value = self.value(ident, "in", label, 0.0)
        low, high = self.value(ident, "low", label, 0.0), self.value(ident, "high", label, 1.0)
        if is_constant(value):
            if isinstance(value, tuple):
                return tuple(min(max(v, lo), hi) for v, lo, hi in zip(value, as_color(low), as_color(high)))
            return min(max(value, low), high)
        name = self.node(ident).get("name", "clamp")
        if value.kind == "float" and not isinstance(low, tuple) and not isinstance(high, tuple):
            key = self.plan.add("clamp", name, clamp_type="minmax")
            self.plan.set(key, "value", value)
            self.plan.set(key, "min", low)
            self.plan.set(key, "max", high)
            return Socket(key, "result", "float")
        lower = self.plan.add("vector_math", name + "_low", math_type="maximum")
        self.plan.set(lower, "vector1", value)
        self.plan.set(lower, "vector2", as_color(low) if is_constant(low) else low)
        upper = self.plan.add("vector_math", name + "_high", math_type="minimum")
        self.plan.set(upper, "vector1", Socket(lower, "vector", value.kind))
        self.plan.set(upper, "vector2", as_color(high) if is_constant(high) else high)
        return Socket(upper, "vector", value.kind)

    def _remap(self, value, inlow, inhigh, outlow, outhigh, clamp, name, label):
        if is_constant(value) and all(
            is_constant(v) and not isinstance(v, tuple) for v in (inlow, inhigh, outlow, outhigh)
        ):
            if isinstance(value, tuple):
                return tuple(_remap_number(v, inlow, inhigh, outlow, outhigh, clamp) for v in value)
            return _remap_number(value, inlow, inhigh, outlow, outhigh, clamp)
        if any(
            isinstance(v, (tuple, Socket)) and getattr(v, "kind", "color") != "float"
            for v in (inlow, inhigh, outlow, outhigh)
        ):
            self.plan.skip(label, "per-channel remap ranges are not carried")
            return None
        if getattr(value, "kind", "float") == "float":
            key = self.plan.add("map_range", name, range_type="linear", clamp=int(bool(clamp)))
            for input, item in (
                ("value", value),
                ("from_min", inlow),
                ("from_max", inhigh),
                ("to_min", outlow),
                ("to_max", outhigh),
            ):
                self.plan.set(key, input, item)
            return Socket(key, "result", "float")
        # Vector Map Range names its clamp toggle use_clamp (Map Range: clamp).
        key = self.plan.add("vector_map_range", name, range_type="linear", use_clamp=int(bool(clamp)))
        for input, item in (
            ("vector", value),
            ("from_min", inlow),
            ("from_max", inhigh),
            ("to_min", outlow),
            ("to_max", outhigh),
        ):
            self.plan.set(key, input, as_color(item) if is_constant(item) else item)
        return Socket(key, "vector", value.kind)

    def node_remap(self, ident, output, label):
        get = lambda name, default: self.value(ident, name, label, default)  # noqa: E731
        return self._remap(
            get("in", 0.0),
            get("inlow", 0.0),
            get("inhigh", 1.0),
            get("outlow", 0.0),
            get("outhigh", 1.0),
            False,
            self.node(ident).get("name", "remap"),
            label,
        )

    def node_range(self, ident, output, label):
        get = lambda name, default: self.value(ident, name, label, default)  # noqa: E731
        name = self.node(ident).get("name", "range")
        gamma = get("gamma", 1.0)
        clamp = bool(self.parm(ident, "doclamp", 0))
        if isinstance(gamma, tuple) and len(set(gamma)) == 1:
            gamma = gamma[0]
        if isinstance(gamma, (tuple, Socket)):
            self.plan.skip(label, "a per-channel or connected range gamma is not carried")
            return None
        normalized = self._remap(
            get("in", 0.0), get("inlow", 0.0), get("inhigh", 1.0), 0.0, 1.0, False, name + "_in", label
        )
        if normalized is None:
            return None
        if gamma != 1.0:
            normalized = self.plan.power(normalized, 1.0 / gamma if gamma else 1.0, name + "_gamma")
        return self._remap(normalized, 0.0, 1.0, get("outlow", 0.0), get("outhigh", 1.0), clamp, name, label)

    def node_contrast(self, ident, output, label):
        value = self.value(ident, "in", label, 0.0)
        amount, pivot = self.value(ident, "amount", label, 1.0), self.value(ident, "pivot", label, 0.5)
        if not is_constant(amount) or not is_constant(pivot) or isinstance(amount, tuple) or isinstance(pivot, tuple):
            self.plan.skip(label, "connected or per-channel contrast is not carried")
            return None
        return self._contrast(value, amount, pivot, self.node(ident).get("name", "contrast"))

    def _contrast(self, value, amount, pivot, name):
        if amount == 1.0:
            return value
        if is_constant(value):
            if isinstance(value, tuple):
                return tuple((v - pivot) * amount + pivot for v in value)
            return (value - pivot) * amount + pivot
        if value.kind == "float":
            scaled = self.plan.multiply(self.plan.subtract(value, pivot, name + "_offset"), amount, name + "_amount")
            return self.plan.add_values(scaled, pivot, name)
        # Cycles Brightness/Contrast: (1 + C) * x + B - C / 2, so C = amount - 1, B = (amount - 1) * (0.5 - pivot).
        key = self.plan.add(
            "brightness_contrast", name, contrast=float(amount - 1.0), bright=float((amount - 1.0) * (0.5 - pivot))
        )
        self.plan.set(key, "color", value)
        self.plan.approx(name, "Cycles Brightness/Contrast clips negative results to zero")
        return Socket(key, "color", "color")

    def _hsv(self, value, hue, saturation, brightness, name):
        if hue == 0.0 and saturation == 1.0 and brightness == 1.0:
            return value
        key = self.plan.add("hsv", name, hue=0.5 + hue, saturation=saturation, value=brightness, fac=1.0)
        self.plan.set(key, "color", value if not is_constant(value) else as_color(value))
        return Socket(key, "color", "color")

    def node_hsvadjust(self, ident, output, label):
        value = self.value(ident, "in", label, (0.0, 0.0, 0.0))
        amount = self.value(ident, "amount", label, (0.0, 1.0, 1.0))
        if not is_constant(amount):
            self.plan.skip(label, "a connected HSV amount is not carried")
            return value
        return self._hsv(value, *as_color(amount), self.node(ident).get("name", "hsv"))

    def node_colorcorrect(self, ident, output, label):
        """MaterialX order: hue, saturation, gamma, lift, gain, contrast, exposure."""
        name = self.node(ident).get("name", "colorcorrect")
        value = self.value(ident, "in", label, WHITE)
        settings = {}
        for key, default in (
            ("hue", 0.0),
            ("saturation", 1.0),
            ("gamma", 1.0),
            ("lift", 0.0),
            ("gain", 1.0),
            ("contrast", 1.0),
            ("contrastpivot", 0.5),
            ("exposure", 0.0),
        ):
            item = self.value(ident, key, label, default)
            if not is_constant(item):
                self.plan.skip(label, "a connected colour-correct " + key + " is not carried")
                item = self.parm(ident, key, default)
            settings[key] = float(item)
        value = self._hsv(value, settings["hue"], 1.0, 1.0, name + "_hue")
        if settings["saturation"] != 1.0:
            self.plan.skip(label, "colour-correct saturation (luma based) is not carried")
        if settings["gamma"] != 1.0:
            value = self.plan.power(value, 1.0 / settings["gamma"] if settings["gamma"] else 1.0, name + "_gamma")
        if settings["lift"]:
            value = self.plan.lerp(value, WHITE, settings["lift"], name + "_lift")
        value = self.plan.multiply(value, (settings["gain"],) * 3, name + "_gain")
        value = self._contrast(value, settings["contrast"], settings["contrastpivot"], name + "_contrast")
        value = self.plan.multiply(value, (2.0 ** settings["exposure"],) * 3, name + "_exposure")
        return value

    def node_separate3c(self, ident, output, label):
        value = self.value(ident, "in", label, (0.0, 0.0, 0.0))
        channel = {"outr": "r", "outg": "g", "outb": "b", "outx": "r", "outy": "g", "outz": "b"}.get(output, "r")
        return self.plan.channel(value, channel, self.node(ident).get("name", "separate"))

    node_separate3v = node_separate3c

    def node_extract(self, ident, output, label):
        index = int(self.parm(ident, "index", 0))
        source = self.node(ident).get("inputs", {}).get("in")
        if index == 3 and source and _short(self.node(source[0]).get("type", "")) in ("mtlximage", "mtlxtiledimage"):
            made = self.image_node(source[0], label, _short(self.node(source[0])["type"]) == "mtlxtiledimage")
            return None if made is None else Socket(made[0], "alpha", "float")
        if index > 2:
            self.plan.skip(label, "only the first three channels or an image's alpha can be extracted")
            return None
        value = self.value(ident, "in", label, (0.0, 0.0, 0.0))
        return self.plan.channel(value, "rgb"[index], self.node(ident).get("name", "extract"))

    def node_luminance(self, ident, output, label):
        value = self.value(ident, "in", label, (0.0, 0.0, 0.0))
        result = self.plan.to_float(value, self.node(ident).get("name", "luminance"))
        if not is_constant(result):
            self.plan.approx(label, "Cycles RGB to BW uses the render space's luminance weights")
        return result if is_constant(result) else Socket(result.node, result.output, "float")


def _remap_number(value, inlow, inhigh, outlow, outhigh, clamp):
    t = (value - inlow) / (inhigh - inlow) if inhigh != inlow else 0.0
    result = outlow + (outhigh - outlow) * t
    if clamp:
        result = min(max(result, min(outlow, outhigh)), max(outlow, outhigh))
    return result


class _Surface:
    """Shared Principled BSDF assembly for Standard Surface and OpenPBR."""

    def __init__(self, graph, kind):
        self.graph = graph
        self.kind = kind
        self.plan = Plan(source=graph.get("name", ""))
        self.net = _Network(graph, self.plan)
        self.ident = graph["surface"]
        self.p = {}

    def v(self, name, default=None):
        label = _label(name)
        return self.net.value(self.ident, name, label, default)

    def const(self, name, default=0.0):
        value = self.net.parm(self.ident, name, default)
        return tuple(value) if isinstance(value, list) else value

    def report_nondefault(self, name, default, reason):
        value = self.v(name, default)
        if not is_constant(value) or not close(value, default):
            self.plan.skip(_label(name), reason)

    def set(self, input, value):
        self.plan.set("surface", input, value)

    def tangent_from_uv(self):
        key = self.plan.add("tangent", "uv_tangent", direction_type="uv_map")
        self.set("tangent", Socket(key, "tangent", "vector"))

    def base_and_layers(self, base, metal, transmission, transmission_color, depth, subsurface, subsurface_color):
        """Cycles tints transmission and subsurface with Base Color; carry exact cases, report the rest."""
        plan = self.plan
        final = base
        transmitting = not (is_constant(transmission) and transmission == 0.0)
        if transmitting:
            plan.set("surface", "transmission_weight", transmission)
            deep = is_constant(depth) and depth > 0.0
            if not is_constant(depth):
                plan.skip("Transmission Depth", "a connected depth is not carried")
            tint = WHITE if deep else transmission_color
            if deep:
                if not is_constant(transmission_color):
                    plan.skip("Transmission Color", "a connected colour cannot drive Cycles absorption")
                elif not close(transmission_color, WHITE):
                    volume = absorption(transmission_color, depth)
                    if volume is not None:
                        key = plan.add(
                            "absorption_volume", "transmission_absorption", color=volume[0], density=volume[1]
                        )
                        plan.set("output", "volume", Socket(key, "volume", "closure"))
            full = is_constant(transmission) and transmission == 1.0 and is_constant(metal) and metal == 0.0
            if full:
                final = tint
            elif not close(tint, base):
                plan.approx(
                    "Transmission Color", "Cycles colours transmission with Base Color; the Base Color is used for both"
                )
        if not (is_constant(subsurface) and subsurface == 0.0):
            plan.set("surface", "subsurface_weight", subsurface)
            plan.set("surface", "subsurface_method", "random_walk")
            exclusive = (
                is_constant(subsurface)
                and subsurface == 1.0
                and is_constant(metal)
                and metal == 0.0
                and not transmitting
            )
            if exclusive:
                final = subsurface_color
            elif not close(subsurface_color, base):
                plan.approx("Subsurface Color", "Cycles scatters subsurface light with Base Color")
        plan.set("surface", "base_color", final)

    def normals(self, normal_name, coat_normal_name, tangent_names):
        normal = self.v(normal_name, None)
        if isinstance(normal, Socket):
            self.set("normal", normal)
        coat_normal = self.v(coat_normal_name, None)
        if isinstance(coat_normal, Socket):
            self.set("coat_normal", coat_normal)
        for name in tangent_names:
            if self.net.connected(self.ident, name):
                self.plan.skip(_label(name), "a connected tangent is not carried; the UV direction is used")

    def opacity(self, value):
        if is_constant(value):
            if isinstance(value, tuple):
                color = as_color(value)
                if not close(color, (color[0],) * 3):
                    self.plan.approx("Opacity", "Cycles opacity is one value; the colour's average is used")
                value = sum(color) / 3.0
        else:
            value = self.plan.to_float(value, "opacity")
        self.set("alpha", value)

    def displacement(self):
        ident = self.graph.get("displacement")
        if not ident:
            return
        node = self.net.node(ident)
        kind = _short(node.get("type", ""))
        if kind != "mtlxdisplacement":
            self.plan.skip("Displacement", "'" + node.get("name", ident) + "' (" + kind + ") is not carried")
            return
        signature = node.get("parms", {}).get("signature", "default")
        scale = float(self.net.parm(ident, "scale", 1.0))
        if str(signature).startswith("vector"):
            self.plan.skip("Displacement", "vector displacement from MaterialX is not carried")
            return
        height = self.net.value(ident, "displacement", "Displacement", 0.0)
        if is_constant(height) and height == 0.0:
            return
        key = self.plan.add("displacement", "displacement", space="object", midlevel=0.0, scale=scale)
        self.plan.set(key, "height", height)
        self.plan.set("output", "displacement", Socket(key, "displacement", "vector"))
        self.plan.builder["cycles_displacement_method"] = "both"
        self.plan.approx(
            "Displacement",
            "Cycles moves existing vertices only; turn on Cycles subdivision on the object for fine detail",
        )


def _standard(graph):
    s = _Surface(graph, "standard_surface")
    plan = s.plan
    base = plan.multiply(s.v("base", 1.0), s.v("base_color", (0.8, 0.8, 0.8)), "base_weight")
    metal = s.v("metalness", 0.0)
    s.set("metallic", metal)
    s.set("diffuse_roughness", s.v("diffuse_roughness", 0.0))
    s.set("specular_ior_level", plan.multiply(s.v("specular", 1.0), 0.5, "specular_level"))
    s.set("specular_tint", s.v("specular_color", WHITE))
    s.set("roughness", s.v("specular_roughness", 0.2))
    s.set("ior", s.v("specular_IOR", 1.5))
    anisotropy = s.v("specular_anisotropy", 0.0)
    if not (is_constant(anisotropy) and anisotropy == 0.0):
        s.set("anisotropic", anisotropy)
        s.set("anisotropic_rotation", s.v("specular_rotation", 0.0))
        if not s.net.connected(s.ident, "tangent"):
            s.tangent_from_uv()
    s.base_and_layers(
        base,
        metal,
        s.v("transmission", 0.0),
        s.v("transmission_color", WHITE),
        s.v("transmission_depth", 0.0),
        s.v("subsurface", 0.0),
        s.v("subsurface_color", WHITE),
    )
    s.report_nondefault("transmission_scatter", (0.0, 0.0, 0.0), "Cycles Principled has no interior scattering")
    s.report_nondefault("transmission_scatter_anisotropy", 0.0, "Cycles Principled has no interior scattering")
    s.report_nondefault("transmission_dispersion", 0.0, "not carried; OpenPBR's Abbe number maps, this value does not")
    s.report_nondefault("transmission_extra_roughness", 0.0, "Cycles uses one roughness for reflection and refraction")
    if not (
        is_constant(plan.value("surface", "subsurface_weight", 0.0))
        and plan.value("surface", "subsurface_weight", 0.0) == 0.0
    ):
        s.set("subsurface_radius", s.v("subsurface_radius", WHITE))
        s.set("subsurface_scale", s.v("subsurface_scale", 1.0))
        s.set("subsurface_anisotropy", s.v("subsurface_anisotropy", 0.0))
    s.set("sheen_weight", s.v("sheen", 0.0))
    s.set("sheen_tint", s.v("sheen_color", WHITE))
    s.set("sheen_roughness", s.v("sheen_roughness", 0.3))
    s.set("coat_weight", s.v("coat", 0.0))
    s.set("coat_tint", s.v("coat_color", WHITE))
    s.set("coat_roughness", s.v("coat_roughness", 0.1))
    s.set("coat_ior", s.v("coat_IOR", 1.5))
    s.report_nondefault("coat_anisotropy", 0.0, "Cycles coat is isotropic")
    s.report_nondefault(
        "coat_affect_color", 0.0, "Cycles darkens under the coat physically; this control is not carried"
    )
    s.report_nondefault("coat_affect_roughness", 0.0, "not carried")
    s.set("thin_film_thickness", s.v("thin_film_thickness", 0.0))  # nanometres in both
    s.set("thin_film_ior", s.v("thin_film_IOR", 1.5))
    s.set("emission_strength", s.v("emission", 0.0))
    s.set("emission_color", s.v("emission_color", WHITE))
    s.opacity(s.v("opacity", WHITE))
    thin = s.v("thin_walled", 0)
    s.set("thin_wall", int(thin) if is_constant(thin) else thin)
    s.normals("normal", "coat_normal", ("tangent",))
    s.displacement()
    return plan


def _open_pbr(graph):
    s = _Surface(graph, "open_pbr_surface")
    plan = s.plan
    base = plan.multiply(s.v("base_weight", 1.0), s.v("base_color", (0.8, 0.8, 0.8)), "base_weight")
    metal = s.v("base_metalness", 0.0)
    s.set("metallic", metal)
    s.set("diffuse_roughness", s.v("base_diffuse_roughness", 0.0))
    s.set("specular_ior_level", plan.multiply(s.v("specular_weight", 1.0), 0.5, "specular_level"))
    s.set("specular_tint", s.v("specular_color", WHITE))
    s.set("roughness", s.v("specular_roughness", 0.3))
    s.set("ior", s.v("specular_ior", 1.5))
    anisotropy = s.v("specular_roughness_anisotropy", 0.0)
    if not (is_constant(anisotropy) and anisotropy == 0.0):
        s.set("anisotropic", anisotropy)
        plan.approx("Specular Anisotropy", "OpenPBR and Cycles stretch the highlight with different curves")
        if not s.net.connected(s.ident, "geometry_tangent"):
            s.tangent_from_uv()
    s.base_and_layers(
        base,
        metal,
        s.v("transmission_weight", 0.0),
        s.v("transmission_color", WHITE),
        s.v("transmission_depth", 0.0),
        s.v("subsurface_weight", 0.0),
        s.v("subsurface_color", (0.8, 0.8, 0.8)),
    )
    if not (
        is_constant(plan.value("surface", "transmission_weight", 0.0))
        and plan.value("surface", "transmission_weight", 0.0) == 0.0
    ):
        s.set("transmission_dispersion_scale", s.v("transmission_dispersion_scale", 0.0))
        s.set("transmission_dispersion_abbe_number", s.v("transmission_dispersion_abbe_number", 20.0))
    s.report_nondefault("transmission_scatter", (0.0, 0.0, 0.0), "Cycles Principled has no interior scattering")
    s.report_nondefault("transmission_scatter_anisotropy", 0.0, "Cycles Principled has no interior scattering")
    if not (
        is_constant(plan.value("surface", "subsurface_weight", 0.0))
        and plan.value("surface", "subsurface_weight", 0.0) == 0.0
    ):
        s.set("subsurface_radius", s.v("subsurface_radius_scale", (1.0, 0.5, 0.25)))
        s.set("subsurface_scale", s.v("subsurface_radius", 1.0))
        s.set("subsurface_anisotropy", s.v("subsurface_scatter_anisotropy", 0.0))
    s.set("sheen_weight", s.v("fuzz_weight", 0.0))
    s.set("sheen_tint", s.v("fuzz_color", WHITE))
    s.set("sheen_roughness", s.v("fuzz_roughness", 0.5))
    s.set("coat_weight", s.v("coat_weight", 0.0))
    s.set("coat_tint", s.v("coat_color", WHITE))
    s.set("coat_roughness", s.v("coat_roughness", 0.0))
    s.set("coat_ior", s.v("coat_ior", 1.6))
    s.report_nondefault("coat_roughness_anisotropy", 0.0, "Cycles coat is isotropic")
    s.report_nondefault("coat_darkening", 1.0, "Cycles always darkens under the coat (darkening 1)")
    film = s.v("thin_film_weight", 0.0)
    if not (is_constant(film) and film == 0.0):
        thickness = s.v("thin_film_thickness", 0.5)
        # OpenPBR thickness is in micrometres, Cycles in nanometres.
        s.set("thin_film_thickness", plan.multiply(thickness, 1000.0, "thin_film_nanometres"))
        s.set("thin_film_ior", s.v("thin_film_ior", 1.4))
        if not (is_constant(film) and film == 1.0):
            plan.approx("Thin Film Weight", "Cycles thin film has no weight; the film is applied fully")
    s.set("emission_strength", s.v("emission_luminance", 0.0))
    s.set("emission_color", s.v("emission_color", WHITE))
    s.opacity(s.v("geometry_opacity", 1.0))
    thin = s.v("geometry_thin_walled", 0)
    s.set("thin_wall", int(thin) if is_constant(thin) else thin)
    s.normals("geometry_normal", "geometry_coat_normal", ("geometry_tangent", "geometry_coat_tangent"))
    s.displacement()
    return plan


def plan_materialx(graph):
    """Plan for a MaterialX surface network snapshot (Standard Surface or OpenPBR)."""
    kind = _short(graph["nodes"][graph["surface"]].get("type", ""))
    if kind == "mtlxstandard_surface":
        return _standard(graph)
    if kind == "mtlxopen_pbr_surface":
        return _open_pbr(graph)
    raise ValueError("Not a MaterialX Standard Surface or OpenPBR Surface: " + kind)
