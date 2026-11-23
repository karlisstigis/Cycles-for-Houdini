# SPDX-License-Identifier: Apache-2.0
"""Atomic native publication, discarded edits and bounded background retirement."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import time
import traceback

import hou
import numpy as np
import cycles_houdini as ch
from stylesheet_storage_fixture import Fixture


parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
f = Fixture(device, heavy=False)
path = f.dest / "scene-publication-report.json"
report = dict(result="running", device=device, scope=__doc__, checks=[])
capture = ch._native.create(device, f.width, f.height, 1, True, False, False, False, None, True)
engine = ch._native.engine_create(capture)
job = None
reference_planes = None
engine_previous = None


def rejected(call, text):
    try:
        call()
    except RuntimeError as error:
        assert text in str(error), str(error)
    else:
        raise AssertionError("Invalid transaction operation was accepted")


def prepare(data, executor):
    global job
    if "geometry" in data:
        job = ch._native.acquire_apply(capture, data, hou.time())
        for _ in range(1024):
            executor.submit(ch._native.prewarm_apply, job).result(timeout=30)
            if not ch._native.expand_apply(job):
                break
        else:
            raise AssertionError("Packed acquisition did not complete")
        for _ in range(1024):
            if ch._native.prepare_apply(capture, data, hou.time(), False, job):
                break
        else:
            raise AssertionError("Host preparation did not complete")
    else:
        job = ch._native.prepare_apply(capture, data, hou.time(), False)
    executor.submit(ch._native.convert_apply, job).result(timeout=30)
    return job


def accept(data, executor):
    global job
    prepare(data, executor)
    result = ch._native.finish_apply(job)
    job = None
    return result


def check(name, snapshot, objects=12, triangles=8, previous=None, empty=False):
    global reference_planes, engine_previous
    info = ch._native.snapshot_info(snapshot, previous) if previous is not None else ch._native.snapshot_info(snapshot)
    assert info["objects"] == objects and info["mesh_triangles"] == triangles, info
    assert info["geometry_resources"] == (0 if empty else 1), info
    if previous is not None:
        assert not any(
            info["difference"][key]
            for key in ("mesh_upserts", "object_upserts", "object_poses", "object_deletions", "geometry_deletions")
        ), info
    generation = (
        ch._native.apply_snapshot(engine, snapshot, engine_previous)
        if engine_previous is not None
        else ch._native.apply_snapshot(engine, snapshot)
    )
    engine_previous = snapshot
    deadline = time.monotonic() + 60
    while True:
        status = ch._native.status(engine)
        assert status["state"] != "failed" and not status["error"], status
        frame = ch._native.frame(engine)
        if frame is not None and frame["complete"] and frame["generation"] == generation:
            break
        assert time.monotonic() < deadline, status
        time.sleep(0.01)
    assert status["render_device_ids"] == [device], status
    if empty:
        plane = frame["planes"]["combined"]
        pixels = np.frombuffer(plane["pixels"], np.float32).reshape(f.height, f.width, plane["channels"])
        assert np.isfinite(pixels).all() and np.max(np.abs(pixels[:, :, :3])) < 1e-6, dict(
            minimum=pixels.min((0, 1)).tolist(),
            maximum=pixels.max((0, 1)).tolist(),
            center=pixels[f.height // 2, f.width // 2].tolist(),
            status=status,
            inventory=info,
        )
    else:
        f.check(frame)
        planes = {name: np.frombuffer(plane["pixels"], np.float32) for name, plane in frame["planes"].items()}
        if reference_planes is None:
            reference_planes = {name: values.copy() for name, values in planes.items()}
        else:
            assert planes.keys() == reference_planes.keys()
            for plane_name, values in planes.items():
                np.testing.assert_allclose(values, reference_planes[plane_name], atol=1e-6, rtol=0)
    life = ch._native.native_stats(capture)["scene_lifecycle"]
    assert life["retired_scenes"] <= 1 and life["retired_snapshots"] <= 1, life
    if life["retirements"]:
        assert not life["retirement_on_host"], life
    report["checks"].append(dict(name=name, inventory=info, lifecycle=life))
    path.write_text(json.dumps(report, indent=2))
    return life


try:
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="Owned scene conversion") as executor:
        baseline = accept(f.update(), executor)
        check("Initial shared prototype and independently specified emission colors", baseline)
        converted = prepare(f.update(), executor)
        rejected(lambda: ch._native.convert_apply(converted), "only once")
        repeated = ch._native.finish_apply(converted)
        job = None
        rejected(lambda: ch._native.finish_apply(converted), "already consumed")
        check("Single-use guards preserve the accepted scene", repeated, previous=baseline)

        # A converted edit must not affect the accepted image until publication.
        f.prototype.parm("rows").set(5)
        f.targets.setParms(dict(rows=4, cols=4))
        f.obj.parm("tx").set(1)
        prepare(f.update(), executor)
        ch._native.discard_apply(job)
        job = None
        retained = accept(dict(camera_path=f.camera.path()), executor)
        life = check(
            "Discarding changed topology, placement count and transform retains the accepted image",
            retained,
            previous=repeated,
        )
        assert life["last_retired_objects"] == 16, life

        f.prototype.parm("rows").set(3)
        f.targets.setParms(dict(rows=3, cols=4))
        f.obj.parm("tx").set(0)
        restored = accept(f.update(), executor)
        check("A full repaired capture restores source retention after discard", restored)
        request = f.update()["geometry"][0]
        deletion = dict(deleted_objects=[request["object_key"]], deleted_geometry=[request["geometry_key"]])
        prepare(deletion, executor)
        ch._native.discard_apply(job)
        job = None
        retained = accept(dict(camera_path=f.camera.path()), executor)
        check(
            "Discarded alias deletion leaves all accepted instances and geometry visible", retained, previous=restored
        )
        removed = accept(deletion, executor)
        life = check(
            "Explicit root aliases remove every physical instance and resource",
            removed,
            objects=0,
            triangles=0,
            empty=True,
        )
        assert life["retired_objects"] == 12, life
        drained = accept(dict(camera_path=f.camera.path()), executor)
        life = check(
            "Camera-only conversion drains the old instance table in the background",
            drained,
            objects=0,
            triangles=0,
            previous=removed,
            empty=True,
        )
        assert life["retired_scenes"] == 0 and life["last_retired_objects"] == 12, life
        restored = accept(f.update(), executor)
        check("Reintroducing a directly deleted source restores the complete render", restored)

        rejected(
            lambda: ch._native.prepare_apply(capture, dict(camera_path="/obj/absent_publication_camera"), hou.time()),
            "camera",
        )
        retained = accept(dict(camera_path=f.camera.path()), executor)
        check("An essential camera error remains explicit and preserves accepted geometry", retained, previous=restored)

        removed = accept(dict(geometry=[]), executor)
        check("Authoritative removal retires the packed source", removed, objects=0, triangles=0, empty=True)
        restored = accept(f.update(), executor)
        check("Reintroducing an authoritatively removed source restores its image", restored)
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    if job is not None:
        ch._native.discard_apply(job)
    ch._native.close(engine)
    ch._native.close(capture)
    report["closed"] = ch._native.status(engine)
    report["closed_lifecycle"] = ch._native.native_stats(capture)["scene_lifecycle"]
    assert report["closed"]["device_allocation_bytes"] == report["closed"]["owned_geometry_bytes"] == 0
    assert report["closed_lifecycle"]["retired_scenes"] == report["closed_lifecycle"]["retired_snapshots"] == 0
    path.write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
