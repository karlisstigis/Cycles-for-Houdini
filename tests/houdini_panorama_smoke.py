# SPDX-License-Identifier: Apache-2.0
"""Seven real native panoramic camera models against independent colored ray geometry."""

from support.paths import artifacts_root

from native_test_runtime import runtime_executable
import argparse, json, os, subprocess, time, traceback
from pathlib import Path
import hou, numpy as np
from cycles_houdini import native_scene, registry
from panorama_fixture import Fixture, MODELS, reference

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
dest = artifacts_root() / "stage2/cameras/panorama"
dest.mkdir(parents=True, exist_ok=True)
report = dict(result="running", device=device, checks=[])
f = Fixture(device)
entry = registry.for_rop(f.rop, native_scene.configuration(f.rop))
try:
    run = subprocess.run(
        [
            str(runtime_executable("ch_harness.exe")),
            "--resources",
            os.environ["CYCLES_HOUDINI_RESOURCES"],
            "--device",
            device,
            "--panorama-contracts",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 0, (run.stdout, run.stderr)
    report["contracts"] = json.loads(run.stdout.strip().splitlines()[-1])
    for model in MODELS:
        spec = f.configure(model)
        frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
        status = entry.session.status()
        plane = frame["planes"]["combined"]
        pixels = np.frombuffer(plane["pixels"], np.float32).reshape(64, 128, plane["channels"])[:, :, :3]
        expected = reference(spec)
        error = float(np.abs(pixels - expected).mean())
        report["checks"].append(dict(model=model, mean_difference=error, status=status))
        assert (
            status["state"] != "failed"
            and status["geometry_resources"] == status["object_resources"] == 6
            and not status["source_diagnostics"]
        ), status
        if not np.isfinite(pixels).all() or error >= 0.0012:
            np.savez(dest / "failed-image.npz", expected=expected, actual=pixels)
            raise AssertionError((model, error))
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    registry.close_all()
    (dest / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
