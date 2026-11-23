# SPDX-License-Identifier: Apache-2.0
"""Bounded owned-process IPR soak; no artist scene or preference writes.

Run through run-prototype-test.ps1 with CPU or an explicit device ID. The large
profile adds dense geometry, packed instances, file/COP raster replacement and
analytic settled-frame checks. This is a bounded retention/lifecycle workload,
not indefinite or out-of-memory certification. Qt runs in isolated hython.
"""

import argparse
import gc
import json
import os
from pathlib import Path
import time
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import hou
import numpy as np
import OpenImageIO as OIIO
from PySide6 import QtCore, QtWidgets
import cycles_houdini as ch
from cycles_houdini import device_selection, registry
from cycles_houdini.panel import RenderPanel
from session_soak_fixture import (
    create_large_scene,
    cuda_board,
    gpu_usage,
    retention_bounds,
    retention_window,
    selected_adapter,
)

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
parser.add_argument("--seconds", type=float, default=180.0)
parser.add_argument("--profile", choices=("small", "large"), default="small")
parser.add_argument("--dwell-seconds", type=float, default=None)
parser.add_argument("--resolution", type=int, nargs=2, metavar=("WIDTH", "HEIGHT"))
parser.add_argument(
    "--prolonged", action="store_true", help="Require fully instrumented large NVIDIA retention evidence"
)
parser.add_argument("--report", required=True)
args = parser.parse_args()
assert 60 <= args.seconds <= 900, "Use a bounded 60–900 second soak"
large = args.profile == "large"
assert not args.resolution or (large and 0 < args.resolution[0] <= 3840 and 0 < args.resolution[1] <= 2160)
assert not args.prolonged or (large and args.device.startswith("CUDA_")), (
    "Prolonged telemetry requires one CUDA/OptiX board"
)
resolution = tuple(args.resolution or (480, 270)) if large else (160, 100)
dwell_seconds = args.dwell_seconds if args.dwell_seconds is not None else (120.0 if large else 12.0)
assert 1 <= dwell_seconds <= 180
path = Path(args.report).resolve()
path.parent.mkdir(parents=True, exist_ok=True)
app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
started = time.monotonic()
report = dict(
    result="running",
    device=args.device,
    profile=args.profile,
    requested_seconds=args.seconds,
    dwell_seconds=dwell_seconds,
    prolonged=args.prolonged,
    host_pid=os.getpid(),
    samples=[],
    correctness=[],
    lifecycles=[],
    edits=[],
    budgets_mib=dict(host=128, worker=64, device=32),
    scope=__doc__,
    capabilities=ch.capabilities(),
)
expected_host = Path(os.environ["CYCLES_HOUDINI_HOST_ROOT"]).resolve()
definition = hou.nodeType(hou.ropNodeTypeCategory(), "cycles_ipr").definition()
identity = dict(
    houdini=hou.applicationVersionString(),
    python=ch.__file__,
    native=ch._native.__file__,
    hda=definition.libraryFilePath(),
    resources=hou.getenv("CYCLES_HOUDINI_RESOURCES"),
    host=str(expected_host),
)
for key in ("python", "native", "hda"):
    assert Path(identity[key]).resolve().is_relative_to(expected_host), identity
assert Path(identity["resources"]).resolve() == expected_host / "resources", identity
report["runtime_identity"] = identity
panel = None
board = None


class SoakTelemetryError(RuntimeError):
    pass


class SoakCoverageError(RuntimeError):
    pass


class SoakRetentionError(RuntimeError):
    pass


def bound(metric, value, limit, detail):
    if value > limit:
        raise SoakRetentionError(f"{metric} observed {value} bytes above {limit}: {detail}")


def verify_render_device(status):
    actual = status.get("render_device_ids")
    if actual != [args.device]:
        raise SoakTelemetryError(f"Expected selected renderer {args.device}, observed {actual}")


def pump(seconds=0.0):
    end = time.monotonic() + seconds
    while True:
        app.processEvents()
        registry.poll()
        if panel is not None:
            panel.poll()
        if time.monotonic() >= end:
            return
        time.sleep(0.01)


def until(label, predicate, timeout=None):
    timeout = timeout if timeout is not None else (120 if large else 45)
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        pump()
        if panel and panel.entry:
            status = panel.entry.session.status()
            assert status["state"] != "failed" and not panel.entry.error, (label, status, panel.entry.error)
        if predicate():
            return
        time.sleep(0.01)
    raise TimeoutError(
        (label, panel.info.text() if panel else "", panel.entry.session.status() if panel and panel.entry else None)
    )


def displayed():
    if panel is None or panel.entry is None:
        return False
    s = panel.entry.session.status()
    f = panel.entry.session.frame_info()
    return bool(
        f
        and f.get("current_scene")
        and not s.get("pending_update")
        and not s.get("capture_pending")
        and panel._live_generation == s["generation"]
        and panel._live_frame_metadata.get("render_revision") == s["render_revision"]
        and (
            not args.resolution
            or tuple(panel._live_frame_metadata.get(key) for key in ("rendered_width", "rendered_height")) == resolution
        )
        and not panel._settings_dirty
    )


def render(label, expected=None):
    panel.render()
    until(label, displayed)
    s = panel.entry.session.status()
    assert s["state"] == "rendering", (label, s)
    verify_render_device(s)
    if expected is not None:
        frame = panel.entry.session.frame()
        plane = frame["planes"]["combined"]
        pixels = np.frombuffer(plane["pixels"], np.float32).reshape(frame["height"], frame["width"], plane["channels"])
        assert np.isfinite(pixels).all(), label
        # BufferDisplay::map_texture_buffer in src/core/session.cpp carries
        # progressive, non-denoised combined pixels in IEEE float16. Compare
        # to the independently quantized reference, preserving the strict
        # numerical tolerance instead of mistaking display precision for bias.
        display_expected = np.asarray(expected, np.float16).astype(np.float32)
        error = float(np.abs(pixels[8:-8, 8:-8, :3] - display_expected).max())
        assert error <= 2e-5, (label, error, expected)
        report["correctness"].append(
            dict(
                label=label,
                maximum_absolute_error=error,
                tolerance=2e-5,
                expected=list(expected),
                display_expected=display_expected.tolist(),
                reference_encoding="IEEE float16 progressive BufferDisplay",
                generation=s["generation"],
            )
        )
    report["edits"].append(
        dict(label=label, time=time.monotonic() - started, pid=s["renderer_pid"], generation=s["generation"])
    )


def snapshot(label, dwell=None):
    session = panel.entry.session
    previous = session._controller.backend_memory
    fresh = [None]

    def received():
        fresh[0] = session.memory_snapshot()
        return fresh[0] is not None and fresh[0] is not previous

    until("Fresh worker memory", received)
    s = session.status()
    worker = fresh[0]
    process = session._controller.backend.process
    assert worker["pid"] == s["renderer_pid"] == process.pid != os.getpid() and process.poll() is None
    verify_render_device(s)
    assert s["state"] == "rendering" and displayed(), (label, s)
    value = dict(
        label=label,
        time=time.monotonic() - started,
        host=ch.process_memory(),
        worker=worker,
        dwell_requested_seconds=dwell,
        render_device_ids=s["render_device_ids"],
        rendered_resolution=[panel._live_frame_metadata.get(key) for key in ("rendered_width", "rendered_height")],
        render={k: s.get(k) for k in ("state", "generation", "render_revision", "current_sample", "progress")},
        counters={
            k: s[k]
            for k in (
                "object_resources",
                "geometry_resources",
                "geometry_conversions",
                "owned_geometry_bytes",
                "material_resources",
                "light_resources",
                "mesh_variant_resources",
                "device_allocation_bytes",
            )
            if k in s
        },
    )
    report["samples"].append(value)
    assert value["host"]["pid"] == os.getpid()
    for owner in ("host", "worker"):
        snapshot = value[owner]
        counters = snapshot.get("host", {})
        if snapshot.get("schema_version") != 1 or any(
            counters.get(key, 0) <= 0 for key in ("working_set_bytes", "private_commit_bytes")
        ):
            raise SoakTelemetryError(f"Missing required {owner} process memory at {label}: {snapshot}")
    if board:
        try:
            value["gpu_usage"] = {
                owner: {
                    board["luid"]: dict(
                        name=args.device, **selected_adapter(value[owner], board, require_usage=owner == "worker")
                    )
                }
                for owner in ("host", "worker")
            }
        except (AssertionError, KeyError, TypeError) as error:
            raise SoakTelemetryError(f"Cannot attribute all required DXGI counters for {label} to {board}") from error
    else:
        value["gpu_usage"] = dict(host=gpu_usage(value["host"]), worker=gpu_usage(worker))
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return value


def scene():
    obj = hou.node("/obj").createNode("geo", "soak_subject", run_init_scripts=False)
    box = obj.createNode("box")
    uv = obj.createNode("attribwrangle")
    uv.setInput(0, box)
    uv.parm("snippet").set("v@uv=set(.5,.5,0);")
    uv.setRenderFlag(True)
    uv.setDisplayFlag(True)
    material = ch.create_material(name="soak_material", shader="diffuse_bsdf")
    image = material.createNode("cycles::image_texture::1", "soak_image")
    image.setParms(dict(filename=str(texture), colorspace="data", alpha_type="ignore"))
    material.node("surface_shader").setNamedInput("color", image, "color")
    obj.parm("shop_materialpath").set(material.path())
    camera = hou.node("/obj").createNode("cam", "soak_camera")
    camera.parm("tz").set(5)
    light = hou.node("/obj").createNode("hlight", "soak_light")
    light.setParms(dict(tx=2, ty=3, tz=4, light_intensity=3))
    rop = hou.node("/out").createNode("cycles_ipr", "soak_render")
    rop.setParms(
        dict(
            vobject=obj.name() + " " + light.name(),
            camera=camera.path(),
            resx=160,
            resy=100,
            resolution_percent=100,
            ipr_resolution_percent=100,
            ipr_samples=1048576,
            ipr_adaptive_sampling=0,
            ipr_use_denoise=0,
            allowmotionblur=0,
        )
    )
    device_selection.set_devices(rop, args.device)
    return obj, box, camera, light, rop, image


def open_panel(rop):
    p = RenderPanel()
    p.auto.setChecked(False)
    p.set_rop(rop)
    p.resize(900, 650)
    p.show()
    return p


def retire(label, clear=False):
    global panel
    session = panel.entry.session
    # Retain the backend process handle so retirement can be checked after the
    # controller has relinquished its backend reference.
    backend = session._controller.backend
    process = backend.process
    worker_path = Path(process.args[0]).resolve()
    assert worker_path == expected_host / "bin/ch_worker.exe", (worker_path, expected_host)
    if clear:
        hou.hipFile.clear(suppress_save_prompt=True)
        assert panel._rop is None
        panel.change_preview_resolution()
        panel.sync_preview_controls()
        until(
            "HIP replacement retires worker",
            lambda: not registry.entries() and session.status()["state"] == "closed" and process.poll() is not None,
        )
    panel.dispose()
    panel.close()
    panel.deleteLater()
    panel = None
    pump(0.2)
    app.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
    gc.collect()
    # Verify the actual panel/scene lifetime path before emergency cleanup can
    # hide a forgotten registry entry or an orphaned worker.
    until(
        "Normal worker retirement",
        lambda: not registry.entries() and session.status()["state"] == "closed" and process.poll() is not None,
    )
    status = session.status()
    assert status["state"] == "closed" and status["device_allocation_bytes"] == 0, status
    assert process.poll() is not None, ("Owned worker survived retirement", process.pid)
    assert process.returncode == 0, ("Owned worker did not retire cleanly", process.pid, process.returncode)
    assert not registry.entries()
    report["lifecycles"].append(
        dict(
            label=label,
            time=time.monotonic() - started,
            pid=process.pid,
            worker=str(worker_path),
            exit_code=process.returncode,
            tracked_after_close=status["device_allocation_bytes"],
            host=ch.process_memory(),
        )
    )


texture = path.parent / "soak-color.exr"
replacement = path.parent / "soak-replacement.exr"
texture_size = 1024 if large else 4
for image_path, color in ((texture, (0.2, 0.6, 0.3)), (replacement, (0.7, 0.1, 0.4))):
    writer = OIIO.ImageOutput.create(str(image_path))
    assert writer.open(str(image_path), OIIO.ImageSpec(texture_size, texture_size, 3, OIIO.FLOAT))
    assert writer.write_image(np.broadcast_to(np.array(color, np.float32), (texture_size, texture_size, 3)).copy())
    assert writer.close()

try:
    if args.prolonged:
        try:
            board = cuda_board(args.device)
        except (AssertionError, AttributeError, OSError, RuntimeError) as error:
            raise SoakTelemetryError(f"Cannot bind {args.device} to an independently verified DXGI adapter") from error
        report["gpu_telemetry"] = dict(
            result="attested", board=board, scope="DXGI current-process local/nonlocal segment usage on node 0"
        )
    cycle = 0
    while cycle < 4 or time.monotonic() - started < args.seconds:
        if panel is None:
            if cycle == 0 or cycle % 2 == 0:
                if large:
                    fixture = create_large_scene(texture, args.device, resolution)
                    obj, box, camera, light, rop, image = [
                        fixture[k] for k in ("obj", "geometry", "camera", "light", "rop", "image")
                    ]
                    report["workload"] = fixture["inventory"]
                else:
                    obj, box, camera, light, rop, image = scene()
            panel = open_panel(rop)
            render("viewer open " + str(cycle), (0.2, 0.6, 0.3) if large else None)
            pump(1)
        pid = panel.entry.session.status()["renderer_pid"]
        for label, parm, value in (
            ("camera", camera.parm("tx"), 0.1),
            ("camera repair", camera.parm("tx"), 0),
            ("light", light.parm("light_intensity"), 4),
            ("light repair", light.parm("light_intensity"), 3),
            ("object", obj.parm("tx"), 0.15),
            ("object repair", obj.parm("tx"), 0),
            ("geometry", box.parm("sx" if large else "sizex"), 1.2),
            ("geometry repair", box.parm("sx" if large else "sizex"), 1),
        ):
            parm.set(value)
            render(label, (0.2, 0.6, 0.3) if large else None)
            assert panel.entry.session.status()["renderer_pid"] == pid
        if large:
            strength = fixture["material"].node("surface_shader").parm("strength")
            strength.set(2)
            render("material strength", (0.4, 1.2, 0.6))
            strength.set(1)
            render("material repair", (0.2, 0.6, 0.3))
            fixture["instances"].parm("tx").set(0.04)
            render("packed instance edit", (0.2, 0.6, 0.3))
            fixture["instances"].parm("tx").set(0)
            render("packed instance repair", (0.2, 0.6, 0.3))
            image.parm("filename").set(str(replacement))
            render("file image replacement", (0.7, 0.1, 0.4))
            image.parm("filename").set("op:" + fixture["cop"].path())
            render("COP binding", (0.3, 0.2, 0.7))
            fixture["cop"].parmTuple("f3").set((0.6, 0.4, 0.2))
            render("COP raster replacement", (0.6, 0.4, 0.2))
            image.parm("filename").set("op:/img/soak_cops/missing")
            render("missing COP", (1, 0, 1))
            assert panel.entry.session.status()["source_diagnostics"]
            fixture["cop"].parmTuple("f3").set((0.3, 0.2, 0.7))
            image.parm("filename").set("op:" + fixture["cop"].path())
            render("COP repair", (0.3, 0.2, 0.7))
            image.parm("filename").set(str(texture))
            render("file binding repair", (0.2, 0.6, 0.3))
            assert not panel.entry.session.status()["source_diagnostics"]
            assert panel.entry.session.status()["renderer_pid"] == pid
        image.parm("filename").set(str(texture.with_name("missing-soak-image.exr")))
        render("missing texture", (1, 0, 1) if large else None)
        assert panel.entry.session.status()["source_diagnostics"], "Missing image produced no diagnostic"
        image.parm("filename").set(str(texture))
        render("texture repair", (0.2, 0.6, 0.3) if large else None)
        assert not panel.entry.session.status()["source_diagnostics"]
        panel.cancel()
        until("Stop", lambda: panel.entry.session.status()["state"] not in ("rendering", "paused"))
        stopped = panel.entry.session.status()["generation"]
        camera.parm("tx").set(0.1)
        pump(0.3)
        assert panel.entry.session.status()["generation"] == stopped, "Stopped viewer restarted on edit"
        camera.parm("tx").set(0)
        render("restart", (0.2, 0.6, 0.3) if large else None)
        assert panel.entry.session.status()["renderer_pid"] == pid
        sample = snapshot("cycle " + str(cycle))
        assert sample["counters"]["object_resources"] > 0, sample["counters"]
        if large:
            assert sample["counters"]["object_resources"] >= 4097, sample["counters"]
            assert sample["counters"]["geometry_resources"] >= 2, sample["counters"]
            assert sample["counters"]["owned_geometry_bytes"] >= 513 * 513 * 3 * 4, sample["counters"]
        # Repeated identical workloads after initial warm-up have the same
        # resource ledger, even while the renderer and display remain active.
        dwell = min(dwell_seconds, max(0.0, args.seconds - (time.monotonic() - started)))
        # Sample throughout the long same-worker dwell; a final live sample is
        # insufficient evidence that rendering stayed healthy for two minutes.
        dwell_end = time.monotonic() + dwell
        next_sample = time.monotonic() + 15.0
        observation = 0
        while next_sample < dwell_end:
            pump(max(0.0, next_sample - time.monotonic()))
            assert displayed(), panel.entry.session.status()
            assert panel.entry.session.status()["renderer_pid"] == pid
            if large:
                observation += 1
                snapshot(f"dwell {observation} cycle {cycle}")
            next_sample = time.monotonic() + 15.0
        pump(max(0.0, dwell_end - time.monotonic()))
        snapshot("settled " + str(cycle), dwell=dwell)
        cycle += 1
        if cycle % 2 == 0:
            retire("HIP replacement " + str(cycle), clear=True)
        else:
            retire("viewer close/reopen " + str(cycle))
    # Ignore first lifecycle warm-up; compare equivalent end-of-cycle states
    # rather than transient update peaks or process-wide peak counters.
    # Intermediate dwell samples have separate same-worker checks below.
    warm = [s for s in report["samples"] if s["label"].startswith("settled ")][1:]
    full_dwells = [s for s in warm if s["dwell_requested_seconds"] == dwell_seconds]
    report["coverage"] = dict(
        warm_settled_cycles=len(warm),
        warm_full_dwells=len(full_dwells),
        warm_settled_span_seconds=warm[-1]["time"] - warm[0]["time"],
    )
    if args.prolonged:
        if len(warm) < 4 or len(full_dwells) < 2:
            raise SoakCoverageError(str(report["coverage"]))
        pids = [item["pid"] for item in report["lifecycles"]]
        settled_pids = [s["worker"]["pid"] for s in report["samples"] if s["label"].startswith("settled ")]
        assert pids == settled_pids and len(set(pids)) == len(pids), (pids, settled_pids)
    if large:
        image_bytes = (1024 * 1024 * 3 + 512 * 512 * 3) * 4
        frame_bytes = resolution[0] * resolution[1] * 4 * 4
        bounds = retention_bounds(warm[0], image_bytes, frame_bytes)
        report["budgets_mib"] = {k: v / 2**20 for k, v in bounds.items()}
        report["budget_basis"] = dict(
            owned_geometry_bytes=warm[0]["counters"]["owned_geometry_bytes"],
            raster_bytes=image_bytes,
            frame_bytes=frame_bytes,
            formula="host=max(2*(geometry+rasters),4*rasters,16*frames); worker=max(geometry+rasters,2*rasters,8*frames); tracked-device=max(64KiB,1*frame); DXGI=max(device-ledger/8,2*rasters,8*frames)",
        )
    summary = {}
    for owner in ("host", "worker"):
        values = [s[owner]["host"]["private_commit_bytes"] for s in warm]
        summary[owner] = dict(
            first_bytes=values[0],
            last_bytes=values[-1],
            **retention_window(warm, values),
        )
        bound(owner, summary[owner]["median_growth_bytes"], report["budgets_mib"][owner] * 2**20, summary[owner])
    allocations = [s["counters"]["device_allocation_bytes"] for s in warm]
    summary["device"] = dict(
        first_bytes=allocations[0], last_bytes=allocations[-1], span_bytes=max(allocations) - min(allocations)
    )
    bound("tracked device", summary["device"]["span_bytes"], report["budgets_mib"]["device"] * 2**20, summary["device"])
    same_worker = []
    for settled in warm:
        cycle_id = settled["label"].split()[-1]
        samples = [s for s in report["samples"] if s["label"].split()[-1] == cycle_id]
        first = samples[0]
        assert first["label"] == "cycle " + cycle_id and samples[-1] is settled
        assert all(s["worker"]["pid"] == settled["worker"]["pid"] for s in samples)
        assert all(s["render"]["render_revision"] == settled["render"]["render_revision"] for s in samples)
        growth = settled["worker"]["host"]["private_commit_bytes"] - first["worker"]["host"]["private_commit_bytes"]
        same_worker.append(
            dict(
                pid=first["worker"]["pid"],
                growth_bytes=growth,
                elapsed_seconds=settled["time"] - first["time"],
                first_sample=first["render"]["current_sample"],
                last_sample=settled["render"]["current_sample"],
                dwell_observations=len(samples),
                commit_window=retention_window(samples, [s["worker"]["host"]["private_commit_bytes"] for s in samples]),
            )
        )
        bound("worker private commit during dwell", growth, report["budgets_mib"]["worker"] * 2**20, same_worker[-1])
        if large and same_worker[-1]["elapsed_seconds"] >= 10:
            assert (
                settled["render"]["current_sample"] > first["render"]["current_sample"]
                or settled["render"]["progress"] == 1
            ), same_worker[-1]
        if board:
            bound(
                "worker private commit median during dwell",
                same_worker[-1]["commit_window"]["median_growth_bytes"],
                bounds["worker"],
                same_worker[-1]["commit_window"],
            )
            if settled["dwell_requested_seconds"] == dwell_seconds:
                # Two disjoint two-sample windows from a genuinely sustained dwell.
                if len(samples) < 4 or same_worker[-1]["elapsed_seconds"] < dwell_seconds:
                    raise SoakCoverageError(str(same_worker[-1]))
            selected = [s["gpu_usage"]["worker"][board["luid"]] for s in samples]
            same_worker[-1]["gpu_windows"] = {
                segment: retention_window(samples, [v[segment] for v in selected]) for segment in ("local", "nonlocal")
            }
            for segment, entry in same_worker[-1]["gpu_windows"].items():
                bound(f"worker DXGI {segment} during dwell", entry["median_growth_bytes"], bounds["gpu"], entry)
            observed = [s["counters"]["device_allocation_bytes"] for s in samples]
            same_worker[-1]["tracked_device_span_bytes"] = max(observed) - min(observed)
            bound(
                "tracked device span during dwell",
                same_worker[-1]["tracked_device_span_bytes"],
                bounds["device"],
                same_worker[-1],
            )
    summary["same_worker_dwell"] = same_worker
    if large:
        summary["dxgi_growth"] = []
        if board:
            report["gpu_telemetry"]["result"] = "available"
            report["gpu_telemetry"]["checked_active_observations"] = len(report["samples"])
        elif args.device == "CPU":
            report["gpu_telemetry"] = dict(result="not_applicable", reason="CPU render device selected")
        else:
            selected = next(d for d in ch.devices() if d["id"] == args.device)
            name = " ".join(selected["name"].casefold().split())
            matches = [
                luid
                for luid, adapter in warm[0]["gpu_usage"]["worker"].items()
                if " ".join(adapter["name"].casefold().split()) == name
            ]
            # The ledger alone does not prove that Windows supplied process
            # GPU residency telemetry. Require the selected adapter, rather
            # than silently accepting an empty adapter loop or unrelated GPU.
            report["gpu_telemetry"] = dict(
                result="unavailable",
                selected_device=selected,
                matched_luids=matches,
                reason="Selected render GPU must appear exactly once and report nonzero worker segment usage in every equivalent sample",
            )
            assert len(matches) == 1, report["gpu_telemetry"]
            luid = matches[0]
            usage = [s["gpu_usage"]["worker"].get(luid) for s in warm]
            assert all(v is not None and v["local"] + v["nonlocal"] > 0 for v in usage), report["gpu_telemetry"]
            report["gpu_telemetry"].update(
                result="available",
                reason=None,
                minimum_worker_segment_bytes=min(v["local"] + v["nonlocal"] for v in usage),
            )
        for owner in ("host", "worker"):
            for luid, adapter in warm[0]["gpu_usage"][owner].items():
                for segment in ("local", "nonlocal"):
                    values = [s["gpu_usage"][owner][luid][segment] for s in warm]
                    entry = dict(
                        owner=owner, adapter=adapter["name"], segment=segment, **retention_window(warm, values)
                    )
                    summary["dxgi_growth"].append(entry)
                    bound(f"{owner} DXGI {segment}", entry["median_growth_bytes"], bounds["gpu"], entry)
    for counter in (
        "object_resources",
        "geometry_resources",
        "owned_geometry_bytes",
        "material_resources",
        "light_resources",
        "mesh_variant_resources",
    ):
        assert len({s["counters"][counter] for s in warm}) == 1, (counter, [s["counters"] for s in warm])
    report.update(
        result="passed",
        retention=summary,
        checks=[
            "camera/light/object/geometry edits",
            "missing texture repair",
            "Stop does not auto-restart",
            "restart preserves worker",
            "viewer reopen",
            "HIP replacement",
            "owned workers exit",
            "zero tracked allocations after retirement",
            "bounded post-warmup commit growth",
        ],
    )
    if large:
        report["checks"] += [
            "dense mesh and 4096 packed instances",
            "file/COP/material replacement and repair",
            "analytic linear emission reference for each settled edit",
            "same-worker long dwell telemetry",
            "resource-scaled commit and DXGI growth bounds",
        ]
except BaseException as error:
    kinds = {
        SoakTelemetryError: "telemetry_unavailable",
        SoakCoverageError: "insufficient_observations",
        SoakRetentionError: "retention_growth",
    }
    report.update(
        result="failed", failure_kind=kinds.get(type(error), "lifecycle_or_correctness"), error=traceback.format_exc()
    )
    raise
finally:
    cleanup_failed = False
    try:
        if panel:
            panel.dispose()
            panel.close()
    except BaseException:
        cleanup_failed = True
        report["panel_cleanup_error"] = traceback.format_exc()
    finally:
        try:
            registry.close_all()
        except BaseException:
            cleanup_failed = True
            report["registry_cleanup_error"] = traceback.format_exc()
        finally:
            if cleanup_failed:
                report["result"] = "failed"
            report["wall_seconds"] = time.monotonic() - started
            path.write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(
                json.dumps(
                    dict(
                        result=report["result"],
                        wall_seconds=report["wall_seconds"],
                        lifecycles=len(report["lifecycles"]),
                        report=str(path),
                    )
                )
            )
    if cleanup_failed and not report.get("error"):
        raise RuntimeError("Soak cleanup failed; see report")
