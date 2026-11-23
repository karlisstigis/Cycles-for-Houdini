# SPDX-License-Identifier: Apache-2.0
"""Real DLSS worker pixels, temporal edits, finite sample budget and camera latency."""

import argparse
import json
from pathlib import Path
import time
import traceback
import numpy as np
import cycles_houdini as ch

parser = argparse.ArgumentParser()
parser.add_argument("--device", required=True)
parser.add_argument("--report", required=True)
args = parser.parse_args()
WIDTH, HEIGHT, SAMPLES = 128, 96, 7
report = dict(
    result="running",
    device=args.device,
    width=WIDTH,
    height=HEIGHT,
    authored_samples=SAMPLES,
    cases=[],
    responsiveness={},
)
target = Path(args.report)
target.parent.mkdir(parents=True, exist_ok=True)
# Preserve malformed control-header bytes as diagnostics; never bypass validation.
from cycles_houdini import queued

_original_read_exact = queued.ExternalBackend.read_exact


def _record_protocol_bytes(backend, count):
    value = _original_read_exact(backend, count)
    if count == queued._HEADER.size and not value.startswith(b"CHWP"):
        report["invalid_worker_header"] = dict(
            hex=value.hex(), text=value.decode("utf-8", errors="replace"), log=str(backend.log_path)
        )
    return value


queued.ExternalBackend.read_exact = _record_protocol_bytes


def camera(x=0):
    # Public core convention: +Z forward; Houdini camera_path converts its basis.
    return dict(transform=(1, 0, 0, x, 0, 1, 0, 0, 0, 0, -1, 5))


def pixels(frame):
    plane = frame["planes"]["combined"]
    # frame() expands logical raster; compact sizes live in frame_info().
    return np.frombuffer(plane["pixels"], np.float32).reshape(frame["height"], frame["width"], plane["channels"])


def wait(session, generation, label):
    began = time.perf_counter()
    first = None
    records = []
    seen = set()
    maximum_sample = 0
    while True:
        status = session.status()
        if status["state"] in ("failed", "closed", "cancelled"):
            raise AssertionError((label, status))
        maximum_sample = max(maximum_sample, status["current_sample"])
        assert maximum_sample <= SAMPLES, (label, "authored budget exceeded", maximum_sample, status)
        info = session.frame_info()
        if info and info["generation"] == generation and info.get("current_scene", True):
            if first is None:
                first = time.perf_counter() - began
            if info["sequence"] not in seen:
                seen.add(info["sequence"])
                records.append(
                    dict(
                        sequence=info["sequence"],
                        complete=info["complete"],
                        samples=info["samples"],
                        logical_size=[info["width"], info["height"]],
                        rendered_size=[info["rendered_width"], info["rendered_height"]],
                        plane_storage=info["plane_storage"],
                    )
                )
            if (
                info["complete"]
                and info["samples"] == SAMPLES
                and status["current_sample"] == SAMPLES
                and status.get("backend_state", status["state"]) == "complete"
                and not status.get("pending_update")
                and not status.get("capture_pending")
            ):
                break
        if time.perf_counter() - began > 120:
            raise TimeoutError((label, status, records[-2:]))
        time.sleep(0.002)
    frame = session.frame()
    assert frame and frame["generation"] == generation and frame["complete"] and frame["samples"] == SAMPLES
    rgba = pixels(frame)
    assert rgba.shape[2] == 4 and np.isfinite(rgba).all(), label
    assert rgba[:, :, 3].min() >= -0.002 and rgba[:, :, 3].max() <= 1.002, label
    assert rgba[:, :, :3].max() > 0.01, label
    assert status["current_sample"] == SAMPLES, (label, status)
    result = dict(
        name=label,
        first_frame_seconds=first,
        complete_seconds=time.perf_counter() - began,
        maximum_sample=maximum_sample,
        status=status,
        frames=records,
        alpha_range=[float(rgba[:, :, 3].min()), float(rgba[:, :, 3].max())],
    )
    report["cases"].append(result)
    target.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return frame, result


def apply_wait(session, update, label):
    began = time.perf_counter()
    generation = session.apply(update)
    elapsed = time.perf_counter() - began
    frame, result = wait(session, generation, label)
    result.update(apply_seconds=elapsed, end_to_first_frame_seconds=elapsed + result["first_frame_seconds"])
    return frame, result


def centroid(frame):
    alpha = pixels(frame)[:, :, 3].clip(0, 1)
    yy, xx = np.indices(alpha.shape)
    assert alpha.sum() > 1
    return float((xx * alpha).sum() / alpha.sum()), float((yy * alpha).sum() / alpha.sum())


try:
    assert ch.capabilities()["image_iris"], "Image Iris disabled in candidate runtime"
    with ch.Session(
        device=args.device,
        width=WIDTH,
        height=HEIGHT,
        samples=SAMPLES,
        interactive=True,
        execution="worker",
        denoising=dict(enabled=True, method="dlss", upscale_factor=2),
        passes=["combined", "noisy_combined", "depth"],
    ) as session:
        frame, initial = wait(session, session.calibration(), "DLSS worker initialization and finite sample budget")
        status = initial["status"]
        renderer_pid = status["renderer_pid"]
        assert status["denoiser"] == "dlss" and renderer_pid > 0 and status["execution"] == "worker", status
        assert any(d["id"] == args.device and "dlss" in d["denoisers"] for d in status["worker_devices"]), status
        assert (frame["width"], frame["height"], frame["full_width"], frame["full_height"]) == (
            WIDTH,
            HEIGHT,
            WIDTH,
            HEIGHT,
        )
        assert not any(
            name in frame["planes"]
            for name in (
                "denoising_specular_albedo",
                "denoising_roughness",
                "denoising_backward_motion",
                "denoising_specular_motion",
            )
        )
        for observed in initial["frames"]:
            assert observed["plane_storage"]["combined"]["width"] == WIDTH, observed
            assert observed["plane_storage"]["combined"]["height"] == HEIGHT, observed
            if not observed["complete"] and "noisy_combined" in observed["plane_storage"]:
                assert observed["plane_storage"]["noisy_combined"]["width"] == WIDTH // 2, observed
                assert observed["plane_storage"]["noisy_combined"]["height"] == HEIGHT // 2, observed
        settled = session.frame_info()["sequence"]
        time.sleep(0.2)
        assert session.status()["current_sample"] == SAMPLES and session.frame_info()["sequence"] == settled
        apply_wait(
            session,
            dict(
                transparent_background=True,
                materials=[dict(key="calibration/red", kind="emission", color=(0.05, 0.7, 0.2), strength=1)],
            ),
            "Transparent DLSS beauty and alpha",
        )
        centers = []
        for x in (-0.25, 0.0, 0.25):
            moved, record = apply_wait(session, dict(camera=camera(x)), f"Camera reprojection x={x:g}")
            centers.append(centroid(moved))
            record["alpha_centroid"] = centers[-1]
            assert record["status"]["renderer_pid"] == renderer_pid
            rgba = pixels(moved)
            assert rgba[HEIGHT // 2, WIDTH // 2, 3] > 0.9 and rgba[0, 0, 3] < 0.02
        assert centers[0][0] > centers[1][0] + 2 and centers[1][0] > centers[2][0] + 2, centers
        for color in ((0.8, 0.03, 0.02), (0.02, 0.8, 0.03)):
            changed, record = apply_wait(
                session,
                dict(
                    camera=camera(), materials=[dict(key="calibration/red", kind="emission", color=color, strength=1)]
                ),
                "Material edit resets temporal content",
            )
            center = pixels(changed)[HEIGHT // 2, WIDTH // 2, :3]
            assert np.allclose(center, color, atol=0.08, rtol=0.15), (center.tolist(), color)
            record.update(expected_color=color, center=center.tolist())
        sharp, _ = apply_wait(
            session,
            dict(camera=camera(), camera_dof=dict(aperture_size=0.3, focus_distance=5)),
            "Focused aperture reference",
        )
        blurred, focus = apply_wait(
            session,
            dict(camera=camera(), camera_dof=dict(aperture_size=0.3, focus_distance=2)),
            "Focus edit changes temporal optical image",
        )
        focus["maximum_pixel_change"] = float(np.max(np.abs(pixels(sharp) - pixels(blurred))))
        assert focus["maximum_pixel_change"] > 0.02, focus
        cropped, crop = apply_wait(
            session,
            dict(camera=camera(), camera_dof=dict(aperture_size=0, focus_distance=5), crop=(16, 16, 96, 64)),
            "Crop edit changes logical output bounds",
        )
        assert (
            cropped["x"],
            cropped["y"],
            cropped["width"],
            cropped["height"],
            cropped["full_width"],
            cropped["full_height"],
        ) == (16, 16, 96, 64, WIDTH, HEIGHT)
        assert pixels(cropped).shape == (64, 96, 4)
        apply_wait(session, dict(crop=(0, 0, WIDTH, HEIGHT)), "Restore full raster after crop")
        conventional, policy = apply_wait(
            session,
            dict(passes=["combined", "motion"], denoising=dict(enabled=True, method="dlss", upscale_factor=2)),
            "Explicit motion AOV retains authored semantics",
        )
        assert policy["status"]["denoiser"] in ("oidn_gpu", "oidn_cpu", "optix"), policy
        assert any(issue["code"] == "render.dlss_motion" for issue in policy["status"]["source_diagnostics"])
        assert "motion" in conventional["planes"]
        # Warmed small-scene observations, not a general hardware benchmark.
        for name, settings in [
            ("dlss_native", dict(enabled=True, method="dlss", upscale_factor=1)),
            ("dlss_2x", dict(enabled=True, method="dlss", upscale_factor=2)),
            ("conventional", dict(enabled=True, method="auto", start_sample=0)),
        ]:
            apply_wait(
                session,
                dict(passes=["combined", "noisy_combined"], denoising=settings, camera=camera()),
                name + " warmup",
            )
            times = []
            for x in (-0.15, -0.05, 0.05, 0.15):
                _, record = apply_wait(session, dict(camera=camera(x)), name + " camera latency")
                times.append(record["end_to_first_frame_seconds"])
            report["responsiveness"][name] = dict(
                count=len(times),
                median_seconds=float(np.median(times)),
                p95_seconds=float(np.percentile(times, 95)),
                values_seconds=times,
                effective_denoiser=session.status()["denoiser"],
            )
        assert session.status()["renderer_pid"] == renderer_pid
    try:
        with ch.Session(
            device=args.device,
            width=64,
            height=64,
            samples=1,
            interactive=False,
            execution="sync",
            denoising=dict(enabled=True, method="dlss"),
        ):
            pass
    except (ValueError, RuntimeError) as error:
        assert "interactive" in str(error), str(error)
    else:
        raise AssertionError("DLSS accepted for final output")
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    target.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
