# SPDX-License-Identifier: Apache-2.0
"""Per-worker GPU scheduling; no desktop or driver preference changes."""

import sys


def prioritize_interaction(process):
    # https://learn.microsoft.com/windows-hardware/drivers/ddi/d3dkmthk/nf-d3dkmthk-d3dkmtsetprocessschedulingpriorityclass
    # Keep the interactive application's graphics ahead of background tracing.
    # This affects only our owned child. CPU scheduling and final renders retain
    # their normal policy; unsupported drivers continue rendering normally.
    result = dict(requested=1, applied=False)
    if sys.platform != "win32":
        return dict(result, reason="GPU scheduling is not available on this platform")
    try:
        import ctypes
        from ctypes import wintypes

        library = ctypes.WinDLL("gdi32.dll", winmode=0x00000800)
        get = library.D3DKMTGetProcessSchedulingPriorityClass
        get.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_int)]
        get.restype = ctypes.c_long
        change = library.D3DKMTSetProcessSchedulingPriorityClass
        change.argtypes = [wintypes.HANDLE, ctypes.c_int]
        change.restype = ctypes.c_long
        handle = wintypes.HANDLE(int(process._handle))
        before = ctypes.c_int()
        after = ctypes.c_int()
        queried = get(handle, ctypes.byref(before))
        changed = change(handle, 1)
        verified = get(handle, ctypes.byref(after))
        result.update(
            before=before.value if queried == 0 else None,
            after=after.value if verified == 0 else None,
            set_status=changed,
            verify_status=verified,
            applied=changed == 0 and verified == 0 and after.value == 1,
        )
    except (OSError, AttributeError) as error:
        result["reason"] = str(error)
    return result
