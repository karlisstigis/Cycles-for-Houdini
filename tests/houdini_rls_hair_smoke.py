# SPDX-License-Identifier: Apache-2.0
"""RLS Huang fallback, mixed closures, live edits and unchanged non-Huang transport.

Run once against the previous package with --baseline, then against the repaired
package. compare-rls-hair.py checks the saved linear planes between builds.
"""

import argparse
import json
import os
from pathlib import Path
import time
import traceback

import hou
import numpy as np
import cycles_houdini as ch

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--device", default="CPU")
parser.add_argument("--label", required=True)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--baseline", action="store_true")
parser.add_argument("--samples", type=int, default=128)
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=True)
report_path = args.output / (args.label + ".json")
assert not report_path.exists(), report_path
report = dict(result="running", host=os.environ["CYCLES_HOUDINI_HOST_ROOT"], args=vars(args), runs=[], checks=[])


def save():
    report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")


def shader_node(identity, kind, **parameters):
    return dict(id=identity, definition="cycles/" + kind + "/1", parameters=parameters)


def endpoint(identity, socket):
    return dict(node=identity, socket=socket)


def material(kind):
    hair = shader_node(
        "hair", "principled_hair_bsdf", model="Huang", color=[0.35, 0.12, 0.035], roughness=0.3, aspect_ratio=0.85
    )
    if kind == "huang":
        nodes, links, surface = [hair], [], endpoint("hair", "BSDF")
    elif kind == "mixed_huang":
        # Keep Huang present at every hit in this exact-equivalence fixture. Elliptical
        # Huang can legitimately become transparent outside its projected radius;
        # the remaining diffuse closure then uses RLS and changes the noise realization.
        # The pure-Huang case above still covers elliptical coverage and transparency.
        hair["parameters"]["aspect_ratio"] = 1.0
        nodes = [
            hair,
            shader_node("diffuse", "diffuse_bsdf", color=[0.3, 0.5, 0.7]),
            shader_node("mix", "mix_closure", fac=0.35),
        ]
        links = [
            {"from": endpoint("hair", "BSDF"), "to": endpoint("mix", "closure1")},
            {"from": endpoint("diffuse", "BSDF"), "to": endpoint("mix", "closure2")},
        ]
        surface = endpoint("mix", "closure")
    else:
        definitions = {
            "chiang": ("principled_hair_bsdf", dict(model="Chiang", color=[0.35, 0.12, 0.035], roughness=0.3)),
            "diffuse": ("diffuse_bsdf", dict(color=[0.3, 0.5, 0.7], roughness=0.25)),
            "glossy": ("glossy_bsdf", dict(color=[0.7, 0.5, 0.3], roughness=0.25)),
            "glass": ("glass_bsdf", dict(color=[0.8, 0.95, 1.0], roughness=0.15, IOR=1.45)),
            "principled": ("principled_bsdf", dict(base_color=[0.45, 0.25, 0.1], roughness=0.3)),
            "subsurface": (
                "principled_bsdf",
                dict(base_color=[0.65, 0.25, 0.1], roughness=0.3, subsurface_weight=0.6, subsurface_scale=0.1),
            ),
        }
        definition, parameters = definitions[kind]
        nodes, links, surface = [shader_node("surface", definition, **parameters)], [], endpoint("surface", "BSDF")
    return dict(key="receiver", kind="graph", graph=dict(schema_version=1, nodes=nodes, links=links, surface=surface))


def planes(frame):
    result = {
        name: np.frombuffer(plane["pixels"], np.float32)
        .reshape(frame["height"], frame["width"], plane["channels"])
        .copy()
        for name, plane in frame["planes"].items()
    }
    assert all(np.isfinite(value).all() for value in result.values()), "Non-finite render"
    return result


def difference(a, b):
    assert a.keys() == b.keys()
    return {key: float(np.max(np.abs(a[key] - b[key]))) for key in a}


try:
    devices = ch.devices()
    if args.device == "optix4090":
        device = next(d["id"] for d in devices if d["backend"] == "OPTIX" and "4090" in d["name"])
    elif args.device == "CPU":
        device = next(d["id"] for d in devices if d["backend"] == "CPU")
    else:
        device = args.device
    report["device"] = device
    obj = hou.node("/obj").createNode("geo", "rls_hair_fixture", run_init_scripts=False)
    hair = obj.createNode("python", "strands")
    hair.parm("python").set("""import hou, math
g=hou.pwd().geometry()
g.addAttrib(hou.attribType.Point, 'width', 0.08)
for i in range(25):
    curve=g.createPolygon(is_closed=False)
    x=(i-12)*0.1
    for j in range(5):
        p=g.createPoint()
        p.setPosition((x+0.03*math.sin(j+i), -0.9+j*0.45, 0.06*math.sin(i*0.7)))
        curve.addVertex(p)
""")
    sphere = obj.createNode("sphere", "receiver_sphere")
    sphere.parm("type").set("polymesh")
    sphere.parm("freq").set(5)
    sphere.parmTuple("rad").set((0.9, 0.9, 0.9))
    camera = hou.node("/obj").createNode("cam", "rls_hair_camera")
    camera.setParms(dict(tz=5, projection=1, orthowidth=3.2))
    lights = [
        dict(
            key="key",
            kind="area",
            size=(1.2, 0.8),
            strength=(40, 35, 28),
            transform=(1, 0, 0, -0.8, 0, 1, 0, 0.8, 0, 0, 1, 2.2),
        ),
        dict(
            key="fill",
            kind="area",
            size=(0.5, 0.7),
            strength=(12, 15, 22),
            transform=(1, 0, 0, 0.9, 0, 1, 0, -0.5, 0, 0, 1, 1.5),
        ),
        dict(
            key="sphere_light",
            kind="point",
            radius=0.2,
            strength=(15, 12, 10),
            transform=(1, 0, 0, 0.7, 0, 1, 0, 1.0, 0, 0, 1, 1.7),
        ),
    ]
    for light in lights:
        light["use_mis"] = True
    passes = [
        "combined",
        "diffuse_direct",
        "diffuse_indirect",
        "glossy_direct",
        "glossy_indirect",
        "transmission_direct",
        "transmission_indirect",
        "denoising_albedo",
        "denoising_normal",
    ]
    base = dict(
        samples=args.samples,
        adaptive_sampling=False,
        noise_threshold=0.01,
        min_samples=0,
        seed=17,
        animate_seed=False,
        sample_subset=False,
        time_limit=0,
        sampling_pattern="blue_noise",
        clamp_direct=0.0,
        clamp_indirect=0.0,
        light_tree=True,
        light_threshold=0.0,
    )
    with ch.Session(
        device=device,
        width=64,
        height=48,
        samples=args.samples,
        threads=4,
        interactive=True,
        execution="worker",
        denoising={"enabled": False},
        passes=passes,
        collect_diagnostics=True,
    ) as session:

        def render(kind, enabled, seed=17, tree=True, clamp=0.0, tag=None):
            rendering = dict(base, seed=seed, resampled_light_sampling=enabled, light_tree=tree, clamp_direct=clamp)
            source = hair if kind in ("huang", "mixed_huang", "chiang") else sphere
            request = ch.geometry_request(obj, source, material="receiver")
            if kind == "mixed_huang":
                # Ribbons keep the circular Huang h coordinate inside (-1, 1),
                # avoiding a numerical tangent hit being replaced by transparency.
                request["curve_shape"] = "ribbon"
            update = dict(
                geometry=[request],
                materials=[material(kind)],
                lights=lights,
                camera_path=camera.path(),
                background=(0, 0, 0),
                transparent_background=True,
                denoising={"enabled": False},
                rendering=rendering,
            )
            started = time.monotonic()
            frame = session.wait(session.apply(update), timeout=240)
            result = planes(frame)
            assert float(result["combined"][..., :3].max()) > 1e-5, (kind, "Unlit fixture")
            assert np.any(result["combined"][..., 3] > 0.5), (kind, "No geometry coverage")
            status = session.status()
            assert status["curves"] > 0 if source == hair else status["curves"] == 0, status
            name = tag or f"{kind}-s{seed}-tree{int(tree)}-clamp{clamp:g}-{int(enabled)}"
            np.savez_compressed(args.output / (args.label + "-" + name + ".npz"), **result)
            report["runs"].append(
                dict(
                    name=name,
                    material=kind,
                    rls=enabled,
                    seed=seed,
                    tree=tree,
                    clamp=clamp,
                    seconds=time.monotonic() - started,
                    mean_rgb=result["combined"][..., :3].mean((0, 1)).tolist(),
                    alpha_mean=float(result["combined"][..., 3].mean()),
                )
            )
            save()
            print(name, flush=True)
            return result

        for kind in ("huang", "mixed_huang", "chiang", "diffuse", "glossy", "glass", "principled", "subsurface"):
            for seed in (17, 29):
                off = render(kind, False, seed)
                on = render(kind, True, seed)
                if kind in ("huang", "mixed_huang"):
                    delta = difference(off, on)
                    report["checks"].append(dict(name="Huang native fallback", material=kind, seed=seed, max_abs=delta))
                    if not args.baseline:
                        if any(not np.allclose(off[key], on[key], rtol=3e-6, atol=3e-6) for key in off):
                            repeated_off = render(kind, False, seed, tag=f"{kind}-s{seed}-repeat-off")
                            repeated_on = render(kind, True, seed, tag=f"{kind}-s{seed}-repeat-on")
                            report["checks"].append(
                                dict(
                                    name="Native/RLS repeat controls",
                                    material=kind,
                                    seed=seed,
                                    native_repeat=difference(off, repeated_off),
                                    rls_repeat=difference(on, repeated_on),
                                    repeated_pair=difference(repeated_off, repeated_on),
                                )
                            )
                            save()
                        for key in off:
                            assert np.allclose(off[key], on[key], rtol=3e-6, atol=3e-6), (kind, key, delta)
        for kind in ("huang", "mixed_huang"):
            for tree, clamp in ((False, 0.0), (True, 2.0)):
                off = render(kind, False, tree=tree, clamp=clamp)
                on = render(kind, True, tree=tree, clamp=clamp)
                if not args.baseline:
                    for key in off:
                        assert np.allclose(off[key], on[key], rtol=3e-6, atol=3e-6), (kind, tree, clamp, key)
        if not args.baseline:
            # Replace the shader while the worker is actively tracing, then cancel/restart.
            long_render = dict(base, samples=100000, resampled_light_sampling=True)
            generation = session.apply(dict(materials=[material("chiang")], rendering=long_render))
            deadline = time.monotonic() + 20
            while True:
                status = session.status()  # Pumps deferred host capture in headless Houdini.
                # A headless worker without a viewer may defer preview frames. Its
                # committed generation and sample counter still prove active tracing.
                if (
                    status.get("committed_generation") == generation
                    and status.get("backend_state") == "rendering"
                    and status.get("current_sample", 0) > 0
                    and not status.get("pending_update")
                ):
                    break
                assert time.monotonic() < deadline, status
                time.sleep(0.02)
            # The next render atomically replaces Chiang by a mixed Huang material mid-render.
            live = render("mixed_huang", True, tag="live-replacement")
            session.cancel()
            restarted = render("mixed_huang", True, tag="after-stop-restart")
            for key in live:
                assert np.allclose(live[key], restarted[key], rtol=3e-6, atol=3e-6), key
            report["checks"].append(dict(name="Live shader replacement and Stop/restart", result="passed"))
    report["result"] = "passed"
except Exception:
    report["result"] = "failed"
    report["error"] = traceback.format_exc()
finally:
    save()
print(json.dumps(dict(result=report["result"], report=str(report_path))))
raise SystemExit(0 if report["result"] == "passed" else 1)
