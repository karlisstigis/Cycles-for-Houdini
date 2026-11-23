# SPDX-License-Identifier: Apache-2.0
"""Owned Houdini batch jobs for final renders; independent of the IPR worker.

Only scene snapshotting runs in the artist process. Cooking, translation,
Cycles synchronization and ordered frame output run in the matching hython.
Jobs retain a private HIP and reports so closing the viewer/artist process
does not discard either the render or the evidence needed to diagnose it.
"""

from __future__ import annotations


import ctypes
import atexit
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
import uuid

import hou

from .background_jobs import (
    Job,
    JobRegistry,
    TERMINAL as _terminal,
    read_json as _read,
    write_json as _write,
    process_identity as _process_identity,
)

_registry = JobRegistry()
atexit.register(_registry.shutdown)


def _root():
    custom = hou.getenv("CYCLES_HOUDINI_BACKGROUND_ROOT")
    return (Path(custom) if custom else Path(hou.getenv("HIP")) / "cycles_background").resolve()


def _environment():
    # hou.putenv updates the native process environment, which os.environ may
    # not reflect. Inherit it without mutating global environment or CWD.
    if os.name != "nt":
        return dict(os.environ)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetEnvironmentStringsW.restype = ctypes.c_void_p
    kernel.FreeEnvironmentStringsW.argtypes = [ctypes.c_void_p]
    block = kernel.GetEnvironmentStringsW()
    if not block:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        result, offset = {}, 0
        while True:
            entry = ctypes.wstring_at(block + offset)
            if not entry:
                break
            name, value = entry.split("=", 2)[1:] if entry.startswith("=") else entry.split("=", 1)
            if entry.startswith("="):
                name = "=" + name  # Windows drive-current-directory entries.
            result[name] = value
            offset += len(entry.encode("utf-16-le")) + 2
        return result
    finally:
        kernel.FreeEnvironmentStringsW(block)


def _save_copy(path):
    # mwrite -n preserves the artist's scene name, HIP/HIPNAME and dirty state.
    quoted = path.as_posix().replace("$", "\\$").replace("`", "\\`").replace('"', '\\"')
    _, error = hou.hscript('mwrite -n "' + quoted + '"')
    if error.strip() or not path.is_file():
        raise RuntimeError("Unable to snapshot the scene for background rendering: " + error.strip())


def _color_snapshot():
    import PyOpenColorIO as ocio
    from .. import color_policy

    current = ocio.GetCurrentConfig()
    context = current.getCurrentContext()
    path = Path(hou.Color.ocio_configPath()).resolve(strict=True)
    if not path.is_file():
        raise RuntimeError("Houdini OCIO configuration is not a readable file: " + str(path))
    return dict(path=str(path), context=dict(context.getStringVars()), policy=color_policy())


def get_job(directory):
    return _registry.get(directory)


def last_job(rop):
    directory = rop.cachedUserData("cycles_houdini.background_job_directory")
    if not directory:
        identifier = rop.cachedUserData("cycles_houdini.background_job")
        if not identifier:
            return None
        directory = _root() / identifier
    return get_job(directory)


def active_jobs():
    return _registry.active_jobs()


def jobs(limit=50, before=None):
    return _registry.page(_root(), limit, before).jobs


def launch(rop, *, viewer=False, _pause_token=None):
    """Snapshot the current scene, return immediately after starting hython."""
    from .. import _native

    if not isinstance(rop, hou.RopNode) or rop.type().name() != "cycles_ipr":
        raise ValueError("Choose a Cycles render ROP")
    host = Path(_native.__file__).resolve().parent.parent
    release_manifest = host.parent / "release-manifest.json"
    release = _read(release_manifest) if release_manifest.is_file() else {}
    if Path(__file__).resolve().parents[3] != host:
        raise RuntimeError(
            "Python and native Cycles hosts differ; restart Houdini before launching a background render"
        )
    executable = Path(hou.getenv("HFS")) / "bin" / ("hython.exe" if os.name == "nt" else "hython")
    if not executable.is_file():
        raise RuntimeError("The matching Houdini batch executable is unavailable: " + str(executable))
    directory = _root() / (time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex[:12])
    directory.mkdir(parents=True)
    status = dict(
        schema=1,
        id=directory.name,
        state="preparing",
        rop=rop.path(),
        created=time.time(),
        completed_frames=0,
        skipped_frames=0,
        error="",
        scene=hou.hipFile.path(),
    )
    _write(directory / "status.json", status)
    process = None
    from ..ipr import registry

    pause_token = _pause_token
    if pause_token is None and hou.isUIAvailable():
        pause_token = registry.begin_local_final()
    try:
        scene = directory / ("scene" + Path(hou.hipFile.path()).suffix)
        if scene.suffix not in (".hip", ".hiplc", ".hipnc"):
            scene = directory / "scene.hip"
        if scene.suffix == ".hip":
            category = str(hou.licenseCategory())
            if "Indie" in category:
                scene = scene.with_suffix(".hiplc")
            elif "Apprentice" in category:
                scene = scene.with_suffix(".hipnc")
        context = {name: hou.getenv(name) for name in ("HIP", "HIPNAME", "JOB")}
        spec = dict(
            schema=1,
            id=directory.name,
            host=str(host),
            houdini=list(hou.applicationVersion()),
            release_id=release.get("package_id"),
            native_sha256=hashlib.sha256(Path(_native.__file__).read_bytes()).hexdigest(),
            snapshot=str(scene),
            original_scene=hou.hipFile.path(),
            rop=rop.path(),
            frame=hou.frame(),
            take=hou.takes.currentTake().name(),
            environment=context,
            cwd=os.getcwd(),
            color=_color_snapshot(),
        )
        if viewer:
            spec["frame_range"] = (
                list(rop.parmTuple("f").eval()) if rop.evalParm("trange") else [hou.frame(), hou.frame(), 1]
            )
            spec["viewer"] = dict(
                address=r"\\.\pipe\CyclesFinal-" + uuid.uuid4().hex,
                key=os.urandom(32).hex(),
                parent_pid=os.getpid(),
                parent_identity=_process_identity(os.getpid()),
            )
        _save_copy(scene)
        _write(directory / "job.json", spec)
        # Preferences packages precede HOUDINI_PACKAGE_DIR. Skip the mutable
        # product registrations in this child only, then load a distinct pin.
        # Other artist packages, preferences and HDAs retain their normal paths.
        package = directory / "packages"
        package.mkdir(parents=True)
        target = ".".join(str(value) for value in hou.applicationVersion())
        filename = "cycles_houdini_" + target.replace(".", "_") + ".json"
        pinned_env = [
            {"CYCLES_HOUDINI_HOST_ROOT": host.as_posix()},
            {"CYCLES_HOUDINI_RESOURCES": (host / "resources").as_posix()},
            {"CYCLES_HOUDINI_PACKAGE_FILE": filename},
        ]
        if release:
            pinned_env += [
                {"CYCLES_HOUDINI_RELEASE_ID": release["package_id"]},
                {"PATH": dict(method="prepend", value=["$HFS/bin", (host.parent / "runtime/bin").as_posix()])},
            ]
        pin = dict(
            enable=f"houdini_version == '{target}'", load_package_once=True, hpath=[host.as_posix()], env=pinned_env
        )
        _write(package / ("cycles_houdini_job_" + directory.name + ".json"), pin)
        entry = directory / "render.py"
        entry.write_text(
            "from cycles_houdini.rop.background_render import run_job\nimport sys\nsys.exit(run_job(sys.argv[1]))\n",
            encoding="utf-8",
        )
        environment = _environment()
        for name, value in spec["color"]["context"].items():
            environment[name] = value
        original_packages = environment.get("HOUDINI_PACKAGE_DIR", "")
        package_paths = [str(package)]
        if original_packages:
            package_paths.append(original_packages)
        skiplist = [value for value in environment.get("HOUDINI_PACKAGE_SKIPLIST", "").split(",") if value]
        skiplist += [filename, "cycles_houdini_dev.json"]
        environment.update(
            HOUDINI_PACKAGE_SKIPLIST=",".join(dict.fromkeys(skiplist)),
            HOUDINI_PACKAGE_DIR=os.pathsep.join(package_paths),
            OCIO=spec["color"]["path"],
        )
        status.update(state="starting", snapshot_seconds=time.time() - status["created"])
        _write(directory / "status.json", status)
        with (directory / "render.log").open("wb") as log:
            process = subprocess.Popen(
                [str(executable), str(entry), str(directory / "job.json")],
                cwd=spec["cwd"],
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        job = _registry.adopt(directory, process)
        if pause_token is not None:
            registry.track_local_final(pause_token, job)
        if viewer:
            from ..ipr.worker_scheduling import prioritize_interaction

            _write(directory / "viewer-process.json", dict(gpu_scheduling=prioritize_interaction(process)))
        _write(
            directory / "process.json",
            dict(pid=process.pid, executable=str(executable), identity=_process_identity(process.pid)),
        )
        rop.setCachedUserData("cycles_houdini.background_job", job.id)
        rop.setCachedUserData("cycles_houdini.background_job_directory", str(directory))
        if hou.isUIAvailable():
            hou.ui.setStatusMessage("Cycles background render started — " + rop.path())
        return job
    except BaseException as error:
        if pause_token is not None:
            registry.end_local_final(pause_token)
        if process is not None and process.poll() is None:
            process.terminate()
        status.update(state="failed", error=str(error), finished=time.time())
        _write(directory / "status.json", status)
        raise


def run_job(filename):
    """Batch entry point. All HOM and render callbacks stay on its main thread."""
    from .. import _native, color_policy, synchronize_color_configuration
    from . import final_render

    path = Path(filename)
    directory = path.parent
    status = _read(directory / "status.json")
    started, published = time.monotonic(), 0.0
    frame_started = None
    rop = None
    display = None

    def stopping():
        return (directory / "stop").is_file()

    def publish():
        nonlocal frame_started
        status["elapsed_seconds"] = time.monotonic() - started
        if frame_started is not None:
            status["frame_elapsed_seconds"] = time.monotonic() - frame_started
            if status["state"] in _terminal:
                frame_started = None
        status["frame_started_monotonic"] = frame_started
        _write(directory / "status.json", status)

    def notice_stop(message):
        status.update(state="stopping", message=message)
        status.setdefault("stop_observed_wall_ns", time.time_ns())
        status.setdefault("stop_requested_wall_ns", (directory / "stop").stat().st_mtime_ns)
        publish()

    def observe(event, value):
        nonlocal published, frame_started
        if event == "session" and display is not None:
            # Release a retained previous engine before the next scene upload,
            # while the artist process retains its last displayed image.
            display.bind(value)
        elif event == "progress":
            session, metadata = value
            if display is not None:
                display.bind(session)
                if display.error:
                    raise RuntimeError("Final display failed: " + display.error)
            if stopping():
                notice_stop("Stopping renderer")
                cancel_started = time.monotonic()
                try:
                    session.cancel()
                finally:
                    status["cancel_seconds"] = time.monotonic() - cancel_started
                    publish()
                raise final_render.RenderCancelled("Background render stopped")
            now = time.monotonic()
            if now - published >= 0.25:
                status.update(
                    progress=metadata["progress"],
                    message=metadata.get("message", "Rendering"),
                    generation=metadata["generation"],
                )
                publish()
                published = now
        elif event == "output-validation" and value == rop and stopping():
            notice_stop("Stopping while checking existing output")
            raise final_render.RenderCancelled("Background render stopped")
        elif event == "sequence":
            node, summary = value
            if node != rop:
                return
            status.update(
                completed_frames=summary["completed_frames"],
                skipped_frames=summary["skipped_frames"],
                created_sessions=summary["created_sessions"],
                last_frame=summary["last_frame"],
            )
            if summary["reason"] in ("frame-written", "frame-skipped"):
                status["frame_elapsed_seconds"] = (
                    time.monotonic() - frame_started
                    if summary["reason"] == "frame-written" and frame_started is not None
                    else 0.0
                )
                frame_started = None
                if summary["reason"] == "frame-written":
                    status["last_frame_seconds"] = status["frame_elapsed_seconds"]
                if display is not None and summary["reason"] == "frame-written":
                    display.capture()
                status["last_picture"] = node.parm("picture").evalAtFrame(summary["last_frame"])
                with (directory / "frames.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(
                        json.dumps(
                            dict(
                                frame=summary["last_frame"],
                                reason=summary["reason"],
                                picture=node.parm("picture").evalAtFrame(summary["last_frame"]),
                                status=summary["last_status"],
                                outputs=summary.get("last_outputs", []),
                                frame_seconds=status["frame_elapsed_seconds"],
                                previous_frame_seconds=status.get("previous_frame_seconds"),
                            )
                        )
                        + "\n"
                    )
            publish()

    def before_frame(node, event_type, at_time):
        nonlocal frame_started
        if event_type == hou.ropRenderEventType.PreFrame:
            if stopping():
                notice_stop("Stopping before the next frame")
                final_render.request_cancel(node)
                raise final_render.RenderCancelled("Background render stopped")
            frame_started = time.monotonic()
            status.update(
                frame=hou.timeToFrame(at_time),
                frame_index=status.get("frame_index", 0) + 1,
                progress=0.0,
                message="Cooking and capturing scene",
                frame_elapsed_seconds=0.0,
                previous_frame_seconds=status.get("last_frame_seconds"),
            )
            publish()

    try:
        spec = _read(path)
        if spec.get("viewer"):
            from .final_stream import Server

            display = Server(directory, spec["viewer"])
        if spec["schema"] != 1 or spec["id"] != directory.name:
            raise RuntimeError("Invalid background job descriptor")
        if list(hou.applicationVersion()) != spec["houdini"]:
            raise RuntimeError("Background Houdini version differs from the artist session")
        if (
            Path(_native.__file__).resolve().parent.parent != Path(spec["host"]).resolve()
            or hashlib.sha256(Path(_native.__file__).read_bytes()).hexdigest() != spec["native_sha256"]
        ):
            raise RuntimeError("Background Cycles host differs from the artist session")
        expected_host = Path(spec["host"]).resolve()
        if (
            Path(__file__).resolve().parents[3] != expected_host
            or Path(hou.getenv("CYCLES_HOUDINI_RESOURCES")).resolve() != expected_host / "resources"
        ):
            raise RuntimeError("Background Python/resources differ from the artist session")
        if spec.get("release_id") and hou.getenv("CYCLES_HOUDINI_RELEASE_ID") != spec["release_id"]:
            raise RuntimeError("Background release identity differs from the artist session")
        status.update(state="loading", pid=os.getpid(), host=spec["host"])
        publish()
        hou.hipFile.load(spec["snapshot"], suppress_save_prompt=True)
        # The private filename must not redirect $HIP/$HIPNAME output or asset
        # paths. Changing the in-memory name does not save/overwrite the artist HIP.
        hou.hipFile.setName(spec["original_scene"])
        for name, value in spec["environment"].items():
            if value is not None:
                hou.putenv(name, value)
        for name, value in spec["color"]["context"].items():
            hou.putenv(name, value)
        hou.putenv("OCIO", spec["color"]["path"])
        hou.Color.reloadOCIO()
        synchronize_color_configuration()
        policy = color_policy()
        for name in ("working_space", "output_interop_id"):
            if policy[name] != spec["color"]["policy"][name]:
                raise RuntimeError("Background color configuration differs: " + name)
        hou.setFrame(spec["frame"])
        if hou.takes.currentTake().name() != spec["take"]:
            raise RuntimeError("Background take differs from the artist session")
        rop = hou.node(spec["rop"])
        if not isinstance(rop, hou.RopNode) or rop.type().name() != "cycles_ipr":
            raise RuntimeError("Background Cycles ROP is missing")
        rop.parm("soho_foreground").set(1)
        # This process already owns redirected stdout/stderr in render.log.
        # Keep the SOHO license header and diagnostics in that job's log.
        rop.parm("soho_outputmode").set(2)
        final_render.register(rop)
        rop.addRenderEventCallback(before_frame, run_before_script=True)
        status.update(
            state="rendering",
            color=policy,
            scene=hou.hipFile.path(),
            context={name: hou.getenv(name) for name in ("HIP", "HIPNAME", "JOB")},
        )
        publish()
        from contextlib import ExitStack

        frame_range = spec.get("frame_range") or (
            rop.parmTuple("f").eval() if rop.evalParm("trange") else (hou.frame(), hou.frame(), 1)
        )
        start, end, increment = frame_range
        if not all(math.isfinite(value) for value in frame_range) or increment <= 0 or end < start:
            raise ValueError("Invalid final render frame range")
        status.update(frame_range=list(frame_range), total_frames=math.floor((end - start) / increment + 1e-7) + 1)
        publish()
        with ExitStack() as stack:
            stack.enter_context(final_render.observe(observe))
            if display is not None:
                stack.enter_context(final_render.retain_for_viewer(rop, display.retain))
            if stopping():
                raise final_render.RenderCancelled("Background render stopped")
            rop.render(frame_range=frame_range)
        summary = rop.cachedUserData(final_render._summary_key)
        if not summary or summary["error"] or not (summary["completed_frames"] + summary["skipped_frames"]):
            raise RuntimeError("Background render did not complete its output: " + str(summary))
        status.update(state="completed", progress=1.0, summary=summary, finished=time.time(), exit_code=0)
        publish()
        if display is not None and display.session is not None:
            display.wait()
        return 0
    except BaseException as error:
        final_render.close_all()
        cancelled = stopping()
        status.update(
            state="cancelled" if cancelled else "failed",
            error=str(error),
            traceback=traceback.format_exc(),
            finished=time.time(),
            exit_code=2 if cancelled else 1,
            summary=rop.cachedUserData(final_render._summary_key) if rop is not None else None,
        )
        publish()
        traceback.print_exc()
        if display is not None and display.session is not None:
            display.wait()
        return status["exit_code"]
    finally:
        if display is not None:
            display.close()


def _job_label(value):
    scene = Path(value.get("scene", "")).stem
    source = (scene + " / " if scene else "") + value["rop"]
    try:
        when = time.strftime("%d %b %H:%M", time.localtime(value["created"])) if value["created"] else "Unknown time"
    except (ValueError, OSError, OverflowError):
        when = "Unknown time"
    progress = ""
    if value.get("frame") is not None:
        frame = f"{value['frame']:.3f}".rstrip("0").rstrip(".")
        progress = " — Frame " + frame
    return f"{value['state'].title()} — {source}{progress} — {value['completed_frames']} written — {when}"


def show_jobs():
    """Open a live, non-modal list with direct cancellation controls."""
    from . import background_panel

    return background_panel.show()
