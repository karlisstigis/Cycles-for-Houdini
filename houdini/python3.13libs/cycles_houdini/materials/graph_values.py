# SPDX-License-Identifier: Apache-2.0
"""Pure owned shader-value lowering shared by native graph extraction."""


def canonical_socket_type(kind):
    if kind in ("closure", "bsdf"):
        return "closure"
    if kind in ("color", "vector", "normal", "point", "direction", "float3"):
        return "float3"
    return kind


def constant_node(key, kind, value):
    if kind == "closure":
        definition, parameters, socket = "emission", {"color": (0.0, 0.0, 0.0), "strength": 0.0}, "emission"
    elif kind == "int":
        definition, parameters, socket = (
            "integer_math",
            {"math_type": "add", "value1": int(value), "value2": 0},
            "value",
        )
    elif kind == "boolean":
        definition, parameters, socket = (
            "boolean_math",
            {"math_type": "or", "boolean1": int(bool(value)), "boolean2": 0},
            "boolean",
        )
    elif kind == "float":
        definition, parameters, socket = "value", {"value": float(value)}, "value"
    elif kind == "float3":
        definition, parameters, socket = "color", {"value": tuple(float(v) for v in value)}, "color"
    else:
        raise ValueError("Unsupported Cycles output override type: " + kind)
    node = dict(id=key, definition="cycles/" + definition + "/1", parameters=parameters)
    return node, dict(node=key, socket=socket), kind
