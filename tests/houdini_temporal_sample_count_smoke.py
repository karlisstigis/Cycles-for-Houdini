# SPDX-License-Identifier: Apache-2.0
"""Raw Sample Count survives temporal buffer resets; progress stays cumulative."""

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
target = Path(args.report)
target.parent.mkdir(parents=True, exist_ok=True)
report = dict(result="running", device=args.device, cases=[])
SAMPLES = 7


def plane(frame, name):
    value = frame["planes"][name]
    return np.frombuffer(value["pixels"], np.float32).reshape(frame["height"], frame["width"], value["channels"])


def wait(session, generation, label, temporal=False):
    deadline = time.monotonic() + 120
    observed = []
    seen = set()
    while True:
        status = session.status()
        assert status["state"] not in ("failed", "closed", "cancelled"), (label, status)
        info = session.frame_info()
        if info and info["generation"] == generation and info.get("current_scene", True):
            if info["sequence"] not in seen:
                seen.add(info["sequence"])
                observed.append(dict(sequence=info["sequence"], samples=info["samples"], complete=info["complete"]))
            if (
                info["complete"]
                and status.get("backend_state", status["state"]) == "complete"
                and not status.get("pending_update")
                and not status.get("capture_pending")
            ):
                break
        assert time.monotonic() < deadline, (label, status, observed)
        time.sleep(0.002)
    frame = session.frame()
    assert frame["generation"] == generation and frame["complete"], (label, frame)
    counts = plane(frame, "sample_count")[:, :, 0]
    assert np.isfinite(counts).all() and np.allclose(counts, np.rint(counts), atol=1e-4, rtol=0), label
    assert np.isfinite(plane(frame, "combined")).all(), label
    result = dict(
        name=label,
        generation=generation,
        cumulative_samples=frame["samples"],
        raw_count_min=float(counts.min()),
        raw_count_max=float(counts.max()),
        raw_count_mean=float(counts.mean()),
        observed_frames=observed,
        status=status,
    )
    report["cases"].append(result)
    target.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if temporal:
        assert status["denoiser"] == "dlss", result
        assert frame["samples"] == status["current_sample"] == SAMPLES, result
        # With adaptive sampling disabled, every pixel holds exactly C current
        # samples. The first single-sample DLSS update necessarily precedes the
        # final batch, so its reset leaves 0 < C < cumulative T in THIS generation.
        # The old adapter multiplies n/C by T and returns T at every pixel here.
        assert np.allclose(counts, counts.flat[0], atol=1e-4, rtol=0), result
        assert 0 < counts.min() <= counts.max() < frame["samples"], (
            "Sample Count used cumulative progress instead of the current buffer",
            result,
        )
        result["same_generation_total_exceeds_current"] = True
    return frame, counts, result


try:
    assert ch.capabilities()["image_iris"], "Image Iris disabled in candidate runtime"
    with ch.Session(
        device=args.device,
        width=128,
        height=96,
        samples=SAMPLES,
        interactive=True,
        execution="worker",
        denoising=dict(enabled=True, method="dlss", upscale_factor=2),
        passes=["combined", "noisy_combined", "depth", "sample_count"],
    ) as session:
        # Pin non-adaptive counts rather than relying on constructor defaults.
        session.calibration()
        generation = session.apply(dict(rendering=dict(samples=SAMPLES, adaptive_sampling=False)))
        _, _, first = wait(session, generation, "Temporal current buffer versus cumulative progress", temporal=True)
        renderer_pid = first["status"]["renderer_pid"]
        generations = [generation]
        for x in (-0.2, 0.2):
            generation = session.apply(dict(camera=dict(transform=(1, 0, 0, x, 0, 1, 0, 0, 0, 0, -1, 5))))
            _, _, case = wait(session, generation, f"Temporal generation reset x={x:g}", temporal=True)
            assert case["status"]["renderer_pid"] == renderer_pid, case
            generations.append(generation)
        assert len(set(generations)) == 3, generations

        generation = session.apply(
            dict(denoising=dict(enabled=False), rendering=dict(samples=SAMPLES, adaptive_sampling=False))
        )
        frame, counts, fixed = wait(session, generation, "Ordinary fixed accumulation control")
        assert frame["samples"] == SAMPLES and np.allclose(counts, SAMPLES, atol=1e-4, rtol=0), fixed

        generation = session.apply(
            dict(rendering=dict(samples=128, adaptive_sampling=True, min_samples=16, noise_threshold=1))
        )
        frame, counts, adaptive = wait(session, generation, "Ordinary adaptive accumulation control")
        assert counts.min() >= 16 and counts.max() <= frame["samples"] <= 128, adaptive
        assert np.any(counts < counts.max()) or adaptive["status"]["current_sample"] < 128, adaptive

        generation = session.apply(dict(rendering=dict(samples=3, adaptive_sampling=False)))
        frame, counts, reset = wait(session, generation, "Ordinary generation reset control")
        assert frame["samples"] == 3 and np.allclose(counts, 3, atol=1e-4, rtol=0), reset
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    target.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
