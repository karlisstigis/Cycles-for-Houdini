# SPDX-License-Identifier: Apache-2.0
"""A Cycles material described as plain data, before any Houdini node exists.

The mappings in ``principled`` and ``materialx`` fill a :class:`Plan`; the host
module turns it into ``cycles::*`` nodes. Constant values are folded here, so a
material without textures becomes one Principled BSDF with its values set and
no helper nodes.
"""

from dataclasses import dataclass, field
import math

WHITE = (1.0, 1.0, 1.0)
BLACK = (0.0, 0.0, 0.0)

# Houdini wrap / MaterialX address modes -> Cycles Image Texture extension.
WRAP = {"repeat": "periodic", "streak": "clamp", "decal": "black"}
ADDRESS = {"periodic": "periodic", "clamp": "clamp", "constant": "black", "mirror": "mirror"}


@dataclass(frozen=True)
class Socket:
    """An output of a planned node: ``kind`` is ``float``, ``color`` or ``vector``."""

    node: str
    output: str
    kind: str = "color"


@dataclass(frozen=True)
class ColorSpace:
    """Cycles Image Texture input space: the first name the active configuration offers wins."""

    candidates: tuple

    @property
    def name(self):
        return self.candidates[0]


@dataclass
class PlanNode:
    type: str  # Cycles engine type, e.g. "image_texture" (created as cycles::<type>::1)
    name: str
    parms: dict = field(default_factory=dict)


@dataclass
class Plan:
    source: str = ""
    nodes: dict = field(default_factory=dict)  # key -> PlanNode, in creation order
    links: list = field(default_factory=list)  # (node key, input name, Socket)
    builder: dict = field(default_factory=dict)  # parameters of the material builder itself
    not_carried: list = field(default_factory=list)  # (label, reason)
    approximated: list = field(default_factory=list)  # (label, reason)
    _cache: dict = field(default_factory=dict, repr=False)

    def __post_init__(self):
        if not self.nodes:
            self.nodes["surface"] = PlanNode("principled_bsdf", "surface_shader")
            self.nodes["output"] = PlanNode("surface_output", "surface_output")

    # -- bookkeeping -------------------------------------------------------------
    def skip(self, label, reason):
        if (label, reason) not in self.not_carried:
            self.not_carried.append((label, reason))

    def approx(self, label, reason):
        if (label, reason) not in self.approximated:
            self.approximated.append((label, reason))

    def message(self):
        """One text per material: what was not carried and what is only approximated."""
        if not self.not_carried and not self.approximated:
            return ""
        lines = ["Converted from " + self.source + "." if self.source else "Converted."]
        if self.not_carried:
            lines.append("Not carried:")
            lines += ["- " + label + ": " + reason for label, reason in self.not_carried]
        if self.approximated:
            lines.append("Approximated:")
            lines += ["- " + label + ": " + reason for label, reason in self.approximated]
        return "\n".join(lines)

    def add(self, type, name, **parms):
        key, number = name, 2
        while key in self.nodes:
            key, number = f"{name}{number}", number + 1
        self.nodes[key] = PlanNode(type, key, dict(parms))
        return key

    def set(self, key, input, value):
        """Connect a Socket or set a constant on a node input."""
        if value is None:
            return
        if isinstance(value, Socket):
            self.links = [link for link in self.links if link[:2] != (key, input)]
            self.links.append((key, input, value))
            self.nodes[key].parms.pop(input, None)
        else:
            self.nodes[key].parms[input] = _clean(value)

    def value(self, key, input, default=None):
        for node, name, socket in self.links:
            if (node, name) == (key, input):
                return socket
        return self.nodes[key].parms.get(input, default)

    # -- folded operations ---------------------------------------------------------
    def multiply(self, a, b, name="multiply"):
        """a * b for floats or colours; a float times a colour gives a colour."""
        if _is_one(a):
            return b
        if _is_one(b):
            return a
        if _const(a) and _const(b):
            if isinstance(a, tuple) or isinstance(b, tuple):
                a3, b3 = _color(a), _color(b)
                return tuple(x * y for x, y in zip(a3, b3))
            return a * b
        if _kind(a) == "float" and _kind(b) == "float":
            return self._math("multiply", a, b, name)
        return self._mix("multiply", a, b, 1.0, name)

    def add_values(self, a, b, name="add"):
        if _const(a) and _const(b):
            if isinstance(a, tuple) or isinstance(b, tuple):
                return tuple(x + y for x, y in zip(_color(a), _color(b)))
            return a + b
        if _kind(a) == "float" and _kind(b) == "float":
            return self._math("add", a, b, name)
        return self._mix("add", a, b, 1.0, name)

    def subtract(self, a, b, name="subtract"):
        if _const(a) and _const(b):
            if isinstance(a, tuple) or isinstance(b, tuple):
                return tuple(x - y for x, y in zip(_color(a), _color(b)))
            return a - b
        if _kind(a) == "float" and _kind(b) == "float":
            return self._math("subtract", a, b, name)
        return self._mix("subtract", a, b, 1.0, name)

    def divide(self, a, b, name="divide"):
        if _is_one(b):
            return a
        if _const(a) and _const(b):
            if isinstance(a, tuple) or isinstance(b, tuple):
                return tuple(x / y if y else 0.0 for x, y in zip(_color(a), _color(b)))
            return a / b if b else 0.0
        if _kind(a) == "float" and _kind(b) == "float":
            return self._math("divide", a, b, name)
        return self._mix("divide", a, b, 1.0, name)

    def power(self, a, b, name="power"):
        if _is_one(b):
            return a
        if _const(a) and _const(b) and not isinstance(b, tuple):
            if isinstance(a, tuple):
                return tuple(math.copysign(abs(x) ** b, x) for x in a)
            return math.copysign(abs(a) ** b, a)
        if _kind(a) == "float" and not isinstance(b, tuple) and _kind(b) == "float":
            return self._math("power", a, b, name)
        if not isinstance(b, Socket):
            key = self.add("gamma", name, gamma=float(b))
            self.set(key, "color", a)
            return Socket(key, "color", "color")
        key = self.add("vector_math", name, math_type="power")
        self.set(key, "vector1", a)
        self.set(key, "vector2", b)
        return Socket(key, "vector", "color")

    def lerp(self, a, b, fac, name="mix"):
        """a + (b - a) * fac (Cycles Mix Color with blend 'mix')."""
        if _const(fac) and not isinstance(fac, tuple):
            if fac == 0:
                return a
            if fac == 1:
                return b
        if _const(a) and _const(b) and _const(fac) and not isinstance(fac, tuple):
            if isinstance(a, tuple) or isinstance(b, tuple):
                return tuple(x + (y - x) * fac for x, y in zip(_color(a), _color(b)))
            return a + (b - a) * fac
        if _kind(a) == "float" and _kind(b) == "float":
            difference = self.subtract(b, a, name + "_difference")
            key = self.add("math", name, math_type="multiply_add")
            self.set(key, "value1", difference)
            self.set(key, "value2", fac)
            self.set(key, "value3", a)
            return Socket(key, "value", "float")
        return self._mix("mix", a, b, fac, name)

    def to_float(self, value, name="luminance"):
        """A colour used where Cycles wants one value."""
        if not isinstance(value, Socket):
            return sum(_color(value)) / 3.0 if isinstance(value, tuple) else value
        if value.kind == "float":
            return value
        key = self.add("rgb_to_bw", name)
        self.set(key, "color", value)
        return Socket(key, "val", "float")

    def channel(self, value, which, name="channel"):
        """'luminance', 'r', 'g' or 'b' of a colour signal."""
        if which == "luminance":
            return self.to_float(value, name)
        if not isinstance(value, Socket):
            return _color(value)["rgb".index(which)]
        if value.kind == "float":
            return value
        cache = ("separate", value)
        if cache not in self._cache:
            key = self.add("separate_color", name, color_type="rgb")
            self.set(key, "color", value)
            self._cache[cache] = key
        return Socket(self._cache[cache], which, "float")

    def attribute(self, attribute, name):
        cache = ("attribute", attribute)
        if cache not in self._cache:
            self._cache[cache] = self.add("attribute", name, attribute=attribute, missing=WHITE, missing_alpha=1.0)
        return self._cache[cache]

    def image(self, filename, colorspace, extension="periodic", interpolation="linear", vector=None, name="texture"):
        """One Image Texture per file, space, wrap and coordinates; returns its node key."""
        cache = ("image", filename, colorspace, extension, interpolation, vector)
        if cache not in self._cache:
            key = self.add(
                "image_texture",
                name,
                filename=_udim_tokens(filename),
                colorspace=colorspace,
                extension=extension,
                interpolation=interpolation,
            )
            self.set(key, "vector", vector)
            self._cache[cache] = key
        return self._cache[cache]

    def _math(self, operation, a, b, name):
        key = self.add("math", name, math_type=operation)
        self.set(key, "value1", a)
        self.set(key, "value2", b)
        return Socket(key, "value", "float")

    def _mix(self, blend, a, b, fac, name):
        key = self.add("mix_color", name, data_type="color", blend_type=blend, use_clamp=1, use_clamp_result=0)
        self.set(key, "a", _as_color(a))
        self.set(key, "b", _as_color(b))
        self.set(key, "fac", fac)
        return Socket(key, "result", "color")


def _udim_tokens(filename):
    """Mantra-style tile tokens become the <UDIM>/<UVTILE> tokens Cycles reads."""
    text = str(filename)
    for old, new in (("%(UDIM)d", "<UDIM>"), ("%(UVTILE)d", "<UVTILE>")):
        text = text.replace(old, new)
    return text


def _clean(value):
    if isinstance(value, (list, tuple)):
        return tuple(float(v) for v in value)
    if isinstance(value, bool):
        return int(value)
    return value


def _const(value):
    return not isinstance(value, Socket)


def _kind(value):
    if isinstance(value, Socket):
        return value.kind
    return "color" if isinstance(value, tuple) else "float"


def _color(value):
    if isinstance(value, (list, tuple)):
        values = tuple(float(v) for v in value)
        return (values + (values[-1],) * 3)[:3]
    return (float(value),) * 3


def _as_color(value):
    return value if isinstance(value, Socket) else _color(value)


def _is_one(value):
    if isinstance(value, Socket):
        return False
    if isinstance(value, (list, tuple)):
        return all(abs(float(v) - 1.0) < 1e-9 for v in value)
    return abs(float(value) - 1.0) < 1e-9


def is_constant(value):
    return _const(value)


def as_color(value):
    return _color(value)


def close(a, b, tolerance=1e-6):
    """Constant equality for floats and colours (Sockets are never equal to constants)."""
    if isinstance(a, Socket) or isinstance(b, Socket):
        return a == b
    if isinstance(a, tuple) or isinstance(b, tuple):
        return all(abs(x - y) <= tolerance for x, y in zip(_color(a), _color(b)))
    return abs(float(a) - float(b)) <= tolerance


def absorption(color, distance):
    """Cycles Volume Absorption (color, density) that turns white light into ``color`` after ``distance``.

    Cycles absorbs with sigma = density * (1 - color); Beer-Lambert gives
    sigma = -ln(color) / distance per channel.
    """
    distance = max(float(distance), 1e-6)
    sigma = [-math.log(min(max(c, 1e-6), 1.0)) / distance for c in _color(color)]
    density = max(sigma)
    if density <= 0:
        return None
    return tuple(1.0 - s / density for s in sigma), density
