# SPDX-License-Identifier: Apache-2.0
"""Native camera UI, optics, persistent worker, panel and SOHO depth of field."""

from support.paths import artifacts_root

from cycles_houdini.device_selection import set_devices as set_rop_devices
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import json
import time
from pathlib import Path
import numpy as np
import hou
import cycles_houdini as ch
from cycles_houdini import camera_properties, menu_actions, native_scene, registry

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage3/camera-dof"
destination.mkdir(parents=True, exist_ok=True)
hou.setFrame(1)
obj = hou.node("/obj").createNode("geo", "dof_subject", run_init_scripts=False)
sop = obj.createNode("python", "point_target")
sop.parm("python").set(
    "import hou\ng=hou.pwd().geometry();g.addAttrib(hou.attribType.Point,'pscale',.04);g.createPoint()"
)
camera = hou.node("/obj").createNode("cam", "dof_camera")
camera.setParms(dict(tz=5, focus=5, fstop=0.05))
original_names = {p.name() for p in camera.parms()}
assert menu_actions.add_properties("cameras", [camera])["changed"] == [camera.path()]
assert not camera_properties.install(camera)
assert original_names <= {p.name() for p in camera.parms()}
assert camera.evalParm("cycles_focus_distance") == 5 and camera.evalParm("cycles_fstop") == 0.05
other = hou.node("/obj").createNode("cam", "pinhole_camera")
other.setParms(dict(tz=5, focus=2.5, fstop=0.05))
rop = hou.node("/out").createNode("cycles_ipr", "cycles_dof")
rop.setParms(
    dict(
        source_mode="sop",
        soppath=sop.path(),
        camera=camera.path(),
        resx=128,
        resy=96,
        samples=2048,
        material_kind="emission",
        allowmotionblur=0,
    )
)
rop.parmTuple("basecolor").set((1, 1, 1))
rop.parmTuple("background").set((0, 0, 0))


def capture():
    return native_scene.capture(rop, at_time=0)


def spread(frame):
    a = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).reshape(96, 128, 4)[:, :, 0]
    assert np.isfinite(a).all() and a.sum() > 0
    y, x = np.indices(a.shape)
    total = a.sum()
    cx = (x * a).sum() / total
    cy = (y * a).sum() / total
    return float((((x - cx) ** 2 + (y - cy) ** 2) * a).sum() / total)


assert capture()["camera_dof"]["aperture_size"] == 0
camera.parm("cycles_dof").set(1)
assert abs(capture()["camera_dof"]["aperture_size"] - 0.5) < 1e-7
camera.parm("focalunits").set("cm")
camera.parm("focal").set(5)
camera.parm("aperture").set(4.14214)
assert abs(capture()["camera_dof"]["aperture_size"] - 0.5) < 1e-7
camera.parm("focalunits").set("mm")
camera.parm("focal").set(50)
camera.parm("aperture").set(41.4214)
reports = []
for device in ("CPU", ch.default_device()):
    camera.parm("focus").set(5)
    rop.parm("camera").set(camera.path())
    rop.parm("dof_mode").set("camera")
    with ch.Session(device=device, width=128, height=96, samples=2048, interactive=False) as session:

        def render():
            return spread(session.wait(session.apply(capture(), at_time=0)))

        sharp = render()
        conversions = session.status()["geometry_conversions"]
        camera.parm("focus").set(2.5)
        blur = render()
        assert blur > sharp * 10, (sharp, blur)
        assert session.status()["geometry_conversions"] == conversions
        camera.parm("cycles_aperture_blades").set(5)
        camera.parm("cycles_aperture_ratio").set(2)
        shaped = render()
        assert shaped > sharp * 5
        camera.parm("cycles_aperture_blades").set(0)
        camera.parm("cycles_aperture_ratio").set(1)
        rop.parm("dof_mode").set("off")
        disabled = render()
        assert disabled < blur / 10
        rop.parm("dof_mode").set("camera")
        rop.parm("camera").set(other.path())
        assert render() < blur / 10
        rop.parm("dof_mode").set("on")
        assert render() > sharp * 10
        rop.parm("camera").set(camera.path())
        camera.parm("focus").set(5)
        assert render() < blur / 10
        reports.append(
            dict(
                device=device,
                sharp_variance=sharp,
                defocused_variance=blur,
                polygon_variance=shaped,
                disabled_variance=disabled,
            )
        )

# Exercise the artist's panel controls using one persistent worker and one viewer.
from PySide6 import QtWidgets
from cycles_houdini.panel import RenderPanel

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
set_rop_devices(rop, ch.default_device())
rop.setParms(dict(dof_mode="camera", samples=512))
panel = RenderPanel()
panel.auto.setChecked(False)
panel.set_rop(rop)
panel.show()


def panel_render():
    panel.render()
    entry = panel.entry
    generation = entry.session.status()["generation"]
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        app.processEvents()
        panel.poll()
        info = entry.session.frame_info()
        if (
            info
            and info["complete"]
            and info["generation"] == generation
            and panel._live_generation == generation
            and not panel._settings_dirty
        ):
            break
        time.sleep(0.005)
    else:
        raise TimeoutError(panel.info.text())
    assert not entry.error, panel.info.text()
    return spread(entry.session.frame())


try:
    camera.parm("focus").set(5)
    panel_sharp = panel_render()
    before = panel.entry.session.status()
    camera.parm("focus").set(2.5)
    panel_blur = panel_render()
    assert panel_blur > 10 * panel_sharp
    after = panel.entry.session.status()
    assert (
        after["renderer_pid"] == before["renderer_pid"]
        and after["geometry_conversions"] == before["geometry_conversions"]
    )
finally:
    panel.dispose()
    panel.close()
    registry.close_all()

# Native SOHO final rendering must receive the same camera-specific lens.
import OpenImageIO as OIIO

set_rop_devices(rop, "CPU")
rop.setParms(dict(picture=str(destination / "soho-dof.exr")))
rop.render(frame_range=(1, 1))
image = OIIO.ImageInput.open(str(destination / "soho-dof.exr"))
assert image
spec = image.spec()
a = image.read_image(format=OIIO.FLOAT)
image.close()
channel = spec.channelnames.index("R")
rgba = np.repeat(a[:, :, channel : channel + 1], 4, axis=2).astype(np.float32)
batch = spread(dict(planes={"combined": {"pixels": rgba.tobytes()}}))
assert batch > 10 * panel_sharp
assert menu_actions.remove_properties([camera]) == [camera.path()] and camera.parm("cycles_dof") is None
assert camera.evalParm("focus") == 2.5 and original_names <= {p.name() for p in camera.parms()}
menu_actions.add_properties("cameras", [camera])
camera.parm("cycles_dof").set(1)
set_rop_devices(rop, ch.default_device())
hou.hipFile.save(str(destination / "camera-depth-of-field.hiplc"))
report = dict(
    result="passed",
    devices=reports,
    panel=dict(sharp=panel_sharp, blur=panel_blur),
    soho_variance=batch,
    scope="CPU/OptiX optical defocus, camera selection and ROP override, native spare-tab lifecycle, headless Qt panel and SOHO EXR",
)
(destination / "houdini-camera-dof-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
