# SPDX-License-Identifier: Apache-2.0
"""Read-only GPU input views own their frame after session/capsule retirement."""

from support.paths import artifacts_root
import argparse
import gc
import hashlib
import json
import os
from pathlib import Path
import threading

import numpy as np
import cycles_houdini as ch

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
with ch.Session(
    device=device, width=128, height=96, samples=1, interactive=False, passes=["combined", "normal"]
) as session:
    session.wait(session.calibration())
    owned = session.display_frame()[0]
closed = session.status()
assert closed["device_allocation_bytes"] == closed["owned_geometry_bytes"] == 0
copied = ch._native.display_gpu_input(owned, {})
assert copied is not None and copied["pixels_copied"] and isinstance(copied["pixels"], bytes)
expected = hashlib.sha256(copied["pixels"]).hexdigest()
expected_array = np.frombuffer(copied["pixels"], np.float32).copy()
views = []
for settings in ({}, dict(exposure=0.75, gamma=1.4), dict(channels="rgb")):
    plan = ch._native.display_gpu_input(owned, settings, "", False)
    assert plan is not None and not plan["pixels_copied"]
    memory = plan["pixels"]
    assert isinstance(memory, memoryview) and memory.readonly and memory.c_contiguous
    assert memory.nbytes == 128 * 96 * 4 * 4
    assert type(memory.obj).__name__ == "FramePixels"
    assert hashlib.sha256(memory).hexdigest() == expected
    pixels = np.frombuffer(memory, np.float32)
    assert not pixels.flags.writeable and np.array_equal(pixels, expected_array)
    for mutate in (
        lambda: memory.__setitem__(0, 0),
        lambda: pixels.__setitem__(0, 0),
        lambda: pixels.setflags(write=True),
    ):
        try:
            mutate()
        except (TypeError, ValueError, BufferError):
            pass
        else:
            raise AssertionError("An immutable renderer frame became writable")
    # Retain only the NumPy consumer. It must own a buffer export independently
    # of both this memoryview and the returned plan dictionary.
    views.append(pixels)
    del plan, memory, pixels
assert ch._native.display_gpu_input(owned, {"pass": "normal"}, "", False) is None
assert ch._native.display_gpu_input(owned, dict(raw=True), "", False) is None
del copied, owned, session
gc.collect()

# Allocate and retire different render frames after discarding every source
# capsule; stale pointers must not read those replacement frame allocations.
with ch.Session(device=device, width=128, height=96, samples=1, interactive=False, passes=["combined"]) as other:
    other.wait(other.calibration())
del other
gc.collect()
errors = []


def check_on_consumer_thread():
    try:
        for value in views:
            assert not value.flags.writeable
            assert np.array_equal(value, expected_array)
            assert hashlib.sha256(value).hexdigest() == expected
        views.clear()
        gc.collect()
    except BaseException as error:
        errors.append(repr(error))


worker = threading.Thread(target=check_on_consumer_thread)
worker.start()
worker.join(10)
assert not worker.is_alive() and not errors, errors
assert not views
report = dict(
    result="passed",
    device=device,
    closed=closed,
    rgba_float_bytes=128 * 96 * 4 * 4,
    sha256=expected,
    checks=[
        "Default copied input is retained",
        "Borrowed input is read-only and owns a frame reference",
        "NumPy cannot enable writes",
        "Data/raw passes retain the CPU fallback",
        "Consumers outlive capsule and renderer deletion",
        "Consumers remain valid through new renderer allocations",
        "Last consumer releases on another thread",
    ],
)
path = artifacts_root() / "stage1/display-input/ownership-report.json"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(report, indent=2))
print(path)
