# SPDX-License-Identifier: Apache-2.0
"""Native curve SOPs and independent Bernstein/Cox-de Boor reference values."""

from support.paths import artifacts_root
from math import comb
import json
import os
from pathlib import Path

import hou
import numpy as np

CONTROLS = ((-1.25, -0.8, 0), (-0.95, 0.8, 0.1), (0.6, -0.7, -0.1), (1.25, 0.7, 0))


def coordinates(points):
    return " ".join(",".join(str(float(c)) for c in p) for p in points)


class Fixture:
    def __init__(self):
        self.dest = artifacts_root() / "stage2/hair/parametric"
        self.dest.mkdir(parents=True, exist_ok=True)
        self.obj = hou.node("/obj").createNode("geo", "parametric_curves", run_init_scripts=False)
        self.sources = {}
        for name, kind, points, order in (
            ("cubic_bezier", "bezier", CONTROLS, 4),
            ("multi_bezier", "bezier", CONTROLS + ((1.5, -0.3, 0.1), (-0.3, -1, -0.1), (-1, -0.1, 0)), 4),
            ("quintic_bezier", "bezier", CONTROLS[:2] + ((-0.4, -0.2, 0.2), (0.4, 0.3, -0.2)) + CONTROLS[2:], 6),
            ("open_nurbs", "nurbs", CONTROLS[:2] + ((-0.4, -0.2, 0.2), (0.4, 0.3, -0.2)) + CONTROLS[2:], 4),
        ):
            node = self.obj.createNode("curve", name, exact_type_name=True)
            node.setParms(dict(type=kind, coords=coordinates(points), order=order))
            self.sources[name] = node
        circle = self.obj.createNode("circle", "analytic_circle")
        circle.setParms(dict(type="prim"))
        circle.parmTuple("rad").set((0.8, 0.8))
        for kind, target in (("rational_bezier", "bezCurve"), ("rational_nurbs", "nurbCurve")):
            node = self.obj.createNode("convert", kind)
            node.setInput(0, circle)
            node.parm("totype").set(target)
            self.sources[kind] = node
        for kind in ("nurbs", "bezier"):
            node = self.obj.createNode("circle", "periodic_" + kind)
            node.setParms(dict(type=kind, order=4, divs=8))
            node.parmTuple("rad").set((0.8, 0.8))
            self.sources["periodic_" + kind] = node
        # Deliberately malformed knot/control pairing in the host's JSON format.
        # Houdini's loader truncates this edited knot vector; the adapter must
        # diagnose it rather than silently evaluate only the first piece.
        g = hou.Geometry()
        curve = g.createNURBSCurve(8, False, 4)
        for vertex, p in zip(
            curve.vertices(),
            (
                (-1.2, -0.4, 0),
                (-1, 0.8, 0),
                (-0.5, 0.8, 0),
                (-0.3, -0.4, 0),
                (0.3, -0.4, 0),
                (0.5, 0.8, 0),
                (1, 0.8, 0),
                (1.2, -0.4, 0),
            ),
        ):
            vertex.point().setPosition(p)
        path = self.dest / "discontinuous-native.geo"
        g.saveToFile(str(path))
        data = json.loads(path.read_text())

        def replace_knots(value):
            if not isinstance(value, list):
                return
            for i, part in enumerate(value):
                if isinstance(part, str) and part == "knots":
                    value[i + 1] = [0, 0, 0, 0, 0.5, 0.5, 0.5, 0.5, 1, 1, 1, 1]
                elif isinstance(part, list):
                    replace_knots(part)

        replace_knots(data)
        path.write_text(json.dumps(data))
        node = self.obj.createNode("file", "discontinuous_nurbs")
        node.parm("file").set(str(path))
        self.invalid_sources = {"bad_basis": node}
        node = self.obj.createNode("convert", "piecewise_nurbs")
        node.setInput(0, self.sources["multi_bezier"])
        node.parm("totype").set("nurbCurve")
        self.sources["piecewise_nurbs"] = node
        self.attributes = {}
        for name, source in self.sources.items():
            node = self.obj.createNode("attribwrangle", name + "_attributes")
            node.setInput(0, source)
            node.parm("snippet").set("""float t=float(@ptnum)/max(1,@numpt-1);
f@width=.06+.04*t; v@Cd=set(.2+.4*t,.7-.3*t,.1+.2*t);
v@uv=set(.1+.6*t,.2+.3*t,0); f@signal=.15+.5*t;
v@custom=set(t,t*t,1-t); v@v=set(.5*t,.2*(1-t),-.1*t);""")
            self.attributes[name] = node


def basis(primitive, u, *, rational=True):
    """Independent normalized rational control weights; no plugin/Houdini evaluator."""
    knots = np.array(primitive.intrinsicValue("knots"), float)
    order = primitive.intrinsicValue("order")
    degree = order - 1
    count = len(primitive.vertices())
    closed = primitive.isClosed()
    weights = (
        np.array([v.point().weight() for v in primitive.vertices()])
        if rational and primitive.intrinsicValue("rational")
        else np.ones(count)
    )
    result = np.zeros(count)
    if primitive.type() == hou.primType.BezierCurve:
        t = knots[0] + u * (knots[-1] - knots[0])
        span = min(len(knots) - 2, max(0, int(np.searchsorted(knots, t, side="right") - 1)))
        v = (t - knots[span]) / (knots[span + 1] - knots[span])
        for i in range(order):
            result[(span * degree + i) % count] += comb(degree, i) * v**i * (1 - v) ** (degree - i)
    else:
        expanded = len(knots) - order
        start, end = knots[degree], knots[expanded]
        t = start + u * (end - start)
        if u == 1:
            t = np.nextafter(end, start)
        values = np.array([float(knots[i] <= t < knots[i + 1]) for i in range(len(knots) - 1)])
        for p in range(1, order):
            next_values = np.zeros(len(values) - 1)
            for i in range(len(next_values)):
                if knots[i + p] != knots[i]:
                    next_values[i] += (t - knots[i]) / (knots[i + p] - knots[i]) * values[i]
                if knots[i + p + 1] != knots[i + 1]:
                    next_values[i] += (knots[i + p + 1] - t) / (knots[i + p + 1] - knots[i + 1]) * values[i + 1]
            values = next_values
        for i, value in enumerate(values):
            result[i % count] += value
    result *= weights
    return result / result.sum()


def parameters(primitive, divisions):
    knots = np.array(primitive.intrinsicValue("knots"), float)
    if primitive.type() != hou.primType.BezierCurve:
        degree = primitive.intrinsicValue("order") - 1
        knots = knots[degree : len(knots) - degree]
    breaks = np.unique(knots)
    parameters = []
    first = [0]
    for span, (a, b) in enumerate(zip(breaks[:-1], breaks[1:])):
        start, end = (a - breaks[0]) / (breaks[-1] - breaks[0]), (b - breaks[0]) / (breaks[-1] - breaks[0])
        values = np.linspace(start, end, divisions + 1)
        split = (
            span + 2 < len(breaks)
            and primitive.type() != hou.primType.BezierCurve
            and np.count_nonzero(knots == b) >= primitive.intrinsicValue("order")
        )
        if split:
            values[-1] = float(np.nextafter(np.float32(end), np.float32(start)))
        parameters.extend(values[1:] if parameters and first[-1] != len(parameters) else values)
        if split:
            first.append(len(parameters))
    return np.array(parameters), first


def expected(primitive, divisions):
    u, first = parameters(primitive, divisions)
    coefficients = np.array([basis(primitive, float(t)) for t in u])
    attributes = np.array([basis(primitive, float(t), rational=False) for t in u])
    controls = np.array([v.point().position() for v in primitive.vertices()])
    result = {"positions": coefficients @ controls}
    for name in ("width", "Cd", "uv", "signal", "custom", "v"):
        values = np.array([v.point().attribValue(name) for v in primitive.vertices()])
        result[name] = (coefficients if name == "v" else attributes) @ values
    result["parameters"] = u
    result["first_key"] = first
    return result


def raw_arrays(raw):
    result = {
        name: np.frombuffer(raw[name], np.float32).reshape(-1, components)
        for name, components in (("positions", 3), ("colors", 3), ("uv", 2), ("velocities", 3))
    }
    result["radii"] = np.frombuffer(raw["radii"], np.float32)
    result["first_key"] = np.frombuffer(raw["first_key"], np.int32)
    result["attributes"] = {
        name: np.frombuffer(field["values"], np.float32).reshape(-1, 3) for name, field in raw["attributes"].items()
    }
    return result


def reference_geometry(primitive, divisions, geometry=None):
    """Polygon reference authored by independent basis math, including attributes."""
    result = expected(primitive, divisions)
    g = hou.Geometry() if geometry is None else geometry
    for name, default in (
        ("width", 0.08),
        ("Cd", (1.0, 1.0, 1.0)),
        ("uv", (0.0, 0.0, 0.0)),
        ("v", (0.0, 0.0, 0.0)),
        ("signal", 0.0),
        ("custom", (0.0, 0.0, 0.0)),
    ):
        g.addAttrib(hou.attribType.Point, name, default)
    curve = None
    for index, position in enumerate(result["positions"]):
        if index in result["first_key"]:
            curve = g.createPolygon(is_closed=False)
        p = g.createPoint()
        p.setPosition(position)
        for name in ("width", "Cd", "uv", "v", "signal", "custom"):
            value = result[name][index]
            p.setAttribValue(name, float(value) if np.ndim(value) == 0 else tuple(float(v) for v in value))
        curve.addVertex(p)
    return g
