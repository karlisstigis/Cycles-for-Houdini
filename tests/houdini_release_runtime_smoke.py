# SPDX-License-Identifier: Apache-2.0
"""Exact runtime identity and actual CPU/GPU Image Iris renders, both samplers."""

import argparse
import hashlib
import json
import os
from pathlib import Path

import hou
import numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import camera_properties, image_iris

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--device", default="CPU")
parser.add_argument("--houdini", required=True)
parser.add_argument("--expected-host", type=Path, required=True)
parser.add_argument("--report", type=Path, required=True)
parser.add_argument("--package-id", default="")
args = parser.parse_args()
host = args.expected_host.resolve()
args.report.parent.mkdir(parents=True, exist_ok=True)


def inside(path):
    path = Path(path).resolve()
    assert path.is_relative_to(host), (str(path), str(host))
    return str(path)


assert hou.applicationVersionString() == args.houdini
assert Path(os.environ["CYCLES_HOUDINI_HOST_ROOT"]).resolve() == host
assert Path(os.environ["CYCLES_HOUDINI_RESOURCES"]).resolve() == host / "resources"
identity = dict(python=inside(ch.__file__), native=inside(ch._native.__file__), resources=inside(host / "resources"))
capabilities = ch.capabilities()
lock = (
    host.parent / "provenance/source.lock.json"
    if args.package_id
    else Path(__file__).resolve().parents[1] / "source.lock.json"
)
expected_upstream = json.loads(lock.read_text(encoding="utf-8-sig"))["cycles"]["commit"]
assert capabilities["engine_commit"] == expected_upstream, (capabilities["engine_commit"], expected_upstream)
assert capabilities["image_iris"] and image_iris.supported(), capabilities
assert capabilities["openimagedenoise_provider"]["houdini"] == args.houdini
if args.package_id:
    manifest = json.loads((host.parent / "release-manifest.json").read_text())
    assert manifest["package_id"] == os.environ["CYCLES_HOUDINI_RELEASE_ID"] == args.package_id
    for record in manifest["files"]:
        path = host.parent / record["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == record["sha256"], record["path"]
    identity["package_id"] = manifest["package_id"]
requested_optix = os.environ.get("CYCLES_HOUDINI_TEST_OPTIX_DEVICE")
gpu = requested_optix or ch.default_device()
available_devices = ch.devices()
matching_devices = [device for device in available_devices if device["id"] == gpu]
assert len(matching_devices) == 1, ("Selected release GPU is unavailable", gpu, available_devices)
selected_gpu = matching_devices[0]
assert selected_gpu["backend"] in ("CUDA", "OPTIX"), available_devices
if requested_optix:
    assert selected_gpu["backend"] == "OPTIX" and selected_gpu["id"] == requested_optix, selected_gpu
mask = args.report.parent / "aperture.exr"
y, x = np.mgrid[-1:1:33j, -1:1:33j]
pixels = np.ascontiguousarray(((x * x + y * y < 0.9) * (1 + 0.3 * x)).astype(np.float32))
out = oiio.ImageOutput.create(str(mask))
assert out.open(str(mask), oiio.ImageSpec(33, 33, 1, oiio.FLOAT))
assert out.write_image(pixels[:, :, None])
out.close()
cam = hou.node("/obj").createNode("cam", "release_image_iris")
camera_properties.install(cam)
cam.setParms(dict(tz=5, focus=2.5, fstop=0.05, cycles_dof=1, cycles_image_iris=1, cycles_image_iris_file=str(mask)))
rop = hou.node("/out").createNode("cycles_ipr", "release_image_iris")
identity["hda"] = inside(rop.type().definition().libraryFilePath())
for name in ("nested_dielectrics", "resampled_light_sampling"):
    assert rop.parm(name) is not None, name
rop.parm("camera").set(cam.path())
rop.parm("dof_mode").set("camera")
rows = []
for device in ("CPU", gpu):
    with ch.Session(
        device=device, width=32, height=32, samples=32, threads=2, execution="worker", denoising={"enabled": False}
    ) as session:
        for method, number in [("cdf", 1), ("hierarchical", 2)]:
            cam.parm("cycles_image_iris_method").set(method)
            lens = camera_properties.configuration(rop, hou.time())
            assert lens["image_iris_method"] == number and lens["image_iris"] == str(mask), lens
            frame = session.wait(
                session.apply(
                    dict(
                        geometry=[],
                        camera_path=cam.path(),
                        camera_dof=lens,
                        background=(0.2, 0.4, 0.8),
                        motion={"enabled": False},
                    )
                ),
                timeout=120,
            )
            status = session.status()
            assert not status["error"] and status["render_device_ids"] == [device], status
            assert status["image_iris_preparations"] == 1 and status["image_iris_table_bytes"] > 0, status
            worker = inside(session._controller.backend.process.args[0])
            assert Path(worker) == host / "bin/ch_worker.exe"
            plane = frame["planes"]["combined"]
            image = np.frombuffer(plane["pixels"], np.float32).reshape(32, 32, plane["channels"])
            error = float(np.max(np.abs(image[:, :, :3] - np.array((0.2, 0.4, 0.8)))))
            assert np.isfinite(image).all() and error < 1e-5, error
            rows.append(
                dict(
                    device=device,
                    method=method,
                    preparations=status["image_iris_preparations"],
                    table_bytes=status["image_iris_table_bytes"],
                    maximum_energy_error=error,
                    worker=worker,
                )
            )
report = dict(
    result="passed",
    houdini=args.houdini,
    image_iris_enabled=True,
    engine_upstream_commit=expected_upstream,
    identity=identity,
    selected_gpu=selected_gpu,
    available_devices=available_devices,
    checks=rows,
)
args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
