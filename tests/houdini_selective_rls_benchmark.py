# SPDX-License-Identifier: Apache-2.0
"""Immutable-host, warmed equal-time comparison on the artist's two RLS scenes."""

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import time
import traceback
import hou
import numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import native_scene

p = argparse.ArgumentParser()
p.add_argument("--scene", type=Path, required=True)
p.add_argument("--output", type=Path, required=True)
p.add_argument("--label", required=True)
p.add_argument("--host", type=Path, required=True)
p.add_argument("--rls", type=int, choices=(0, 1), required=True)
p.add_argument("--seconds", type=float, default=25)
p.add_argument("--seeds", type=int, default=8)
p.add_argument("--seed-start", type=int, default=410)
p.add_argument("--width", type=int, default=640)
p.add_argument("--height", type=int, default=360)
a = p.parse_args()
out = a.output / a.label
out.mkdir(parents=True, exist_ok=False)
assert Path(os.environ["CYCLES_HOUDINI_HOST_ROOT"]).resolve() == a.host.resolve()
source_hash = hashlib.sha256(a.scene.read_bytes()).hexdigest()
report = dict(result="running", host=str(a.host), args=vars(a), source_sha256=source_hash, runs=[])


def save():
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str))


try:
    hou.hipFile.load(str(a.scene), suppress_save_prompt=True, ignore_load_warnings=True)
    hou.setFrame(1)
    rop = hou.node("/out/cycles_render")
    assert rop
    rop.setParms(
        dict(
            resx=a.width,
            resy=a.height,
            resolution_percent=100,
            ipr_resolution_percent=100,
            use_denoise=0,
            ipr_use_denoise=0,
            render_region=0,
            pass_count=0,
        )
    )
    update = native_scene.capture(rop)
    update["rendering"].update(
        samples=1048576,
        adaptive_sampling=False,
        animate_seed=False,
        sample_subset=False,
        time_limit=a.seconds,
        resampled_light_sampling=bool(a.rls),
        clamp_direct=0.0,
        clamp_indirect=0.0,
    )
    update["denoising"]["enabled"] = False
    update["passes"] = ["combined"]
    report.update(
        rendering=copy.deepcopy(update["rendering"]),
        camera_dof=update.get("camera_dof"),
        resolution=[a.width, a.height],
    )
    device = next(d["id"] for d in ch.devices() if d["backend"] == "OPTIX" and "4090" in d["name"])
    report["device"] = device
    save()
    with ch.Session(
        device=device,
        width=a.width,
        height=a.height,
        samples=1048576,
        threads=4,
        interactive=True,
        use_texture_cache=False,
        denoising={"enabled": False},
        execution="worker",
        passes=["combined"],
    ) as session:

        def render(seed, seconds, samples):
            u = copy.deepcopy(update)
            u["rendering"].update(seed=seed, time_limit=seconds, samples=samples)
            start = time.monotonic()
            frame = session.wait(session.apply(u), timeout=max(180, seconds + 120))
            status = session.status()
            assert not status["error"], status
            plane = frame["planes"]["combined"]
            data = np.frombuffer(plane["pixels"], np.float32).reshape(a.height, a.width, plane["channels"]).copy()
            assert np.isfinite(data).all()
            return data, dict(
                seed=seed,
                samples=status["current_sample"],
                seconds=status["elapsed_seconds"],
                wall_seconds=time.monotonic() - start,
                mean_rgb=data[..., :3].mean((0, 1)).tolist(),
                alpha_mean=float(data[..., 3].mean()),
                image_iris_bytes=status["image_iris_table_bytes"],
            )

        _, warm = render(998, 0, 4)
        report["warmup"] = warm
        report["output_metadata"] = session.output_metadata()
        for seed in range(a.seed_start, a.seed_start + a.seeds):
            data, row = render(seed, a.seconds, 1048576)
            np.save(out / f"seed{seed}.npy", data)
            report["runs"].append(row)
            save()
            if seed == a.seed_start:
                image = oiio.ImageOutput.create(str(out / f"seed{seed}.exr"))
                assert image.open(str(out / f"seed{seed}.exr"), oiio.ImageSpec(a.width, a.height, 4, oiio.FLOAT))
                assert image.write_image(data) and image.close()
            print(json.dumps(row), flush=True)
    report["source_unchanged"] = hashlib.sha256(a.scene.read_bytes()).hexdigest() == source_hash
    assert report["source_unchanged"]
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    print(report["error"], flush=True)
save()
raise SystemExit(0 if report["result"] == "passed" else 1)
