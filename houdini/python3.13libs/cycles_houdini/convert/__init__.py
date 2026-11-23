# SPDX-License-Identifier: Apache-2.0
"""Convert Houdini materials to Cycles materials.

Right-click a Principled Shader, a MaterialX Standard Surface / OpenPBR
Surface, or a material builder holding one, and choose Cycles > Convert to
Cycles Material. Each source gets a new ``cycles::material::1`` beside it,
named ``<source>_cycles``; the source is not changed and nothing is
reassigned. What could not be carried is written into the new material's
comment (shown in the network editor) and returned to scripts.

Scripts::

    from cycles_houdini import convert
    for result in convert.convert([hou.node("/mat/principledshader1")]):
        print(result.material.path(), result.message)

Importing this package does not import ``hou``; the mapping modules
(``plan``, ``principled``, ``materialx``, ``spaces``) are pure Python.
"""

from dataclasses import dataclass, field
from importlib import import_module


@dataclass
class Conversion:
    source: str  # path of the converted shader
    material: object  # the new cycles::material::1 node
    message: str = ""  # empty when everything was carried
    not_carried: list = field(default_factory=list)
    approximated: list = field(default_factory=list)


def can_convert(node):
    """True for a node that Convert to Cycles Material accepts."""
    try:
        from . import host

        return host.kind_of(node) is not None
    except Exception:
        return False


def convert(nodes):
    """Convert each node (shader or material builder); returns a Conversion per new material."""
    hou = import_module("hou")
    from . import host

    results, seen = [], set()
    with hou.undos.group("Convert to Cycles Material"):
        for node in nodes:
            for surface, displacement, anchor, network, name in host.sources(node):
                if surface.path() in seen:
                    continue
                seen.add(surface.path())
                plan = host.plan_for(surface, displacement, anchor.path())
                material = host.build(plan, network, name, anchor)
                results.append(
                    Conversion(
                        source=surface.path(),
                        material=material,
                        message=plan.message(),
                        not_carried=list(plan.not_carried),
                        approximated=list(plan.approximated),
                    )
                )
    return results


# -- node menu (houdini/OPmenu.xml) -------------------------------------------------------
def menu_nodes(kwargs=None):
    """The clicked node, or the selection when the clicked node is part of it, that can be converted."""
    try:
        hou = import_module("hou")
        clicked = (kwargs or {}).get("node")
        selected = list(hou.selectedNodes())
        nodes = selected if clicked is None or clicked in selected else [clicked]
        return [node for node in nodes if can_convert(node)]
    except Exception:
        return []


def menu_visible(kwargs=None):
    """OPmenu context expression: never raises."""
    try:
        return bool(menu_nodes(kwargs))
    except Exception:
        return False


def convert_selected(kwargs=None):
    """OPmenu script: convert, then say in the status bar what happened."""
    hou = import_module("hou")
    results = convert(menu_nodes(kwargs))
    incomplete = [result for result in results if result.message]
    for result in incomplete:
        print("Cycles: " + result.material.path() + "\n" + result.message)
    if not results:
        text, severity = "Cycles: nothing to convert", hou.severityType.Warning
    elif incomplete:
        names = ", ".join(result.material.name() for result in incomplete)
        text = f"Cycles: converted {len(results)} material(s); not everything was carried for {names} (see the node comment)"
        severity = hou.severityType.Warning
    else:
        text = f"Cycles: converted {len(results)} material(s)"
        severity = hou.severityType.Message
    try:
        if hou.isUIAvailable():
            hou.ui.setStatusMessage(text, severity=severity)
            if len(results) == 1:
                results[0].material.setSelected(True, clear_all_selected=True)
    except Exception:
        pass
    return results
