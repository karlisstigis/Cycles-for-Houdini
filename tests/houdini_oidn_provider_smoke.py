# SPDX-License-Identifier: Apache-2.0
"""Noisy SDK OIDN controls and actual host/worker provider identities.

Cross-render data comparisons use numerical tolerances; Cryptomatte uses exact
bit patterns. This verifies working controls, not equivalence to another OIDN
version's model, image quality or performance.
"""

from support.paths import artifacts_root

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import struct
import time
import traceback

import hou
import numpy as np
import cycles_houdini as ch

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--device", default="CPU", help="CPU, exact ID, unique OptiX name substring, or a JSON array of GPU IDs"
)
parser.add_argument("--report", type=Path)
parser.add_argument("--output", type=Path)
parser.add_argument("--expected-host", type=Path)
parser.add_argument("--expected-data-root", type=Path)
args = parser.parse_args()
data = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"]).resolve()
args.report = args.report or artifacts_root() / "stage3/denoising/sdk-provider/report.json"
args.output = args.output or args.report.parent / "images"
args.report.parent.mkdir(parents=True, exist_ok=True)
args.output.mkdir(parents=True, exist_ok=True)
report = dict(
    result="running",
    scope=__doc__,
    groups=[],
    module_snapshots=[],
    parent_pid=os.getpid(),
    host=os.environ["CYCLES_HOUDINI_HOST_ROOT"],
    data_root=os.environ["CYCLES_HOUDINI_SOURCE_ROOT"],
)
active = None
hash_cache = {}


def save():
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")


def file_info(path):
    key = str(path).casefold()
    if key in hash_cache:
        return hash_cache[key]
    version_api = ctypes.WinDLL("version", use_last_error=True)
    version_api.GetFileVersionInfoSizeW.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD))
    version_api.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    version_api.GetFileVersionInfoW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p)
    version_api.VerQueryValueW.argtypes = (
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.UINT),
    )
    unused = wintypes.DWORD()
    size = version_api.GetFileVersionInfoSizeW(str(path), ctypes.byref(unused))
    version = None
    if size:
        buffer = ctypes.create_string_buffer(size)
        if version_api.GetFileVersionInfoW(str(path), 0, size, buffer):
            pointer, length = ctypes.c_void_p(), wintypes.UINT()
            if version_api.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)):
                fixed = struct.unpack_from("<13I", ctypes.string_at(pointer, length.value))
                version = ".".join(str(x) for x in (fixed[2] >> 16, fixed[2] & 65535, fixed[3] >> 16, fixed[3] & 65535))
    with Path(path).open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    value = dict(path=str(Path(path).resolve()), version=version, sha256=digest, bytes=Path(path).stat().st_size)
    hash_cache[key] = value
    return value


def modules(pid, label):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    psapi.EnumProcessModulesEx.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.HMODULE),
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.DWORD,
    )
    psapi.GetModuleFileNameExW.argtypes = (wintypes.HANDLE, wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD)
    psapi.GetModuleFileNameExW.restype = wintypes.DWORD
    handle = kernel.OpenProcess(0x400 | 0x10, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    result = []
    try:
        capacity = 1024
        while True:
            buffer = (wintypes.HMODULE * capacity)()
            needed = wintypes.DWORD()
            if not psapi.EnumProcessModulesEx(handle, buffer, ctypes.sizeof(buffer), ctypes.byref(needed), 3):
                raise ctypes.WinError(ctypes.get_last_error())
            if needed.value <= ctypes.sizeof(buffer):
                break
            capacity = needed.value // ctypes.sizeof(wintypes.HMODULE) + 128
        for index in range(needed.value // ctypes.sizeof(wintypes.HMODULE)):
            name = ctypes.create_unicode_buffer(32768)
            length = psapi.GetModuleFileNameExW(handle, buffer[index], name, len(name))
            assert 0 < length < len(name), ("Incomplete module attestation", pid, index, ctypes.get_last_error())
            path = Path(name.value)
            if path.name.lower().startswith("openimagedenoise"):
                result.append(file_info(path))
    finally:
        kernel.CloseHandle(handle)
    assert any(Path(x["path"]).name.lower() == "openimagedenoise.dll" for x in result), (pid, result)
    expected = {
        (Path(os.environ["HFS"]) / entry["path"]).resolve(): entry
        for entry in provider["files"]
        if entry["kind"] == "runtime"
    }
    for entry in result:
        path = Path(entry["path"])
        assert path in expected, ("Foreign or preloaded OIDN provider", label, entry)
        assert all(entry[key] == expected[path][key] for key in ("version", "sha256", "bytes")), (
            label,
            entry,
            expected[path],
        )
    snapshot = dict(pid=pid, label=label, modules=result)
    report["module_snapshots"].append(snapshot)
    save()
    return snapshot


def array(frame, name):
    value = frame["planes"][name]
    return np.frombuffer(value["pixels"], np.float32).reshape(frame["height"], frame["width"], value["channels"])


def settings(**overrides):
    return dict(
        dict(
            enabled=True,
            method="oidn",
            use_gpu=False,
            quality="high",
            prefilter="accurate",
            passes="albedo_normal",
            start_sample=0,
        ),
        **overrides,
    )


def wait(session, generation):
    frame = session.wait(generation, timeout=120)
    assert session.status()["state"] != "failed", session.status()
    return frame


def selected_targets(value, available):
    """A requested GPU array is one render target, never separate sessions."""
    by_id = {entry["id"]: entry for entry in available}
    if value.lstrip().startswith("["):
        requested = json.loads(value)
        assert (
            isinstance(requested, list) and requested and all(isinstance(item, str) and item for item in requested)
        ), "Expected a nonempty JSON GPU ID array"
        assert len(requested) == len(set(requested)), "Duplicate device IDs"
        assert all(item in by_id for item in requested), ("Unavailable device IDs", requested, list(by_id))
        records = [by_id[item] for item in requested]
        backends = {entry["backend"] for entry in records}
        assert len(backends) == 1 and backends <= {"CUDA", "OPTIX"}, (
            "Use GPUs from one CUDA/OptiX backend; CPU mixing is unsupported"
        )
        physical = [entry["physical_id"] for entry in records]
        assert all(isinstance(item, str) and item for item in physical), "Missing physical adapter identity"
        assert len(physical) == len(set(physical)), "Duplicate physical adapters"
        return [requested]
    if value == "CPU":
        assert value in by_id, "CPU device is unavailable"
        return [value]
    exact = [entry["id"] for entry in available if entry["id"] == value]
    choices = exact or [
        entry["id"]
        for entry in available
        if entry["backend"] == "OPTIX" and value.casefold() in entry["name"].casefold()
    ]
    assert len(choices) == 1, (value, available)
    return choices


def assert_render_target(status, target):
    expected = [target] if isinstance(target, str) else target
    actual = status["render_device_ids"]
    assert len(actual) == len(expected) and set(actual) == set(expected), (
        "Session did not use requested devices",
        expected,
        actual,
    )


try:
    host = Path(report["host"]).resolve()
    if args.expected_host:
        assert host == args.expected_host.resolve(), (host, args.expected_host)
    if args.expected_data_root:
        assert data == args.expected_data_root.resolve(), (data, args.expected_data_root)
    assert Path(ch.__file__).resolve().is_relative_to(host)
    assert Path(ch._native.__file__).resolve().is_relative_to(host)
    report["native"] = str(Path(ch._native.__file__).resolve())
    release_manifest = host.parent / "release-manifest.json"
    report["package"] = json.loads(release_manifest.read_text())["package_id"] if release_manifest.is_file() else None
    provider = json.loads((host / "resources/sdk-dependencies.json").read_text())["openimagedenoise"]
    assert (
        provider["provider"] == "houdini-sdk" and provider["version"] == "2.3.3" and provider["houdini"] == "22.0.466"
    ), provider
    report["expected_provider"] = provider
    report["capabilities"] = ch.capabilities()
    assert report["capabilities"]["openimagedenoise_provider"]["version"] == provider["version"]
    report["available_devices"] = ch.devices()
    selected = selected_targets(args.device, report["available_devices"])
    report["selected_devices"] = selected
    modules(os.getpid(), "host after native import/device enumeration")
    hou.setFrame(1)
    plane_obj = hou.node("/obj").createNode("geo", "oidn_probe_receiver", run_init_scripts=False)
    plane = plane_obj.createNode("python", "surface")
    plane.parm("python").set("""import hou
g=hou.pwd().geometry(); face=g.createPolygon()
for position in ((-3.,-3.,0.),(3.,-3.,0.),(3.,3.,0.),(-3.,3.,0.)):
    p=g.createPoint(); p.setPosition(position); face.addVertex(p)
""")
    box_obj = hou.node("/obj").createNode("geo", "oidn_probe_occluder", run_init_scripts=False)
    box_obj.parmTuple("t").set((-0.3, -0.1, 0.5))
    box_obj.parmTuple("r").set((10, 25, 0))
    box = box_obj.createNode("box")
    box.parmTuple("size").set((0.7, 0.8, 0.8))
    camera = hou.node("/obj").createNode("cam", "oidn_probe_camera")
    camera.setParms(dict(tz=5, projection=1, orthowidth=4))
    scene = dict(
        geometry=[
            ch.geometry_request(plane_obj, plane, material="probe/receiver"),
            ch.geometry_request(box_obj, box, material="probe/rough"),
        ],
        materials=[
            dict(
                key="probe/receiver",
                kind="diffuse",
                color=(0.3, 0.4, 0.5),
                roughness=0.3,
                cryptomatte_name="probe/receiver",
            ),
            dict(
                key="probe/rough", kind="diffuse", color=(0.7, 0.25, 0.1), roughness=0.5, cryptomatte_name="probe/rough"
            ),
        ],
        camera_path=camera.path(),
        background=(0.01, 0.01, 0.01),
        lights=[
            dict(
                key="probe/area",
                kind="area",
                transform=(1, 0, 0, -1, 0, 1, 0, 1.5, 0, 0, 1, 2.5),
                strength=(80, 70, 60),
                size=(1.2, 1.2),
            )
        ],
        cryptomatte=dict(object=True, material=True, asset=False, levels=2),
    )
    variants = [
        ("high", {}),
        ("balanced", dict(quality="balanced")),
        ("fast", dict(quality="fast")),
        ("prefilter_fast", dict(prefilter="fast")),
        ("prefilter_none", dict(prefilter="none")),
        ("guides_rgb", dict(passes="rgb")),
        ("guides_albedo", dict(passes="albedo")),
    ]
    for execution in ("sync", "worker"):
        for device in selected:
            for use_gpu in [False] if device == "CPU" else [False, True]:
                index = len(report["groups"])
                group = dict(
                    execution=execution,
                    device=device,
                    requested_device_ids=[device] if isinstance(device, str) else list(device),
                    use_gpu=use_gpu,
                    cases=[],
                    result="running",
                )
                report["groups"].append(group)
                active = ch.Session(
                    device=device,
                    width=96,
                    height=64,
                    samples=4,
                    interactive=False,
                    execution=execution,
                    threads=2,
                    collect_diagnostics=True,
                    denoising=dict(enabled=False),
                    passes=["combined", "depth", "normal", "position", "uv"],
                )
                initial = wait(active, active.apply(scene))
                baseline = {name: array(initial, name).copy() for name in initial["planes"]}
                opaque = baseline["combined"][:, :, 3] > 0.999
                opaque[:3] = opaque[-3:] = False
                opaque[:, :3] = opaque[:, -3:] = False
                assert opaque.sum() > 1000
                initial_status = active.status()
                assert_render_target(initial_status, device)
                geometry_conversions = initial_status["geometry_conversions"]
                worker = active._controller.backend.process if execution == "worker" else None
                if worker:
                    group["startup_provider"] = active._controller.backend.oidn_provider
                    assert group["startup_provider"]["provider"] == "houdini-sdk"
                    assert group["startup_provider"]["version"] == provider["version"]
                process_id = worker.pid if worker else os.getpid()
                group["initial_status"] = initial_status
                group["baseline_noisy_sha256"] = hashlib.sha256(baseline["combined"].tobytes()).hexdigest()
                for label, overrides in variants:
                    policy = settings(**dict(overrides, use_gpu=use_gpu))
                    frame = wait(active, active.apply(dict(denoising=policy)))
                    current = active.status()
                    assert_render_target(current, device)
                    assert current["denoiser"] == ("oidn_gpu" if use_gpu else "oidn_cpu"), current
                    assert current["geometry_conversions"] == geometry_conversions, current
                    noisy, denoised = array(frame, "noisy_combined"), array(frame, "combined")
                    assert np.isfinite(denoised).all()
                    assert np.allclose(noisy, baseline["combined"], rtol=1e-5, atol=1e-5), label
                    assert np.array_equal(denoised[:, :, 3], noisy[:, :, 3]), label
                    errors = {}
                    for name in ("depth", "normal", "position", "uv"):
                        actual = array(frame, name)
                        errors[name] = float(np.max(np.abs(actual - baseline[name])))
                        assert np.allclose(actual, baseline[name], rtol=1e-6, atol=1e-5), (label, name, errors[name])
                    crypto = [name for name in initial["planes"] if name.startswith("Crypto")]
                    assert {"CryptoObject00", "CryptoMaterial00"} <= set(crypto), crypto
                    for name in crypto:
                        assert np.array_equal(array(frame, name).view(np.uint32), baseline[name].view(np.uint32)), (
                            label,
                            name,
                        )
                    difference = np.abs(denoised[:, :, :3] - noisy[:, :, :3])[opaque]
                    assert float(difference.mean()) > 1e-6 and float(difference.max()) > 1e-5, (
                        label,
                        float(difference.mean()),
                    )
                    group["cases"].append(
                        dict(
                            case=label,
                            settings=policy,
                            status=current,
                            raw_pass_max_errors=errors,
                            cryptomatte_bitwise_unchanged=crypto,
                            denoised_noisy_mean_absolute=float(difference.mean()),
                            denoised_noisy_max_absolute=float(difference.max()),
                            denoised_sha256=hashlib.sha256(denoised.tobytes()).hexdigest(),
                        )
                    )
                    save()
                hashes = {case["case"]: case["denoised_sha256"] for case in group["cases"]}
                group["distinct_control_outputs"] = {
                    "qualities": len({hashes[name] for name in ("high", "balanced", "fast")}),
                    "prefilters": len({hashes[name] for name in ("high", "prefilter_fast", "prefilter_none")}),
                    "guides": len({hashes[name] for name in ("high", "guides_rgb", "guides_albedo")}),
                }
                assert all(count == 3 for count in group["distinct_control_outputs"].values()), group[
                    "distinct_control_outputs"
                ]
                group["loaded_provider"] = modules(process_id, f"{execution}:{device}:gpu={use_gpu}")
                output = args.output / f"group-{index:02d}-denoised.exr"
                active.write_exr(output)
                group["output"] = dict(
                    path=str(output),
                    bytes=output.stat().st_size,
                    sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
                )
                disabled = wait(active, active.apply(dict(denoising=dict(enabled=False))))
                assert "noisy_combined" not in disabled["planes"]
                assert np.allclose(array(disabled, "combined"), baseline["combined"], atol=1e-5, rtol=1e-5)
                group["disabled_status"] = active.status()
                if execution == "worker":
                    worker_log = active._controller.backend.log_path.resolve()
                    group["worker_log"] = str(worker_log)
                    if explicit := os.environ.get("CYCLES_HOUDINI_WORKER_LOG_DIR"):
                        assert worker_log.parent == Path(explicit).resolve()
                    else:
                        assert worker_log.parent.relative_to(Path(report["data_root"]).resolve()).parts == (
                            "artifacts",
                            "workers",
                        )
                active.close()
                closed = active.status()
                assert (
                    closed["state"] == "closed"
                    and closed["owned_geometry_bytes"] == closed["device_allocation_bytes"] == 0
                ), closed
                group["closed_status"] = closed
                if worker:
                    group["worker_exit_code"] = worker.wait(timeout=15)
                    assert group["worker_exit_code"] == 0, group["worker_exit_code"]
                group["result"] = "passed"
                active = None
                save()
                print(
                    json.dumps(dict(group=index, execution=execution, device=device, use_gpu=use_gpu, result="passed")),
                    flush=True,
                )
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    if active:
        active.close()
    save()
print(json.dumps(dict(result=report["result"], groups=len(report["groups"]), report=str(args.report))), flush=True)
