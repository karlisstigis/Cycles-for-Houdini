# SPDX-License-Identifier: Apache-2.0
"""Owner-triggered renderer validation. Run through run-heavy-renderer-tests.ps1.

The numerical rules below are frozen before the first qualification run. A
missing backend or pristine runtime is an error, never a passing comparison.
"""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import random
import subprocess
import sys
import time

import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene

from support.images import assert_image_close, rmse, ssim

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from cycles_cli_backend import require_like_backends  # noqa: E402
from heavy_bias_statistics import BIAS_SEEDS, evaluate as evaluate_bias  # noqa: E402


WIDTH, HEIGHT = 96, 64
SEEDS = (701, 1103, 1409, 1901, 2203)
BIAS_SPP = 16384
EQUAL_SEEDS = (701, 1103, 1409, 1901, 2203, 2711, 3203, 3701, 4201, 4703)
EQUAL_SECONDS = 20.0
RIS_LOSS_RATIO = 1.10
RIS_LOSS_SCENES = 2
RIS_LOSS_REPETITIONS = 4
SCENES = ("hair", "glossy_interior", "courtyard")
EQUAL_SCENES = {
    "OPTIX": ("artist_particles", "many_light", "glossy_interior"),
    "CPU": ("hair", "glossy_interior", "courtyard"),
}
PARTICLE_SCENE_SHA256 = "a60fafbf32203da3dc77f99c9c2762aca889ee0e13e391ae5b1ec17a72d64eb7"
PARTICLE_IRIS_SHA256 = "964a9b269ac2c3cce16c60945fb65a224036bc0cc760dd1ae91f4d1f03b528c2"
BIAS_SCENES = ("diffuse_cube", "glossy_glass", "glass_caustics")
FURNACE_CASES = (
    "diffuse",
    "glossy",
    "glass",
    "principled",
    "refraction",
    "transparent",
    "translucent",
    "nested",
    "absorption",
)


def graph(key, kind, **parameters):
    return dict(
        key=key,
        kind="graph",
        graph=dict(
            schema_version=1,
            nodes=[dict(id="surface", definition=f"cycles/{kind}/1", parameters=parameters)],
            links=[],
            surface=dict(node="surface", socket="BSDF"),
        ),
    )


def box(name, center, size, material):
    obj = hou.node("/obj").createNode("geo", name, run_init_scripts=False)
    sop = obj.createNode("box")
    sop.parmTuple("size").set(size)
    sop.parmTuple("t").set(center)
    return ch.geometry_request(obj, sop, material=material)


def sphere(name, center, radius, material):
    obj = hou.node("/obj").createNode("geo", name, run_init_scripts=False)
    sop = obj.createNode("sphere")
    sop.parm("type").set("polymesh")
    sop.parm("freq").set(4)
    sop.parmTuple("rad").set((radius,) * 3)
    sop.parmTuple("t").set(center)
    return ch.geometry_request(obj, sop, material=material, smooth=True)


def camera(name, *, ortho=True):
    node = hou.node("/obj").createNode("cam", name)
    node.setParms(dict(tz=5, projection=1 if ortho else 0, orthowidth=4, resx=WIDTH, resy=HEIGHT))
    return node


def light(name, location, strength, size=(1.0, 1.0)):
    x, y, z = location
    return dict(
        key=name,
        kind="area",
        size=size,
        strength=strength,
        use_mis=True,
        shadow_catcher=True,
        transform=(1, 0, 0, x, 0, 1, 0, y, 0, 0, 1, z),
    )


def scene(name):
    cam = camera("heavy_" + name)
    common = dict(
        camera_path=cam.path(),
        background=(0.04, 0.04, 0.04),
        rendering=dict(
            adaptive_sampling=False,
            animate_seed=False,
            clamp_direct=0.0,
            clamp_indirect=0.0,
            sample_subset=False,
            nested_dielectrics=False,
            light_tree=True,
            use_guiding=False,
            resampled_light_sampling=False,
        ),
    )
    if name == "hair":
        obj = hou.node("/obj").createNode("geo", "heavy_hair", run_init_scripts=False)
        sop = obj.createNode("python", "strands")
        sop.parm("python").set("""import hou, math
g=hou.pwd().geometry(); g.addAttrib(hou.attribType.Point,'width',0.07)
for i in range(25):
    c=g.createPolygon(is_closed=False)
    for j in range(5):
        p=g.createPoint(); p.setPosition(((i-12)*0.09+0.03*math.sin(i+j),-0.9+j*0.45,0.0)); c.addVertex(p)
""")
        common.update(
            geometry=[ch.geometry_request(obj, sop, material="subject")],
            materials=[
                graph("subject", "principled_hair_bsdf", model="Huang", color=[0.35, 0.12, 0.035], roughness=0.3)
            ],
            lights=[light("key", (-0.8, 0.9, 2.2), (35, 29, 21)), light("fill", (1.2, -0.2, 1.8), (8, 10, 14))],
        )
    elif name == "glossy_interior":
        common.update(
            geometry=[
                sphere("heavy_glossy", (0, 0, 0), 0.8, "subject"),
                box("heavy_glossy_floor", (0, -1.0, 0), (5, 0.1, 5), "floor"),
                box("heavy_glossy_wall", (0, 0, -2), (5, 4, 0.1), "floor"),
            ],
            materials=[
                graph("subject", "principled_bsdf", base_color=[0.7, 0.45, 0.2], metallic=1.0, roughness=0.16),
                dict(key="floor", kind="diffuse", color=(0.55, 0.5, 0.43)),
            ],
            lights=[light("slot", (0, 1.3, 1.8), (50, 44, 39), (0.5, 1.4))],
        )
    elif name == "courtyard":
        common.update(
            geometry=[box("heavy_court_floor", (0, -1.1, 0), (6, 0.1, 6), "stone")]
            + [
                box(f"heavy_pillar_{i}", (x, 0, z), (0.35, 2.1, 0.35), "stone")
                for i, (x, z) in enumerate(((-1.7, -1.2), (1.7, -1.2), (-1.7, 1.2), (1.7, 1.2)))
            ]
            + [sphere("heavy_court_subject", (0, -0.25, 0), 0.65, "subject")],
            materials=[
                dict(key="stone", kind="diffuse", color=(0.6, 0.59, 0.56)),
                graph("subject", "principled_bsdf", base_color=[0.22, 0.43, 0.65], roughness=0.42),
            ],
            lights=[
                light("sunpatch", (-1.2, 2.2, 1.3), (50, 40, 30), (0.6, 0.6)),
                light("skyfill", (1.4, 1.8, 0.6), (12, 18, 25), (1.5, 1.5)),
            ],
        )
    elif name == "many_light":
        common.update(
            geometry=[
                sphere("heavy_many_glossy", (0, -0.2, 0), 0.75, "subject"),
                box("heavy_many_floor", (0, -1.15, 0), (7, 0.1, 7), "floor"),
                box("heavy_many_wall", (0, 0, -2.3), (7, 4, 0.1), "floor"),
            ],
            materials=[
                graph("subject", "principled_bsdf", base_color=[0.7, 0.55, 0.35], metallic=0.6, roughness=0.22),
                dict(key="floor", kind="diffuse", color=(0.55, 0.52, 0.5)),
            ],
            lights=[
                light(
                    f"many_{i}_{j}",
                    ((i - 2) * 0.7, 1.5, (j - 2) * 0.55 + 0.8),
                    (3.5 + 0.3 * i, 3.2 + 0.2 * j, 3.1 + 0.4 * (i + j)),
                    (0.25, 0.25),
                )
                for i in range(5)
                for j in range(5)
            ],
        )
    else:
        raise ValueError(name)
    return common


def artist_particle_scene(path, folder, iris_image=None):
    path = path or Path(__file__).resolve().parents[1] / "artifacts/exec-audit-20261029/scenes/particles_rls.hiplc"
    if not path.is_file():
        raise FileNotFoundError(
            f"Audited RIS particle HIP is missing; pass --ris-scene with its relocated copy: {path}"
        )
    scene_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    if scene_hash != PARTICLE_SCENE_SHA256:
        raise ValueError(f"Audited RIS particle HIP changed: {path} ({scene_hash})")
    hou.hipFile.load(str(path), suppress_save_prompt=True, ignore_load_warnings=True)
    hou.setFrame(1)
    rop = hou.node("/out/cycles_render")
    if rop is None:
        raise RuntimeError("Audited RIS particle HIP lacks /out/cycles_render")
    if iris_image:
        if not iris_image.is_file() or hashlib.sha256(iris_image.read_bytes()).hexdigest() != PARTICLE_IRIS_SHA256:
            raise ValueError(f"Relocated RIS Image Iris JPG is missing or differs from the audit: {iris_image}")
        hou.node("/obj/cam2").parm("cycles_image_iris_file").set(str(iris_image))
    rop.setParms(
        dict(
            resx=1280,
            resy=720,
            resolution_percent=100,
            ipr_resolution_percent=100,
            render_region=0,
            pass_count=0,
            use_denoise=0,
            ipr_use_denoise=0,
        )
    )
    assets = []
    for parm, authored in hou.fileReferences():
        if not authored or parm is None:
            continue
        if parm.path() == "/out/cycles_render/picture":
            continue
        expanded = Path(hou.text.expandString(authored))
        if expanded.suffix.lower() not in {".exr", ".hdr", ".png", ".jpg", ".jpeg", ".vdb", ".bgeo", ".sc"}:
            continue
        if not expanded.is_file():
            raise FileNotFoundError(
                f"Audited RIS scene input is missing at {parm.path()}: {authored}; "
                "use --ris-iris-image for a relocated audit JPG"
            )
        asset_hash = hashlib.sha256(expanded.read_bytes()).hexdigest()
        if parm.path() == "/obj/cam2/cycles_image_iris_file" and asset_hash != PARTICLE_IRIS_SHA256:
            raise ValueError("Audited RIS Image Iris JPG differs from its pinned SHA-256")
        assets.append(dict(parameter=parm.path(), path=str(expanded), sha256=asset_hash))
    update = native_scene.capture(rop)
    update["denoising"] = {"enabled": False}
    update["passes"] = ["combined"]
    provenance = dict(
        scene=str(path), sha256=scene_hash, assets=assets, source="exec-audit-20261029 particle scene copy"
    )
    (folder / "artist-particles-provenance.json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    return update


def furnace(material):
    cam = camera("furnace_" + material)
    materials = [dict(key="enclosure", kind="emission", color=(1, 1, 1), emission_strength=1)]
    if material == "diffuse":
        materials.append(dict(key="subject", kind="diffuse", color=(1, 1, 1)))
    elif material == "glossy":
        materials.append(graph("subject", "glossy_bsdf", color=[1, 1, 1], roughness=0.15))
    elif material in ("glass", "nested"):
        materials.append(graph("subject", "glass_bsdf", color=[1, 1, 1], roughness=0, IOR=1.45))
    elif material == "refraction":
        materials.append(graph("subject", "refraction_bsdf", color=[1, 1, 1], roughness=0, IOR=1.45))
    elif material == "transparent":
        materials.append(graph("subject", "transparent_bsdf", color=[1, 1, 1]))
    elif material == "translucent":
        materials.append(graph("subject", "translucent_bsdf", color=[1, 1, 1]))
    elif material == "principled":
        materials.append(graph("subject", "principled_bsdf", base_color=[1, 1, 1], roughness=0.4))
    else:
        raise ValueError(material)
    geometry = [
        sphere("enclosure_" + material, (0, 0, 0), 12, "enclosure"),
        sphere("subject_" + material, (0, 0, 0), 0.8, "subject"),
    ]
    if material == "nested":
        # Normal-incidence layered slabs have an independent analytic
        # transmission reference, unlike curved overlapping spheres.
        geometry = [box("nested_screen", (0, 0, -3), (12, 10, 0.1), "enclosure")]
        materials = [dict(key="enclosure", kind="emission", color=(1, 1, 1), emission_strength=1)]
        layers = (
            ("glass", 1.5, 3.0, 3, 1.5),
            ("liquid", -1.0, 1.8, 2, 1.333),
            ("ice", -0.6, 0.9, 4, 1.309),
            ("bubble", -0.2, 0.4, 5, 1.0),
        )
        for key, lo, hi, priority, ior in layers:
            materials.append(graph(key, "glass_bsdf", color=[1, 1, 1], roughness=0, IOR=ior))
        for col in range(4):
            for key, lo, hi, priority, _ in layers[: col + 1]:
                part = box(f"nested_{key}_{col}", (col - 1.5, 0, (lo + hi) / 2), (1, 4, hi - lo), key)
                part["object_properties"]["dielectric_priority"] = priority
                geometry.append(part)
    return dict(
        camera_path=cam.path(),
        geometry=geometry,
        materials=materials,
        background=(0, 0, 0) if material == "nested" else (1, 1, 1),
        rendering=dict(
            adaptive_sampling=False,
            clamp_direct=0,
            clamp_indirect=0,
            max_bounce=32,
            diffuse_bounce=32,
            glossy_bounce=32,
            transmission_bounce=32,
            nested_dielectrics=material == "nested",
            use_guiding=False,
            resampled_light_sampling=False,
        ),
    )


def bias_feature_scene():
    update = scene("courtyard")
    for lamp in update["lights"]:
        lamp["use_mis"] = True
        lamp["shadow_catcher"] = True
    return update


def save_bias_feature(folder, key, image, metadata):
    folder.mkdir(parents=True, exist_ok=True)
    array_path = folder / f"{key}.npy"
    if array_path.exists() or (folder / f"{key}.json").exists():
        raise FileExistsError(f"Bias feature capture already exists: {key}")
    np.save(array_path, image)
    metadata["image_sha256"] = hashlib.sha256(array_path.read_bytes()).hexdigest()
    (folder / f"{key}.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")


def bias_image_matches(folder, key, metadata):
    recorded = metadata["image_sha256"]
    return recorded == hashlib.sha256((folder / f"{key}.npy").read_bytes()).hexdigest()


def absorption(folder):
    identity = json.loads(os.environ["CYCLES_HOUDINI_TEST_IDENTITY"])
    harness = Path(identity["build_directory"]) / "bin/ch_harness.exe"
    volume = folder / "density.vdb"
    subprocess.run([str(harness), "--write-volume-fixture", str(volume)], check=True)
    obj = hou.node("/obj").createNode("geo", "furnace_absorption", run_init_scripts=False)
    sop = obj.createNode("file")
    sop.parm("file").set(str(volume))
    cam = camera("furnace_absorption_camera")
    return dict(
        camera_path=cam.path(),
        geometry=[ch.geometry_request(obj, sop, material="absorb")],
        materials=[dict(key="absorb", kind="volume_absorption", color=(0, 0, 0), density=1)],
        background=(1, 1, 1),
        rendering=dict(
            adaptive_sampling=False, clamp_direct=0, clamp_indirect=0, use_guiding=False, resampled_light_sampling=False
        ),
    )


def slab_transmission(indices):
    resistance = 0.0
    for left, right in zip(indices, indices[1:]):
        reflectance = ((left - right) / (left + right)) ** 2
        resistance += reflectance / (1 - reflectance)
    return 1 / (1 + resistance)


NESTED_EXPECTED = tuple(
    slab_transmission(indices)
    for indices in (
        (1.0, 1.5, 1.0),
        (1.0, 1.5, 1.333, 1.0),
        (1.0, 1.5, 1.333, 1.309, 1.333, 1.0),
        (1.0, 1.5, 1.333, 1.309, 1.0, 1.309, 1.333, 1.0),
    )
)


def device_for(backend):
    matches = [d for d in ch.devices() if d["backend"].upper() == backend.upper()]
    if not matches:
        raise RuntimeError(f"Required {backend} device is unavailable")
    return matches[0]["id"]


def render(update, backend, samples, seed, *, seconds=0, ris=False, guiding=False, size=(WIDTH, HEIGHT)):
    width, height = size
    device = device_for(backend)
    authored = dict(
        update,
        rendering=dict(
            update["rendering"],
            samples=samples,
            seed=seed,
            time_limit=seconds,
            resampled_light_sampling=ris,
            use_guiding=guiding,
        ),
    )
    started = time.monotonic()
    with ch.Session(
        device=device,
        width=width,
        height=height,
        samples=samples,
        threads=4,
        interactive=seconds > 0,
        execution="worker",
        denoising={"enabled": False},
        passes=["combined"],
    ) as session:
        frame = session.wait(session.apply(authored), timeout=max(300, seconds + 180))
        status = session.status()
    elapsed = time.monotonic() - started
    assert frame["complete"] and not status["error"], status
    assert status["render_device_ids"] == [device], status["render_device_ids"]
    plane = frame["planes"]["combined"]
    image = np.frombuffer(plane["pixels"], np.float32).reshape(height, width, plane["channels"])[..., :3].copy()
    assert image.shape == (height, width, 3) and np.isfinite(image).all()
    return image, dict(
        device=device,
        backend=backend,
        samples=status["current_sample"],
        trace_seconds=status["elapsed_seconds"],
        wall_seconds=elapsed,
        render_device_ids=status["render_device_ids"],
        guiding_requested=guiding,
        ris_requested=ris,
        resolution=[width, height],
    )


def save(folder, key, image, metadata):
    folder.mkdir(parents=True, exist_ok=True)
    np.save(folder / f"{key}.npy", image)
    (folder / f"{key}.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")


def load(folder, key, *, size=(WIDTH, HEIGHT)):
    data = np.load(folder / f"{key}.npy", allow_pickle=False)
    if data.shape != (size[1], size[0], 3) or not np.isfinite(data).all():
        raise ValueError(f"Invalid finite linear RGB image: {folder / key}")
    return data


def regions(image):
    return np.stack(
        [image[y : y + 16, x : x + 24].mean((0, 1)) for y in range(0, HEIGHT, 16) for x in range(0, WIDTH, 24)]
    )


def regional_relative_summary(cpu_runs, other_runs):
    cpu, other = np.mean(cpu_runs, axis=0), np.mean(other_runs, axis=0)
    reference = regions(cpu)
    scale = np.maximum(np.abs(reference), 0.03)
    difference = np.abs(regions(other) - reference) / scale
    noise = max(
        float(np.max(np.abs(regions(cpu_runs[0]) - regions(cpu_runs[1])) / (math.sqrt(2) * scale))),
        float(np.max(np.abs(regions(other_runs[0]) - regions(other_runs[1])) / (math.sqrt(2) * scale))),
    )
    tolerance = 0.02 + 3 * noise
    error = float(np.max(difference))
    state = "inconclusive" if noise > 0.02 else "failed" if error > tolerance else "passed"
    return dict(
        result=state,
        max_relative_regional_rgb=error,
        relative_seed_noise=noise,
        dark_floor=0.03,
        relative_tolerance=tolerance,
    )


def paired_error_summary(enabled, disabled):
    if len(enabled) != len(disabled) or not enabled:
        raise ValueError("Equal-time modes require the same nonempty seed set")
    ratios = [1.0 if off == on == 0 else math.inf if off == 0 else on / off for on, off in zip(enabled, disabled)]
    return dict(
        median_ratio=float(np.median(ratios)),
        ratios=ratios,
        losses=sum(ratio > RIS_LOSS_RATIO for ratio in ratios),
        wins=sum(ratio < 1 / RIS_LOSS_RATIO for ratio in ratios),
    )


def paired_variance_summary(enabled, disabled):
    if len(enabled) != len(disabled) or len(enabled) != len(EQUAL_SEEDS):
        raise ValueError("Equal-time variance requires ten matched independent seeds")
    if any(image.shape != enabled[0].shape for image in enabled + disabled):
        raise ValueError("Equal-time images have different shapes")
    pairs = tuple(zip(range(0, len(EQUAL_SEEDS), 2), range(1, len(EQUAL_SEEDS), 2)))
    variance = {}
    for mode, images in ((False, disabled), (True, enabled)):
        variance[mode] = [float(np.mean(np.square(images[a].astype(np.float64) - images[b])) / 2) for a, b in pairs]
    rms = {mode: [math.sqrt(value) for value in values] for mode, values in variance.items()}
    summary = paired_error_summary(rms[True], rms[False])
    overall = paired_error_summary(
        [math.sqrt(float(np.mean(variance[True])))], [math.sqrt(float(np.mean(variance[False])))]
    )
    return dict(
        median_ratio=summary["median_ratio"],
        paired_ratios=summary["ratios"],
        losses=summary["losses"],
        wins=summary["wins"],
        overall_rms_ratio=overall["median_ratio"],
        pair_variance=variance,
        zero_baseline_pairs=[off == 0 and on > 0 for on, off in zip(variance[True], variance[False])],
    )


def ris_release_decision(loss_scenes, win_scenes, neutral_scenes, unresolved_scenes, optix_evidence_valid):
    if not optix_evidence_valid or unresolved_scenes or (loss_scenes and win_scenes):
        return "inconclusive"
    if loss_scenes >= RIS_LOSS_SCENES and win_scenes == 0:
        return "remove"
    if loss_scenes == 0 and win_scenes + neutral_scenes == len(EQUAL_SCENES["OPTIX"]):
        return "retain"
    return "inconclusive"


def numeric_probe():
    loss = paired_error_summary([1.2, 2.4, 3.6, 4.8, 0.1], [1, 2, 3, 4, 5])
    win = paired_error_summary([0.8, 0.8, 0.8, 0.8, 1.5], [1] * 5)
    zero = paired_error_summary([0, 0, 0, 0, 0], [0] * 5)
    assert loss["median_ratio"] > RIS_LOSS_RATIO and loss["losses"] == 4
    assert win["median_ratio"] < 1 / RIS_LOSS_RATIO and win["wins"] == 4
    assert zero["median_ratio"] == 1 and zero["losses"] == zero["wins"] == 0
    backend_timing_valid = {"OPTIX": True, "CPU": False}
    assert ris_release_decision(2, 0, 1, 0, backend_timing_valid["OPTIX"]) == "remove"
    assert ris_release_decision(2, 0, 1, 0, False) == "inconclusive"
    assert ris_release_decision(2, 1, 0, 0, True) == "inconclusive"
    assert ris_release_decision(0, 1, 2, 0, True) == "retain"
    assert ris_release_decision(0, 0, 2, 1, True) == "inconclusive"
    base = np.full((HEIGHT, WIDTH, 3), 0.08, dtype=np.float32)
    mutation = regional_relative_summary([base, base], [base * 1.2, base * 1.2])
    dark = regional_relative_summary([base * 0.001, base * 0.001], [base * 0.002, base * 0.002])
    assert mutation["result"] == "failed" and mutation["max_relative_regional_rgb"] > 0.19
    assert dark["result"] == "passed"
    offsets = [float(i - 5) for i in range(10)]
    off_images = [np.full((4, 4, 3), offset, dtype=np.float32) for offset in offsets]
    on_images = [np.full((4, 4, 3), offset * 1.2, dtype=np.float32) for offset in offsets]
    variance = paired_variance_summary(on_images, off_images)
    assert variance["median_ratio"] > RIS_LOSS_RATIO and variance["losses"] == 5
    return dict(
        result="passed",
        paired_loss=loss,
        paired_win=win,
        zero_tie=zero,
        variance_mutation=variance,
        regional_mutation=mutation,
        dark_floor_control=dark,
        cpu_timing_invalid_ris_decision="remove",
        optix_timing_invalid_ris_decision="inconclusive",
    )


def ris_activity_probe(folder, ris_scene, ris_iris_image=None):
    rows = []
    for name in EQUAL_SCENES["OPTIX"]:
        update = artist_particle_scene(ris_scene, folder, ris_iris_image) if name == "artist_particles" else scene(name)
        size = (1280, 720) if name == "artist_particles" else (640, 360)
        eligible = [
            item
            for item in update.get("lights", ())
            if item.get("kind") == "area" and item.get("use_mis", True) and item.get("shadow_catcher", True)
        ]
        if not eligible:
            raise RuntimeError(f"RIS scene {name} contains no eligible MIS area light")
        off, off_meta = render(update, "OPTIX", 32, 97, ris=False, size=size)
        off_repeat, repeat_meta = render(update, "OPTIX", 32, 97, ris=False, size=size)
        on, on_meta = render(update, "OPTIX", 32, 97, ris=True, size=size)
        repeat_rmse = rmse(off_repeat, off)
        contrast_rmse = rmse(on, off)
        activity_floor = max(1e-5, 5 * repeat_rmse)
        active = contrast_rmse > activity_floor
        rows.append(
            dict(
                scene=name,
                eligible_area_lights=len(eligible),
                same_seed_changed=not np.array_equal(off, on),
                off_repeat_rmse=repeat_rmse,
                contrast_rmse=contrast_rmse,
                activity_floor=activity_floor,
                active=active,
                off_sha256=hashlib.sha256(off.tobytes()).hexdigest(),
                repeat_sha256=hashlib.sha256(off_repeat.tobytes()).hexdigest(),
                on_sha256=hashlib.sha256(on.tobytes()).hexdigest(),
                off=off_meta,
                off_repeat=repeat_meta,
                on=on_meta,
            )
        )
        if not active:
            raise RuntimeError(f"RIS on/off contrast in eligible scene {name} did not exceed off/off repeatability")
    report = dict(result="passed", rows=rows)
    (folder / "ris-activity-probe.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def capture(suite, folder, role, ris_scene=None, ris_iris_image=None):
    if suite == "furnace":
        absorption_update = absorption(folder)
        for material in FURNACE_CASES:
            update = absorption_update if material == "absorption" else furnace(material)
            for backend in ("CPU", "CUDA", "OPTIX"):
                for seed in SEEDS[:2]:
                    image, meta = render(update, backend, BIAS_SPP, seed)
                    save(folder, f"{material}-{backend}-{seed}", image, meta)
    elif suite == "bias":
        if role != "fork":
            raise ValueError("Bias feature capture requires the packaged fork runtime")
        # The standalone XML loader does not expose the light object's shadow
        # catcher flag. Use the packaged fork worker, whose native light default
        # makes these MIS-enabled area lamps eligible for RIS.
        update = bias_feature_scene()
        eligible = [
            lamp["key"]
            for lamp in update["lights"]
            if lamp["kind"] == "area" and lamp["use_mis"] and lamp["shadow_catcher"]
        ]
        if len(eligible) < 2:
            raise RuntimeError("Bias feature scene needs at least two eligible analytic lamps")
        feature_folder = folder / "feature"
        if feature_folder.exists() and any(feature_folder.iterdir()):
            raise FileExistsError(f"Bias feature output is not empty: {feature_folder}")
        fixture_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        runtime_sha256 = json.loads(os.environ["CYCLES_HOUDINI_TEST_IDENTITY"])["manifest_sha256"]
        run_id = json.loads((folder / "inputs.json").read_text(encoding="utf-8"))["run_id"]
        order = []
        rng = random.Random(20261110)
        for seed in BIAS_SEEDS:
            for feature in ("ris_analytic", "guiding_analytic"):
                modes = [False, True]
                rng.shuffle(modes)
                for mode in modes:
                    image, meta = render(
                        update,
                        "CPU",
                        BIAS_SPP,
                        seed,
                        ris=mode if feature == "ris_analytic" else False,
                        guiding=mode if feature == "guiding_analytic" else False,
                    )
                    meta.update(
                        feature=feature,
                        enabled=mode,
                        seed=seed,
                        role="fork",
                        eligible_analytic_lights=eligible,
                        scene="courtyard",
                        shadow_catcher_light_requested=True,
                        fixture_sha256=fixture_sha256,
                        runtime_manifest_sha256=runtime_sha256,
                        run_id=run_id,
                    )
                    save_bias_feature(folder / "feature", f"{feature}-{'on' if mode else 'off'}-{seed}", image, meta)
                    order.append(dict(feature=feature, seed=seed, enabled=mode))
        (folder / "feature-order.json").write_text(json.dumps(order, indent=2), encoding="utf-8")
    elif suite == "cross-backend":
        for name in SCENES:
            update = scene(name)
            for backend in ("CPU", "CUDA", "OPTIX"):
                for seed in SEEDS[:2]:
                    image, meta = render(update, backend, 4096, seed)
                    save(folder, f"{name}-{backend}-{seed}", image, meta)
    elif suite == "equal-time":
        ris_activity_probe(folder, ris_scene, ris_iris_image)
        capture_order = []
        for backend in ("OPTIX", "CPU"):
            for name in EQUAL_SCENES[backend]:
                update = (
                    artist_particle_scene(ris_scene, folder, ris_iris_image)
                    if name == "artist_particles"
                    else scene(name)
                )
                size = (1280, 720) if name == "artist_particles" else (640, 360)
                eligible = [
                    item
                    for item in update.get("lights", ())
                    if item.get("kind") == "area" and item.get("use_mis", True) and item.get("shadow_catcher", True)
                ]
                if backend == "OPTIX" and not eligible:
                    raise RuntimeError(f"RIS scene {name} contains no eligible MIS area light")
                for mode in (False, True):
                    render(
                        update,
                        backend,
                        8,
                        97,
                        ris=mode if backend == "OPTIX" else False,
                        guiding=mode if backend == "CPU" else False,
                        size=size,
                    )
                randomized = list(EQUAL_SEEDS)
                rng = random.Random(17011 + sum(ord(char) for char in name + backend))
                rng.shuffle(randomized)
                for seed in randomized:
                    modes = [False, True]
                    rng.shuffle(modes)
                    for mode in modes:
                        # Alternating, paired seed order avoids all off runs
                        # preceding all on runs as the workstation warms.
                        image, meta = render(
                            update,
                            backend,
                            1048576,
                            seed,
                            seconds=EQUAL_SECONDS,
                            ris=mode if backend == "OPTIX" else False,
                            guiding=mode if backend == "CPU" else False,
                            size=size,
                        )
                        save(folder, f"{name}-{backend}-{int(mode)}-{seed}", image, meta)
                        capture_order.append(
                            dict(
                                scene=name,
                                backend=backend,
                                seed=seed,
                                enabled=mode,
                                eligible_area_lights=len(eligible),
                                resolution=size,
                            )
                        )
        (folder / "capture-order.json").write_text(json.dumps(capture_order, indent=2), encoding="utf-8")
    else:
        raise ValueError(suite)


def analyze(suite, folder):
    rows = []
    result = "passed"
    if suite == "furnace":
        for material in FURNACE_CASES:
            for backend in ("CPU", "CUDA", "OPTIX"):
                images = [load(folder, f"{material}-{backend}-{seed}") for seed in SEEDS[:2]]
                if material == "nested":
                    samples = [
                        np.stack([image[8:-8, 24 * i + 4 : 24 * i + 20].mean((0, 1)) for i in range(4)])
                        for image in images
                    ]
                    expected = np.asarray(NESTED_EXPECTED)[:, None]
                else:
                    roi = (slice(29, 35), slice(45, 51)) if material == "absorption" else (slice(24, 40), slice(36, 60))
                    samples = [image[roi].mean((0, 1)) for image in images]
                    expected = math.exp(-9 / 8) if material == "absorption" else 1.0
                signed_error = np.mean(samples, axis=0) - expected
                # Independent same-backend seeds bound stochastic uncertainty;
                # the absolute cap prevents a noisy case being reported green.
                noise = float(np.max(np.abs(samples[0] - samples[1])) / math.sqrt(2))
                error = float(np.max(np.abs(signed_error)))
                tolerance = max(0.02, 3 * noise)
                state = "inconclusive" if noise > 0.02 else "failed" if error > tolerance else "passed"
                rows.append(
                    dict(
                        case=material,
                        backend=backend,
                        result=state,
                        signed_rgb=np.asarray(signed_error).tolist(),
                        error=error,
                        same_backend_noise=noise,
                        tolerance=tolerance,
                    )
                )
                if state == "failed":
                    result = "failed"
                elif state == "inconclusive" and result == "passed":
                    result = "inconclusive"
    elif suite == "bias":
        manifest = json.loads((folder / "inputs.json").read_text(encoding="utf-8"))
        if len(manifest["run_id"]) != 32 or any(
            character not in "0123456789abcdef" for character in manifest["run_id"]
        ):
            raise ValueError("Bias capture run identifier is invalid")
        fixture_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        runtime_sha256 = json.loads(os.environ["CYCLES_HOUDINI_TEST_IDENTITY"])["manifest_sha256"]
        for name in BIAS_SCENES:
            stock = [load(folder / "stock", f"{name}-{seed}") for seed in BIAS_SEEDS]
            fork = [load(folder / "fork", f"{name}-{seed}") for seed in BIAS_SEEDS]
            for seed in BIAS_SEEDS:
                key = f"{name}-{seed}"
                stock_meta = json.loads((folder / "stock" / f"{key}.json").read_text(encoding="utf-8"))
                fork_meta = json.loads((folder / "fork" / f"{key}.json").read_text(encoding="utf-8"))
                if (
                    stock_meta["role"] != "stock"
                    or fork_meta["role"] != "fork"
                    or stock_meta["case"] != name
                    or fork_meta["case"] != name
                    or stock_meta["seed"] != seed
                    or fork_meta["seed"] != seed
                    or stock_meta["run_id"] != manifest["run_id"]
                    or fork_meta["run_id"] != manifest["run_id"]
                    or stock_meta["input_sha256"] != manifest["inputs"][key]
                    or stock_meta["input_sha256"] != fork_meta["input_sha256"]
                    or not bias_image_matches(folder / "stock", key, stock_meta)
                    or not bias_image_matches(folder / "fork", key, fork_meta)
                    or stock_meta["executable_sha256"] == fork_meta["executable_sha256"]
                    or stock_meta["samples"] != BIAS_SPP
                    or fork_meta["samples"] != BIAS_SPP
                    or not math.isfinite(stock_meta["wall_seconds"])
                    or not math.isfinite(fork_meta["wall_seconds"])
                    or stock_meta["wall_seconds"] <= 0
                    or fork_meta["wall_seconds"] <= 0
                    or len(stock_meta["mean_rgb"]) != 3
                    or len(fork_meta["mean_rgb"]) != 3
                    or not np.isfinite(stock_meta["mean_rgb"]).all()
                    or not np.isfinite(fork_meta["mean_rgb"]).all()
                ):
                    raise ValueError("Bias input or stock/fork executable provenance is invalid")
                # BVH2 against Embree differs by intersection rounding, not by engine patches.
                require_like_backends(stock_meta.get("bvh_backend"), fork_meta.get("bvh_backend"))
            comparison = evaluate_bias(stock, fork)
            rows.append(dict(scene=name, comparison="pristine_stock_vs_fork_defaults", **comparison))
            if comparison["result"] == "failed":
                result = "failed"
            elif comparison["result"] == "inconclusive" and result == "passed":
                result = "inconclusive"
        for feature in ("ris_analytic", "guiding_analytic"):
            off = [load(folder / "feature", f"{feature}-off-{seed}") for seed in BIAS_SEEDS]
            on = [load(folder / "feature", f"{feature}-on-{seed}") for seed in BIAS_SEEDS]
            for seed in BIAS_SEEDS:
                off_meta = json.loads((folder / "feature" / f"{feature}-off-{seed}.json").read_text(encoding="utf-8"))
                on_meta = json.loads((folder / "feature" / f"{feature}-on-{seed}.json").read_text(encoding="utf-8"))
                if (
                    off_meta["scene"] != on_meta["scene"]
                    or off_meta["scene"] != "courtyard"
                    or off_meta["role"] != "fork"
                    or on_meta["role"] != "fork"
                    or off_meta["feature"] != feature
                    or on_meta["feature"] != feature
                    or off_meta["seed"] != seed
                    or on_meta["seed"] != seed
                    or off_meta["run_id"] != manifest["run_id"]
                    or on_meta["run_id"] != manifest["run_id"]
                    or not bias_image_matches(folder / "feature", f"{feature}-off-{seed}", off_meta)
                    or not bias_image_matches(folder / "feature", f"{feature}-on-{seed}", on_meta)
                    or off_meta["device"] != on_meta["device"]
                    or off_meta["backend"] != "CPU"
                    or on_meta["backend"] != "CPU"
                    or off_meta["render_device_ids"] != [off_meta["device"]]
                    or on_meta["render_device_ids"] != [on_meta["device"]]
                    or off_meta["samples"] != BIAS_SPP
                    or on_meta["samples"] != BIAS_SPP
                    or len(off_meta["eligible_analytic_lights"]) < 2
                    or off_meta["eligible_analytic_lights"] != on_meta["eligible_analytic_lights"]
                    or not off_meta["shadow_catcher_light_requested"]
                    or not on_meta["shadow_catcher_light_requested"]
                    or off_meta["fixture_sha256"] != fixture_sha256
                    or on_meta["fixture_sha256"] != fixture_sha256
                    or off_meta["runtime_manifest_sha256"] != runtime_sha256
                    or on_meta["runtime_manifest_sha256"] != runtime_sha256
                    or not math.isfinite(off_meta["trace_seconds"])
                    or not math.isfinite(on_meta["trace_seconds"])
                    or not math.isfinite(off_meta["wall_seconds"])
                    or not math.isfinite(on_meta["wall_seconds"])
                    or off_meta["wall_seconds"] <= 0
                    or on_meta["wall_seconds"] <= 0
                    or off_meta["trace_seconds"] < 0
                    or on_meta["trace_seconds"] < 0
                    or off_meta["enabled"]
                    or not on_meta["enabled"]
                    or off_meta["ris_requested"]
                    or on_meta["ris_requested"] != (feature == "ris_analytic")
                    or off_meta["guiding_requested"]
                    or on_meta["guiding_requested"] != (feature == "guiding_analytic")
                ):
                    raise ValueError("Bias feature pair provenance is invalid")
            comparison = evaluate_bias(off, on, feature=True)
            rows.append(dict(scene=feature, comparison="fork_feature_on_vs_off", **comparison))
            if comparison["result"] == "failed":
                result = "failed"
            elif comparison["result"] == "inconclusive" and result == "passed":
                result = "inconclusive"
    elif suite == "cross-backend":
        for name in SCENES:
            cpu_runs = [load(folder, f"{name}-CPU-{seed}") for seed in SEEDS[:2]]
            cpu = np.mean(cpu_runs, axis=0)
            cpu_noise = rmse(cpu_runs[0], cpu_runs[1]) / math.sqrt(2)
            for backend in ("CUDA", "OPTIX"):
                other_runs = [load(folder, f"{name}-{backend}-{seed}") for seed in SEEDS[:2]]
                other = np.mean(other_runs, axis=0)
                backend_noise = rmse(other_runs[0], other_runs[1]) / math.sqrt(2)
                error = rmse(other, cpu)
                score = ssim(other, cpu, data_range=1.0, channel_axis=-1)
                tolerance = max(0.025, 3 * max(cpu_noise, backend_noise))
                regional = regional_relative_summary(cpu_runs, other_runs)
                case_result = regional["result"]
                if max(cpu_noise, backend_noise) > 0.025 and case_result == "passed":
                    case_result = "inconclusive"
                try:
                    assert_image_close(other, cpu, tolerance=tolerance, min_ssim=0.95, data_range=1.0, channel_axis=-1)
                except AssertionError:
                    case_result = "failed" if case_result == "passed" else "inconclusive"
                rows.append(
                    dict(
                        scene=name,
                        backend=backend,
                        result=case_result,
                        rmse=error,
                        ssim=score,
                        same_backend_noise=max(cpu_noise, backend_noise),
                        tolerance=tolerance,
                        regional=regional,
                    )
                )
                if case_result == "failed":
                    result = "failed"
                elif case_result == "inconclusive" and result == "passed":
                    result = "inconclusive"
    elif suite == "equal-time":
        ris_scene_losses = 0
        ris_scene_wins = 0
        ris_scene_neutral = 0
        ris_scene_unresolved = 0
        ris_timing_valid = True
        activity = json.loads((folder / "ris-activity-probe.json").read_text(encoding="utf-8"))
        ris_activity_valid = (
            activity["result"] == "passed"
            and len(activity["rows"]) == len(EQUAL_SCENES["OPTIX"])
            and all(row["eligible_area_lights"] > 0 and row["active"] for row in activity["rows"])
        )
        if not ris_activity_valid:
            result = "inconclusive"
        for backend in ("OPTIX", "CPU"):
            for name in EQUAL_SCENES[backend]:
                size = (1280, 720) if name == "artist_particles" else (640, 360)
                images = {False: [], True: []}
                walls = {False: [], True: []}
                traces = {False: [], True: []}
                samples = {False: [], True: []}
                for mode in (False, True):
                    for seed in EQUAL_SEEDS:
                        key = f"{name}-{backend}-{int(mode)}-{seed}"
                        images[mode].append(load(folder, key, size=size))
                        meta = json.loads((folder / f"{key}.json").read_text(encoding="utf-8"))
                        if meta["render_device_ids"] != [meta["device"]]:
                            raise ValueError(f"Equal-time capture used a different device: {key}")
                        walls[mode].append(meta["wall_seconds"])
                        traces[mode].append(meta["trace_seconds"])
                        samples[mode].append(meta["samples"])
                paired = paired_variance_summary(images[True], images[False])
                ratio = paired["median_ratio"]
                paired_losses = paired["losses"]
                paired_wins = paired["wins"]
                wall_gap = abs(float(np.median(walls[True])) - float(np.median(walls[False])))
                equal_wall = wall_gap <= 0.1 * EQUAL_SECONDS and all(
                    0.9 * EQUAL_SECONDS <= t <= 1.2 * EQUAL_SECONDS for run in traces.values() for t in run
                )
                case_result = "inconclusive" if not equal_wall else "passed"
                effect = "unresolved"
                if backend == "OPTIX":
                    ris_timing_valid &= equal_wall
                    if not equal_wall or max(paired["pair_variance"][False]) < 1e-12:
                        ris_scene_unresolved += 1
                    elif ratio > RIS_LOSS_RATIO and paired_losses >= RIS_LOSS_REPETITIONS:
                        ris_scene_losses += 1
                        effect = "loss"
                    elif ratio < 1 / RIS_LOSS_RATIO and paired_wins >= RIS_LOSS_REPETITIONS:
                        ris_scene_wins += 1
                        effect = "win"
                    elif all(1 / RIS_LOSS_RATIO <= value <= RIS_LOSS_RATIO for value in paired["paired_ratios"]):
                        ris_scene_neutral += 1
                        effect = "neutral"
                    else:
                        ris_scene_unresolved += 1
                    if effect == "unresolved":
                        case_result = "inconclusive"
                rows.append(
                    dict(
                        scene=name,
                        backend=backend,
                        result=case_result,
                        effect=effect if backend == "OPTIX" else "guiding_comparison",
                        ratio=ratio if math.isfinite(ratio) else None,
                        ratio_state="finite" if math.isfinite(ratio) else "off_variance_zero_on_positive",
                        paired_losses=paired_losses,
                        paired_wins=paired_wins,
                        paired_rms_ratios=[
                            value if math.isfinite(value) else None for value in paired["paired_ratios"]
                        ],
                        overall_rms_ratio=paired["overall_rms_ratio"]
                        if math.isfinite(paired["overall_rms_ratio"])
                        else None,
                        pair_variance=paired["pair_variance"],
                        zero_baseline_pairs=paired["zero_baseline_pairs"],
                        samples=samples,
                        wall_seconds=walls,
                        trace_seconds=traces,
                    )
                )
                if case_result == "inconclusive":
                    result = "inconclusive"
        ris_decision = ris_release_decision(
            ris_scene_losses,
            ris_scene_wins,
            ris_scene_neutral,
            ris_scene_unresolved,
            ris_timing_valid and ris_activity_valid,
        )
        # The release decision is recorded, never executed by this runner.
        return dict(
            result=result,
            suite=suite,
            rows=rows,
            ris_decision=ris_decision,
            ris_loss_scenes=ris_scene_losses,
            ris_win_scenes=ris_scene_wins,
            ris_neutral_scenes=ris_scene_neutral,
            ris_unresolved_scenes=ris_scene_unresolved,
            ris_timing_valid=ris_timing_valid,
            ris_activity_valid=ris_activity_valid,
        )
    else:
        raise ValueError(suite)
    return dict(result=result, suite=suite, rows=rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", required=True, choices=("furnace", "bias", "cross-backend", "equal-time"))
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--role", choices=("stock", "fork"), default="fork")
    parser.add_argument("--analyze", action="store_true")
    parser.add_argument("--probe", action="store_true", help="Lightweight CPU fixture construction check")
    parser.add_argument("--numeric-probe", action="store_true", help="Synthetic equal-time verdict check")
    parser.add_argument("--ris-activity-probe", action="store_true", help="Short active-RIS OptiX contrast")
    parser.add_argument("--ris-scene", type=Path, help="Relocated audited particle HIP (SHA-256 pinned)")
    parser.add_argument("--ris-iris-image", type=Path, help="Relocated audited particle Image Iris JPG")
    parser.add_argument("--bias-probe", action="store_true", help="Short CPU RIS and guiding activation check")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.bias_probe:
        update = bias_feature_scene()
        rows = []
        for feature in ("ris_analytic", "guiding_analytic"):
            images = []
            for mode in (False, True):
                image, metadata = render(
                    update,
                    "CPU",
                    64,
                    701,
                    ris=mode if feature == "ris_analytic" else False,
                    guiding=mode if feature == "guiding_analytic" else False,
                )
                images.append(image)
                rows.append(dict(feature=feature, enabled=mode, render=metadata))
            difference = float(np.max(np.abs(images[0] - images[1])))
            rows[-1]["max_on_off_difference"] = difference
            if difference == 0:
                raise RuntimeError(f"{feature} did not change the eligible-light CPU estimator")
        report = dict(result="passed", probe="bias_feature_activation", rows=rows)
        (args.output / "bias-probe.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report))
        return 0
    if args.numeric_probe:
        report = numeric_probe()
        (args.output / "numeric-probe.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report))
        return 0
    if args.ris_activity_probe:
        print(json.dumps(ris_activity_probe(args.output, args.ris_scene, args.ris_iris_image)))
        return 0
    if args.probe:
        cases = (
            ("hair", scene),
            ("glossy_interior", scene),
            ("courtyard", scene),
            ("diffuse", furnace),
            ("refraction", furnace),
            ("transparent", furnace),
            ("translucent", furnace),
            ("nested", furnace),
        )
        rows = []
        for name, builder in cases:
            image, metadata = render(builder(name), "CPU", 8, 19)
            row = dict(case=name, mean_rgb=image.mean((0, 1)).tolist(), render=metadata)
            if name == "nested":
                row["regional_rgb"] = [image[8:-8, 24 * i + 4 : 24 * i + 20].mean((0, 1)).tolist() for i in range(4)]
                row["expected"] = NESTED_EXPECTED
            rows.append(row)
        image, metadata = render(absorption(args.output), "CPU", 8, 19)
        rows.append(
            dict(
                case="absorption",
                mean_rgb=image.mean((0, 1)).tolist(),
                regional_rgb=image[29:35, 45:51].mean((0, 1)).tolist(),
                expected=math.exp(-9 / 8),
                render=metadata,
            )
        )
        report = dict(result="passed", probe=True, rows=rows)
        (args.output / "fixture-probe.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report))
        return 0
    if args.analyze:
        report = analyze(args.suite, args.output)
        (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report))
        return 0 if report["result"] == "passed" else 2
    capture(args.suite, args.output, args.role, args.ris_scene, args.ris_iris_image)
    return 0


if __name__ == "__main__":
    sys.exit(main())
