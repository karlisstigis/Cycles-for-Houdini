# SPDX-License-Identifier: Apache-2.0
"""Native Cycles VOP authoring and read-only, owned shader graph extraction."""

from collections import Counter
from functools import lru_cache
import threading
import uuid

from importlib import import_module as _import_host_module


class _LazyHoudini:
    def __getattr__(self, name):
        return getattr(_import_host_module("hou"), name)


hou = _LazyHoudini()

BUILDER = "cycles::material::1"
SURFACE_OUTPUT = "cycles::surface_output::1"
VOLUME_BUILDER = "cycles::volume_material::1"
VOLUME_OUTPUT = "cycles::volume_output::1"
WORLD_BUILDER = "cycles::world_material::1"
WORLD_OUTPUT = "cycles::world_output::1"
LIGHT_BUILDER = "cycles::light_material::1"
LIGHT_OUTPUT = "cycles::light_output::1"
SUBNET = "subnet"
CONNECTOR = "subnetconnector"
COLOR_RAMP = "cycles::color_ramp::1"
RAMPS = (COLOR_RAMP,)  # Float Ramp was retired; Float Curve replaces it.
CURVES = frozenset(("float_curve", "rgb_curves", "vector_curves"))
PRIVATE_MIX_TYPES = frozenset(("mix", "mix_float", "mix_vector", "mix_vector_non_uniform"))
# Material > Settings (Blender names): builder parameter, graph key, default.
# Defaults equal the engine's Shader defaults. Only values that differ are
# sent, so materials made before these parameters existed keep their graph.
MATERIAL_SETTINGS = (
    ("cycles_emission_sampling", "emission_sampling", "auto"),
    ("cycles_transparent_shadows", "transparent_shadows", True),
    ("cycles_bump_map_correction", "bump_map_correction", True),
    ("cycles_volume_sampling", "volume_sampling", "multiple_importance"),
    ("cycles_volume_interpolation", "volume_interpolation", "linear"),
    ("cycles_volume_step_rate", "volume_step_rate", 1.0),
)
_VECTOR_DEFAULTS = {
    "color": "colordef",
    "vector": "vectordef",
    "float3": "float3def",
    "normal": "normaldef",
    "point": "pointdef",
    "direction": "directiondef",
}


@lru_cache(maxsize=1)
def _engine_definitions():
    from .. import shader_catalogue

    return {
        f"cycles::{item['engine_type']}::1": item
        for item in shader_catalogue()["definitions"]
        if item["surface_graph_adapter"] or item["volume_graph_adapter"] or item.get("world_graph_adapter")
    }


@lru_cache(maxsize=1)
def _definitions():
    # Blender's single modern Mix lowers to typed engine nodes. The old RGB
    # mixer and typed backend variants are not separate native authoring nodes.
    return {name: item for name, item in _engine_definitions().items() if item["engine_type"] not in PRIVATE_MIX_TYPES}


def _builder(node):
    if node is None or node.type().name() not in (BUILDER, VOLUME_BUILDER, WORLD_BUILDER, LIGHT_BUILDER):
        raise ValueError("Choose a version 1 Cycles Material builder")
    if node.isBypassed():
        raise ValueError("Bypassed Cycles material builders are unsupported: " + node.path())
    return node


def _source(connection):
    """Resolve public Houdini dots and their original output indices."""
    item, index = connection.inputItem(), connection.inputItemOutputIndex()
    seen = set()
    while isinstance(item, hou.NetworkDot):
        if item in seen:
            raise ValueError("Cyclic material network dots")
        seen.add(item)
        index, item = item.inputItemOutputIndex(), item.inputItem()
    if not isinstance(item, hou.VopNode):
        raise ValueError("Unconnected or unsupported material network input")
    return item, index


def _reachable_children(builder, output, now, output_mode, input_active, output_scope=None):
    """Follow used subnet ports, not every child of a participating subnet."""
    used, visited, executing = {output}, set(), set()

    def visit(node, index=0, through_output=False):
        portal = node, index, through_output
        if portal in visited:
            return  # The graph/interface compiler diagnoses reachable cycles.
        visited.add(portal)
        used.add(node)
        parent = node.parent()
        while parent is not None and parent != builder:
            used.add(parent)
            parent = parent.parent()
        kind = node.type().name()
        override = output_mode(node, index) if node.outputNames() and not through_output else None
        if override is not None:
            if override["mode"] == "input":
                for connection in node.inputConnections():
                    if connection.outputName() == override["input"]:
                        visit(*_source(connection))
            return
        executing.add(node)
        if kind == SUBNET:
            names = node.outputNames()
            if not 0 <= index < len(names):
                raise ValueError("Invalid Cycles subnet output index")
            for connector in node.children():
                if connector.type().name() == CONNECTOR and names[index] in connector.outputNames():
                    visit(connector, through_output=True)
            return
        if (
            kind == CONNECTOR
            and not through_output
            and node.parm("connectorkind").evalAsString() in ("input", "inputoutput")
        ):
            name = node.parm("parmname").evalAtTime(now)
            connections = [item for item in node.parent().inputConnections() if item.outputName() == name]
        else:
            connections = node.inputConnections()
        for connection in connections:
            if (
                node == output
                and output_scope is not None
                and ((connection.outputName() == "displacement") != (output_scope == "displacement"))
            ):
                continue
            if input_active(node, connection.outputName()):
                visit(*_source(connection))

    visit(output)
    for node in builder.allSubChildren():
        if output_scope != "displacement" and node.type().name() == "cycles::aov_output::1" and not node.isBypassed():
            visit(node)  # AOV outputs are independently observable roots.
    # Warning side effects belong only to compute contexts that participate in
    # this material. A dormant subnet must not report unrelated warnings.
    for node in builder.allSubChildren():
        if (
            output_scope != "displacement"
            and node.type().name() == "cycles::warning::1"
            and not node.isBypassed()
            and (node.parent() == builder or node.parent() in used)
        ):
            visit(node)
    return [node for node in builder.allSubChildren() if node in used], executing


def _reachability_controls(now, evaluation=None):
    modes = {}

    def output_mode(node, index):
        if not node.isBypassed() and not node.isDebugFlagSet():
            return None
        key = node, index
        if key not in modes:
            from .. import _native

            names = node.outputNames()
            if not 0 <= index < len(names):
                raise ValueError("Invalid Cycles output override: " + node.path())
            modes[key] = _native.vop_output_override(node.sessionId(), names[index])
        return modes[key]

    activity = {}

    def input_active(node, name):
        if node.type().name() not in _definitions():
            return True
        if node not in activity:
            from .. import _native

            values = (
                {name: evaluation.token(node.parm(name), now) for name in _native.vop_input_controls(node.sessionId())}
                if evaluation
                else {}
            )
            activity[node] = _native.vop_input_activity(node.sessionId(), now, values)
        return activity[node].get(name, True)

    return output_mode, input_active


def execution_nodes(builder, at_time=None):
    """Host nodes whose parameters execute, without evaluating image filenames."""
    builder = _builder(builder)
    now = hou.time() if at_time is None else at_time
    outputs = [
        n for n in builder.children() if n.type().name() in (SURFACE_OUTPUT, VOLUME_OUTPUT, WORLD_OUTPUT, LIGHT_OUTPUT)
    ]
    if len(outputs) != 1:
        return set()
    output_mode, input_active = _reachability_controls(now)
    return _reachable_children(builder, outputs[0], now, output_mode, input_active)[1]


class MaterialSourceError(ValueError):
    """An authored graph cannot be translated; eligible for material fallback."""


def material_graph(builder, at_time=None, *, evaluation=None, output_scope=None):
    from .material_overrides import UnsupportedOverride

    try:
        return _material_graph(builder, at_time, evaluation=evaluation, output_scope=output_scope)
    except UnsupportedOverride:
        raise
    except ValueError as error:
        raise MaterialSourceError(str(error)) from error


def _material_graph(builder, at_time=None, *, evaluation=None, output_scope=None):
    """Extract versioned sockets and evaluated parameters; never read SOP data."""
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("Material graph preparation requires the host thread")
    builder = _builder(builder)
    if output_scope not in (None, "surface", "displacement"):
        raise RuntimeError("Invalid internal material output scope")
    now = hou.time() if at_time is None else at_time

    def evaluated(parameter):
        return evaluation.tuple(parameter, now) if evaluation else parameter.evalAtTime(now)

    def scalar(parameter):
        return evaluation.parm(parameter, now) if evaluation else parameter.evalAtTime(now)

    children = builder.allSubChildren()
    if len(children) > 1024:
        raise ValueError("Cycles Material supports at most 1024 host nodes")
    outputs = [
        node
        for node in builder.children()
        if node.type().name() in (SURFACE_OUTPUT, VOLUME_OUTPUT, WORLD_OUTPUT, LIGHT_OUTPUT)
    ]
    if len(outputs) != 1:
        raise ValueError(
            "Cycles Material requires exactly one output matching its Surface, Volume, World or Light builder"
        )
    expected = {
        BUILDER: SURFACE_OUTPUT,
        VOLUME_BUILDER: VOLUME_OUTPUT,
        WORLD_BUILDER: WORLD_OUTPUT,
        LIGHT_BUILDER: LIGHT_OUTPUT,
    }[builder.type().name()]
    if outputs[0].type().name() != expected:
        raise ValueError("Use the output matching the Cycles material builder domain")
    domain = "volume" if outputs[0].type().name() == VOLUME_OUTPUT else "surface"
    output_label = (
        "Cycles Light Output"
        if expected == LIGHT_OUTPUT
        else "Cycles World Output"
        if expected == WORLD_OUTPUT
        else "Cycles Volume Output"
        if domain == "volume"
        else "Cycles Material Output"
    )
    if outputs[0].isBypassed():
        raise ValueError("Bypassed " + output_label + " requires explicit rewiring")
    output_mode, input_active = _reachability_controls(now, evaluation)
    children, executing = _reachable_children(builder, outputs[0], now, output_mode, input_active, output_scope)
    definitions = _definitions()
    selected_definitions = {}

    def definition_for(node):
        if node not in selected_definitions:
            definition = definitions.get(node.type().name())
            if definition and definition["engine_type"].startswith("index_switch_"):
                count = int(scalar(node.parm("item_count")))
                if not 0 <= count <= 9998:
                    raise ValueError("Index Switch exceeds Houdini input limit (9998 items)")
                prototype = next(s for s in definition["inputs"] if s["id"] == "item0")
                definition = dict(
                    definition,
                    inputs=[
                        s for s in definition["inputs"] if not s["id"].startswith("item") or s["id"] == "item_count"
                    ]
                    + [dict(prototype, id="item" + str(i), label=str(i)) for i in range(count)],
                )
            if node.type().name() == "cycles::mix_color::1":
                data_type = str(scalar(node.parm("data_type")))
                mode = str(scalar(node.parm("factor_mode"))) if data_type == "vector" else "uniform"
                if data_type not in ("float", "vector", "color") or mode not in ("uniform", "non_uniform"):
                    raise ValueError("Unsupported Mix data type or factor mode: " + node.path())
                kind = (
                    "mix_float"
                    if data_type == "float"
                    else "mix_vector_non_uniform"
                    if mode == "non_uniform"
                    else "mix_vector"
                    if data_type == "vector"
                    else "mix_color"
                )
                definition = _engine_definitions()["cycles::" + kind + "::1"]
                aliases = (
                    {"a": "a_float", "b": "b_float"}
                    if data_type == "float"
                    else {"a": "a_vector", "b": "b_vector"}
                    if data_type == "vector"
                    else {}
                )
                if mode == "non_uniform":
                    aliases["fac"] = "fac_vector"
                definition = dict(
                    definition, inputs=[dict(s, parm_id=aliases.get(s["id"], s["id"])) for s in definition["inputs"]]
                )
            selected_definitions[node] = definition
        return selected_definitions[node]

    shaders, subnets, connectors, ramps, curves, overridden = [], [], [], [], [], []
    for node in children:
        type = node.type().name()
        if node in outputs:
            continue
        if type == SUBNET:
            subnets.append(node)
        elif type == CONNECTOR:
            connectors.append(node)
        elif type in RAMPS:
            ramps.append(node)
        elif type in definitions and type.split("::")[1] in CURVES:
            curves.append(node)  # Inline native ramps lower to owned nodes on use.
        elif type in definitions:
            shaders.append(node)
        elif node not in executing:
            overridden.append(node)
        else:
            raise ValueError("Unsupported Cycles material node: " + node.path())
        ancestor, depth = node.parent(), 0
        while ancestor != builder and ancestor is not None:
            if ancestor.type().name() != SUBNET:
                raise ValueError("Material nodes require a Cycles subnetwork: " + node.path())
            ancestor, depth = ancestor.parent(), depth + 1
        if ancestor is None or depth > 32:
            raise ValueError("Cycles material subnet nesting supports at most 32 levels")
    if sum(node in executing for node in shaders) > 256:
        raise ValueError("Cycles Material supports at most 256 shader nodes")
    ids = {
        node: node.userData("cycles_graph_id") or builder.relativePathTo(node)
        for node in shaders + connectors + ramps + curves + overridden + subnets
    }
    counts = Counter(ids.values())
    # Copy/paste can retain user data. Disambiguate within this graph without
    # changing the host scene; ordinary renames preserve the persistent ID.
    ids = {
        node: value if counts[value] == 1 else value + "/" + builder.relativePathTo(node) for node, value in ids.items()
    }

    interfaces = {subnet: {"input": {}, "output": {}} for subnet in subnets}
    declared_ports = {subnet: {"input": set(), "output": set()} for subnet in subnets}
    connector_info = {}
    for connector in connectors:
        parent = connector.parent()
        if parent not in interfaces:
            raise ValueError("Subnet connectors must be inside a Cycles subnetwork")
        for field in ("parmname", "parmtype", "parmtypename", "connectorkind"):
            if evaluation:
                evaluation.reject_if_overridden(
                    (connector.parm(field),), "Subnet interface overrides require authoring the graph interface"
                )
            if connector.parm(field).keyframes():
                raise ValueError("Subnet interface names and types cannot be animated: " + connector.path())
        name = connector.parm("parmname").evalAtTime(now)
        kind = connector.parm("connectorkind").evalAsString()
        type = connector.parm("parmtype").evalAsString()
        if not name or len(name.encode("utf-8")) > 256:
            raise ValueError("Invalid Cycles subnet port name")
        if kind not in ("input", "output", "inputoutput") or type not in ("float", "bsdf", *_VECTOR_DEFAULTS):
            raise ValueError("Unsupported Cycles subnet port kind/type: " + connector.path())
        connector_info[connector] = name, kind, type
        for direction in ("input", "output"):
            if kind not in (direction, "inputoutput"):
                continue
            if name in declared_ports[parent][direction]:
                raise ValueError("Duplicate Cycles subnet " + direction + " port: " + name)
            declared_ports[parent][direction].add(name)
            # The public host publishes an input/output connector as `_name`
            # while its parent input remains `name`. Read actual output tokens
            # instead of assuming the two sides have identical port names.
            published = connector.outputNames()
            if direction == "output" and len(published) != 1:
                raise ValueError("Unsupported Cycles subnet connector output layout")
            key = name if direction == "input" else published[0]
            if key in interfaces[parent][direction]:
                raise ValueError("Duplicate published Cycles subnet port: " + key)
            interfaces[parent][direction][key] = connector

    def canonical(type):
        from .graph_values import canonical_socket_type

        return canonical_socket_type(type)

    constants = {}
    nodes, links, ramp_outputs, override_outputs, curve_outputs = [], [], {}, {}, {}
    compiling_ramps, compiling_curves = set(), set()

    def constant_endpoint(key, type, value):
        from .graph_values import constant_node

        node, endpoint, kind = constant_node(key, type, value)
        constants[key] = node
        return endpoint, kind

    def default_endpoint(node, name):
        definition = definition_for(node) or {}
        socket = next((s for s in definition.get("inputs", ()) if s.get("host_id", s["id"]) == name), None)
        key = ids[node] + "/bypass-default/" + name
        if socket and socket["default_links"]:
            coordinates = {
                "normal": ("geometry", "normal"),
                "position": ("geometry", "position"),
                "incoming": ("geometry", "incoming"),
                "texture_uv": ("texture_coordinate", "UV"),
                "texture_generated": ("texture_coordinate", "generated"),
            }
            if len(socket["default_links"]) != 1 or socket["default_links"][0] not in coordinates:
                raise ValueError("Unsupported implicit input in Cycles bypass: " + node.path() + "/" + name)
            kind, output = coordinates[socket["default_links"][0]]
            constants[key] = dict(id=key, definition="cycles/" + kind + "/1", parameters={})
            return dict(node=key, socket=output), "float3"
        parameter = node.parmTuple(socket.get("parm_id", name) if socket else name)
        type = canonical(socket["type"] if socket else node.inputDataTypes()[node.inputNames().index(name)])
        if parameter is not None:
            value = evaluated(parameter)
            value = value[0] if len(value) == 1 else value
        elif node.type().name() in RAMPS:
            type, value = "float", scalar(node.parm("factor"))
        else:
            value = socket["default"] if socket else ((0.0, 0.0, 0.0) if type == "float3" else 0.0)
        return constant_endpoint(key, type, value)

    def typed_override(key, result, type):
        if result[1] == type or (result[1] == "float" and type in ("int", "boolean")):
            return result
        if type == "closure" and result[1] in ("float", "float3", "int", "boolean"):
            constants[key] = dict(
                id=key, definition="cycles/emission/1", parameters={"color": (1.0, 1.0, 1.0), "strength": 1.0}
            )
            links.append(
                {"from": result[0], "to": dict(node=key, socket="color" if result[1] == "float3" else "strength")}
            )
            return dict(node=key, socket="emission"), type
        if type not in ("float", "float3") or result[1] not in ("float", "float3", "int", "boolean"):
            raise ValueError("Incompatible Cycles bypass input/output types")
        # A neutral typed input lets Cycles perform its own normal scalar/vector
        # conversion. No host-side color math or shader evaluation is guessed.
        vector = type == "float3"
        constants[key] = dict(
            id=key, definition="cycles/vector_math/1" if vector else "cycles/math/1", parameters={"math_type": "add"}
        )
        links.append({"from": result[0], "to": dict(node=key, socket="vector1" if vector else "value1")})
        return dict(node=key, socket="vector" if vector else "value"), type

    def ramp_endpoint(node, active, index=0):
        portal = node, index
        if portal in ramp_outputs:
            return ramp_outputs[portal]
        if portal in compiling_ramps:
            raise ValueError("Cyclic Cycles ramp interface")
        compiling_ramps.add(portal)
        color = True
        incoming = node.inputConnections()
        factor = None
        if incoming:
            if len(incoming) != 1 or incoming[0].outputName() != "factor":
                raise ValueError("Unsupported Cycles ramp input layout")
            factor, type = endpoint(incoming[0], active)
            if factor is None or type != "float":
                raise ValueError("Cycles ramp Factor requires a scalar input")
        from .ramps import lower_color_output

        lowered, connections, result = lower_color_output(
            node,
            ids[node],
            "alpha" if index else "color",
            now,
            evaluation,
            factor,
            0.5 if factor is not None else scalar(node.parm("factor")),
        )
        nodes.extend(lowered)
        links.extend(connections)
        if len(nodes) + len(constants) > 256:
            raise ValueError("Flattened Cycles material supports 1..256 shader nodes")
        compiling_ramps.remove(portal)
        ramp_outputs[portal] = result, "float3" if color and index == 0 else "float"
        return ramp_outputs[portal]

    def curve_endpoint(node, active):
        if node in curve_outputs:
            return curve_outputs[node]
        if node in compiling_curves:
            raise ValueError("Cyclic Cycles curve interface")
        compiling_curves.add(node)
        vector = node.type().name() != "cycles::float_curve::1"
        incoming = {}
        for connection in node.inputConnections():
            if connection.outputName() not in ("value", "fac") or connection.outputName() in incoming:
                raise ValueError("Unsupported Cycles curve input layout")
            incoming[connection.outputName()] = connection
        operands, factor_constant = {}, None
        for name in ("value", "fac"):
            if name in incoming:
                result, type = endpoint(incoming[name], active)
                if result is None:
                    raise ValueError("Cycles curve inputs require a numeric source")
                operands[name] = result
            else:
                value = evaluated(node.parmTuple(name))
                kind = "float3" if name == "value" and vector else "float"
                operands[name] = constant_endpoint(
                    ids[node] + "/" + name, kind, value if kind == "float3" else value[0]
                )[0]
                if name == "fac":
                    factor_constant = float(value[0])
        from .shader_curves import lower as lower_curve

        lowered, connections, result = lower_curve(
            node, ids[node], now, evaluation, operands["value"], operands["fac"], factor_constant
        )
        nodes.extend(lowered)
        links.extend(connections)
        if len(nodes) + len(constants) > 256:
            raise ValueError("Flattened Cycles material supports 1..256 shader nodes")
        compiling_curves.remove(node)
        curve_outputs[node] = result, "float3" if vector else "float"
        return curve_outputs[node]

    def input_default(connector):
        name, kind, type = connector_info[connector]
        if type == "bsdf":
            if scalar(connector.parm("bsdfdef")):
                raise ValueError("Named host BSDF defaults require a Cycles graph adapter")
            return None, "closure"
        parent_value = connector.parent().parmTuple(name)
        value = evaluated(
            parent_value or connector.parmTuple("floatdef" if type == "float" else _VECTOR_DEFAULTS[type])
        )
        count = 1 if type == "float" else 3
        if len(value) != count:
            raise ValueError("Subnet default parameter has an incompatible tuple size: " + name)
        key = ids[connector] + "/default"
        constants[key] = dict(
            id=key,
            definition="cycles/value/1" if type == "float" else "cycles/color/1",
            parameters={
                "value": float(value[0]) if type == "float" else tuple(float(component) for component in value)
            },
        )
        return dict(node=key, socket="value" if type == "float" else "color"), canonical(type)

    def boundary(connector, result):
        expected = canonical(connector_info[connector][2])
        if result[1] != expected:
            raise ValueError("Cycles subnet port requires an explicit type conversion: " + connector.path())
        return result

    def endpoint(connection, active=None):
        source, index = _source(connection)
        active = set() if active is None else active
        portal = source, index
        if portal in active:
            raise ValueError("Cyclic Cycles subnet interface")
        active = active | {portal}
        override = output_mode(source, index)
        if override is not None:
            if portal in override_outputs:
                return override_outputs[portal]
            type = canonical(
                definition_for(source)["outputs"][index]["type"]
                if source.type().name() == "cycles::mix_color::1"
                else source.outputDataTypes()[index]
            )
            key = ids[source] + "/override/" + source.outputNames()[index]
            if override["mode"] == "constant":
                result = constant_endpoint(key, type, override["value"])
            else:
                name = override["input"]
                incoming = [item for item in source.inputConnections() if item.outputName() == name]
                if len(incoming) > 1:
                    raise ValueError("Ambiguous Cycles bypass input: " + source.path())
                result = endpoint(incoming[0], active) if incoming else default_endpoint(source, name)
                result = typed_override(key, result, type)
            override_outputs[portal] = result
            return result
        if source in ramps:
            if index not in (0, 1):
                raise ValueError("Invalid Cycles ramp output index")
            return ramp_endpoint(source, active, index)
        if source in curves:
            if index != 0:
                raise ValueError("Invalid Cycles curve output index")
            return curve_endpoint(source, active)
        if source.type().name() == SUBNET and source in interfaces:
            names = source.outputNames()
            if not 0 <= index < len(names) or names[index] not in interfaces[source]["output"]:
                raise ValueError("Missing Cycles subnet output connector")
            connector = interfaces[source]["output"][names[index]]
            incoming = connector.inputConnections()
            if len(incoming) != 1:
                raise ValueError("Connect a shader value to the subnet output: " + connector.path())
            return boundary(connector, endpoint(incoming[0], active))
        if source in connector_info:
            name, kind, type = connector_info[source]
            if kind not in ("input", "inputoutput") or index != 0:
                raise ValueError("Read a Cycles subnet input connector inside its subnet")
            incoming = [item for item in source.parent().inputConnections() if item.outputName() == name]
            result = endpoint(incoming[0], active) if incoming else input_default(source)
            return boundary(source, result)
        if source not in shaders:
            raise ValueError("Material connections must refer to shader nodes inside this builder")
        names = source.outputNames()
        if not 0 <= index < len(names):
            raise ValueError("Invalid material output index")
        if source.type().name() == "cycles::scene_time::1":
            name = names[index]
            key = ids[source] + "/" + name
            constants[key] = dict(
                id=key,
                definition="cycles/value/1",
                parameters={"value": float(now if name == "seconds" else hou.timeToFrame(now))},
            )
            return dict(node=key, socket="value"), "float"
        socket = next(socket for socket in definition_for(source)["outputs"] if socket["id"] == names[index])
        return dict(node=ids[source], socket=names[index]), canonical(socket["type"])

    for node in sorted(shaders, key=lambda item: ids[item]):
        if node not in executing:
            continue  # No parameters, extra inputs or assets from muted work.
        if node.type().name() == "cycles::scene_time::1":
            continue  # Nominal host time is lowered to owned constants on use.
        definition = definition_for(node)
        if definition is None:
            raise ValueError("Unsupported Cycles material node: " + node.path())
        connections = sorted(
            (item for item in node.inputConnections() if input_active(node, item.outputName())),
            key=lambda item: item.inputIndex(),
        )
        connected = {connection.outputName() for connection in connections}
        parameters = {}
        if definition["engine_type"] == "sky_texture":
            parameters["tex_mapping.type"] = "point"
        for socket in definition["inputs"]:
            if socket["id"].startswith("tex_mapping.") and not (
                definition["engine_type"] == "sky_texture" and socket["id"] == "tex_mapping.rotation"
            ):
                continue
            host_id = socket.get("host_id", socket["id"])
            if not input_active(node, host_id):
                continue
            if socket["type"] == "transform":
                continue
            if definition["engine_type"] == "bump" and socket["id"] in ("sample_center", "sample_x", "sample_y"):
                continue
            if (
                socket["internal_svm"]
                or socket["internal_osl"]
                or socket["default_links"]
                or socket["type"] == "closure"
                or host_id in connected
            ):
                continue
            parm_name = (
                "sky_rotation"
                if definition["engine_type"] == "sky_texture" and socket["id"] == "tex_mapping.rotation"
                else socket.get("parm_id", host_id)
            )
            parm = node.parmTuple(parm_name)
            if parm is None:
                raise ValueError("Missing versioned Cycles parameter: " + node.path() + "/" + socket["id"])
            value = evaluated(parm)
            type = socket["type"]
            if type == "boolean":
                parameters[socket["id"]] = bool(value[0])
            elif type == "int":
                parameters[socket["id"]] = int(value[0])
            elif type == "float":
                parameters[socket["id"]] = float(value[0])
                if definition["engine_type"] == "sky_texture" and socket["id"] in (
                    "sun_size",
                    "sun_elevation",
                    "sun_rotation",
                ):
                    import math

                    parameters[socket["id"]] = math.radians(parameters[socket["id"]])
            elif type in ("color", "vector", "point", "normal"):
                parameters[socket["id"]] = tuple(float(component) for component in value)
                if definition["engine_type"] == "sky_texture" and socket["id"] == "tex_mapping.rotation":
                    import math

                    parameters[socket["id"]] = tuple(math.radians(component) for component in value)
            elif type in ("string", "enum"):
                parameters[socket["id"]] = value[0]
                if (
                    definition["engine_type"] in ("image_texture", "environment_texture", "ies_light")
                    and socket["id"] == "filename"
                    and value[0]
                    and not value[0].startswith("op:")
                ):
                    # Relative artist paths are HIP-relative, independent of
                    # the process working directory or the renderer thread.
                    import os

                    path = value[0]
                    parameters[socket["id"]] = os.path.normpath(
                        path if os.path.isabs(path) else os.path.join(hou.expandString("$HIP"), path)
                    )
            elif type == "array_int" and definition["engine_type"] == "image_texture" and socket["id"] == "tiles":
                try:
                    parameters[socket["id"]] = [int(token) for token in value[0].split()]
                except ValueError as error:
                    raise ValueError(
                        "Image Texture tiles require space-separated integer IDs: " + node.path()
                    ) from error
            else:
                raise ValueError("Unauthored Cycles parameter type: " + type)
        nodes.append(dict(id=ids[node], definition=definition["id"], parameters=parameters))
        engine_inputs = {s.get("host_id", s["id"]): s["id"] for s in definition["inputs"]}
        for connection in connections:
            source, type = endpoint(connection)
            if source is not None:
                links.append(
                    {"from": source, "to": dict(node=ids[node], socket=engine_inputs[connection.outputName()])}
                )
            elif type != canonical(
                next(
                    socket
                    for socket in definition["inputs"]
                    if socket.get("host_id", socket["id"]) == connection.outputName()
                )["type"]
            ):
                raise ValueError("Unconnected closure default cannot feed a scalar/vector input")
    # Ramp outputs lower lazily: an unused invalid Color/Alpha branch must not
    # participate in validation or discard the other output.
    connections = outputs[0].inputConnections()
    if output_scope is not None:
        connections = [
            connection
            for connection in connections
            if (connection.outputName() == "displacement") == (output_scope == "displacement")
        ]
    closure = [connection for connection in connections if connection.outputName() == domain]
    volume_only = (
        expected in (SURFACE_OUTPUT, WORLD_OUTPUT)
        and not closure
        and any(connection.outputName() == "volume" for connection in connections)
    )
    if len(closure) != 1 and not volume_only and output_scope != "displacement":
        raise ValueError("Connect a " + domain + " closure to " + output_label)
    terminal = None
    if closure:
        terminal, type = endpoint(closure[0])
        allowed = ("closure", "float", "float3", "int", "boolean") if domain == "surface" else ("closure",)
        if terminal is None or type not in allowed:
            raise ValueError(
                "Connect a shader, color or value to " + output_label
                if domain == "surface"
                else "Connect a volume closure to " + output_label
            )
    extra = {}
    for connection in connections:
        if connection in closure:
            continue
        if expected in (SURFACE_OUTPUT, WORLD_OUTPUT) and connection.outputName() == "volume":
            source, type = endpoint(connection)
            if source is None or type != "closure":
                raise ValueError("Connect a volume closure to Cycles Material Output")
            extra["volume"] = source
            continue
        if domain != "surface" or connection.outputName() != "displacement":
            raise ValueError("Unsupported material output: " + connection.outputName())
        source, type = endpoint(connection)
        if source is None or type != "float3":
            raise ValueError("Connect a float3 vector to Cycles Displacement output")
        extra["displacement"] = source
        parameter = builder.parm("cycles_displacement_method")
        extra["displacement_method"] = scalar(parameter) if parameter else "bump"
    nodes.extend(constants.values())
    if "displacement" in extra:
        # Only the active displacement dependency graph needs shutter-time
        # parameter snapshots. Surface-only animation must not disable blur.
        upstream = {}
        for link in links:
            upstream.setdefault(link["to"]["node"], set()).add(link["from"]["node"])
        active, pending = set(), [extra["displacement"]["node"]]
        while pending:
            key = pending.pop()
            if key not in active:
                active.add(key)
                pending.extend(upstream.get(key, ()))
        animated = any(
            parameter.isTimeDependent() for parameter in (builder.parm("cycles_displacement_method"),) if parameter
        )
        animated |= any(
            node["id"] in active and node.get("parameters", {}).get("filename", "").startswith("op:") for node in nodes
        )
        for node, key in ids.items():
            if any(value == key or value.startswith(key + "/") for value in active):
                animated |= (
                    node.type().name() == "cycles::scene_time::1"
                    or any(parm.isTimeDependent() for parm in node.parms())
                    or bool(evaluation and node in evaluation.animated_nodes)
                )
        extra["displacement_time_dependent"] = bool(animated)
    if len(links) > 2048:
        raise ValueError("Cycles Material supports at most 2048 links")
    if not (0 if output_scope == "displacement" else 1) <= len(nodes) <= 256:
        raise ValueError("Flattened Cycles material supports 1..256 shader nodes")
    if any(node.get("parameters", {}).get("filename", "").startswith("op:") for node in nodes):
        extra["image_time"] = float(now)
    if builder.type().name() in (BUILDER, VOLUME_BUILDER):
        settings = {}
        for name, key, default in MATERIAL_SETTINGS:
            parameter = builder.parm(name)
            if parameter is None:
                continue
            value = default.__class__(scalar(parameter))  # "type" is a local name here
            if value != default:
                settings[key] = value
        if settings:
            extra["settings"] = settings
    if expected == LIGHT_OUTPUT:
        from ..scene.light_shaders import _allowed

        unsupported = next((node["definition"] for node in nodes if node["definition"] not in _allowed()), None)
        if unsupported:
            raise ValueError("Unsupported node in Cycles Light Material: " + unsupported)
    return dict(
        schema_version=1,
        nodes=sorted(nodes, key=lambda item: item["id"]),
        links=links,
        **({domain: terminal} if terminal is not None else {}),
        **extra,
    )


def authoring_root(network):
    """Resolve a Cycles owner through an uninterrupted native subnet chain."""
    owner = network
    while owner is not None and owner.type().name() == SUBNET:
        owner = owner.parent()
    if owner is None or owner.type().name() not in (BUILDER, VOLUME_BUILDER, WORLD_BUILDER, LIGHT_BUILDER):
        raise ValueError("Choose a network inside a Cycles material or subnetwork")
    return owner


def _initialize_authoring_node(node, palette):
    # Native nodes have no Cycles create callback. This explicit creation path
    # supplies persistent identity and factory color once; loaded/user colors
    # are never revisited or rewritten by traversal, preview or extraction.
    if not node.userData("cycles_graph_id"):
        node.setUserData("cycles_graph_id", uuid.uuid4().hex)
        node.setColor(hou.Color(tuple(value / 255.0 for value in palette)))
    return node


def initialize_subnet(subnet, *, fresh_defaults=False):
    """Initialize an explicitly newly authored built-in Cycles group once."""
    if subnet.type().name() != SUBNET:
        raise ValueError("Choose a native subnet inside a Cycles material")
    root = authoring_root(subnet)
    if subnet.userData("cycles_graph_id"):
        return subnet
    if not subnet.setShaderLanguageName("Cycles"):
        raise ValueError("The Cycles shader language is unavailable")
    allowed = set()
    if root.type().name() == LIGHT_BUILDER:
        from ..scene.light_shaders import _allowed

        light = _allowed()
        allowed = {name for name, definition in _definitions().items() if definition["id"] in light}
    elif root.type().name() == WORLD_BUILDER:
        allowed = {name for name, definition in _definitions().items() if definition.get("world_graph_adapter")}
    else:
        allowed = {
            name
            for name, definition in _definitions().items()
            if definition["surface_graph_adapter"] or definition["volume_graph_adapter"]
        }
    allowed.update((SUBNET, CONNECTOR, *RAMPS))
    group = subnet.parmTemplateGroup()
    mask = hou.properties.parmTemplate("op", "tabmenumask")
    mask.setDefaultValue([" ".join(sorted(allowed))])
    if group.find(mask.name()) is None:
        group.append(mask)
    else:
        group.replace(mask.name(), mask)
    subnet.setParmTemplateGroup(group)
    if fresh_defaults:
        # genericTool runs Houdini's create script. Only its fresh stock
        # placeholders are removed; this branch is never used for loading.
        for name, kind in (("subinput1", "subinput"), ("suboutput1", "suboutput")):
            child = subnet.node(name)
            if child is not None and child.type().name() == kind:
                child.destroy()
    return _initialize_authoring_node(subnet, (55, 71, 37))


def initialize_connector(connector):
    if connector.type().name() != CONNECTOR or connector.parent().type().name() != SUBNET:
        raise ValueError("Choose a native connector inside a Cycles subnetwork")
    authoring_root(connector.parent())
    return _initialize_authoring_node(connector, (29, 29, 29))


def create_subnet(parent, name="cycles_subnetwork"):
    authoring_root(parent)
    with hou.undos.group("Create Cycles subnetwork"):
        subnet = parent.createNode(SUBNET, name, run_init_scripts=False, exact_type_name=True)
        return initialize_subnet(subnet)


def create_connector(subnet, name, kind="input", type="float", label=None, default=None):
    """Declare a native typed subnet port using the host-managed connector."""
    if subnet.type().name() != SUBNET or kind not in ("input", "output", "inputoutput"):
        raise ValueError("Choose a Cycles subnetwork and a supported connector kind")
    authoring_root(subnet)
    if not name.isascii() or not name.isidentifier() or type not in ("float", "bsdf", *_VECTOR_DEFAULTS):
        raise ValueError("Choose an identifier and a float, float3 or bsdf port type")
    for child in subnet.children():
        if child.type().name() != CONNECTOR or child.evalParm("parmname") != name:
            continue
        other = child.parm("connectorkind").evalAsString()
        if kind == "inputoutput" or other in (kind, "inputoutput"):
            raise ValueError("Subnet connector already exists: " + name)
    if type == "bsdf" and default is not None:
        raise ValueError("BSDF ports have an unconnected closure default")
    value = None
    if default is not None:
        value = float(default) if type == "float" else tuple(float(component) for component in default)
        if type != "float" and len(value) != 3:
            raise ValueError("A float3 port default requires three components")
    with hou.undos.group("Create Cycles subnet port"):
        connector = subnet.createNode(CONNECTOR, name + "_" + kind, run_init_scripts=False, exact_type_name=True)
        initialize_connector(connector)
        connector.parm("parmname").set(name)
        connector.parm("parmlabel").set(label or name)
        for field, token in (("connectorkind", kind), ("parmtype", type)):
            connector.parm(field).set(connector.parm(field).parmTemplate().menuItems().index(token))
        if value is not None:
            if type == "float":
                connector.parm("floatdef").set(value)
            else:
                connector.parmTuple(_VECTOR_DEFAULTS[type]).set(value)
    return connector


def create_subnet_tool(kwargs):
    """Keep native placement while sharing the explicit group initializer."""
    import toolutils
    import voptoolutils

    authoring_root(toolutils.activePane(kwargs).pwd())
    node = voptoolutils.genericTool(kwargs, SUBNET, "cycles_subnetwork")
    return initialize_subnet(node, fresh_defaults=True) if node is not None else None


def create_connector_tool(kwargs):
    import toolutils
    import voptoolutils

    parent = toolutils.activePane(kwargs).pwd()
    if parent.type().name() != SUBNET:
        raise ValueError("Create the connector inside a Cycles subnetwork")
    authoring_root(parent)
    node = voptoolutils.genericTool(kwargs, CONNECTOR, "cycles_connector")
    return initialize_connector(node) if node is not None else None


def create_material(parent=None, name="cycles_material", shader="principled_bsdf"):
    """Create a connected, editable native material using engine defaults."""
    volume = shader in ("principled_volume", "absorption_volume", "scatter_volume")
    if not volume and shader not in (
        "principled_bsdf",
        "diffuse_bsdf",
        "emission",
        "glass_bsdf",
        "transparent_bsdf",
        "holdout",
    ):
        raise ValueError("Choose a supported surface shader, principled_volume, absorption_volume or scatter_volume")
    parent = hou.node("/mat") if parent is None else parent
    with hou.undos.group("Create Cycles material"):
        builder = parent.createNode(VOLUME_BUILDER if volume else BUILDER, name)
        surface = builder.node("volume_shader" if volume else "surface_shader")
        output = builder.node("volume_output" if volume else "surface_output")
        if shader != ("principled_volume" if volume else "principled_bsdf"):
            surface.destroy()
            surface = builder.createNode(f"cycles::{shader}::1", "volume_shader" if volume else "surface_shader")
        closure = next(name for name, type in zip(surface.outputNames(), surface.outputDataTypes()) if type == "bsdf")
        output.setNamedInput("volume" if volume else "surface", surface, closure)
        if volume and shader != "principled_volume":
            field = builder.createNode("cycles::volume_info::1", "volume_fields")
            scale = builder.createNode("cycles::math::1", "density_scale")
            scale.parm("math_type").set("multiply")
            scale.parm("value2").set(1.0)
            scale.setNamedInput("value1", field, "density")
            surface.setNamedInput("density", scale, "value")
        builder.layoutChildren()
    return builder


def create_light_material(parent=None, name="cycles_light_material"):
    """Create a unity Emission network with the dedicated Light Output."""
    parent = hou.node("/mat") if parent is None else parent
    with hou.undos.group("Create Cycles light material"):
        builder = parent.createNode(LIGHT_BUILDER, name)
        builder.layoutChildren()
    return builder


def create_world_material(parent=None, name="cycles_world", texture=None, sky=False):
    """Create native editable world nodes; image color defaults to Houdini Auto."""
    parent = hou.node("/mat") if parent is None else parent
    if texture and sky:
        raise ValueError("Choose an image or sky preset; combine both explicitly inside a World Material")
    with hou.undos.group("Create Cycles world material"):
        builder = parent.createNode(WORLD_BUILDER, name)
        if texture:
            image = builder.createNode("cycles::environment_texture::1", "environment_texture")
            image.parm("filename").set(texture)
            builder.node("background_shader").setNamedInput("color", image, "color")
        elif sky:
            node = builder.createNode("cycles::sky_texture::1", "sky_texture")
            builder.node("background_shader").setNamedInput("color", node, "color")
        builder.layoutChildren()
    return builder


def promote_parameter(builder, node, socket, name=None, label=None):
    """Expose a native socket parameter and preserve its existing animation."""
    if builder.type().name() not in (BUILDER, VOLUME_BUILDER, WORLD_BUILDER, LIGHT_BUILDER, SUBNET):
        raise ValueError("Promote to a Cycles material or subnetwork")
    authoring_root(builder)
    ancestor = node.parent()
    while ancestor is not None and ancestor != builder and ancestor.type().name() == SUBNET:
        ancestor = ancestor.parent()
    if ancestor != builder or node.type().name() not in (*_definitions(), CONNECTOR, SUBNET, *RAMPS):
        raise ValueError("Promote a shader or connector inside this material/subnetwork")
    source = node.parmTuple(socket)
    if source is None:
        raise ValueError("This socket is a connection, not a promotable parameter")
    if socket in {connection.outputName() for connection in node.inputConnections()}:
        raise ValueError("Disconnect the socket before promoting its parameter")
    name = name or node.name() + "_" + socket
    if builder.parmTuple(name) is not None:
        raise ValueError("Material parameter already exists: " + name)
    if node.type().name() == COLOR_RAMP and socket == "ramp":
        from .ramp_promotion import promote

        return promote(builder, node, name, label)
    template = source.parmTemplate()
    template.setName(name)
    if label is not None:
        template.setLabel(label)
    group = builder.parmTemplateGroup()
    group.append(template)
    with hou.undos.group("Promote Cycles parameter"):
        builder.setParmTemplateGroup(group)
        target = builder.parmTuple(name)
        for destination, original in zip(target, source):
            destination.setFromParm(original)
            original.set(destination, follow_parm_reference=False)
    return target


__all__ = [
    "BUILDER",
    "SURFACE_OUTPUT",
    "VOLUME_BUILDER",
    "VOLUME_OUTPUT",
    "WORLD_BUILDER",
    "WORLD_OUTPUT",
    "LIGHT_BUILDER",
    "LIGHT_OUTPUT",
    "SUBNET",
    "CONNECTOR",
    "COLOR_RAMP",
    "RAMPS",
    "CURVES",
    "PRIVATE_MIX_TYPES",
    "execution_nodes",
    "MaterialSourceError",
    "material_graph",
    "authoring_root",
    "initialize_subnet",
    "initialize_connector",
    "create_subnet",
    "create_connector",
    "create_subnet_tool",
    "create_connector_tool",
    "create_material",
    "create_light_material",
    "create_world_material",
    "promote_parameter",
]
