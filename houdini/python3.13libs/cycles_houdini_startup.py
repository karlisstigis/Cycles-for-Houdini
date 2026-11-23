# SPDX-License-Identifier: Apache-2.0
"""Non-modal automatic startup boundary, independent of the native package.

A failed package import must not make every loaded ROP repeat that import and
dialog. Explicit rendering still raises the original SDK dependency failure.
"""

import sys
import os

_startup_error = None
_NODE_ERROR = "cycles_startup_error"


def startup_error():
    return _startup_error


def _node_error(node, message):
    if node is not None:
        if message:
            node.setCachedUserData(_NODE_ERROR, message)
        else:
            node.destroyCachedUserData(_NODE_ERROR, must_exist=False)


def _run(action, node=None):
    global _startup_error
    if _startup_error is not None:
        _node_error(node, _startup_error)
        return False
    try:
        action()
    except Exception as error:
        # A failed package import leaves its successfully imported dependency
        # module cached. Catch only that module's explicit failure type.
        dependencies = sys.modules.get("cycles_houdini.core.sdk_dependencies") or sys.modules.get(
            "cycles_houdini.sdk_dependencies"
        )
        error_type = getattr(dependencies, "SDKDependencyError", None)
        if error_type is None or not isinstance(error, error_type):
            raise
        _startup_error = str(error)
        _node_error(node, _startup_error)
        message = "Cycles renderer unavailable: " + _startup_error
        print(message, file=sys.stderr)
        import hou

        if hou.isUIAvailable():
            try:
                hou.ui.setStatusMessage(message, severity=hou.severityType.Error)
            except hou.NotAvailable:
                # OnLoaded may precede the status bar. The logged failure and
                # latch already exist; do not create a replacement callback.
                pass
        return False
    _node_error(node, None)
    return True


def initialize_rop(node, *, created=False):
    def initialize():
        from cycles_houdini.rop import final_render
        from cycles_houdini.core import device_selection
        from cycles_houdini.scene import object_properties
        from cycles_houdini.rop.output import ensure_viewer_resolution_precision
        from cycles_houdini.rop.rop_layout import ensure_ipr_settings_layout

        ensure_viewer_resolution_precision(node)
        ensure_ipr_settings_layout(node)
        final_render.register(node)
        object_properties.install_scene_refresh()
        device_selection.initialize(node, created=created)
        # Saved per-node layouts retain this internal channel's old default.
        # SOHO tokenizes an unquoted absolute path at spaces; its own script
        # search path already contains this host's soho directory.
        program = node.parm("soho_program")
        if program is not None and program.unexpandedString() != "CHRender.py":
            program.set("CHRender.py")
        # Final SOHO output belongs in a log, not the artist's console. Native
        # IPR supplies its own pipe-mode overrides when starting the viewer.
        mode = node.parm("soho_outputmode")
        if mode is not None and mode.eval() != 1:
            mode.set(1)
        log = node.parm("soho_diskfile")
        if log is not None:
            log.set(f"$HOUDINI_TEMP_DIR/cycles-{os.getpid()}-{node.sessionId()}-$F4.log")

    return _run(initialize, node)


# Marks the hip-file callback below, so a reloaded module never registers a second one.
_MOVE_OFFER_CALLBACK = "cycles_houdini.move_offer"


def _offer_move_after_save(event_type):
    """hou.hipFile event callback: after a save, offer Move Imported Assets into Project
    (move_project.maybe_offer_move asks once per .hip and never moves by itself; blend-import.md: Collect and relink).
    Nothing may escape into Houdini's save."""
    try:
        import hou

        if event_type != hou.hipFileEventType.AfterSave or not hou.isUIAvailable():
            return
        from cycles_houdini.assets import move_project

        move_project.maybe_offer_move()
    except Exception as error:  # noqa: BLE001 - a failed offer must never fail the save
        print("Cycles: Move into Project offer skipped: " + str(error), file=sys.stderr)


_offer_move_after_save.cycles_houdini_callback = _MOVE_OFFER_CALLBACK


def install_move_offer():
    """Register _offer_move_after_save once per session (UI sessions only). True when added."""
    try:
        import hou

        if not hou.isUIAvailable():
            return False
        for callback in hou.hipFile.eventCallbacks():
            if getattr(callback, "cycles_houdini_callback", None) == _MOVE_OFFER_CALLBACK:
                return False
        hou.hipFile.addEventCallback(_offer_move_after_save)
        return True
    except Exception as error:  # noqa: BLE001 - the offer is optional; the UI start-up goes on
        print("Cycles: Move into Project offer not installed: " + str(error), file=sys.stderr)
        return False


def hide_native_reference():
    """Hide cycles::blender_reference from the Tab menu once the Cycles Blender File asset is
    installed (the type stays creatable for old scenes and inside the asset; blend-import.md: Asset nodes). UI start-up
    only (uiready.py): hython and other no-UI sessions keep the menu unchanged, which nothing
    there uses. Never raises. Returns True when hidden."""
    try:
        from cycles_houdini.blender import blend_file_node

        return bool(blend_file_node.hide_native_reference())
    except Exception as error:  # noqa: BLE001 - the Tab menu entry is cosmetic; start-up goes on
        print("Cycles: Blender Reference not hidden from the Tab menu: " + str(error), file=sys.stderr)
        return False


def install_ui():
    def install():
        from cycles_houdini.viewer.network_preview import install as install_preview
        from cycles_houdini.materials.material_previews import install as install_materials

        install_preview()
        install_materials()
        install_move_offer()

    installed = _run(install)
    hide_native_reference()
    return installed
