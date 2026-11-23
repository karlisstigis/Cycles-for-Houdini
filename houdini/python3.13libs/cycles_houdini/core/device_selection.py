# SPDX-License-Identifier: Apache-2.0
"""Native ROP device checkboxes, persisted by physical GPU identity."""

import hashlib
import hou
from .. import devices, default_device

_PHYSICAL = "cycles_gpu_physical_id"
_NAME = "cycles_gpu_name"
_device_cache = None
_selection_layouts = {}


def _selection_layout_changed(node, event_type, **kwargs):
    identity = node.sessionId()
    if event_type == hou.nodeEventType.BeingDeleted:
        _selection_layouts.pop(identity, None)
    elif identity in _selection_layouts:
        _selection_layouts[identity] = None


def _selection_layout(rop):
    """Cache immutable device descriptors, never evaluated checkbox values.

    Houdini invalidates this on spare-layout changes (including definition
    refresh), and deletes it with the node. Expressions are evaluated below on
    every query, so camera/object-dependent device selections remain live.
    """
    identity = rop.sessionId()
    if identity not in _selection_layouts:
        rop.addEventCallback(
            (hou.nodeEventType.SpareParmTemplatesChanged, hou.nodeEventType.BeingDeleted), _selection_layout_changed
        )
        _selection_layouts[identity] = None
    if _selection_layouts[identity] is None:
        _selection_layouts[identity] = tuple(
            (parm.name(), parm.tags()[_PHYSICAL], parm.tags().get(_NAME, parm.tags()[_PHYSICAL]))
            for parm in gpu_parameters(rop)
        )
    return _selection_layouts[identity]


def _available():
    global _device_cache
    if _device_cache is None:
        _device_cache = devices()
    result = {}
    for device in _device_cache:
        if device["backend"] not in ("CUDA", "OPTIX"):
            continue
        identity = device["physical_id"]
        gpu = result.setdefault(identity, dict(name=device["name"], backends={}))
        gpu["backends"][device["backend"]] = device["id"]
    return result


def _gpu_folder(group):
    # Houdini rewrites folder names (including Simple folders) when serializing
    # HDAs and spare parameter layouts. Ownership tags survive those rewrites.
    candidates = []

    def visit(parms):
        for parm in parms:
            if isinstance(parm, hou.FolderParmTemplate):
                if parm.tags().get("cycles_device_list") == "1":
                    candidates.append(parm)
                visit(parm.parmTemplates())

    visit(group.entries())
    if len(candidates) > 1:
        raise ValueError("The Cycles ROP has more than one GPU device list")
    return candidates[0] if candidates else None


def gpu_parameters(rop):
    folder = _gpu_folder(rop.parmTemplateGroup())
    return tuple(parm for parm in folder.parmTemplates() if parm.tags().get(_PHYSICAL)) if folder else ()


def _toggle(identity, name):
    parm_name = "device_gpu_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return hou.ToggleParmTemplate(
        parm_name,
        name,
        default_value=False,
        disable_when="{ device_mode != gpu }",
        help="Use this GPU for rendering with the selected backend. Saved by physical PCI identity: " + identity,
        tags={_PHYSICAL: identity, _NAME: name},
    )


def _leaves(entries):
    for parm in entries:
        if isinstance(parm, hou.FolderParmTemplate) and parm.folderType() != hou.folderType.MultiparmBlock:
            yield from _leaves(parm.parmTemplates())
        else:
            yield parm


def refresh(rop):
    """Add newly discovered cards and retain unavailable saved selections.

    The GPU checkboxes make the ROP store its own parameter layout. Rebuild that
    layout from the current asset definition each time, so controls added by a
    later definition appear in place rather than at the end. Artist-added spare
    parameters are kept, after the definition's controls.
    """
    global _device_cache
    _device_cache = devices()
    available = _available()
    current = rop.parmTemplateGroup()
    existing = {parm.tags()[_PHYSICAL]: parm for parm in gpu_parameters(rop)}
    definition = rop.type().definition()
    group = definition.parmTemplateGroup() if definition is not None else current
    if definition is not None:
        from .. import _native

        retired = {"pass_" + p["id"] for p in _native.pass_catalogue() if p["id"] != "combined"} | {
            "aov_count",
            "lightgroup_count",
            "crypto_object",
            "crypto_material",
            "crypto_asset",
            "crypto_levels",
        }
        known = {parm.name() for parm in _leaves(group.entries())}
        for parm in _leaves(current.entries()):
            if parm.name() not in known and parm.name() not in retired and not parm.tags().get(_PHYSICAL):
                group.append(parm)
    folder = _gpu_folder(group)
    if folder is None:
        if group.asDialogScript() != current.asDialogScript():
            rop.setParmTemplateGroup(group)
        return
    contents = []
    for identity in (*existing, *(key for key in available if key not in existing)):
        gpu = available.get(identity)
        parm = existing.get(identity) or _toggle(identity, gpu["name"])
        name = gpu["name"] if gpu else parm.tags().get(_NAME, identity)
        # Distinguish two cards with the same name without duplicating CUDA/OptiX.
        if gpu and sum(value["name"] == name for value in available.values()) > 1:
            name += " (" + identity + ")"
        parm.setLabel(name if gpu else name + " (Unavailable)")
        contents.append(parm)
    folder.setParmTemplates(contents)
    group.replace(folder.name(), folder)
    if group.asDialogScript() != current.asDialogScript():
        rop.setParmTemplateGroup(group)


def initialize(rop, *, created=False):
    """Populate physical devices; only new nodes receive a default selection."""
    refresh(rop)
    if created:
        try:
            requested = default_device()
        except RuntimeError:
            # Keep a new ROP editable on an unsupported machine. Rendering
            # requires an explicit device; it never silently chooses the CPU.
            return
        set_devices(rop, requested)


def selected_devices(rop, at_time):
    """Resolve checked physical cards through one backend, with no fallback."""
    _available()
    mode = rop.parm("device_mode").evalAtTime(at_time)
    if mode == "cpu":
        if not any(value["id"] == "CPU" for value in _device_cache):
            raise RuntimeError("The explicitly selected CPU render device is unavailable")
        return ("CPU",)
    if mode != "gpu":
        raise RuntimeError("Render Device must be CPU or GPU Compute")
    backend = rop.parm("compute_backend").evalAtTime(at_time)
    if backend not in ("CUDA", "OPTIX"):
        raise RuntimeError("GPU Backend must be CUDA or OptiX")
    available = _available()
    selected = []
    for name, identity, label in _selection_layout(rop):
        if not rop.parm(name).evalAtTime(at_time):
            continue
        gpu = available.get(identity)
        if gpu is None or backend not in gpu["backends"]:
            raise RuntimeError("Selected GPU is unavailable with " + backend + ": " + label)
        selected.append(gpu["backends"][backend])
    if not selected:
        raise RuntimeError("No GPU selected. Check a GPU on the Cycles ROP Devices tab, then press Render.")
    # Ordering does not alter session identity when the saved checkbox layout
    # is refreshed. The engine receives each selected GPU exactly once.
    return tuple(sorted(selected))


def set_devices(rop, requested):
    """Select an exact CPU or GPU set through the same controls as the UI."""
    ids = (requested,) if isinstance(requested, str) else tuple(requested)
    if not ids or len(set(ids)) != len(ids):
        raise ValueError("Choose a nonempty set of unique render devices")
    available = {value["id"]: value for value in devices()}
    if any(value not in available for value in ids):
        raise ValueError("An explicitly requested render device is unavailable")
    backends = {available[value]["backend"] for value in ids}
    if len(backends) != 1 or (len(ids) > 1 and "CPU" in backends) or not backends.issubset({"CPU", "CUDA", "OPTIX"}):
        raise ValueError("Use CPU alone, or GPUs with one CUDA/OptiX backend")
    refresh(rop)
    rop.parm("device_mode").set("cpu" if backends == {"CPU"} else "gpu")
    if backends != {"CPU"}:
        rop.parm("compute_backend").set(next(iter(backends)))
        physical = {available[value]["physical_id"] for value in ids}
        for parm in gpu_parameters(rop):
            rop.parm(parm.name()).set(parm.tags()[_PHYSICAL] in physical)
    selected = selected_devices(rop, hou.time())
    if selected != tuple(sorted(ids)):
        raise RuntimeError("Native device controls did not retain the requested selection")
    return selected
