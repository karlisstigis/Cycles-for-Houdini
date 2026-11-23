# SPDX-License-Identifier: Apache-2.0
"""Viewer canvas and panel use of the scene-linear background composites, headless (offscreen Qt).

Canvas: the QPainter canvas shows the composite of the background colour under each screen
pixel (solid, and both checker colours on their screen cells), keeps the display-space blend
where composites are missing or were made over other backgrounds, and in the A/B wipe.
Panel: a transparent render registers live composites for the current backgrounds, a checker
change reconverts the live frame, snapshots keep their composites within the 64 MiB budget,
and the live JPEG Save image is the frame over black in scene-linear light. No window is shown.
"""

from support.paths import artifacts_root
import os, json, time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import hou

if hou.isUIAvailable():
    raise SystemExit("houdini_viewer_background_composites_smoke.py runs only under hython (offscreen Qt).")
import numpy as np
from PySide6 import QtWidgets, QtGui, QtCore
import cycles_houdini as ch
from cycles_houdini import _native, registry, device_selection
from cycles_houdini.viewer import panel as viewer_panel

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
report = dict(result="running", cases=[])
failures = []


def check(name, condition, detail=None):
    report["cases"].append(dict(name=name, ok=bool(condition), detail=detail))
    if not condition:
        failures.append(name)


def array(image):
    image = image.convertToFormat(QtGui.QImage.Format_RGBA8888)
    data = np.frombuffer(image.constBits(), np.uint8).reshape(image.height(), image.bytesPerLine())
    return data[:, : image.width() * 4].reshape(image.height(), image.width(), 4).copy()


# --- Canvas -----------------------------------------------------------------------------
rng = np.random.default_rng(3)
width, height = 40, 24
pixels = np.zeros((height, width, 4), np.float32)
pixels[..., 3] = rng.choice(np.asarray([0.0, 0.13, 0.5, 1.0], np.float32), (height, width))
pixels[..., :3] = rng.uniform(0, 1.2, (height, width, 3)).astype(np.float32) * pixels[..., 3:4]
pixels[0, 0] = (0.45, 0.13, 0.13, 0.13)
frame = _native.display_frame_fixture(np.ascontiguousarray(pixels), width, height)
canvas = viewer_panel.ImageCanvas()
canvas.resize(200, 120)
canvas.update_checker(QtGui.QColor(48, 48, 48), QtGui.QColor(64, 64, 64))


def show(backgrounds, logical_size=None):
    result = _native.display_image(frame, {"pass": "combined", "channels": "rgba", "backgrounds": backgrounds})
    if logical_size is not None:
        result.update(
            buffer_width=width,
            buffer_height=height,
            width=logical_size[0],
            height=logical_size[1],
            full_width=logical_size[0],
            full_height=logical_size[1],
        )
    straight = viewer_panel.ImageCanvas.merge_frame(None, result)
    composites = [
        viewer_panel.ImageCanvas.merge_frame(None, dict(result, rgba=c), QtCore.Qt.transparent)
        for c in result["composites"]
    ]
    canvas.image = straight
    canvas.full_size = viewer_panel.ImageCanvas.logical_size(straight)
    canvas.composites = {straight.cacheKey(): (tuple(tuple(b) for b in backgrounds), composites)}
    canvas.actual_size()
    return straight, [array(c) for c in composites]


def view():
    grabbed = array(canvas.grab().toImage())
    rect = canvas.image_rect()
    x, y = round(rect.x()), round(rect.y())
    return grabbed, x, y


def display_space(straight, background):
    s = array(straight).astype(np.int64)
    a = s[..., 3:4]
    mul = lambda v, al: ((v * al) + ((v * al) >> 8) + 128) >> 8
    out = mul(s[..., :3], a) + mul(np.asarray(background, np.int64), 255 - a)
    out = np.where(a == 255, s[..., :3], np.where(a == 0, background, out))
    return out


canvas.checker_enabled = False
canvas.background_enabled = True
canvas.background_color = QtGui.QColor(200, 120, 40)
straight, composites = show([(200, 120, 40)])
grabbed, x, y = view()
shown = grabbed[y : y + height, x : x + width, :3]
check(
    "solid: shows the composite",
    np.array_equal(shown, composites[0][..., :3]),
    int(np.abs(shown.astype(int) - composites[0][..., :3]).max()),
)
canvas.background_color = QtGui.QColor(10, 20, 30)  # Composites made over another colour: not used.
canvas.update()
grabbed, x, y = view()
shown = grabbed[y : y + height, x : x + width, :3]
check("stale backgrounds: display-space blend", np.array_equal(shown, display_space(straight, (10, 20, 30))))

canvas.checker_enabled = True
canvas.background_enabled = False
straight, composites = show([(48, 48, 48), (64, 64, 64)])
grabbed, x, y = view()
shown = grabbed[y : y + height, x : x + width, :3]
yy, xx = np.mgrid[y : y + height, x : x + width]
light = (((xx % 16) < 8) == ((yy % 16) < 8))[..., None]
expected = np.where(light, composites[1][..., :3], composites[0][..., :3])
check(
    "checker: composite of the colour under each screen pixel",
    np.array_equal(shown, expected),
    int(np.abs(shown.astype(int) - expected).max()),
)
empty = (pixels[::-1][..., 3] == 0) & (pixels[::-1][..., :3].max(axis=2) == 0)
check(
    "checker: empty pixels show the checker", np.array_equal(shown[empty], np.where(light, 64, 48).repeat(3, 2)[empty])
)
canvas.compare_image = straight.mirrored(True, False)
canvas.composites[canvas.compare_image.cacheKey()] = (
    ((48, 48, 48), (64, 64, 64)),
    [QtGui.QImage(c.mirrored(True, False)) for c in canvas.composites[straight.cacheKey()][1]],
)
canvas.compare_fraction = 0.5
canvas.update()
grabbed, x, y = view()
clip = canvas.compare_geometry()[1]
left = int(round(clip.right())) - x
shown = grabbed[y : y + height, x : x + width, :3]
mirrored = np.where(light, composites[1][:, ::-1, :3], composites[0][:, ::-1, :3])
bad = np.argwhere((shown[:, : left - 4] != mirrored[:, : left - 4]).any(axis=2))  # Split handle: 3 px.
check("compare wipe: composites of the compare image", bad.size == 0, dict(left=left, bad=bad[:8].tolist()))
canvas.compare_image = QtGui.QImage()

logical_width, logical_height = 2 * width, 2 * height
straight, composites = show([(48, 48, 48), (64, 64, 64)], (logical_width, logical_height))
grabbed, x, y = view()
shown = grabbed[y : y + logical_height, x : x + logical_width, :3]
yy, xx = np.mgrid[y : y + logical_height, x : x + logical_width]
light = (((xx % 16) < 8) == ((yy % 16) < 8))[..., None]
dark, bright = (c[..., :3].repeat(2, axis=0).repeat(2, axis=1) for c in composites)
check(
    "checker: compact layers scale to the logical image with a screen-space checker",
    straight.size().toTuple() == (width, height)
    and canvas.full_size == (logical_width, logical_height)
    and np.array_equal(shown, np.where(light, bright, dark)),
)

# --- Panel ------------------------------------------------------------------------------
from cycles_houdini.panel import RenderPanel

geo = hou.node("/obj").createNode("geo", run_init_scripts=False)
sphere = geo.createNode("sphere")
sphere.parm("type").set("polymesh")
sphere.setRenderFlag(True)
sphere.setDisplayFlag(True)
cam = hou.node("/obj").createNode("cam")
cam.parm("tz").set(6)
rop = hou.node("/out").createNode("cycles_ipr")
device_selection.set_devices(rop, "CPU")
rop.setParms(
    dict(
        source_mode="sop",
        soppath=geo.renderNode().path(),
        camera=cam.path(),
        resx=96,
        resy=64,
        samples=2,
        ipr_samples=2,
        ipr_use_denoise=0,
        allowmotionblur=0,
        transparent_background=1,
    )
)
p = RenderPanel()
p.resize(900, 600)
p.set_rop(rop)
p.show()
p.render()


def wait(test, label):
    end = time.monotonic() + 40
    while time.monotonic() < end:
        app.processEvents()
        p.poll()
        if test():
            return True
        time.sleep(0.005)
    check("wait: " + label, False, p.info.text())
    return False


def live_entry():
    return p.canvas.composites.get(p._live_image.cacheKey()) if p._live_image is not None else None


def complete():
    entry = p.entry
    return entry is not None and entry.session.status()["state"] == "complete" and live_entry() is not None


if wait(complete, "render with live composites"):
    entry = live_entry()
    check(
        "panel: live composites over the checker",
        entry[0] == p.canvas.display_backgrounds() and len(entry[1]) == 2,
        entry[0],
    )
    p.checker.setChecked(False)
    wait(lambda: live_entry() is not None and len(live_entry()[1]) == 1, "reconversion after the checker change")
    entry = live_entry()
    check(
        "panel: checker off reconverts for one solid composite",
        entry is not None and entry[0] == p.canvas.display_backgrounds(),
        entry and entry[0],
    )
    image = p.canvas.image
    jpg = p._live_over_black(image.copy())
    settings = dict(p.display_options(), backgrounds=((0, 0, 0),))
    expected = p.entry.session.display_image(**settings)["composites"][0]
    expected = np.frombuffer(expected, np.uint8).reshape(image.height(), image.width(), 4)[..., :3]
    check(
        "panel: live JPEG image is the frame over black in scene-linear light",
        np.array_equal(array(jpg)[..., :3], expected),
    )
    alpha = array(image)[..., 3]
    check(
        "panel: render has partial coverage",
        bool(((alpha > 0) & (alpha < 255)).any()),
        dict(
            alpha_min=int(alpha.min()), alpha_max=int(alpha.max()), transparent=rop.evalParm("transparent_background")
        ),
    )
    count = len(p._snapshots)
    p.snapshot()
    stored = p.canvas.composites.get(p._snapshots[-1].cacheKey())
    check(
        "panel: snapshot keeps its composites",
        len(p._snapshots) == count + 1
        and stored is not None
        and stored[1][0] is not entry[1][0]
        and stored[0] == entry[0],
    )
    for _ in range(10):
        p.snapshot()
    keys = {s.cacheKey() for s in p._snapshots} | {p._live_image.cacheKey()}
    check(
        "panel: registry holds only live and history images", set(p.canvas.composites) <= keys, len(p.canvas.composites)
    )
p.close()
app.processEvents()

report["result"] = "failed" if failures else "passed"
destination = artifacts_root() / "viewer/background-composites"
destination.mkdir(parents=True, exist_ok=True)
(destination / "report.json").write_text(json.dumps(report, indent=1, default=str))
print(
    json.dumps(
        dict(result=report["result"], failures=[c for c in report["cases"] if not c["ok"]], cases=len(report["cases"]))
    )
)
assert not failures, failures
