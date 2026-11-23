# SPDX-License-Identifier: Apache-2.0
"""Strict render contracts and local source recovery in worker IPR/ordered final."""

import argparse
import json
from pathlib import Path
import time
import traceback

import hou
import numpy as np
import cycles_houdini as ch
from transaction_scene_fixture import Fixture


parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
f = Fixture(device)
path = f.dest.parent / "contract-report.json"
report = dict(result="running", device=device, scope=__doc__, checks=[])


def until(condition):
    deadline = time.monotonic() + 60
    while not condition():
        assert time.monotonic() < deadline, "Session stalled"
        time.sleep(0.01)


def record(name, session, **extra):
    report["checks"].append(dict(name=name, operation=operation, status=session.status(), **extra))
    path.write_text(json.dumps(report, indent=2))


def reject(session, update, name, initialized=True, stable=True):
    before = session.status()
    previous = session.frame_info()
    try:
        generation = session.apply(update, at_time=hou.time())
        session.wait(generation, timeout=60)
    except RuntimeError as error:
        message = str(error)
    else:
        raise AssertionError("An invalid render contract was accepted: " + name)
    after = session.status()
    for key in (
        "geometry_resources",
        "object_resources",
        "geometry_conversions",
        "owned_geometry_bytes",
        "mesh_variant_resources",
    ):
        assert after[key] == before[key], (name, key, before[key], after[key])
    assert after.get("committed_generation", after["generation"]) == before.get(
        "committed_generation", before["generation"]
    ), (name, before, after)
    if initialized:
        if operation == "ipr":
            assert (
                after["renderer_pid"] == before["renderer_pid"] and after["backend_epoch"] == before["backend_epoch"]
            ), after
            assert not after["capture_restart_required"], after
        if stable:
            assert session.frame_info() == previous, (name, previous, session.frame_info())
    record(name, session, error=message, before=before)


def render(session, name, color=None, background=None, warning=False):
    generation = session.apply(f.capture(), at_time=hou.time())
    frame = session.wait(generation, timeout=60)
    assert frame["generation"] == generation and frame["complete"] and frame.get("current_scene", True), frame
    physical = frame.get("render_revision")
    # A worker samples status before collecting its frame. A fast final image
    # can arrive in a reply whose status still describes an earlier sample.
    # Require that same render to reach Complete; never follow a newer request.
    deadline = time.monotonic() + 5
    while True:
        status = session.status()
        assert not status["error"] and not status.get("update_rejected"), status
        assert status["generation"] == generation, status
        assert status.get("committed_generation", generation) == generation, status
        if physical is not None:
            assert status["render_revision"] == physical, status
        assert status["state"] in ("rendering", "complete"), status
        if status["state"] == "complete":
            break
        assert time.monotonic() < deadline, ("Completion status did not catch up", status)
        time.sleep(0.01)
    assert status["geometry_resources"] == status["object_resources"] == 1, status
    assert status["render_device_ids"] == [device], status
    assert bool(status["source_diagnostics"]) == warning, status["source_diagnostics"]
    values = f.check(frame, color, background)
    record(name, session, pixels=values)
    return frame


try:
    for operation in ("final", "ipr"):
        f.geo.parm("shop_materialpath").set(f.material.path())
        f.texture.parm("filename").set(f.write_image(f.dest / "color.exr", f.color))
        f.rop.setParms(dict(world_source="color", world_material=f.world.path(), samples=4, ipr_samples=4))
        with ch.Session(
            device=device,
            width=f.width,
            height=f.height,
            samples=4,
            threads=2,
            execution="worker" if operation == "ipr" else "sync",
            interactive=operation == "ipr",
            denoising=dict(enabled=False),
            use_texture_cache=False,
            collect_diagnostics=True,
        ) as session:
            reject(
                session,
                dict(rendering=dict(samples=4, max_bounce=-1)),
                "Rejected first request commits no scene",
                initialized=False,
            )
            assert session.status()["geometry_resources"] == 0 and session.frame_info() is None
            baseline = render(session, "Full first scene after rejected request")
            for index in range(3):
                reject(
                    session,
                    dict(rendering=dict(samples=4, max_bounce=-1)),
                    f"Invalid bounce limit retains accepted data {index}",
                )
                render(session, f"Repair bounce limit {index}")
            reject(session, dict(passes=["not_a_render_pass"]), "Unknown pass metadata fails before render reservation")
            render(session, "Valid metadata after preflight error")
            request = f.capture()["geometry"][0]
            reject(
                session,
                dict(deleted_geometry=[request["geometry_key"]]),
                "A geometry resource still bound to an object cannot be retired",
            )
            render(session, "Full source repair after rejected ownership edit")

            missing = f.dest / (operation + "-missing.exr")
            missing.unlink(missing_ok=True)
            f.texture.parm("filename").set(str(missing))
            frame = render(session, "Missing image accepts a magenta color input", color=(1, 0, 1), warning=True)
            for name in ("depth", "position"):
                np.testing.assert_allclose(f.plane(frame, name), f.plane(baseline, name), atol=1e-6, rtol=0)
            render(session, "Missing-image warning survives warm capture", color=(1, 0, 1), warning=True)
            f.write_image(missing, (0.1, 0.6, 0.2))
            render(session, "Creating the missing file repairs shading", color=(0.1, 0.6, 0.2))
            f.geo.parm("shop_materialpath").set(f.foreign.path())
            render(session, "Unfamiliar assigned shader keeps the plane visible", color=(1, 0, 1), warning=True)
            f.geo.parm("shop_materialpath").set(f.material.path())
            render(session, "Repair assignment after unfamiliar shader", color=(0.1, 0.6, 0.2))
            f.rop.setParms(dict(world_source="material", world_material=f.world.path()))
            render(session, "Independently specified world radiance", color=(0.1, 0.6, 0.2), background=f.world_color)
            f.rop.parm("world_material").set("/mat/absent_transaction_world")
            render(session, "Broken world accepts configured background", color=(0.1, 0.6, 0.2), warning=True)
            render(session, "Broken-world warning persists on warm capture", color=(0.1, 0.6, 0.2), warning=True)
            f.rop.parm("world_material").set(f.world.path())
            render(session, "World repair clears source diagnostics", color=(0.1, 0.6, 0.2), background=f.world_color)

            blocked = f.dest / (operation + "-blocked.exr")
            blocked.mkdir(exist_ok=True)
            try:
                session.write_exr(str(blocked))
            except RuntimeError as error:
                record("Output writing failure remains explicit", session, error=str(error))
            else:
                raise AssertionError("Writing an image over a directory succeeded")
            render(
                session,
                "Failed output operation leaves accepted shading usable",
                color=(0.1, 0.6, 0.2),
                background=f.world_color,
            )
            if operation == "ipr":
                f.rop.parm("ipr_samples").set(1048576)
                active = session.apply(f.capture(), at_time=hou.time())
                # status() advances host capture; frame metadata is deliberately
                # read-only so image conversion can query it off the UI thread.
                until(
                    lambda: session.status()["committed_generation"] == active
                    and session.frame_info() is not None
                    and session.frame_info()["generation"] == active
                )
                reject(
                    session,
                    dict(rendering=dict(samples=4, max_bounce=-1)),
                    "An active render survives an invalid settings request",
                    stable=False,
                )
                session.pause(True)
                until(lambda: session.status()["backend_state"] == "paused")
                assert session.status()["update_rejected"]
                repaired = session.apply(f.capture(), at_time=hou.time())
                until(lambda: session.status()["committed_generation"] == repaired)
                assert session.status()["state"] == "paused" and not session.status()["update_rejected"]
                record("Repair while paused preserves pause", session)
                session.pause(False)
                until(lambda: session.status()["backend_state"] == "rendering")
                session.cancel()
                until(lambda: session.status()["state"] == "cancelled")
                assert not session.status()["update_rejected"] and not session.status()["pending_update"]
                record("Resume and Stop control retained active scene", session)
                f.rop.parm("ipr_samples").set(4)
                render(session, "Render restarts after Stop", color=(0.1, 0.6, 0.2), background=f.world_color)
        closed = session.status()
        assert closed["owned_geometry_bytes"] == closed["device_allocation_bytes"] == 0, closed
        report.setdefault("closed", []).append(dict(operation=operation, status=closed))
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    path.write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
