# SPDX-License-Identifier: Apache-2.0
"""Multiple in-memory VDB fields, emission integral and temperature controls."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
from cycles_houdini.device_selection import set_devices as set_rop_devices
from array import array
import json
import math
import os
from pathlib import Path
import subprocess
import hou
import cycles_houdini as ch
from cycles_houdini import native_scene

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage2/pyro"
destination.mkdir(parents=True, exist_ok=True)
fixture = destination / "fields.vdb"
subprocess.run([str(runtime_executable("ch_harness.exe")), "--write-pyro-fixture", str(fixture)], check=True)
geo = hou.node("/obj").createNode("geo", "pyro_fixture", run_init_scripts=False)
source = geo.createNode("file")
source.parm("file").set(str(fixture))
stash = geo.createNode("stash")
stash.parm("stash").set(source.geometry())
source.destroy()
fixture.unlink()
fields = {primitive.attribValue("name"): primitive for primitive in stash.geometry().prims()}
assert set(fields) == {"density", "temperature", "emission", "velocity"}
assert fields["temperature"].voxel((0, 0, 0)) == 2 and fields["emission"].voxel((0, 0, 0)) == 0.25
assert tuple(fields["velocity"].samplev((0, 0, 0))) == (0.25, 0.5, -0.125)
camera = hou.node("/obj").createNode("cam")
camera.parm("tz").set(5)
camera.parm("projection").set(1)
camera.parm("orthowidth").set(4)
rop = hou.node("/out").createNode("cycles_ipr")
for name, value in dict(
    soppath=stash.path(),
    camera=camera.path(),
    resx=80,
    resy=48,
    samples=512,
    material_kind="principled_volume",
    density=0,
    volume_emission_strength=2,
    emission_field="emission",
).items():
    rop.parm(name).set(value)
rop.parmTuple("background").set((0, 0, 0))
rop.parmTuple("volume_emission_color").set((1, 0.5, 0.25))
bad_type = geo.createNode("python", "invalid_density_type")
bad_type.setInput(0, stash)
bad_type.parm("python").set("""import hou
for primitive in hou.pwd().geometry().prims():
    name = primitive.attribValue('name')
    if name == 'density': primitive.setAttribValue('name', 'previous_density')
    elif name == 'velocity': primitive.setAttribValue('name', 'density')
""")
low_field = geo.createNode("volumewrangle", "low_value_emission")
low_field.setInput(0, stash)
low_field.parm("snippet").set("@emission *= 0.001;")


def mean(frame, outside=False):
    values = array("f")
    values.frombytes(frame["planes"]["combined"]["pixels"])
    x = 5 if outside else 40
    return [
        sum(values[((24 + dy) * 80 + x + dx) * 4 + c] for dy in range(-3, 3) for dx in range(-3, 3)) / 36
        for c in range(3)
    ]


reports = []
for device in ("CPU", ch.default_device()):
    set_rop_devices(rop, device)
    with ch.Session(device=device, width=80, height=48, samples=512) as session:
        scene = native_scene.capture(rop)
        frame = session.wait(session.apply(scene))
        emission = mean(frame)
        expected = (9 / 16, 9 / 32, 9 / 64)
        assert all(abs(a - b) < 0.015 for a, b in zip(emission, expected)), (emission, expected)
        assert max(mean(frame, outside=True)) < 1e-6
        initial = session.status()
        material = scene["materials"][0]
        # A small nonzero emission grid remains meaningful when the artist
        # scales it. A loader's implicit density-style cutoff must not erase it.
        low_material = dict(material, volume_emission_strength=2000)
        frame = session.wait(
            session.apply(
                dict(geometry=[ch.geometry_request(geo, low_field, material=material["key"])], materials=[low_material])
            )
        )
        low_emission = mean(frame)
        assert all(abs(a - b) < 0.015 for a, b in zip(low_emission, expected)), (
            "Low-valued emission was lost",
            low_emission,
            expected,
        )
        session.wait(session.apply(scene))
        initial = session.status()
        # No emission field: strength is multiplied by density, including the
        # voxel boundary interpolation, rather than emitting throughout padding.
        frame = session.wait(session.apply(dict(materials=[dict(material, emission_field="")])))
        density_emission = mean(frame)
        density_expected = tuple(4 * x for x in expected)
        assert all(abs(a - b) < 0.03 for a, b in zip(density_emission, density_expected)), (
            density_emission,
            density_expected,
        )
        blackbody = dict(material, volume_emission_strength=0, blackbody_intensity=1, temperature_kelvin_scale=1000)
        frame = session.wait(session.apply(dict(materials=[blackbody])))
        warm = mean(frame)
        assert warm[0] > warm[1] > warm[2] >= 0 and all(math.isfinite(x) for x in warm), warm
        frame = session.wait(session.apply(dict(materials=[dict(blackbody, temperature_kelvin_scale=1500)])))
        hot = mean(frame)
        assert sum(hot) > 2 * sum(warm), (warm, hot)
        assert session.status()["geometry_conversions"] == initial["geometry_conversions"], (
            "Material edit copied the sparse grids"
        )
        session.write_exr(destination / ("cpu.exr" if device == "CPU" else "optix.exr"))
        session.wait(session.apply(dict(materials=[dict(material, emission_field="missing")])))
        missing = session.status()
        assert any(
            issue["code"] == "volume.shader_field_unavailable" and "missing" in issue["message"]
            for issue in missing["source_diagnostics"]
        ), missing
        assert missing["geometry_conversions"] == initial["geometry_conversions"]
        session.wait(
            session.apply(
                dict(geometry=[ch.geometry_request(geo, bad_type, material=material["key"])], materials=[material])
            )
        )
        invalid = session.status()
        assert any(issue["code"] == "volume.density_unavailable" for issue in invalid["source_diagnostics"]), invalid
        repaired = mean(session.wait(session.apply(scene)))
        assert not session.status()["source_diagnostics"]
        assert all(abs(a - b) < 0.015 for a, b in zip(repaired, expected)), repaired
        reports.append(
            dict(
                device=device,
                emission=emission,
                expected=list(expected),
                low_value_emission=low_emission,
                density_emission=density_emission,
                blackbody_2000K=warm,
                blackbody_3000K=hot,
                initial=initial,
                final=session.status(),
            )
        )
    assert session.status()["owned_geometry_bytes"] == session.status()["device_allocation_bytes"] == 0
    # Enabling blackbody introduces a new requested attribute. Compare the
    # edited shader against a fresh session to detect stale attribute maps.
    with ch.Session(device=device, width=80, height=48, samples=512) as fresh:
        fresh_scene = native_scene.capture(rop)
        fresh_scene["materials"] = [dict(blackbody, temperature_kelvin_scale=1500)]
        fresh_hot = mean(fresh.wait(fresh.apply(fresh_scene)))
        # These means originate in float32 HDR accumulation buffers. A fixed
        # absolute threshold below one representable step at these magnitudes
        # rejects equivalent GPU results. Keep a small absolute floor and four
        # float32 epsilons of relative tolerance; the analytic checks above
        # independently retain their emission and temperature requirements.
        relative_tolerance, absolute_tolerance = 4 * 2**-23, 1e-7
        errors = [abs(a - b) for a, b in zip(fresh_hot, hot)]
        limits = [max(absolute_tolerance, relative_tolerance * max(abs(a), abs(b))) for a, b in zip(fresh_hot, hot)]
        assert all(
            math.isclose(a, b, rel_tol=relative_tolerance, abs_tol=absolute_tolerance) for a, b in zip(fresh_hot, hot)
        ), ("Blackbody edit differs from fresh scene", device, fresh_hot, hot, errors, limits)
        reports[-1]["fresh_blackbody_3000K"] = fresh_hot
        reports[-1]["fresh_comparison"] = dict(
            absolute_errors=errors,
            limits=limits,
            relative_tolerance=relative_tolerance,
            absolute_tolerance=absolute_tolerance,
        )

# The asynchronous artist IPR must publish the same recoverable diagnostic
# without replacing the worker or re-uploading the grids.
from cycles_houdini import registry

entry = registry.for_rop(rop, native_scene.configuration(rop))
entry.session.wait(entry.apply(native_scene.capture(rop), hou.time()))
before = entry.session.status()
candidate = entry.apply(
    dict(materials=[dict(native_scene.material_definition(rop), emission_field="missing")]), hou.time()
)
entry.session.wait(candidate)
fallback = entry.session.status()
assert fallback["committed_generation"] == candidate and not fallback.get("update_rejected"), fallback
assert any(
    issue["code"] == "volume.shader_field_unavailable" and "missing" in issue["message"]
    for issue in fallback["source_diagnostics"]
), fallback
for name in (
    "renderer_pid",
    "backend_epoch",
    "geometry_conversions",
    "geometry_resources",
    "object_resources",
    "owned_geometry_bytes",
):
    assert fallback[name] == before[name], (name, before, fallback)
assert entry.session.frame_info()["generation"] == candidate
assert entry.session.diagnostics()["session_cancel_calls"] == 0
entry.session.wait(entry.apply(native_scene.capture(rop), hou.time()))
recovered = entry.session.status()
assert not entry.error and not recovered["update_rejected"] and len(registry.entries()) == 1
assert not recovered["source_diagnostics"]
assert recovered["renderer_pid"] == before["renderer_pid"] and recovered["backend_epoch"] == before["backend_epoch"]
registry.close_all()

# Save an editable, original native example with embedded sparse grids. Indie
# creates .hiplc; this is development evidence, not a commercial .hip claim.
bad_type.destroy()
low_field.destroy()
stash.setDisplayFlag(True)
stash.setRenderFlag(True)
geo.layoutChildren()
set_rop_devices(rop, ch.default_device())
rop.parm("volume_emission_strength").set(0)
rop.parm("blackbody_intensity").set(1)
rop.parm("temperature_kelvin_scale").set(1500)
hou.hipFile.save(str(destination / "native-pyro-fields.hiplc"))
report = dict(
    result="passed",
    fixtures=reports,
    source_file_removed_before_render=True,
    scope="Sparse density, scalar emission, temperature and vector velocity storage; volume velocity motion has separate regressions, and production pyro sequences remain pending",
)
(destination / "houdini-pyro-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(destination / "houdini-pyro-smoke.json")
