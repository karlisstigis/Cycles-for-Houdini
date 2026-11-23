# SPDX-License-Identifier: Apache-2.0
"""Small portable optical and equal-time sampling scenes for release builds."""

import argparse
import json
from pathlib import Path
import time

import hou
import numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch


WIDTH, HEIGHT = 96, 48


def shader(key, kind, **parameters):
    node = dict(id="surface", definition="cycles/" + kind + "/1", parameters=parameters)
    return dict(
        key=key,
        kind="graph",
        graph=dict(schema_version=1, nodes=[node], links=[], surface=dict(node="surface", socket="BSDF")),
    )


def geometry(name, shape, center, size, material, priority=0):
    obj = hou.node("/obj").createNode("geo", name.replace("-", "_"), run_init_scripts=False)
    sop = obj.createNode(shape)
    if shape == "sphere":
        sop.parm("type").set("polymesh")
        sop.parm("freq").set(3)
        sop.parmTuple("rad").set(size)
    else:
        sop.parmTuple("size").set(size)
    sop.parmTuple("t").set(center)
    request = ch.geometry_request(obj, sop, material=material, smooth=shape == "sphere")
    request["object_properties"]["dielectric_priority"] = priority
    return request


def write_exr(path, rgb):
    rgb = np.ascontiguousarray(rgb, dtype=np.float32)
    height, width, channels = rgb.shape
    out = oiio.ImageOutput.create(str(path))
    if not out or not out.open(str(path), oiio.ImageSpec(width, height, channels, oiio.FLOAT)):
        raise RuntimeError("Cannot open " + str(path))
    try:
        if not out.write_image(rgb):
            raise RuntimeError(out.geterror())
    finally:
        if not out.close():
            raise RuntimeError(out.geterror())


def publish(directory, name, reference, candidate):
    for suffix, image in (
        ("reference", reference),
        ("candidate", candidate),
        ("absolute-difference", np.abs(reference - candidate)),
    ):
        write_exr(directory / (name + "-" + suffix + ".exr"), image)


def render(session, update, **settings):
    update = dict(update, rendering=dict(update["rendering"], **settings))
    start = time.monotonic()
    frame = session.wait(session.apply(update), timeout=180)
    wall = time.monotonic() - start
    status = session.status()
    assert frame["complete"] and not status["error"], status
    assert not status.get("source_diagnostics"), status.get("source_diagnostics")
    assert status["render_device_ids"] == [args.device], status["render_device_ids"]
    plane = frame["planes"]["combined"]
    image = np.frombuffer(plane["pixels"], np.float32).reshape(HEIGHT, WIDTH, 4).copy()
    assert np.isfinite(image).all()
    return image, dict(seconds=status["elapsed_seconds"], samples=status["current_sample"], wall_seconds=wall)


def transmission(indices):
    # Normal incidence, lossless parallel planes, including all internal returns.
    # Combining two reciprocal slabs gives 1/T_total - 1 = sum(1/T_i - 1).
    resistance = 0.0
    for left, right in zip(indices, indices[1:]):
        reflect = ((left - right) / (left + right)) ** 2
        resistance += reflect / (1 - reflect)
    return 1 / (1 + resistance)


def nested(session, camera):
    iors = dict(glass=1.5, liquid=1.333, ice=1.309, bubble=1.0)
    mats = [shader(key, "glass_bsdf", color=[1.0, 1.0, 1.0], roughness=0.0, IOR=ior) for key, ior in iors.items()]
    mats.append(dict(key="emitter", kind="emission", color=(1.0, 1.0, 1.0), emission_strength=1.0))
    geometry_list = [geometry("screen", "box", (0, 0, -3), (12, 10, 0.1), "emitter")]
    layers = [("glass", 1.5, 3.0, 3), ("liquid", -1.0, 1.8, 2), ("ice", -0.6, 0.9, 4), ("bubble", -0.2, 0.4, 5)]
    expected = np.zeros((HEIGHT, WIDTH, 3), np.float32)
    for column in range(4):
        for key, lo, hi, priority in layers[: column + 1]:
            geometry_list.append(
                geometry(key + str(column), "box", (column - 1.5, 0, (lo + hi) / 2), (1.0, 4.0, hi - lo), key, priority)
            )
        # On the return side of the ice/bubble interfaces liquid still surrounds
        # the ice; glass is only the front wall. Its back interface meets liquid.
        media = (
            [1.0, 1.5, 1.0],
            [1.0, 1.5, 1.333, 1.0],
            [1.0, 1.5, 1.333, 1.309, 1.333, 1.0],
            [1.0, 1.5, 1.333, 1.309, 1.0, 1.309, 1.333, 1.0],
        )[column]
        expected[:, column * 24 : (column + 1) * 24] = transmission(media)
    update = dict(
        geometry=geometry_list,
        materials=mats,
        camera_path=camera.path(),
        background=(0, 0, 0),
        rendering=dict(
            adaptive_sampling=False, seed=51, min_bounce=32, max_bounce=32, glossy_bounce=32, transmission_bounce=32
        ),
    )
    image, timing = render(session, update, nested_dielectrics=True, samples=512)
    publish(args.report.parent, "layered-media", expected, image[:, :, :3])
    residual = image[:, :, :3] - expected
    errors = [np.abs(residual[4:-4, 24 * i + 4 : 24 * i + 20].mean(axis=(0, 1))).max().item() for i in range(4)]
    assert max(errors) < 0.008, ("slab transmission", errors)
    # A broken toggle cannot pass by agreement with a noisy image of one slab.
    conventional, _ = render(session, update, nested_dielectrics=False, samples=512)
    off_errors = [
        abs(float(conventional[4:-4, 24 * i + 4 : 24 * i + 20, 0].mean()) - float(expected[0, 24 * i, 0]))
        for i in range(4)
    ]
    assert max(off_errors[1:]) > 0.025, off_errors
    return dict(mean_channel_errors=errors, off_control_errors=off_errors, **timing)


def sampling(session, camera, particles):
    label = "particles" if particles else "shiny-disk"
    camera.parm("projection").set(0)
    metal = shader("metal", "principled_bsdf", base_color=[0.6, 0.47, 0.37], metallic=1.0, roughness=0.05)
    glass = shader("glass", "glass_bsdf", color=[1.0, 1.0, 1.0], roughness=0.015, IOR=1.45)
    mats = [metal, glass, dict(key="ground", kind="diffuse", color=(0.45, 0.4, 0.36))]
    objects = [
        geometry(label + "_subject", "sphere", (0, 0, 0), (0.9, 0.9, 0.9), "metal"),
        geometry(label + "_floor", "box", (0, -1.02, 0), (9, 0.2, 9), "ground"),
    ]
    if particles:
        for i in range(12):
            x = (i % 4 - 1.5) * 0.48
            objects.append(
                geometry("particle", "sphere", (x, (i // 4 - 1) * 0.47, 1.4 + i * 0.025), (0.19, 0.19, 0.19), "glass")
            )
    light = dict(
        key="disk",
        kind="area",
        ellipse=True,
        size=(0.8, 0.8),
        strength=(90, 80, 72),
        use_mis=True,
        transform=(1, 0, 0, 0.55, 0, 1, 0, 1.25, 0, 0, 1, 2.7),
    )
    update = dict(
        geometry=objects,
        materials=mats,
        lights=[light],
        camera_path=camera.path(),
        camera_dof=dict(aperture_size=0.10, focus_distance=3.6),
        background=(0.05, 0.05, 0.05),
        rendering=dict(adaptive_sampling=False, clamp_direct=0.0, clamp_indirect=0.0),
    )
    reference, reference_timing = render(session, update, seed=1401, samples=4096, resampled_light_sampling=False)
    reference = reference[:, :, :3]
    assert np.ptp(reference) > 0.2 and np.mean(reference) > 0.02, ("Unlit scene", label)
    rows = []
    for enabled in (False, True):
        render(session, update, seed=104, samples=8, resampled_light_sampling=enabled)
        images = []
        timings = []
        for seed in (811, 812, 813):
            image, timing = render(
                session, update, seed=seed, samples=1048576, time_limit=1.5, resampled_light_sampling=enabled
            )
            assert 1.35 <= timing["seconds"] <= 2.5 and 0 < timing["samples"] < 1048576, timing
            images.append(image[:, :, :3])
            timings.append(timing)
        average = np.mean(images, axis=0)
        mode = "rls" if enabled else "standard"
        publish(args.report.parent, label + "-" + mode, reference, average)
        relative_energy = float(abs(average.mean() - reference.mean()) / reference.mean())
        assert relative_energy < 0.06, (label, mode, relative_energy)
        rows.append(
            dict(
                mode=mode,
                runs=timings,
                relative_energy_error=relative_energy,
                rmse=float(np.sqrt(np.mean((np.asarray(images) - reference) ** 2))),
            )
        )
    return dict(reference=reference_timing, comparisons=rows)


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--device", required=True)
parser.add_argument("--report", type=Path, required=True)
args = parser.parse_args()
args.report.parent.mkdir(parents=True, exist_ok=True)
camera = hou.node("/obj").createNode("cam", "optics_camera")
camera.setParms(dict(tz=5.0, projection=1, orthowidth=4.0, resx=WIDTH, resy=HEIGHT))
report = dict(device=args.device, result="running")
for key in ("layered-media", "shiny-disk", "particles"):
    with ch.Session(
        device=args.device,
        width=WIDTH,
        height=HEIGHT,
        samples=512,
        threads=4,
        execution="worker",
        interactive=True,
        denoising={"enabled": False},
    ) as session:
        report[key] = (
            nested(session, camera) if key == "layered-media" else sampling(session, camera, key == "particles")
        )
        args.report.write_text(json.dumps(report, indent=2))
report["result"] = "passed"
args.report.write_text(json.dumps(report, indent=2))
