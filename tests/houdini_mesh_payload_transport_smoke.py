# SPDX-License-Identifier: Apache-2.0
"""Same-runtime dense transport control, exact render/data identity and live source repair."""

import argparse
import hashlib
import json
import time
import traceback
from pathlib import Path
import hou
import numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import _native, final_render
from stylesheet_storage_fixture import Fixture

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
f = Fixture(device)
report = dict(result="running", device=device, scope=__doc__, controls={}, checks=[])
encode_snapshot = _native.encode_snapshot


def plane(frame, name):
    p = frame["planes"][name]
    return np.frombuffer(p["pixels"], np.float32).reshape(frame["height"], frame["width"], p["channels"])


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


def memory(session):
    event = session._controller.control("memory")
    assert event.wait(15) and not event.error, event.error
    return session._controller.backend_memory


def transfer_profile(session):
    backend = session._controller.backend
    host = dict(backend.last_apply_profile)
    try:
        lines = Path(backend.log_path).read_text(errors="replace").split("\n")[:-1]
    except FileNotFoundError:
        lines = []
    records = [json.loads(line[len("CH_PROFILE ") :]) for line in lines if line.startswith("CH_PROFILE ")]
    return dict(host=host, worker=next((row for row in reversed(records) if row["request"] == host["request"]), None))


def crypto_names(metadata):
    result = {}
    for key, layer in metadata.items():
        if key.startswith("cryptomatte/") and key.endswith("/name"):
            result[layer] = json.loads(metadata[key[:-4] + "manifest"])
    return result


def material_matte(frame, metadata, name):
    bits = int(crypto_names(metadata)["CryptoMaterial"][name], 16)
    if bits & 0x7F800000 in (0, 0x7F800000):
        bits ^= 1 << 23
    mask = np.zeros((frame["height"], frame["width"]), np.float32)
    for layer in frame["planes"]:
        if layer.startswith("CryptoMaterial"):
            pixels = plane(frame, layer)
            for offset in (0, 2):
                mask += (pixels[:, :, offset].copy().view(np.uint32) == bits) * pixels[:, :, offset + 1]
    return mask


try:
    reference_frames = {}
    for mode in ("full", "references"):
        calls = []
        enabled = mode == "references"

        def encode(desired, acknowledged=None, *, _enabled=enabled):
            began = time.perf_counter()
            packet = encode_snapshot(desired, acknowledged, _enabled)
            calls.append(dict(bytes=len(packet), seconds=time.perf_counter() - began))
            return packet

        _native.encode_snapshot = encode
        rows = []
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
            for label, kind, offset in (
                ("uniform", "uniform", 0),
                ("split", "colors", 0),
                ("edit-1", "colors", 0.03),
                ("edit-2", "colors", 0.06),
                ("removed", "uniform", 0),
            ):
                f.styles(kind, offset)
                start = len(calls)
                began = time.perf_counter()
                update = f.update()
                capture_seconds = time.perf_counter() - began
                frame = session.wait(session.apply(update, reuse_if_unchanged=True), timeout=240)
                elapsed = time.perf_counter() - began
                status = completed_status(session, frame)
                assert status["state"] == "complete" and not status["error"] and not status["source_diagnostics"], (
                    status
                )
                assert status["object_resources"] == 12 and status["mesh_variant_resources"] == 1, status
                assert f.prototype.cookCount() == 1
                f.check(frame, offset)
                actual = calls[start:]
                assert actual, label
                metadata = session.output_metadata()
                names = crypto_names(metadata)
                assert f.material.path() in names["CryptoMaterial"], names
                images = {name: plane(frame, name).copy() for name in frame["planes"]}
                if mode == "full":
                    reference_frames[label] = images
                else:
                    assert images.keys() == reference_frames[label].keys()
                    for name, pixels in images.items():
                        before = reference_frames[label][name]
                        if name.startswith("Crypto") or name == "material_id":
                            assert np.array_equal(pixels.view(np.uint32), before.view(np.uint32)), name
                        else:
                            np.testing.assert_allclose(pixels, before, atol=2e-5, rtol=0, err_msg=name)
                    if label != "uniform":
                        assert actual[-1]["bytes"] < 200000, actual
                rows.append(
                    dict(
                        label=label,
                        status=status,
                        complete_render_seconds=elapsed,
                        capture_seconds=capture_seconds,
                        actual_encodes=actual,
                        prototype_cooks=f.prototype.cookCount(),
                        renderer_memory=memory(session),
                        diagnostics=session.diagnostics(),
                        transfer_profile=transfer_profile(session),
                        image_hashes={
                            name: hashlib.sha256(pixels.tobytes()).hexdigest() for name, pixels in images.items()
                        },
                        crypto=names,
                    )
                )
                report["controls"][mode] = rows
                (f.dest / "transport-report.json").write_text(json.dumps(report, indent=2))
        assert session.status()["device_allocation_bytes"] == session.status()["owned_geometry_bytes"] == 0
    _native.encode_snapshot = encode_snapshot
    full = report["controls"]["full"]
    compact = report["controls"]["references"]
    for before, after in zip(full, compact):
        if after["label"] in ("split", "edit-1", "edit-2"):
            assert before["actual_encodes"][-1]["bytes"] > 50_000_000, before
            assert before["actual_encodes"][-1]["bytes"] > after["actual_encodes"][-1]["bytes"] * 1000, after
    # Small native edits cover changed payloads, nonuniform face bindings,
    # source warnings/deletion/repair, shader identity and retained references.
    g = Fixture(device, heavy=False)
    with ch.Session(
        device=device,
        width=g.width,
        height=g.height,
        samples=1,
        threads=2,
        execution="worker",
        use_texture_cache=False,
        collect_diagnostics=True,
    ) as session:

        def render(label, warning=False, extra=()):
            frame = session.wait(session.apply(g.update(), reuse_if_unchanged=True))
            status = completed_status(session, frame)
            assert (
                status["state"] == "complete" and not status["error"] and bool(status["source_diagnostics"]) == warning
            ), status
            colors = g.check(frame, g.offset)
            values = np.asarray(colors)
            for color in extra:
                assert np.max(np.abs(values - np.asarray(color)), axis=1).min() < 3e-4, (label, color, values)
            if report["checks"]:
                assert status["renderer_pid"] == report["checks"][0]["status"]["renderer_pid"]
            report["checks"].append(
                dict(label=label, status=status, colors=colors, crypto=crypto_names(session.output_metadata()))
            )
            return frame, status

        render("Uniform")
        g.styles()
        render("Four styles")
        g.styles(offset=0.04)
        render("Surface edit")
        g.styles(offset=0.04, bad=True)
        render("Unsupported export retained locally", True)
        g.styles(offset=0.04)
        render("Export repair")
        g.styles("uniform")
        render("Styles removed")
        g.styles()
        render("Styles restored")
        g.prototype.parmTuple("t").set((0, 0, 0.1))
        frame, _ = render("Real prototype positions change")
        depth = plane(frame, "depth")
        mask = (depth[:, :, 0] > 0) & (depth[:, :, 0] < 100)
        np.testing.assert_allclose(plane(frame, "position")[:, :, 2][mask], 0.1, atol=2e-5)
        g.prototype.parmTuple("t").set((0, 0, 0))
        render("Position repair")
        g.camera.parm("tx").set(0.05)
        render("Camera continues")
        g.obj.parm("tx").set(0.05)
        render("Object continues")
        g.styles("uniform")
        rule = dict(
            target=dict(group="0", subTarget=dict(group="0")),
            overrides=dict(materialParameters=dict(tint=[0.9, 0.9, 0.2])),
        )
        g.obj.parm("shop_materialstylesheet").set(json.dumps(dict(styles=[rule])))
        render("Independent internal face assignment", extra=[(0.9, 0.9, 0.2)])
        g.styles()
        render("Face assignment repair")
        other = ch.create_material(name="storage_other", shader="emission")
        other.node("surface_shader").setParms(dict(strength=1))
        other.node("surface_shader").parmTuple("color").set((0.8, 0.1, 0.2))
        other.parm("cycles_pass_index").set(9)
        path = other.path()
        g.styles("uniform")
        g.obj.parm("shop_materialstylesheet").set(
            json.dumps(dict(styles=[dict(target=dict(group="0"), overrides=dict(material=dict(name=path)))]))
        )
        frame, _ = render("Different material identity", extra=[(0.8, 0.1, 0.2)])
        assigned = {name for name in report["checks"][-1]["crypto"]["CryptoMaterial"] if name.startswith("/mat/")}
        assert assigned == {g.material.path(), path}, assigned
        beauty = plane(frame, "combined")
        ids = plane(frame, "material_id")[:, :, 0]
        for color, index in (((0.8, 0.1, 0.2), 9), (g.colors[0], 3)):
            mask = np.all(np.isclose(beauty[:, :, :3], color, atol=3e-4, rtol=0), axis=2)
            assert mask.sum() > 20
            np.testing.assert_array_equal(ids[mask], np.full(mask.sum(), index, dtype=np.float32))
            np.testing.assert_allclose(
                material_matte(frame, session.output_metadata(), path if index == 9 else g.material.path())[mask],
                1,
                atol=1e-6,
                rtol=0,
            )
        other.destroy()
        render("Missing material retains geometry and identity", True, [(1, 0, 1)])
        assert path in report["checks"][-1]["crypto"]["CryptoMaterial"]
        other = ch.create_material(name=path.rsplit("/", 1)[-1], shader="emission")
        other.node("surface_shader").parmTuple("color").set((0.8, 0.1, 0.2))
        other.node("surface_shader").parm("strength").set(1)
        other.parm("cycles_pass_index").set(9)
        render("Material recreation repairs live binding", extra=[(0.8, 0.1, 0.2)])
        g.styles()
        render("Ordinary styles after repair")
    assert session.status()["device_allocation_bytes"] == session.status()["owned_geometry_bytes"] == 0
    values = [g.style_text(offset=0.02 * number, bad=number == 3) for number in range(1, 6)]
    g.obj.parm("shop_materialstylesheet").setExpression(
        repr(values) + "[max(0,min(4,int(hou.frame())-1))]", hou.exprLanguage.Python
    )
    g.rop.setParms(dict(picture=str(g.dest / "transport.$F4.exr"), persistent_data=1))
    g.rop.render(frame_range=(1, 5, 1))
    summary = g.rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 5 and summary["created_sessions"] == 1 and not summary["session_alive"], (
        summary
    )
    for number in range(1, 6):
        image = oiio.ImageInput.open(str(g.dest / f"transport.{number:04}.exr"))
        assert image
        spec = image.spec()
        issues = json.loads(spec.get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        assert bool(issues) == (number == 3), (number, issues)
        pixels = np.asarray(image.read_image(format=oiio.FLOAT))
        image.close()
        channels = [
            list(spec.channelnames).index(("Combined." if "Combined.R" in spec.channelnames else "") + c) for c in "RGB"
        ]
        colors = np.unique(np.round(pixels[:, :, channels].reshape(-1, 3), 4), axis=0)
        for color in g.colors:
            assert np.max(np.abs(colors - (np.asarray(color) + (0.02 * number, 0, 0))), axis=1).min() < 3e-4, (
                number,
                colors,
            )
    report.update(result="passed", batch_frames=5, batch=summary)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
finally:
    _native.encode_snapshot = encode_snapshot
    (f.dest / "transport-report.json").write_text(json.dumps(report, indent=2))
assert report["result"] == "passed", report.get("error")
print(
    json.dumps(
        dict(
            result=report["result"],
            checks=len(report["checks"]),
            paired_states=sum(map(len, report["controls"].values())),
            batch=report.get("batch_frames"),
        )
    )
)
