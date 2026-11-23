# SPDX-License-Identifier: Apache-2.0
"""Named UV bases, independent motion images and live source repairs."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
from subdivision_test_setup import configure_render, set_subdivision
import pass_fixtures as pass_fixture

import argparse, json, os, subprocess, time, traceback
from pathlib import Path
import hou
import numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import native_scene, registry
from tangent_basis_fixture import Fixture, CASES

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
f = Fixture(device)
authoring = f.authoring_controls()
refs = f.dest / ("reference-cpu" if device == "CPU" else "reference-optix")
run = subprocess.run(
    [
        str(runtime_executable("ch_harness.exe")),
        "--resources",
        os.environ["CYCLES_HOUDINI_RESOURCES"],
        "--device",
        device,
        "--tangent-reference",
        "--output",
        str(refs),
    ],
    capture_output=True,
    text=True,
    timeout=240,
)
assert run.returncode == 0, (run.stdout, run.stderr)
contracts = json.loads(run.stdout.strip().splitlines()[-1])
assert (
    contracts["generated_name_collision"]
    and contracts["independent_tangent_references"] == 32
    and contracts["recovery_reference_images"] == 28
    and contracts["held_uv_reference_images"] == 14
), contracts


def reference(case, steps, name="combined"):
    reader = oiio.ImageInput.open(str(refs / f"{case}-{steps}.exr"))
    assert reader
    spec = reader.spec()
    pixels = np.asarray(reader.read_image(), np.float32)
    reader.close()
    channels = list(spec.channelnames)
    names = (
        ("R", "G", "B") if name == "combined" else ("A",) if name == "alpha" else tuple(name + "." + c for c in "XYZ")
    )
    return np.flipud(pixels[:, :, [channels.index(c) for c in names]])


entry = registry.for_rop(f.rop, native_scene.configuration(f.rop))
checks = []


def apply():
    frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
    return frame, entry.session.status()


try:
    for case in CASES:
        f.configure(case)
        for steps in (3, 5):
            pass_fixture.parm(f.rop, "motion_samples").set(steps)
            frame, status = apply()
            baseline = f.check(frame, status)
            errors = {
                name: float(np.abs(baseline[name][:, :, :3] - reference(case, steps, name)).mean())
                for name in ("combined", "position", "normal")
            }
            assert errors["combined"] < 0.0015 and errors["normal"] < 0.0015, (case, steps, errors)
            visible = (
                (baseline["position"][:, :, 0] > 0.2)
                & (np.linalg.norm(baseline["normal"], axis=2) > 0.975)
                & (np.linalg.norm(reference(case, steps, "normal"), axis=2) > 0.975)
            )
            interior = visible.copy()
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    interior &= np.roll(visible, (dy, dx), (0, 1))
            assert interior.sum() > 30, (case, steps, int(interior.sum()))
            interior_error = float(np.abs(baseline["combined"][interior, :3] - reference(case, steps)[interior]).mean())
            assert interior_error < 0.003, (case, steps, interior_error)
            actual_position = baseline["position"][interior]
            expected_position = reference(case, steps, "position")[interior]
            delta = actual_position - expected_position
            position_error = np.abs(delta.mean(axis=0))
            position_bound = 0.0005 + 5 * delta.std(axis=0) / np.sqrt(interior.sum())
            assert np.all(position_error < position_bound), (case, steps, position_error, position_bound)
            checks.append(
                dict(
                    case=case,
                    samples=steps,
                    mean_differences=errors,
                    interior_color_difference=interior_error,
                    interior_position_difference=position_error.tolist(),
                    interior_position_tolerance=position_bound.tolist(),
                )
            )
        # Broken nominal fields and broken shutter-only samples are distinct.
        independent_rebuilds = status["displacement_rebuilds"]
        for shutter_only in (False, True):
            f.shutter_only.set(shutter_only)
            for broken in (1, 2, 3, 0):
                f.edit(broken)
                frame, status = apply()
                if not f.displacement_driven:
                    assert status["displacement_rebuilds"] == independent_rebuilds, (
                        case,
                        shutter_only,
                        broken,
                        independent_rebuilds,
                        status,
                    )
                compare = (
                    baseline if not broken or not shutter_only or f.displacement_driven or CASES[case][5] else None
                )
                planes = f.check(frame, status, broken, compare)
                if not f.displacement_driven:
                    # UV repair can change shader requirements and packed
                    # attribute offsets, but independent geometry stays exact.
                    for data_pass in ("position", "normal"):
                        np.testing.assert_allclose(
                            planes[data_pass],
                            baseline[data_pass],
                            rtol=0,
                            atol=3e-6,
                            err_msg=str((case, shutter_only, broken, data_pass)),
                        )
                if broken:
                    codes = {d["code"] for d in status["source_diagnostics"]}
                    if shutter_only:
                        assert "geometry.shutter_attribute" in codes, (case, codes)
                        assert not any(code.startswith("motion.displacement_") for code in codes), (case, codes)
                        if f.displacement_driven:
                            error = float(np.abs(planes["combined"][:, :, :3] - reference("held-uv-" + case, 5)).mean())
                            assert error < 0.0015, (case, "held UV with retained geometry motion", error)
                    else:
                        assert "material.missing_attribute" in codes, (case, codes)
                        if f.displacement_driven:
                            bare = reference("bare-" + case, 5, "position")
                            mask = (
                                (np.linalg.norm(planes["normal"], axis=2) > 0.975)
                                & (np.linalg.norm(reference("bare-" + case, 5, "normal"), axis=2) > 0.975)
                                & (planes["position"][:, :, 0] > 0.2)
                            )
                            delta = planes["position"][mask] - bare[mask]
                            difference = np.abs(delta.mean(axis=0))
                            bound = 0.0005 + 5 * delta.std(axis=0) / np.sqrt(mask.sum())
                            assert np.all(difference < bound), (case, "bare geometry recovery", difference, bound)
                        if f.visualized:
                            mask = (
                                (planes["position"][:, :, 0] > 0.75)
                                & (planes["position"][:, :, 0] < 1.25)
                                & (np.abs(planes["position"][:, :, 1]) < 0.2)
                                & (np.linalg.norm(planes["normal"], axis=2) > 0.97)
                            )
                            assert mask.sum() > 20, (case, broken, "No fully covered interior")
                            # Missing normal branches are disconnected, retaining
                            # the downstream visualizer's neutral 0.5 default.
                            expected = (0.5, 0.5, 0.5) if f.mode == "normal" else (1, 0, 1)
                            assert np.allclose(planes["combined"][mask, :3], expected, atol=2e-5), (
                                case,
                                broken,
                                planes["combined"][mask, :3].mean(axis=0),
                                expected,
                            )
            f.shutter_only.set(False)
        conversions = status["geometry_conversions"]
        pass_fixture.parm(f.camera, "tx").set(0.01)
        entry.session.wait(entry.apply(dict(camera_path=f.camera.path()), hou.time()))
        assert entry.session.status()["geometry_conversions"] == conversions
        pass_fixture.parm(f.camera, "tx").set(0)
    # Values in hidden, non-contributing UV controls remain ordinary saved
    # parameters. They must not create field demands or source warnings.
    for case, parameter, value in (
        ("tangent3", "direction_type", "radial"),
        ("normal_original", "space", "world"),
        ("displacement_both", "space", "object"),
    ):
        f.configure(case)
        pass_fixture.parm(f.source, parameter).set(value)
        f.map_parm.set("ignored_missing_map")
        f.source.updateParmStates()
        assert f.map_parm.isHidden()
        frame, status = apply()
        f.check(frame, status)
        checks.append(dict(check="Unused UV name is ignored", case=case, mode=value))
    # Both fields are already demanded by real AOVs. Changing the tangent's map
    # must generate the new basis even though host field capture is a cache hit.
    f.configure("tangent3")
    pass_fixture.parm(f.rop, "motion_samples").set(3)
    pass_fixture.parm(f.rop, "aov_count").set(2)
    for i, name in enumerate(("named_uv3", "named_uv2"), 1):
        configure_render(f.rop, {"aov_name" + str(i): "reserve" + str(i), "aov_type" + str(i): "color"})
        field = f.material.createNode("cycles::attribute::1", "reserved_field" + str(i))
        pass_fixture.parm(field, "attribute").set(name)
        writer = f.material.createNode("cycles::aov_output::1", "reserve_output" + str(i))
        pass_fixture.parm(writer, "name").set("reserve" + str(i))
        writer.setNamedInput("color", field, "vector")
    frame, status = apply()
    conversions = status["geometry_conversions"]
    f.map_parm.set("named_uv2")
    frame, status = apply()
    planes = f.check(frame, status)
    assert status["geometry_conversions"] == conversions, status
    assert float(np.abs(planes["combined"][:, :, :3] - reference("tangent2", 3)).mean()) < 0.0015
    checks.append(
        dict(
            check="Changing a tangent map generates the newly required basis from retained source without host conversion"
        )
    )
    pass_fixture.parm(f.rop, "aov_count").set(0)
    # Linear and Catmull-Clark dicing retain named float2/float3 UV fields.
    for scheme in ("linear", "catmull_clark"):
        configure_render(f.rop, dict(subdivision=dict(type=scheme, space="object", dicing_rate=0.1, max_level=3)))
        for case in ("tangent_uv", "normal_displaced", "displacement_both"):
            f.configure(case)
            frame, status = apply()
            subdiv_baseline = f.check(frame, status)
            for broken in (1, 0):
                f.edit(broken)
                frame, status = apply()
                f.check(frame, status, broken)
            # A separately authored default UV set with the same XY coordinates
            # checks named-map subdivision against the existing engine UV route.
            uv = "v@uv=v@named_uv3;" if f.field == "named_uv3" else "vector2 map=u@named_uv2; v@uv=set(map.x,map.y,0);"
            pass_fixture.parm(f.animate, "snippet").set(f.base_snippet.replace("v@uv=set(p.x+.5,p.y+.5,0);", uv))
            pass_fixture.parm(f.remove_default, "ptdel").set("")
            f.map_parm.set("uv")
            frame, status = apply()
            default = f.check(frame, status)
            error = float(np.abs(subdiv_baseline["combined"] - default["combined"]).mean())
            assert error < 0.0001, (scheme, case, error)
            checks.append(
                dict(
                    case=case,
                    subdivision=scheme,
                    triangles=status["subdivision_triangles"],
                    default_uv_difference=error,
                )
            )
    set_subdivision(f.rop, type="none")
    f.configure("displacement_both")
    warm = []
    for n in range(16):
        f.edit(n % 2)
        frame, status = apply()
        f.check(frame, status, n % 2)
        warm.append(
            (status["owned_geometry_bytes"], status["device_allocation_bytes"], status["engine_shader_resources"])
        )
    allocation_bounds = []
    for parity in (0, 1):
        states = warm[parity::2]
        limit = max(value[1] for value in states[:4])
        assert len({(value[0], value[2]) for value in states[4:]}) == 1, warm
        assert all(value[1] <= limit for value in states[4:]), warm
        allocation_bounds.append(
            dict(broken=parity == 1, warmed_limit=limit, measured=[value[1] for value in states[4:]])
        )
    f.configure("normal_original")
    with ch.Session(
        device=device,
        width=f.width,
        height=f.height,
        samples=1024,
        execution="sync",
        interactive=False,
        passes=["combined", "position", "normal"],
    ) as final:
        for broken in (0, 1, 0):
            f.edit(broken)
            frame = final.wait(final.apply(native_scene.capture(f.rop), at_time=hou.time()))
            f.check(frame, final.status(), broken)
    f.control.setExpression("int(hou.frame()+.5)-1 if int(hou.frame()+.5) in (2,3,4) else 0", hou.exprLanguage.Python)
    pass_fixture.parm(f.rop, "picture").set(str(f.dest / "basis.$F4.exr"))
    f.rop.render(frame_range=(1, 5, 1))
    for n in range(1, 6):
        reader = oiio.ImageInput.open(str(f.dest / f"basis.{n:04d}.exr"))
        assert reader
        spec = reader.spec()
        pixels = reader.read_image()
        reader.close()
        issues = json.loads(spec.get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        assert bool(issues) == (n in (2, 3, 4)), (n, issues)
        assert np.isfinite(pixels).all() and pixels.max() > 0.1
    f.control.deleteAllKeyframes()
    f.edit(0)
    hou.setFrame(1)
    hou.hipFile.save(str(f.dest / "tangent-bases.hiplc"))
except BaseException:
    failure = dict(result="failed", device=device, error=traceback.format_exc(), checks=checks, case=f.case)
    current = entry.session.frame()
    if current:
        planes_path = f.dest / ("failure-" + str(time.time_ns()) + ".npz")
        np.savez(planes_path, **f.planes(current))
        failure["planes"] = str(planes_path)
        failure.update(status=entry.session.status(), diagnostics=entry.session.diagnostics())
    path = artifacts_root() / "stage3/materials/tangent-bases/houdini-tangent-basis-smoke.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(failure, indent=2))
    raise
finally:
    registry.close_all()
report = dict(
    result="passed",
    device=device,
    independent_images=32,
    recovery_reference_images=28,
    contracts=contracts,
    checks=checks,
    authoring_controls=authoring,
    final_states=3,
    native_batch_frames=5,
    warm_tracked_resources=warm,
    warm_allocation_bounds=allocation_bounds,
)
path = artifacts_root() / "stage3/materials/tangent-bases/houdini-tangent-basis-smoke.json"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(report, indent=2))
print(json.dumps(report))
