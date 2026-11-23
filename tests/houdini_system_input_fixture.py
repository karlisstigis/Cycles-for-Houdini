# SPDX-License-Identifier: Apache-2.0
"""Disposable viewport observed while external system-mouse gestures drive it.

The fixture changes tools only when asked through its control marker. It never
synthesizes input or writes object/camera poses after scene setup. OS-injected
input and Qt paint completion do not establish physical monitor latency.
"""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback

import hou
import numpy as np
from PySide6 import QtCore, QtGui
import cycles_houdini as ch
from cycles_houdini import device_selection, native_scene, registry
from cycles_houdini.panel import ImageCanvas, RenderPanel
from system_input_evidence import validate_records, validate_render_devices, mouse_event_at

destination = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
finish_marker = destination.with_suffix(".finish")
control_marker = destination.with_suffix(".control.json")
min_bursts = int(os.environ.get("CYCLES_HOUDINI_SYSTEM_INPUT_BURSTS", "3"))
min_seconds = float(os.environ.get("CYCLES_HOUDINI_SYSTEM_INPUT_SECONDS", "20"))
max_seconds = float(os.environ.get("CYCLES_HOUDINI_SYSTEM_INPUT_TIMEOUT", "600"))
assert 3 <= min_bursts <= 20 and 20 <= min_seconds < max_seconds <= 1200
bounds = dict(min_bursts=min_bursts, min_seconds=min_seconds, max_seconds=max_seconds)
orbit_mode = os.environ.get("CYCLES_HOUDINI_SYSTEM_INPUT_ORBIT", "viewport")
assert orbit_mode in ("viewport", "camera_handle")
orbit_method = "native camera pivot rotation handle" if orbit_mode == "camera_handle" else "native viewport View tool"
obj = hou.node("/obj").createNode("geo", "gesture_toy", run_init_scripts=False)
source = obj.createNode("testgeometry_rubbertoy")
mesh = obj.createNode("subdivide", "gesture_refined_toy")
mesh.setInput(0, source)
mesh.parm("iterations").set(1)
mesh.setDisplayFlag(True)
mesh.setRenderFlag(True)
geometry = mesh.geometry()
fixture_geometry = dict(
    points=len(geometry.points()),
    primitives=len(geometry.prims()),
    triangle_estimate=sum(max(0, len(primitive.vertices()) - 2) for primitive in geometry.prims()),
)
assert fixture_geometry["triangle_estimate"] >= 10000, fixture_geometry
cam = hou.node("/obj").createNode("cam", "gesture_camera")
cam.parm("tz").set(7)
if orbit_mode == "camera_handle":
    initial_camera_world = tuple(cam.worldTransform().asTuple())
    cam.parm("pz").set(-7)
    assert max(abs(a - b) for a, b in zip(initial_camera_world, cam.worldTransform().asTuple())) < 1e-8
rop = hou.node("/out").createNode("cycles_ipr", "gesture_render")
device = os.environ["CYCLES_HOUDINI_GUI_DEVICE"]
requested_devices = json.loads(device) if device.startswith("[") else [device]
assert len(requested_devices) == 1
if requested_devices[0] != "CPU":
    selected = next(row for row in ch.devices() if row["id"] == requested_devices[0])
    assert selected["backend"] == "OPTIX" and selected["name"] == "NVIDIA GeForce RTX 4090", selected
expected_devices = device_selection.set_devices(rop, requested_devices)
rop.setParms(
    dict(
        source_mode="objects",
        vobject=obj.path(),
        lights="",
        camera=cam.path(),
        resx=640,
        resy=360,
        resolution_percent=100,
        ipr_resolution_percent=100,
        samples=256,
        ipr_samples=256,
        adaptive_sampling=0,
        ipr_adaptive_sampling=0,
        ipr_use_denoise=0,
        use_denoise=0,
        allowmotionblur=0,
        render_threads=2,
        world_source="color",
        use_material_assignments=0,
        material_kind="emission",
        emission_strength=1,
        color_attribute="",
        seed=17,
        animate_seed=0,
    )
)
rop.parmTuple("basecolor").set((0.5, 0.5, 0.5))
rop.parmTuple("background").set((0.075, 0.075, 0.075))
scene = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.SceneViewer, size=(920, 760))
scene.setPwd(hou.node("/obj"))
viewport = scene.curViewport()
viewport.setCamera(cam)
viewport.lockCameraToView(True)
viewport.exportViewToCameraContinuously(True)
obj.setCurrent(True, clear_all_selected=True)


# enterViewState is the documented View tool entry; this SDK names it objview.
# The documented Space+LMB tumble chord is available if plain LMB is remapped.
def enter_tool(tool):
    actor = cam if tool == "orbit" and orbit_mode == "camera_handle" else obj
    actor.setSelected(True, clear_all_selected=True)
    actor.setCurrent(True)
    if tool == "orbit":
        if orbit_mode == "camera_handle":
            scene.enterRotateToolState()
        else:
            scene.enterViewState()
    elif tool == "translate":
        scene.enterTranslateToolState()
    else:
        scene.enterRotateToolState()


enter_tool("orbit")
window = hou.qt.floatingPanelWindow(scene.floatingPanel())
window.setWindowTitle("Cycles System Input Viewport - Orbit ready")
window.setGeometry(20, 70, 920, 760)
window.show()
panel = RenderPanel()
panel.setWindowTitle("Cycles System Input Render")
panel.resize(920, 760)
panel.move(950, 70)
panel.set_rop(rop)
panel.show()
records = []
paints = []
gaps = []
started = previous = time.monotonic()
busy = False
last_save = 0
last_control = None
phase = "orbit"
input_burst = 0
gesture_evidence = None
fresh_reference = None
finish_requested_at = None
last_status = None
final_diagnostics = None
original_paint = type(panel.canvas).paintEvent
mouse_event_path = destination.with_suffix(".mouse.jsonl")
mouse_state_path = destination.with_suffix(".mouse-state.json")
mouse_stop_path = destination.with_suffix(".mouse-stop")
observer_source = Path(__file__).with_name("system_input_mouse_observer.py").read_bytes()
observer_script = destination.parent / "system_input_mouse_observer.py"
observer_script.write_bytes(observer_source)
observer_python = Path(hou.getenv("HFS")) / f"python{sys.version_info.major}{sys.version_info.minor}" / "python.exe"
assert observer_python.is_file(), observer_python
observer_stderr = destination.with_suffix(".observer.stderr.log").open("w", encoding="utf-8")
observer = subprocess.Popen(
    [
        str(observer_python),
        str(observer_script),
        "--events",
        str(mouse_event_path),
        "--state",
        str(mouse_state_path),
        "--stop",
        str(mouse_stop_path),
        "--started",
        repr(started),
        "--timeout",
        str(max_seconds + 10),
        "--parent-pid",
        str(os.getpid()),
    ],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=observer_stderr,
    creationflags=subprocess.CREATE_NO_WINDOW,
)
last_mouse_state = dict(input_burst=0, mouse_buttons=[], cursor=[0, 0])


def mouse_events():
    if not mouse_event_path.exists():
        return []
    lines = mouse_event_path.read_text(encoding="utf-8").splitlines()
    result = []
    for index, line in enumerate(lines):
        try:
            result.append(json.loads(line))
        except json.JSONDecodeError:
            if index != len(lines) - 1:
                raise
    return result


def image_values(image):
    rgba = image.convertToFormat(QtGui.QImage.Format_RGBA8888)
    values = np.frombuffer(rgba.constBits(), np.uint8).reshape(rgba.height(), rgba.bytesPerLine())
    return values[:, : rgba.width() * 4].reshape(rgba.height(), rgba.width(), 4).copy()


def paint(canvas, event):
    original_paint(canvas, event)
    if canvas is panel.canvas and not canvas.image.isNull():
        metadata = dict(getattr(canvas, "frame_metadata", {}))
        if "generation" not in metadata or "sequence" not in metadata:
            return
        pixels = image_values(canvas.image)
        paints.append(
            dict(
                at=time.monotonic() - started,
                metadata=metadata,
                phase=phase,
                input_burst=input_burst,
                stats=dict(
                    finite=bool(np.isfinite(pixels).all()),
                    width=pixels.shape[1],
                    height=pixels.shape[0],
                    rgb_mean=float(pixels[..., :3].mean() / 255),
                    rgb_std=float(pixels[..., :3].std() / 255),
                    alpha_mean=float(pixels[..., 3].mean() / 255),
                ),
            )
        )


type(panel.canvas).paintEvent = paint
panel.render()
timer = QtCore.QTimer(hou.qt.mainWindow())
timer.setInterval(20)


def save(result="running", error=None):
    destination.write_text(
        json.dumps(
            dict(
                result=result,
                error=error,
                scope=__doc__,
                phase=phase,
                tool=scene.currentState(),
                expected_devices=expected_devices,
                orbit_method=orbit_method,
                orbit_mode=orbit_mode,
                navigation_limit=(
                    "Camera-pivot rotation widget qualified; Alt/Space viewport tumble chords are not exercised"
                    if orbit_mode == "camera_handle"
                    else "Only the observed native viewport gestures are qualified"
                ),
                fixture_geometry=fixture_geometry,
                qualification_bounds=bounds,
                material_override=dict(
                    use_material_assignments=False, material_kind="emission", basecolor=[0.5, 0.5, 0.5]
                ),
                gesture_evidence=gesture_evidence,
                fresh_reference=fresh_reference,
                final_status=last_status,
                final_diagnostics=final_diagnostics,
                mouse_events=mouse_events(),
                input_observer=dict(
                    method="Independent stock Python process; passive GetAsyncKeyState/GetCursorPos at 5 ms",
                    pid=observer.pid,
                    exit_code=observer.poll(),
                    python=str(observer_python),
                    script=str(observer_script),
                    script_sha256=hashlib.sha256(observer_source).hexdigest(),
                ),
                elapsed=time.monotonic() - started,
                records=records,
                paints=paints,
                heartbeat_max=max(gaps, default=0),
                heartbeat_samples=len(gaps),
                instructions=dict(
                    tool_control=str(control_marker),
                    finish=str(finish_marker),
                    orbit=(
                        "Camera rotation rings around the world-origin pivot: separate native left drags"
                        if orbit_mode == "camera_handle"
                        else "Active View tool: left drag over geometry; alternatively Space+LMB. Wheel alone does not qualify orbit."
                    ),
                    translate="W handle or control tool=translate, three separate left-button handle drags",
                    rotate="E handle or control tool=rotate, three separate left-button ring drags",
                    bounds="At least three accepted bursts of each family spread over at least 20 seconds",
                ),
            ),
            indent=2,
        ),
        encoding="utf-8",
    )


def cleanup():
    timer.stop()
    type(panel.canvas).paintEvent = original_paint
    if observer.poll() is None:
        mouse_stop_path.touch()
        try:
            observer.wait(timeout=3)
        except subprocess.TimeoutExpired:
            observer.terminate()
            observer.wait(timeout=3)
    if not observer_stderr.closed:
        observer_stderr.close()
    panel.dispose()
    panel.close()
    registry.close_all()


def finish_reference(status):
    global fresh_reference, final_diagnostics
    session = panel.entry.session
    final_diagnostics = session.diagnostics()
    frame = session.frame()
    assert frame and frame["complete"] and frame["generation"] == status["generation"], frame
    actual = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).copy()
    actual_display = image_values(panel.canvas.image)
    assert np.isfinite(actual).all() and actual_display[..., :3].std() > 1, "Final image is blank or nonfinite"
    settings = panel.display_options()
    config = native_scene.configuration(rop)
    update = native_scene.capture(rop)
    session.write_exr(str(destination.with_suffix(".live.exr")))
    panel.canvas.image.save(str(destination.with_suffix(".display.png")))
    # Retire the live worker before the fresh render. Reference rendering is
    # sequential and excluded from the gesture heartbeat measurement.
    cleanup()
    with ch.Session(
        device=config[0],
        width=config[1],
        height=config[2],
        samples=config[3],
        interactive=True,
        execution="sync",
        threads=int(config[6]),
        use_texture_cache=bool(config[4]),
        auto_texture_cache=bool(config[5]),
        denoising=dict(enabled=False),
    ) as reference:
        expected = reference.wait(reference.apply(update), timeout=180)
        target = np.frombuffer(expected["planes"]["combined"]["pixels"], np.float32)
        assert actual.shape == target.shape and np.isfinite(target).all()
        linear_mean = float(np.abs(actual - target).mean())
        assert linear_mean < 0.002, ("Final live image differs from fresh full scene capture", linear_mean)
        target_display = image_values(ImageCanvas.merge_frame(None, reference.display_image(**settings)))
        assert actual_display.shape == target_display.shape
        display_mean = float(np.abs(actual_display.astype(float) - target_display.astype(float)).mean())
        assert display_mean < 1.0, ("Painted canvas differs from fresh display conversion", display_mean)
        reference.write_exr(str(destination.with_suffix(".reference.exr")))
        fresh_reference = dict(
            result="passed",
            linear_mean_absolute_error=linear_mean,
            display_byte_mean_absolute_error=display_mean,
            live_generation=status["generation"],
            painted_generation=paints[-1]["metadata"]["generation"],
            scope="Final live linear frame and actual painted canvas versus fresh full capture at identical final poses; same renderer",
        )
    gesture_evidence["image_appearance_certified"] = True


def tick():
    global previous, last_save, busy, gesture_evidence, input_burst, last_mouse_state
    global last_control, phase, finish_requested_at, last_status, final_diagnostics
    if busy:
        return
    busy = True
    try:
        now = time.monotonic()
        gaps.append(now - previous)
        previous = now
        assert observer.poll() is None, "Passive mouse observer stopped unexpectedly; see observer stderr log"
        buttons = last_mouse_state["mouse_buttons"]
        input_burst = last_mouse_state["input_burst"]
        if control_marker.exists():
            text = control_marker.read_text(encoding="utf-8-sig")
            if text != last_control:
                control = json.loads(text)
                tool = control["tool"]
                assert tool in ("translate", "rotate", "orbit")
                enter_tool(tool)
                phase = tool
                last_control = text
                window.setWindowTitle("Cycles System Input Viewport - " + tool + " ready")
        if panel.entry:
            entry = panel.entry
            status = entry.session.status()
            info = entry.session.frame_info()
            last_status = status
            assert not status["error"] and not entry.error, (status, entry.error)
            validate_render_devices(status, expected_devices)
            record = dict(
                at=now - started,
                object_t=list(obj.parmTuple("t").eval()),
                object_r=list(obj.parmTuple("r").eval()),
                camera_t=list(cam.parmTuple("t").eval()),
                camera_r=list(cam.parmTuple("r").eval()),
                tool=scene.currentState(),
                phase=phase,
                object_world=list(obj.worldTransform().asTuple()),
                camera_world=list(cam.worldTransform().asTuple()),
                view_transform=list(viewport.viewTransform().asTuple()),
                generation=status["generation"],
                conversions=status["geometry_conversions"],
                worker=status["renderer_pid"],
                state=status["state"],
                render_revision=status.get("render_revision"),
                render_device_ids=status.get("render_device_ids", []),
                input_burst=input_burst,
                mouse_buttons=buttons,
                cursor=last_mouse_state["cursor"],
                painted_metadata=dict(getattr(panel.canvas, "frame_metadata", {})),
                current_sample=status.get("current_sample"),
                frame_info=info,
                change_serial=entry.change_serial,
                applied_serial=entry.applied_serial,
            )
            sampled_at = time.monotonic() - started
            canonical = mouse_event_at(mouse_events(), sampled_at)
            if canonical is not None:
                last_mouse_state = canonical
                input_burst = canonical["input_burst"]
                record.update(
                    input_burst=input_burst,
                    mouse_buttons=canonical["mouse_buttons"],
                    cursor=canonical["cursor"],
                    mouse_observer_at=canonical["at"],
                )
            record.update(at=sampled_at, pose_observation_window=[now - started, sampled_at])
            if (
                not records
                or now - started - records[-1]["at"] >= 0.1
                or any(
                    record[key] != records[-1][key]
                    for key in (
                        "object_t",
                        "object_r",
                        "camera_t",
                        "camera_r",
                        "tool",
                        "generation",
                        "conversions",
                        "worker",
                        "state",
                        "mouse_buttons",
                        "painted_metadata",
                    )
                )
            ):
                records.append(record)
            if finish_marker.exists():
                if finish_requested_at is None:
                    finish_requested_at = now
                metadata = record["painted_metadata"]
                settled = (
                    status["state"] == "complete"
                    and info
                    and info["complete"]
                    and entry.change_serial == entry.applied_serial
                    and not status.get("capture_pending")
                    and not status.get("pending_update")
                    and metadata.get("generation") == status["generation"]
                    and metadata.get("render_revision") == status.get("render_revision")
                    and paints
                    and paints[-1]["metadata"] == metadata
                )
                if settled:
                    validate_render_devices(status, expected_devices, require_committed=True)
                    gesture_evidence = validate_records(
                        records, paints, orbit_method=orbit_method, mouse_events=mouse_events(), **bounds
                    )
                    finish_reference(status)
                    save("passed")
                    hou.ui.postEventCallback(lambda: hou.exit(0, suppress_save_prompt=True))
                    return
                assert now - finish_requested_at < 90, "Final pose did not reach a completed painted image"
        if now - started > max_seconds:
            raise TimeoutError("System input fixture timed out")
        if now - last_save > 2:
            save()
            last_save = now
    except BaseException:
        error = traceback.format_exc()
        if panel.entry:
            try:
                final_diagnostics = panel.entry.session.diagnostics()
            except BaseException:
                final_diagnostics = dict(error=traceback.format_exc())
        cleanup()
        save("failed", error)
        hou.ui.postEventCallback(lambda: hou.exit(1, suppress_save_prompt=True))
    finally:
        busy = False


timer.timeout.connect(tick)
timer.start()
save()
