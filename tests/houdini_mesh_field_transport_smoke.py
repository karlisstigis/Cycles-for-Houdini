# SPDX-License-Identifier: Apache-2.0
"""Dense field-only edits against full-copy transport, with independent AOV checks."""

import argparse
import json
import time
import traceback
from pathlib import Path
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import _native
from mesh_field_fixture import Fixture

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
device = json.loads(device) if device.startswith("[") else device
f = Fixture(device if isinstance(device, str) else "CPU")
path = f.dest / "field-transport-report.json"
report = dict(result="running", device=device, scope=__doc__, controls={})
original_encode = _native.encode_snapshot

edits = f.edits
previous = f.output
states = (
    ("initial", None, ""),
    ("uv", "uv", "v@uv.x+=.1;"),
    ("color", "Cd", "v@Cd.y+=.1;"),
    ("named_vector", "paint", "v@paint.z+=.1;"),
    ("named_scalar", "mask", "f@mask+=.1;"),
    ("uv_repair", "uv", ""),
    ("color_repair", "Cd", ""),
    ("vector_repair", "paint", ""),
    ("scalar_repair", "mask", ""),
    ("camera", None, ""),
    ("object", None, ""),
)


def plane(frame, name):
    value = frame["planes"][name]
    return np.frombuffer(value["pixels"], np.float32).reshape(frame["height"], frame["width"], value["channels"])


def completed_status(session, frame):
    deadline = time.monotonic() + 15
    while True:
        status = session.status()
        if status["state"] == "failed" or (
            status["state"] == "complete" and status["generation"] == frame["generation"]
        ):
            return status
        assert time.monotonic() < deadline, status
        time.sleep(0.005)


try:
    reference = {}
    for mode in ("full", "fields"):
        rows = []
        encodes = []
        enabled = mode == "fields"
        offsets = {name: 0 for name in edits}
        for node in edits.values():
            node.parm("snippet").set("")
        f.camera.parm("tx").set(0)
        f.obj.parm("tx").set(0)

        def encode(desired, acknowledged=None, *, _enabled=enabled):
            began = time.perf_counter()
            packet = original_encode(desired, acknowledged, _enabled)
            encodes.append(dict(bytes=len(packet), seconds=time.perf_counter() - began))
            return packet

        _native.encode_snapshot = encode
        with ch.Session(
            device=device,
            width=f.width,
            height=f.height,
            samples=1,
            threads=2,
            execution="worker",
            use_texture_cache=False,
            collect_diagnostics=True,
        ) as session:
            for label, name, code in states:
                if name:
                    edits[name].parm("snippet").set(code)
                    offsets[name] = 0.1 if code else 0
                if label == "camera":
                    f.camera.parm("tx").set(0.07)
                if label == "object":
                    f.obj.parm("tx").set(0.04)
                began = time.perf_counter()
                start = len(encodes)
                update = f.update()
                update["passes"] = ["combined", "position", "depth", "normal", "uv", "material_id"]
                frame = session.wait(session.apply(update, reuse_if_unchanged=True), timeout=240)
                elapsed = time.perf_counter() - began
                status = completed_status(session, frame)
                assert status["state"] == "complete" and not status["error"] and not status["source_diagnostics"], (
                    status
                )
                assert f.prototype.cookCount() == 1
                assert status["object_resources"] == 12 and status["mesh_variant_resources"] == 1, status
                pixels = plane(frame, "combined")
                mask = (pixels[:, :, 3] > 0.999) & (plane(frame, "depth")[:, :, 0] < 100)
                assert mask.sum() > 500
                expected = (
                    np.array((0.33, 0.48, 0.65)) + offsets["mask"] + np.array((0, offsets["Cd"], offsets["paint"]))
                )
                np.testing.assert_allclose(
                    pixels[:, :, :3][mask], np.broadcast_to(expected, (mask.sum(), 3)), rtol=0, atol=3e-5
                )
                uv = plane(frame, "uv")[:, :, :2][mask]
                expected_uv = np.array((0.2 + offsets["uv"], 0.4))
                np.testing.assert_allclose(uv, np.broadcast_to(expected_uv, uv.shape), rtol=0, atol=3e-5)
                np.testing.assert_allclose(plane(frame, "position")[:, :, 2][mask], 0, rtol=0, atol=3e-5)
                images = {key: plane(frame, key).copy() for key in frame["planes"]}
                if mode == "full":
                    reference[label] = images
                else:
                    assert images.keys() == reference[label].keys()
                    for key, value in images.items():
                        old = reference[label][key]
                        if key.startswith("Crypto") or key == "material_id":
                            np.testing.assert_array_equal(value.view(np.uint32), old.view(np.uint32))
                        else:
                            np.testing.assert_allclose(value, old, rtol=0, atol=2e-5, err_msg=key)
                actual = encodes[start:]
                assert actual
                if mode == "fields" and name:
                    full = next(row for row in report["controls"]["full"] if row["label"] == label)
                    assert actual[-1]["bytes"] < full["encodes"][-1]["bytes"] * 0.55, (label, actual, full["encodes"])
                if label in ("camera", "object"):
                    assert actual[-1]["bytes"] < rows[0]["encodes"][-1]["bytes"] * 0.001, actual
                    assert status["geometry_conversions"] == rows[-1]["status"]["geometry_conversions"], status
                source_ids = [
                    list(previous.geometry().findPointAttrib(n).dataId().vexAttribDataId())
                    for n in ("P", "uv", "Cd", "paint", "mask")
                ]
                if rows:
                    assert source_ids[0] == rows[0]["source_data_ids"][0]
                    assert status["mesh_triangulated_faces"] == rows[0]["status"]["mesh_triangulated_faces"]
                    if name:
                        assert status["mesh_topology_reuses"] > rows[-1]["status"]["mesh_topology_reuses"]
                        assert status["mesh_retained_faces"] > rows[-1]["status"]["mesh_retained_faces"]
                rows.append(
                    dict(
                        label=label,
                        encodes=actual,
                        complete_image_seconds=elapsed,
                        status=status,
                        apply_profile=dict(session._controller.backend.last_apply_profile),
                        source_data_ids=source_ids,
                    )
                )
                report["controls"][mode] = rows
                path.write_text(json.dumps(report, indent=2))
        closed = session.status()
        assert closed["device_allocation_bytes"] == closed["owned_geometry_bytes"] == 0, closed
    report.update(result="passed", paired_states=len(states), triangles=1_048_576, placements=12)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
finally:
    _native.encode_snapshot = original_encode
    path.write_text(json.dumps(report, indent=2))
assert report["result"] == "passed", report.get("error")
print(json.dumps(dict(result=report["result"], paired_states=report["paired_states"])))
