# SPDX-License-Identifier: Apache-2.0
"""Isolated installed-host worker recovery and ordered native ROP output.

Launch with the installer's explicit preference/package directory. This fixture
does not import developer test helpers or use source-checkout assets.
"""

import pass_fixtures as pass_fixture

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import traceback

import hou
import numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import final_render, menu_actions, native_scene, registry
from cycles_houdini.device_selection import set_devices

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--device", default="CPU")
parser.add_argument("--report", type=Path, required=True)
parser.add_argument("--expected-host", type=Path, required=True)
parser.add_argument("--expected-data-root", type=Path)
args = parser.parse_args()
host = args.expected_host.resolve()
data = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"]).resolve()
report = dict(
    result="running",
    device=args.device,
    expected_host=str(host),
    data_root=str(data),
    checks=[],
    scope="Relocated installed Python/HDA/native/kernels; persistent worker source repair, Stop/restart, worker failure recovery; three ordered foreground final frames",
)
session = None


def save():
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")


def under(path, root):
    value = Path(path).resolve()
    assert value.is_relative_to(root), (str(value), str(root))
    return str(value)


def until(predicate, timeout=45):
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise TimeoutError("Relocated release condition stalled: " + json.dumps(session.status() if session else {}))


def plane(frame, name):
    layer = frame["planes"][name]
    return np.frombuffer(layer["pixels"], np.float32).reshape(frame["height"], frame["width"], layer["channels"])


def render(label, expected, warning=False):
    generation = session.apply(native_scene.capture(rop))
    frame = session.wait(generation, timeout=90)
    image = plane(frame, "combined")
    center = image[frame["height"] // 2, frame["width"] // 2, :3]
    assert np.isfinite(image).all() and np.allclose(center, expected, atol=0.002, rtol=0), (label, center, expected)
    status = session.status()
    assert status["state"] != "failed" and bool(status["source_diagnostics"]) == warning, status
    assert status["render_device_ids"] == [args.device], status["render_device_ids"]
    backend = session._controller.backend
    executable = under(backend.process.args[0], host)
    assert Path(executable) == host / "bin/ch_worker.exe"
    log = under(backend.log_path, data)
    assert Path(log).is_file() and status["renderer_pid"] != os.getpid()
    report["checks"].append(dict(case=label, pixel=center.tolist(), status=status, worker=executable, worker_log=log))
    save()
    return frame, status


def inspect(path, expected):
    under(path, data)
    image = oiio.ImageInput.open(str(path))
    assert image, (str(path), oiio.geterror())
    try:
        spec = image.spec()
        pixels = np.asarray(image.read_image(format=oiio.FLOAT), np.float32)
        center = pixels[spec.height // 2, spec.width // 2, [spec.channelnames.index(name) for name in ("R", "G", "B")]]
        assert (spec.width, spec.height) == (80, 60) and np.allclose(center, expected, atol=0.002, rtol=0), (
            str(path),
            center,
            expected,
        )
        return dict(
            path=str(path),
            bytes=path.stat().st_size,
            pixel=center.tolist(),
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    finally:
        image.close()


try:
    assert host == Path(os.environ["CYCLES_HOUDINI_HOST_ROOT"]).resolve()
    assert (host / "resources").resolve() == Path(os.environ["CYCLES_HOUDINI_RESOURCES"]).resolve()
    assert data != Path(__file__).resolve().parents[1], "Installed runtime data points into the developer source root"
    if args.expected_data_root:
        assert data == args.expected_data_root.resolve()
    report["python"] = under(ch.__file__, host)
    report["native"] = under(ch._native.__file__, host)
    manifest = json.loads((host.parent / "release-manifest.json").read_text())
    report.update(package_id=manifest["package_id"], runtime_fingerprint=manifest["runtime_fingerprint"])
    assert os.environ["CYCLES_HOUDINI_RELEASE_ID"] == manifest["package_id"]
    kernels = []
    for record in manifest["files"]:
        if record["path"].startswith("host/resources/lib/"):
            path = host.parent / record["path"]
            under(path, host)
            assert hashlib.sha256(path.read_bytes()).hexdigest() == record["sha256"]
            kernels.append(str(path))
    assert kernels, "Installed GPU kernel resources are absent"
    report["kernels"] = kernels
    output = (
        data
        / "artifacts/relocated-release"
        / (args.device.replace(":", "_").replace(" ", "_") + "-" + str(time.time_ns()))
    )
    output.mkdir(parents=True)
    report["output_root"] = str(output)
    hou.setFps(24)
    hou.setFrame(1)
    geo = hou.node("/obj").createNode("geo", "relocated_subject", run_init_scripts=False)
    shape = geo.createNode("box")
    shape.parmTuple("size").set((2, 2, 0.2))
    shape.setRenderFlag(True)
    material = ch.create_material(name="relocated_material", shader="emission")
    shader = material.node("surface_shader")
    shader.parmTuple("color").set((0.1, 0.4, 0.7))
    pass_fixture.parm(shader, "strength").set(1)
    pass_fixture.parm(geo, "shop_materialpath").set(material.path())
    camera = hou.node("/obj").createNode("cam", "relocated_camera")
    pass_fixture.set_parameters(camera, dict(tz=5, projection=1, orthowidth=4))
    light = menu_actions.create_light("grid")
    rop = hou.node("/out").createNode("cycles_ipr", "relocated_render")
    for node in (rop, light):
        definition = node.type().definition()
        assert definition is not None
        report.setdefault("hda", {})[node.type().name()] = under(definition.libraryFilePath(), host)
    set_devices(rop, args.device)
    pass_fixture.set_parameters(
        rop,
        dict(
            source_mode="sop",
            soppath=shape.path(),
            camera=camera.path(),
            resx=80,
            resy=60,
            samples=4,
            ipr_samples=4,
            adaptive_sampling=0,
            ipr_adaptive_sampling=0,
            use_denoise=0,
            ipr_use_denoise=0,
            allowmotionblur=0,
            use_material_assignments=1,
            render_threads=2,
            persistent_data=1,
            pass_depth=1,
        ),
    )
    rop.parmTuple("background").set((0, 0, 0))
    session = ch.Session(
        device=args.device,
        width=80,
        height=60,
        samples=4,
        interactive=True,
        execution="worker",
        collect_diagnostics=True,
        threads=2,
        passes=["combined", "depth"],
    )
    _, initial = render("initial", (0.1, 0.4, 0.7))
    initial_pid = initial["renderer_pid"]
    missing_path = "/mat/relocated_repaired_material"
    pass_fixture.parm(geo, "shop_materialpath").set(missing_path)
    render("missing material stays visible", (1, 0, 1), warning=True)
    repaired = ch.create_material(name="relocated_repaired_material", shader="emission")
    assert repaired.path() == missing_path
    repaired.node("surface_shader").parmTuple("color").set((0.7, 0.2, 0.1))
    pass_fixture.parm(repaired.node("surface_shader"), "strength").set(1)
    _, repaired_status = render("material repair in existing worker", (0.7, 0.2, 0.1))
    assert repaired_status["renderer_pid"] == initial_pid
    session.cancel()
    until(lambda: session.status()["state"] == "cancelled")
    _, restarted = render("Stop then restart", (0.7, 0.2, 0.1))
    assert restarted["renderer_pid"] == initial_pid
    backend = session._controller.backend
    backend.process.kill()
    until(lambda: session.status()["state"] == "failed")
    report["worker_failure"] = session.status()
    _, recovered = render("terminated worker recovers from installed host", (0.7, 0.2, 0.1))
    assert recovered["renderer_pid"] != initial_pid and recovered["backend_epoch"] > initial["backend_epoch"]
    session.write_exr(output / "worker-recovered.exr")
    report["worker_output"] = inspect(output / "worker-recovered.exr", (0.7, 0.2, 0.1))
    session.close()
    closed = until(lambda: session.status() if session.status()["state"] == "closed" else None)
    assert closed["owned_geometry_bytes"] == closed["device_allocation_bytes"] == 0, closed
    report["worker_closed"] = closed
    session = None

    # HDA lifecycle callbacks, SOHO and final rendering must also resolve from
    # the installed host. Frame order and values are independent expectations.
    pass_fixture.parm(geo, "shop_materialpath").set(material.path())
    shader.parmTuple("color").set((0.2, 0.3, 0.5))
    shader.parmTuple("color")[0].setExpression(".2 + .1*($F-1)", hou.exprLanguage.Hscript)
    pass_fixture.parm(rop, "picture").set(str(output / "final.$F4.exr"))
    ordered = []

    def observe(event, value):
        if event == "sequence" and value[0] == rop and value[1]["reason"] == "frame-written":
            ordered.append(dict(value[1]))

    with final_render.observe(observe):
        rop.render(frame_range=(1, 3, 1))
    summary = rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 3 and summary["created_sessions"] == 1 and not summary["session_alive"], (
        summary
    )
    assert [round(value["last_frame"]) for value in ordered] == [1, 2, 3], ordered
    assert not final_render.active_sequences()
    report["final"] = dict(
        summary=summary,
        ordered=ordered,
        images=[inspect(output / f"final.{frame:04d}.exr", (0.2 + 0.1 * (frame - 1), 0.3, 0.5)) for frame in (1, 2, 3)],
    )
    report["imported_modules"] = {
        name: under(module.__file__, host)
        for name, module in list(sys.modules.items())
        if name.startswith("cycles_houdini") and getattr(module, "__file__", None)
    }
    assert not rop.errors(), rop.errors()
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    if session is not None:
        session.close()
    final_render.close_all()
    registry.close_all()
    save()
print(json.dumps(dict(result=report["result"], report=str(args.report), package_id=report.get("package_id"))))
