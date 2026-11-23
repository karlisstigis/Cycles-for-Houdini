# SPDX-License-Identifier: Apache-2.0
"""Bit-sensitive shutter fields, local field repairs and ordered final output."""

import argparse
import json
import traceback

import hou
import numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch
from hash_displacement_fixture import Fixture, plane

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
device = json.loads(device) if device.startswith("[") else device
f = Fixture(device)
path = f.dest / "recovery-report.json"
report = dict(result="running", device=device, checks=[], batch=[])
try:
    with ch.Session(
        device=device, width=f.width, height=f.height, samples=4, threads=2, execution="worker", use_texture_cache=False
    ) as retained:
        for case in ("scalar", "vector", "float2", "uv", "color", "packed"):
            f.configure(case)
            for label, bits in (("valid", 0x80000000), ("invalid", 0x7FC00000), ("repaired", 0)):
                f.edit(bits)
                frame, status = f.render(retained)
                height = f.check(frame)
                assert bool(status["source_diagnostics"]) == (label == "invalid"), status
                if label == "invalid":
                    assert abs(height) < 2e-5, (case, height)
                    # A retained warning is still useful on an unchanged capture.
                    again, cached = f.render(retained)
                    assert cached["source_diagnostics"] == status["source_diagnostics"], cached
                    np.testing.assert_allclose(plane(again, "position"), plane(frame, "position"), atol=2e-5, rtol=0)
                report["checks"].append(dict(case=case, label=label, height=height, status=status))
                path.write_text(json.dumps(report, indent=2))
        for case in ("scalar", "vector", "float2", "uv", "color"):
            f.configure(case, sampled=True)
            f.camera.parm("projection").set(0)
            sampled_images = []
            for bits in (0, 0x80000000, 0):
                f.edit(bits)
                frame, status = f.render(retained)
                with ch.Session(
                    device=device,
                    width=f.width,
                    height=f.height,
                    samples=4,
                    threads=2,
                    execution="worker",
                    use_texture_cache=False,
                ) as fresh:
                    reference, fresh_status = f.render(fresh)
                assert not status["source_diagnostics"] and not fresh_status["source_diagnostics"], (
                    status,
                    fresh_status,
                )
                # Perspective displacement changes the silhouette during the
                # shutter; edge pixels legitimately mix surface/background.
                f.check(frame, interior=True)
                for name in ("combined", "position", "normal", "depth"):
                    np.testing.assert_allclose(plane(frame, name), plane(reference, name), atol=2e-5, rtol=0)
                sampled_images.append(plane(frame, "combined").copy())
                report["checks"].append(dict(case="shutter_" + case, bits=bits, status=status))
                path.write_text(json.dumps(report, indent=2))
            assert np.max(np.abs(sampled_images[0] - sampled_images[1])) > 1e-4, case
            np.testing.assert_allclose(sampled_images[0], sampled_images[2], atol=2e-5, rtol=0)
    closed = retained.status()
    assert closed["owned_geometry_bytes"] == closed["device_allocation_bytes"] == 0, closed

    # Five actual SOHO frames: an invalid middle frame retains the independently
    # valid emission shader, writes its image, then clears diagnostics on repair.
    f.configure("scalar")
    f.camera.parm("projection").set(1)
    f.source.parm("bits").setExpression(
        "[0,-2147483648,2143289344,0,-2147483648][int(hou.frame())-1]", hou.exprLanguage.Python
    )
    f.rop.setParms(dict(persistent_data=1, transparent_background=1, picture=str(f.dest / "hash-recovery.$F4.exr")))
    f.rop.render(frame_range=(1, 5, 1))
    expected = (0.29121291637420654, 0.45130395889282227, 0.0, 0.29121291637420654, 0.45130395889282227)
    for index, height in enumerate(expected, 1):
        image = oiio.ImageInput.open(str(f.dest / f"hash-recovery.{index:04d}.exr"))
        assert image
        spec = image.spec()
        pixels = image.read_image(format=oiio.FLOAT)
        image.close()
        issues = json.loads(spec.get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        assert bool(issues) == (index == 3), (index, issues)
        rgba = pixels[..., [spec.channelnames.index(c) for c in ("R", "G", "B", "A")]]
        mask = rgba[..., 3] > 0.999
        assert mask.sum() > 100
        np.testing.assert_allclose(rgba[mask, :3], np.broadcast_to((0.2, 0.4, 0.6), (mask.sum(), 3)), atol=3e-5, rtol=0)
        measured = float(np.median(pixels[..., spec.channelnames.index("position.Z")][mask]))
        assert abs(measured - height) < 2e-5, (index, measured, height)
        report["batch"].append(dict(frame=index, height=measured, diagnostics=issues))
    report.update(result="passed", closed=closed)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
finally:
    f.source.parm("bits").deleteAllKeyframes()
    hou.setFrame(1)
    path.write_text(json.dumps(report, indent=2))
assert report["result"] == "passed", report.get("error")
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]), batch=len(report["batch"]))))
