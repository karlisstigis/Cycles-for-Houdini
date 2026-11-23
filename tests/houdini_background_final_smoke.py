# SPDX-License-Identifier: Apache-2.0
"""Real batch isolation, five final frames, source recovery, OCIO and stop/failure."""

from support.paths import artifacts_root
import pass_fixtures as pass_fixture

from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import copy
import os
from pathlib import Path
import sys
from support.devices import fixture_device
import subprocess
import time
import traceback

import hou
import numpy as np
import OpenImageIO as oiio
import PyOpenColorIO as ocio
import cycles_houdini as ch
from cycles_houdini import background_render as bg, final_render

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage3/background-final" / str(time.time_ns())
destination.mkdir(parents=True)
hou.putenv("CYCLES_HOUDINI_BACKGROUND_ROOT", str(destination / "jobs $literal `tick` 🐷"))
device = fixture_device(sys.argv)
checks = []
report = dict(result="running", checks=checks, scope=__doc__, device=device)


def wait(job, state=None):
    began = time.monotonic()
    while time.monotonic() - began < 100:
        status = job.status()
        if state and status["state"] == state:
            return status
        if status["state"] in bg._terminal:
            if job.owned:
                job.wait(timeout=15)
            if state:
                raise AssertionError(status)
            return status
        time.sleep(0.05)
    raise TimeoutError(job.status())


def image(path):
    source = oiio.ImageInput.open(str(path))
    assert source, oiio.geterror()
    spec = source.spec()
    pixels = np.asarray(source.read_image(format=oiio.FLOAT), np.float32)
    source.close()
    return spec, pixels


def active_render(job):
    began = time.monotonic()
    while time.monotonic() - began < 50:
        status = job.status()
        if status["state"] == "rendering" and status.get("generation") and status.get("progress", 0) > 0:
            return status
        assert status["state"] not in bg._terminal, status
        time.sleep(0.02)
    raise TimeoutError(job.status())


try:
    # A relative LUT must retain the active config's working directory after
    # its serialized copy moves into the private background job directory.
    (destination / "luts").mkdir()
    (destination / "luts/gain.spi1d").write_text("Version 1\nFrom 0.0 1.0\nLength 2\nComponents 1\n{\n0.0\n2.0\n}\n")
    config = copy.deepcopy(ocio.GetCurrentConfig())
    config.setName("Background Snapshot Test")
    config.clearSearchPaths()
    config.addSearchPath("luts")
    target = ocio.ColorSpace(name="Background Output")
    target.setTransform(ocio.FileTransform(src="gain.spi1d"), ocio.COLORSPACE_DIR_TO_REFERENCE)
    config.addColorSpace(target)
    config.validate()
    config_path = destination / "active.ocio"
    config_path.write_text(config.serialize(), encoding="utf-8")
    hou.putenv("OCIO", str(config_path))
    hou.Color.reloadOCIO()
    ch.synchronize_color_configuration()
    material = ch.create_material(name="background_material", shader="emission")
    shader = material.node("surface_shader")
    shader.parmTuple("color").set((0.2, 0.4, 0.7))
    pass_fixture.parm(shader, "strength").set(1)
    geometry = hou.node("/obj").createNode("geo", "background_subject", run_init_scripts=False)
    grid = geometry.createNode("grid")
    pass_fixture.set_parameters(grid, dict(orient=0, rows=16, cols=16))
    binding = geometry.createNode("attribwrangle", "animated_material_recovery")
    binding.setInput(0, grid)
    pass_fixture.parm(binding, "class").set(1)
    pass_fixture.parm(binding, "snippet").set(
        's@shop_materialpath = (@Frame==2 || @Frame==4) ? "/mat/missing_shader" : "' + material.path() + '";'
    )
    binding.setRenderFlag(True)
    camera = hou.node("/obj").createNode("cam", "background_camera")
    pass_fixture.set_parameters(camera, dict(tz=5, projection=1, orthowidth=4))
    rop = hou.node("/out").createNode("cycles_ipr", "background_final")
    set_rop_devices(rop, device)
    pass_fixture.set_parameters(
        rop,
        dict(
            source_mode="sop",
            soppath=binding.path(),
            camera=camera.path(),
            resx=80,
            resy=60,
            samples=4,
            viewer_interactive=1,
            ipr_samples=1,
            adaptive_sampling=0,
            use_denoise=0,
            render_threads=2,
            allowmotionblur=0,
            use_material_assignments=1,
            output_color_override=1,
            output_color_space="Background Output",
            pass_depth=1,
            crypto_object=1,
            output_precision="float",
            trange=1,
            persistent_data=1,
            picture="$HIP/reference_$HIPNAME.$F4.exr",
        ),
    )
    for parm in rop.parmTuple("f"):
        parm.deleteAllKeyframes()
    rop.parmTuple("f").set((1, 5, 1))
    scene = destination / "artist.hiplc"
    hou.hipFile.save(str(scene), save_to_recent_files=False)
    hou.setFrame(3)
    rop.render(frame_range=(1, 5, 1))
    foreground = rop.cachedUserData(final_render._summary_key)
    assert foreground["completed_frames"] == 5 and foreground["created_sessions"] == 1, foreground
    references = [Path(pass_fixture.parm(rop, "picture").evalAtFrame(frame)) for frame in range(1, 6)]
    pass_fixture.parm(rop, "picture").set("$HIP/background_$HIPNAME.$F4.exr")
    before = dict(
        scene=hou.hipFile.path(),
        frame=hou.frame(),
        dirty=hou.hipFile.hasUnsavedChanges(),
        env={name: hou.getenv(name) for name in ("HIP", "HIPNAME", "JOB")},
    )
    assert rop.parmTemplateGroup().find("executebackground").isHidden()
    started = time.monotonic()
    pass_fixture.parm(rop, "ch_render_background").pressButton()
    job = bg.last_job(rop)
    launch_seconds = time.monotonic() - started
    assert (
        dict(
            scene=hou.hipFile.path(),
            frame=hou.frame(),
            dirty=hou.hipFile.hasUnsavedChanges(),
            env={name: hou.getenv(name) for name in ("HIP", "HIPNAME", "JOB")},
        )
        == before
    )
    assert rop.cachedUserData(final_render._summary_key) == foreground
    # Changes made immediately after submission must not leak into the snapshot.
    shader.parmTuple("color").set((1, 0, 0))
    pass_fixture.parm(geometry, "tx").set(100)
    status = wait(job)
    assert status["state"] == "completed" and status["completed_frames"] == 5, status
    assert status["pid"] != os.getpid() and status["created_sessions"] == 1, status
    frames = [json.loads(line) for line in (job.directory / "frames.jsonl").read_text().splitlines()]
    assert [value["frame"] for value in frames] == [1, 2, 3, 4, 5], frames
    assert all(value["frame_seconds"] > 0 for value in frames)
    assert frames[0]["previous_frame_seconds"] is None
    for previous, current in zip(frames, frames[1:]):
        assert current["previous_frame_seconds"] == previous["frame_seconds"]
    normal_max_error = 0.0
    for number, reference, record in zip(range(1, 6), references, frames):
        actual = destination / f"background_artist.{number:04d}.exr"
        assert Path(record["picture"]) == actual
        expected_spec, expected = image(reference)
        actual_spec, pixels = image(actual)
        assert expected_spec.channelnames == actual_spec.channelnames
        for index, name in enumerate(expected_spec.channelnames):
            before_channel, after_channel = expected[:, :, index], pixels[:, :, index]
            if np.array_equal(before_channel.view(np.uint32), after_channel.view(np.uint32)):
                continue
            # Independent OptiX renders can round the filtered normal sum by
            # one float32 step. Keep color, depth, UV and Cryptomatte bit exact.
            error = float(np.max(np.abs(before_channel - after_channel)))
            assert device != "CPU" and name.startswith("normal.") and error <= 4 * np.finfo(np.float32).eps, (
                number,
                name,
                error,
            )
            normal_max_error = max(normal_max_error, error)
        diagnostics = json.loads(actual_spec.get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        assert bool(diagnostics) == (number in (2, 4)), (number, diagnostics)
        assert actual_spec.get_string_attribute("oiio:ColorSpace") == "Background Output"
        assert record["status"]["rendering"]["samples"] == 4
        assert record["status"]["render_device_ids"] == [device]
    checks.append(
        dict(
            name="Five isolated frames retain recovery, data passes and relative-LUT color",
            status=status,
            launch_seconds=launch_seconds,
            bit_identical_to_foreground=normal_max_error == 0,
            other_channels_bit_identical=True,
            filtered_normal_max_error=normal_max_error,
        )
    )
    pass_fixture.parm(geometry, "tx").set(0)
    shader.parmTuple("color").set((0.2, 0.4, 0.7))
    pass_fixture.parm(rop, "output_existing").set("skip")
    skipped = wait(bg.launch(rop))
    assert skipped["state"] == "completed" and skipped["skipped_frames"] == 5 and skipped["completed_frames"] == 0, (
        skipped
    )
    assert skipped["frame_elapsed_seconds"] == 0 and skipped.get("last_frame_seconds") is None
    assert skipped.get("previous_frame_seconds") is None
    checks.append(dict(name="Existing output skip policy", status=skipped))
    pass_fixture.parm(rop, "output_existing").set("error")
    failure = wait(bg.launch(rop))
    assert failure["state"] == "failed" and "already exists" in failure["error"], failure
    checks.append(dict(name="Output failures remain failures", status=failure))
    pass_fixture.set_parameters(
        rop,
        dict(
            output_existing="overwrite",
            picture="$HIP/cancelled.$F4.exr",
            samples=1048576,
            time_limit=30,
            trange=0,
            resx=320,
            resy=180,
        ),
    )
    cancelled = bg.launch(rop)
    active_render(cancelled)
    assert bg.Job(cancelled.directory).status()["state"] == "rendering"
    bg.Job(cancelled.directory).stop()  # Reopened Houdini can request a safe stop.
    stopped = wait(cancelled)
    assert stopped["state"] == "cancelled" and stopped["completed_frames"] == 0, stopped
    assert not list(destination.glob("cancelled.*.exr"))
    checks.append(dict(name="Owned background stop", status=stopped))
    interrupted = bg.launch(rop)
    active_render(interrupted)
    interrupted.terminate()
    interrupted.wait(timeout=15)
    recovered = bg.Job(interrupted.directory).status()
    assert recovered["state"] == "failed" and "exited" in recovered["error"], recovered
    checks.append(dict(name="Discover an interrupted job using process creation identity", status=recovered))
    # A submitted render must outlive normal closure of the launching Houdini.
    # The outer test holds no process handle for this renderer: it reads the
    # persistent report just as a newly opened artist session would.
    pass_fixture.set_parameters(rop, dict(picture="$HIP/after-parent-close.$F4.exr", time_limit=2))
    parent_scene = destination / "parent-exit-source.hiplc"
    bg._save_copy(parent_scene)
    announcement = destination / "parent-exit-job.json"
    helper = destination / "launch-and-close.py"
    helper.write_text(
        "import hou,json,os\nfrom pathlib import Path\n"
        "from cycles_houdini import background_render as bg\n"
        f"hou.hipFile.load({str(parent_scene)!r},suppress_save_prompt=True)\n"
        f"hou.hipFile.setName({str(scene)!r})\n"
        'job=bg.launch(hou.node("/out/background_final"))\n'
        f"Path({str(announcement)!r}).write_text(json.dumps(dict(directory=str(job.directory),parent_pid=os.getpid())))\n"
        "hou.exit(0,suppress_save_prompt=True)\n",
        encoding="utf-8",
    )
    with (destination / "parent-exit.log").open("wb") as log:
        launching_parent = subprocess.Popen(
            [str(Path(hou.getenv("HFS")) / "bin/hython.exe"), str(helper)],
            env=bg._environment(),
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        assert launching_parent.wait(timeout=40) == 0
    announcement = json.loads(announcement.read_text())
    orphan = bg.Job(announcement["directory"])
    after_close = orphan.status()
    assert after_close["state"] not in bg._terminal, after_close
    survives = wait(orphan)
    assert survives["state"] == "completed" and survives["completed_frames"] == 1, survives
    assert survives["pid"] != announcement["parent_pid"] != os.getpid()
    checks.append(
        dict(
            name="Submitted render survives closing its launching Houdini",
            state_after_parent_exit=after_close["state"],
            status=survives,
        )
    )
    # The artist need not save a new scene before starting a batch job. Indie
    # snapshots need their actual license-compatible extension even when the
    # in-memory scene still has Houdini's default .hip suffix.
    unsaved = destination / "unsaved.hip"
    hou.hipFile.setName(str(unsaved))
    pass_fixture.set_parameters(rop, dict(samples=4, time_limit=0, picture="$HIP/unsaved-$HIPNAME.$F4.exr"))
    new_scene = bg.launch(rop)
    fresh = wait(new_scene)
    assert fresh["state"] == "completed" and fresh["completed_frames"] == 1 and not unsaved.exists(), fresh
    assert hou.hipFile.path().replace("\\", "/") == str(unsaved).replace("\\", "/")
    assert (destination / "unsaved-unsaved.0003.exr").is_file()
    snapshot = Path(json.loads((new_scene.directory / "job.json").read_text())["snapshot"])
    assert snapshot.is_file()
    if "Indie" in str(hou.licenseCategory()):
        assert snapshot.suffix == ".hiplc"
    checks.append(dict(name="Unsaved scene snapshot preserves the original name without saving it", status=fresh))
    assert not final_render.active_sequences()
    assert rop.cachedUserData(final_render._summary_key) == foreground
    assert any(candidate.directory == job.directory for candidate in bg.jobs())
    report.update(result="passed", parent_context_preserved=before, foreground_summary=foreground)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    for job in bg.active_jobs():
        if job.is_running():
            job.stop()
            try:
                job.wait(timeout=15)
            except Exception:
                job.terminate()
                job.wait(timeout=15)
    (destination / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (destination.parent / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result="passed", checks=len(checks), report=str(destination / "report.json"))))
