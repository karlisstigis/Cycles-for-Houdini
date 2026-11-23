# SPDX-License-Identifier: Apache-2.0
"""Fresh-process native ownership, concurrent close and worker mapping probes."""

import argparse
import gc
import hashlib
import json
import mmap
from pathlib import Path
import threading
import time

import cycles_houdini as ch

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--case",
    choices=(
        "frame-close",
        "concurrent-close",
        "worker-mapping",
        "display-close",
        "display-replace",
        "display-ordering",
        "transaction-close",
        "capture-retire-reader",
    ),
    required=True,
)
parser.add_argument("--device", default="CPU", help="CPU or an explicit OptiX device ID")
parser.add_argument("--report", type=Path, required=True)
args = parser.parse_args()
args.report.parent.mkdir(parents=True, exist_ok=True)
native = ch._native


def digest(value):
    return hashlib.sha256(value).hexdigest()


def render(width=64, height=48):
    session = ch.Session(
        device=args.device,
        width=width,
        height=height,
        samples=1,
        interactive=False,
        threads=4,
        passes=["combined"],
        execution="sync",
    )
    session.wait(session.calibration(), timeout=90)
    assert session.status()["render_device_ids"] == [args.device]
    return session


def closed(session):
    status = session.status()
    assert status["device_allocation_bytes"] == status["owned_geometry_bytes"] == 0, status
    return status


def release_thread(values, expected):
    errors = []

    def consume():
        try:
            assert digest(values[0]) == expected
            values.clear()
            gc.collect()
        except BaseException as error:
            errors.append(repr(error))

    thread = threading.Thread(target=consume, daemon=True)
    thread.start()
    thread.join(10)
    assert not thread.is_alive() and not errors and not values, errors


def frame_close():
    session = render()
    owned = session.display_frame()[0]
    plan = native.display_gpu_input(owned, {}, "", False)
    view = plan["pixels"]
    assert view.readonly and type(view.obj).__name__ == "FramePixels"
    expected = digest(view)
    session.close()
    status = closed(session)
    del plan, owned, session
    gc.collect()
    assert digest(view) == expected
    values = [view]
    del view
    replacement = render()
    replacement.close()
    closed(replacement)
    release_thread(values, expected)
    return dict(sha256=expected, closed=status, held_before_close=True, off_thread_release=True, recovery=True)


def concurrent_close():
    session = render(1536, 864)
    filename = args.report.parent / "concurrent.exr"
    errors, times = [], {}
    finished = threading.Event()

    def write():
        times["call_started"] = time.perf_counter()
        try:
            native.write_exr(session._handle, str(filename), {"compression": "zip"})
        except BaseException as error:
            errors.append(repr(error))
        finally:
            times["call_finished"] = time.perf_counter()
            finished.set()

    thread = threading.Thread(target=write, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    staged = []
    while time.monotonic() < deadline and not finished.is_set():
        staged = list(args.report.parent.glob(".cycles-*.tmp.exr"))
        if staged:
            break
        time.sleep(0.0005)
    assert staged and not finished.is_set(), "Did not observe native output in progress"
    times["close_started"] = time.perf_counter()
    session.close()
    times["close_finished"] = time.perf_counter()
    thread.join(30)
    assert not thread.is_alive() and not errors, errors
    assert times["call_started"] < times["close_started"] < times["call_finished"], times
    assert filename.is_file() and filename.stat().st_size > 0
    status = closed(session)
    assert not list(args.report.parent.glob(".cycles-*.tmp.exr"))
    replacement = render()
    replacement.close()
    closed(replacement)
    return dict(
        times=times, observed_staged_output=True, output_bytes=filename.stat().st_size, closed=status, recovery=True
    )


def display_lifetime(replace):
    session = render()
    image = session.display_frame()[0]
    pixels = native.display_gpu_input(image, {}, "", False)["pixels"]
    expected = digest(pixels)
    native.open_display(session._handle, str(args.report.parent / "display-before.exr"), "")
    entered, release = threading.Event(), threading.Event()
    errors = []

    def present():
        try:
            native._test_present_frame_rendezvous(session._handle, image, entered, release)
        except BaseException as error:
            errors.append(repr(error))

    thread = threading.Thread(target=present, daemon=True)
    thread.start()
    try:
        assert entered.wait(10), "Presenter did not acquire its display"
        if replace:
            native.open_display(session._handle, str(args.report.parent / "display-after.exr"), "")
        else:
            native.close_display(session._handle)
        assert thread.is_alive(), "Presenter left rendezvous before release"
    finally:
        release.set()
    thread.join(20)
    assert not thread.is_alive() and not errors, errors
    native.close_display(session._handle)
    session.close()
    status = closed(session)
    assert digest(pixels) == expected
    replacement = render()
    replacement.close()
    closed(replacement)
    return dict(
        admitted_presenter=True,
        operation="replace" if replace else "close",
        retained_sha256=expected,
        closed=status,
        recovery=True,
    )


def display_ordering():
    import hou
    import numpy as np
    import OpenImageIO as oiio
    import PyOpenColorIO as ocio

    session = render()
    oversized = None
    original = ocio.GetCurrentConfig()
    original_env = hou.getenv("OCIO")
    old = session.display_frame()[0]
    old_info = native.frame_info(old)
    errors = []
    entered, release = threading.Event(), threading.Event()

    def open_sink(name):
        path = args.report.parent / (name + ".exr")
        native.open_display(session._handle, str(path), "")
        return path

    def finish(path):
        native._test_wait_display(session._handle)
        native.close_display(session._handle)
        image = oiio.ImageInput.open(str(path))
        assert image, oiio.geterror()
        channels = list(image.spec().channelnames)
        pixels = image.read_image(format=oiio.FLOAT)
        assert pixels is not None
        rgba = pixels[:, :, [channels.index(c) for c in ("R", "G", "B", "A")]]
        subimage = 1
        while image.seek_subimage(subimage, 0):
            channels.extend(image.spec().channelnames)
            subimage += 1
        image.close()
        return channels, rgba

    def has_aov(channels):
        return any("ordering_probe" in name for name in channels)

    def late_presenter():
        try:
            native._test_present_frame_rendezvous(session._handle, old, entered, release)
        except BaseException as error:
            errors.append(repr(error))

    thread = None
    try:
        path = open_sink("ordering-generation")
        thread = threading.Thread(target=late_presenter, daemon=True)
        thread.start()
        assert entered.wait(10), "Old-layout presenter did not pin its display"
        session.wait(session.apply(dict(aovs=[dict(name="ordering_probe", type="value")])), timeout=90)
        new = session.display_frame()[0]
        rewrapped = native.owned_frame(session._handle)
        new_info = native.frame_info(new)
        assert new_info["generation"] > old_info["generation"]
        assert new_info["sequence"] > old_info["sequence"]
        native.present_frame(session._handle, new)
        native._test_wait_display(session._handle)
        release.set()
        thread.join(20)
        assert not thread.is_alive() and not errors, errors
        generation_channels, reference = finish(path)
        assert has_aov(generation_channels), "Late pre-change presenter removed the current AOV layout"

        # Caller serials can span restarted backends. Their order takes
        # precedence over the source frame's generation and raw sequence.
        path = open_sink("ordering-caller-serial")
        native.present_frame(session._handle, new, 100)
        native._test_wait_display(session._handle)
        native.present_frame(session._handle, old, 200)
        native._test_wait_display(session._handle)
        for _ in range(50):
            native.present_frame(session._handle, new, 100)
            native.present_frame(session._handle, old, 200)
        caller_channels, _ = finish(path)
        assert not has_aov(caller_channels), "Stale caller serial overrode the newer presentation"

        # An invalid newer layout must not destroy the valid device or advance
        # its high-water mark. A following stale frame must still be ignored.
        oversized = render(65, 48)
        path = open_sink("ordering-invalid-layout")
        native.present_frame(session._handle, new, 200)
        native._test_wait_display(session._handle)
        try:
            native.present_frame(session._handle, oversized.display_frame()[0], 300)
        except (ValueError, RuntimeError) as error:
            assert "dimensions" in str(error), str(error)
        else:
            raise AssertionError("Oversized differing-layout frame was admitted")
        native.present_frame(session._handle, old, 100)
        invalid_channels, _ = finish(path)
        assert has_aov(invalid_channels), "Invalid frame damaged the current display layout"

        # A color-policy change permits recoloring the same serial, but never
        # admits older/equal-serial frames with a different pass layout.
        config = ocio.Config.CreateFromStream(original.serialize())
        config.setWorkingDir(original.getWorkingDir())
        target_space = next(
            name
            for name in ("ACEScg", "ACES2065-1")
            if config.getColorSpace(name) and name != new_info["working_space"]
        )
        config.setRole(ocio.ROLE_SCENE_LINEAR, target_space)
        config_path = args.report.parent / "ordering-color.ocio"
        config_path.write_text(config.serialize(), encoding="utf-8")

        stale_path = open_sink("ordering-stale-recolor")
        native.present_frame(session._handle, new, 200)
        before = native._test_wait_display(session._handle)
        hou.putenv("OCIO", str(config_path))
        hou.Color.reloadOCIO()
        ch.synchronize_color_configuration()
        assert ch.color_policy()["render_policy_id"] != new_info["render_color_policy"]
        native.present_frame(session._handle, new, 100)
        native.present_frame(session._handle, old, 100)
        native.present_frame(session._handle, old, 200)
        assert native._test_wait_display(session._handle) == before, "Stale recolor wrote a frame"
        stale_channels, unchanged = finish(stale_path)
        assert has_aov(stale_channels), "Stale recolor replaced the current layout"
        np.testing.assert_array_equal(unchanged, reference)

        # Queue the same retained image at its original serial before and
        # after another target color change, using a fresh capsule wrapper.
        hou.putenv("OCIO", original_env or "")
        hou.Color.reloadOCIO()
        ocio.SetCurrentConfig(original)
        ch.synchronize_color_configuration()
        path = open_sink("ordering-same-frame-recolor")
        native.present_frame(session._handle, new, 200)
        before = native._test_wait_display(session._handle)
        hou.putenv("OCIO", str(config_path))
        hou.Color.reloadOCIO()
        ch.synchronize_color_configuration()
        native.present_frame(session._handle, rewrapped, 200)
        assert native._test_wait_display(session._handle) == before + 1, "Same-serial recolor was skipped"
        recolor_channels, recolored = finish(path)
        assert has_aov(recolor_channels)
        # Native EXR output converts the transmitted working pixels to its
        # lin_rec709 file space. Independently compose both transforms.
        image = oiio.ImageInput.open(str(path))
        output_space = image.spec().get_string_attribute("oiio:ColorSpace")
        image.close()
        assert original.getColorSpace(output_space), output_space
        expected = reference.copy()
        rgb = np.ascontiguousarray(expected[:, :, :3])
        alpha = expected[:, :, 3]
        rgb[alpha != 0] /= alpha[alpha != 0, None]
        original.getProcessor(new_info["working_space"], target_space).getDefaultCPUProcessor().applyRGB(rgb)
        assert not np.allclose(rgb, reference[:, :, :3], atol=1e-5), "Recolor negative control has no effect"
        config.getProcessor(target_space, output_space).getDefaultCPUProcessor().applyRGB(rgb)
        rgb[alpha != 0] *= alpha[alpha != 0, None]
        expected[:, :, :3] = rgb
        np.testing.assert_allclose(recolored, expected, atol=5e-4, rtol=5e-4)
    finally:
        release.set()
        if thread is not None:
            thread.join(20)
        native.close_display(session._handle)
        session.close()
        if oversized is not None:
            oversized.close()
        hou.putenv("OCIO", original_env or "")
        hou.Color.reloadOCIO()
        ocio.SetCurrentConfig(original)
        ch.synchronize_color_configuration()
    replacement = render()
    replacement.close()
    closed(replacement)
    return dict(
        same_session_generation=dict(old=old_info["generation"], new=new_info["generation"]),
        generation_channels=generation_channels,
        caller_serial_channels=caller_channels,
        repeated_stale_presentations=50,
        invalid_dimensions_preserved_layout=True,
        stale_recolor_rejected=True,
        same_frame_recolor=True,
        closed=closed(session),
        recovery=True,
    )


def transaction_close():
    import hou

    handle = native.create(args.device, 64, 48, 1, False, False, True, False, None, True, 4, ["combined"])
    job = native.prepare_apply(handle, {}, hou.time(), False)
    assert job is not False, "Empty capture did not prepare"
    try:
        try:
            native.close(handle)
        except RuntimeError as error:
            assert "transaction" in str(error), str(error)
        else:
            raise AssertionError("Close admitted during a prepared transaction")
    finally:
        native.discard_apply(job)
    native.close(handle)
    native.close(handle)
    status = native.status(handle)
    assert status["device_allocation_bytes"] == status["owned_geometry_bytes"] == 0, status
    replacement = render()
    replacement.close()
    closed(replacement)
    return dict(rejected_during_apply=True, repeated_close=True, closed=status, recovery=True)


def capture_retire_reader():
    handle = native.create(args.device, 64, 48, 1, False, False, True, False, None, True, 4, ["combined"])
    desired = native.fixture(handle)
    assert desired is not None
    entered, release = threading.Event(), threading.Event()
    errors = []

    def retire():
        try:
            native._test_retire_capture_rendezvous(handle, entered, release)
        except BaseException as error:
            errors.append(repr(error))

    thread = threading.Thread(target=retire, daemon=True)
    thread.start()
    try:
        assert entered.wait(10), "Capture retirement did not reach rendezvous"
        before = native.status(handle)
        stats = native.native_stats(handle)
        assert before["extractions"] >= 0 and isinstance(stats["points"], int)
        assert isinstance(native.native_object_keys(handle), list)
        assert isinstance(native.native_material_paths(handle), list)
        assert isinstance(native.native_stylesheet_resources(handle), list)
        assert isinstance(native.native_material_references(handle), list)
        assert isinstance(native.cop_capture_stats(handle), dict)
        assert native.cop_image_sources(handle) is None
        assert thread.is_alive(), "Retirement left rendezvous before release"
    finally:
        release.set()
    thread.join(20)
    assert not thread.is_alive() and not errors, errors
    after = native.status(handle)
    assert after["extractions"] == before["extractions"]
    native.close(handle)
    native.close(handle)
    replacement = render()
    replacement.close()
    closed(replacement)
    return dict(reader_during_retirement=True, repeated_close=True, closed=after, recovery=True)


def worker_mapping():
    from cycles_houdini.queued import ExternalBackend, _HEADER, _ACK

    capture = native.create(args.device, 64, 48, 1, False, False, True, False, None, True, 4, ["combined"])
    desired = native.fixture(capture)
    backend = ExternalBackend(capture, desired)
    target = view = None
    try:
        backend.apply(desired, None)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            backend.request += 1
            request = backend.request
            backend.process.stdin.write(_HEADER.pack(b"CHWP", 1, 3, request, 0, 0))
            backend.process.stdin.flush()
            magic, version, command, reply_id, size, length = _HEADER.unpack(backend.read_exact(_HEADER.size))
            assert (magic, version, command, reply_id) == (b"CHWP", 1, 0x8003, request)
            name = backend.read_exact(length).decode("utf-8")
            assert name.startswith("Local\\CyclesHoudiniReply-" + str(backend.process.pid) + "-")
            target = mmap.mmap(-1, size, tagname=name, access=mmap.ACCESS_READ)
            reply = native.decode_reply(target, desired)
            assert reply["success"], reply
            if reply["frame"] is not None:
                break
            backend.process.stdin.write(_HEADER.pack(b"CHWP", 1, _ACK, request, 0, 0))
            backend.process.stdin.flush()
            target.close()
            target = None
            time.sleep(0.01)
        assert target is not None and reply["frame"] is not None
        view = memoryview(target)
        packet_hash = digest(view)
        owned = reply["frame"]
        pixels = native.display_gpu_input(owned, {}, "", False)["pixels"]
        pixel_hash = digest(pixels)
        pid = backend.process.pid
        backend.process.kill()
        backend.process.wait(timeout=5)
        assert digest(view) == packet_hash
        decoded_after_exit = native.decode_reply(target, desired)
        assert decoded_after_exit["frame"] is not None
        view.release()
        view = None
        target.close()
        target = None
        backend.abort()
        del reply, decoded_after_exit, owned
        native.close(capture)
        gc.collect()
        assert digest(pixels) == pixel_hash
        retained = [pixels]
        del pixels
        release_thread(retained, pixel_hash)
        replacement = ExternalBackend(capture, desired)
        try:
            replacement.apply(desired, None)
            assert replacement.process.pid != pid
        finally:
            status = replacement.close()
        assert status["device_allocation_bytes"] == status["owned_geometry_bytes"] == 0, status
        return dict(
            worker_pid=pid,
            packet_sha256=packet_hash,
            frame_sha256=pixel_hash,
            held_until_worker_exit=True,
            decoded_after_exit=True,
            recovery=True,
            closed=status,
        )
    finally:
        if view is not None:
            view.release()
        if target is not None:
            target.close()
        backend.abort()
        native.close(capture)


started = time.perf_counter()
try:
    evidence = {
        "frame-close": frame_close,
        "concurrent-close": concurrent_close,
        "worker-mapping": worker_mapping,
        "display-close": lambda: display_lifetime(False),
        "display-replace": lambda: display_lifetime(True),
        "display-ordering": display_ordering,
        "transaction-close": transaction_close,
        "capture-retire-reader": capture_retire_reader,
    }[args.case]()
    report = dict(result="passed", case=args.case, device=args.device, evidence=evidence)
except BaseException:
    import traceback

    report = dict(result="failed", case=args.case, device=args.device, error=traceback.format_exc())
    raise
finally:
    report["wall_seconds"] = time.perf_counter() - started
    args.report.write_text(json.dumps(report, indent=2))
    print(json.dumps(report))
