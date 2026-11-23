# SPDX-License-Identifier: Apache-2.0
"""Finite IPR budgets honor the same convergence target as final rendering."""

from support.paths import artifacts_root

import argparse, json, os, time
from pathlib import Path
import numpy as np
import cycles_houdini as ch

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
destination = artifacts_root() / "stage3/render-settings"
destination.mkdir(parents=True, exist_ok=True)
maximum = 1024
checks = []
frames = {}
counts = {}
settings = dict(
    samples=maximum,
    min_samples=128,
    adaptive_sampling=True,
    noise_threshold=0.0001,
    sampling_pattern="blue_noise_first",
    seed=0,
)


def plane(frame, name):
    p = frame["planes"][name]
    return np.frombuffer(p["pixels"], np.float32).reshape(frame["height"], frame["width"], p["channels"]).copy()


for interactive in (False, True):
    with ch.Session(
        device=device,
        width=64,
        height=48,
        samples=maximum,
        interactive=interactive,
        threads=4,
        denoising=dict(enabled=False),
        passes=["combined", "sample_count"],
    ) as session:
        session.wait(session.calibration())
        before = session.status()
        if interactive:
            # Change the sampling policy during a running IPR, retaining its worker and geometry.
            session.apply(dict(rendering=dict(samples=1000000, adaptive_sampling=False)))
            deadline = time.monotonic() + 30
            while True:
                active = session.status()
                if active["state"] == "rendering" and active["current_sample"] > 0:
                    break
                assert time.monotonic() < deadline, active
                time.sleep(0.01)
            assert active["current_sample"] < 1000000, active
        strict = session.wait(session.apply(dict(rendering=settings)))
        frames[interactive] = plane(strict, "combined")
        counts[interactive] = plane(strict, "sample_count")[:, :, 0]
        after = session.status()
        assert after["geometry_conversions"] == before["geometry_conversions"]
        if interactive:
            assert after["renderer_pid"] == before["renderer_pid"]
            fixed = session.wait(session.apply(dict(rendering=dict(settings, min_samples=maximum))))
            fixed_counts = plane(fixed, "sample_count")
            assert np.allclose(fixed_counts, maximum, atol=0.01, rtol=0), fixed_counts
            loose = session.wait(session.apply(dict(rendering=dict(settings, noise_threshold=0.3))))
            loose_counts = plane(loose, "sample_count")[:, :, 0]
            assert loose_counts.mean() < counts[True].mean() * 0.9, (loose_counts.mean(), counts[True].mean())
            checks.append(
                dict(
                    name="Live sampling changes retain worker/geometry; Min=Max uses every sample; looser targets stop early",
                    strict_mean=float(counts[True].mean()),
                    loose_mean=float(loose_counts.mean()),
                    status=session.status(),
                )
            )

# Identical explicit sample sequences isolate scheduling from sampler choice.
hard = counts[False] >= maximum - 0.5
assert hard.sum() > 32, hard.sum()
fraction = float(np.mean(counts[True][hard] >= maximum - 0.5))
assert fraction > 0.99, (
    "IPR froze pixels at a relaxed threshold before the global budget ended",
    fraction,
    float(counts[False].mean()),
    float(counts[True].mean()),
)
error = float(np.sqrt(np.mean((frames[False][hard].astype(float) - frames[True][hard].astype(float)) ** 2)))
assert error < 1e-4, error
checks.append(
    dict(
        name="Strict IPR matches final convergence and raw radiance on independently identified unconverged pixels",
        hard_pixels=int(hard.sum()),
        fraction_reaching_maximum=fraction,
        raw_rms_difference=error,
        final_mean_samples=float(counts[False].mean()),
        ipr_mean_samples=float(counts[True].mean()),
    )
)
report = dict(
    result="passed", device=device, execution=os.environ.get("CYCLES_HOUDINI_EXECUTION", "sync"), checks=checks
)
backend = "CPU" if device == "CPU" else "OPTIX"
path = destination / ("houdini-adaptive-target-smoke-" + backend + ".json")
path.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
