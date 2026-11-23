# SPDX-License-Identifier: Apache-2.0
"""Houdini side of material conversion: read source shaders, build Cycles materials."""

from importlib import import_module

from .materialx import SURFACES, plan_materialx
from .plan import ColorSpace, Socket
from .principled import plan_principled
from .spaces import resolve

PRINCIPLED = "principledshader"
DISPLACEMENTS = ("mtlxdisplacement",)
OFFSET = (3.0, 0.0)  # where the new builder goes, relative to its source, in network units


def _hou():
    return import_module("hou")


def short_type(node):
    """'principledshader::2.0' -> 'principledshader'; 'mtlximage' stays."""
    return node.type().name().split("::")[0]


def kind_of(node):
    """'principled', 'materialx', 'container' or None, without cooking anything."""
    kind = short_type(node)
    if kind == PRINCIPLED:
        return "principled"
    if kind in SURFACES:
        return "materialx"
    hou = _hou()
    if isinstance(node, hou.VopNode) and node.isNetwork() and not kind.startswith("cycles"):
        try:
            if any(short_type(child) in SURFACES or short_type(child) == PRINCIPLED for child in node.children()):
                return "container"
        except Exception:
            return None
    return None


def _anchor(node):
    """The outermost VOP container holding ``node`` (or the node itself) and its network."""
    hou = _hou()
    anchor = node
    while isinstance(anchor.parent(), hou.VopNode):
        anchor = anchor.parent()
    return anchor, anchor.parent()


def _upstream(connection):
    try:
        return connection.inputNode()
    except Exception:
        return None


def container_sources(container):
    """[(surface, displacement or None)] wired to the container's outputs, else every surface inside."""
    surfaces, displacement, pairs = [], None, []
    for child in container.children():
        kind = short_type(child)
        if kind == "mtlxsurfacematerial":
            surface = shader = None
            for connection in child.inputConnections():
                upstream = _upstream(connection)
                if upstream is None:
                    continue
                if short_type(upstream) in SURFACES:
                    surface = upstream
                elif short_type(upstream) in DISPLACEMENTS:
                    shader = upstream
            if surface is not None:
                pairs.append((surface, shader))
        elif kind in ("suboutput", "subnetconnector", "output"):
            for connection in child.inputConnections():
                upstream = _upstream(connection)
                if upstream is None:
                    continue
                if short_type(upstream) in SURFACES or short_type(upstream) == PRINCIPLED:
                    surfaces.append(upstream)
                elif short_type(upstream) in DISPLACEMENTS:
                    displacement = upstream
    for surface in surfaces:
        if surface not in [pair[0] for pair in pairs]:
            pairs.append((surface, displacement if len(surfaces) == 1 else None))
    if not pairs:
        pairs = [
            (child, None)
            for child in container.children()
            if short_type(child) in SURFACES or short_type(child) == PRINCIPLED
        ]
    return pairs


def sources(node):
    """[(shader node, displacement node or None, anchor, network, name)] that converting ``node`` creates.

    The new material lives in the network that holds the outermost container
    (``anchor``), named after it: ``<anchor>_cycles``, or
    ``<anchor>_<surface>_cycles`` when the container holds several surfaces.
    """
    kind = kind_of(node)
    if kind is None:
        return []
    anchor, network = _anchor(node)
    if kind == "container":
        pairs = container_sources(node)
    elif node.parent() is network:
        pairs = [(node, None)]
    else:
        pairs = [(node, dict(container_sources(node.parent())).get(node))]
    siblings = len(container_sources(anchor)) if anchor is not pairs[0][0] and kind_of(anchor) == "container" else 1
    result = []
    for surface, displacement in pairs:
        if anchor is surface:
            name = surface.name()
        elif max(siblings, len(pairs)) > 1:
            name = anchor.name() + "_" + surface.name()
        else:
            name = anchor.name()
        result.append((surface, displacement, anchor, network, name + "_cycles"))
    return result


# -- snapshots -------------------------------------------------------------------------
def _value(parm_tuple):
    hou = _hou()
    template = parm_tuple.parmTemplate()
    if template.type() == hou.parmTemplateType.String:
        parm = parm_tuple[0]
        try:
            return parm.unexpandedString()  # keeps $HIP, $JOB, <UDIM> and backtick expressions
        except Exception:
            return parm.evalAsString()
    values = parm_tuple.eval()
    return values[0] if len(values) == 1 else tuple(values)


def _driven(parm_tuple):
    """Animated (time dependent): the conversion copies the value at the current frame."""
    hou = _hou()
    if parm_tuple.parmTemplate().type() == hou.parmTemplateType.String:
        return False
    for parm in parm_tuple:
        try:
            if parm.keyframes() and parm.isTimeDependent():
                return True
        except Exception:
            continue
    return False


def _parms(node):
    values, driven = {}, []
    for parm_tuple in node.parmTuples():
        try:
            values[parm_tuple.name()] = _value(parm_tuple)
            if _driven(parm_tuple):
                driven.append(parm_tuple.name())
        except Exception:
            continue
    return values, driven


def _inputs(node):
    inputs = {}
    names = node.inputNames()
    for connection in node.inputConnections():
        upstream = _upstream(connection)
        if upstream is None:
            continue
        index = connection.inputIndex()
        name = names[index] if index < len(names) else str(index)
        outputs = upstream.outputNames()
        output = outputs[connection.outputIndex()] if connection.outputIndex() < len(outputs) else "out"
        inputs[name] = [upstream.path(), output]
    return inputs


def snapshot_principled(node):
    hou = _hou()
    parms, driven = _parms(node)
    names = node.inputNames()
    connected = [names[c.inputIndex()] for c in node.inputConnections() if c.inputIndex() < len(names)]
    return dict(name=node.path(), parms=parms, connected=connected, animated=driven, frame=hou.frame())


def snapshot_materialx(surface, displacement, name):
    hou = _hou()
    nodes, pending, animated = {}, [n for n in (surface, displacement) if n is not None], []
    while pending:
        node = pending.pop()
        if node.path() in nodes:
            continue
        parms, driven = _parms(node)
        animated += [node.name() + "." + parm for parm in driven]
        nodes[node.path()] = dict(type=node.type().name(), name=node.name(), parms=parms, inputs=_inputs(node))
        for connection in node.inputConnections():
            upstream = _upstream(connection)
            if upstream is not None and upstream.path() not in nodes:
                pending.append(upstream)
    return dict(
        name=name,
        surface=surface.path(),
        displacement=displacement.path() if displacement is not None else None,
        nodes=nodes,
        animated=animated,
        frame=hou.frame(),
    )


def plan_for(surface, displacement, label):
    if short_type(surface) == PRINCIPLED:
        return plan_principled(snapshot_principled(surface))
    graph = snapshot_materialx(surface, displacement, label)
    plan = plan_materialx(graph)
    for name in graph["animated"]:
        plan.approx(name, "animated or driven by an expression; the current value was copied")
    return plan


# -- building ----------------------------------------------------------------------
def _set(node, name, value):
    parm_tuple = node.parmTuple(name)
    if parm_tuple is None:
        raise ValueError("Cycles parameter is missing: " + node.path() + "/" + name)
    if isinstance(value, ColorSpace):
        value = resolve(value, parm_tuple[0].menuItems())
    if isinstance(value, tuple):
        parm_tuple.set(value[: len(parm_tuple)])
    else:
        parm_tuple[0].set(value)


def _output(node, socket):
    names = node.outputNames()
    if socket.output in names:
        return socket.output
    if socket.kind == "closure":
        return next(name for name, kind in zip(names, node.outputDataTypes()) if kind in ("bsdf", "closure", "shader"))
    raise ValueError("Cycles output is missing: " + node.path() + "/" + socket.output)


def _free_position(network, position):
    hou = _hou()
    taken = [child.position() for child in network.children()]
    x, y = position
    while any(abs(p[0] - x) < 1.0 and abs(p[1] - y) < 0.6 for p in taken):
        y -= 1.0
    return hou.Vector2(x, y)


def build(plan, network, name, anchor):
    """Create the planned material in ``network`` next to ``anchor``; returns the builder."""
    hou = _hou()
    from .. import materials

    builder = materials.create_material(parent=network, name=name)
    made = {"surface": builder.node("surface_shader"), "output": builder.node("surface_output")}
    for key, item in plan.nodes.items():
        if key not in made:
            made[key] = builder.createNode("cycles::" + item.type + "::1", item.name)
    for key, item in plan.nodes.items():
        for parm, value in item.parms.items():
            _set(made[key], parm, value)
    for key, input, socket in plan.links:
        target, source = made[key], made[socket.node]
        target.setNamedInput(input, source, _output(source, socket))
    for parm, value in plan.builder.items():
        _set(builder, parm, value)
    builder.layoutChildren()
    position = anchor.position()
    builder.setPosition(_free_position(network, (position[0] + OFFSET[0], position[1] + OFFSET[1])))
    builder.setUserData("cycles_converted_from", anchor.path())
    message = plan.message()
    if message:
        builder.setComment(message)
        builder.setGenericFlag(hou.nodeFlag.DisplayComment, True)
    return builder


def links_of(plan):
    """(target key, input, source key, output) tuples, for tests and diagnostics."""
    return [(key, input, socket.node, socket.output) for key, input, socket in plan.links if isinstance(socket, Socket)]
