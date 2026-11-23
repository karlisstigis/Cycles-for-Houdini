# SPDX-License-Identifier: Apache-2.0
"""Native thumbnail pixels, bounded queues/cache, live shader repair and owned worker retirement."""

from support.paths import artifacts_root
import json
import os
from pathlib import Path
import sys
from support.devices import fixture_device
import time
import traceback

import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import _native
from cycles_houdini.material_previews import PreviewService

device = fixture_device(sys.argv)
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage3/material-thumbnails" / str(time.time_ns())
destination.mkdir(parents=True)
busy = [False]
service = PreviewService(device=device, samples=8, idle_seconds=0.1, busy=lambda: busy[0])
report = dict(result="running", scope=__doc__, device=device, events=[])
nodes = []


def wait(predicate, seconds=40):
    began = time.monotonic()
    while time.monotonic() - began < seconds:
        service.tick(force=True)
        if predicate():
            return
        time.sleep(0.01)
    raise TimeoutError(service.diagnostics())


def rendered(node, minimum):
    wait(lambda: service.completed >= minimum and service.active is None and not service.pending)
    assert _native.thumbnail_pixels(node.sessionId()), service.diagnostics()
    return np.frombuffer(_native.thumbnail_pixels(node.sessionId()), np.uint8).reshape(64, 64, 4).copy()


def native_token(node, size=64):
    return service.request(node, size)


try:
    material = ch.create_material(name="thumbnail_emission", shader="emission")
    nodes.append(material)
    shader = material.node("surface_shader")
    shader.parmTuple("color").set((0.2, 0.4, 0.6))
    shader.parm("strength").set(1)
    hou.hipFile.save(str(destination / "artist.hiplc"), save_to_recent_files=False)
    before = dict(
        children=[node.path() for node in hou.node("/obj").children()],
        dirty=hou.hipFile.hasUnsavedChanges(),
        frame=hou.frame(),
    )
    token = native_token(material)
    pixels = rendered(material, 1)
    assert (
        dict(
            children=[node.path() for node in hou.node("/obj").children()],
            dirty=hou.hipFile.hasUnsavedChanges(),
            frame=hou.frame(),
        )
        == before
    )
    linear = service.worker.frame()
    rgb = np.frombuffer(linear["planes"]["combined"]["pixels"], np.float32).reshape(64, 64, 4)
    np.testing.assert_allclose(rgb[32, 32, :3], (0.2, 0.4, 0.6), rtol=0, atol=0.0001)
    display = service.worker.display_image(channels="rgba", checkerboard=False, exposure=0, gamma=1)
    np.testing.assert_array_equal(pixels, np.frombuffer(display["rgba"], np.uint8).reshape(64, 64, 4)[::-1])
    status = service.worker.status()
    assert status["geometry_resources"] == status["object_resources"] == 1 and status["render_device_ids"] == [
        device
    ], status
    report["events"].append(
        dict(
            name="Native raster matches OCIO display pixels and analytic linear emission",
            token=token,
            status=status,
            scene_preserved=before,
        )
    )

    first = native_token(material)
    for i in range(100):
        latest = native_token(material)
    stats = _native.thumbnail_stats()
    assert stats["queued"] == 1 and stats["coalesced"] >= 100, stats
    assert not _native.publish_thumbnail(first, bytes(64 * 64 * 4))
    assert _native.current_thumbnail(latest)
    shader.parmTuple("color").set((0.7, 0.1, 0.05))  # Existing watch must request automatically.
    rendered(material, 2)
    rgb = np.frombuffer(service.worker.frame()["planes"]["combined"]["pixels"], np.float32).reshape(64, 64, 4)
    np.testing.assert_allclose(rgb[32, 32, :3], (0.7, 0.1, 0.05), rtol=0, atol=0.0001)
    assert service.worker.status()["geometry_conversions"] == status["geometry_conversions"]
    report["events"].append(
        dict(
            name="Latest request wins and native parameter edits update without geometry conversion",
            stats=service.diagnostics(),
        )
    )

    output = material.node("surface_output")
    output.bypass(True)
    rendered(material, 3)
    assert material.sessionId() in service.errors, service.diagnostics()
    rgb = np.frombuffer(service.worker.frame()["planes"]["combined"]["pixels"], np.float32).reshape(64, 64, 4)
    np.testing.assert_allclose(rgb[32, 32, :3], (1, 0, 1), rtol=0, atol=0.001)
    output.bypass(False)
    rendered(material, 4)
    assert material.sessionId() not in service.errors, service.diagnostics()
    report["events"].append(dict(name="Broken output remains visible as a diagnostic swatch and repairs automatically"))

    busy[0] = True
    started = service.started
    native_token(material)
    end = time.monotonic() + 0.3
    while time.monotonic() < end:
        service.tick(force=True)
        time.sleep(0.01)
    assert service.started == started and service.pending, service.diagnostics()
    busy[0] = False
    rendered(material, 5)
    doomed = ch.create_material(name="thumbnail_deleted")
    nodes.append(doomed)
    deleted = native_token(doomed)
    wait(lambda: service.active and service.active["token"] == deleted)
    nodes.remove(doomed)
    doomed.destroy()
    assert not _native.current_thumbnail(deleted)
    wait(lambda: service.active is None)
    assert not _native.publish_thumbnail(deleted, bytes(64 * 64 * 4))
    report["events"].append(
        dict(name="Foreground priority and deletion suppress stale publication", stats=service.diagnostics())
    )

    # Exercise bounded native pending metadata without rendering every request.
    for i in range(48):
        node = ch.create_material(name="bounded_thumbnail_" + str(i))
        nodes.append(node)
        native_token(node, 256)
    stats = _native.thumbnail_stats()
    assert stats["queued"] <= 16 and stats["evicted"] >= 32, stats
    _native.take_thumbnails()
    pattern = np.zeros((256, 256, 4), np.uint8)
    pattern[:, :, 0] = np.arange(256)[:, None]
    pattern[:, :, 3] = 255
    for node in nodes:
        token = native_token(node, 256)
        assert _native.publish_thumbnail(token, pattern.tobytes())
    stats = _native.thumbnail_stats()
    assert stats["images"] <= 32 and stats["image_bytes"] <= 8 * 1024 * 1024, stats
    np.testing.assert_array_equal(
        np.frombuffer(_native.thumbnail_pixels(nodes[-1].sessionId()), np.uint8).reshape(256, 256, 4), pattern[::-1]
    )
    _native.take_thumbnails()
    report["events"].append(
        dict(name="Pending queue, raster count and bytes stay bounded; rows retain orientation", stats=stats)
    )
    service.pending.clear()
    wait(lambda: service.worker is None and service.retiring is None)
    assert service.diagnostics()["worker"] == 0 and service.diagnostics()["retiring"] == 0
    report.update(result="passed", final=service.diagnostics())
except BaseException:
    report.update(result="failed", error=traceback.format_exc(), final=service.diagnostics())
    raise
finally:
    try:
        service.close(wait=True)
        for node in nodes:
            try:
                node.destroy()
            except hou.ObjectWasDeleted:
                pass
    except BaseException:
        report.update(result="failed", cleanup_error=traceback.format_exc())
        raise
    finally:
        (destination / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        (destination.parent / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result=report["result"], events=len(report["events"]), report=str(destination / "report.json"))))
