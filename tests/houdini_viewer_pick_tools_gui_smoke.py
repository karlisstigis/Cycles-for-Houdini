# SPDX-License-Identifier: Apache-2.0
"""Owned GUI: render viewer pick tools driven by synthesised Qt events, checked by buffers.

Scene: three objects (a box with an OBJ material, a packed sphere whose embedded
assignment overrides its OBJ material, a polygon ball with an OBJ material), two Cycles
materials, a point instancer and a lit background. Checks:
- Ctrl+click selects the object under the pointer (the instancer for an instance,
  the owning object for a packed primitive) and makes it current; Ctrl+double-click
  selects its material; a background click changes nothing.
- Holding the left button shows the pixel values: linear values equal the
  session's float buffer, display values equal the viewer's image, for Beauty
  and the depth pass.
- Clay renders like the same scene with every material replaced by a grey
  material (a second ROP with that material as override), Clay off restores the
  original image, and no scene or ROP parameter differs.
Never saves; no screenshots (values come from image buffers).
"""

import json
import math
import os
import time
import traceback
from pathlib import Path

import hou
import numpy as np
from PySide6 import QtCore, QtTest, QtWidgets
import cycles_houdini as ch
from cycles_houdini import registry, device_selection
from cycles_houdini.panel import RenderPanel
from gui_shutdown import retire_and_exit

app = QtWidgets.QApplication.instance()
report = dict(result="running", checks=[], values={})
path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
WIDTH, HEIGHT = 192, 108
CAMERA_Z = 10.0


def material(name, color):
    builder = ch.create_material(name=name, shader="diffuse_bsdf")
    shader = builder.node("surface_shader")
    shader.parmTuple("color").set(color)
    if shader.parm("roughness") is not None:
        shader.parm("roughness").set(0.0)
    return builder


mat_a = material("pick_red", (0.8, 0.1, 0.1))
mat_b = material("pick_blue", (0.1, 0.2, 0.8))
grey = material("pick_grey", (0.5, 0.5, 0.5))


def geo(name, translate):
    node = hou.node("/obj").createNode("geo", name, run_init_scripts=False)
    node.parmTuple("t").set(translate)
    return node


box_obj = geo("pick_box", (-3.0, -0.5, 0.0))
box = box_obj.createNode("box")
box.parmTuple("size").set((1.6, 1.6, 1.6))
box.setRenderFlag(True)
box.setDisplayFlag(True)
box_obj.parm("shop_materialpath").set(mat_a.path())

packed_obj = geo("pick_packed", (0.0, -0.5, 0.0))
sphere = packed_obj.createNode("sphere")
sphere.parm("type").set("polymesh")
sphere.parmTuple("rad").set((0.95, 0.95, 0.95))
assign = packed_obj.createNode("material")
assign.setInput(0, sphere)
assign.parm("shop_materialpath1").set(mat_b.path())
pack = packed_obj.createNode("pack")
pack.setInput(0, assign)
pack.setRenderFlag(True)
pack.setDisplayFlag(True)
packed_obj.parm("shop_materialpath").set(mat_a.path())  # The embedded assignment wins.

tube_obj = geo("pick_tube", (3.0, -0.5, 0.0))
tube = tube_obj.createNode("sphere")
tube.parm("type").set("polymesh")
tube.parmTuple("rad").set((0.8, 0.8, 0.8))
tube.setRenderFlag(True)
tube.setDisplayFlag(True)
tube_obj.parm("shop_materialpath").set(mat_b.path())

prototype = geo("pick_prototype", (0.0, 0.0, 0.0))
small = prototype.createNode("box")
small.parmTuple("size").set((0.9, 0.9, 0.9))
small.setRenderFlag(True)
small.setDisplayFlag(True)
prototype.setDisplayFlag(False)  # Rendered only through the instancer.
prototype.parm("shop_materialpath").set(mat_a.path())

instancer = hou.node("/obj").createNode("instance", "pick_instancer")
for child in instancer.children():
    child.destroy()
points = instancer.createNode("add")
points.parm("points").set(2)
points.parmTuple("pt0").set((-1.2, 2.2, 0.0))
points.parmTuple("pt1").set((1.2, 2.2, 0.0))
for index in range(2):
    points.parm(f"usept{index}").set(True)
points.setRenderFlag(True)
points.setDisplayFlag(True)
instancer.setParms(dict(instancepath=prototype.path(), ptinstance="fast"))

camera = hou.node("/obj").createNode("cam", "pick_camera")
camera.parm("tz").set(CAMERA_Z)
camera.parm("resx").set(WIDTH)
camera.parm("resy").set(HEIGHT)


def make_rop(name, override=""):
    rop = hou.node("/out").createNode("cycles_ipr", name)
    rop.setParms(
        dict(
            source_mode="objects",
            camera=camera.path(),
            resx=WIDTH,
            resy=HEIGHT,
            viewer_interactive=1,
            resolution_percent=100,
            ipr_resolution_percent=0,
            ipr_samples=16,
            ipr_adaptive_sampling=0,
            ipr_use_denoise=0,
            allowmotionblur=0,
            materialpath=override,
        )
    )
    rop.parmTuple("background").set((0.7, 0.7, 0.7))
    device_selection.set_devices(rop, os.environ.get("CYCLES_HOUDINI_GUI_DEVICE", "CPU"))
    return rop


rop = make_rop("pick_rop")


def project(point):
    """Full-raster pixel (x, y from the bottom) of a world point, for the default 50/41.4214 lens."""
    focal, aperture = camera.evalParm("focal"), camera.evalParm("aperture")
    depth = CAMERA_Z - point[2]
    sx = 0.5 + point[0] / depth * focal / aperture
    sy = 0.5 + point[1] / depth * focal / aperture * WIDTH / HEIGHT
    return int(sx * WIDTH), int(sy * HEIGHT)


PIXELS = dict(
    box=project((-3.0, -0.5, 0.8)),
    packed=project((0.0, -0.5, 0.95)),
    tube=project((3.0, -0.5, 0.8)),
    instance=project((1.2, 2.2, 0.45)),
    background=(3, 3),
)
report["pixels"] = PIXELS


def dump():
    """Every parameter of every scene and ROP node, unexpanded."""
    result = {}
    for root in ("/obj", "/mat", "/out"):
        for node in hou.node(root).allSubChildren():
            values = {}
            for parm in node.parms():
                try:
                    values[parm.name()] = parm.unexpandedString()
                except hou.OperationFailed:
                    values[parm.name()] = repr(parm.eval())
            result[node.path()] = values
    return result


p = RenderPanel()
p.resize(1000, 700)
p.set_rop(rop)
p.show()
p.activateWindow()
began = time.monotonic()


def position(pixel):
    rect = p.canvas.image_rect()
    x, y = pixel
    point = QtCore.QPointF(
        rect.left() + (x + 0.5) / WIDTH * rect.width(), rect.top() + (HEIGHT - 1 - y + 0.5) / HEIGHT * rect.height()
    )
    assert p.canvas.pixel_at(point) == (x, y), (pixel, p.canvas.pixel_at(point))
    return point.toPoint()


def complete():
    if not p.entry:
        return False
    status = p.entry.session.status()
    assert status["state"] != "failed" and not p.entry.error, (status.get("error"), p.entry.error)
    frame = p.entry.session.frame_info()
    return bool(
        status["state"] == "complete"
        and frame
        and frame.get("complete")
        and frame["generation"] == status["generation"]
        and p._live_generation == status["generation"]
        and getattr(p.canvas, "frame_metadata", {}).get("display_epoch") == p._display_epoch
        and not p.canvas.image.isNull()
        and not p._pending
    )


def beauty(name="combined"):
    frame = p.entry.session.frame()
    plane = frame["planes"][name]
    pixels = np.frombuffer(plane["pixels"], np.float32).reshape(frame["height"], frame["width"], plane["channels"])
    return frame, pixels.copy()


def network_current():
    pane = hou.ui.paneTabOfType(hou.paneTabType.NetworkEditor)
    return pane.currentNode() if pane is not None else None


def ctrl_click(pixel, double=False):
    target = position(pixel)
    if double:
        QtTest.QTest.mouseDClick(p.canvas, QtCore.Qt.LeftButton, QtCore.Qt.ControlModifier, target)
    else:
        QtTest.QTest.mouseClick(p.canvas, QtCore.Qt.LeftButton, QtCore.Qt.ControlModifier, target)


def selection():
    return [node.path() for node in hou.selectedNodes()]


def check_selected(node, what):
    report.setdefault("status", []).append([what, p.info.text().splitlines()[0]])
    assert selection() == [node.path()], (what, selection(), p.info.text())
    assert node.isCurrent(), (what, "not current")
    current = network_current()
    assert current is None or current == node, (what, current.path() if current else None)


def readout(pixel, frame, pixels):
    """Expected (values, display) at a pixel; frame planes are bottom-up."""
    x, y = pixel
    values = tuple(float(v) for v in pixels[y - frame["y"], x - frame["x"]])
    color = p.canvas.image.pixelColor(x, HEIGHT - 1 - y)
    return values, (color.red(), color.green(), color.blue(), color.alpha())


def check_readout(pixel, frame, pixels, label):
    values, display = readout(pixel, frame, pixels)
    shown = p.last_readout
    assert (shown["x"], shown["y"]) == pixel, (label, shown, pixel)
    assert shown["values"] is not None and np.allclose(shown["values"], values, rtol=0, atol=0), (label, shown, values)
    assert tuple(shown["display"]) == display, (label, shown["display"], display)
    assert p.info.text().startswith(f"x {pixel[0]}  y {pixel[1]}"), (label, p.info.text())
    report["values"][label] = dict(pixel=pixel, linear=values, display=display, status=p.info.text())


def script():
    # Initial render.
    p.render()
    yield complete
    sessions.append(p.entry.session)
    rendered = PIXELS
    _, original = beauty()
    for name, pixel in rendered.items():
        alpha = original[pixel[1], pixel[0], 3] if original.shape[2] > 3 else 1.0
        report["values"]["coverage_" + name] = float(alpha)
    assert not p.focus_pick.isChecked()

    # 1. Selection. With Pick Focus armed the renderer's own camera hit identifies the
    # pixel (Ctrl+click still selects); on a finished render a host camera ray does.
    hou.clearAllSelected()
    p.focus_pick.setChecked(True)
    yield lambda: p.entry.session.focus_sample(*rendered["packed"]) is not None and p._pick_sample(*rendered["packed"])
    ctrl_click(rendered["packed"])
    yield lambda: p._pending_pick is None
    check_selected(packed_obj, "packed object (renderer hit)")
    assert p.last_pick_route == "renderer", p.last_pick_route
    assert p.focus_pick.isChecked() and p.canvas.focus_enabled
    p.focus_pick.setChecked(False)
    report["checks"].append("With Pick Focus armed, Ctrl+click selects by the renderer's camera hit")
    hou.clearAllSelected()
    for name, node, mat in (
        ("box", box_obj, mat_a),
        ("packed", packed_obj, mat_b),
        ("tube", tube_obj, mat_b),
        ("instance", instancer, mat_a),
    ):
        ctrl_click(rendered[name])
        yield lambda: p._pending_pick is None
        check_selected(node, name + " object")
        ctrl_click(rendered[name], double=True)
        yield lambda: p._pending_pick is None
        check_selected(mat, name + " material")
        assert p.last_pick_route == "ray", p.last_pick_route
        report["checks"].append(f"{name}: Ctrl+click selects {node.path()}, Ctrl+double-click selects {mat.path()}")
    assert p.entry.session.focus_sample(*rendered["box"]) is None, "Picking planes must stay retired"
    report["checks"].append("Picks on a finished render do not request picking planes")
    before = (selection(), [n.path() for n in hou.selectedNodes()], mat_a.isCurrent())
    ctrl_click(rendered["background"])
    yield lambda: p._pending_pick is None
    assert (selection(), [n.path() for n in hou.selectedNodes()], mat_a.isCurrent()) == before, p.info.text()
    report["checks"].append("Ctrl+click on the background changes no selection")

    # 2. Pixel readout, Beauty.
    frame, pixels = beauty()
    QtTest.QTest.mousePress(p.canvas, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, position(rendered["box"]))
    check_readout(rendered["box"], frame, pixels, "beauty_box")
    QtTest.QTest.mouseMove(p.canvas, position(rendered["packed"]))
    check_readout(rendered["packed"], frame, pixels, "beauty_packed")
    QtTest.QTest.mouseRelease(p.canvas, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, position(rendered["tube"]))
    check_readout(rendered["tube"], frame, pixels, "beauty_tube_release")
    assert p._notice is None and p._inspection is None
    report["checks"].append("Holding the left button reads linear Beauty and display values; release clears it")

    # Data pass: depth.
    index = p.pass_menu.findData("depth")
    assert index >= 0, [p.pass_menu.itemData(i) for i in range(p.pass_menu.count())]
    p.pass_menu.setCurrentIndex(index)

    def depth_ready():
        if p._displayed_pass != "depth" or not complete():
            return False
        frame = p.entry.session.frame()
        return "depth" in frame["planes"] and p._sequence == frame["sequence"]

    yield depth_ready
    frame, pixels = beauty("depth")
    QtTest.QTest.mousePress(p.canvas, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, position(rendered["box"]))
    check_readout(rendered["box"], frame, pixels, "depth_box")
    depth = p.last_readout["values"][0]
    assert abs(depth - math.dist((-3.0, -0.5, 0.8), (0, 0, CAMERA_Z))) < 0.5, depth
    QtTest.QTest.mouseRelease(p.canvas, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, position(rendered["box"]))
    report["checks"].append("Depth readout equals the depth buffer (raw values)")
    p.pass_menu.setCurrentIndex(p.pass_menu.findData("combined"))
    yield lambda: p._displayed_pass == "combined" and complete()

    # 3. Clay.
    parameters = dump()
    generation = p.entry.session.status()["generation"]
    p.clay.click()
    assert p.clay.isChecked()
    yield lambda: complete() and p.entry.session.status()["generation"] != generation
    _, clay = beauty()
    assert dump() == parameters, "Clay changed a scene or ROP parameter"
    difference = float(np.abs(clay[..., :3] - original[..., :3]).max())
    assert difference > 0.05, ("Clay did not change the image", difference)
    box_color = clay[rendered["box"][1], rendered["box"][0], :3]
    assert abs(box_color[0] - box_color[2]) < 0.02 and abs(box_color[1] - box_color[2]) < 0.02, box_color
    report["values"]["clay_box"] = [float(v) for v in box_color]
    generation = p.entry.session.status()["generation"]
    p.clay.click()
    assert not p.clay.isChecked()
    yield lambda: complete() and p.entry.session.status()["generation"] != generation
    _, restored = beauty()
    assert dump() == parameters, "Clay off changed a scene or ROP parameter"
    restore_error = float(np.abs(restored - original).max())
    report["values"]["clay_off_max_difference"] = restore_error
    assert restore_error < 0.02, restore_error
    report["checks"].append("Clay off restores the original image; no scene or ROP parameter differs")

    # Reference: the same scene with every material replaced by the grey material.
    reference_rop = make_rop("pick_reference_rop", override=grey.path())
    p.set_rop(reference_rop)
    p.render()
    sessions.append(p.entry.session)
    yield complete
    _, reference = beauty()
    clay_error = np.abs(clay[..., :3] - reference[..., :3])
    report["values"]["clay_vs_reference"] = dict(max=float(clay_error.max()), mean=float(clay_error.mean()))
    assert float(clay_error.mean()) < 0.01 and float(clay_error.max()) < 0.2, report["values"]["clay_vs_reference"]
    report["checks"].append("Clay equals the scene rendered with every material replaced by the grey material")

    # 4. While the render still refines, Ctrl+click requests the picking planes for one
    # frame, selects by the renderer's hit, then retires them again.
    reference_rop.parm("ipr_samples").set(1048576)
    p.render()

    def refining():
        if not p.entry:
            return False
        status = p.entry.session.status()
        return (
            status["state"] == "rendering"
            and p._live_generation == status["generation"]
            and getattr(p.canvas, "frame_metadata", {}).get("display_epoch") == p._display_epoch
            and not p.canvas.image.isNull()
        )

    yield refining
    hou.clearAllSelected()
    ctrl_click(rendered["tube"])
    yield lambda: p._pending_pick is None
    check_selected(tube_obj, "tube object while rendering")
    assert p.last_pick_route == "renderer", p.last_pick_route
    assert not p._focus_transport() and p.entry.session.focus_sample(*rendered["tube"]) is None
    report["checks"].append("While rendering, Ctrl+click uses the renderer's hit and retires the picking planes")
    p.cancel()


steps = script()
waiting = None
sessions = []


def finish(error=None):
    global report
    timer.stop()
    if p.entry and p.entry.session not in sessions:
        sessions.append(p.entry.session)
    report.update(error=error, seconds=time.monotonic() - began)
    report = json.loads(json.dumps(report, default=str))
    retire_and_exit(path, report, sessions, 1 if error else 0, (p.dispose, p.close, registry.close_all))


def tick():
    global waiting
    try:
        if time.monotonic() - began > 200:
            raise TimeoutError("Timed out: " + p.info.text())
        if waiting is not None and not waiting():
            return
        waiting = next(steps)
    except StopIteration:
        finish()
    except BaseException:
        finish(traceback.format_exc())


timer = QtCore.QTimer(hou.qt.mainWindow())
timer.setInterval(25)
timer.timeout.connect(tick)
timer.start()
