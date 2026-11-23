# SPDX-License-Identifier: Apache-2.0
"""Typed native frusta: owned numerical references, live repairs and five final frames."""

from support.paths import artifacts_root
import argparse
import gc
import json
from pathlib import Path
import threading
import time
import traceback
import hou
import numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import final_render, registry
from tapered_volume_fixture import Fixture
from tapered_volume_source import build, descriptors
import tapered_volume_reference as reference

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
p.add_argument("--report", type=Path)
args = p.parse_args()
path = args.report or artifacts_root() / "stage3/volumes/tapered/report.json"
path.parent.mkdir(parents=True, exist_ok=True)
report = dict(result="running", scope=__doc__, device=args.device, checks=[], phase="initializing", numerical=[])
stop = threading.Event()
errors = []
count = [0]
thread = None
lease = None
f = None


def save():
    tmp = path.with_name(path.name + ".writing")
    tmp.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def numeric(control, *, integer_density=False):
    # Actual SDK primitive/point maps and centres are frozen BEFORE conversion.
    authored = descriptors(control)
    owned = ch._native.preserve_volume(control.path(), hou.time())
    converted = ch._native.convert_preserved_volume(owned)
    observed = {field["name"]: field for field in converted["fields"]}
    comparisons = []
    for field in authored:
        assert reference.assert_centres(field) < 3e-6
        expected = reference.numerical(
            field, level=control.evalParm("level"), integer_density=integer_density and field["name"] == "density"
        )
        actual = observed[field["name"]]
        assert actual["active_voxels"] == expected["active_voxels"], (field, actual, expected)
        for key in ("active_sums", "voxel_zero", "origin", "voxel_size"):
            np.testing.assert_allclose(
                actual[key], expected[key], rtol=3e-6, atol=3e-6, err_msg=field["name"] + " " + key
            )
        comparisons.append(dict(name=field["name"], source=field, observed=actual, independent=expected))
    return owned, converted, comparisons


save()
try:
    hou.setFrame(1)
    f = Fixture(args.device)
    authored = f.materials()
    for label, edits in (
        ("positive anisotropic", {}),
        ("compressed positive", dict(taperx=0.5, tapery=1.8)),
        ("collapsed cone", dict(taperx=0, tapery=0)),
        ("signed hourglass", dict(taperx=-0.5, tapery=1.8)),
        ("reflected rotated", dict(mirror=1, angle=37)),
        ("native integer", dict(mode=1, taperx=2, tapery=1.5, mirror=0, angle=0)),
        ("constant zero border", dict(mode=0, border=0)),
        ("constant zero integer border", dict(mode=1, border=0)),
    ):
        report["phase"] = "Frozen numerical " + label
        save()
        f.set(**edits)
        held, converted, checks = numeric(f.control, integer_density=f.control.evalParm("mode") == 1)
        report["numerical"].append(dict(label=label, fields=checks, fingerprint=converted["fingerprint"]))
        held = None
        gc.collect()
        save()
    # Independent true cone volume is 8/3 in local coordinates. Double source
    # resolution must reduce the observed finite-grid support error, with no
    # invented density outside the actual collapsed cone.
    convergence = []
    for n in (13, 26):
        obj = hou.node("/obj").createNode("geo", "cone_convergence_" + str(n), run_init_scripts=False)
        source = build(obj, resolution=(n, n, n))
        source.setParms(dict(mode=1, taperx=0, tapery=0))
        held, converted, checks = numeric(source, integer_density=True)
        field = next(field for field in descriptors(source) if field["name"] == "density")
        actual = next(field for field in converted["fields"] if field["name"] == "density")
        determinant = abs(float(np.linalg.det(reference.matrix(field)[:3, :3])))
        observed = actual["active_sums"][0] / n**3 * determinant
        expected = 8 / 3 * determinant
        convergence.append(
            dict(
                source_resolution=n,
                observed_volume=observed,
                analytic_volume=expected,
                error=abs(observed - expected),
                fields=checks,
            )
        )
        held = None
        obj.destroy()
        gc.collect()
    assert convergence[1]["error"] < convergence[0]["error"] * 0.35, convergence
    report["collapsed_apex_convergence"] = convergence
    f.set(mode=0, taperx=2, tapery=1.5, mirror=0, angle=0, border=2)
    lease, frozen, checks = numeric(f.control)
    report["frozen_owner"] = dict(fingerprint=frozen["fingerprint"], fields=checks)

    def inspect_frozen():
        try:
            while not stop.is_set():
                actual = ch._native.convert_preserved_volume(lease)
                assert actual["fingerprint"] == frozen["fingerprint"], (
                    "Owned nonlinear source changed after a host recook"
                )
                count[0] += 1
                stop.wait(0.15)
        except BaseException:
            errors.append(traceback.format_exc())

    thread = threading.Thread(target=inspect_frozen, name="Frozen nonlinear field conversion")
    thread.start()
    entry = f.session()
    previous = None
    for label, action in f.steps():
        report["phase"] = label
        save()
        action()
        began = time.monotonic()
        # This SDK-only descriptor snapshot is also taken before native capture.
        source = f.source_reference()
        frame = f.update()
        status = entry.session.status()
        f.assert_status(status)
        measured = f.assert_pixels(frame)
        assert f.materials() == authored
        assert not errors, errors
        cooks = f.control.cookCount()
        if previous:
            assert status["renderer_pid"] == previous["status"]["renderer_pid"], status
            if f.navigation:
                assert cooks == previous["source_cooks"], (cooks, previous)
                assert all(
                    status[k] == previous["status"][k]
                    for k in ("dense_field_conversions", "geometry_conversions", "extractions")
                ), status
                assert status["source_diagnostics"] == previous["status"]["source_diagnostics"], status
        event = dict(
            label=label,
            status=status,
            source=source,
            source_cooks=cooks,
            elapsed_seconds=time.monotonic() - began,
            measured=measured,
        )
        report["checks"].append(event)
        previous = event
        save()
    session = entry.session
    registry.close_all()
    deadline = time.monotonic() + 20
    while session.status()["state"] != "closed" and time.monotonic() < deadline:
        time.sleep(0.01)
    report["closed"] = session.status()
    assert report["closed"]["state"] == "closed" and all(
        report["closed"][key] == 0
        for key in ("owned_geometry_bytes", "device_allocation_bytes", "retained_dense_voxel_bytes")
    ), report["closed"]
    report["phase"] = "Five ordered persistent final frames"
    save()
    f.set(mode=0, level=0.35, taperx=2, tapery=1.5, mirror=0, angle=0, shading=0)
    f.control.parm("mode").setExpression("(0,5,0,4,0)[max(0,min(4,int(hou.frame()+.5)-1))]", hou.exprLanguage.Python)
    f.rop.setParms(dict(persistent_data=1, picture=str(f.dest / "tapered.$F4.exr")))
    f.rop.render(frame_range=(1, 5, 1))
    summary = f.rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 5 and summary["created_sessions"] == 1 and not summary["session_alive"], (
        summary
    )
    report["batch"] = []
    for index in range(1, 6):
        hou.setFrame(index)
        image = oiio.ImageBuf(str(f.dest / f"tapered.{index:04d}.exr"))
        assert not image.has_error, image.geterror()
        issues = json.loads(image.spec().get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        assert {issue["code"] for issue in issues} == f.expected_codes(), issues
        assert all(
            issue.get("severity", "warning") == ("info" if issue["code"] == "volume.resampled" else "warning")
            for issue in issues
        ), issues
        channels = list(image.spec().channelnames)
        # EXR scanlines are top-down; session frames/native IPR coordinates are
        # bottom-up. This asymmetric fixture must compare the same orientation.
        pixels = np.ascontiguousarray(
            np.flipud(image.get_pixels()[:, :, [channels.index(c) for c in ("R", "G", "B")]]), dtype=np.float32
        )
        measured = f.assert_pixels(
            dict(width=96, height=64, planes={"combined": dict(channels=pixels.shape[-1], pixels=pixels.tobytes())})
        )
        report["batch"].append(dict(frame=index, diagnostics=issues, measured=measured))
    report["final"] = summary
    # The read lease must survive both source recooks and final node deletion.
    f.obj.destroy()
    assert ch._native.convert_preserved_volume(lease)["fingerprint"] == frozen["fingerprint"]
    assert count[0] > 0 and not errors, (count, errors)
    stop.set()
    thread.join(15)
    assert not thread.is_alive() and not errors, errors
    report["frozen_conversions"] = count[0]
    lease = None
    gc.collect()
    assert ch._native.geometry_lease_count() == 0
    report.update(result="passed", phase="complete")
    save()
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    save()
    raise
finally:
    stop.set()
    if thread:
        thread.join(15)
    lease = None
    registry.close_all()
    save()
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]), frames=len(report.get("batch", [])))))
