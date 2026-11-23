# SPDX-License-Identifier: Apache-2.0
"""Analytic pixel motion, independent guides, real EXR encoding and viewer controls."""

from support.paths import artifacts_root

import pass_fixtures as pass_fixture

from cycles_houdini.device_selection import set_devices as set_rop_devices
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import json
import sys
import time
from pathlib import Path
import numpy as np
import OpenImageIO as oiio
import hou
import cycles_houdini as ch
from cycles_houdini import native_scene, output, registry

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
dest = artifacts_root() / "stage3/vectors-output"
dest.mkdir(parents=True, exist_ok=True)
device = sys.argv[sys.argv.index("--device") + 1] if "--device" in sys.argv else "CPU"
hou.setFps(24)
hou.setFrame(10)
geo = hou.node("/obj").createNode("geo", "vector_subject", run_init_scripts=False)
box = geo.createNode("box")
box.parmTuple("size").set((1, 1, 0.1))
cam = hou.node("/obj").createNode("cam")
pass_fixture.set_parameters(cam, dict(tz=5, projection=1, orthowidth=4))
rop = hou.node("/out").createNode("cycles_ipr")
set_rop_devices(rop, device)
pass_fixture.set_parameters(
    rop,
    dict(
        source_mode="sop",
        soppath=box.path(),
        camera=cam.path(),
        resx=128,
        resy=96,
        samples=16,
        ipr_samples=16,
        ipr_adaptive_sampling=0,
        ipr_use_denoise=0,
        adaptive_sampling=0,
        allowmotionblur=0,
        use_denoise=0,
        pass_motion=1,
        pass_noisy_combined=1,
        pass_denoising_albedo=1,
        pass_denoising_normal=1,
        pass_denoising_depth=1,
        pass_depth=1,
        material_kind="diffuse",
    ),
)
rop.parmTuple("background").set((0.5, 0.5, 0.5))
checks = []


def plane(frame, name):
    p = frame["planes"][name]
    return np.frombuffer(p["pixels"], np.float32).reshape(frame["height"], frame["width"], p["channels"])


def vector(frame, expected):
    v = plane(frame, "motion")[40:56, 56:72]
    assert np.isfinite(v).all()
    actual = np.median(v, axis=(0, 1))
    assert np.allclose(actual, expected, atol=0.025), (actual, expected)
    return actual.tolist()


def read(path):
    f = oiio.ImageInput.open(str(path))
    assert f, oiio.geterror()
    spec = f.spec()
    pixels = f.read_image(format=oiio.FLOAT)
    f.close()
    return spec, pixels


with ch.Session(device=device, width=128, height=96, samples=16, interactive=True) as session:

    def render():
        return session.wait(session.apply(native_scene.capture(rop), at_time=hou.time()))

    frame = render()
    vector(frame, [0, 0, 0, 0])
    for name, n in [("denoising_albedo", 3), ("denoising_normal", 3), ("denoising_depth", 1), ("noisy_combined", 4)]:
        a = plane(frame, name)
        assert a.shape[-1] == n and np.isfinite(a).all() and np.any(a != 0), name
    assert np.array_equal(plane(frame, "combined"), plane(frame, "noisy_combined"))
    pass_fixture.parm(geo, "tx").setExpression("($FF-10)*0.1")
    frame = render()
    checks.append(dict(transform=vector(frame, [-3.2, 0, -3.2, 0])))
    # A different edit must not reinterpret animation vectors as viewport history.
    rop.parmTuple("background").set((0.3, 0.4, 0.5))
    vector(render(), [-3.2, 0, -3.2, 0])
    for frame_number in range(8, 13):
        hou.setFrame(frame_number)
        vector(render(), [-3.2, 0, -3.2, 0])
    hou.setFrame(10)
    pass_fixture.parm(geo, "tx").deleteAllKeyframes()
    pass_fixture.parm(geo, "tx").set(0)
    pass_fixture.parm(cam, "tx").setExpression("($FF-10)*0.1")
    vector(render(), [3.2, 0, 3.2, 0])
    pass_fixture.parm(cam, "tx").deleteAllKeyframes()
    pass_fixture.parm(cam, "tx").set(0)
    deform = geo.createNode("xform")
    deform.setInput(0, box)
    pass_fixture.parm(deform, "tx").setExpression("($FF-10)*0.1")
    pass_fixture.set_parameters(rop, dict(soppath=deform.path(), geometry_motion="deformation"))
    vector(render(), [-3.2, 0, -3.2, 0])
    velocity = geo.createNode("attribwrangle")
    velocity.setInput(0, box)
    pass_fixture.parm(velocity, "snippet").set("v@v=set(2.4,0,0);")
    pass_fixture.set_parameters(rop, dict(soppath=velocity.path(), geometry_motion="velocity"))
    vector(render(), [-3.2, 0, -3.2, 0])
    pass_fixture.parm(velocity, "snippet").set("v@v=set(0,0,0);")
    vector(render(), [0, 0, 0, 0])
    # Points and curves use the same velocity contract without artist-authored IDs.
    primitive = geo.createNode("python", "vector_primitives")
    common = "import hou\ng=hou.pwd().geometry();g.addAttrib(hou.attribType.Point,'v',(2.4,0.,0.));"
    for kind, code in [
        ("points", "g.addAttrib(hou.attribType.Point,'pscale',.4);g.createPoint()"),
        (
            "curves",
            "g.addAttrib(hou.attribType.Point,'width',.4);c=g.createPolygon(is_closed=False)\nfor y in (-1,0,1):\n p=g.createPoint();p.setPosition((0,y,0));c.addVertex(p)",
        ),
    ]:
        pass_fixture.parm(primitive, "python").set(common + code)
        pass_fixture.parm(rop, "soppath").set(primitive.path())
        f = render()
        mask = plane(f, "depth")[:, :, 0] < 100
        assert np.count_nonzero(mask) > 20
        v = np.median(plane(f, "motion")[mask], axis=0)
        assert np.allclose(v, [-3.2, 0, -3.2, 0], atol=0.025), (kind, v)
    pass_fixture.set_parameters(rop, dict(soppath=box.path(), allowmotionblur=1))
    assert "motion" not in render()["planes"]
    pass_fixture.parm(rop, "allowmotionblur").set(0)
    vector(render(), [0, 0, 0, 0])
    packed = geo.createNode("pack")
    packed.setInput(0, box)
    packed_move = geo.createNode("xform")
    packed_move.setInput(0, packed)
    pass_fixture.parm(packed_move, "tx").setExpression("($FF-10)*0.1")
    pass_fixture.set_parameters(rop, dict(soppath=packed_move.path(), geometry_motion="transform"))
    vector(render(), [-3.2, 0, -3.2, 0])
    # A nominal packed population supplies vectors directly from point v;
    # neither artist IDs nor shutter SOP evaluation are required.
    packed_velocity = geo.createNode("attribwrangle", "packed_vector_velocity")
    packed_velocity.setInput(0, packed)
    pass_fixture.set_parameters(rop, dict(soppath=packed_velocity.path(), geometry_motion="velocity"))
    for value, wanted in (("2.4", [-3.2, 0, -3.2, 0]), ('atof("nan")', [0, 0, 0, 0]), ("2.4", [-3.2, 0, -3.2, 0])):
        pass_fixture.parm(packed_velocity, "snippet").set("v@v=set(" + value + ",0,0);")
        vector(render(), wanted)
        status = session.status()
        assert status["motion_cooks"] == 0 and bool(status["source_diagnostics"]) == ("nan" in value), status
    pass_fixture.parm(rop, "soppath").set(packed.path())
    vector(render(), [0, 0, 0, 0])
    assert not session.status()["source_diagnostics"]
    checks.append(dict(packed_vectors="Point v, invalid-value recovery, repair and missing-attribute static fallback"))
    pass_fixture.set_parameters(rop, dict(soppath=box.path(), pixel_aspect=1, crypto_object=1))
    square = render()
    mask = plane(square, "depth")[:, :, 0] < 100
    ys, xs = np.where(mask)
    square_bounds = (np.ptp(xs) + 1, np.ptp(ys) + 1)
    pass_fixture.parm(rop, "pixel_aspect").set(2)
    frame = render()
    mask = plane(frame, "depth")[:, :, 0] < 100
    ys, xs = np.where(mask)
    wide_bounds = (np.ptp(xs) + 1, np.ptp(ys) + 1)
    assert square_bounds[0] == wide_bounds[0] and abs(wide_bounds[1] - square_bounds[1] * 2) <= 2, (
        square_bounds,
        wide_bounds,
    )
    pass_fixture.set_parameters(rop, dict(output_precision="half", output_compression="piz"))
    path = dest / "mixed-half.exr"
    session.write_exr(path, output.options(rop, hou.time()))
    spec, pixels = read(path)
    assert spec.get_float_attribute("PixelAspectRatio") == 2
    assert spec.get_string_attribute("compression") == "piz"
    formats = dict(zip(spec.channelnames, map(str, spec.channelformats)))
    assert formats["R"] == "half" and formats["motion.X"] == "float" and formats["CryptoObject00.R"] == "float", formats
    for name in ("motion", "denoising_albedo", "denoising_normal", "denoising_depth", "CryptoObject00"):
        desc = session.frame_info()["pass_metadata"][name]
        indices = [spec.channelnames.index(name + "." + c) for c in desc["channels"]]
        assert np.array_equal(pixels[:, :, indices], np.flipud(plane(frame, name))), name
    session.wait(session.apply(dict(crop=(16, 12, 64, 48))))
    for cropped in (False, True):
        session.write_exr(path, dict(crop_to_region=cropped))
        spec, _ = read(path)
        assert (spec.width, spec.height) == (64, 48)
        assert (spec.x, spec.y, spec.full_width, spec.full_height) == ((0, 0, 64, 48) if cropped else (16, 36, 128, 96))
    pass_fixture.set_parameters(rop, dict(use_denoise=1, ipr_use_denoise=1, pixel_aspect=1))
    denoised = render()
    assert len(denoised["planes"]) == len(set(denoised["planes"]))
    assert all(np.isfinite(plane(denoised, n)).all() for n in denoised["planes"])
    checks.append(
        dict(
            guides="independent of denoiser",
            exr="mixed precision, lossless data, pixel aspect and both region policies",
        )
    )

# Real SOHO final output uses final percentage, not the IPR override.
pass_fixture.set_parameters(
    rop,
    dict(
        resolution_percent=50,
        ipr_resolution_percent=25,
        pixel_aspect=1,
        pass_motion=0,
        use_denoise=0,
        render_region=1,
        crop_to_region=1,
        output_existing="overwrite",
    ),
)
rop.parmTuple("render_region_bounds").set((0.25, 0.75, 0.25, 0.75))
target = dest / "batch.exr"
pass_fixture.parm(rop, "picture").set(str(target))
rop.render(frame_range=(10, 10))
spec, _ = read(target)
assert (spec.width, spec.height) == (32, 24), (spec.width, spec.height)
before = target.read_bytes()
pass_fixture.parm(rop, "output_existing").set("skip")
rop.render(frame_range=(10, 10))
assert target.read_bytes() == before
pass_fixture.parm(rop, "output_existing").set("error")
try:
    rop.render(frame_range=(10, 10))
except hou.OperationFailed:
    pass
else:
    raise AssertionError("Existing output error policy did not fail")
assert target.read_bytes() == before

# Five final frames preserve vector timing and scaled pixel units.
pass_fixture.set_parameters(
    rop,
    dict(
        render_region=0,
        pass_motion=1,
        output_existing="overwrite",
        output_precision="float",
        geometry_motion="transform",
    ),
)
pass_fixture.parm(geo, "tx").setExpression("($FF-10)*0.1")
pass_fixture.parm(rop, "picture").set(str(dest / "sequence.$F4.exr"))
rop.render(frame_range=(8, 12))
for number in range(8, 13):
    spec, pixels = read(dest / f"sequence.{number:04d}.exr")
    channels = [spec.channelnames.index("motion." + c) for c in "XYZW"]
    actual = np.median(pixels[20:28, 28:36, channels], axis=(0, 1))
    assert np.allclose(actual, [-1.6, 0, -1.6, 0], atol=0.025), (number, actual)
pass_fixture.parm(geo, "tx").deleteAllKeyframes()
pass_fixture.parm(geo, "tx").set(0)
pass_fixture.parm(rop, "pass_motion").set(0)
hou.setFrame(10)
checks.append(dict(sequence="five worker updates and five final frames; vectors remain per-frame pixel displacements"))

# Operate actual Qt controls and observe renderer output, not just parameter values.
from PySide6 import QtWidgets
from cycles_houdini.panel import RenderPanel

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
pass_fixture.set_parameters(rop, dict(render_region=0, ipr_resolution_percent=100, ipr_use_denoise=0, crypto_object=0))
panel = RenderPanel()
panel.resize(1000, 700)
panel.set_rop(rop)
panel.show()
panel.auto.setChecked(False)


def panel_render(start=True):
    if start:
        panel.render()
    entry = panel.entry
    generation = entry.session.status()["generation"]
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        app.processEvents()
        panel.poll()
        info = entry.session.frame_info()
        if info and info["complete"] and info["generation"] == generation and panel._live_generation == generation:
            return info
        assert not entry.error, entry.error
        time.sleep(0.01)
    raise TimeoutError(panel.info.text())


try:
    assert panel_render()["full_width"] == 128
    pass_fixture.parm(rop, "pixel_aspect").set(2)
    panel_render()
    assert panel.canvas.pixel_aspect() == 2
    rect = panel.canvas.image_rect()
    assert abs(rect.width() / rect.height() - 128 * 2 / 96) < 1e-5
    pass_fixture.parm(rop, "pixel_aspect").set(1)
    panel.preview_resolution.setCurrentText("50%")
    panel.change_preview_resolution()
    assert panel_render()["full_width"] == 64
    panel.preview_denoise.click()
    info = panel_render()
    assert info["denoiser"] != "none" and pass_fixture.evaluate(rop, "use_denoise") == 0
    assert panel.show_noisy.isEnabled()
    before = panel.entry.session.status()
    denoise_settings = native_scene.denoising_configuration(rop, hou.time())

    def display_bytes():
        return bytes(panel.canvas.image.constBits())

    beauty = display_bytes()

    def compare_click():
        panel.show_noisy.click()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            app.processEvents()
            panel.poll()
            if not panel._settings_dirty:
                return display_bytes()
            time.sleep(0.01)
        raise TimeoutError("Display-only comparison did not update")

    noisy = compare_click()
    assert noisy != beauty, "Comparison did not switch displayed pixels"
    assert compare_click() == beauty, "Returning to denoised Beauty changed the image"
    after = panel.entry.session.status()
    assert after["generation"] == before["generation"]
    assert after["geometry_conversions"] == before["geometry_conversions"]
    assert native_scene.denoising_configuration(rop, hou.time()) == denoise_settings
    panel.update_render_controls()
    assert panel.refresh_button.isEnabled()
    panel.refresh_button.click()
    panel_render(start=False)
    assert panel.entry.session.status()["generation"] > before["generation"]
    assert not panel.render_button.isEnabled() and panel.stop_button.isEnabled()
    checks.append(
        dict(
            comparison="display pixels differ and restore exactly; no render generation or geometry conversion; ROP settings unchanged",
            refresh="button submits a new update while render mode stays active",
        )
    )
    panel.preview_denoise.click()
    assert panel_render()["denoiser"] == "none"
    pass_fixture.set_parameters(rop, dict(samples=12, ipr_samples=8, resolution_percent=100, use_denoise=1))
    panel.preview_interactive.click()
    info = panel_render()
    assert info["full_width"] == 64 and info["denoiser"] != "none"
    assert native_scene.configuration(rop)[3] == 12
    panel.preview_denoise.click()
    assert panel_render()["denoiser"] == "none"
    assert pass_fixture.evaluate(rop, "use_denoise") == 0 and pass_fixture.evaluate(rop, "ipr_use_denoise") == 0
    panel.preview_interactive.click()
    assert panel_render()["full_width"] == 64
    assert native_scene.configuration(rop)[3] == 8
    app.processEvents()
    panel.grab().save(str(dest / "viewer-controls.png"))
finally:
    panel.dispose()
    panel.close()
    registry.close_all()
checks.append(
    dict(viewer="resolution and denoising controls changed actual renders", batch="percentage, crop, skip and error")
)
report = dict(result="passed", device=device, execution=os.environ.get("CYCLES_HOUDINI_EXECUTION"), checks=checks)
(dest / "houdini-vectors-output-smoke.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
