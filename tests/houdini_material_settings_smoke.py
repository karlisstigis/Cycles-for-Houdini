# SPDX-License-Identifier: Apache-2.0
"""Per-material Cycles settings (Blender: Material > Settings) on the Cycles Material builder.

    hython tests/houdini_material_settings_smoke.py [--device CPU] [--folder DIR] [--cases a,b]
    hython tests/houdini_material_settings_smoke.py --defaults-only --folder DIR   (older builds too)

Cases (small fixed-sample renders, black world, no denoising):
  emitters  a field of 4096 instanced emissive spheres lighting a diffuse floor, seen only through
            the floor: Emission Sampling None against Auto (floor mean, noise, render time, device
            memory), and None set live in the running session against a fresh session (identical);
  sides     a single-sided emitter (Backfacing mix) facing the floor: Auto, Front, Back, Front and Back;
  volume    a low-resolution VDB with a point light just outside it: each Volume Sampling mode at equal samples
            (means agree; only noise differs) and Interpolation Linear against Cubic;
  bump      a low-poly bump-mapped sphere at the shadow terminator: Bump Map Correction on/off;
  shadows   a Transparent BSDF card over a floor: Transparent Shadows on/off;
  defaults  one scene with every material kind at default settings: the material graphs carry no
            settings, and with --compare the image equals (max difference 0) the image the same
            scene gave in an older build (--defaults-only there writes defaults.npy).
  reload    both builders, at default and non-default settings, with a promoted parameter (an
            interface edit): save, reload without load warnings, identical values and graphs; with
            --old-scene, a scene saved by an older build (--defaults-only writes defaults.hiplc)
            loads without warnings and gets the default settings.
Writes report.json, .npy and .png images to --folder. Exits non-zero when a check fails.
"""

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

import hou
import numpy as np

from cycles_houdini import materials, native_scene, registry
from cycles_houdini.device_selection import set_devices

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
parser.add_argument("--folder", type=Path, default=None)
parser.add_argument("--cases", default="emitters,sides,volume,bump,shadows,defaults,reload")
parser.add_argument("--defaults-only", action="store_true", help="only render the default scene (any build)")
parser.add_argument("--compare", type=Path, default=None, help="defaults.npy rendered by an older build")
parser.add_argument("--old-scene", type=Path, default=None, help="defaults.hiplc saved by an older build")
args = parser.parse_args()
if args.folder is None:
    from support.paths import artifacts_root

    args.folder = artifacts_root() / "features/material-settings"
args.folder.mkdir(parents=True, exist_ok=True)
report = dict(result="running", device=args.device, houdini=hou.applicationVersionString(), checks=[], renders={})
OBJ, MAT = hou.node("/obj"), hou.node("/mat")


def check(name, ok, data=None):
    report["checks"].append(dict(check=name, ok=bool(ok), data=data))
    print(("PASS " if ok else "FAIL ") + name + ("" if data is None else ": " + json.dumps(data, default=str)[:600]))
    return ok


def clear():
    registry.close_all()
    hou.hipFile.clear(suppress_save_prompt=True)


def geo(name, material=None):
    node = OBJ.createNode("geo", name, run_init_scripts=False)
    if material is not None:
        node.parm("shop_materialpath").set(material.path())
    return node


def out(node):
    node.setDisplayFlag(True)
    node.setRenderFlag(True)
    return node


def camera(t, r, focal=35.0, size=(160, 120)):
    cam = OBJ.createNode("cam", "camera")
    cam.parmTuple("t").set(t)
    cam.parmTuple("r").set(r)
    cam.parm("focal").set(focal)
    cam.parm("aperture").set(36.0)
    cam.parm("resx").set(size[0])
    cam.parm("resy").set(size[1])
    return cam


def light(kind, t=(0, 0, 0), r=(0, 0, 0), intensity=1.0, exposure=0.0):
    node = OBJ.createNode("hlight::2.0", kind + "_light")
    node.setParms(dict(light_type=kind, light_intensity=intensity, light_exposure=exposure))
    node.parmTuple("t").set(t)
    node.parmTuple("r").set(r)
    return node


def rop(cam, samples):
    node = hou.node("/out").createNode("cycles_ipr", "settings_rop")
    set_devices(node, args.device)
    node.setParms(
        dict(
            source_mode="objects",
            objectroot="/obj",
            vobject="*",
            lights="*",
            camera=cam.path(),
            resx=cam.evalParm("resx"),
            resy=cam.evalParm("resy"),
            samples=samples,
            ipr_samples=samples,
            use_denoise=0,
            ipr_use_denoise=0,
            allowmotionblur=0,
            use_material_assignments=1,
        )
    )
    node.parmTuple("background").set((0, 0, 0))
    return node


class Session:
    """One renderer session; render() applies the current scene and waits for all samples."""

    def __init__(self, node):
        self.rop = node
        self.entry = registry.for_rop(node, native_scene.configuration(node))

    def render(self, label):
        started = time.perf_counter()
        frame = self.entry.session.wait(self.entry.apply(native_scene.capture(self.rop), hou.time()))
        seconds = time.perf_counter() - started
        status = self.entry.session.status()
        if status.get("error"):
            raise RuntimeError(label + ": " + str(status["error"]))
        plane = frame["planes"]["combined"]
        pixels = np.frombuffer(plane["pixels"], np.float32).reshape(frame["height"], frame["width"], -1)[..., :3]
        pixels = pixels[::-1].copy()  # top row first
        np.save(args.folder / (label + ".npy"), pixels)
        save_png(args.folder / (label + ".png"), pixels)
        info = dict(
            seconds=round(seconds, 3),
            elapsed_seconds=status.get("elapsed_seconds"),
            device_allocation_peak_bytes=status.get("device_allocation_peak_bytes"),
            material_resources=status.get("material_resources"),
            packed_instances=status.get("packed_instances"),
            mean=float(pixels.mean()),
            noise=noise(pixels),
            diagnostics=[d.get("code") for d in status.get("source_diagnostics") or []],
        )
        report["renders"][label] = info
        print("RENDER " + label + " " + json.dumps(info))
        return pixels, info


def noise(pixels):
    """Mean absolute deviation from a 5x5 box blur of the luminance: grain, not lighting."""
    lum = pixels.mean(axis=2)
    padded = np.pad(lum, 2, mode="edge")
    blur = sum(padded[dy : dy + lum.shape[0], dx : dx + lum.shape[1]] for dy in range(5) for dx in range(5)) / 25
    return float(np.abs(lum - blur)[2:-2, 2:-2].mean())


def save_png(path, pixels):
    import OpenImageIO as oiio

    image = (np.clip(pixels, 0, 1) ** (1 / 2.2) * 255 + 0.5).astype(np.uint8)
    output = oiio.ImageOutput.create(str(path))
    output.open(str(path), oiio.ImageSpec(image.shape[1], image.shape[0], 3, oiio.UINT8))
    output.write_image(image)
    output.close()


def emissive(name, strength, color=(1.0, 0.8, 0.6)):
    material = materials.create_material(MAT, name, "emission")
    material.node("surface_shader").parmTuple("color").set(color)
    material.node("surface_shader").parm("strength").set(strength)
    return material


def floor(size=12.0, material=None):
    material = material or materials.create_material(MAT, "floor", "diffuse_bsdf")
    node = geo("floor", material)
    grid = node.createNode("grid")
    grid.parmTuple("size").set((size, size))
    out(grid)
    return material


def emitter_field(material, count=64, height=3.0, radius=0.05):
    node = geo("emitters", material)
    points = node.createNode("grid", "points")
    points.setParms(dict(rows=count, cols=count))
    points.parmTuple("size").set((12, 12))
    points.parmTuple("t").set((0, height, 0))
    sphere = node.createNode("sphere")
    sphere.setParms(dict(type="poly", freq=2))
    sphere.parmTuple("rad").set((radius, radius, radius))
    copy = node.createNode("copytopoints::2.0")
    copy.setInput(0, sphere)
    copy.setInput(1, points)
    copy.parm("pack").set(1)
    out(copy)
    return node


def case_emitters():
    clear()
    floor()
    field = emissive("field", 4.0)
    emitter_field(field)
    # The camera sits between the floor and the emitters looking down: every pixel is floor.
    session = Session(rop(camera((0, 1.5, 0), (-90, 0, 0), size=(320, 240)), 64))
    auto, auto_info = session.render("emitters_auto")
    field.parm("cycles_emission_sampling").set("none")
    live, live_info = session.render("emitters_none_live")
    clear()
    floor()
    field = emissive("field", 4.0)
    emitter_field(field)
    field.parm("cycles_emission_sampling").set("none")
    graph = materials.material_graph(field)
    check(
        "None reaches the material graph",
        graph.get("settings") == dict(emission_sampling="none"),
        graph.get("settings"),
    )
    fresh, fresh_info = Session(rop(camera((0, 1.5, 0), (-90, 0, 0), size=(320, 240)), 64)).render("emitters_none")
    check("4096 packed emitter instances", auto_info["packed_instances"] == 4096, auto_info["packed_instances"])
    # CPU renders are bit-exact across sessions; GPU kernels may differ in the last float bit.
    tolerance = 0.0 if args.device == "CPU" else 1e-5
    check(
        f"None set live equals None in a fresh session (max difference <= {tolerance:g})",
        float(np.abs(live - fresh).max()) <= tolerance,
        float(np.abs(live - fresh).max()),
    )
    ratio = fresh_info["mean"] / auto_info["mean"]
    check("None changes only the noise: floor mean within 15% of Auto", abs(ratio - 1) < 0.15, ratio)
    check(
        "None is noisier than Auto (no direct light sampling)",
        fresh_info["noise"] > auto_info["noise"],
        [fresh_info["noise"], auto_info["noise"]],
    )
    report["emitters"] = dict(
        auto_seconds=auto_info["seconds"],
        none_seconds=fresh_info["seconds"],
        auto_peak_bytes=auto_info["device_allocation_peak_bytes"],
        none_peak_bytes=fresh_info["device_allocation_peak_bytes"],
        floor_mean=dict(auto=auto_info["mean"], none=fresh_info["mean"]),
        noise=dict(auto=auto_info["noise"], none=fresh_info["noise"]),
    )


def case_sides():
    clear()
    floor()
    card = materials.create_material(MAT, "one_sided", "emission")
    card.node("surface_shader").parm("strength").set(8.0)
    black = card.createNode("cycles::diffuse_bsdf::1", "back_side")
    black.parmTuple("color").set((0, 0, 0))
    facing = card.createNode("cycles::geometry::1", "facing")
    mix = card.createNode("cycles::mix_closure::1", "front_only")
    mix.setInput(mix.inputIndex("fac"), facing, facing.outputIndex("backfacing"))
    mix.setInput(mix.inputIndex("closure1"), card.node("surface_shader"))
    mix.setInput(mix.inputIndex("closure2"), black)
    card.node("surface_output").setInput(0, mix)
    node = geo("card", card)
    grid = node.createNode("grid")
    grid.parmTuple("size").set((2, 2))
    grid.parmTuple("t").set((0, 2.5, 0))
    grid.parmTuple("r").set((180, 0, 0))  # front (emitting) side faces the floor
    out(grid)
    session = Session(rop(camera((0, 1.2, 0), (-90, 0, 0)), 64))
    results = {}
    for mode in ("auto", "front", "back", "front_back"):
        card.parm("cycles_emission_sampling").set(mode)
        results[mode] = session.render("sides_" + mode)[1]
    check(
        "Front: floor mean within 10% of Auto",
        abs(results["front"]["mean"] / results["auto"]["mean"] - 1) < 0.1,
        [results["front"]["mean"], results["auto"]["mean"]],
    )
    check(
        "Back (the dark side) samples nothing directly: noisier than Front",
        results["back"]["noise"] > 1.5 * results["front"]["noise"],
        [results["back"]["noise"], results["front"]["noise"]],
    )
    check(
        "Back keeps the mean within 25% (lit only by bounces of camera paths)",
        abs(results["back"]["mean"] / results["front"]["mean"] - 1) < 0.25,
        [results["back"]["mean"], results["front"]["mean"]],
    )


def volume_scene():
    medium = materials.create_material(MAT, "medium", "principled_volume")
    medium.node("volume_shader").parm("density").set(1.5)
    node = geo("vdb", medium)
    sphere = node.createNode("sphere")
    sphere.setParms(dict(type="poly", freq=6))
    vdb = node.createNode("vdbfrompolygons")
    vdb.setInput(0, sphere)
    vdb.setParms(dict(voxelsize=0.25, builddistance=0, buildfog=1, fillinterior=1))  # 8 voxels across
    wrangle = node.createNode("volumewrangle")
    wrangle.setInput(0, vdb)
    wrangle.parm("snippet").set("@density = clamp(1.0 - length(@P), 0, 1) * (0.6 + 0.4 * sin(9 * @P.x));")
    name = node.createNode("attribwrangle", "name")
    name.setInput(0, wrangle)
    name.parm("class").set(1)
    name.parm("snippet").set('s@name = "density";')
    out(name)
    light("point", t=(1.3, 0.3, 0.4), intensity=3.0, exposure=7.0)  # just outside: finite variance for Distance
    return medium


def case_volume():
    clear()
    medium = volume_scene()
    session = Session(rop(camera((0, 0, 4), (0, 0, 0), size=(128, 96)), 512))
    results = {}
    for mode in ("multiple_importance", "distance", "equiangular"):
        medium.parm("cycles_volume_sampling").set(mode)
        results[mode] = session.render("volume_" + mode)[1]
    reference = results["multiple_importance"]["mean"]
    for mode in ("distance", "equiangular"):
        check(
            f"Volume Sampling {mode}: mean within 5% of Multiple Importance",
            abs(results[mode]["mean"] / reference - 1) < 0.05,
            [results[mode]["mean"], reference],
        )
    medium.parm("cycles_volume_sampling").set("multiple_importance")
    linear = np.load(args.folder / "volume_multiple_importance.npy")
    medium.parm("cycles_volume_interpolation").set("cubic")
    cubic, cubic_info = session.render("volume_cubic")
    difference = float(np.abs(cubic - linear).mean())
    check("Cubic interpolation changes the low-resolution VDB image", difference > 1e-3, difference)
    report["volume"] = dict(results=results, cubic=cubic_info, linear_vs_cubic_mean_abs=difference)


def case_bump():
    clear()
    material = materials.create_material(MAT, "bumpy", "principled_bsdf")
    shader = material.node("surface_shader")
    noise_node = material.createNode("cycles::noise_texture::1", "noise")
    noise_node.parm("scale").set(12.0)
    bump = material.createNode("cycles::bump::1", "bump")
    bump.parm("strength").set(0.6)
    bump.setInput(bump.inputIndex("height"), noise_node, noise_node.outputIndex("fac"))
    shader.setInput(shader.inputIndex("normal"), bump, bump.outputIndex("normal"))
    node = geo("sphere", material)
    sphere = node.createNode("sphere")
    sphere.setParms(dict(type="poly", freq=2))
    normal = node.createNode("normal")
    normal.setInput(0, sphere)
    out(normal)
    light("distant", r=(0, 90, 0), intensity=8.0)  # from +X: the terminator runs down the middle
    session = Session(rop(camera((0, 0, 4), (0, 0, 0), focal=50.0, size=(128, 128)), 64))
    on, _ = session.render("bump_correction_on")
    material.parm("cycles_bump_map_correction").set(0)
    off, _ = session.render("bump_correction_off")
    difference = np.abs(on - off).mean(axis=2)
    columns = difference.mean(axis=0)
    check("Bump Map Correction changes shading", float(difference.mean()) > 1e-4, float(difference.mean()))
    report["bump"] = dict(mean_abs=float(difference.mean()), column_profile=[round(float(v), 5) for v in columns[::8]])


def case_shadows():
    clear()
    floor()
    card = materials.create_material(MAT, "card", "transparent_bsdf")
    node = geo("card", card)
    grid = node.createNode("grid")
    grid.parmTuple("size").set((2, 2))
    grid.parmTuple("t").set((0, 1, 0))
    out(grid)
    light("distant", r=(-90, 0, 0), intensity=2.0)
    session = Session(rop(camera((0, 6, 0), (-90, 0, 0), size=(96, 96)), 16))
    on, _ = session.render("shadows_on")
    card.parm("cycles_transparent_shadows").set(0)
    off, _ = session.render("shadows_off")
    centre = (slice(40, 56), slice(40, 56))
    values = [float(on[centre].mean()), float(off[centre].mean())]
    check("Transparent Shadows on: the floor under the card is lit", values[0] > 0.2, values)
    check("Transparent Shadows off: the card casts an opaque shadow", values[1] < 0.05 * values[0], values)


def defaults_scene():
    """Every material kind at default settings, built only with interfaces older builds have."""
    clear()
    floor()
    emitter_field(emissive("field", 20.0), count=16)
    card = materials.create_material(MAT, "card", "transparent_bsdf")
    node = geo("card", card)
    grid = node.createNode("grid")
    grid.parmTuple("size").set((1.5, 1.5))
    grid.parmTuple("t").set((-1.5, 0.8, 0))
    out(grid)
    volume_scene()
    OBJ.node("vdb").parmTuple("t").set((1.2, 1.0, 0))
    light("distant", r=(-60, 30, 0), intensity=1.0)
    builders = [MAT.node(name) for name in ("floor", "field", "card", "medium")]
    return builders, rop(camera((0, 4, 7), (-30, 0, 0)), 16)


def case_defaults():
    builders, node = defaults_scene()
    graphs = {b.name(): sorted(materials.material_graph(b)) for b in builders}
    pixels, _ = Session(node).render("defaults")
    report["defaults"] = dict(
        host=hou.getenv("CYCLES_HOUDINI_HOST_ROOT"),
        settings_parameters=builders[0].parm("cycles_emission_sampling") is not None,
        graph_keys=graphs,
    )
    print("DEFAULTS " + json.dumps(report["defaults"]))
    if args.defaults_only:
        # The scene for the reload check of newer builds: builders with a promoted parameter,
        # which rewrites their parameter interface (spare parameters are saved with the scene).
        for builder in builders:
            shader = builder.node("volume_shader" if builder.name() == "medium" else "surface_shader")
            materials.promote_parameter(builder, shader, "density" if builder.name() == "medium" else "color")
        hou.hipFile.save(str(args.folder / "defaults.hiplc"), save_to_recent_files=False)
        return
    check(
        "Default materials send no settings (graph unchanged)",
        all("settings" not in keys for keys in graphs.values()),
        graphs,
    )
    names = [p.name() for p in builders[0].parms() if p.name().startswith("cycles_")]
    check("Builder has the settings parameters", "cycles_emission_sampling" in names, names)
    if args.compare:
        before = np.load(args.compare)
        check(
            "Defaults: image identical to the older build (max difference 0)",
            before.shape == pixels.shape and float(np.abs(before - pixels).max()) == 0.0,
            float(np.abs(before - pixels).max()) if before.shape == pixels.shape else [before.shape, pixels.shape],
        )


def load(path):
    """Load a scene; return Houdini's load warning text, or "" when it loaded cleanly."""
    try:
        hou.hipFile.load(str(path), suppress_save_prompt=True)
    except hou.LoadWarning as warning:
        return str(warning)
    return ""


def settings_values(node):
    return {name: node.evalParm(name) for name, _, _ in materials.MATERIAL_SETTINGS if node.parm(name)}


def case_reload():
    """Save and reload both builders, at default and non-default settings, after an interface edit."""
    clear()
    surface = materials.create_material(MAT, "surface", "principled_bsdf")
    changed = materials.create_material(MAT, "changed", "principled_bsdf")
    medium = materials.create_material(MAT, "medium", "principled_volume")
    changed_medium = materials.create_material(MAT, "changed_medium", "principled_volume")
    changed.setParms(
        dict(
            cycles_emission_sampling="none",
            cycles_transparent_shadows=0,
            cycles_bump_map_correction=0,
            cycles_volume_sampling="equiangular",
            cycles_volume_interpolation="cubic",
            cycles_volume_step_rate=0.25,
        )
    )
    changed_medium.setParms(
        dict(cycles_volume_sampling="distance", cycles_volume_interpolation="cubic", cycles_volume_step_rate=2.0)
    )
    builders = (surface, changed, medium, changed_medium)
    # Promotion rewrites the builder's parameter interface (setParmTemplateGroup), like Edit
    # Parameter Interface; it is what used to add duplicate folders that failed to load.
    for builder in builders:
        volume = builder.type().name() == materials.VOLUME_BUILDER
        shader = builder.node("volume_shader" if volume else "surface_shader")
        materials.promote_parameter(builder, shader, "density" if volume else "base_color")
    before = {b.name(): (settings_values(b), materials.material_graph(b).get("settings")) for b in builders}
    spare = {b.name(): sorted(p.name() for p in b.spareParms()) for b in builders}
    check(
        "Interface edit adds only the promoted parameter",
        all(not any(name.startswith("cycles_") for name in names) for names in spare.values()),
        spare,
    )
    path = args.folder / "reload.hiplc"
    hou.hipFile.save(str(path), save_to_recent_files=False)
    warning = load(path)
    check("Saved scene reloads without warnings", not warning, warning)
    after = {
        name: (settings_values(MAT.node(name)), materials.material_graph(MAT.node(name)).get("settings"))
        for name in before
    }
    check("Settings and graphs identical after reload", after == before, dict(before=before, after=after))
    if args.old_scene:
        warning = load(args.old_scene)
        check("Scene saved by the older build loads without warnings", not warning, warning)
        nodes = [n for n in MAT.children() if n.type().name() in (materials.BUILDER, materials.VOLUME_BUILDER)]
        values = {n.name(): settings_values(n) for n in nodes}
        defaults = {name: default for name, _, default in materials.MATERIAL_SETTINGS}
        check(
            "Older scene: every builder has the settings at their defaults",
            bool(nodes) and all(v and all(v[k] == defaults[k] for k in v) for v in values.values()),
            values,
        )
        check(
            "Older scene: graphs carry no settings",
            all("settings" not in materials.material_graph(n) for n in nodes),
            [n.name() for n in nodes],
        )


CASES = dict(
    emitters=case_emitters,
    sides=case_sides,
    volume=case_volume,
    bump=case_bump,
    shadows=case_shadows,
    defaults=case_defaults,
    reload=case_reload,
)
code = 1
try:
    for name in ["defaults"] if args.defaults_only else args.cases.split(","):
        print("CASE " + name)
        CASES[name]()
    failed = [c for c in report["checks"] if not c["ok"]]
    report["result"] = "failed" if failed else "passed"
    code = 1 if failed else 0
except BaseException:
    report.update(result="error", error=traceback.format_exc())
    traceback.print_exc()
finally:
    registry.close_all()
    (args.folder / "report.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
print("RESULT " + report["result"])
sys.stdout.flush()
hou.exit(code, suppress_save_prompt=True)
