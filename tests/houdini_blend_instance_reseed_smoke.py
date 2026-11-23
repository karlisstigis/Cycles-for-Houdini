# SPDX-License-Identifier: Apache-2.0
"""Compare reseeded external point instances with a fresh engine after every edit.

Authored native mesh fixtures isolate index/prototype identity from Blender export. Each
frame has a different silhouette; shuffled assignments, changing prototype
membership, and revisiting old frames exercise incremental packing. The default
keeps topology counts and material equal so buffer repacking or shader edits
cannot conceal a missing scene-BVH update when cached prototypes exchange owners.
"""

from support.paths import artifacts_root

import argparse
import json
from pathlib import Path

import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import blender_assets, native_scene, materials
from blend_direct_fixture import mesh, prototype as direct_prototype
from cycles_houdini.device_selection import set_devices


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--out", type=Path)
parser.add_argument("--report", type=Path)
parser.add_argument("--device", default="CPU")
parser.add_argument("--execution", choices=("worker", "sync"), default="worker")
parser.add_argument("--motion", action="store_true")
parser.add_argument("--dlss", action="store_true")
parser.add_argument(
    "--same-topology",
    dest="same_topology",
    action="store_true",
    default=True,
    help="Use differently shaped boxes sharing primitive counts and material (default)",
)
parser.add_argument("--different-topology", dest="same_topology", action="store_false")
args = parser.parse_args()
selected = next(d for d in ch.devices() if d["id"] == args.device or d["backend"] == args.device.upper())
assert not args.dlss or selected["backend"] == "OPTIX", "DLSS requires OptiX"
if args.out is None:
    args.out = args.report.parent if args.report else artifacts_root() / "verification/blend-reseed"
args.out.mkdir(parents=True, exist_ok=True)
device = selected["id"]
hou.setFrame(1)
root = hou.node("/obj").createNode("subnet", "reseed_fixture")
owned = root.createNode("subnet", "owned")
owned.setUserData("cycles_external_blend", "1")
source_obj = root.createNode("geo", "points", run_init_scripts=False)
source = source_obj.createNode("attribwrangle", "indices")
source.parm("class").set(0)
source.setDisplayFlag(True)
source.setRenderFlag(True)
camera = root.createNode("cam", "camera")
camera.setParms(dict(tz=8, projection=1, orthowidth=7))
rop = hou.node("/out").createNode("cycles_ipr", "reseed_fixture")
set_devices(rop, device)
rop.setParms(
    dict(
        source_mode="objects",
        objectroot=root.path(),
        vobject="points",
        lights="",
        camera=camera.path(),
        resx=96,
        resy=72,
        samples=8,
        ipr_samples=8,
        use_denoise=0,
        ipr_use_denoise=0,
        adaptive_sampling=0,
        ipr_adaptive_sampling=0,
        transparent_background=1,
        allowmotionblur=int(args.motion),
        use_material_assignments=1,
        render_threads=2,
    )
)
if args.motion:
    source_obj.parm("tx").setExpression(".1*$T", hou.exprLanguage.Hscript)
if args.dlss:
    rop.setParms(dict(ipr_use_denoise=1, ipr_denoiser="dlss", ipr_dlss_quality="native"))
material = materials.create_material(
    name="reseed_material", shader="diffuse_bsdf" if args.same_topology else "emission"
)
material.node("surface_shader").parmTuple("color").set((0.3, 0.6, 0.9))
rop.parmTuple("background").set((0.3, 0.3, 0.3))
asset = args.out.resolve() / "identity_fixture.blend"
asset.write_bytes(b"owned-houdini-prototype-fixture")
reference = asset.as_posix() + "#object=Shape"
prepared = {}


def prepare(value, frame, *, native_subdivision=True):
    assert value == reference
    if frame not in prepared:
        kind = "box" if args.same_topology else {1.0: "box", 2.0: "sphere", 3.0: "torus"}[frame]
        size = (
            {1.0: (0.3, 1.1, 0.3), 2.0: (1.1, 0.3, 0.3), 3.0: (0.7, 0.7, 0.3)}[frame]
            if args.same_topology
            else (0.8, 0.8, 0.4)
        )
        key = "reseed/mesh/" + repr(frame)
        prepared[frame] = direct_prototype(key, mesh(key, size, material.path(), kind))
    return prepared[frame]


def author(indices):
    token = json.dumps(reference)
    source.parm("snippet").set(
        "float frames[]={" + ",".join(str(float(i)) for i in indices) + "}; "
        "foreach(int i; float frame; frames){int p=addpoint(0,set((i%3-1)*1.8,(i/3-.5)*1.7,0)); "
        'setpointattrib(0,"id",p,i); setpointattrib(0,"index",p,frame); '
        f'setpointattrib(0,"instance",p,{token});' + "}"
    )


def pixels(session, name="combined"):
    frame = session.frame()
    plane = frame["planes"][name]
    return np.frombuffer(plane["pixels"], np.float32).reshape(frame["height"], frame["width"], plane["channels"]).copy()


def dilate(mask):
    padded = np.pad(mask, 1)
    return np.logical_or.reduce(
        [padded[y : y + mask.shape[0], x : x + mask.shape[1]] for y in range(3) for x in range(3)]
    )


def render(session):
    update = native_scene.capture(rop)
    if args.dlss:
        update["passes"] = ["combined", "noisy_combined"]
    session.wait(session.apply(update))
    status = session.status()
    assert not status["error"], status
    assert not any(d["code"].startswith("instancing.") for d in status["source_diagnostics"]), status[
        "source_diagnostics"
    ]
    if args.dlss:
        assert status["denoiser"] == "dlss", status
        status["_beauty"] = pixels(session)
    return pixels(session, "noisy_combined" if args.dlss else "combined"), status


original = blender_assets.prepare_prototype
original_many = blender_assets.prepare_prototypes
original_warmup = blender_assets.prewarm_prototypes
blender_assets.prepare_prototype = prepare


def prepare_many(specs):
    results = []
    for reference, frame, native in specs:
        try:
            results.append(blender_assets.prepare_prototype(reference, frame, native_subdivision=native))
        except Exception as error:
            results.append(error)
    return results


blender_assets.prepare_prototypes = prepare_many
blender_assets.prewarm_prototypes = lambda specs: None
report = dict(
    result="failed",
    device=device,
    execution=args.execution,
    motion=args.motion,
    dlss=args.dlss,
    same_topology=args.same_topology,
    checks=[],
)
options = dict(device=device, width=96, height=72, samples=8, threads=2, execution=args.execution)
if args.dlss:
    options["denoising"] = dict(enabled=True, method="dlss", upscale_factor=1)
try:
    with ch.Session(**options, collect_diagnostics=True) as session:
        for sequence, indices in enumerate(
            ((1, 2, 3, 1, 2, 3), (3, 1, 2, 2, 3, 1), (2, 2, 1, 1, 2, 1), (3, 3, 3, 3, 3, 3), (1, 2, 3, 1, 2, 3))
        ):
            author(indices)
            actual, status = render(session)
            with ch.Session(**options) as fresh:
                expected, fresh_status = render(fresh)
            difference = np.abs(actual - expected)
            row = dict(
                sequence=sequence,
                indices=indices,
                max_abs=float(difference.max()),
                mean_abs=float(difference.mean()),
                identical=bool(np.array_equal(actual, expected)),
                conversions=status.get("geometry_conversions"),
            )
            if args.dlss:
                beauty_difference = np.abs(status["_beauty"] - fresh_status["_beauty"])
                assert np.isfinite(beauty_difference).all()
                # Neural temporal reconstruction is measured separately from
                # the raw intersection/packing contract checked below.
                row.update(
                    dlss_beauty_max_abs=float(beauty_difference.max()),
                    dlss_beauty_mean_abs=float(beauty_difference.mean()),
                )
                # DLSS advances camera jitter between frames. A fresh worker
                # starts its own pattern, so compare raw silhouettes within
                # one raster pixel instead of requiring identical edge samples.
                actual_mask, expected_mask = actual[:, :, 3] > 0.1, expected[:, :, 3] > 0.1
                row["unmatched_silhouette_pixels"] = int(
                    np.count_nonzero(actual_mask & ~dilate(expected_mask))
                    + np.count_nonzero(expected_mask & ~dilate(actual_mask))
                )
            if sequence in (1, 2):
                assert row["conversions"] == report["checks"][0]["conversions"], row
            report["checks"].append(row)
            print(json.dumps(row), flush=True)
            assert np.isfinite(actual).all() and np.count_nonzero(actual[:, :, 3] > 0.1) > 100
            if args.dlss:
                assert row["unmatched_silhouette_pixels"] == 0 and row["mean_abs"] < 0.02, row
            else:
                assert row["max_abs"] < (1e-7 if selected["backend"] == "CPU" else 1e-5), row
    report["result"] = "passed"
finally:
    blender_assets.prepare_prototype = original
    blender_assets.prepare_prototypes = original_many
    blender_assets.prewarm_prototypes = original_warmup
    target = args.report or args.out / (
        "reseed-"
        + selected["backend"]
        + ("-motion" if args.motion else "")
        + ("-dlss" if args.dlss else "")
        + ("-same-topology" if args.same_topology else "")
        + ".json"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2))
print(json.dumps(report), flush=True)
