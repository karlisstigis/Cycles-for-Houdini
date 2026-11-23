# SPDX-License-Identifier: Apache-2.0
"""Real same-Houdini release activation during accumulating Image Iris output.

Run with ordinary Python through the native verification queue. All installations,
preferences, scenes, child logs and evidence belong to a new artifact directory.
The two archives must be different, valid payloads for the exact same host SDK.
"""

from __future__ import annotations

from support.paths import artifacts_root

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback


ROOT = Path(__file__).resolve().parents[1]
ABORT_PATH = None


def write(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def until(predicate, *, seconds=120, process=None):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if ABORT_PATH is not None and ABORT_PATH.is_file():
            raise RuntimeError("Upgrade driver aborted; retiring owned batch jobs")
        value = predicate()
        if value:
            return value
        if process is not None and process.poll() is not None:
            raise RuntimeError("Child exited before evidence was available; inspect its log")
        time.sleep(0.05)
    raise TimeoutError("Timed out waiting for native upgrade evidence")


def runtime(host, release):
    import hou
    import cycles_houdini as ch
    from cycles_houdini import image_iris

    host = host.resolve()
    paths = dict(python=ch.__file__, native=ch._native.__file__, resources=hou.getenv("CYCLES_HOUDINI_RESOURCES"))
    for name, path in paths.items():
        assert Path(path).resolve().is_relative_to(host), (name, path, host)
    assert Path(paths["resources"]).resolve() == host / "resources"
    assert Path(hou.getenv("CYCLES_HOUDINI_HOST_ROOT")).resolve() == host
    assert hou.getenv("CYCLES_HOUDINI_RELEASE_ID") == release
    assert hou.applicationVersionString() == read(host.parent / "release-manifest.json")["houdini"]
    assert ch.capabilities()["image_iris"] and image_iris.supported()
    return dict(
        **paths,
        host=str(host),
        release=release,
        pid=os.getpid(),
        houdini=hou.applicationVersionString(),
        image_iris=True,
    )


def isolated_preferences(directory):
    import hou

    version = ".".join(str(part) for part in hou.applicationVersion()[:2])
    expected = directory / "preferences" / ("houdini" + version)
    assert Path(hou.getenv("HOUDINI_USER_PREF_DIR")).resolve() == expected, "Houdini must use isolated test preferences"


def iris_camera(directory):
    import hou
    import numpy as np
    import OpenImageIO as oiio
    from cycles_houdini import camera_properties

    mask = directory / "aperture.exr"
    y, x = np.mgrid[-1:1:33j, -1:1:33j]
    pixels = np.ascontiguousarray(((x * x + y * y < 0.9) * (1 + 0.3 * x)).astype(np.float32))
    out = oiio.ImageOutput.create(str(mask))
    assert out.open(str(mask), oiio.ImageSpec(33, 33, 1, oiio.FLOAT))
    assert out.write_image(pixels[:, :, None]) and out.close()
    camera = hou.node("/obj").createNode("cam", "upgrade_iris_camera")
    camera_properties.install(camera)
    camera.setParms(
        dict(tz=5, focus=2.5, fstop=0.05, cycles_dof=1, cycles_image_iris=1, cycles_image_iris_file=mask.as_posix())
    )
    return camera


def fresh(args):
    import hou
    import numpy as np

    isolated_preferences(args.directory)
    import cycles_houdini as ch
    from cycles_houdini import camera_properties

    identity = runtime(args.host, args.release)
    camera = iris_camera(args.directory)
    rop = hou.node("/out").createNode("cycles_ipr", "fresh_release_probe")
    assert Path(rop.type().definition().libraryFilePath()).resolve().is_relative_to(args.host)
    identity["hda"] = rop.type().definition().libraryFilePath()
    rop.setParms(dict(camera=camera.path(), dof_mode="camera"))
    checks = []
    with ch.Session(
        device="CPU", width=32, height=32, samples=32, threads=2, execution="worker", denoising={"enabled": False}
    ) as session:
        for method in ("cdf", "hierarchical"):
            camera.parm("cycles_image_iris_method").set(method)
            lens = camera_properties.configuration(rop, hou.time())
            frame = session.wait(
                session.apply(
                    dict(
                        geometry=[],
                        camera_path=camera.path(),
                        camera_dof=lens,
                        background=(0.2, 0.4, 0.8),
                        motion={"enabled": False},
                    )
                ),
                timeout=90,
            )
            status = session.status()
            assert not status["error"] and status["render_device_ids"] == ["CPU"], status
            assert status["image_iris_preparations"] == 1 and status["image_iris_table_bytes"] > 0, status
            worker = Path(session._controller.backend.process.args[0]).resolve()
            assert worker == args.host / "bin/ch_worker.exe", worker
            plane = frame["planes"]["combined"]
            pixels = np.frombuffer(plane["pixels"], np.float32).reshape(32, 32, plane["channels"])
            error = float(np.max(np.abs(pixels[:, :, :3] - np.array((0.2, 0.4, 0.8)))))
            assert np.isfinite(pixels).all() and error < 1e-5, error
            checks.append(
                dict(
                    method=method,
                    worker=str(worker),
                    preparations=status["image_iris_preparations"],
                    table_bytes=status["image_iris_table_bytes"],
                    maximum_energy_error=error,
                )
            )
    write(args.directory / "fresh.json", dict(result="passed", identity=identity, checks=checks))


def parent(args):
    import hou
    import numpy as np
    import OpenImageIO as oiio

    isolated_preferences(args.directory)
    import cycles_houdini as ch
    from cycles_houdini import background_render as bg, final_stream
    from cycles_houdini.device_selection import set_devices

    identity = runtime(args.host, args.release)
    report = dict(result="running", identity=identity)
    owned = []
    remote = None
    try:
        camera = iris_camera(args.directory)
        geo = hou.node("/obj").createNode("geo", "upgrade_subject", run_init_scripts=False)
        grid = geo.createNode("grid")
        grid.setParms(dict(rows=2, cols=2, orient=0))
        probe = geo.createNode("python", "child_identity")
        probe.setInput(0, grid)
        # This runs inside the actual batch child's source cook, before sampling.
        script = """import json,os,hou
from pathlib import Path
import cycles_houdini as ch
actual = dict(python=ch.__file__,native=ch._native.__file__,
    resources=hou.getenv('CYCLES_HOUDINI_RESOURCES'),release=hou.getenv('CYCLES_HOUDINI_RELEASE_ID'),
    pid=os.getpid(),image_iris=ch.capabilities()['image_iris'])
Path(REPORT).write_text(json.dumps(actual,indent=2))
""".replace("REPORT", repr((args.directory / "batch-identity.json").as_posix()))
        probe.parm("python").set(
            "import os\nif os.getpid() != "
            + str(os.getpid())
            + ":\n"
            + "\n".join("    " + line for line in script.splitlines())
        )
        probe.setRenderFlag(True)
        rop = hou.node("/out").createNode("cycles_ipr", "upgrade_background")
        set_devices(rop, "CPU")
        identity["hda"] = rop.type().definition().libraryFilePath()
        assert Path(identity["hda"]).resolve().is_relative_to(args.host)
        rop.setParms(
            dict(
                source_mode="sop",
                soppath=probe.path(),
                camera=camera.path(),
                dof_mode="camera",
                resx=96,
                resy=54,
                samples=1048576,
                time_limit=args.duration,
                adaptive_sampling=0,
                use_denoise=0,
                viewer_interactive=0,
                render_threads=2,
                allowmotionblur=0,
                material_kind="emission",
                use_material_assignments=0,
                picture=(args.directory / "surviving.exr").as_posix(),
            )
        )
        rop.parmTuple("basecolor").set((0.2, 0.4, 0.8))
        rop.parmTuple("background").set((0.2, 0.4, 0.8))
        hou.hipFile.save((args.directory / "artist.hiplc").as_posix(), save_to_recent_files=False)
        hou.putenv("CYCLES_HOUDINI_BACKGROUND_ROOT", str(args.directory / "jobs"))
        job = bg.launch(rop, viewer=True)
        owned.append(job)
        remote = final_stream.RemoteSession(job, read(job.directory / "job.json")["viewer"])

        def accumulating(after=None):
            status = job.status()
            assert status["state"] not in bg._terminal, status
            native = remote.status()
            if native.get("current_sample", 0) <= 0:
                return None
            if status["state"] != "rendering" or not 0 < status.get("progress", 0) < 1:
                return None
            if after and (
                native["current_sample"] <= after["native"]["current_sample"]
                or status["progress"] <= after["status"]["progress"]
            ):
                return None
            return dict(
                status=status,
                native={
                    key: native.get(key)
                    for key in (
                        "current_sample",
                        "generation",
                        "state",
                        "render_device_ids",
                        "image_iris_preparations",
                        "image_iris_table_bytes",
                    )
                },
                wall_ns=time.time_ns(),
                pid=job.pid,
                job=str(job.directory),
            )

        before = until(accumulating, seconds=90)
        batch = read(args.directory / "batch-identity.json")
        assert batch["pid"] == job.pid and batch["release"] == args.release and batch["image_iris"]
        for key in ("python", "native", "resources"):
            assert Path(batch[key]).resolve().is_relative_to(args.host), batch
        report.update(before=before, batch_identity=batch)
        write(args.directory / "ready.json", before)
        # The driver activates a second valid payload and renders it in a fresh
        # process before releasing this check. The first process keeps sampling.
        until(lambda: (args.directory / "upgraded.json").is_file(), seconds=120)
        after = until(lambda: accumulating(before), seconds=40)
        assert after["pid"] == before["pid"] and after["status"]["frame"] == before["status"]["frame"]
        assert after["native"]["generation"] == before["native"]["generation"]
        report["after"] = after
        status = until(
            lambda: (value if (value := job.status())["state"] in bg._terminal else None), seconds=args.duration + 60
        )
        assert status["state"] == "completed" and status["completed_frames"] == 1, status
        records = [json.loads(line) for line in (job.directory / "frames.jsonl").read_text().splitlines()]
        assert len(records) == 1 and records[0]["reason"] == "frame-written", records
        assert records[0]["status"]["image_iris_preparations"] == 1, records[0]
        image = oiio.ImageInput.open(str(args.directory / "surviving.exr"))
        assert image
        pixels = np.asarray(image.read_image(format=oiio.FLOAT), np.float32)
        image.close()
        assert np.isfinite(pixels).all() and float(pixels[:, :, :3].max()) > 0.01
        energy_error = float(np.max(np.abs(pixels[:, :, :3] - np.array((0.2, 0.4, 0.8)))))
        assert energy_error < 0.02, energy_error
        report.update(
            completed=status,
            output=dict(
                path=str(args.directory / "surviving.exr"),
                sha256=hashlib.sha256((args.directory / "surviving.exr").read_bytes()).hexdigest(),
                shape=list(pixels.shape),
                maximum=float(pixels.max()),
                maximum_energy_error=energy_error,
            ),
            frames=records,
            progressive_pixels_observed=False,
            accumulation_evidence="Native current_sample and progress advance within one pinned batch PID/frame/generation; output pixels checked after completion",
        )
        remote.close()
        job.wait(timeout=15)
        remote = None

        # The existing A process must still launch its own pinned renderer after
        # registration now selects B. Exercise Stop during real accumulation.
        rop.setParms(dict(picture=(args.directory / "cancelled.exr").as_posix(), time_limit=60))
        stopped = bg.launch(rop)
        owned.append(stopped)
        active = until(
            lambda: (s if (s := stopped.status())["state"] == "rendering" and 0 < s.get("progress", 0) < 1 else None),
            seconds=90,
        )
        began = time.monotonic()
        stopped.stop()
        terminal = until(lambda: (s if (s := stopped.status())["state"] in bg._terminal else None), seconds=5)
        stopped.wait(timeout=10)
        latency = time.monotonic() - began
        assert terminal["state"] == "cancelled" and terminal["completed_frames"] == 0, terminal
        assert not terminal.get("forced") and not (args.directory / "cancelled.exr").exists(), terminal
        assert terminal["cancel_seconds"] < 2 and latency < 5, terminal
        assert read(stopped.directory / "job.json")["host"] == str(args.host)
        report.update(
            result="passed", stop=dict(active=active, terminal=terminal, seconds=latency, job=str(stopped.directory))
        )
    except BaseException:
        report.update(result="failed", error=traceback.format_exc())
        raise
    finally:
        cleanup_errors = []
        if remote is not None:
            try:
                remote.close()
            except Exception:
                cleanup_errors.append(traceback.format_exc())
        for job in owned:
            try:
                if job.is_running():
                    job.terminate()
                    job.wait(timeout=15)
            except Exception:
                cleanup_errors.append(traceback.format_exc())
        if cleanup_errors:
            report.update(result="failed", cleanup_errors=cleanup_errors)
        write(args.directory / "parent.json", report)


def driver(args):
    spec = importlib.util.spec_from_file_location("upgrade_release_manager", ROOT / "scripts/release-package.py")
    manager = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(manager)
    first, second = [manager.verify_archive(path) for path in (args.archive_a, args.archive_b)]
    assert first["houdini"] == second["houdini"], "Both payloads must target the same exact Houdini version"
    assert first["package_id"] != second["package_id"], "The test requires two different valid payloads"
    assert first["runtime_fingerprint"] != second["runtime_fingerprint"], (
        "Runtime payloads must differ, beyond provenance"
    )
    assert args.duration >= 30, "Allow activation and a fresh probe to overlap accumulation"
    assert args.directory.is_relative_to(artifacts_root()), "Use an isolated artifacts directory"
    args.directory.mkdir(parents=True, exist_ok=False)
    report = dict(
        result="running", scope=__doc__, houdini=first["houdini"], archives=[str(args.archive_a), str(args.archive_b)]
    )
    process = None
    try:
        install_root, data = [args.directory / value for value in ("installation", "data")]
        preference_pattern = args.directory / "preferences/houdini__HVER__"
        prefs = args.directory / "preferences" / ("houdini" + ".".join(first["houdini"].split(".")[:2]))
        receipt_a = manager.install(args.archive_a, install_root, prefs, data, allow_internal=True)
        report["install_a"] = receipt_a
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("CYCLES_HOUDINI_")
            and key
            not in (
                "HOUDINI_PACKAGE_DIR",
                "HOUDINI_PACKAGE_SKIPLIST",
                "HOUDINI_PATH",
                "HOUDINI_OTLSCAN_PATH",
                "PYTHONPATH",
                "PYTHONHOME",
                "OCIO",
            )
        }
        packages = args.directory / "extra-packages"
        packages.mkdir()
        environment.update(
            HOUDINI_USER_PREF_DIR=str(preference_pattern),
            HOUDINI_PACKAGE_DIR=str(packages),
            HOUDINI_NO_ENV_FILE="1",
            HOUDINI_DSO_ERROR="2",
            HFS=str(args.houdini_root),
        )
        environment["PATH"] = str(args.houdini_root / "bin") + os.pathsep + environment.get("PATH", "")
        executable = args.houdini_root / "bin/hython.exe"
        assert executable.is_file(), executable

        def command(phase, receipt):
            return [
                str(executable),
                str(Path(__file__).resolve()),
                "--phase",
                phase,
                "--directory",
                str(args.directory),
                "--host",
                receipt["host"],
                "--release",
                receipt["package_id"],
                "--duration",
                str(args.duration),
            ]

        with (args.directory / "parent.log").open("wb") as log:
            process = subprocess.Popen(
                command("parent", receipt_a),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            before = until(
                lambda: read(args.directory / "ready.json") if (args.directory / "ready.json").is_file() else None,
                seconds=120,
                process=process,
            )

            def live_status():
                value = read(Path(before["job"]) / "status.json")
                assert value["state"] == "rendering" and 0 < value.get("progress", 0) < 1, value
                assert value["pid"] == before["pid"] and value["frame"] == before["status"]["frame"], value
                return dict(status=value, wall_ns=time.time_ns())

            # Install B through the real transactional manager while A is in the
            # middle of a live frame. Never rewrite a registration by hand.
            report["pre_activation"] = live_status()
            began = time.time_ns()
            receipt_b = manager.install(args.archive_b, install_root, prefs, data, allow_internal=True)
            report.update(install_b=receipt_b, activation_started_ns=began, activation_finished_ns=time.time_ns())
            report["post_activation"] = live_status()
            state = read(install_root / "state.json")
            assert state["active"] == receipt_b["package_id"]
            assert Path(receipt_a["host"]).is_dir(), "Activation discarded the running release"
            with (args.directory / "fresh.log").open("wb") as fresh_log:
                subprocess.run(
                    command("fresh", receipt_b),
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=fresh_log,
                    stderr=subprocess.STDOUT,
                    timeout=120,
                    check=True,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            fresh_report = read(args.directory / "fresh.json")
            assert fresh_report["identity"]["release"] == receipt_b["package_id"]
            report["post_fresh"] = live_status()
            report["fresh"] = fresh_report
            write(
                args.directory / "upgraded.json", dict(fresh=fresh_report, activated=receipt_b, wall_ns=time.time_ns())
            )
            process.wait(timeout=args.duration + 150)
            assert process.returncode == 0, "The first release failed; inspect parent.json and parent.log"
            parent_report = read(args.directory / "parent.json")
            assert parent_report["result"] == "passed", parent_report
            assert (
                before["wall_ns"]
                < report["activation_started_ns"]
                < report["activation_finished_ns"]
                < parent_report["after"]["wall_ns"]
            )
            report.update(
                result="passed",
                parent=parent_report,
                retained_release_ids=sorted(path.name for path in (install_root / "versions").iterdir()),
            )
    except BaseException:
        report.update(result="failed", error=traceback.format_exc())
        raise
    finally:
        try:
            if process is not None and process.poll() is None:
                write(args.directory / "abort.json", dict(reason=report.get("error", "Driver cleanup")))
                try:
                    # Let parent.finally release its owned renderer and pipe handles.
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    report["forced_parent_tree_cleanup"] = True
                    if os.name == "nt":
                        subprocess.run(
                            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=15,
                            check=False,
                            creationflags=subprocess.CREATE_NO_WINDOW,
                        )
                    else:
                        process.terminate()
                    process.wait(timeout=15)
        except Exception:
            report.update(result="failed", cleanup_error=traceback.format_exc())
        write(args.directory / "report.json", report)
    print(json.dumps(dict(result=report["result"], report=str(args.directory / "report.json"))))


def main():
    global ABORT_PATH
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("driver", "parent", "fresh"), default="driver")
    parser.add_argument("--directory", type=lambda p: Path(p).resolve(), required=True)
    parser.add_argument("--archive-a", type=lambda p: Path(p).resolve())
    parser.add_argument("--archive-b", type=lambda p: Path(p).resolve())
    parser.add_argument("--houdini-root", type=lambda p: Path(p).resolve())
    parser.add_argument("--host", type=lambda p: Path(p).resolve())
    parser.add_argument("--release")
    parser.add_argument("--duration", type=float, default=60)
    args = parser.parse_args()
    if args.phase == "parent":
        ABORT_PATH = args.directory / "abort.json"
    if args.phase == "driver" and not all((args.archive_a, args.archive_b, args.houdini_root)):
        parser.error("--archive-a, --archive-b and --houdini-root are required for the driver")
    if args.phase != "driver" and not all((args.host, args.release)):
        parser.error("--host and --release are required for a native phase")
    {"driver": driver, "parent": parent, "fresh": fresh}[args.phase](args)


if __name__ == "__main__":
    main()
