# SPDX-License-Identifier: Apache-2.0
"""Viewer background composites in scene-linear light, against an independent OCIO reference.

Also RGB, R, G and B with the alpha toggle off: the image over black, opaque,
(View(2^exposure * RGB_premult))^(1 / gamma), the channel views taking one channel of it.

displayed = (View(2^exposure * RGB_premult + (1 - alpha) * B_linear))^(1 / gamma), where
View(B_linear)^(1 / gamma) is the chosen background byte triple. Checks the CPU route
(display_image) and, where an offscreen OpenGL context exists, the GPU route (gpu_display).
No window is shown: the GPU route uses a hidden offscreen surface.
"""

from support.paths import artifacts_root
import json
import time

import numpy as np
import PyOpenColorIO as OCIO
import cycles_houdini as ch
from cycles_houdini import _native

report = dict(result="running", cases=[], gpu=None)
failures = []


def check(name, condition, detail=None):
    report["cases"].append(dict(name=name, ok=bool(condition), detail=detail))
    if not condition:
        failures.append(name)


# Associated (premultiplied) working-space RGBA. Row 0 is the bottom row, as rendered.
named = {
    "brief (0.45, 0.13, 0.13, a 0.13)": (0.45, 0.13, 0.13, 0.13),
    "opaque": (0.3, 0.5, 0.7, 1.0),
    "opaque bright": (4.0, 2.0, 1.0, 1.0),
    "alpha 0, zero RGB": (0.0, 0.0, 0.0, 0.0),
    "alpha 0, additive RGB": (0.2, 0.1, 0.05, 0.0),
    "half coverage": (0.25, 0.1, 0.4, 0.5),
    "low coverage": (0.01, 0.02, 0.005, 0.02),
}
rng = np.random.default_rng(7)
width, height = 16, 8
pixels = np.empty((height, width, 4), np.float32)
alpha = rng.choice(np.asarray([0.0, 0.05, 0.13, 0.5, 0.9, 1.0], np.float32), (height, width))
pixels[..., 3] = alpha
pixels[..., :3] = rng.uniform(0, 1.5, (height, width, 3)).astype(np.float32) * np.maximum(alpha, 0.1)[..., None]
names = list(named)
for index, name in enumerate(names):
    pixels[0, index] = named[name]
frame = _native.display_frame_fixture(np.ascontiguousarray(pixels), width, height)

policy = ch.color_policy()
config = OCIO.GetCurrentConfig()
working = policy["working_space"]
display = policy["default_display"]
views = [policy["default_views"][display]]
for view in policy["views"][display]:
    if view not in views and ("ACES" in view or view.lower() in ("raw", "un-tone-mapped")):
        views.append(view)
report.update(config=config.getName(), working=working, display=display, views=views)
backgrounds = {
    "black": [(0, 0, 0)],
    "solid orange": [(200, 120, 40)],
    "checker": [(48, 48, 48), (64, 64, 64)],
}


def reference(view, exposure, gamma, background):
    forward = config.getProcessor(working, display, view, OCIO.TRANSFORM_DIR_FORWARD).getDefaultCPUProcessor()
    inverse = config.getProcessor(working, display, view, OCIO.TRANSFORM_DIR_INVERSE).getDefaultCPUProcessor()
    b = np.power(np.asarray(background, np.float32) / 255.0, gamma).astype(np.float32).reshape(1, 3).copy()
    inverse.applyRGB(b)
    rgb = pixels[..., :3] * np.float32(2.0**exposure) + (1 - pixels[..., 3:4]) * b.reshape(1, 1, 3)
    flat = np.ascontiguousarray(rgb.reshape(-1, 3), np.float32).copy()
    forward.applyRGB(flat)
    values = flat.reshape(height, width, 3)
    if gamma != 1:
        values = np.power(np.maximum(values, 0), 1 / gamma)
    values = np.nan_to_num(values, nan=0.0, posinf=0.0, neginf=0.0)
    return np.rint(np.clip(values, 0, 1) * 255).astype(np.uint8)[::-1]  # Top-down display rows.


def layers(result):
    straight = np.frombuffer(result["rgba"], np.uint8).reshape(height, width, 4)
    composites = [np.frombuffer(c, np.uint8).reshape(height, width, 4) for c in result.get("composites", [])]
    return straight, composites


top = height - 1  # Display row of the named pixels.
cpu_results = {}
for view in views:
    for exposure, gamma in ((0.0, 1.0), (1.5, 1.0), (0.0, 2.2)):
        for label, colors in backgrounds.items():
            settings = {
                "pass": "combined",
                "display": display,
                "view": view,
                "exposure": exposure,
                "gamma": gamma,
                "channels": "rgba",
                "backgrounds": colors,
            }
            straight, composites = layers(_native.display_image(frame, settings))
            old, _ = layers(_native.display_image(frame, dict(settings, backgrounds=[])))
            key = f"{view} | exposure {exposure} | gamma {gamma} | {label}"
            cpu_results[key] = (settings, composites)
            check(key + ": one composite per background", len(composites) == len(colors), len(composites))
            check(key + ": straight image unchanged", np.array_equal(straight, old))
            for color, composite in zip(colors, composites):
                expected = reference(view, exposure, gamma, color)
                difference = int(np.abs(composite[..., :3].astype(int) - expected.astype(int)).max())
                check(f"{key} {color}: CPU equals OCIO reference within 1/255", difference <= 1, difference)
                check(f"{key} {color}: composite opaque", bool((composite[..., 3] == 255).all()))
                row = composite[top]
                empty = row[names.index("alpha 0, zero RGB"), :3].tolist()
                check(f"{key} {color}: alpha 0 shows the background exactly", empty == list(color), empty)
                opaque = pixels[::-1][..., 3] == 1
                diff_opaque = int(np.abs(composite[opaque][:, :3].astype(int) - old[opaque][:, :3].astype(int)).max())
                check(f"{key} {color}: alpha 1 equals today's bytes", diff_opaque == 0, diff_opaque)
                additive = row[names.index("alpha 0, additive RGB"), :3]
                check(
                    f"{key} {color}: additive RGB at alpha 0 adds to the background",
                    bool((additive.astype(int) >= np.asarray(color) - 1).all()) and not np.array_equal(additive, color),
                    additive.tolist(),
                )
                if view == views[0] and exposure == 0 and gamma == 1:
                    report.setdefault("named_pixels", {})[f"{view} {color}"] = {
                        name: dict(new=row[i, :3].tolist(), old_straight_rgba=old[top, i].tolist())
                        for i, name in enumerate(names)
                    }


# RGB without alpha: the image over black (Nuke's RGB view), on the CPU route and, below, both converter routes.
def over_black(view, exposure, gamma):
    forward = config.getProcessor(working, display, view, OCIO.TRANSFORM_DIR_FORWARD).getDefaultCPUProcessor()
    flat = np.ascontiguousarray((pixels[..., :3] * np.float32(2.0**exposure)).reshape(-1, 3), np.float32).copy()
    forward.applyRGB(flat)
    values = flat.reshape(height, width, 3)
    if gamma != 1:
        values = np.power(np.maximum(values, 0), 1 / gamma)
    values = np.nan_to_num(values, nan=0.0, posinf=0.0, neginf=0.0)
    return np.rint(np.clip(values, 0, 1) * 255).astype(np.uint8)[::-1]


rgb_cases = {}
for view in views:
    for exposure, gamma in ((0.0, 1.0), (1.5, 1.0), (0.0, 2.2)):
        settings = {"pass": "combined", "display": display, "view": view, "exposure": exposure, "gamma": gamma}
        key = f"RGB over black | {view} | exposure {exposure} | gamma {gamma}"
        rgb, _ = layers(_native.display_image(frame, dict(settings, channels="rgb")))
        straight, _ = layers(_native.display_image(frame, dict(settings, channels="rgba")))
        expected = over_black(view, exposure, gamma)
        rgb_cases[key] = (settings, expected)
        difference = int(np.abs(rgb[..., :3].astype(int) - expected.astype(int)).max())
        check(key + ": CPU equals OCIO reference within 1/255", difference <= 1, difference)
        check(key + ": opaque", bool((rgb[..., 3] == 255).all()))
        source = pixels[::-1]
        for label, mask in (("alpha 1", source[..., 3] == 1), ("alpha 0", source[..., 3] == 0)):
            delta = int(np.abs(rgb[mask][:, :3].astype(int) - straight[mask][:, :3].astype(int)).max())
            check(f"{key}: {label} equals today's bytes (opaque, or View(RGB) for additive)", delta == 0, delta)
        if view == views[0] and exposure == 0 and gamma == 1:
            report["rgb_named_pixels"] = {name: rgb[top, i, :3].tolist() for i, name in enumerate(names)}

# Views and settings that keep their meaning: no composites.
base = {"pass": "combined", "display": display, "view": views[0], "backgrounds": [(48, 48, 48), (64, 64, 64)]}
for label, extra in {
    "RGB (alpha ignored)": dict(channels="rgb"),
    "Alpha": dict(channels="alpha"),
    "raw": dict(channels="rgba", raw=True),
}.items():
    result = _native.display_image(frame, dict(base, **extra))
    check(f"{label}: no composites", "composites" not in result)
    plain = _native.display_image(frame, {k: v for k, v in dict(base, **extra).items() if k != "backgrounds"})
    check(f"{label}: bytes unchanged", result["rgba"] == plain["rgba"])

# GPU route through the viewer's converter on a hidden offscreen surface.
try:
    from PySide6 import QtGui

    app = QtGui.QGuiApplication.instance() or QtGui.QGuiApplication([])
    from cycles_houdini.viewer import gpu_display

    converter = gpu_display.Converter()
    gpu_max = 0
    try:
        for key, (settings, cpu) in cpu_results.items():
            result = converter(frame, dict(settings, collect_diagnostics=True))
            backend = result.get("diagnostics", {}).get("backend")
            if backend != "opengl":
                raise RuntimeError("GPU route unavailable: " + str(result.get("diagnostics")))
            straight, composites = layers(result)
            check(key + ": GPU one composite per background", len(composites) == len(cpu), len(composites))
            for color, a, b in zip(settings["backgrounds"], composites, cpu):
                difference = int(np.abs(a.astype(int) - b.astype(int)).max())
                gpu_max = max(gpu_max, difference)
                check(f"{key} {color}: GPU equals CPU within 1/255", difference <= 1, difference)
                empty = a[top, names.index("alpha 0, zero RGB"), :3].tolist()
                check(
                    f"{key} {color}: GPU alpha 0 shows the background within 1/255",
                    max(abs(x - y) for x, y in zip(empty, color)) <= 1,
                    empty,
                )
            plain = converter(frame, dict(settings, backgrounds=[]))
            check(key + ": GPU straight image unchanged", plain["rgba"] == result["rgba"] and "composites" not in plain)
        for label, extra in {"R with alpha": dict(channels="r", use_alpha=True), "RGB": dict(channels="rgb")}.items():
            result = converter(frame, dict(base, **extra))
            check(f"GPU {label}: no composites", "composites" not in result)
        # RGB, R, G, B without alpha through the GPU route and through the converter's CPU fallback.
        fallback = gpu_display.Converter()
        fallback.failure = "forced CPU route for the test"
        try:
            for route, convert in (("GPU", converter), ("CPU fallback", fallback)):
                for key, (settings, expected) in rgb_cases.items():
                    for channels, component in (("rgb", None), ("r", 0), ("g", 1), ("b", 2)):
                        result = convert(
                            frame, dict(settings, channels=channels, use_alpha=False, collect_diagnostics=True)
                        )
                        backend = result.get("diagnostics", {}).get("backend")
                        shown = np.frombuffer(result["rgba"], np.uint8).reshape(height, width, 4)
                        want = (
                            expected if component is None else np.repeat(expected[..., component : component + 1], 3, 2)
                        )
                        difference = int(np.abs(shown[..., :3].astype(int) - want.astype(int)).max())
                        check(
                            f"{route} {channels} | {key}: equals the over-black reference within 1/255 ({backend})",
                            difference <= 1 and (backend == "opengl") == (route == "GPU"),
                            difference,
                        )
                    r_alpha = convert(frame, dict(settings, channels="r", use_alpha=True))
                    straight = convert(frame, dict(settings, channels="rgba"))
                    a = np.frombuffer(r_alpha["rgba"], np.uint8).reshape(-1, 4)
                    b = np.frombuffer(straight["rgba"], np.uint8).reshape(-1, 4)
                    check(
                        f"{route} R with alpha | {key}: unchanged (red of the straight image, its alpha)",
                        np.array_equal(a[:, 0], b[:, 0]) and np.array_equal(a[:, 3], b[:, 3]),
                    )
        finally:
            fallback.close()
        report["gpu"] = dict(available=True, max_difference_to_cpu=gpu_max, renderer=converter.context.renderer)
    finally:
        converter.close()
        app.processEvents()
except Exception as error:
    report["gpu"] = dict(available=False, reason=repr(error))

report["result"] = "failed" if failures else "passed"
report["failures"] = failures
destination = artifacts_root() / "viewer/linear-background"
destination.mkdir(parents=True, exist_ok=True)
(destination / "houdini-viewer-linear-background-smoke.json").write_text(json.dumps(report, indent=1))
print(
    json.dumps(
        dict(
            result=report["result"],
            failures=[c for c in report["cases"] if not c["ok"]][:20],
            gpu=report["gpu"],
            cases=len(report["cases"]),
            views=views,
            working=working,
            named_pixels=report.get("named_pixels"),
        )
    )
)
assert not failures, failures[:20]
