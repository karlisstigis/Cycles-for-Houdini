# SPDX-License-Identifier: Apache-2.0
"""Noisy beauty/data-pass invariance, live denoising and explicit device policy."""

from support.paths import artifacts_root
import argparse, json, os
from pathlib import Path
import numpy as np
import cycles_houdini as ch

parser = argparse.ArgumentParser()
parser.add_argument("--device")
args = parser.parse_args()
selected = (json.loads(args.device) if args.device.startswith("[") else args.device) if args.device else None
reports = []
for device in [selected] if selected is not None else ("CPU", ch.default_device()):
    with ch.Session(device=device, width=128, height=96, samples=32, interactive=False) as plain:
        reference = plain.wait(plain.calibration())
    methods = [("auto", True)] if device == "CPU" else [("auto", True), ("oidn", False), ("optix", True)]
    for method, use_gpu in methods:
        with ch.Session(
            device=device,
            width=128,
            height=96,
            samples=32,
            interactive=False,
            denoising=dict(enabled=True, method=method, use_gpu=use_gpu),
        ) as session:
            frame = session.wait(session.calibration())
            errors = {}
            for name in ("depth", "normal", "uv", "position"):
                actual = np.frombuffer(frame["planes"][name]["pixels"], np.float32)
                expected_raw = np.frombuffer(reference["planes"][name]["pixels"], np.float32)
                errors[name] = float(np.max(np.abs(actual - expected_raw)))
                assert np.allclose(actual, expected_raw, rtol=1e-6, atol=1e-6), (device, method, name, errors[name])
            noisy = np.frombuffer(frame["planes"]["noisy_combined"]["pixels"], np.float32)
            expected = np.frombuffer(reference["planes"]["combined"]["pixels"], np.float32)
            assert np.allclose(noisy, expected, rtol=1e-5, atol=1e-5), (device, method, "noisy beauty changed")
            denoised = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32)
            assert np.isfinite(denoised).all()
            assert np.array_equal(denoised.reshape(-1, 4)[:, 3], noisy.reshape(-1, 4)[:, 3]), (
                "Denoising altered coverage"
            )
            before = session.status()
            session.apply(dict(denoising=dict(enabled=False)))
            disabled = session.wait()
            assert session.status()["geometry_conversions"] == before["geometry_conversions"]
            assert "noisy_combined" not in disabled["planes"]
            reports.append(
                dict(
                    device=device,
                    method=method,
                    use_gpu=use_gpu,
                    denoiser=before["denoiser"],
                    denoise_device=before["denoise_device_id"],
                    raw_pass_max_errors=errors,
                )
            )
        assert session.status()["owned_geometry_bytes"] == session.status()["device_allocation_bytes"] == 0
report = dict(result="passed", cases=reports)
destination = artifacts_root() / "stage3/denoising/houdini-denoising-smoke.json"
destination.parent.mkdir(parents=True, exist_ok=True)
destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
