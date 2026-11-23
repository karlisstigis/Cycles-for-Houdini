# SPDX-License-Identifier: Apache-2.0
"""Observable activity of one owned worker process, for long-operation deadlines.

A legitimately long exchange (first OptiX kernel load, large texture/volume
uploads, BVH builds, adaptive dicing) keeps consuming CPU time or transferring
data. A hung worker does neither. Only the owned child is inspected.
"""

import sys


class ProcessActivity:
    def __init__(self, process, *, minimum_cpu_seconds=0.25, minimum_io_bytes=1 << 20):
        self.minimum_cpu_seconds = minimum_cpu_seconds
        self.minimum_io_bytes = minimum_io_bytes
        self._handle = None
        if sys.platform != "win32":
            return
        import ctypes
        from ctypes import wintypes

        class IoCounters(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_ulonglong)
                for name in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        times = kernel.GetProcessTimes
        times.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(ctypes.c_ulonglong)] * 4
        times.restype = wintypes.BOOL
        counters = kernel.GetProcessIoCounters
        counters.argtypes = [wintypes.HANDLE, ctypes.POINTER(IoCounters)]
        counters.restype = wintypes.BOOL
        self._ctypes, self._times, self._counters, self._io_type = ctypes, times, counters, IoCounters
        self._handle = wintypes.HANDLE(int(process._handle))

    def sample(self):
        """(CPU seconds, transferred bytes), or None when unobservable."""
        if self._handle is None:
            return None
        ctypes = self._ctypes
        values = [ctypes.c_ulonglong() for _ in range(4)]
        io = self._io_type()
        if not self._times(self._handle, *(ctypes.byref(v) for v in values)):
            return None
        if not self._counters(self._handle, ctypes.byref(io)):
            return None
        cpu = (values[2].value + values[3].value) * 1e-7  # kernel + user, 100 ns units
        return cpu, io.ReadTransferCount + io.WriteTransferCount + io.OtherTransferCount

    def progressed(self, before, after):
        if before is None or after is None:
            return False
        return after[0] - before[0] >= self.minimum_cpu_seconds or after[1] - before[1] >= self.minimum_io_bytes
