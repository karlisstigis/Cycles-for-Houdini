# SPDX-License-Identifier: Apache-2.0
"""Saved native device choices and actual multi-GPU rendering in every execution mode."""

from support.paths import artifacts_root
import argparse
import json
import os
from pathlib import Path
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import device_selection, native_scene, registry

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
parser.parse_args()
destination = artifacts_root() / "stage3/devices"
destination.mkdir(parents=True, exist_ok=True)
available = ch.devices()
gpus = {backend: [value for value in available if value["backend"] == backend] for backend in ("CUDA", "OPTIX")}
assert all(len(values) >= 2 for values in gpus.values()), "This certified-workstation test requires two CUDA/OptiX GPUs"
assert {value["physical_id"] for value in gpus["CUDA"]} == {value["physical_id"] for value in gpus["OPTIX"]}
checks = []
rop = hou.node("/out").createNode("cycles_ipr", "device_selection_test")
parms = device_selection.gpu_parameters(rop)
assert len(parms) == len(gpus["OPTIX"])
assert rop.parm("compute_backend").containingFolders() == ("Devices",)
assert all(rop.parm(name) is None for name in ("device", "device_legacy_seen", "device_selection_version"))
for parm in parms:
    rop.parm(parm.name()).set(1)
rop.setParms(dict(device_mode="gpu", compute_backend="OPTIX"))
selected = native_scene.configuration(rop)[0]
assert set(selected) == {value["id"] for value in gpus["OPTIX"]}
hou.hipFile.save(str(destination / "devices-persist.hiplc"))
hou.hipFile.clear(suppress_save_prompt=True)
hou.hipFile.load(str(destination / "devices-persist.hiplc"), suppress_save_prompt=True)
rop = hou.node("/out/device_selection_test")
assert native_scene.configuration(rop)[0] == selected
device_selection.refresh(rop)
assert native_scene.configuration(rop)[0] == selected
rop.parm("compute_backend").set("CUDA")
assert set(native_scene.configuration(rop)[0]) == {value["id"] for value in gpus["CUDA"]}
assert all(rop.evalParm(parm.name()) for parm in device_selection.gpu_parameters(rop))
checks.append(
    dict(
        name="Each physical GPU appears once; HIP reload, refresh and backend changes preserve checked cards",
        selected=selected,
    )
)

# Scripts and the UI select exactly the same physical-device checkboxes.
device_selection.set_devices(rop, "CPU")
assert native_scene.configuration(rop)[0] == ("CPU",) and rop.evalParm("device_mode") == "cpu"
device_selection.set_devices(rop, gpus["OPTIX"][0]["id"])
assert native_scene.configuration(rop)[0] == (gpus["OPTIX"][0]["id"],)
assert sum(rop.evalParm(p.name()) for p in device_selection.gpu_parameters(rop)) == 1
for p in device_selection.gpu_parameters(rop):
    rop.parm(p.name()).set(0)
try:
    native_scene.configuration(rop)
    raise AssertionError("An empty GPU selection silently chose a device")
except RuntimeError as error:
    assert "No GPU selected" in str(error)
before = tuple(rop.evalParm(p.name()) for p in device_selection.gpu_parameters(rop))
try:
    device_selection.set_devices(rop, "CUDA_missing_card_0000:ff:00_OptiX")
    raise AssertionError("An unavailable device was accepted")
except ValueError as error:
    assert "unavailable" in str(error)
assert tuple(rop.evalParm(p.name()) for p in device_selection.gpu_parameters(rop)) == before
device_selection.set_devices(rop, [v["id"] for v in gpus["OPTIX"]])
saved = tuple(native_scene.configuration(rop)[0])
p = device_selection.gpu_parameters(rop)[0]
physical = p.tags()["cycles_gpu_physical_id"]
cache = device_selection._device_cache
device_selection._device_cache = [v for v in cache if v["physical_id"] != physical]
try:
    native_scene.configuration(rop)
    raise AssertionError("A checked unavailable physical GPU silently fell back")
except RuntimeError as error:
    assert "unavailable" in str(error)
finally:
    device_selection._device_cache = cache
assert native_scene.configuration(rop)[0] == saved
checks.append(
    dict(
        name="Current CPU/single-GPU/multi-GPU controls; empty and unavailable choices fail explicitly without hidden compatibility channels"
    )
)


def beauty(frame):
    value = frame["planes"]["combined"]
    return np.frombuffer(value["pixels"], np.float32).reshape(frame["height"], frame["width"], 4)


for backend, values in gpus.items():
    ids = tuple(sorted(value["id"] for value in values))
    with ch.Session(
        device=ids,
        width=256,
        height=160,
        samples=32,
        interactive=True,
        collect_diagnostics=True,
        passes=["combined", "depth", "normal"],
    ) as session:
        initial = session.wait(session.calibration(), timeout=90)
        assert tuple(session.status()["render_device_ids"]) == ids, session.status()
        pixels = beauty(initial)
        assert np.isfinite(pixels).all() and np.count_nonzero(pixels[:, :, :3] > 0.02) > 100
        geometry = session.status()["geometry_conversions"]
        cropped = session.wait(session.apply(dict(crop=(48, 32, 128, 96))), timeout=60)
        assert tuple(cropped[key] for key in ("x", "y", "width", "height")) == (48, 32, 128, 96)
        restored = session.wait(session.apply(dict(crop=(0, 0, 256, 160))), timeout=60)
        assert restored["complete"] and tuple(restored[key] for key in ("x", "y", "width", "height")) == (
            0,
            0,
            256,
            160,
        )
        for method in ("oidn", "optix") if backend == "OPTIX" else ("oidn",):
            denoised = session.wait(
                session.apply(dict(denoising=dict(enabled=True, method=method, use_gpu=True))), timeout=60
            )
            status = session.status()
            assert status["denoise_device_id"] in ids, status
            assert status["denoiser"] == ("optix" if method == "optix" else "oidn_gpu"), status
            assert "noisy_combined" in denoised["planes"] and np.isfinite(beauty(denoised)).all()
        assert session.status()["geometry_conversions"] == geometry
        session.write_exr(destination / (backend.lower() + "-multi.exr"))
        checks.append(
            dict(
                name=backend
                + " uses all selected GPUs, supports region/reset and live GPU denoising without geometry reconversion",
                status=session.status(),
            )
        )
        session.cancel()
        session.close()
        assert session.status()["state"] == "closed" and session.status()["device_allocation_bytes"] == 0

invalid = [
    (),
    (gpus["CUDA"][0]["id"],) * 2,
    ("CUDA_unavailable",),
    ("CPU", gpus["CUDA"][0]["id"]),
    (gpus["CUDA"][0]["id"], gpus["OPTIX"][0]["id"]),
    (gpus["CUDA"][0]["id"], gpus["OPTIX"][1]["id"]),
]
for ids in invalid:
    try:
        with ch.Session(device=ids, width=32, height=32, samples=1) as session:
            session.wait(session.calibration(), timeout=20)
        raise AssertionError("Invalid device selection was accepted: " + repr(ids))
    except (RuntimeError, ValueError) as error:
        checks.append(dict(name="Rejected device selection", selection=ids, error=str(error)))
registry.close_all()
report = dict(result="passed", execution=os.environ.get("CYCLES_HOUDINI_EXECUTION", "sync"), checks=checks)
(destination / "houdini-device-selection-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
