# SPDX-License-Identifier: Apache-2.0
"""Paired same-runtime physical storage, images and warm update observations.

One million-triangle native prototype, twelve packed instances and four surface
styles. The internal sharing-disabled control retains identical source capture,
transport, light-tree cache and kernel code. Timings are observations, not p95
or physical-input latency certification.
"""

import argparse, hashlib, json, os, time, traceback
from pathlib import Path
import numpy as np
import cycles_houdini as ch
from cycles_houdini import device_selection
from stylesheet_storage_fixture import Fixture

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
device = json.loads(device) if device.startswith("[") else device
f = Fixture(device[0] if isinstance(device, list) else device)
if isinstance(device, list):
    device_selection.set_devices(f.rop, device)
dest = f.dest.parent / "mesh-sharing"
dest.mkdir(parents=True, exist_ok=True)
path = dest / "storage-report.json"
report = dict(result="running", device=device, scope=__doc__, modes={})
reference = {}
previous = os.environ.pop("CYCLES_HOUDINI_DISABLE_MESH_SHARING", None)


def planes(frame):
    return {
        name: np.frombuffer(value["pixels"], np.float32)
        .reshape(frame["height"], frame["width"], value["channels"])
        .copy()
        for name, value in frame["planes"].items()
    }


try:
    for mode in ("independent", "shared"):
        if mode == "independent":
            os.environ["CYCLES_HOUDINI_DISABLE_MESH_SHARING"] = "1"
        else:
            os.environ.pop("CYCLES_HOUDINI_DISABLE_MESH_SHARING", None)
        f.camera.parm("tx").set(0)
        f.obj.parm("tx").set(0)
        f.styles("uniform")
        rows = []
        report["modes"][mode] = rows
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
            for label, change, offset in (
                ("Uniform", lambda: f.styles("uniform"), 0),
                ("Four styles", lambda: f.styles(), 0),
                ("Surface edit", lambda: f.styles(offset=0.03), 0.03),
                ("Camera", lambda: f.camera.parm("tx").set(0.05), 0.03),
                ("Object", lambda: f.obj.parm("tx").set(0.05), 0.03),
                ("Styles removed", lambda: f.styles("uniform"), 0),
            ):
                change()
                began = time.perf_counter()
                frame = session.wait(session.apply(f.update(), reuse_if_unchanged=True), timeout=240)
                elapsed = time.perf_counter() - began
                status = session.status()
                images = planes(frame)
                f.check(frame, offset)
                assert status["state"] == "complete" and not status["error"] and not status["source_diagnostics"], (
                    status
                )
                physical = 1 if mode == "shared" or f.mode == "uniform" else 4
                assert status["mesh_variant_resources"] == physical and status["object_resources"] == 12, status
                if mode == "independent":
                    reference[label] = images
                else:
                    assert reference[label].keys() == images.keys()
                    for name, value in images.items():
                        if name.startswith("Crypto") or name == "material_id":
                            assert np.array_equal(value.view(np.uint32), reference[label][name].view(np.uint32)), name
                        else:
                            np.testing.assert_allclose(value, reference[label][name], rtol=0, atol=2e-5, err_msg=name)
                diagnostics = session.diagnostics()
                if label in ("Camera", "Object"):
                    assert status["geometry_conversions"] == rows[-1]["status"]["geometry_conversions"], status
                    assert all(
                        not diagnostics["buffer_copy_requests"].get(name, {}).get("calls")
                        for name in ("tri_verts", "tri_vindex")
                    ), diagnostics
                rows.append(
                    dict(
                        label=label,
                        status=status,
                        seconds=elapsed,
                        prototype_cooks=f.prototype.cookCount(),
                        diagnostics=diagnostics,
                        image_hashes={
                            name: hashlib.sha256(value.tobytes()).hexdigest() for name, value in images.items()
                        },
                    )
                )
                path.write_text(json.dumps(report, indent=2))
        closed = session.status()
        assert closed["device_allocation_bytes"] == closed["owned_geometry_bytes"] == 0, closed
        report.setdefault("closed", {})[mode] = closed
    a = report["modes"]["independent"][1]["status"]
    b = report["modes"]["shared"][1]["status"]
    assert b["device_allocation_bytes"] < a["device_allocation_bytes"], (a, b)
    assert len({row["prototype_cooks"] for rows in report["modes"].values() for row in rows}) == 1
    report.update(
        result="passed",
        physical_meshes=dict(independent=a["mesh_variant_resources"], shared=b["mesh_variant_resources"]),
        device_bytes_saved=a["device_allocation_bytes"] - b["device_allocation_bytes"],
        device_bytes_reduction=(a["device_allocation_bytes"] - b["device_allocation_bytes"])
        / a["device_allocation_bytes"],
    )
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
finally:
    if previous is None:
        os.environ.pop("CYCLES_HOUDINI_DISABLE_MESH_SHARING", None)
    else:
        os.environ["CYCLES_HOUDINI_DISABLE_MESH_SHARING"] = previous
    path.write_text(json.dumps(report, indent=2))
assert report["result"] == "passed", report.get("error")
print(
    json.dumps(
        dict(
            result="passed",
            device_bytes_saved=report["device_bytes_saved"],
            device_bytes_reduction=report["device_bytes_reduction"],
        )
    )
)
