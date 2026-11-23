# SPDX-License-Identifier: Apache-2.0
"""Native volume shutter timing and sparse field ownership with analytic references."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
from cycles_houdini.device_selection import set_devices as set_rop_devices
from array import array
import json
import math
import os
from pathlib import Path
import subprocess
import time

import hou
import cycles_houdini as ch

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage2/motion/volume"
destination.mkdir(parents=True, exist_ok=True)
harness = runtime_executable("ch_harness.exe")
subprocess.run([str(harness), "--write-volume-motion-fixtures", str(destination)], check=True)
hou.setFps(24)
hou.setFrame(1)
geo = hou.node("/obj").createNode("geo", "volume_motion_fixture", run_init_scripts=False)
sources = {}
for filename in ("vector", "slow", "reverse", "pyro", "affine", "components", "components-mismatch", "nonfinite"):
    path = destination / f"{filename}.vdb"
    file = geo.createNode("file")
    file.parm("file").set(str(path))
    stash = geo.createNode("stash", filename.replace("-", "_"))
    stash.parm("stash").set(file.geometry())
    sources[filename] = stash
    file.destroy()
    path.unlink()
source = sources["vector"]
camera = hou.node("/obj").createNode("cam", "volume_motion_camera")
camera.parm("tz").set(5)
camera.parm("projection").set(1)
camera.parm("orthowidth").set(4)
camera.parm("shutter").set(0.5)
missing = geo.createNode("python", "missing_velocity")
missing.setInput(0, source)
missing.parm("python").set(
    "import hou\ng=hou.pwd().geometry()\ng.deletePrims([p for p in g.prims() if p.attribValue('name') == 'vel'])"
)
ambiguous = geo.createNode("merge", "ambiguous_velocity")
ambiguous.setInput(0, source)
ambiguous.setInput(1, sources["components"])
velocity_only = geo.createNode("python", "velocity_components_only")
velocity_only.setInput(0, sources["components"])
velocity_only.parm("python").set(
    "import hou\ng=hou.pwd().geometry()\ng.deletePrims([p for p in g.prims() if p.attribValue('name') == 'density'])"
)
ambiguous_motion = geo.createNode("merge", "ambiguous_motion_only")
ambiguous_motion.setInput(0, source)
ambiguous_motion.setInput(1, velocity_only)
partial_motion = geo.createNode("python", "partial_velocity")
partial_motion.setInput(0, sources["components"])
partial_motion.parm("python").set(
    "import hou\ng=hou.pwd().geometry()\ng.deletePrims([p for p in g.prims() if p.attribValue('name').endswith('z')])"
)
wrangle = geo.createNode("volumewrangle", "density_edit")
wrangle.setInput(0, source)
wrangle.parm("snippet").set("@density *= 0.5;")


def motion(**changes):
    return dict(dict(enabled=True, fps=24.0, shutter_frames=0.5, offset=0.0, samples=3), **changes)


def scene(sop=source, settings=None, mode="velocity", now=0.0):
    return dict(
        geometry=[ch.geometry_request(geo, sop, material="absorb", motion=mode, at_time=now)],
        camera_path=camera.path(),
        background=(1, 1, 1),
        motion=motion() if settings is None else settings,
        materials=[dict(key="absorb", kind="volume_absorption", color=(0, 0, 0), density=1)],
    )


def reference(x, settings, speed=96, scale_x=1, scale_z=1, density=1):
    duration = settings["shutter_frames"] / settings["fps"] if settings["enabled"] else 0
    time_reference = (1 - settings["offset"]) / 2
    total = 0
    for step in range(2048):
        sample_x = abs(x / scale_x - speed * duration * ((step + 0.5) / 2048 - time_reference))
        profile = 1 if sample_x <= 0.5 else max(0, (0.625 - sample_x) / 0.125)
        total += math.exp(-1.125 * abs(scale_z) * density * profile)
    return total / 2048


def check(frame, name, settings=None, world_x=0, emission_ratio=0, **changes):
    settings = settings or motion()
    values = array("f")
    values.frombytes(frame["planes"]["combined"]["pixels"])
    assert all(math.isfinite(value) for value in values), name
    centre = round((world_x / 4 + 0.5) * frame["width"])
    middle = frame["height"] // 2
    measured = expected = 0
    for y in range(middle - 2, middle + 2):
        for x in range(centre - 2, centre + 2):
            measured += values[(y * frame["width"] + x) * 4]
            transmission = reference((x + 0.5) * 4 / frame["width"] - 2, settings, **changes)
            expected += transmission + emission_ratio * (1 - transmission)
    expected /= 16
    measured /= 16
    assert abs(measured - expected) < 0.025, (name, measured, expected)
    return dict(name=name, measured=measured, analytic=expected, tolerance=0.025)


reports = []
for device in ("CPU", ch.default_device()):
    started = time.monotonic()
    core_output = destination / ("core-cpu" if device == "CPU" else "core-optix")
    run = subprocess.run(
        [
            str(harness),
            "--resources",
            os.environ["CYCLES_HOUDINI_RESOURCES"],
            "--device",
            device,
            "--volume-motion-test",
            "--output",
            str(core_output),
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert run.returncode == 0, (device, run.returncode, run.stdout, run.stderr)
    core_events = [json.loads(line) for line in run.stdout.splitlines() if line.startswith("{")]
    assert core_events[-1]["result"] == "passed" and len([item for item in core_events if "check" in item]) == 9
    geo.parmTuple("s").set((1, 1, 1))
    geo.parm("tx").set(0)
    camera.parm("tx").set(0)
    with ch.Session(device=device, width=96, height=64, samples=512) as session:
        checks = []
        frame = session.wait(session.apply(scene(), at_time=0))
        checks.append(check(frame, "Centred volume velocity"))
        initial = session.status()
        assert initial["volume_ray_marching"]
        session.wait(
            session.apply(dict(geometry=scene()["geometry"], camera_path=camera.path(), motion=motion()), at_time=0)
        )
        assert session.status()["geometry_conversions"] == initial["geometry_conversions"]
        assert session.status()["extractions"] == initial["extractions"]
        session.wait(session.apply(scene(now=0.125), at_time=0.125))
        assert hou.time() == 0 and session.status()["geometry_conversions"] == initial["geometry_conversions"]
        checks.append(dict(name="Camera and absolute-time edits retain static owned grids"))
        for offset in (-1, 1, 0.5):
            settings = motion(offset=offset)
            frame = session.wait(session.apply(scene(settings=settings), at_time=0))
            checks.append(check(frame, f"Shutter offset {offset}", settings, world_x=offset))
            if offset == 1:
                checks.append(check(frame, "Unclipped fast trail", settings, world_x=1.75))
                session.write_exr(destination / ("native-cpu.exr" if device == "CPU" else "native-optix.exr"))
        for settings in (motion(shutter_frames=0.25), motion(fps=48), motion(samples=5)):
            checks.append(
                check(session.wait(session.apply(scene(settings=settings), at_time=0)), f"Timing {settings}", settings)
            )
        for settings in (motion(enabled=False), motion(shutter_frames=0)):
            checks.append(
                check(
                    session.wait(session.apply(scene(settings=settings), at_time=0)), f"Disabled {settings}", settings
                )
            )
            assert not session.status()["volume_ray_marching"]
        frame = session.wait(session.apply(scene(), at_time=0))
        conversions = session.status()["geometry_conversions"]
        checks.append(
            check(
                session.wait(session.apply(dict(motion_blur=False))),
                "Core disable retains velocity fields",
                motion(enabled=False),
            )
        )
        assert not session.status()["volume_ray_marching"]
        checks.append(
            check(session.wait(session.apply(dict(motion_blur=True))), "Core re-enable retains velocity fields")
        )
        assert session.status()["volume_ray_marching"]
        assert session.status()["geometry_conversions"] == conversions
        for key, speed in (("slow", 48), ("reverse", -96), ("components", 96)):
            checks.append(
                check(
                    session.wait(session.apply(scene(sources[key]), at_time=0)), f"{key} velocity fields", speed=speed
                )
            )
        affine_settings = motion(offset=1)
        checks.append(
            check(
                session.wait(session.apply(scene(sources["affine"], affine_settings), at_time=0)),
                "Distinct affine field maps with a fast trail",
                affine_settings,
                world_x=1.75,
                scale_z=2,
            )
        )
        pyro_scene = scene(sources["pyro"])
        pyro_scene["materials"] = [
            dict(
                key="absorb",
                kind="principled_volume",
                color=(0, 0, 0),
                density=1,
                emission_field="emission",
                volume_emission_strength=2,
                volume_emission_color=(1, 1, 1),
            )
        ]
        checks.append(
            check(
                session.wait(session.apply(pyro_scene, at_time=0)),
                "Advected emission and extinction",
                emission_ratio=0.5,
            )
        )
        checks.append(
            check(
                session.wait(session.apply(scene(wrangle), at_time=0)), "Density field edit during motion", density=0.5
            )
        )
        # OBJ scale changes world extinction length and velocity, without
        # replacing the owned grid. Include a negative axis scale.
        frame = session.wait(session.apply(scene(), at_time=0))
        conversions = session.status()["geometry_conversions"]
        geo.parmTuple("s").set((2, 0.5, 2))
        checks.append(
            check(session.wait(session.apply(scene(), at_time=0)), "Nonuniform OBJ scale", scale_x=2, scale_z=2)
        )
        geo.parmTuple("s").set((-2, 0.5, 2))
        checks.append(
            check(session.wait(session.apply(scene(), at_time=0)), "Negative OBJ scale", scale_x=-2, scale_z=2)
        )
        assert session.status()["geometry_conversions"] == conversions
        geo.parmTuple("s").set((1, 1, 1))
        geo.parm("tx").setExpression("$T * 48", hou.exprLanguage.Hscript)
        camera.parm("tx").setExpression("$T * 48", hou.exprLanguage.Hscript)
        checks.append(check(session.wait(session.apply(scene(), at_time=0)), "Matching object/camera transform motion"))
        geo.parm("tx").deleteAllKeyframes()
        geo.parm("tx").set(0)
        camera.parm("tx").deleteAllKeyframes()
        camera.parm("tx").set(0)
        checks.append(
            check(
                session.wait(session.apply(scene(missing), at_time=0)),
                "Missing volume velocity renders static density",
                motion(enabled=False),
            )
        )
        assert not session.status()["volume_ray_marching"]
        checks.append(
            check(session.wait(session.apply(scene(), at_time=0)), "Restoring volume velocity restores advection")
        )
        assert session.status()["volume_ray_marching"]
        for sop, message in (
            (sources["components-mismatch"], "transforms must match"),
            (sources["nonfinite"], "finite values"),
            (ambiguous_motion, "without ambiguity"),
            (partial_motion, "all three scalar components"),
        ):
            checks.append(
                check(
                    session.wait(session.apply(scene(sop), at_time=0)),
                    "Invalid " + sop.name() + " velocity renders static density",
                    motion(enabled=False),
                )
            )
            assert any(message in warning for warning in session.status()["motion_warnings"])
            assert not session.status()["volume_ray_marching"]
        checks.append(check(session.wait(session.apply(scene(), at_time=0)), "Repaired volume velocity restores blur"))
        assert not session.status()["motion_warnings"]
        checks.append(
            check(
                session.wait(session.apply(scene(ambiguous), at_time=0)),
                "Duplicate density retains the first field",
                motion(enabled=False),
            )
        )
        assert any(d["code"] == "volume.field_ignored" for d in session.status()["source_diagnostics"])
        generation = session.status()["generation"]
        checks.append(
            check(
                session.wait(session.apply(scene(mode="deformation"), at_time=0)),
                "Unsupported field interpolation renders static density",
                motion(enabled=False),
            )
        )
        assert any(d["code"] == "motion.unsupported_method" for d in session.status()["source_diagnostics"])
        generation = session.status().get("committed_generation", session.status()["generation"])
        invalid_step = scene()
        invalid_step["geometry"][0]["volume_step_size"] = -1
        try:
            session.apply(invalid_step, at_time=0)
        except RuntimeError as error:
            assert "Volume step size" in str(error)
        else:
            raise AssertionError("Negative volume sampling step accepted")
        assert session.status().get("committed_generation", session.status()["generation"]) == generation
        checks.append(dict(name="Rejected invalid ray-march step before scene mutation"))
        checks.append(check(session.wait(session.apply(scene(), at_time=0)), "Recovery after invalid fields"))
        # A different sampling step is a real per-geometry state variant, even
        # when two objects refer to one packed/owned source.
        variants = scene()
        other = dict(variants["geometry"][0], object_key="volume-step-variant", volume_step_size=0.03125)
        variants["geometry"].append(other)
        session.wait(session.apply(variants, at_time=0))
        assert session.status()["geometry_resources"] == 2
        checks.append(dict(name="Different volume steps create explicit geometry variants"))
        variants["geometry"][1]["volume_step_size"] = 0
        session.wait(session.apply(variants, at_time=0))
        assert session.status()["geometry_resources"] == 1
        checks.append(dict(name="Compatible volume steps share an owned geometry"))
        # Rigid packed motion is independent of field advection. The existing
        # density fixture without vel gives a separately known moving medium.
        packed = geo.createNode("pack", "packed_density")
        packed.setInput(0, missing)
        packed.parm("pivot").set("origin")
        packed_v = geo.createNode("attribwrangle", "packed_density_velocity")
        packed_v.setInput(0, packed)
        for value in ("96", 'atof("nan")', "96"):
            packed_v.parm("snippet").set("v@v=set(" + value + ",0,0);")
            frame = session.wait(session.apply(scene(packed_v), at_time=0))
            status = session.status()
            broken = "nan" in value
            checks.append(
                check(
                    frame, "Packed density velocity " + ("fallback" if broken else "valid"), motion(enabled=not broken)
                )
            )
            assert status["geometry_resources"] == status["object_resources"] == 1 and status["motion_cooks"] == 0, (
                status
            )
            assert bool(status["source_diagnostics"]) == broken, status
        frame = session.wait(session.apply(scene(packed), at_time=0))
        checks.append(check(frame, "Packed density without v stays static", motion(enabled=False)))
        assert not session.status()["source_diagnostics"]
        session.wait(session.apply(scene(), at_time=0))
        allocations = []
        conversions = session.status()["geometry_conversions"]
        for repeat in range(6):
            session.wait(session.apply(scene(), at_time=0))
            allocations.append(session.status()["device_allocation_bytes"])
        assert len(set(allocations)) == 1 and session.status()["geometry_conversions"] == conversions
        reports.append(
            dict(
                device=device,
                checks=checks,
                core_checks=core_events,
                retained_allocations=allocations,
                initial=initial,
                final=session.status(),
                test_seconds=time.monotonic() - started,
                samples=512,
            )
        )
        print(json.dumps(dict(device=device, native_checks=len(checks), core_checks=9, result="passed")), flush=True)
    deadline = time.monotonic() + 20
    while session.status()["state"] != "closed" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert session.status()["state"] == "closed", session.status()
    assert session.status()["owned_geometry_bytes"] == session.status()["device_allocation_bytes"] == 0

from cycles_houdini import native_scene

rop = hou.node("/out").createNode("cycles_ipr", "volume_velocity_cycles")
rop.parm("soppath").set(source.path())
rop.parm("camera").set(camera.path())
set_rop_devices(rop, ch.default_device())
rop.parm("samples").set(1024)
rop.parm("material_kind").set("volume_absorption")
rop.parmTuple("basecolor").set((0, 0, 0))
rop.parmTuple("background").set((1, 1, 1))
rop.parm("allowmotionblur").set(1)
rop.parm("geometry_motion").set("velocity")
rop.parm("shutteroffset").set(1)
rop.parm("resx").set(96)
rop.parm("resy").set(64)
assert native_scene.capture(rop)["motion"]["offset"] == 1
source.setDisplayFlag(True)
source.setRenderFlag(True)
geo.layoutChildren()
hou.hipFile.save(str(destination / "native-volume-velocity.hiplc"))
report = dict(
    result="passed",
    devices=reports,
    scope="Owned nominal sparse fields with velocity in SOP units/second; explicit normalized shutter reference, conservative bounds, upstream ray marcher",
)
(destination / "houdini-volume-motion-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(
    json.dumps(
        dict(
            result="passed",
            checks=[len(item["checks"]) for item in reports],
            evidence=str(destination / "houdini-volume-motion-smoke.json"),
        )
    )
)
