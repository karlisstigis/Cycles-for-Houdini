# SPDX-License-Identifier: Apache-2.0
"""Worker validation of large arrays on all cores refuses exactly like the serial checks.

A large mesh (positions, triangles, corner normals/UV/colours) and many curves
are rendered once. Then copies of the encoded packet with one invalid value
(start, middle or end of one large array) or two invalid arrays are sent to the
renderer, once with CYCLES_HOUDINI_SERIAL_CONVERSION=1 and once in parallel.
Every refusal message must be the expected one (the earlier check wins when two
arrays are invalid) and identical between the two schedules; after the
refusals the accepted scene, its generation and image are unchanged.
"""

from support.paths import artifacts_root
from cycles_houdini.device_selection import set_devices as set_rop_devices
import argparse, json, os, re, struct, time, traceback
import hou, numpy as np
from cycles_houdini import native_scene, registry
from cycles_houdini.ipr import queued

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
dest = artifacts_root() / "stage3/validation-parallel"
dest.mkdir(parents=True, exist_ok=True)
report = dict(result="running", device=device, cases={})
W, H = 64, 48
ROWS = 300  # 90,000 points, 178,802 triangles: above the 65,536-element serial limit

obj = hou.node("/obj").createNode("geo", "v5val_mesh", run_init_scripts=False)
grid = obj.createNode("grid")
grid.setParms(dict(rows=ROWS, cols=ROWS))
grid.parmTuple("size").set((3, 2))
attrs = obj.createNode("attribwrangle")
attrs.setInput(0, grid)
attrs.parm("class").set(3)  # vertices: corner arrays
attrs.parm("snippet").set("v@N = set(0, 1, 0); v@uv = set(@P.x, @P.z, 0); v@Cd = set(0.5, 0.4, 0.3);")
attrs.setDisplayFlag(True)
attrs.setRenderFlag(True)
hair = hou.node("/obj").createNode("geo", "v5val_curves", run_init_scripts=False)
line = hair.createNode("line")
line.setParms(dict(points=8, dist=0.3))
line.parmTuple("dir").set((0, 1, 0))
roots = hair.createNode("grid")
roots.setParms(dict(rows=100, cols=100))
roots.parmTuple("size").set((2, 2))
copies = hair.createNode("copytopoints::2.0")
copies.setInput(0, line)
copies.setInput(1, roots)
width = hair.createNode("attribwrangle")
width.setInput(0, copies)
width.parm("snippet").set("f@width = 0.005;")
width.setDisplayFlag(True)
width.setRenderFlag(True)
camera = hou.node("/obj").createNode("cam", "v5val_cam")
camera.setParms(dict(tz=5, resx=W, resy=H))
rop = hou.node("/out").createNode("cycles_ipr", "v5val")
set_rop_devices(rop, device)
rop.setParms(
    dict(
        source_mode="objects",
        vobject="v5val_*",
        camera=camera.path(),
        resx=W,
        resy=H,
        samples=4,
        ipr_samples=4,
        adaptive_sampling=0,
        ipr_adaptive_sampling=0,
        use_denoise=0,
        ipr_use_denoise=0,
        allowmotionblur=0,
        world_source="color",
    )
)

points = grid.geometry().intrinsicValue("pointcount")
triangles = 2 * (ROWS - 1) ** 2
corners = 3 * triangles
curve_points = 100 * 100 * 8
replace = dict(packet=None)
captured = []


class Hook:
    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, name):
        value = getattr(self.inner, name)
        if name != "encode_snapshot":
            return value

        def encode(desired, ack=None, *rest):
            if replace["packet"] is not None:
                packet, replace["packet"] = replace["packet"], None
                return packet
            captured.append(desired)
            return value(desired, ack, *rest) if ack is not None else value(desired)

        return encode


queued._native = Hook(queued._native)


def locate(packet):
    """Offsets of the arrays (after their u64 count), in wire order."""
    found, start = {}, 0
    for name, count, item in (
        ("positions", points, 12),
        ("triangles", triangles, 12),
        ("normals", corners, 12),
        ("uv", corners, 8),
        ("colors", corners, 12),
        ("curve_positions", curve_points, 12),
        ("curve_radii", curve_points, 4),
    ):
        at = packet.find(struct.pack("<Q", count), start)
        assert at >= 0, (name, count)
        found[name] = (at + 8, count, item)
        start = at + 8 + count * item
    return found


def corrupt(base, arrays, edits):
    data = bytearray(base)
    for name, where in edits:
        offset, count, item = arrays[name]
        index = dict(start=0, middle=count // 2, end=count - 1)[where]
        if name == "triangles":
            data[offset + index * item : offset + index * item + 4] = struct.pack("<i", points + 7)
        elif name == "curve_radii":
            data[offset + index * 4 : offset + index * 4 + 4] = struct.pack("<f", -1.0)
        else:
            data[offset + index * item : offset + index * item + 4] = struct.pack("<f", float("inf"))
    return bytes(data)


expected = {
    "positions": "Non-finite vertex: ",
    "normals": "Non-finite normal: ",
    "colors": "Non-finite mesh color: ",
    "uv": "Non-finite UV: ",
    "triangles": "Triangle index outside mesh: ",
    "curve_positions": "Non-finite point/curve position: ",
    "curve_radii": "Point/curve radius must be finite and nonnegative: ",
}
cases = [((name, where),) for name in expected for where in ("start", "middle", "end")]
cases += [
    (("uv", "end"), ("triangles", "start")),
    (("colors", "middle"), ("normals", "end")),
    (("triangles", "end"), ("positions", "middle")),
    (("curve_radii", "start"), ("curve_positions", "end")),
]


def first_expected(edits):
    order = list(expected)  # today's serial order of these checks
    return expected[min((e[0] for e in edits), key=order.index)]


def pixels(frame):
    plane = frame["planes"]["combined"]
    return np.frombuffer(plane["pixels"], np.float32).reshape(H, W, plane["channels"]).copy()


def run(serial):
    if serial:
        os.environ["CYCLES_HOUDINI_SERIAL_CONVERSION"] = "1"
    try:
        results = {}
        with registry.session_from_configuration(native_scene.configuration(rop), interactive=True) as session:
            hou.setFrame(1)
            image = pixels(session.wait(session.apply(native_scene.capture(rop), at_time=hou.time())))
            before = session.status()
            os.environ["CYCLES_HOUDINI_WIRE_LEGACY"] = "1"
            try:
                base = queued._native.inner.encode_snapshot(captured[-1])
            finally:
                os.environ.pop("CYCLES_HOUDINI_WIRE_LEGACY", None)
            arrays = locate(base)
            # One valid edit first (the first edit after the initial render is
            # not answered with a per-submission event in this harness).
            grid.parmTuple("size").set((3.0005, 2))
            session.wait(session.apply(native_scene.capture(rop), at_time=hou.time()))
            frame = 2
            for edits in cases:
                replace["packet"] = corrupt(base, arrays, edits)
                # A real edit, so that an update is encoded (and replaced by the corrupt packet).
                grid.parmTuple("size").set((3 + 0.001 * frame, 2))
                frame += 1
                with session._controller.condition:
                    seen = len(session._controller.events)
                    last = session._controller.events[-1] if session._controller.events else None
                session.apply(native_scene.capture(rop), at_time=hou.time())
                key = "+".join("%s@%s" % e for e in edits)
                deadline = time.monotonic() + 60
                event = None
                while event is None and time.monotonic() < deadline:
                    session.status()  # pumps the host capture into the controller
                    with session._controller.condition:
                        events = list(session._controller.events)
                    fresh = (
                        events[events.index(last) + 1 :]
                        if last in events
                        else events[-(len(events) - seen) :]
                        if len(events) > seen
                        else []
                    )
                    event = next((e for e in fresh if not e.get("accepted", True)), None)
                    if event is None and replace["packet"] is None and any(e.get("accepted") for e in fresh):
                        event = dict(error="accepted")
                    time.sleep(0.01)
                results[key] = event["error"] if event else "no event"
                assert replace["packet"] is None, "the corrupt packet was not sent"
            grid.parmTuple("size").set((3, 2))
            after_image = pixels(session.wait(session.apply(native_scene.capture(rop), at_time=hou.time())))
            after = session.status()
        return results, image, after_image, before, after
    finally:
        os.environ.pop("CYCLES_HOUDINI_SERIAL_CONVERSION", None)


try:
    serial, s_image, s_after, s_before, s_status = run(True)
    parallel, p_image, p_after, p_before, p_status = run(False)
    report["cases"] = dict(serial=serial, parallel=parallel)
    for edits in cases:
        key = "+".join("%s@%s" % e for e in edits)
        want = first_expected(edits)
        assert serial[key].startswith(want), (key, want, serial[key])
        assert parallel[key] == serial[key], (key, serial[key], parallel[key])
    assert s_image.tobytes() == p_image.tobytes() or device != "CPU", "accepted images differ"
    assert s_after.tobytes() == s_image.tobytes() or device != "CPU", "image changed after refusals"
    assert p_after.tobytes() == p_image.tobytes() or device != "CPU", "image changed after refusals"
    for name in ("geometry_resources", "object_resources"):
        assert s_before.get(name) == s_status.get(name) == p_status.get(name), (
            name,
            s_before.get(name),
            s_status.get(name),
            p_status.get(name),
        )
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    queued._native = queued._native.inner if isinstance(queued._native, Hook) else queued._native
    registry.close_all()
    (dest / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print("V5VALID", json.dumps(report, default=str)[:6000])
