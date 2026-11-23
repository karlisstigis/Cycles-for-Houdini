# SPDX-License-Identifier: Apache-2.0
"""Fresh-process saved-HIP reload, external texture edit and same-session repair.

Run through the current runtime's run-hython.ps1. During development the explicit
dev_overlay.py launcher can select source Python modules over a staged binary.
The input HIP/assets are copied before loading; artist/source files are untouched.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import traceback

import hou
import numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import native_scene


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--source-dir", type=Path, required=True)
parser.add_argument("--out", type=Path, required=True)
parser.add_argument("--device", default="CPU")
args = parser.parse_args()
source = args.source_dir.resolve(strict=True)
destination = args.out.resolve()
if destination == source or source.is_relative_to(destination):
    raise ValueError("Use a separate QA directory outside the input fixture directory")
destination.mkdir(parents=True, exist_ok=True)
for name in ("example-CPU.hip", "animated panels.blend", "external.png", "packed.png", "rec709-CPU.ocio"):
    shutil.copy2(source / name, destination / name)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def read_exr(path):
    reader = oiio.ImageInput.open(str(path))
    assert reader, path
    try:
        names = list(reader.spec().channelnames)
        return reader.read_image(format=oiio.FLOAT)[:, :, [names.index(name) for name in "RGBA"]]
    finally:
        reader.close()


report = dict(
    result="running",
    source=str(source),
    output=str(destination),
    host_python=ch.__file__,
    host_native=ch._native.__file__,
    checks=[],
)
texture = destination / "external.png"
original = texture.read_bytes()
replacement = (destination / "packed.png").read_bytes()
assert digest(original) != digest(replacement), "Fixture replacement must contrast with the external texture"
report["texture_sha256"] = dict(baseline=digest(original), edited=digest(replacement))


def replace_texture(value):
    previous = texture.stat().st_mtime_ns
    temporary = texture.with_name("external.pending.png")
    temporary.write_bytes(value)
    os.replace(temporary, texture)
    # Exercise the real stat-based dependency observer without relying on the
    # filesystem's clock resolution or inserting a timing sleep.
    stat = texture.stat()
    os.utime(texture, ns=(stat.st_atime_ns, max(stat.st_mtime_ns, previous + 1_000_000_000)))


try:
    hou.putenv("OCIO", str(destination / "rec709-CPU.ocio"))
    hou.Color.reloadOCIO()
    ch.synchronize_color_configuration()
    hou.hipFile.load(str(destination / "example-CPU.hip"), suppress_save_prompt=True)
    assert Path(hou.text.expandString("$HIP")).resolve() == destination
    assert not any(node.userData("cycles_external_blend") for node in hou.node("/obj").children()), (
        "Saved HIP retained generated prototype nodes"
    )
    rop = hou.node("/out/blend_instances")
    assert rop is not None
    point_source = hou.node("/obj/blend_points/point_reference")
    assert point_source is not None and "$HIP/animated panels.blend" in point_source.parm("snippet").unexpandedString()
    from cycles_houdini.device_selection import set_devices

    set_devices(rop, args.device)
    with ch.Session(
        device=args.device, width=128, height=128, samples=128, execution="worker", threads=4, collect_diagnostics=True
    ) as session:
        renderer_pid = None

        def render(label):
            frame = session.wait(session.apply(native_scene.capture(rop)), timeout=180)
            status = session.status()
            assert not status["error"] and status["render_device_ids"] == [args.device], status
            assert status["execution"] == "worker" and status["renderer_pid"] > 0, status
            diagnostics = status.get("source_diagnostics", [])
            assert not [
                row
                for row in diagnostics
                if row.get("severity") == "error" or row.get("code", "").startswith("instancing.unavailable")
            ], diagnostics
            plane = frame["planes"]["combined"]
            image = (
                np.frombuffer(plane["pixels"], np.float32)
                .reshape(frame["height"], frame["width"], plane["channels"])
                .copy()
            )
            assert image.shape == (128, 128, 4) and np.isfinite(image).all()
            assert (image[:, :, 3] > 0.01).sum() > 1500, (label, status)
            session.write_exr(destination / (label + ".exr"))
            # EXR scanlines and the raw film buffer have opposite Y order.
            # Use file pixels consistently for saved-image and edit checks.
            image = read_exr(destination / (label + ".exr"))
            np.save(destination / (label + ".npy"), image)
            report["checks"].append(dict(label=label, status=status))
            return image, status

        baseline, initial_status = render("reloaded-baseline")
        renderer_pid = initial_status["renderer_pid"]
        reference = read_exr(source / "after-save-CPU.exr")
        foreground = (baseline[:, :, 3] > 0.01) | (reference[:, :, 3] > 0.01)
        reload_error = float(np.sqrt(np.mean((baseline[foreground] - reference[foreground]) ** 2)))
        assert reload_error < 0.018, ("Saved-HIP fresh-process image differs", reload_error)
        try:
            replace_texture(replacement)
            edited, edit_status = render("external-texture-edited")
        finally:
            replace_texture(original)
        repaired, repair_status = render("external-texture-repaired")
        assert edit_status["renderer_pid"] == repair_status["renderer_pid"] == renderer_pid, (
            "Dependency edit restarted the worker"
        )
        mask = (baseline[:, :, 3] > 0.01) | (edited[:, :, 3] > 0.01)
        change = float(np.mean(np.abs(edited[:, :, :3] - baseline[:, :, :3])[mask]))
        repair_error = float(np.sqrt(np.mean((repaired[mask] - baseline[mask]) ** 2)))
        alpha_error = float(np.max(np.abs(edited[:, :, 3] - baseline[:, :, 3])))
        report.update(
            reload_rgba_rmse=reload_error,
            edited_foreground_rgb_change=change,
            repaired_rgba_rmse=repair_error,
            edited_maximum_alpha_change=alpha_error,
            renderer_pid=renderer_pid,
        )
        assert change > 0.02, ("External texture change left stale rendered pixels", change)
        assert alpha_error < 0.012, ("Texture edit changed geometry coverage", alpha_error)
        assert repair_error < 0.006 and np.allclose(repaired, baseline, atol=0.012), (
            "Repair did not restore baseline",
            repair_error,
        )
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    if texture.read_bytes() != original:
        replace_texture(original)
    (destination / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(destination / "report.json")
