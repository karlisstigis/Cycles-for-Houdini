# SPDX-License-Identifier: Apache-2.0
"""Callbacks for the Cycles menus.

Host package (blend-import.md: Host menus): the network editor's Cycles menu (houdini/NetworkViewMenu.xml) and
the node right-click Cycles submenu (houdini/OPmenu.xml): Collect Textures, Relink,
Move into Project, Make Editable and Reload for linked materials and worlds (linked
Environment Lights in /obj, linked World Materials in /mat). Their items are
active only when the Blender bridge component is enabled. Asset browser add-on:
Cycles > Asset Browser (addons/asset_browser/MainMenuCommon.xml) and the shelf.

Menu context expressions call these on every menu open, so they are cheap and
never raise: every HOM call is guarded and a failure means "not shown".
"""

PANEL_INTERFACE = "cycles_houdini_assets"
# Id of the menu the "Cycles" menus are inserted before (MainMenuCommon.xml and NetworkViewMenu.xml
# <insertBefore>). SideFX Labs also inserts before Help, so the two menus sit side by side. Checked in
# Houdini 22.0: <insertAfter>labs_menu</insertAfter> lands after Help, because Labs' menu file loads later.
HELP_MENU_ID = "help_menu"


def _hou():
    try:
        import hou

        return hou
    except Exception:
        return None


def _selected(hou):
    try:
        return list(hou.selectedNodes())
    except Exception:
        return []


def _is_linked(node):
    try:
        from ..blender.blend_library_materials import link_info

        return link_info(node) is not None
    except Exception:
        return False


def _is_linked_world(node):
    try:
        from ..blender.blend_library_materials import link_info

        return (link_info(node) or {}).get("kind") == "world"
    except Exception:
        return False


def _editor_pwd(kwargs):
    editor = (kwargs or {}).get("editor")
    try:
        return editor.pwd() if editor is not None else None
    except Exception:
        return None


def bridge_enabled():
    """True when the Blender bridge component is enabled (every Cycles network/node menu item needs it)."""
    try:
        from ..core import blender_bridge

        return bool(blender_bridge.enabled())
    except Exception:
        return False


def _network_kind(kwargs):
    """ "vop", "obj" or "sop" for the network the editor shows, else None."""
    hou = _hou()
    pwd = _editor_pwd(kwargs)
    if hou is None or pwd is None:
        return None
    try:
        category = pwd.childTypeCategory()
    except Exception:
        return None
    for kind in ("vop", "obj", "sop"):
        getter = getattr(hou, kind + "NodeTypeCategory", None)
        try:
            if callable(getter) and category == getter():
                return kind
        except Exception:
            continue
    return None


def in_material_network(kwargs=None):
    """Network-menu context: True when the network editor shows a material (VOP) network, like /mat."""
    hou = _hou()
    pwd = _editor_pwd(kwargs)
    try:
        return hou is not None and pwd is not None and pwd.childTypeCategory() == hou.vopNodeTypeCategory()
    except Exception:
        return False


def network_has_linked(kwargs=None):
    """Network-menu item context: in a material network with linked materials or worlds selected
    or shown, or in /obj with a linked Environment Light selected."""
    if not bridge_enabled():
        return False
    kind = _network_kind(kwargs)
    if kind == "vop":
        return has_linked(kwargs)
    if kind == "obj":
        try:
            return any(_is_linked_world(node) for node in linked_candidates(kwargs))
        except Exception:
            return False
    return False


def network_any_linked(kwargs=None):
    """Network-menu item context: in a material network with any linked asset in the scene, or
    in /obj with any linked Environment Light or World Material."""
    if not bridge_enabled():
        return False
    kind = _network_kind(kwargs)
    if kind == "vop":
        return any_linked_in_scene(kwargs)
    if kind == "obj":
        try:
            from .importers import all_linked_worlds

            return bool(all_linked_worlds())
        except Exception:
            return False
    return False


def in_collect_network(kwargs=None):
    """Network-menu item context for Collect Textures for All: /mat-like (VOP) networks and /obj."""
    return bridge_enabled() and _network_kind(kwargs) in ("vop", "obj")


def in_asset_network(kwargs=None):
    """Network-menu item context for Relink and Move into Project: VOP networks, /obj and SOP networks."""
    return bridge_enabled() and _network_kind(kwargs) in ("vop", "obj", "sop")


def _collectable(node):
    try:
        from .scene_paths import is_builder, is_envlight, is_texture_node

        return is_builder(node) or is_texture_node(node) or is_envlight(node)
    except Exception:
        return False


def collect_candidates(kwargs=None):
    """Materials, worlds, texture nodes and Environment Lights among the clicked node and the selection."""
    hou = _hou()
    if hou is None:
        return []
    nodes = []
    for candidate in [(kwargs or {}).get("node"), *_selected(hou)]:
        if candidate is not None and candidate not in nodes and _collectable(candidate):
            nodes.append(candidate)
    return nodes


def network_can_collect_selected(kwargs=None):
    """Network-menu item context for Collect Textures for Selected."""
    return in_collect_network(kwargs) and bool(collect_candidates(kwargs))


def node_can_collect(kwargs=None):
    """OPmenu item context: the clicked or selected nodes include something to collect."""
    return bridge_enabled() and bool(collect_candidates(kwargs))


def node_menu(kwargs=None):
    """OPmenu context of the whole Cycles submenu: Blender bridge on and something to act on."""
    try:
        return bridge_enabled() and (bool(collect_candidates(kwargs)) or has_linked(kwargs))
    except Exception:
        return False


def linked_candidates(kwargs=None):
    """Linked materials and worlds among the right-clicked node, the network shown and the selection."""
    hou = _hou()
    if hou is None:
        return []
    nodes = []
    node = (kwargs or {}).get("node")
    pwd = _editor_pwd(kwargs)  # inside a linked material's own network
    for candidate in [n for n in (node, pwd) if n is not None] + _selected(hou):
        try:
            if candidate is not None and candidate not in nodes and _is_linked(candidate):
                nodes.append(candidate)
        except Exception:
            continue
    return nodes


def has_linked(kwargs=None):
    """OPmenu context expression: True when the node or any selected node is a linked material,
    Environment Light or World Material."""
    try:
        return bool(linked_candidates(kwargs))
    except Exception:
        return False


def any_linked_in_scene(kwargs=None):
    """True when the scene holds any linked material or world."""
    try:
        from .importers import all_linked

        return bool(all_linked())
    except Exception:
        return False


def _report(text, error=False):
    hou = _hou()
    try:
        if hou is not None and hou.isUIAvailable():
            severity = hou.severityType.Warning if error else hou.severityType.Message
            hou.ui.setStatusMessage("Cycles: " + text, severity=severity)
            return
    except Exception:
        pass
    print("Cycles: " + text)


def append_selected(kwargs=None):
    """Make editable every linked material or world among the clicked/selected nodes; one undo step."""
    hou = _hou()
    nodes = linked_candidates(kwargs)
    if hou is None or not nodes:
        _report("No linked Blender assets selected.", True)
        return []
    from ..blender.blend_library_materials import unmark_linked

    with hou.undos.group("Make linked assets editable"):
        for node in nodes:
            unmark_linked(node)
    _report(f"Made {len(nodes)} linked asset(s) editable.")
    return nodes


def reload_selected(kwargs=None):
    """Reload the clicked/selected linked materials and worlds from their library in the background."""
    from .importers import reload_linked_async

    return reload_linked_async(linked_candidates(kwargs), report=_report)


def reload_all(kwargs=None):
    """Reload every linked material (/mat) and world (/obj, /mat) in the background."""
    from .importers import all_linked, reload_linked_async

    nodes = all_linked()
    if not nodes:
        _report("There are no linked Blender assets in the scene.")
        return []
    return reload_linked_async(nodes, report=_report)


def collect_selected(kwargs=None):
    """Collect Textures for the clicked/selected materials, worlds and Environment Lights."""
    from .collect import CollectError, collect_textures

    nodes = collect_candidates(kwargs)
    if not nodes:
        _report("Select Cycles materials, World Materials or Environment Lights to collect.", True)
        return None
    try:
        return collect_textures(nodes)
    except CollectError:
        return None


def collect_all(kwargs=None):
    """Collect Textures for every Cycles material, world and Environment Light in the scene."""
    from .collect import CollectError, collect_textures

    try:
        return collect_textures(None)
    except CollectError:
        return None


def relink_assets(kwargs=None):
    """Relink Blender Assets...: pick a folder and repair missing paths."""
    from .relink import relink_blender_assets

    return relink_blender_assets(kwargs)


def move_into_project(kwargs=None):
    """Move Imported Assets into Project: user-cache blend folders into $HIP/assets_blender."""
    from .move_project import move_imported_assets_into_project

    return move_imported_assets_into_project()


def open_browser(name=PANEL_INTERFACE):
    """Show the asset browser pane tab: reuse an open one, else dock beside the network editor."""
    import hou

    interface = hou.pypanel.interfaceByName(name)
    if interface is None:
        raise hou.Error(
            "The Cycles Asset Browser panel is not installed. Run Cycles Setup and select the "
            "Asset browser component (it requires the Blender bridge)."
        )
    tab = None
    for candidate in hou.ui.paneTabs():
        if candidate.type() == hou.paneTabType.PythonPanel:
            active = candidate.activeInterface()
            if active is not None and active.name() == name:
                tab = candidate
                break
    if tab is None:
        # Dock beside the network editor of the current desktop; float only when
        # the desktop has no pane to dock into.
        anchor = hou.ui.paneTabOfType(hou.paneTabType.NetworkEditor)
        if anchor is None:
            tabs = hou.ui.paneTabs()
            anchor = tabs[0] if tabs else None
        if anchor is not None and anchor.pane() is not None:
            tab = anchor.pane().createTab(hou.paneTabType.PythonPanel)
        else:
            tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel)
        tab.setActiveInterface(interface)
    tab.setIsCurrentTab()
    return tab
