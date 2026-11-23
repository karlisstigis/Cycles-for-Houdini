# SPDX-License-Identifier: Apache-2.0
"""Filesystem fingerprints compatible with packed bounds native signatures.

No Houdini, HDK, or Blender calls. On Windows Python's st_ctime is creation
 time, so dependency validation also needs the kernel's real change time.
"""

import os


def native_signature(path):
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class BasicInfo(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_int64) for name in ("CreationTime", "LastAccessTime", "LastWriteTime", "ChangeTime")
        ] + [("FileAttributes", wintypes.DWORD)]

    class HandleInfo(ctypes.Structure):
        _fields_ = [
            ("attributes", wintypes.DWORD),
            ("created", wintypes.FILETIME),
            ("accessed", wintypes.FILETIME),
            ("modified", wintypes.FILETIME),
        ] + [(name, wintypes.DWORD) for name in ("volume", "size_high", "size_low", "links", "index_high", "index_low")]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(HandleInfo)]
    kernel.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    # FILE_READ_ATTRIBUTES, share read/write/delete, OPEN_EXISTING, backup semantics.
    handle = kernel.CreateFileW(path, 0x80, 7, None, 3, 0x02000000, None)
    if handle == ctypes.c_void_p(-1).value:
        return None
    try:
        basic, info = BasicInfo(), HandleInfo()
        if not kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
            return None
        if not kernel.GetFileInformationByHandleEx(handle, 0, ctypes.byref(basic), ctypes.sizeof(basic)):
            return None
        modified = (info.modified.dwHighDateTime << 32) | info.modified.dwLowDateTime
        size = (info.size_high << 32) | info.size_low
        return ":".join(
            str(value) for value in (modified, basic.ChangeTime, size, info.volume, info.index_high, info.index_low)
        )
    finally:
        kernel.CloseHandle(handle)
