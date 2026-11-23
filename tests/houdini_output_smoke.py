# SPDX-License-Identifier: Apache-2.0
"""Output failures preserve earlier frames and retire staging files."""

from support.paths import artifacts_root
import ctypes
import json
import os
from pathlib import Path

import OpenImageIO as OIIO
import cycles_houdini as ch

root = artifacts_root() / "stage1/output"
root.mkdir(parents=True, exist_ok=True)
target = root / "output-\u00e9.exr"
sentinel = b"previous completed render sentinel"
target.write_bytes(sentinel)

with ch.Session(device="CPU", width=160, height=90, samples=8) as session:
    session.wait(session.calibration())
    rejected = []
    # A nonexistent directory must fail without creating an output hierarchy.
    missing = root / "missing-directory" / "image.exr"
    assert not missing.parent.exists()
    try:
        session.write_exr(missing)
    except RuntimeError:
        rejected.append("missing directory")
    else:
        raise AssertionError("A missing output directory succeeded")
    assert not missing.parent.exists()

    if os.name == "nt":
        # Deny deletion of the previous frame, forcing the final replace to fail
        # after the encoder writes and closes the temporary EXR successfully.
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        kernel.CreateFileW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.CreateFileW(str(target), 0x80000000, 1, None, 3, 0x80, None)
        assert handle != ctypes.c_void_p(-1).value, ctypes.get_last_error()
        try:
            try:
                session.write_exr(target)
            except RuntimeError:
                rejected.append("locked destination")
            else:
                raise AssertionError("Replacing a destination locked against deletion succeeded")
        finally:
            kernel.CloseHandle(handle)
        assert target.read_bytes() == sentinel, "Failure replaced the previous frame"
        assert not list(root.glob(".cycles-*.tmp.exr")), "Temporary output leaked after failure"

    session.write_exr(target)
    assert target.read_bytes() != sentinel
    image = OIIO.ImageInput.open(str(target))
    assert image is not None, OIIO.geterror()
    spec = image.spec()
    assert (spec.width, spec.height) == (160, 90)
    assert {"R", "G", "B", "A", "depth.Z", "normal.X", "position.Z"} <= set(spec.channelnames), spec.channelnames
    assert (
        spec.get_string_attribute("cycles:engine_commit")
        == json.loads((Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"]) / "source.lock.json").read_text())["cycles"][
            "commit"
        ]
    )
    image.close()
    session.wait(session.apply({"crop": (40, 20, 80, 40)}))
    session.write_exr(target)
    image = OIIO.ImageInput.open(str(target))
    spec = image.spec()
    assert (spec.x, spec.y, spec.width, spec.height, spec.full_width, spec.full_height) == (40, 30, 80, 40, 160, 90)
    image.close()
    assert not list(root.glob(".cycles-*.tmp.exr"))

report = dict(
    result="passed",
    rejected=rejected,
    previous_frame="preserved on replacement failure",
    utf8_path="passed",
    crop_window="passed",
    staging_files="retired",
)
(root / "houdini-output-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
