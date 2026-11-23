# SPDX-License-Identifier: Apache-2.0
"""Read-only viewer telemetry, scoped to the active renderer's physical GPUs."""

from functools import lru_cache
import os


@lru_cache(maxsize=32)
def adapter_identity(physical_id):
    """Match CUDA/OptiX PCI identity to DXGI without opening a GPU context."""
    if os.name != "nt" or not physical_id.startswith("NVIDIA:"):
        return None
    import ctypes as ct

    try:
        driver = ct.WinDLL("nvcuda.dll")
        driver.cuInit.argtypes = [ct.c_uint]
        driver.cuDeviceGetByPCIBusId.argtypes = [ct.POINTER(ct.c_int), ct.c_char_p]
        driver.cuDeviceGetLuid.argtypes = [ct.c_void_p, ct.POINTER(ct.c_uint), ct.c_int]
        if driver.cuInit(0):
            return None
        device = ct.c_int()
        if driver.cuDeviceGetByPCIBusId(ct.byref(device), physical_id[7:].encode("ascii")):
            return None
        luid = (ct.c_ubyte * 8)()
        mask = ct.c_uint()
        if driver.cuDeviceGetLuid(luid, ct.byref(mask), device):
            return None
        # The native DXGI collector currently reports node zero only.
        if mask.value != 1:
            return None
        value = bytes(luid)
        return f"{int.from_bytes(value[4:], 'little'):08x}:{int.from_bytes(value[:4], 'little'):08x}", 0
    except (OSError, AttributeError, ValueError):
        return None


def _selected_adapters(renderer, selected, available):
    catalogue = {device["id"]: device for device in available}
    adapters = {
        (adapter.get("luid"), adapter.get("node", 0)): adapter
        for adapter in (renderer or {}).get("gpu", {}).get("adapters", [])
    }
    seen = set()
    for device_id in selected:
        if device_id == "CPU":
            continue
        device = catalogue.get(device_id, {})
        physical = device.get("physical_id", device_id)
        if physical in seen:
            continue
        seen.add(physical)
        yield device.get("name", device_id), adapters.get(adapter_identity(physical), {})


def compact(host, renderer, selected, available):
    """One status-row readout; each GPU remains separate, never a pooled total."""

    def size(value):
        return f"{value / 2**30:.1f} GiB" if value >= 2**30 else f"{value / 2**20:.0f} MiB"

    snapshots = [host]
    if renderer and renderer.get("pid") != host.get("pid"):
        snapshots.append(renderer)
    memory = [s.get("host", {}).get("working_set_bytes") for s in snapshots]
    result = "Mem: " + (size(sum(memory)) if all(value is not None for value in memory) else "N/A")
    gpu = []
    for name, adapter in _selected_adapters(renderer, selected, available):
        value = adapter.get("local", {}).get("current_usage_bytes")
        gpu.append(size(value) if value is not None else "…" if renderer is None else "N/A")
    if gpu:
        result += " · GPU: " + " / ".join(gpu)
    return result


def describe(host, renderer, selected, available):
    lines = []
    memory = host.get("host", {})
    if "working_set_bytes" in memory:
        lines.append(
            f"Houdini working set {memory['working_set_bytes'] / 2**20:.1f} MiB · committed {memory['private_commit_bytes'] / 2**20:.1f} MiB"
        )
    else:
        lines.append("Houdini process memory unavailable")
    if renderer and renderer.get("pid") != host.get("pid"):
        memory = renderer.get("host", {})
        if "working_set_bytes" in memory:
            lines.append(
                f"Renderer working set {memory['working_set_bytes'] / 2**20:.1f} MiB · committed {memory['private_commit_bytes'] / 2**20:.1f} MiB"
            )
    for name, adapter in _selected_adapters(renderer, selected, available):
        local, shared = adapter.get("local", {}), adapter.get("nonlocal", {})
        if not renderer:
            lines.append(name + ": renderer GPU usage pending")
        elif "current_usage_bytes" not in local or "current_usage_bytes" not in shared:
            lines.append(name + ": renderer GPU usage unavailable")
        else:
            lines.append(
                f"{name}: renderer GPU usage {local['current_usage_bytes'] / 2**20:.1f} MiB · "
                f"shared {shared['current_usage_bytes'] / 2**20:.1f} MiB · budget {local['budget_bytes'] / 2**20:.1f} MiB"
            )
    if selected == ["CPU"] or selected == ("CPU",):
        lines.append("CPU rendering")
    elif not selected:
        lines.append("No active render devices")
    scope = (
        "Mem sums Houdini and renderer process working sets; shared pages may be counted in both. "
        "GPU values follow the card order listed above and are not added together. "
        "GPU counters cover only the active renderer process and selected render GPUs; "
        "Houdini viewport GPU allocations and other processes are excluded. "
        "Windows process segment usage includes driver overhead and is not exact physical residency."
    )
    if renderer:
        scope += f" Renderer PID: {renderer.get('pid', 'unavailable')}."
    return "\n".join(lines), scope
