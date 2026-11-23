# SPDX-License-Identifier: Apache-2.0
"""Opt-in timing/copy diagnostics with independent radiance and upload checks."""

from support.paths import artifacts_root
from array import array
import json
import math
import os
from pathlib import Path

import hou
import cycles_houdini as ch

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage1"
destination.mkdir(parents=True, exist_ok=True)
obj = hou.node("/obj").createNode("geo", "diagnostic_quad", run_init_scripts=False)
grid = obj.createNode("grid")
grid.parm("type").set(0)
grid.parm("orient").set(0)
grid.parm("rows").set(2)
grid.parm("cols").set(2)
grid.parmTuple("size").set((2, 2))
color = obj.createNode("color")
color.setInput(0, grid)
color.parmTuple("color").set((0.2, 0.4, 0.8))
camera = hou.node("/obj").createNode("cam", "diagnostic_camera")
camera.parm("tz").set(5)
camera.parm("projection").set(1)
camera.parm("orthowidth").set(4)
material = dict(key="emission", kind="emission", color=(0.8, 0.1, 0.02))


def expect(frame, rgb):
    pixels = array("f")
    pixels.frombytes(frame["planes"]["combined"]["pixels"])
    offset = ((frame["height"] // 2) * frame["width"] + frame["width"] // 2) * 4
    actual = list(pixels[offset : offset + 3])
    assert max(abs(a - b) for a, b in zip(actual, rgb)) < 0.002, (actual, rgb)
    return actual


def check_snapshot(session):
    data = session.diagnostics()
    status = session.status()
    assert not data["engine_statistics_pending"], data
    assert math.isfinite(data["engine_snapshot_wait_seconds"]) and data["engine_snapshot_wait_seconds"] >= 0
    assert data["enabled"] and data["generation"] == status.get("render_revision", status["generation"])
    assert data["output_deliveries"] == 1 and data["output_delivery_seconds"] > 0
    assert data["core_update_seconds"] > 0 and 0 <= data["cancel_join_seconds"] <= data["core_update_seconds"]
    assert data["engine_update_seconds"] and all(
        math.isfinite(value) and value >= 0 for value in data["engine_update_seconds"].values()
    )
    total = data["engine_update_seconds"]["scene/device_update_total"]
    assert total >= data["engine_update_seconds"].get("svm/device_update", 0)
    assert total >= data["engine_update_seconds"]["scene/shader_prepass_and_kernel_load"]
    for copy in data["buffer_copy_requests"].values():
        assert copy["calls"] > 0 and copy["requested_bytes"] >= 0 and copy["host_seconds"] >= 0
    return data


reports = []
for device in ("CPU", ch.default_device()):
    request = ch.geometry_request(obj, color, material="emission")
    scene = dict(geometry=[request], materials=[material], camera_path=camera.path(), background=(0, 0, 0))
    with ch.Session(device=device, width=96, height=54, samples=16) as plain:
        assert not plain.diagnostics()["enabled"]
        if plain.execution == "worker":
            assert not plain.status()["backend_alive"]
        plain_rgb = expect(plain.wait(plain.apply(scene)), (0.8, 0.1, 0.02))
        disabled = plain.diagnostics()
        assert disabled["engine_update_seconds"] == disabled["buffer_copy_requests"] == {}
        assert disabled["display_updates"] == disabled["output_deliveries"] == 0
    with ch.Session(device=device, width=96, height=54, samples=16, collect_diagnostics=True) as session:
        frame = session.wait(session.apply(scene))
        expect(frame, plain_rgb)
        cold = check_snapshot(session)
        assert cold["material_builds"] == cold["background_builds"] == 1
        # A single quad contains two packed three-uint triangle indices:
        # 2 triangles * 3 indices * 4 bytes, independent of the engine timer.
        assert cold["buffer_copy_requests"]["tri_vindex"]["requested_bytes"] == 24, cold
        assert cold["buffer_copy_requests"]["tri_verts"]["requested_bytes"] > 0
        camera.parm("tx").set(0.05)
        expect(session.wait(session.apply(scene)), (0.8, 0.1, 0.02))
        moved_camera = check_snapshot(session)
        for name in ("tri_vindex", "tri_verts", "tri_shader"):
            assert name not in moved_camera["buffer_copy_requests"], (name, moved_camera)
        assert session.status()["geometry_conversions"] == 1
        assert moved_camera["material_builds"] == cold["material_builds"]
        assert moved_camera["background_builds"] == cold["background_builds"]
        assert "svm_nodes" not in moved_camera["buffer_copy_requests"], moved_camera
        # Requiring Cd must invalidate shader attributes even when native
        # positions/topology are retained. This is not a shader-only guarantee.
        tinted = dict(material, color_attribute="Cd")
        expect(session.wait(session.apply(dict(materials=[tinted]))), (0.16, 0.04, 0.016))
        attribute = check_snapshot(session)
        assert any(name.startswith("attributes_") for name in attribute["buffer_copy_requests"]), attribute
        assert session.status()["geometry_conversions"] == 1
        assert attribute["material_builds"] == cold["material_builds"] + 1
        grid.parm("sizex").set(2.2)
        expect(session.wait(session.apply(dict(geometry=[request]))), (0.16, 0.04, 0.016))
        changed_geometry = check_snapshot(session)
        assert changed_geometry["buffer_copy_requests"]["tri_vindex"]["requested_bytes"] == 24
        assert changed_geometry["buffer_copy_requests"]["tri_verts"]["requested_bytes"] > 0
        assert session.status()["geometry_conversions"] == 2
        assert changed_geometry["material_builds"] == attribute["material_builds"]
        snapshot = session.diagnostics()
        repeated = session.diagnostics()
        assert math.isfinite(repeated.pop("engine_snapshot_wait_seconds"))
        snapshot.pop("engine_snapshot_wait_seconds")
        # A live worker keeps polling independently of these reads. Compare the
        # completed renderer snapshot, excluding host transport/capture metadata.
        for key in ("controller", "capture"):
            snapshot.pop(key, None)
            repeated.pop(key, None)
        assert repeated == snapshot, "Reading diagnostics mutated the stored snapshot"
        final = session.status()
    assert session.status()["device_allocation_bytes"] == 0
    grid.parm("sizex").set(2)
    camera.parm("tx").set(0)
    # Registry snapshots must not alias a caller's mutable dictionaries/lists.
    # Otherwise equality filtering can silently drop the later real edit.
    from cycles_houdini.registry import Entry

    with ch.Session(device=device, width=96, height=54, samples=16, collect_diagnostics=True) as shared:
        entry = Entry("diagnostic-registry", (device, 96, 54, 16), shared)
        caller_material = dict(material)
        caller_light = dict(key="point", kind="point", strength=[100, 100, 100])
        caller_background = [0, 0, 0]

        def capture():
            return dict(
                geometry=[request],
                materials=[caller_material],
                lights=[caller_light],
                camera_path=camera.path(),
                background=caller_background,
            )

        expect(shared.wait(entry.apply(capture(), 0)), (0.8, 0.1, 0.02))
        before = shared.diagnostics()
        shared.wait(entry.apply(capture(), 0))
        unchanged = shared.diagnostics()
        assert unchanged["material_builds"] == before["material_builds"]
        assert unchanged["light_updates"] == before["light_updates"]
        assert unchanged["background_builds"] == before["background_builds"]
        caller_material["color"] = (0.4, 0.1, 0.02)
        caller_light["strength"][0] = 50
        caller_background[0] = 0.3
        expect(shared.wait(entry.apply(capture(), 0)), (0.4, 0.1, 0.02))
        mutated = shared.diagnostics()
        assert mutated["material_builds"] == before["material_builds"] + 1
        assert mutated["light_updates"] == before["light_updates"] + 1
        assert mutated["background_builds"] == before["background_builds"] + 1
        background_scene = capture()
        background_scene["geometry"] = []
        expect(shared.wait(entry.apply(background_scene, 0)), (0.3, 0, 0))
        registry_snapshot = shared.diagnostics()
    assert shared.status()["device_allocation_bytes"] == 0
    reports.append(
        dict(
            device=device,
            checks=[
                "disabled collector",
                "unchanged radiance",
                "cold index byte count",
                "camera retains triangle buffers",
                "new shader attribute uploads",
                "geometry edit uploads",
                "generation reset",
                "nonmutating snapshot",
                "tracked shutdown",
                "unchanged shader state retained",
                "mutable registry inputs update material/light/background",
            ],
            cold=cold,
            camera=moved_camera,
            attribute=attribute,
            geometry=changed_geometry,
            final=final,
            registry=registry_snapshot,
        )
    )
report = dict(
    result="passed",
    devices=reports,
    scope="Nested engine wall timers and logical host-backed buffer-copy requests. "
    "No physical PCIe/VRAM, constant-memory, backend-internal or device-only transfer claim.",
)
(destination / "houdini-diagnostics-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
