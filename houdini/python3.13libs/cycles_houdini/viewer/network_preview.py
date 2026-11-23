# SPDX-License-Identifier: Apache-2.0
"""Connect-to-output authoring through Houdini's documented network hook."""

import hou
from .. import materials as m

_PORT_MARK = "cycles_preview_output"
_BODY = ("node", "connectorarea", "preview", "footer")
_TYPES = {"float": "float", "int": "float", "vector": "color", "bsdf": "bsdf"}


def _builder(node):
    if not isinstance(node, hou.VopNode) or not (
        node.type().name().startswith("cycles::") or node.type().name() in (m.SUBNET, m.CONNECTOR)
    ):
        return None
    try:
        parent = m.authoring_root(node.parent())
    except ValueError:
        return None
    return parent if parent.type().name() in (m.BUILDER, m.WORLD_BUILDER, m.LIGHT_BUILDER) else None


def _candidates(node):
    if node.type().name() == m.CONNECTOR and node.parm("connectorkind").evalAsString() == "output":
        return []
    candidates = [i for i, type in enumerate(node.outputDataTypes()) if type in _TYPES]
    if node.type().name() == "cycles::vector_math::1":
        # Cycles exposes both sockets; only one is meaningful for each operation.
        socket = "value" if node.evalParm("math_type") in ("dot_product", "length", "distance") else "vector"
        candidates = [i for i in candidates if node.outputNames()[i] == socket]
    return candidates


def _output(builder):
    kind = {m.WORLD_BUILDER: m.WORLD_OUTPUT, m.LIGHT_BUILDER: m.LIGHT_OUTPUT}.get(
        builder.type().name(), m.SURFACE_OUTPUT
    )
    outputs = [n for n in builder.children() if n.type().name() == kind]
    if len(outputs) != 1:
        raise ValueError("Choose exactly one Cycles material output before previewing a node")
    return outputs[0]


def _current_source(output):
    incoming = [c for c in output.inputConnections() if c.outputName() == "surface"]
    if len(incoming) != 1:
        return None
    source, index = m._source(incoming[0])
    visited = set()
    while source.type().name() == m.SUBNET:
        key = source, index
        if key in visited:
            return None
        visited.add(key)
        name = source.outputNames()[index]
        ports = [
            n
            for n in source.children()
            if n.type().name() == m.CONNECTOR and n.userData(_PORT_MARK) == "1" and name in n.outputNames()
        ]
        if len(ports) != 1 or len(ports[0].inputConnections()) != 1:
            break
        source, index = m._source(ports[0].inputConnections()[0])
    return source, index


def connect_to_output(node, output_index=None):
    """One undoable connection; repeated node clicks cycle compatible outputs."""
    builder = _builder(node)
    if builder is None:
        raise ValueError("Choose a node inside a Cycles Surface or World Material")
    candidates = _candidates(node)
    if not candidates:
        raise ValueError("This node has no shader, color, vector or value output")
    output = _output(builder)
    current = _current_source(output)
    if output_index is None:
        index = (
            candidates[(candidates.index(current[1]) + 1) % len(candidates)]
            if current and current[0] == node and current[1] in candidates
            else candidates[0]
        )
    else:
        if output_index not in candidates:
            raise ValueError("This output cannot be used for a Cycles surface preview")
        index = output_index
    original_index = index
    parents = []
    parent = node.parent()
    while parent != builder:
        ports = [n for n in parent.children() if n.type().name() == m.CONNECTOR and n.userData(_PORT_MARK) == "1"]
        if len(ports) > 1:
            raise ValueError("Ambiguous Cycles preview ports in " + parent.path())
        port = ports[0] if ports else None
        if port is not None and port.parm("connectorkind").evalAsString() != "output":
            raise ValueError("Cycles preview port was changed to an input: " + port.path())
        if not parent.isEditable():
            raise hou.PermissionError("The preview network is locked: " + parent.path())
        parents.append((parent, port))
        parent = parent.parent()
    if not builder.isEditable():
        raise hou.PermissionError("The preview material is locked: " + builder.path())
    with hou.undos.group("Preview Cycles Node Output"):
        source = node
        socket = source.outputNames()[index]
        parent = source.parent()
        type = _TYPES[source.outputDataTypes()[index]]
        for parent, port in parents:
            if port is not None:
                parm = port.parm("parmtype")
                parm.set(parm.parmTemplate().menuItems().index(type))
            else:
                used = {n.evalParm("parmname") for n in parent.children() if n.type().name() == m.CONNECTOR}
                name = "cycles_preview"
                suffix = 1
                while name in used:
                    suffix += 1
                    name = "cycles_preview_" + str(suffix)
                port = m.create_connector(parent, name, kind="output", type=type, label="Preview")
                port.setUserData(_PORT_MARK, "1")
                port.setPosition(source.position() + hou.Vector2(2, -1))
            port.setInput(0, source, index)
            socket = port.outputNames()[0]
            source = parent
            parent = source.parent()
            index = source.outputNames().index(socket)
        output.setNamedInput("surface", source, socket)
    return original_index


def create_event_handler(event, pending_actions):
    if (
        event.eventtype != "mousedown"
        or not event.mousestate.lmb
        or event.mousestate.mmb
        or event.mousestate.rmb
        or event.modifierstate.alt
        or not event.modifierstate.ctrl
        or not event.modifierstate.shift
    ):
        return None, False
    component = event.selected
    node = component.item
    if (
        not isinstance(node, hou.VopNode)
        or _builder(node) is None
        or not _candidates(node)
        or component.name not in (*_BODY, "output")
    ):
        return None, False
    if event.editor.pwd() != node.parent():
        return None, False
    explicit = component.index if component.name == "output" else None
    from nodegraphbase import EventHandler

    class PreviewClick(EventHandler):
        def __init__(self, start):
            super().__init__(start)
            self.dragged = False

        def handleEvent(self, current, actions):
            if current.eventtype in ("context", "contextclear"):
                return None
            if current.eventtype == "keyhit" and current.key in ("Esc", "Escape"):
                return None
            try:
                node.path()
            except hou.ObjectWasDeleted:
                return None
            if current.eventtype == "mousedrag":
                self.dragged |= current.dragging
            if current.eventtype == "mouseup":
                if not self.dragged and current.located.item == node and current.editor.pwd() == node.parent():
                    try:
                        index = connect_to_output(node, explicit)
                        hou.ui.setStatusMessage("Cycles preview: " + node.path() + " / " + node.outputNames()[index])
                    except (ValueError, hou.OperationFailed, hou.PermissionError) as error:
                        hou.ui.setStatusMessage(str(error), severity=hou.severityType.Warning)
                return None
            return self

    return PreviewClick(event), True


def install():
    """Chain the public hook; keep existing artist/studio handlers available."""
    import nodegraphhooks

    previous = nodegraphhooks.createEventHandler
    if getattr(previous, "cycles_preview_hook", False):
        return

    def dispatch(event, actions):
        handler, handled = create_event_handler(event, actions)
        return (handler, handled) if handled else previous(event, actions)

    dispatch.cycles_preview_hook = True
    nodegraphhooks.createEventHandler = dispatch
