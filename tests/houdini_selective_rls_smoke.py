# SPDX-License-Identifier: Apache-2.0
"""Selected glossy fallback and unchanged RLS transport on the other closures."""

import argparse
import json
import os
from pathlib import Path
import traceback
import hou
import numpy as np
import cycles_houdini as ch

p = argparse.ArgumentParser()
p.add_argument("--output", type=Path, required=True)
p.add_argument("--label", required=True)
p.add_argument("--device", default="CPU")
p.add_argument("--baseline", type=Path)
a = p.parse_args()
out = a.output / a.label
out.mkdir(parents=True, exist_ok=False)
report = dict(result="running", runtime=ch.__file__, runs=[], checks=[])


def save():
    (out / "report.json").write_text(json.dumps(report, indent=2))


def node(identity, kind, **parameters):
    return dict(id=identity, definition="cycles/" + kind + "/1", parameters=parameters)


def end(identity, socket):
    return dict(node=identity, socket=socket)


def material(kind):
    sharp = node("sharp", "principled_bsdf", base_color=[0.3, 0.5, 0.7], metallic=1.0, roughness=0.053)
    definitions = {
        "sharp": ("principled_bsdf", dict(base_color=[0.3, 0.5, 0.7], metallic=1.0, roughness=0.053)),
        "sharp_coat": (
            "principled_bsdf",
            dict(base_color=[0.3, 0.5, 0.7], metallic=1.0, roughness=0.053, coat_weight=0.4, coat_roughness=0.07),
        ),
        "sharp_boundary": ("glossy_bsdf", dict(color=[0.3, 0.5, 0.7], roughness=0.099)),
        "rough_boundary": ("glossy_bsdf", dict(color=[0.3, 0.5, 0.7], roughness=0.101)),
        "anisotropic": (
            "principled_bsdf",
            dict(base_color=[0.3, 0.5, 0.7], metallic=1.0, roughness=0.09, anisotropic=0.9),
        ),
        "diffuse": ("diffuse_bsdf", dict(color=[0.3, 0.5, 0.7], roughness=0.25)),
        "rough_glossy": ("glossy_bsdf", dict(color=[0.3, 0.5, 0.7], roughness=0.25)),
        "glass": ("glass_bsdf", dict(color=[0.8, 0.95, 1.0], roughness=0.053, IOR=1.45)),
        "subsurface": (
            "principled_bsdf",
            dict(base_color=[0.65, 0.25, 0.1], roughness=0.3, subsurface_weight=0.6, subsurface_scale=0.1),
        ),
        "chiang": ("principled_hair_bsdf", dict(model="Chiang", color=[0.35, 0.12, 0.035], roughness=0.3)),
        "huang": (
            "principled_hair_bsdf",
            dict(model="Huang", color=[0.35, 0.12, 0.035], roughness=0.3, aspect_ratio=1.0),
        ),
    }
    links = []
    surface = end("surface", "BSDF")
    if kind == "mixed":
        nodes = [sharp, node("diffuse", "diffuse_bsdf", color=[0.3, 0.5, 0.7]), node("mix", "mix_closure", fac=0.35)]
        links = [
            dict(**{"from": end("sharp", "BSDF"), "to": end("mix", "closure1")}),
            dict(**{"from": end("diffuse", "BSDF"), "to": end("mix", "closure2")}),
        ]
        surface = end("mix", "closure")
    else:
        definition, parameters = definitions[kind]
        nodes = [node("surface", definition, **parameters)]
    return dict(key="surface", kind="graph", graph=dict(schema_version=1, nodes=nodes, links=links, surface=surface))


try:
    device = (
        next(d["id"] for d in ch.devices() if d["backend"] == "OPTIX" and "4090" in d["name"])
        if a.device == "optix4090"
        else a.device
    )
    obj = hou.node("/obj").createNode("geo", "selective_rls", run_init_scripts=False)
    sphere = obj.createNode("sphere")
    sphere.parm("type").set("polymesh")
    sphere.parm("freq").set(4)
    hair = obj.createNode("python", "hair")
    hair.parm("python").set("""import hou
g=hou.pwd().geometry();g.addAttrib(hou.attribType.Point,'width',.1)
for i in range(15):
 c=g.createPolygon(is_closed=False)
 for j in range(4):
  p=g.createPoint();p.setPosition(((i-7)*.12,(j-1.5)*.5,0));c.addVertex(p)
""")
    camera = hou.node("/obj").createNode("cam")
    camera.setParms(dict(tz=5, projection=1, orthowidth=3.2))
    lights = [
        dict(
            key="key",
            kind="area",
            size=(1.2, 0.8),
            strength=(40, 35, 28),
            use_mis=True,
            transform=(1, 0, 0, -0.8, 0, 1, 0, 0.8, 0, 0, 1, 2.2),
        ),
        dict(
            key="fill",
            kind="area",
            size=(0.5, 0.7),
            strength=(12, 15, 22),
            use_mis=True,
            transform=(1, 0, 0, 0.9, 0, 1, 0, -0.5, 0, 0, 1, 1.5),
        ),
    ]
    kinds = [
        "sharp",
        "sharp_coat",
        "sharp_boundary",
        "rough_boundary",
        "anisotropic",
        "mixed",
        "diffuse",
        "rough_glossy",
        "glass",
        "subsurface",
        "chiang",
        "huang",
    ]
    selected = {"sharp", "sharp_coat", "sharp_boundary"}
    names = [
        "combined",
        "diffuse_direct",
        "diffuse_indirect",
        "diffuse_color",
        "glossy_direct",
        "glossy_indirect",
        "glossy_color",
        "transmission_direct",
        "transmission_indirect",
        "transmission_color",
        "denoising_albedo",
        "denoising_normal",
    ]
    with ch.Session(
        device=device,
        width=64,
        height=48,
        samples=64,
        threads=4,
        interactive=True,
        execution="worker",
        denoising={"enabled": False},
        passes=names,
    ) as session:
        for kind in kinds:
            results = {}
            for enabled in (False, True):
                request = ch.geometry_request(obj, hair if kind in ("huang", "chiang") else sphere, material="surface")
                if kind == "huang":
                    request["curve_shape"] = "ribbon"
                update = dict(
                    geometry=[request],
                    materials=[material(kind)],
                    lights=lights,
                    camera_path=camera.path(),
                    background=(0, 0, 0),
                    transparent_background=True,
                    denoising={"enabled": False},
                    rendering=dict(
                        samples=64,
                        adaptive_sampling=False,
                        seed=17,
                        animate_seed=False,
                        time_limit=0,
                        light_tree=True,
                        light_threshold=0,
                        clamp_direct=0,
                        clamp_indirect=0,
                        resampled_light_sampling=enabled,
                    ),
                )
                frame = session.wait(session.apply(update), timeout=120)
                status = session.status()
                assert not status["error"], status
                planes = {
                    k: np.frombuffer(v["pixels"], np.float32).reshape(48, 64, v["channels"]).copy()
                    for k, v in frame["planes"].items()
                }
                assert all(np.isfinite(v).all() for v in planes.values())
                assert planes["combined"][..., :3].max() > 1e-5, (kind, "Unlit fixture")
                reconstructed = sum(
                    (planes[k + "_direct"] + planes[k + "_indirect"]) * planes[k + "_color"]
                    for k in ("diffuse", "glossy", "transmission")
                )
                # Preserve every raw plane before asserting, including a failing mixed-closure case.
                name = f"{kind}-{int(enabled)}.npz"
                np.savez_compressed(out / name, **planes)
                error = np.abs(reconstructed - planes["combined"][..., :3])
                report["checks"].append(
                    dict(
                        kind=kind,
                        rls=enabled,
                        check="light-pass reconstruction",
                        max_absolute=float(error.max()),
                        rmse=float(np.sqrt(np.mean(error**2))),
                    )
                )
                save()
                assert np.allclose(reconstructed, planes["combined"][..., :3], atol=2e-5, rtol=2e-5), (
                    kind,
                    "Light-pass reconstruction",
                )
                results[enabled] = planes
                if a.baseline and (not enabled or kind not in selected):
                    with np.load(a.baseline / name) as previous:
                        errors = {k: float(np.max(np.abs(v - previous[k]))) for k, v in planes.items()}
                        assert all(np.allclose(v, previous[k], atol=3e-6, rtol=3e-6) for k, v in planes.items()), (
                            kind,
                            enabled,
                            errors,
                        )
                    report["checks"].append(dict(kind=kind, rls=enabled, baseline_max_errors=errors))
                report["runs"].append(dict(kind=kind, rls=enabled, alpha_mean=float(planes["combined"][..., 3].mean())))
                save()
            # The fallback contract is a property of this current runtime;
            # independent historical transport files are optional controls.
            if kind in selected or kind == "huang":
                assert all(np.allclose(v, results[False][k], atol=3e-6, rtol=3e-6) for k, v in results[True].items()), (
                    kind
                )
            if kind == "diffuse":
                assert np.max(np.abs(results[True]["combined"] - results[False]["combined"])) > 1e-4, (
                    "RLS was disabled globally"
                )
    report.update(result="passed", device=device)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    print(report["error"], flush=True)
save()
print(json.dumps(dict(result=report["result"], runs=len(report["runs"]))))
raise SystemExit(0 if report["result"] == "passed" else 1)
