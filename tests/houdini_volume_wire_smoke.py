# SPDX-License-Identifier: Apache-2.0
"""Protocol 88 volume transport renders exactly like the previous OpenVDB stream form.

A sparse VDB density with a vec3 colour field and a dense Houdini volume are
rendered twice in fresh sessions: once with the raw sparse form and once with
CYCLES_HOUDINI_VOLUME_WIRE_STREAM=1 (the stream form used before). On CPU the
images must be bit-identical; GPU volume renders are not bit-reproducible run to
run, so there they must agree within 1e-6. The encoded snapshot must be
byte-identical between
CYCLES_HOUDINI_SERIAL_CONVERSION=1 and the parallel encode.
"""

from support.paths import artifacts_root
from cycles_houdini.device_selection import set_devices as set_rop_devices
import argparse, hashlib, json, os, re, traceback
import hou, numpy as np
from cycles_houdini import native_scene, registry
from cycles_houdini.ipr import queued

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
dest = artifacts_root() / "stage2/volumes/wire-render"
dest.mkdir(parents=True, exist_ok=True)
report = dict(result="running", device=device)
label = re.sub(r"[^A-Za-z0-9]+", "_", device)
W, H = 96, 64

sparse = hou.node("/obj").createNode("geo", "wire_sparse", run_init_scripts=False)
vol = sparse.createNode("volume", "density")
vol.setParms(dict(name="density", divsize=0.04))
vol.parmTuple("size").set((2, 1.6, 1.2))
wrangle = sparse.createNode("volumewrangle", "noise")
wrangle.setInput(0, vol)
wrangle.parm("snippet").set("@density = max(0, noise(@P * 3) - 0.3) * 40;")
vdb = sparse.createNode("convertvdb", "to_vdb")
vdb.setInput(0, wrangle)
vdb.parm("conversion").set("vdb")
vdb.parm("vdbclass").set("fog")
colour = sparse.createNode("volume", "colour")
colour.setParms(dict(name="Cd", rank=1, divsize=0.08))
colour.parmTuple("size").set((2, 1.6, 1.2))
cw = sparse.createNode("volumewrangle", "colour_values")
cw.setInput(0, colour)
cw.parm("snippet").set("v@Cd = set(@P.x + 1, @P.y + 0.8, 0.3);")
cvdb = sparse.createNode("convertvdb", "colour_vdb")
cvdb.setInput(0, cw)
cvdb.parm("conversion").set("vdb")
merge = sparse.createNode("merge", "fields")
merge.setInput(0, vdb)
merge.setInput(1, cvdb)
merge.setDisplayFlag(True)
merge.setRenderFlag(True)
dense = hou.node("/obj").createNode("geo", "wire_dense", run_init_scripts=False)
dvol = dense.createNode("volume", "dense_density")
dvol.setParms(dict(name="density", initialval1=4, uniformsamples=0, divsx=20, divsy=12, divsz=10))
dvol.parmTuple("size").set((0.8, 0.6, 0.5))
dvol.parmTuple("t").set((0.9, -0.5, 0.3))
dvol.setDisplayFlag(True)
dvol.setRenderFlag(True)
camera = hou.node("/obj").createNode("cam", "camera_for_wire")
camera.setParms(dict(tz=5, resx=W, resy=H))
rop = hou.node("/out").createNode("cycles_ipr", "wire")
set_rop_devices(rop, device)
rop.setParms(
    dict(
        source_mode="objects",
        vobject="wire_*",
        camera=camera.path(),
        resx=W,
        resy=H,
        samples=16,
        ipr_samples=16,
        adaptive_sampling=0,
        ipr_adaptive_sampling=0,
        use_denoise=0,
        ipr_use_denoise=0,
        allowmotionblur=0,
        material_kind="principled_volume",
        world_source="color",
    )
)
rop.parmTuple("background").set((1, 1, 1))


desired_snapshots = []


class CaptureDesired:
    """Records the owned snapshot handed to the worker encoder (test only)."""

    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, name):
        value = getattr(self.inner, name)
        if name != "encode_snapshot":
            return value

        def encode(desired, *args):
            desired_snapshots.append(desired)
            return value(desired, *args)

        return encode


queued._native = CaptureDesired(queued._native)


def pixels(frame):
    plane = frame["planes"]["combined"]
    return np.frombuffer(plane["pixels"], np.float32).reshape(H, W, plane["channels"]).copy()


def render(stream):
    if stream:
        os.environ["CYCLES_HOUDINI_VOLUME_WIRE_STREAM"] = "1"
    else:
        os.environ.pop("CYCLES_HOUDINI_VOLUME_WIRE_STREAM", None)
    try:
        # A worker session: only the external renderer receives the encoded snapshot.
        with registry.session_from_configuration(native_scene.configuration(rop), interactive=True) as session:
            image = pixels(session.wait(session.apply(native_scene.capture(rop), at_time=hou.time())))
            status = session.status()
    finally:
        os.environ.pop("CYCLES_HOUDINI_VOLUME_WIRE_STREAM", None)
    assert status["state"] != "failed" and not status["source_diagnostics"], status
    assert status["geometry_resources"] == 2, status
    return image


try:
    kinds = sorted({p.type().name() for p in merge.geometry().prims()})
    assert kinds == ["VDB"], kinds
    raw = render(False)
    encode = queued._native.inner.encode_snapshot
    os.environ["CYCLES_HOUDINI_SERIAL_CONVERSION"] = "1"
    serial = encode(desired_snapshots[-1], None)
    os.environ.pop("CYCLES_HOUDINI_SERIAL_CONVERSION", None)
    parallel = encode(desired_snapshots[-1], None)
    report.update(
        payload_bytes=len(parallel),
        serial_sha256=hashlib.sha256(serial).hexdigest(),
        parallel_sha256=hashlib.sha256(parallel).hexdigest(),
    )
    assert serial == parallel, "Serial and parallel snapshot encodes differ"
    stream = render(True)
    report.update(
        raw_sha256=hashlib.sha256(raw.tobytes()).hexdigest(),
        stream_sha256=hashlib.sha256(stream.tobytes()).hexdigest(),
        mean=float(raw[:, :, :3].mean()),
        covered=float((raw[:, :, 0] < 0.9).mean()),
    )
    # Both volumes must be visible against the white background.
    assert np.isfinite(raw).all() and report["covered"] > 0.05, report
    difference = float(np.abs(raw - stream).max())
    report["raw_stream_max_difference"] = difference
    if device == "CPU":
        exact = raw.tobytes() == stream.tobytes()
    else:
        # GPU volume renders are not bit-reproducible run to run (measured on
        # OptiX: two raw-form renders differ by up to 1.2e-7). Require the two
        # forms to agree within that run-to-run noise, and record it.
        repeat = render(False)
        report["raw_repeat_max_difference"] = float(np.abs(raw - repeat).max())
        exact = difference <= 1e-6
    if not exact:
        np.savez(dest / f"failed-{label}.npz", raw=raw, stream=stream)
        raise AssertionError(("Raw and stream volume forms render differently", difference))
    # A volume above the transport limit is refused with an artist-readable
    # reason (the limit is lowered by a test hook instead of a huge volume).
    os.environ["CYCLES_HOUDINI_VOLUME_WIRE_TEST_LIMIT"] = "100000"
    try:
        with registry.session_from_configuration(native_scene.configuration(rop), interactive=True) as session:
            try:
                session.wait(session.apply(native_scene.capture(rop), at_time=hou.time()), timeout=60)
                refusal = json.dumps(session.status(), default=str)
            except Exception as error:
                refusal = repr(error) + " " + json.dumps(session.status(), default=str)
    finally:
        os.environ.pop("CYCLES_HOUDINI_VOLUME_WIRE_TEST_LIMIT", None)
    start = refusal.find("is too large to send")
    report["refusal"] = refusal[max(0, start - 60) : start + 300] if start >= 0 else refusal[-1500:]
    assert start >= 0, report["refusal"]
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    os.environ.pop("CYCLES_HOUDINI_SERIAL_CONVERSION", None)
    os.environ.pop("CYCLES_HOUDINI_VOLUME_WIRE_TEST_LIMIT", None)
    queued._native = queued._native.inner if isinstance(queued._native, CaptureDesired) else queued._native
    registry.close_all()
    (dest / f"report-{label}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
