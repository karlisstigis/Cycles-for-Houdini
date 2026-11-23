# SPDX-License-Identifier: Apache-2.0
"""Owned sparse grids with independent Beer-Lambert and projection expectations."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
from array import array
import json
import math
import os
from pathlib import Path
import subprocess

import hou
import cycles_houdini as ch

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage2/volume"
destination.mkdir(parents=True, exist_ok=True)
fixture = destination / "density.vdb"
subprocess.run([str(runtime_executable("ch_harness.exe")), "--write-volume-fixture", str(fixture)], check=True)
geo = hou.node("/obj").createNode("geo", "volume_fixture", run_init_scripts=False)
source = geo.createNode("file")
source.parm("file").set(str(fixture))
# Stash retains the host grid. Remove the source file before rendering: the
# native adapter and engine must use the in-memory grid, without a file loader.
stash = geo.createNode("stash")
stash.parm("stash").set(source.geometry())
primitive = stash.geometry().prims()[0]
assert primitive.attribValue("name") == "density" and primitive.activeVoxelCount() == 729
assert tuple(primitive.voxelSize()) == (0.125, 0.125, 0.125)
assert primitive.voxel((0, 0, 0)) == 1 and primitive.voxel((5, 0, 0)) == 0
assert tuple(primitive.indexToPos((4, 0, 0))) == (0.5, 0, 0)
fixture.unlink()
source.destroy()
camera = hou.node("/obj").createNode("cam", "volume_camera")
camera.parm("tz").set(5)
camera.parm("projection").set(1)
camera.parm("orthowidth").set(4)
wrangle = geo.createNode("volumewrangle")
wrangle.setInput(0, stash)
wrangle.parm("snippet").set("@density *= 0.5;")
shift = geo.createNode("xform")
shift.setInput(0, stash)
shift.parm("tx").set(0.8)
rename = geo.createNode("python")
rename.setInput(0, stash)
rename.parm("python").set("import hou\nhou.pwd().geometry().prims()[0].setAttribValue('name', 'missing_density')")
point_shift = geo.createNode("python")
point_shift.setInput(0, stash)
point_shift.parm("python").set(
    "import hou\nfor point in hou.pwd().geometry().points(): point.setPosition(point.position() + hou.Vector3(0.8, 0, 0))"
)


def transmission(frame, world_x=0):
    values = array("f")
    values.frombytes(frame["planes"]["combined"]["pixels"])
    x = round((world_x / 4 + 0.5) * frame["width"])
    y = frame["height"] // 2
    channels = [
        [values[((y + dy) * frame["width"] + x + dx) * 4 + c] for dy in range(-3, 3) for dx in range(-3, 3)]
        for c in range(3)
    ]
    return [sum(channel) / len(channel) for channel in channels]


def check(frame, optical_depth, world_x=0):
    expected = math.exp(-optical_depth)
    measured = transmission(frame, world_x)
    # Extinction-only paths use stochastic transmission. Average 36 pixels,
    # each with 512 samples; tolerance includes sampling and grid precision.
    assert all(abs(value - expected) < 0.02 for value in measured), (expected, measured)
    return dict(optical_depth=optical_depth, expected=expected, measured=measured)


reports = []
for device in ("CPU", ch.default_device()):
    with ch.Session(device=device, width=80, height=48, samples=512) as session:
        request = ch.geometry_request(geo, stash, material="absorb")
        material = dict(key="absorb", kind="volume_absorption", color=(0, 0, 0), density=1)
        frame = session.wait(
            session.apply(
                dict(geometry=[request], camera_path=camera.path(), background=(1, 1, 1), materials=[material])
            )
        )
        initial = session.status()
        checks = [check(frame, 9 / 8)]
        assert initial["geometry_resources"] == initial["object_resources"] == 1
        assert initial["owned_geometry_bytes"] > 0
        session.write_exr(destination / ("cpu.exr" if device == "CPU" else "optix.exr"))

        session.wait(session.apply(dict(camera_path=camera.path(), geometry=[request])))
        assert session.status()["geometry_conversions"] == initial["geometry_conversions"]
        assert session.status()["extractions"] == initial["extractions"]
        assert session.status()["object_updates"] == initial["object_updates"]
        frame = session.wait(session.apply(dict(materials=[dict(material, density=2)])))
        checks.append(check(frame, 9 / 4))
        assert session.status()["geometry_conversions"] == initial["geometry_conversions"]
        session.wait(session.apply(dict(materials=[material])))

        # Density is a coefficient per world-space length: doubling the
        # object's scale doubles optical depth, without copying the grid.
        geo.parm("scale").set(2)
        frame = session.wait(session.apply(dict(geometry=[ch.geometry_request(geo, stash, material="absorb")])))
        checks.append(check(frame, 9 / 4))
        assert session.status()["geometry_conversions"] == initial["geometry_conversions"]
        geo.parm("scale").set(1)

        # SOP translation lives in the grid transform, independently of OBJ.
        shifted = ch.geometry_request(geo, shift, material="absorb")
        frame = session.wait(session.apply(dict(geometry=[shifted])))
        checks.append(check(frame, 9 / 8, 0.8))
        checks.append(check(frame, 0, -0.5))
        assert abs(shift.geometry().prims()[0].indexToPos((0, 0, 0))[0] - 0.8) < 1e-6
        assert session.status()["geometry_resources"] == 1
        shift.parm("tx").set(-0.8)
        frame = session.wait(session.apply(dict(geometry=[shifted])))
        checks.append(check(frame, 9 / 8, -0.8))
        checks.append(check(frame, 0, 0.5))
        shift.parmTuple("s").set((1, 1, 2))
        frame = session.wait(session.apply(dict(geometry=[shifted])))
        checks.append(check(frame, 9 / 4, -0.8))
        shift.parmTuple("s").set((1, 1, 1))
        shift.parm("tx").set(0.8)
        point_request = ch.geometry_request(geo, point_shift, material="absorb")
        frame = session.wait(session.apply(dict(geometry=[point_request])))
        checks.append(check(frame, 9 / 8, 0.8))
        point_shift.parm("python").set(point_shift.parm("python").eval().replace("0.8", "-0.8"))
        frame = session.wait(session.apply(dict(geometry=[point_request])))
        checks.append(check(frame, 9 / 8, -0.8))
        point_shift.parm("python").set(point_shift.parm("python").eval().replace("-0.8", "0.8"))

        edited = ch.geometry_request(geo, wrangle, material="absorb")
        frame = session.wait(session.apply(dict(geometry=[edited])))
        assert wrangle.geometry().prims()[0].voxel((0, 0, 0)) == 0.5
        checks.append(check(frame, 9 / 16))
        # Mutate a SOP while rendering consumes its previous owned snapshot.
        wrangle.parm("snippet").set("@density *= 0.25;")
        frame = session.wait(session.apply(dict(geometry=[edited])))
        checks.append(check(frame, 9 / 32))
        wrangle.parm("snippet").set("@density *= 0.00025;")
        frame = session.wait(session.apply(dict(geometry=[edited], materials=[dict(material, density=2000)])))
        checks.append(check(frame, 9 / 16))
        wrangle.parm("snippet").set("@density *= 0.25;")
        session.wait(session.apply(dict(geometry=[edited], materials=[material])))

        recovery = []
        for kind in ("diffuse", "principled", "emission", "diagnostic_surface"):
            before = session.status()
            frame = session.wait(
                session.apply(dict(materials=[dict(material, kind=kind, color=(1, 0, 1), strength=20)]))
            )
            state = session.status()
            assert state["state"] == "complete" and not state["error"], state
            assert state["geometry_resources"] == state["object_resources"] == 1, state
            assert state["geometry_conversions"] == before["geometry_conversions"], state
            issues = state["source_diagnostics"]
            assert len(issues) == 1 and issues[0]["code"] == "volume.shader_domain_unavailable", issues
            measured = transmission(frame)
            assert all(math.isfinite(value) for value in measured), measured
            assert min(measured[0], measured[2]) > measured[1] + 0.05, measured
            # Scattering fallback uses the captured density, never the invalid
            # surface's emission. A dark world must remain dark.
            dark = session.wait(session.apply(dict(background=(0, 0, 0))))
            assert max(abs(value) for value in transmission(dark)) < 1e-6
            assert session.status()["source_diagnostics"] == issues
            frame = session.wait(session.apply(dict(materials=[material], background=(1, 1, 1))))
            assert not session.status()["source_diagnostics"]
            assert session.status()["geometry_conversions"] == before["geometry_conversions"]
            repaired = check(frame, 9 / 32)
            recovery.append(dict(kind=kind, measured=measured, diagnostics=issues, repaired=repaired))

        # Removing a resource still referenced by geometry is an engine ownership
        # error, distinct from a deleted native shader assignment. Worker apply
        # is asynchronous: observe its rejection through wait, then repair it.
        previous = session.status()
        try:
            session.wait(session.apply(dict(deleted_materials=["absorb"])))
        except RuntimeError as error:
            assert "Material is still bound" in str(error), str(error)
        else:
            raise AssertionError("Invalid volume ownership transaction was accepted")
        rejected = session.status()
        committed_key = "committed_generation" if "committed_generation" in previous else "generation"
        assert rejected[committed_key] == previous[committed_key], (previous, rejected)
        assert rejected["owned_geometry_bytes"] == previous["owned_geometry_bytes"]
        session.wait(session.apply(dict(materials=[material])))
        session.wait(session.apply(dict(materials=[dict(material, kind="principled_volume", blackbody_intensity=1)])))
        assert any(d["code"] == "volume.shader_field_unavailable" for d in session.status()["source_diagnostics"])
        frame = session.wait(session.apply(dict(geometry=[edited], materials=[material])))
        assert not session.status()["source_diagnostics"]
        checks.append(check(frame, 9 / 32))

        allocations = []
        for iteration in range(12):
            wrangle.parm("snippet").set("@density *= %s;" % (0.5 if iteration % 2 else 0.25))
            session.wait(session.apply(dict(geometry=[edited])))
            allocations.append(session.status()["device_allocation_bytes"])
        assert len(set(allocations[4:])) == 1, allocations
        session.wait(session.apply(dict(geometry=[])))
        assert session.status()["geometry_resources"] == session.status()["object_resources"] == 0
        assert session.status()["owned_geometry_bytes"] == 0
        session.wait(session.apply(dict(geometry=[request])))
        assert session.status()["geometry_resources"] == 1
        reports.append(
            dict(
                device=device,
                initial=initial,
                checks=checks,
                edit_allocations=allocations,
                recovered_materials=recovery,
                failed_transactions=1,
                final=session.status(),
            )
        )
        wrangle.parm("snippet").set("@density *= 0.5;")
    assert session.status()["device_allocation_bytes"] == 0

report = dict(
    result="passed",
    fixtures=reports,
    source_file_removed_before_render=True,
    scope="Scalar sparse fog, uniform absorption, grid/OBJ transforms and owned snapshots; full pyro/motion certification remains pending",
    memory_scope="Owned OpenVDB trees and Cycles tracked allocations; not a physical VRAM or production pyro benchmark",
)
(destination / "houdini-volume-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(destination / "houdini-volume-smoke.json")
