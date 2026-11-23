# SPDX-License-Identifier: Apache-2.0
"""Raw PLY controls, OptiX worker DLSS and Image Iris together, including a radiance edit."""

import argparse
import json
import math
from pathlib import Path
import traceback

import hou
import numpy as np
import OpenImageIO as oiio
import PyOpenColorIO as ocio
import cycles_houdini as ch
from cycles_houdini import camera_properties, device_selection, native_scene, object_properties
from native_test_runtime import runtime_context

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--device", required=True, help="An explicit OptiX device ID; CPU fallback is rejected.")
parser.add_argument("--report", required=True, type=Path)
args = parser.parse_args()
args.report.parent.mkdir(parents=True, exist_ok=True)
context = runtime_context()
caps = ch.capabilities()
assert caps["gaussian_splats"] and caps["image_iris"], caps
selected = next(d for d in ch.devices() if d["id"] == args.device)
# NGX is probed in the rendering worker; the UI host only lists static backends.
assert selected["backend"] == "OPTIX", selected
width = height = 96
samples = 64
report = dict(
    result="running", runtime=context, capabilities=caps, device=selected, authored_samples=samples, checks=[]
)


def ply(label, srgb):
    # Actual original 3DGS file attributes, including log scales/logit opacity
    # and wxyz rotation. The native auto detector must select splats itself.
    fields = [("x", 0.0), ("y", 0.0), ("z", 0.0)]
    fields += [(f"f_dc_{i}", (v - 0.5) / 0.28209479177387814) for i, v in enumerate(srgb)]
    fields += [(f"scale_{i}", math.log(v)) for i, v in enumerate((0.32, 0.08, 0.025))]
    fields += [(f"rot_{i}", v) for i, v in enumerate((1.0, 0.0, 0.0, 0.0))]
    fields += [("opacity", math.log(4.0))]
    path = args.report.parent / (label + ".ply")
    path.write_text(
        "ply\nformat ascii 1.0\nelement vertex 1\n"
        + "".join("property float " + name + "\n" for name, _ in fields)
        + "end_header\n"
        + " ".join(str(v) for _, v in fields)
        + "\n",
        encoding="ascii",
    )
    return path


obj = hou.node("/obj").createNode("geo", "combined_gsplat", run_init_scripts=False)
source = obj.createNode("file", "original_3dgs_ply")
source.setDisplayFlag(True)
source.setRenderFlag(True)
object_properties.install(obj)
obj.parm("cycles_point_render_mode").set("auto")
assert object_properties.component_settings(obj, hou.time())["point_render_mode"] == "auto"
camera = hou.node("/obj").createNode("cam", "combined_gsplat_camera")
camera.setParms(dict(tz=5, focal=50, focus=5, fstop=2.8))
camera_properties.install(camera)
mask = args.report.parent / "combined-iris.exr"
y, x = np.mgrid[-1:1:33j, -1:1:33j]
pixels = np.ascontiguousarray(((x * x + y * y < 0.85) * (1 + 0.3 * x)).astype(np.float32)[:, :, None])
writer = oiio.ImageOutput.create(str(mask))
assert writer.open(str(mask), oiio.ImageSpec(33, 33, 1, oiio.FLOAT))
assert writer.write_image(pixels) and writer.close()
camera.setParms(
    dict(cycles_dof=1, cycles_image_iris=1, cycles_image_iris_file=str(mask), cycles_image_iris_method="cdf")
)
rop = hou.node("/out").createNode("cycles_ipr", "combined_gsplat_dlss")
device_selection.set_devices(rop, args.device)
rop.setParms(
    dict(
        source_mode="sop",
        soppath=source.path(),
        camera=camera.path(),
        resx=width,
        resy=height,
        samples=samples,
        ipr_samples=samples,
        viewer_interactive=1,
        ipr_use_denoise=1,
        ipr_denoiser="dlss",
        ipr_dlss_quality="performance",
        adaptive_sampling=0,
        ipr_adaptive_sampling=0,
        allowmotionblur=0,
        dof_mode="camera",
        transparent_background=1,
    )
)
rop.parmTuple("background").set((0, 0, 0))
config = ocio.GetCurrentConfig()
rec709 = (
    "lin_rec709_scene"
    if config.getColorSpace("lin_rec709_scene")
    else next(n for n in config.getColorSpaceNames() if n.startswith("Linear Rec.709"))
)
processor = config.getProcessor(rec709, ocio.ROLE_SCENE_LINEAR).getDefaultCPUProcessor()


def expected_color(srgb):
    srgb = np.asarray(srgb)
    linear = np.where(srgb <= 0.04045, srgb / 12.92, ((srgb + 0.055) / 1.055) ** 2.4)
    working = np.asarray(processor.applyRGB(linear.tolist()))
    return working / working.sum()


try:
    denoising = native_scene.denoising_configuration(rop, hou.time())
    assert denoising["enabled"] and denoising["method"] == "dlss" and denoising["upscale_factor"] == 2, denoising
    with ch.Session(
        device=args.device,
        width=width,
        height=height,
        samples=samples,
        interactive=True,
        execution="worker",
        denoising=denoising,
        passes=["combined", "noisy_combined"],
    ) as session:
        renderer_pid = None
        images = []
        for label, color in [("initial", (0.8, 0.25, 0.12)), ("radiance-edit", (0.12, 0.65, 0.85))]:
            source.parm("file").set(str(ply(label, color)))
            source.cook(force=True)
            assert not source.errors() and len(source.geometry().points()) == 1, source.errors()
            update = native_scene.capture(rop)
            assert update["camera_dof"]["image_iris"] == str(mask), update["camera_dof"]
            assert update["camera_dof"]["aperture_size"] > 0 and update["camera_dof"]["image_iris_method"] == 1, update[
                "camera_dof"
            ]
            generation = session.apply(update)
            frame = session.wait(generation, timeout=180)
            status = session.status()
            assert not status["error"] and status["render_device_ids"] == [args.device], status
            assert status["execution"] == "worker" and status["denoiser"] == "dlss", status
            assert status["renderer_pid"] > 0, status
            if renderer_pid is None:
                renderer_pid = status["renderer_pid"]
            assert status["renderer_pid"] == renderer_pid, status
            assert any(d["id"] == args.device and "dlss" in d["denoisers"] for d in status["worker_devices"]), status
            assert status["points"] == 1 and status["image_iris_table_bytes"] > 0, status
            assert status["image_iris_preparations"] >= 1, status
            diagnostics = status.get("source_diagnostics", [])
            assert not [
                d
                for d in diagnostics
                if d.get("severity") == "error"
                or d.get("code", "").startswith("render.dlss")
                or "fallback" in d.get("message", "").lower()
            ], diagnostics
            assert frame["generation"] == generation and frame["complete"], frame.keys()
            # Temporal DLSS normalizes each accumulation buffer independently;
            # published metadata must retain the exact cumulative authored total.
            assert status["rendering"]["samples"] == samples, status
            assert status["current_sample"] == frame["samples"] == samples, (label, status, frame["samples"])
            plane = frame["planes"]["combined"]
            image = np.frombuffer(plane["pixels"], np.float32).reshape(height, width, plane["channels"]).copy()
            assert image.shape == (height, width, 4) and np.isfinite(image).all()
            energy = image[:, :, :3].sum(axis=(0, 1))
            assert energy.sum() > 1, energy
            observed = energy / energy.sum()
            expected = expected_color(color)
            assert np.max(np.abs(observed - expected)) < 0.065, (label, observed, expected)
            # A raw Gaussian with a 4:1 scale ratio must keep elongated support;
            # this also detects silently rendering the PLY as ordinary spheres.
            weights = np.maximum(image[:, :, :3].sum(axis=2), 0)
            yy, xx = np.indices(weights.shape)
            cx = (xx * weights).sum() / weights.sum()
            cy = (yy * weights).sum() / weights.sum()
            ratio = float((((xx - cx) ** 2) * weights).sum() / (((yy - cy) ** 2) * weights).sum())
            assert ratio > 4, (label, ratio)
            np.save(args.report.parent / (label + ".npy"), image)
            images.append(image)
            report["checks"].append(
                dict(
                    label=label,
                    status=status,
                    temporal_samples=dict(authored=samples, status=status["current_sample"], frame=frame["samples"]),
                    observed_chromaticity=observed.tolist(),
                    expected_chromaticity=expected.tolist(),
                    support_variance_ratio=ratio,
                )
            )
        # Evaluate the edited splat itself: averaging a mostly empty raster
        # dilutes the real radiance change according to camera framing.
        support = np.maximum(images[0][:, :, 3], images[1][:, :, 3]) > 0.1
        assert support.sum() > 32, int(support.sum())
        support_change = float(np.abs(images[1][:, :, :3] - images[0][:, :, :3])[support].mean())
        report["radiance_edit"] = dict(support_pixels=int(support.sum()), mean_rgb_change=support_change)
        assert support_change > 0.05, report["radiance_edit"]
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(args.report)
