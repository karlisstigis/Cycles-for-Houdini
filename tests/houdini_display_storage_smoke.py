# SPDX-License-Identifier: Apache-2.0
"""Owned readback views avoid a copy and retain exact compact display placement."""

from support.paths import artifacts_root
import gc
import ctypes
import hashlib
import json
import os
from pathlib import Path

import numpy as np
import cycles_houdini as ch

source = np.arange(24, dtype=np.uint8).reshape(2, 3, 4)
source.setflags(write=False)
view = memoryview(source).cast("B").toreadonly()
storage = ch._native.expand_display_rgba(view, 3, 2, 3, 2)
assert storage is view and storage.readonly
assert not np.frombuffer(storage, np.uint8).flags.writeable
expected_hash = hashlib.sha256(source).hexdigest()
expanded = ch._native.expand_display_rgba(view, 3, 2, 7, 5)
assert isinstance(expanded, bytes)
native = source[::-1]
expected = native[(np.arange(5) * 2 // 5)[:, None], np.arange(7) * 3 // 7][::-1]
assert np.array_equal(np.frombuffer(expanded, np.uint8).reshape(5, 7, 4), expected)
copied = view.tobytes()
assert ch._native.expand_display_rgba(copied, 3, 2, 3, 2) is copied
del source, view
gc.collect()
assert hashlib.sha256(storage).hexdigest() == expected_hash
for value, shape in [
    (memoryview(bytearray(24)), (3, 2, 3, 2)),
    (memoryview(bytes(48))[::2], (3, 2, 3, 2)),
    (storage, (4, 2, 4, 2)),
    (storage, (0, 2, 3, 2)),
]:
    try:
        ch._native.expand_display_rgba(value, *shape)
    except (RuntimeError, BufferError):
        pass
    else:
        raise AssertionError("Invalid display buffer/shape was accepted")
# This live scratch allocation is deliberately wrapped without an exporter.
# Reject the missing owner before accessing its bytes, even though this test
# keeps the scratch storage alive for the complete negative call.
scratch = ctypes.create_string_buffer(24)
from_memory = ctypes.pythonapi.PyMemoryView_FromMemory
from_memory.restype = ctypes.py_object
from_memory.argtypes = [ctypes.c_void_p, ctypes.c_ssize_t, ctypes.c_int]
orphan = from_memory(ctypes.addressof(scratch), 24, 0x100)  # PyBUF_READ
assert orphan.obj is None and orphan.readonly
try:
    ch._native.expand_display_rgba(orphan, 3, 2, 3, 2)
except RuntimeError as error:
    assert "owned" in str(error)
else:
    raise AssertionError("Display storage without an exporter owner was accepted")
report = dict(
    result="passed",
    checks=[
        "Full read-only view retains identity without copying",
        "View consumers are read-only",
        "Odd compact expansion preserves native row origin",
        "Existing bytes input retains identity",
        "View owns its allocation after producer deletion",
        "Writable, strided and invalid-shape inputs fail explicitly",
        "Ownerless raw memory views fail before access",
    ],
    rgba_sha256=expected_hash,
)
path = artifacts_root() / "stage1/display-input/storage-report.json"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(report, indent=2))
print(path)
