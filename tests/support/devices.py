# SPDX-License-Identifier: Apache-2.0
"""Translate tier backend labels into the native API's exact device IDs."""

import os
import re
from pathlib import Path


def fixture_device(arguments):
    """Use a caller's exact ID or the device verified by the native launcher."""
    if "--device" in arguments:
        return arguments[arguments.index("--device") + 1]
    return os.environ.get("CYCLES_HOUDINI_DEVICE", "CPU")


# These scripts expose an explicit backend enum and perform their own lookup.
BACKEND_ARGUMENTS = {"houdini_blend_instances_smoke.py", "hython_scene_contract.py"}


def resolve_device_argument(script, arguments, devices, requested_id=None):
    result = list(arguments)
    if "--device" not in result or Path(script).name in BACKEND_ARGUMENTS:
        return result, None
    index = result.index("--device") + 1
    if index == len(result) or result[index] not in ("CPU", "OPTIX"):
        return result, None
    backend = result[index]
    choices = sorted(item["id"] for item in devices if item["backend"] == backend)
    if not choices:
        raise RuntimeError("Required test backend is unavailable: " + backend)
    if requested_id is not None and requested_id not in choices:
        raise RuntimeError("Configured test device does not belong to " + backend + ": " + requested_id)
    result[index] = requested_id or choices[0]
    return result, dict(backend=backend, id=result[index])


def device_pci_bus_id(identifier):
    """Extract domain:bus:slot from backend IDs without assuming their prefixes."""
    match = re.search(r"(?<![0-9A-Fa-f])([0-9A-Fa-f]{4}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2})(?:\.[0-7])?", str(identifier))
    return match.group(1).lower() if match else None


def render_device_evidence(status, device):
    expected = device if isinstance(device, list) else [device]
    render_devices = status["render_device_ids"]
    pid = status["renderer_pid"]
    assert sorted(render_devices) == sorted(expected), (expected, status)
    if os.environ.get("CYCLES_HOUDINI_EXECUTION") == "worker":
        assert pid > 0 and pid != os.getpid(), status
    return dict(render_device_ids=render_devices, renderer_pid=pid)


def match_reference_optix_device(devices, *, name, pci=None):
    """Map a verified host GPU to Blender's backend namespace, rejecting ambiguity."""
    matches = [
        device for device in devices if device["backend"] == "OPTIX" and device["name"].casefold() == name.casefold()
    ]
    if pci:
        identified = [device for device in matches if device_pci_bus_id(device["id"])]
        selected = [device for device in identified if device_pci_bus_id(device["id"]) == pci.lower()]
        if selected:
            matches = selected
        elif identified:
            raise RuntimeError("Reference OptiX PCI identity differs from selected host GPU: " + pci)
    if len(matches) != 1:
        raise RuntimeError("Reference OptiX selection must identify exactly one GPU: " + repr(matches))
    return matches[0]
