# SPDX-License-Identifier: Apache-2.0
"""IPR-only DLSS menu: saved tokens remain editable on unsupported machines."""


def denoiser_menu(rop):
    import hou
    from . import device_selection
    from ..ipr import registry

    label = "DLSS Ray Reconstruction"
    try:
        selected = device_selection.selected_devices(rop, hou.time())
        if isinstance(selected, str):
            selected = [selected]
        entry = registry.get(f"rop:{rop.sessionId()}")
        controller = getattr(entry.session, "_controller", None) if entry else None
        # Menu generation never starts an NGX/device probe on the Houdini UI thread.
        # The renderer process supplies runtime support after initialization.
        if controller is not None:
            with controller.condition:
                available = controller.cached.get("devices", [])
                startup_failure = controller.cached.get("worker_startup_failure", "")
        else:
            available = []
            startup_failure = ""
        if startup_failure:
            return [
                "auto",
                "Automatic",
                "oidn",
                "OpenImageDenoise",
                "optix",
                "OptiX",
                "dlss",
                label + " (renderer startup failed; see render status)",
            ]
        if not available:
            return [
                "auto",
                "Automatic",
                "oidn",
                "OpenImageDenoise",
                "optix",
                "OptiX",
                "dlss",
                label + " (capability pending renderer probe)",
            ]
        supported = any(device["id"] in selected and "dlss" in device.get("denoisers", []) for device in available)
        if not supported:
            label += " (unavailable on selected devices/runtime)"
    except Exception:
        label += " (select an available RTX device)"
    return ["auto", "Automatic", "oidn", "OpenImageDenoise", "optix", "OptiX", "dlss", label]
