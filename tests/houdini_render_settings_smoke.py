# SPDX-License-Identifier: Apache-2.0
"""Real sample counts, linear Film exposure, fixed seeds and update ownership."""

from support.paths import artifacts_root
from cycles_houdini.device_selection import set_devices as set_rop_devices
import argparse
import json
import os
from pathlib import Path
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene, registry

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
destination = artifacts_root() / "stage3/render-settings"
destination.mkdir(parents=True, exist_ok=True)
checks = []


def plane(frame, name):
    value = frame["planes"][name]
    return np.frombuffer(value["pixels"], np.float32).reshape(frame["height"], frame["width"], value["channels"])


with ch.Session(
    device=device,
    width=128,
    height=96,
    samples=32,
    interactive=False,
    passes=["combined", "depth", "normal", "position", "sample_count"],
    collect_diagnostics=True,
) as session:
    reference = session.wait(session.calibration())
    converted = session.status()["geometry_conversions"]
    base_count = float(plane(reference, "sample_count").max())
    assert base_count == 32 and session.status()["current_sample"] == 32
    exposed = session.wait(session.apply(dict(rendering=dict(samples=32, film_exposure=2))))
    assert np.allclose(
        plane(exposed, "combined")[:, :, :3], plane(reference, "combined")[:, :, :3] * 2, atol=1e-6, rtol=1e-5
    )
    for name in ("depth", "normal", "position"):
        assert np.array_equal(plane(exposed, name), plane(reference, name)), name
    checks.append(
        dict(name="Film exposure doubles raw linear beauty without changing data passes", status=session.status())
    )

    def render(**settings):
        return session.wait(session.apply(dict(rendering=dict(samples=32, **settings))))

    seed = render(seed=19)
    repeat = render(seed=19)
    different = render(seed=20)
    repeat_error = float(np.abs(plane(seed, "combined") - plane(repeat, "combined")).max())
    assert np.allclose(plane(seed, "combined"), plane(repeat, "combined"), atol=1e-6, rtol=1e-5), repeat_error
    seed_error = float(np.abs(plane(seed, "combined") - plane(different, "combined")).max())
    assert seed_error > max(8 * repeat_error, 1e-5), (seed_error, repeat_error)
    checks.append(
        dict(
            name="Fixed seed repeats within float tolerance; a changed seed changes actual samples",
            repeat_max_error=repeat_error,
            changed_seed_max_error=seed_error,
        )
    )
    for value in ("tabulated_sobol", "blue_noise", "blue_noise_first", "sobol_burley", "auto"):
        frame = render(sampling_pattern=value)
        assert np.isfinite(plane(frame, "combined")).all()
    frame = session.wait(
        session.apply(dict(rendering=dict(samples=128, adaptive_sampling=True, min_samples=16, noise_threshold=1)))
    )
    counts = plane(frame, "sample_count")
    actual_counts = counts
    assert actual_counts.min() >= 16 and (
        np.count_nonzero(counts < counts.max()) > 0 or session.status()["current_sample"] < 128
    )
    checks.append(
        dict(
            name="Adaptive sampling actually stops converged pixels",
            minimum=float(actual_counts.min()),
            maximum=float(actual_counts.max()),
            fixed_samples=32,
            status=session.status(),
        )
    )
    timed = session.wait(session.apply(dict(rendering=dict(samples=4000000, time_limit=0.03))), timeout=30)
    counts = plane(timed, "sample_count")
    assert 0 < session.status()["current_sample"] < 4000000
    checks.append(
        dict(
            name="Time limit ends path tracing below the total sample budget",
            maximum=float(counts.max()),
            status=session.status(),
        )
    )
    for settings in [
        dict(samples=0),
        dict(noise_threshold=float("nan")),
        dict(max_bounce=-1),
        dict(pixel_filter="invalid"),
        dict(volume_step_rate=0),
        dict(sample_subset=True, subset_offset=32),
    ]:
        before = session.status()
        try:
            session.apply(dict(rendering=dict(dict(samples=32), **settings)))
        except (ValueError, RuntimeError):
            pass
        else:
            raise AssertionError("Accepted invalid render settings: " + str(settings))
        # Workers reserve requested generations before capture validation; only an
        # accepted scene revision must survive rejection without changing.
        after = session.status()
        assert after.get("committed_generation", after["generation"]) == before.get(
            "committed_generation", before["generation"]
        )
    for value in ("rgb", "albedo", "albedo_normal"):
        frame = session.wait(session.apply(dict(denoising=dict(enabled=True, passes=value))))
        assert np.isfinite(plane(frame, "combined")).all()
    assert session.status()["geometry_conversions"] == converted
    checks.append(
        dict(
            name="Render/denoiser edits retain converted geometry; invalid settings preserve the committed render generation",
            status=session.status(),
        )
    )
obj = hou.node("/obj").createNode("geo", "sampling_profiles")
box = obj.createNode("box")
box.setRenderFlag(True)
camera = hou.node("/obj").createNode("cam", "sampling_profiles_camera")
camera.parm("tz").set(5)
rop = hou.node("/out").createNode("cycles_ipr", "sampling_profiles_rop")
set_rop_devices(rop, device)
rop.setParms(
    dict(
        source_mode="sop", soppath=box.path(), camera=camera.path(), samples=32, ipr_samples=8, ipr_adaptive_sampling=0
    )
)
assert native_scene.configuration(rop)[3] == 8 and native_scene.configuration(rop, interactive=False)[3] == 32
entry = registry.for_rop(rop, native_scene.configuration(rop))
entry.session.wait(entry.apply(native_scene.capture(rop), hou.time()))
rop.parm("ipr_samples").set(16)
same = registry.for_rop(rop, native_scene.configuration(rop))
assert same is entry
entry.session.wait(entry.apply(native_scene.capture(rop), hou.time()))
assert entry.session.status()["rendering"]["samples"] == 16
checks.append(
    dict(
        name="IPR/final profiles differ and changing the sample limit retains the native session",
        status=entry.session.status(),
    )
)
registry.close_all()
report = dict(
    result="passed", device=device, execution=os.environ.get("CYCLES_HOUDINI_EXECUTION", "sync"), checks=checks
)
(destination / "houdini-render-settings-smoke.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
