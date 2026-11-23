# SPDX-License-Identifier: Apache-2.0
"""Upscaled DLSS output must not read sample counts at output resolution."""

import argparse
import json
from pathlib import Path

import numpy as np
import cycles_houdini as ch
from native_test_runtime import runtime_context

parser = argparse.ArgumentParser()
parser.add_argument("--device", required=True)
parser.add_argument("--report", required=True)
args = parser.parse_args()
report = dict(result="running", runtime=runtime_context(), cases=[])
target = Path(args.report)
target.parent.mkdir(parents=True, exist_ok=True)


def plane(frame, name):
    p = frame["planes"][name]
    return np.frombuffer(p["pixels"], np.float32).reshape(frame["height"], frame["width"], p["channels"])


try:
    assert ch.capabilities()["image_iris"]
    # Odd dimensions exercise rounded input sizes. Explicit Sample Count ensures
    # the pass exists even when adaptive sampling is disabled by the integrator.
    for factor in (1.0, 1.5, 2.0, 3.0):
        with ch.Session(
            device=args.device.split(","),
            width=321,
            height=181,
            samples=16,
            interactive=True,
            execution="worker",
            denoising=dict(enabled=True, method="dlss", upscale_factor=factor),
            passes=["combined", "noisy_combined", "sample_count"],
        ) as session:
            session.wait(session.calibration(), timeout=90)
            frame = session.frame()
            beauty, noisy = plane(frame, "combined"), plane(frame, "noisy_combined")
            ratio = float(beauty[:, :, :3].mean() / noisy[:, :, :3].mean())
            row = dict(
                factor=factor,
                alpha_min=float(beauty[:, :, 3].min()),
                mean_energy_ratio=ratio,
                complete=frame["complete"],
            )
            report["cases"].append(row)
            assert frame["complete"] and np.isfinite(beauty).all(), row
            assert np.all(beauty[:, :, 3] > 0.999), row
            # Deliberately broad denoising tolerance; catches the erroneous
            # second division by the 16 accumulated samples, not noise changes.
            assert 0.7 < ratio < 1.3, row
    report["result"] = "passed"
finally:
    target.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report), flush=True)
