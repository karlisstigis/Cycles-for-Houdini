# SPDX-License-Identifier: Apache-2.0
"""Blender 5.3 spectral/nested interface renders, live edits and zero-dispersion controls.

Runs real CPU/GPU kernels through the isolated verified runtime. Captures two different
Abbe media, camera-inside probes, procedural IOR, buried priority crossings and MNEE
fallback controls. This fixture supplements the independent --dispersion-contracts
spectroscopic/Snell oracle; finite images alone do not certify transport accuracy.
"""

import argparse, json, traceback
from pathlib import Path
import hou
import numpy as np
import cycles_houdini as ch

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("--output", type=Path, required=True)
p.add_argument("--device", default="CPU")
p.add_argument("--samples", type=int, default=64)
p.add_argument("--oracle-samples", type=int, default=512)
a = p.parse_args()
a.output.mkdir(parents=True, exist_ok=False)
report = dict(result="running", runtime=ch.__file__, device=a.device, runs=[], checks=[])


def save():
    (a.output / "report.json").write_text(json.dumps(report, indent=2))


def node(identity, kind, **parameters):
    return dict(id=identity, definition="cycles/" + kind + "/1", parameters=parameters)


def end(identity, socket):
    return dict(node=identity, socket=socket)


def link(source, output, target, input):
    return {"from": end(source, output), "to": end(target, input)}


def graph(key, nodes, links, identity, socket="BSDF"):
    return dict(
        key=key, kind="graph", graph=dict(schema_version=1, nodes=nodes, links=links, surface=end(identity, socket))
    )


def dielectric(key, nd, abbe, scale, procedural=False):
    nodes = [
        node(
            "surface",
            "principled_bsdf",
            base_color=[1, 1, 1],
            roughness=0.0,
            ior=nd,
            transmission_weight=1.0,
            transmission_dispersion_scale=scale,
            transmission_dispersion_abbe_number=abbe,
        )
    ]
    links = []
    if procedural:
        nodes += [
            node("noise", "noise_texture", scale=2.0),
            node("range", "math", math_type="multiply_add", value2=0.1, value3=nd),
        ]
        links += [link("noise", "fac", "range", "value1"), link("range", "value", "surface", "ior")]
    return graph(key, nodes, links, "surface")


try:
    device = (
        next(d["id"] for d in ch.devices() if d["backend"] == "OPTIX" and "4090" in d["name"])
        if a.device == "optix4090"
        else a.device
    )
    outer = hou.node("/obj").createNode("geo", "spectral_outer", run_init_scripts=False)
    sphere = outer.createNode("sphere")
    sphere.parm("type").set("polymesh")
    sphere.parm("freq").set(5)
    sphere.parmTuple("rad").set((1.8, 1.8, 1.8))
    inner = hou.node("/obj").createNode("geo", "spectral_inner", run_init_scripts=False)
    small = inner.createNode("sphere")
    small.parm("type").set("polymesh")
    small.parm("freq").set(5)
    small.parmTuple("rad").set((1.25, 1.25, 1.25))
    inner.parm("tx").set(0.35)
    screen = hou.node("/obj").createNode("geo", "spectral_screen", run_init_scripts=False)
    plane = screen.createNode("python")
    plane.parm("python").set("""import hou
g=hou.pwd().geometry();poly=g.createPolygon()
for xyz in [(-8,-6,-3),(8,-6,-3),(8,6,-3),(-8,6,-3)]:
 p=g.createPoint();p.setPosition(xyz);poly.addVertex(p)
""")
    camera = hou.node("/obj").createNode("cam", "spectral_camera")
    camera.parm("tz").set(6)
    emitter = graph(
        "screen",
        [
            node("position", "geometry"),
            node("checker", "checker_texture", scale=3.0, color1=[0.03, 0.03, 0.03], color2=[1.0, 1.0, 1.0]),
            node("emitter", "emission", strength=1.0),
            node("diffuse", "diffuse_bsdf", color=[0.2, 0.2, 0.2]),
            node("add", "add_closure"),
        ],
        [
            link("position", "position", "checker", "vector"),
            link("checker", "color", "emitter", "color"),
            link("emitter", "emission", "add", "closure1"),
            link("diffuse", "BSDF", "add", "closure2"),
        ],
        "add",
        "closure",
    )
    requests = []
    for obj, sop, key, priority in [
        (outer, sphere, "outer", 1),
        (inner, small, "inner", 2),
        (screen, plane, "screen", 0),
    ]:
        request = ch.geometry_request(obj, sop, material=key, smooth=key != "screen")
        request["object_properties"]["dielectric_priority"] = priority
        requests.append(request)
    images = {}
    with ch.Session(
        device=device,
        width=64,
        height=48,
        samples=a.samples,
        threads=4,
        interactive=True,
        execution="worker",
        denoising={"enabled": False},
        passes=["combined"],
    ) as session:
        fixture_state = {"caustics_light": False}

        def render(
            label,
            scale=1.0,
            abbe_outer=64.0,
            abbe_inner=20.0,
            inside=False,
            procedural=False,
            nested=True,
            mnee=False,
            ior_inner=1.6,
            glass=False,
            anisotropy=0.0,
            rotation=0.0,
            sample_count=None,
            no_roulette=False,
            fresh_session=False,
        ):
            sample_count = a.samples if sample_count is None else sample_count
            camera.parm("tz").set(1.6 if inside else 6.0)
            update = dict(
                geometry=requests,
                materials=[
                    dielectric("outer", 1.333, abbe_outer, scale),
                    (
                        graph(
                            "inner",
                            [
                                node(
                                    "surface",
                                    "glass_bsdf",
                                    color=[1, 1, 1],
                                    roughness=0.2,
                                    IOR=ior_inner,
                                    anisotropy=anisotropy,
                                    rotation=rotation,
                                )
                            ],
                            [],
                            "surface",
                        )
                        if glass
                        else dielectric("inner", ior_inner, abbe_inner, scale, procedural)
                    ),
                    emitter,
                ],
                camera_path=camera.path(),
                background=(0.03, 0.03, 0.03),
                transparent_background=False,
                rendering=dict(
                    samples=sample_count,
                    adaptive_sampling=False,
                    seed=173,
                    animate_seed=False,
                    time_limit=0,
                    nested_dielectrics=nested,
                    caustics_reflective=True,
                    caustics_refractive=True,
                ),
                denoising={"enabled": False},
            )
            if fixture_state.get("lens"):
                from cycles_houdini import camera_properties, image_iris

                update["camera_dof"] = image_iris.resolve(
                    camera_properties.evaluate(
                        lambda name, default: camera.parm(name).eval() if camera.parm(name) else default
                    )
                )
            if no_roulette:
                update["rendering"].update(min_bounce=16, max_bounce=16, glossy_bounce=16, transmission_bounce=16)
            # The existing object MNEE flags are the host-facing activation route.
            for index, request in enumerate(requests):
                request["object_properties"]["caustics_caster"] = mnee and index < 2
                request["object_properties"]["caustics_receiver"] = mnee and index == 2
            update["lights"] = (
                [
                    dict(
                        key="caustics",
                        kind="area",
                        size=(0.4, 0.4),
                        strength=(12, 12, 12),
                        use_caustics=True,
                        transform=(1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 3),
                    )
                ]
                if mnee
                else []
            )
            if fixture_state["caustics_light"] and not mnee:
                update["deleted_lights"] = ["caustics"]
            fixture_state["caustics_light"] = mnee
            if fresh_session:
                with ch.Session(
                    device=device,
                    width=64,
                    height=48,
                    samples=sample_count,
                    threads=4,
                    interactive=True,
                    execution="worker",
                    denoising={"enabled": False},
                    passes=["combined"],
                ) as control:
                    frame = control.wait(control.apply(update), timeout=240)
                    status = control.status()
                    frame["planes"] = {
                        key: dict(value, pixels=bytes(value["pixels"])) for key, value in frame["planes"].items()
                    }
            else:
                frame = session.wait(session.apply(update), timeout=240)
                status = session.status()
            assert not status["error"], status
            assert frame["complete"] and frame["samples"] == sample_count, (label, frame["samples"])
            assert status.get("nested_probe_unresolved", 0) == 0, (label, status)
            if fixture_state.get("lens"):
                assert status.get("image_iris_preparations", 0) > 0, (label, "Image Iris was not prepared", status)
            plane = frame["planes"]["combined"]
            pixels = np.frombuffer(plane["pixels"], np.float32).reshape(48, 64, plane["channels"]).copy()
            assert np.isfinite(pixels).all() and pixels[..., :3].max() > 1e-4, (label, "invalid image")
            np.save(a.output / (label + ".npy"), pixels)
            images[label] = pixels
            report["runs"].append(
                dict(
                    label=label,
                    samples=frame["samples"],
                    mean=float(pixels[..., :3].mean()),
                    maximum=float(pixels[..., :3].max()),
                    generation=status["generation"],
                    render_revision=status.get("render_revision"),
                    nested_probe_unresolved=status.get("nested_probe_unresolved", 0),
                )
            )
            save()
            return pixels

        zero = render("zero_scale", scale=0.0)
        repeat = render("zero_scale_fresh_session_control", scale=0.0, fresh_session=True)
        repeat_error = float(np.max(np.abs(repeat - zero)))
        assert np.array_equal(repeat, zero) if device == "CPU" else np.allclose(repeat, zero, atol=3e-6, rtol=3e-6), (
            "Unchanged fresh-session repeat exceeded numeric floor",
            repeat_error,
        )
        report["checks"].append(
            dict(name="independent same-seed unchanged fresh-session control", max_absolute_error=repeat_error)
        )
        zero_abbe = render("zero_abbe", scale=1.0, abbe_outer=0.0, abbe_inner=0.0)
        zero_error = float(np.max(np.abs(zero - zero_abbe)))
        assert (
            np.array_equal(zero, zero_abbe) if device == "CPU" else np.allclose(zero, zero_abbe, atol=3e-6, rtol=3e-6)
        ), ("Zero Abbe changed non-dispersive transport", zero_error)
        report["checks"].append(
            dict(
                name="zero-scale equals zero-Abbe feature-off control",
                max_absolute_error=zero_error,
                comparison="exact" if device == "CPU" else "GPU float accumulation tolerance3e-6",
            )
        )
        spectral = render("two_dispersive_media")
        assert np.max(np.abs(spectral - zero)) > 1e-4, "Fixture did not activate dispersion"
        render("nested_off", nested=False)
        render("camera_inside", inside=True)
        render("procedural_ior", procedural=True)
        render("procedural_camera_inside", inside=True, procedural=True)
        render("mnee_flags", mnee=True)
        # A material edit must replace the wavelength-dependent medium values, not retain a prior entry.
        edited = render("live_ior_edit", ior_inner=1.75)
        assert np.max(np.abs(edited - spectral)) > 1e-4, "Live IOR edit retained previous medium transport"
        restored = render("restored_two_media")
        restore_error = float(np.max(np.abs(restored - spectral)))
        assert (
            np.array_equal(restored, spectral)
            if device == "CPU"
            else np.allclose(restored, spectral, atol=3e-6, rtol=3e-6)
        ), ("Restoring material IOR retained stale transport", restore_error)
        report["checks"].append(
            dict(
                name="live material edit restarts, restores and clears previous nested entries",
                max_absolute_error=restore_error,
                comparison="exact" if device == "CPU" else "GPU float accumulation tolerance3e-6",
            )
        )
        isotropic = render("glass_isotropic", scale=0.0, nested=False, glass=True)
        anisotropic = render("glass_anisotropic", scale=0.0, nested=False, glass=True, anisotropy=0.7)
        rotated = render(
            "glass_anisotropic_rotated", scale=0.0, nested=False, glass=True, anisotropy=0.7, rotation=0.25
        )
        assert np.max(np.abs(anisotropic - isotropic)) > 1e-4, "Glass anisotropy input did not affect rendering"
        assert np.max(np.abs(rotated - anisotropic)) > 1e-4, "Glass rotation input did not affect rendering"
        report["checks"].append("native Glass BSDF anisotropy and tangent rotation affect rendering")
        # Uniform RGB iris transmission must factor through glass and nested spectral
        # transport. Keep identical sampled lens positions and disable roulette so an
        # aperture tint cannot alter finite-sample path survival.
        from cycles_houdini import camera_properties
        import OpenImageIO as oiio

        assert ch.capabilities()["image_iris"], "Candidate build lost required Image Iris support"
        camera_properties.install(camera)
        iris_path = a.output / "rgb-iris.exr"
        mask = np.empty((8, 8, 3), np.float32)
        mask[:] = (0.2, 0.5, 0.9)
        image_output = oiio.ImageOutput.create(str(iris_path))
        image_spec = oiio.ImageSpec(8, 8, 3, oiio.FLOAT)
        assert image_output.open(str(iris_path), image_spec) and image_output.write_image(mask) and image_output.close()
        camera.setParms(
            dict(
                cycles_dof=1,
                cycles_focus_distance=6.0,
                cycles_fstop=0.2,
                cycles_image_iris=1,
                cycles_image_iris_file=str(iris_path),
            )
        )
        fixture_state["lens"] = True
        tint = np.array((0.2, 0.5, 0.9), np.float32) / (0.2126 * 0.2 + 0.7152 * 0.5 + 0.0722 * 0.9)
        for method in ("cdf", "hierarchical"):
            camera.parm("cycles_image_iris_method").set(method)
            camera.parm("cycles_image_iris_color").set(0)
            neutral = render("iris_" + method + "_neutral_nested", no_roulette=True, sample_count=max(128, a.samples))
            camera.parm("cycles_image_iris_color").set(1)
            colored = render("iris_" + method + "_rgb_nested", no_roulette=True, sample_count=max(128, a.samples))
            residual = float(np.max(np.abs(colored[..., :3] - neutral[..., :3] * tint)))
            assert np.allclose(colored[..., :3], neutral[..., :3] * tint, rtol=3e-4, atol=3e-4), (
                method,
                "RGB iris did not factor through nested spectral transport",
                residual,
            )
            assert np.array_equal(colored[..., 3], neutral[..., 3]), "RGB iris changed glass coverage alpha"
            report["checks"].append(
                dict(
                    name="RGB iris with glass and nested dispersion",
                    method=method,
                    max_absolute_error=residual,
                    coverage_unchanged=True,
                )
            )
        camera.setParms(dict(cycles_dof=0, cycles_image_iris=0, cycles_image_iris_color=0))
        fixture_state["lens"] = False
        # Independent explicit-interface image oracle. For Cauchy materials, choosing
        # Abbe_inner = Abbe_outer * ((n_inner-1)*n_outer)/((n_outer-1)*n_inner)
        # makes their absolute IOR functions proportional at EVERY wavelength.
        # Their exact relative IOR is therefore the constant n_inner/n_outer.
        # Compare the two strongly dispersive real media against that known interface.
        # The reference retains spectral RGB integration using an effectively infinite
        # Abbe number, so both images use identical per-path wavelength weights.
        sphere.parmTuple("rad").set((100.0, 100.0, 100.0))
        requests[0]["visibility"] = 1  # Camera-inside probe only; no artificial distant exit.
        nd_outer, nd_inner, abbe_outer = 1.333, 1.6, 3.0
        abbe_inner = abbe_outer * ((nd_inner - 1) * nd_outer) / ((nd_outer - 1) * nd_inner)
        actual = render(
            "ratio_oracle_two_media",
            abbe_outer=abbe_outer,
            abbe_inner=abbe_inner,
            sample_count=max(a.samples, a.oracle_samples),
        )
        requests[0]["visibility"] = 0
        reference = render(
            "ratio_oracle_explicit_interface",
            ior_inner=nd_inner / nd_outer,
            abbe_inner=1e10,
            nested=False,
            sample_count=max(a.samples, a.oracle_samples),
        )
        error = actual[..., :3] - reference[..., :3]
        rmse = float(np.sqrt(np.mean(error * error)))
        peak = float(np.max(np.abs(error)))
        assert rmse < 0.002 and peak < 0.08, ("Two-medium image disagrees with exact explicit interface", rmse, peak)
        report["checks"].append(
            dict(
                name="independent proportional-Cauchy explicit-interface image oracle",
                outer_d=nd_outer,
                inner_d=nd_inner,
                outer_abbe=abbe_outer,
                inner_abbe=abbe_inner,
                exact_relative_ior=nd_inner / nd_outer,
                rgb_rmse=rmse,
                rgb_peak_error=peak,
            )
        )

    report.update(result="passed", device=device)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    print(report["error"], flush=True)
save()
print(json.dumps(dict(result=report["result"], runs=len(report["runs"]))))
raise SystemExit(0 if report["result"] == "passed" else 1)
