# SPDX-License-Identifier: Apache-2.0
"""B1 transfer: packets written straight into the reused input mapping.

Proves, against CYCLES_HOUDINI_WIRE_LEGACY=1 (the previous path):
- what WirePacket.write_into put into the renderer's mapping equals the legacy
  bytes for meshes, curves, points, packed instances and volumes, first update
  and frame change (acknowledged base), serial and parallel windows;
- images are identical (CPU bitwise; GPU within the run-to-run noise);
- the mapping is overwritten with garbage right after every reply and the next
  update still renders correctly (the renderer keeps nothing of it);
- a renderer killed mid-update and a watchdog timeout close the mapping, report
  the same error as the legacy path and the next update recovers;
- a second write while the first is outstanding is refused;
- growth, shrink and idle-free of the mapping, old mapping closed first;
- the volume "too large" refusal and a malformed packet keep their messages.
"""

from support.paths import artifacts_root
from cycles_houdini.device_selection import set_devices as set_rop_devices
import argparse, hashlib, json, mmap, os, re, time, traceback
import hou, numpy as np
from cycles_houdini import native_scene, registry
from cycles_houdini.ipr import queued
from cycles_houdini.ipr.exchange_watchdog import ExchangeWatchdog

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
dest = artifacts_root() / "stage3/wire-gather"
dest.mkdir(parents=True, exist_ok=True)
label = re.sub(r"[^A-Za-z0-9]+", "_", device)
report = dict(result="running", device=device, checks=[])
W, H = 96, 64
LEGACY = "CYCLES_HOUDINI_WIRE_LEGACY"


def geo(name):
    return hou.node("/obj").createNode("geo", name, run_init_scripts=False)


def finish(node):
    node.setDisplayFlag(True)
    node.setRenderFlag(True)


# Surfaces: a deforming mesh with N/uv/Cd, deforming curves, loose points, packed instances.
mesh = geo("v5w_mesh")
grid = mesh.createNode("grid")
grid.setParms(dict(rows=160, cols=160))
grid.parmTuple("size").set((3, 2))
wave = mesh.createNode("attribwrangle")
wave.setInput(0, grid)
wave.parm("snippet").set(
    "@P.y += 0.1 * sin(@P.x * 3 + @Frame); v@N = normalize(set(0, 1, 0.2)); v@uv = set(@P.x, @P.z, 0);"
    "v@Cd = set(0.2 + 0.1 * @P.x, 0.5, 0.7);"
)
finish(wave)
curves = geo("v5w_curves")
line = curves.createNode("line")
line.setParms(dict(points=8, dist=0.4))
line.parmTuple("dir").set((0, 1, 0))
roots = curves.createNode("grid")
roots.setParms(dict(rows=30, cols=30))
roots.parmTuple("size").set((2, 2))
roots.parmTuple("t").set((0, -0.6, 0))
copies = curves.createNode("copytopoints::2.0")
copies.setInput(0, line)
copies.setInput(1, roots)
sway = curves.createNode("attribwrangle")
sway.setInput(0, copies)
sway.parm("snippet").set("@P.x += 0.03 * sin(@Frame + @ptnum * 0.1) * (@ptnum % 8); f@width = 0.01;")
finish(sway)
points = geo("v5w_points")
box = points.createNode("box")
scatter = points.createNode("scatter")
scatter.setInput(0, box)
scatter.parm("npts").set(4000)
drift = points.createNode("attribwrangle")
drift.setInput(0, scatter)
drift.parm("snippet").set("@P = @P * 0.6 + set(1.2, 0.3 * sin(@Frame), 0); f@pscale = 0.012;")
finish(drift)
packed = geo("v5w_packed")
sphere = packed.createNode("sphere")
sphere.setParms(dict(type=2, rows=6, cols=8))
sphere.parmTuple("rad").set((0.04, 0.04, 0.04))
spots = packed.createNode("grid")
spots.setParms(dict(rows=15, cols=15))
spots.parmTuple("size").set((1.4, 1.4))
spots.parmTuple("t").set((-1.3, 0.2, 0))
move = packed.createNode("attribwrangle")
move.setInput(0, spots)
move.parm("snippet").set("@P.y += 0.05 * sin(@Frame + @ptnum);")
pack = packed.createNode("copytopoints::2.0")
pack.setInput(0, sphere)
pack.setInput(1, move)
pack.parm("pack").set(1)
named = packed.createNode("attribwrangle")
named.setInput(0, pack)
named.parm("class").set(1)
named.parm("snippet").set('s@name = sprintf("piece%d", @primnum); v@Cd = set(@primnum % 7 / 7.0, 0.4, 0.6);')
finish(named)
# Volumes: sparse VDB density with a vec3 colour field, and a dense volume.
volume = geo("v5v_sparse")
vol = volume.createNode("volume")
vol.setParms(dict(name="density", divsize=0.05))
vol.parmTuple("size").set((2, 1.6, 1.2))
vw = volume.createNode("volumewrangle")
vw.setInput(0, vol)
vw.parm("snippet").set("@density = max(0, noise(@P * 3 + set(0, @Frame * 0.3, 0)) - 0.3) * 40;")
vdb = volume.createNode("convertvdb")
vdb.setInput(0, vw)
vdb.parm("conversion").set("vdb")
vdb.parm("vdbclass").set("fog")
colour = volume.createNode("volume")
colour.setParms(dict(name="Cd", rank=1, divsize=0.08))
colour.parmTuple("size").set((2, 1.6, 1.2))
cw = volume.createNode("volumewrangle")
cw.setInput(0, colour)
cw.parm("snippet").set("v@Cd = set(@P.x + 1, @P.y + 0.8, 0.3);")
cvdb = volume.createNode("convertvdb")
cvdb.setInput(0, cw)
cvdb.parm("conversion").set("vdb")
merge = volume.createNode("merge")
merge.setInput(0, vdb)
merge.setInput(1, cvdb)
finish(merge)
dense = geo("v5v_dense")
dvol = dense.createNode("volume")
dvol.setParms(dict(name="density", initialval1=4, uniformsamples=0, divsx=20, divsy=12, divsz=10))
dvol.parmTuple("size").set((0.8, 0.6, 0.5))
dvol.parmTuple("t").set((0.9, -0.5, 0.3))
finish(dvol)
camera = hou.node("/obj").createNode("cam", "v5w_camera")
camera.setParms(dict(tz=5, resx=W, resy=H))


def make_rop(name, pattern, material):
    rop = hou.node("/out").createNode("cycles_ipr", name)
    set_rop_devices(rop, device)
    rop.setParms(
        dict(
            source_mode="objects",
            vobject=pattern,
            camera=camera.path(),
            resx=W,
            resy=H,
            samples=8,
            ipr_samples=8,
            adaptive_sampling=0,
            ipr_adaptive_sampling=0,
            use_denoise=0,
            ipr_use_denoise=0,
            allowmotionblur=0,
            material_kind=material,
            world_source="color",
        )
    )
    rop.parmTuple("background").set((1, 1, 1))
    return rop


rops = dict(
    surfaces=make_rop("v5w_surfaces", "v5w_*", "principled"),
    volumes=make_rop("v5w_volumes", "v5v_*", "principled_volume"),
)

# ---- instrumentation (test only) ----
encodes = []  # (desired, ack) handed to the encoder, in order
mapped = []  # bytes the renderer read from the reused mapping, per packet
garbage = dict(enabled=True)
malformed_next = dict(on=False, payload=None)


class Recorder:
    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, name):
        value = getattr(self.inner, name)
        if name != "encode_snapshot":
            return value

        def encode(desired, ack=None, *rest):
            if malformed_next["on"]:
                malformed_next["on"] = False
                if malformed_next["payload"] is not None:
                    # A packet of another protocol version: the real packet with its version field changed.
                    real = encode_legacy(desired, ack)
                    return real[:4] + (88).to_bytes(4, "little") + real[8:]
                return b"CSHW" + b"\x00" * 60
            encodes.append((desired, ack))
            return value(desired, ack, *rest) if ack is not None else value(desired)

        return encode


queued._native = Recorder(queued._native)
original_write, original_release = queued._InputMapping.write, queued._InputMapping.release


def write(self, payload):
    name = original_write(self, payload)
    if hasattr(payload, "write_into"):
        mapped.append(bytes(self.view[: len(payload)]))
    self.last_size = len(payload)
    return name


def release(self):
    original_release(self)
    if garbage["enabled"] and self.view is not None:
        n = min(self.capacity, max(getattr(self, "last_size", 0), 1 << 20))
        self.view[:n] = b"\xa5" * n  # the renderer must not hold anything of the previous packet


queued._InputMapping.write, queued._InputMapping.release = write, release


def check(name, ok, **info):
    report["checks"].append(dict(name=name, ok=bool(ok), **info))
    assert ok, (name, info)


def pixels(frame):
    plane = frame["planes"]["combined"]
    return np.frombuffer(plane["pixels"], np.float32).reshape(H, W, plane["channels"]).copy()


def set_legacy(on):
    if on:
        os.environ[LEGACY] = "1"
    else:
        os.environ.pop(LEGACY, None)


def render_frames(rop, legacy, frames=(1, 2, 3)):
    set_legacy(legacy)
    images = []
    try:
        with registry.session_from_configuration(native_scene.configuration(rop), interactive=True) as session:
            for frame in frames:
                hou.setFrame(frame)
                images.append(pixels(session.wait(session.apply(native_scene.capture(rop), at_time=hou.time()))))
                status = session.status()
                assert status["state"] != "failed" and not status["source_diagnostics"], status
            backend = session._controller.backend
            check("legacy flag " + rop.name(), (backend.input is None) == legacy, legacy=legacy)
    finally:
        set_legacy(False)
    return images


def encode_legacy(desired, ack):
    set_legacy(True)
    try:
        data = (
            queued._native.inner.encode_snapshot(desired, ack)
            if ack is not None
            else queued._native.inner.encode_snapshot(desired)
        )
    finally:
        set_legacy(False)
    assert isinstance(data, bytes)
    return data


def write_mapped(desired, ack, serial):
    if serial:
        os.environ["CYCLES_HOUDINI_SERIAL_CONVERSION"] = "1"
    try:
        packet = (
            queued._native.inner.encode_snapshot(desired, ack)
            if ack is not None
            else queued._native.inner.encode_snapshot(desired)
        )
        target = mmap.mmap(-1, len(packet) + 4096)
        target[:] = b"\x5a" * len(target)
        packet.write_into(target)
        return bytes(target[: len(packet)]), bytes(target[len(packet) :])
    finally:
        os.environ.pop("CYCLES_HOUDINI_SERIAL_CONVERSION", None)


def gpu_close(a, b, rop):
    if device == "CPU":
        return a.tobytes() == b.tobytes(), 0.0
    return float(np.abs(a - b).max()) <= 1e-6, float(np.abs(a - b).max())


try:
    for kind, rop in rops.items():
        encodes.clear(), mapped.clear()
        new = render_frames(rop, False)
        new_encodes, new_mapped = list(encodes), list(mapped)
        check(
            kind + " packets mapped",
            len(new_mapped) >= 3 and len(new_mapped) == len(new_encodes),
            encodes=len(new_encodes),
            mapped=len(new_mapped),
        )
        encodes.clear(), mapped.clear()
        old = render_frames(rop, True)
        check(kind + " legacy maps nothing reused", not mapped, mapped=len(mapped))
        sizes = []
        for index, ((desired, ack), seen) in enumerate(zip(new_encodes, new_mapped)):
            legacy = encode_legacy(desired, ack)
            sizes.append(len(legacy))
            check(
                "%s packet %d: mapping == legacy bytes" % (kind, index),
                seen == legacy,
                bytes=len(legacy),
                ack=ack is not None,
                sha=hashlib.sha256(legacy).hexdigest()[:16],
            )
            for serial in (True, False):
                body, tail = write_mapped(desired, ack, serial)
                check("%s packet %d serial=%d: write_into == legacy" % (kind, index, serial), body == legacy)
                check(
                    "%s packet %d serial=%d: nothing written past the end" % (kind, index, serial),
                    tail == b"\x5a" * len(tail),
                )
        report[kind + "_packet_bytes"] = sizes
        for index, (a, b) in enumerate(zip(new, old, strict=True)):
            same, difference = gpu_close(a, b, rop)
            check(
                "%s image %d new == legacy" % (kind, index),
                same,
                max_difference=difference,
                sha=hashlib.sha256(a.tobytes()).hexdigest()[:16],
            )
            check("%s image %d covered" % (kind, index), float((a[:, :, 0] < 0.95).mean()) > 0.02)
    # References must really be exercised: a frame change sends an acknowledged base.
    check("frame changes carry an acknowledged base", any(ack is not None for _, ack in new_encodes))

    rop = rops["surfaces"]
    hou.setFrame(1)
    # A second write while the first is outstanding is refused.
    mapping = queued._InputMapping()
    mapping.write(b"x" * 1000)
    try:
        mapping.write(b"y" * 1000)
        refused = ""
    except RuntimeError as error:
        refused = str(error)
    check("second write while busy refused", refused == "Cycles renderer input is still in use", error=refused)
    mapping.close()
    # Growth, shrink, idle free; the old mapping is closed before the new one exists.
    garbage["enabled"] = False
    m = queued._InputMapping()
    closed = []
    original_close = queued._InputMapping.close

    def tracked_close(self):
        if self.view is not None:
            closed.append(self.name)
        original_close(self)

    queued._InputMapping.close = tracked_close
    original_create = queued._InputMapping._create

    def tracked_create(self, capacity):
        assert self.view is None, "a new mapping was created while the old one was open"
        original_create(self, capacity)

    queued._InputMapping._create = tracked_create

    def put(m, size):
        # As ExternalBackend.exchange: replace (after the renderer released it) before writing.
        if m.needs_replacement(size):
            m.close()
        m.write(b"p" * size)
        m.release()
        return m.name

    try:
        first = put(m, 1 << 20)
        grown = (put(m, 300 << 20), m.capacity)
        check(
            "growth replaces the mapping",
            grown[0] != first and first in closed and grown[1] >= (300 << 20) * 5 // 4,
            capacity=grown[1],
        )
        names = [put(m, 1 << 20) for i in range(queued._InputMapping.SHRINK_AFTER)]
        check(
            "shrink after small packets",
            names[-2] == grown[0] and names[-1] != grown[0] and m.capacity < (8 << 20),
            capacity=m.capacity,
        )
        check("not idle while recent", not m.idle())
        m.replied -= queued._InputMapping.IDLE + 0.1
        check("idle after IDLE seconds", m.idle())
        m.write(b"x" * 10)
        try:
            m.write(b"y" * (64 << 20))
            refused = ""
        except RuntimeError as error:
            refused = str(error)
        check("no growth without release", refused != "", error=refused)
    finally:
        queued._InputMapping.close, queued._InputMapping._create = original_close, original_create
        m.close()
        garbage["enabled"] = True

    def failure_case(name, legacy, break_it):
        """Break one update, then check the error and that the next update renders."""
        set_legacy(legacy)
        try:
            with registry.session_from_configuration(native_scene.configuration(rop), interactive=True) as session:
                hou.setFrame(1)
                session.wait(session.apply(native_scene.capture(rop), at_time=hou.time()))
                backend = session._controller.backend
                undo = break_it(backend)
                hou.setFrame(2)
                error = ""
                try:
                    session.wait(session.apply(native_scene.capture(rop), at_time=hou.time()), timeout=60)
                except Exception as failure:
                    error = str(failure)
                status = session.status()
                error = status.get("error") or error
                undo()
                if backend.input is not None:
                    check(
                        name + ": mapping closed after failure",
                        backend.input.view is None,
                        error=error,
                        alive=backend.process.poll() is None,
                    )
                hou.setFrame(3)
                image = pixels(session.wait(session.apply(native_scene.capture(rop), at_time=hou.time())))
                return re.sub(r"\d+|Log: .*|log: .*", "#", error), image
        finally:
            set_legacy(False)

    def kill_after_write(backend):
        def write_then_kill(self, payload):
            name = write(self, payload)
            backend.process.kill()
            backend.process.wait(timeout=10)
            return name

        def legacy_kill(command, payload=b"", desired=None, original=backend.exchange):
            if command == 2:
                backend.process.kill()
                backend.process.wait(timeout=10)
            return original(command, payload, desired)

        if backend.input is not None:
            queued._InputMapping.write = write_then_kill
            return lambda: setattr(queued._InputMapping, "write", write)
        backend.exchange = legacy_kill
        return lambda: None

    def short_watchdog(backend):
        original_arm = ExchangeWatchdog.arm

        def arm(self, timeout=60.0, *, ceiling=None):
            return original_arm(self, 0.0, ceiling=None)

        ExchangeWatchdog.arm = arm
        return lambda: setattr(ExchangeWatchdog, "arm", original_arm)

    reference = render_frames(rop, False, frames=(3,))[0]
    # Live session: the idle poll releases the mapping on both sides; the next update maps a new one.
    with registry.session_from_configuration(native_scene.configuration(rop), interactive=True) as session:
        hou.setFrame(1)
        session.wait(session.apply(native_scene.capture(rop), at_time=hou.time()))
        backend = session._controller.backend
        first_name = backend.input.name
        deadline = time.monotonic() + 20
        while backend.input.view is not None and time.monotonic() < deadline:
            time.sleep(0.1)
        check(
            "idle release on a live session",
            backend.input.view is None and backend.metrics.get("input_releases", 0) >= 1,
            releases=backend.metrics.get("input_releases"),
        )
        hou.setFrame(3)
        image = pixels(session.wait(session.apply(native_scene.capture(rop), at_time=hou.time())))
        check(
            "new mapping after idle release",
            backend.input.name not in ("", first_name) and gpu_close(image, reference, rop)[0],
        )
    for case, breaker in (("killed", kill_after_write), ("watchdog", short_watchdog)):
        new_error, new_image = failure_case(case, False, breaker)
        old_error, old_image = failure_case(case, True, breaker)
        check(case + ": same error as legacy", new_error == old_error and new_error, new=new_error, old=old_error)
        check(case + ": recovers", gpu_close(new_image, reference, rop)[0] and gpu_close(old_image, reference, rop)[0])

    # Two updates in quick succession: the second wins, same image as legacy.
    def quick(legacy):
        set_legacy(legacy)
        try:
            with registry.session_from_configuration(native_scene.configuration(rop), interactive=True) as session:
                hou.setFrame(1)
                session.apply(native_scene.capture(rop), at_time=hou.time())
                hou.setFrame(3)
                return pixels(session.wait(session.apply(native_scene.capture(rop), at_time=hou.time())))
        finally:
            set_legacy(False)

    check(
        "quick succession", gpu_close(quick(False), quick(True), rop)[0] and gpu_close(quick(False), reference, rop)[0]
    )

    # A malformed packet is refused with the same message on both paths.
    def malformed(legacy, version=False):
        malformed_next["payload"] = True if version else None
        set_legacy(legacy)
        try:
            with registry.session_from_configuration(native_scene.configuration(rop), interactive=True) as session:
                hou.setFrame(1)
                session.wait(session.apply(native_scene.capture(rop), at_time=hou.time()))
                malformed_next["on"] = True
                hou.setFrame(2)
                try:
                    session.wait(session.apply(native_scene.capture(rop), at_time=hou.time()), timeout=60)
                except Exception as error:
                    pass
                status = session.status()
                return re.sub(r"\d+|Log: .*|log: .*", "#", str(status.get("error") or status.get("message")))
        finally:
            malformed_next["on"] = False
            set_legacy(False)

    new_error, old_error = malformed(False), malformed(True)
    check("malformed packet: same refusal", new_error == old_error and new_error, new=new_error, old=old_error)
    new_error, old_error = malformed(False, True), malformed(True, True)
    check(
        "protocol 88 packet refused by the 89 worker",
        new_error == old_error and "Unsupported worker packet version" in new_error,
        new=new_error,
        old=old_error,
    )

    # The volume "too large" refusal keeps its message.
    def too_large(legacy):
        set_legacy(legacy)
        os.environ["CYCLES_HOUDINI_VOLUME_WIRE_TEST_LIMIT"] = "100000"
        try:
            with registry.session_from_configuration(
                native_scene.configuration(rops["volumes"]), interactive=True
            ) as session:
                hou.setFrame(1)
                try:
                    session.wait(session.apply(native_scene.capture(rops["volumes"]), at_time=hou.time()), timeout=60)
                except Exception as error:
                    pass
                text = json.dumps(session.status(), default=str)
                start = text.find("is too large to send")
                return text[max(0, start - 60) : start + 300] if start >= 0 else text[-500:]
        finally:
            os.environ.pop("CYCLES_HOUDINI_VOLUME_WIRE_TEST_LIMIT", None)
            set_legacy(False)

    new_error, old_error = too_large(False), too_large(True)
    check(
        "too-large volume: same refusal",
        "is too large to send" in new_error and new_error == old_error,
        new=new_error,
        old=old_error,
    )
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    set_legacy(False)
    queued._native = queued._native.inner if isinstance(queued._native, Recorder) else queued._native
    queued._InputMapping.write, queued._InputMapping.release = original_write, original_release
    registry.close_all()
    (dest / f"report-{label}.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    for c in report["checks"]:
        print("V5CHECK", json.dumps(c, default=str))
    print(
        json.dumps(
            dict(
                result=report["result"],
                failed=[c for c in report["checks"] if not c["ok"]],
                checks=len(report["checks"]),
                error=report.get("error", "")[-3000:],
            ),
            default=str,
        )
    )
