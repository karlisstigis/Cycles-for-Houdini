# SPDX-License-Identifier: Apache-2.0
import pytest

from cycles_houdini.materials.graph_values import canonical_socket_type, constant_node

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


@pytest.mark.parametrize(
    "kind,value,definition,parameters,socket",
    [
        ("closure", None, "emission", {"color": (0.0, 0.0, 0.0), "strength": 0.0}, "emission"),
        ("int", 4.9, "integer_math", {"math_type": "add", "value1": 4, "value2": 0}, "value"),
        ("boolean", 7, "boolean_math", {"math_type": "or", "boolean1": 1, "boolean2": 0}, "boolean"),
        ("float", 3, "value", {"value": 3.0}, "value"),
        ("float3", [0.1, 0.2, 0.3], "color", {"value": (0.1, 0.2, 0.3)}, "color"),
    ],
)
def test_graph_constant_lowering(kind, value, definition, parameters, socket):
    node, endpoint, actual_kind = constant_node("owned/value", kind, value)
    assert node == dict(id="owned/value", definition="cycles/" + definition + "/1", parameters=parameters)
    assert endpoint == dict(node="owned/value", socket=socket)
    assert actual_kind == kind
    if isinstance(value, list):
        value[0] = 99
        assert node["parameters"]["value"] == (0.1, 0.2, 0.3)


def test_graph_type_normalization_and_unsupported_types():
    for kind in ("color", "vector", "normal", "point", "direction", "float3"):
        assert canonical_socket_type(kind) == "float3"
    assert canonical_socket_type("bsdf") == "closure"
    assert canonical_socket_type("int") == "int"
    with pytest.raises(ValueError, match="Unsupported Cycles output override type: matrix"):
        constant_node("rejected", "matrix", [])
