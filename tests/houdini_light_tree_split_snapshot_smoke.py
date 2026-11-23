# SPDX-License-Identifier: Apache-2.0
"""Paired persistent workers consume identical owned source packets and edit history."""

from support.paths import artifacts_root
import ctypes
import hashlib
import json
import os
from pathlib import Path
import time
import traceback
import numpy as np
from cycles_houdini import _native
from cycles_houdini.queued import ExternalBackend

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
os.environ["CYCLES_HOUDINI_LIGHT_TREE_ROWS"] = "129"
os.environ["CYCLES_HOUDINI_LIGHT_TREE_COLS"] = "129"
# Reuse only the existing native fixture setup, not its render/capture loop.
fixture = root / "tests/houdini_light_tree_cache_smoke.py"
parts = fixture.read_text().split("\noriginal_disable = ", 1)
assert len(parts) == 2
setup = parts[0]
assert "def new_session(device)" in setup
exec(compile(setup, str(fixture), "exec"), globals())
device = selection or "CPU"
dest = artifacts_root() / "stage3/light-tree-split"
dest.mkdir(parents=True, exist_ok=True)
path = dest / "paired-report.json"
cpu = device == "CPU"
modes = ("0", "1") if cpu else ("0", "1", "0")
report = dict(
    result="running",
    device=device,
    scope="Identical captured packet and update history; persistent workers, fixed CPU affinity; exact CPU pixels and GPU roundoff controls; no latency claim",
    modes=modes,
    checks=[],
    closed=[],
)
state = dict(
    source=shape, surface=material(), visible=255, subdiv=dict(type="none"), included=True, tree=True, secondary=False
)


def scene():
    geometry = [ch.geometry_request(receiver, receiver_front, material="receiver", material_mode="uniform")]
    if state["included"]:
        request = ch.geometry_request(
            emitter, state["source"], material="emitter", material_mode="uniform", subdivision=state["subdiv"]
        )
        request["visibility"] = state["visible"]
        geometry.append(request)
    if state["secondary"]:
        geometry.append(ch.geometry_request(secondary, secondary_grid, material="secondary", material_mode="uniform"))
    return dict(
        geometry=geometry,
        materials=[
            state["surface"],
            dict(key="receiver", kind="diffuse", color=(0.8, 0.8, 0.8)),
            dict(key="secondary", kind="emission", color=(0.1, 0.2, 1.0), strength=3.0),
        ],
        camera_path=camera.path(),
        background=(0, 0, 0),
        rendering=dict(samples=samples, adaptive_sampling=False, light_tree=state["tree"], seed=7, animate_seed=False),
    )


def pin_pair(backends):
    from ctypes import wintypes

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.GetProcessAffinityMask.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.POINTER(ctypes.c_size_t),
    ]
    api.GetProcessAffinityMask.restype = wintypes.BOOL
    api.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
    api.SetProcessAffinityMask.restype = wintypes.BOOL
    common = None
    for backend in backends:
        process, system = ctypes.c_size_t(), ctypes.c_size_t()
        assert api.GetProcessAffinityMask(int(backend.process._handle), ctypes.byref(process), ctypes.byref(system)), (
            ctypes.get_last_error()
        )
        common = process.value if common is None else common & process.value
    assert common
    mask = common & -common
    for backend in backends:
        assert api.SetProcessAffinityMask(int(backend.process._handle), mask), ctypes.get_last_error()
    return mask


def finished(backend, reply):
    image = reply.get("frame")
    deadline = time.monotonic() + 120
    while True:
        status = reply["status"]
        assert status["state"] != "failed" and not status["error"] and not status["source_diagnostics"], status
        if reply.get("frame") is not None:
            image = reply["frame"]
        info = _native.frame_info(image) if image is not None else None
        if status["state"] == "complete" and info and info["complete"] and info["generation"] == status["generation"]:
            return _native.frame(image), status
        assert time.monotonic() < deadline, status
        time.sleep(0.005)
        reply = backend.poll()


steps = [
    ("Initial native emitter", lambda: None),
    ("Emitter transform", lambda: emitter.parm("tx").set(-0.5)),
    ("Camera only", lambda: camera.parm("tx").set(0.08)),
    ("Wavy shape", lambda: shape.parm("amplitude").set(0.15)),
    ("Changed connectivity", lambda: grid.parm("rows").set(base_rows + 2)),
    ("Emission edit", lambda: state.update(surface=material((0.8, 0.2, 0.1)))),
    ("Emission repair", lambda: state.update(surface=material())),
    ("Packed instances", lambda: state.update(source=copies)),
    ("Fifteen placements", lambda: targets.setParms(dict(rows=3, cols=5))),
    ("Negative scale", lambda: emitter.parm("sx").set(-1)),
    ("Nonuniform scale", lambda: emitter.parmTuple("s").set((-1.1, 0.7, 1.2))),
    ("Second emitter", lambda: state.update(secondary=True)),
    ("Both emitter poses", lambda: secondary.setParms(dict(tx=-0.2, ty=0.75, rz=-17))),
    ("First emitter topology", lambda: grid.parm("rows").set(base_rows + 5)),
    ("Second emitter topology", lambda: secondary_grid.parm("rows").set(6)),
    ("Remove second emitter", lambda: state.update(secondary=False)),
]
for frame in range(1, 6):
    steps.append(("Animated packed frame " + str(frame), lambda frame=frame: hou.setFrame(frame)))
steps += [
    (
        "Fixed subdivision",
        lambda: state.update(subdiv=dict(type="linear", adaptive=False, max_level=1, space="object")),
    ),
    ("True displacement", lambda: state.update(surface=material(height=0.02))),
    ("Displacement edit", lambda: state.update(surface=material(height=0.05))),
    ("Visibility off", lambda: state.update(visible=0)),
    ("Visibility repair", lambda: state.update(visible=255)),
    ("Emission off", lambda: state.update(surface=material(kind="diffuse"))),
    ("Emission on", lambda: state.update(surface=material())),
    ("Light tree off", lambda: state.update(tree=False)),
    ("Light tree on", lambda: state.update(tree=True)),
    ("Emitter removed", lambda: state.update(included=False)),
    ("Emitter recreated", lambda: state.update(included=True)),
]

backends = []
capture = None
ack = None
desired = None
keys = (
    "CYCLES_HOUDINI_DISABLE_EMPTY_LIGHT_TREE_SPLIT_SKIP",
    "CYCLES_HOUDINI_PROFILE_LIGHT_TREE",
    "CYCLES_HOUDINI_PROFILE_LIGHT_TREE_DETAIL",
)
saved = {key: os.environ.get(key) for key in keys}
try:
    capture = _native.create(
        device,
        width,
        height,
        samples,
        False,
        True,
        False,
        False,
        None,
        True,
        1,
        ["combined", "depth", "normal", "position"],
    )
    os.environ["CYCLES_HOUDINI_PROFILE_LIGHT_TREE"] = "1"
    os.environ["CYCLES_HOUDINI_PROFILE_LIGHT_TREE_DETAIL"] = "0"
    for label, edit in steps:
        edit()
        desired = _native.apply(capture, scene(), hou.time(), False)
        if not backends:
            for disabled in modes:
                os.environ["CYCLES_HOUDINI_DISABLE_EMPTY_LIGHT_TREE_SPLIT_SKIP"] = disabled
                backends.append(ExternalBackend(capture, desired))
            report["affinity_mask"] = pin_pair(backends)
        packet = _native.encode_snapshot(desired, ack)
        packet_hash = hashlib.sha256(packet).hexdigest()
        images = []
        statuses = []
        for backend in backends:
            reply = backend.check(backend.exchange(2, packet, desired))
            backend.last_snapshot = desired
            image, status = finished(backend, reply)
            images.append(image)
            statuses.append(status)
        differences = {}
        repeat_differences = {}
        hashes = []
        for image in images:
            hashes.append(
                {
                    name: hashlib.sha256(image["planes"][name]["pixels"]).hexdigest()
                    for name in ("combined", "depth", "normal", "position")
                }
            )
        for name in ("combined", "depth", "normal", "position"):
            a = np.frombuffer(images[0]["planes"][name]["pixels"], np.float32)
            b = np.frombuffer(images[1]["planes"][name]["pixels"], np.float32)
            assert np.isfinite(a).all() and np.isfinite(b).all(), (label, name)
            differences[name] = float(np.max(np.abs(a - b)))
            if not cpu:
                repeated = np.frombuffer(images[2]["planes"][name]["pixels"], np.float32)
                repeat_differences[name] = float(np.max(np.abs(a - repeated)))
                np.testing.assert_allclose(a, b, atol=2e-6, rtol=2e-6, err_msg=label + " optimized/control " + name)
                np.testing.assert_allclose(
                    a, repeated, atol=2e-6, rtol=2e-6, err_msg=label + " same-policy repeat " + name
                )
        row = dict(
            label=label,
            packet_sha256=packet_hash,
            hashes=hashes,
            differences=differences,
            same_policy_differences=repeat_differences,
            status=statuses,
            bitwise_equal=hashes[0] == hashes[1],
            same_policy_bitwise_equal=hashes[0] == hashes[-1] if not cpu else None,
        )
        report["checks"].append(row)
        path.write_text(json.dumps(report, indent=2))
        if cpu:
            assert row["bitwise_equal"], (label, differences)
        assert _native.encode_snapshot(desired, ack) == packet, "Renderer changed the captured owner"
        ack = desired
    for index, backend in enumerate(backends):
        # Stderr is drained and written independently of the renderer. Closing
        # the backend below still exercises the same live, persistent workers.
        deadline = time.monotonic() + 2.0
        flags = []
        prefix = "CH_LIGHT_TREE_BUILD_PHASES "
        while not flags:
            try:
                lines = Path(backend.log_path).read_text(errors="replace").split("\n")[:-1]
                flags = [
                    json.loads(line[len(prefix) :])["duplicate_split_skip"] for line in lines if line.startswith(prefix)
                ]
            except FileNotFoundError:
                pass
            if flags or time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        assert flags and all(value == (modes[index] == "0") for value in flags), flags
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
finally:
    for backend in backends:
        try:
            closed = backend.close()
            report["closed"].append(closed)
            assert closed["owned_geometry_bytes"] == closed["device_allocation_bytes"] == 0, closed
        except BaseException:
            report.update(result="failed", cleanup_error=traceback.format_exc())
    backends.clear()
    ack = desired = None
    if capture is not None:
        _native.close(capture)
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    path.write_text(json.dumps(report, indent=2))
assert report["result"] == "passed", report.get("error", report.get("cleanup_error"))
print(json.dumps(dict(result="passed", states=len(report["checks"]), same_captured_packets=True)))
