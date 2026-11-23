# SPDX-License-Identifier: Apache-2.0
"""Opt-in Windows process and NVML sampling; no renderer state changes.

Layouts follow the installed Windows SDK psapi.h and CUDA 12.8 nvml.h.
Device-wide framebuffer usage is deliberately not attributed to a process.
"""

import ctypes as ct
from ctypes import wintypes as wt
import json
import time


class ProcessMemory(ct.Structure):
    _fields_ = [("cb", wt.DWORD), ("faults", wt.DWORD)] + [
        (name, ct.c_size_t)
        for name in (
            "peak_working_set",
            "working_set",
            "peak_paged_pool",
            "paged_pool",
            "peak_nonpaged_pool",
            "nonpaged_pool",
            "pagefile",
            "peak_pagefile",
            "private_usage",
        )
    ]


class NvmlMemory(ct.Structure):
    _fields_ = [(name, ct.c_ulonglong) for name in ("total", "free", "used")]


class NvmlProcess(ct.Structure):
    _fields_ = [
        ("pid", ct.c_uint),
        ("used_bytes", ct.c_ulonglong),
        ("gpu_instance", ct.c_uint),
        ("compute_instance", ct.c_uint),
    ]


def bind(library, name, arguments, result=ct.c_int):
    method = getattr(library, name)
    method.argtypes = arguments
    method.restype = result
    return method


class Nvml:
    def __init__(self):
        self.library = ct.CDLL("nvml.dll")
        init = bind(self.library, "nvmlInit_v2", [])
        self.shutdown = bind(self.library, "nvmlShutdown", [])
        code = init()
        if code:
            raise RuntimeError(f"NVML initialization status {code}")
        try:
            self.memory = bind(self.library, "nvmlDeviceGetMemoryInfo", [ct.c_void_p, ct.POINTER(NvmlMemory)])
            self.processes = bind(
                self.library,
                "nvmlDeviceGetComputeRunningProcesses_v3",
                [ct.c_void_p, ct.POINTER(ct.c_uint), ct.POINTER(NvmlProcess)],
            )
            count = ct.c_uint()
            code = bind(self.library, "nvmlDeviceGetCount_v2", [ct.POINTER(ct.c_uint)])(ct.byref(count))
            if code:
                raise RuntimeError(f"NVML device enumeration status {code}")
            get_handle = bind(self.library, "nvmlDeviceGetHandleByIndex_v2", [ct.c_uint, ct.POINTER(ct.c_void_p)])
            get_uuid = bind(self.library, "nvmlDeviceGetUUID", [ct.c_void_p, ct.c_char_p, ct.c_uint])
            get_name = bind(self.library, "nvmlDeviceGetName", [ct.c_void_p, ct.c_char_p, ct.c_uint])
            self.devices = []
            for index in range(count.value):
                handle = ct.c_void_p()
                if get_handle(index, ct.byref(handle)):
                    continue
                uuid = ct.create_string_buffer(128)
                name = ct.create_string_buffer(128)
                if get_uuid(handle, uuid, len(uuid)) or get_name(handle, name, len(name)):
                    continue
                self.devices.append((handle, uuid.value.decode(), name.value.decode()))
        except BaseException:
            # Some supported installations expose fewer NVML entry points.
            # Release our initialization reference even if later binding fails.
            self.shutdown()
            raise

    def sample(self, pid=None):
        devices = []
        for handle, uuid, name in self.devices:
            data = dict(uuid=uuid, name=name)
            memory = NvmlMemory()
            code = self.memory(handle, ct.byref(memory))
            data["framebuffer"] = (
                dict(total_bytes=memory.total, free_bytes=memory.free, used_bytes=memory.used)
                if not code
                else dict(unavailable_status=code)
            )
            if pid is not None:
                # Extra capacity permits normal process-list changes. Report
                # overflow/unavailability instead of reading an incomplete list.
                count = ct.c_uint(256)
                processes = (NvmlProcess * 256)()
                code = self.processes(handle, ct.byref(count), processes)
                if code:
                    data["compute_process"] = dict(unavailable_status=code)
                else:
                    process = next((processes[i] for i in range(count.value) if processes[i].pid == pid), None)
                    data["compute_process"] = dict(
                        present=process is not None,
                        used_bytes=None
                        if process is None or process.used_bytes == 0xFFFFFFFFFFFFFFFF
                        else process.used_bytes,
                        scope="NVML reports unavailable process bytes under WDDM; absence is not a zero-byte measurement",
                    )
            devices.append(data)
        return devices


class MemoryRecorder:
    def __init__(self, process, path):
        self.pid = process.pid
        self.begin = time.monotonic()
        self.records = []
        self.output = path.open("w", encoding="utf-8")
        self.kernel = ct.WinDLL("kernel32", use_last_error=True)
        self.close_handle = bind(self.kernel, "CloseHandle", [wt.HANDLE], wt.BOOL)
        self.read = bind(
            self.kernel, "K32GetProcessMemoryInfo", [wt.HANDLE, ct.POINTER(ProcessMemory), wt.DWORD], wt.BOOL
        )
        self.handle = bind(self.kernel, "OpenProcess", [wt.DWORD, wt.BOOL, wt.DWORD], wt.HANDLE)(
            0x1010, False, self.pid
        )
        self.host_error = None if self.handle else ct.get_last_error()
        self.nvml = None
        self.nvml_error = None
        try:
            self.nvml = Nvml()
        except (OSError, RuntimeError, AttributeError) as error:
            self.nvml_error = str(error)
        self.sample()

    def sample(self):
        record = dict(elapsed_seconds=time.monotonic() - self.begin, pid=self.pid)
        if self.handle:
            data = ProcessMemory()
            data.cb = ct.sizeof(data)
            if self.read(self.handle, ct.byref(data), data.cb):
                record["host"] = dict(
                    working_set_bytes=data.working_set,
                    peak_working_set_bytes=data.peak_working_set,
                    private_commit_bytes=data.private_usage,
                )
            else:
                record["host"] = dict(unavailable_win32_error=ct.get_last_error())
        else:
            record["host"] = dict(unavailable_win32_error=self.host_error)
        if self.nvml:
            record["gpu"] = self.nvml.sample(self.pid)
        self.records.append(record)
        self.output.write(json.dumps(record) + "\n")
        self.output.flush()

    def finish(self):
        self.output.close()
        if self.handle:
            self.close_handle(self.handle)
        if self.nvml:
            self.nvml.shutdown()
        host = [record["host"] for record in self.records if "working_set_bytes" in record["host"]]
        devices = {}
        for record in self.records:
            for gpu in record.get("gpu", []):
                framebuffer = gpu.get("framebuffer", {})
                if "used_bytes" not in framebuffer:
                    continue
                data = devices.setdefault(
                    gpu["uuid"],
                    dict(
                        name=gpu["name"],
                        initial_device_used_bytes=framebuffer["used_bytes"],
                        peak_sampled_device_used_bytes=framebuffer["used_bytes"],
                        process_used_bytes_available=False,
                    ),
                )
                data["peak_sampled_device_used_bytes"] = max(
                    data["peak_sampled_device_used_bytes"], framebuffer["used_bytes"]
                )
                data["last_sampled_device_used_bytes"] = framebuffer["used_bytes"]
                if gpu.get("compute_process", {}).get("used_bytes") is not None:
                    data["process_used_bytes_available"] = True
        return dict(
            samples=len(self.records),
            host_status="measured" if host else "unavailable",
            host_peak_working_set_bytes=max((row["peak_working_set_bytes"] for row in host), default=None),
            host_peak_sampled_private_commit_bytes=max((row["private_commit_bytes"] for row in host), default=None),
            host_last_sample=host[-1] if host else None,
            nvml_error=self.nvml_error,
            gpu=devices,
            scope="Whole benchmark child process working set/private commit and sampled device-wide NVML framebuffer. "
            "Samples can miss transient GPU peaks. No attribution of device-wide usage to the renderer, "
            "no per-process VRAM claim when unavailable, and no post-session retained-memory claim from the last sample.",
        )
