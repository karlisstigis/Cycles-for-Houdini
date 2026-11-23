# SPDX-License-Identifier: Apache-2.0
"""Output Picture extension selects the format; render information in EXR headers.

One small scene, rendered as a 3-frame range to .exr, .png, .jpg/.jpeg and .tif/.tiff in a
temp folder: file format, size, bit depth and alpha; PNG against the viewer's Save of the same
frame; TIFF against the EXR beauty; header entries, their values and their privacy; passes with
a non-EXR picture; Skip/Error policies; the farm entry point (render_hip.py).

  --write-baseline DIR  render only the EXR frames into DIR (run with an older build)
  --baseline DIR        also require EXR pixels and layout identical to that baseline
  --samples DIR         copy sample files and a header dump into DIR
"""

import argparse
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import hou
import numpy as np
import OpenImageIO as oiio
from support.paths import artifacts_root

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
parser.add_argument("--write-baseline", type=Path)
parser.add_argument("--baseline", type=Path)
parser.add_argument("--samples", type=Path)
args = parser.parse_args()
OUT = artifacts_root() / "stage3/output-formats"
OUT.mkdir(parents=True, exist_ok=True)
BASELINE = (args.write_baseline or args.baseline).absolute() if (args.write_baseline or args.baseline) else None
SAMPLES = args.samples.absolute() if args.samples else None
baseline_mode = args.write_baseline is not None
TMP = Path(tempfile.mkdtemp(prefix="cycles_output_formats_"))
checks = {}
failures = []


def check(name, condition, detail=""):
    checks[name] = dict(ok=bool(condition), detail=str(detail)[:2000])
    if not condition:
        failures.append(name)


def build_scene():
    from cycles_houdini.device_selection import set_devices

    geo = hou.node("/obj").createNode("geo", "subject", run_init_scripts=False)
    sphere = geo.createNode("sphere")
    sphere.parm("type").set("polymesh")
    sphere.setRenderFlag(True)
    cam = hou.node("/obj").createNode("cam", "shot_cam")
    cam.setParms(dict(tz=4, focal=35, aperture=36, near=0.05, far=500, resx=64, resy=48))
    rop = hou.node("/out").createNode("cycles_ipr", "formats")
    set_devices(rop, args.device)
    rop.setParms(
        dict(
            source_mode="sop",
            soppath=sphere.path(),
            camera=cam.path(),
            resx=64,
            resy=48,
            samples=8,
            adaptive_sampling=0,
            use_denoise=0,
            render_threads=2,
            use_material_assignments=0,
            material_kind="emission",
            emission_strength=1,
            transparent_background=1,
            allowmotionblur=0,
            trange=1,
        )
    )
    rop.parmTuple("basecolor").set((0.9, 0.4, 0.1))
    for parm in rop.parmTuple("f"):
        parm.deleteAllKeyframes()
    rop.parmTuple("f").set((1, 3, 1))
    hou.hipFile.save(str(TMP / "x3_formats.hip"), save_to_recent_files=False)
    return rop, cam


def render(rop, picture, frames=(1, 3, 1)):
    rop.parm("picture").set(picture)
    rop.render(frame_range=frames)


def pixels(path, subimage=0):
    # Read the stored values: OIIO otherwise premultiplies straight-alpha PNG on read.
    config = oiio.ImageSpec()
    config.attribute("oiio:UnassociatedAlpha", 1)
    image = oiio.ImageInput.open(str(path), config)
    assert image is not None, oiio.geterror()
    image.seek_subimage(subimage, 0)
    spec = image.spec()
    data = image.read_image(oiio.FLOAT if spec.format != oiio.UINT8 else oiio.UINT8)
    result = dict(
        format=image.format_name(),
        width=spec.width,
        height=spec.height,
        channels=list(spec.channelnames),
        depth=str(spec.format),
        alpha=spec.alpha_channel,
        attributes={a.name: a.value for a in spec.extra_attribs},
        data=np.array(data),
    )
    image.close()
    return result


def exr_header(path):
    """Raw attribute names, types and values of a single-part EXR (no OIIO renaming)."""
    data = Path(path).read_bytes()
    assert data[:4] == b"\x76\x2f\x31\x01", "not an EXR"
    pos, attrs = 8, {}
    while True:
        end = data.index(b"\0", pos)
        name = data[pos:end].decode()
        pos = end + 1
        if not name:
            return attrs
        end = data.index(b"\0", pos)
        kind = data[pos:end].decode()
        pos = end + 1
        size = struct.unpack("<i", data[pos : pos + 4])[0]
        pos += 4
        raw = data[pos : pos + size]
        pos += size
        value = {
            "string": lambda b: b.decode("utf-8", "replace"),
            "float": lambda b: struct.unpack("<f", b)[0],
            "double": lambda b: struct.unpack("<d", b)[0],
            "int": lambda b: struct.unpack("<i", b)[0],
            "rational": lambda b: struct.unpack("<iI", b),
            "v2i": lambda b: struct.unpack("<ii", b),
            "v2f": lambda b: struct.unpack("<ff", b),
        }.get(kind, lambda b: b.hex()[:64])(raw)
        attrs[name] = (kind, value)


rop, cam = build_scene()
exr_template = "$HIP/render/formats.$F4.exr"
if baseline_mode:
    render(rop, exr_template)
    BASELINE.mkdir(parents=True, exist_ok=True)
    for frame in (1, 2, 3):
        shutil.copy2(TMP / f"render/formats.{frame:04d}.exr", BASELINE / f"formats.{frame:04d}.exr")
    (BASELINE / "baseline.json").write_text(
        json.dumps(dict(houdini=hou.applicationVersionString(), created=time.ctime()), indent=1)
    )
    print("baseline written", BASELINE)
    raise SystemExit(0)

from cycles_houdini import passes, final_render, color_policy  # noqa: E402
from cycles_houdini import background_render as bg  # noqa: E402

# --- 1. Same scene, 3-frame range, every format ---------------------------------------
retained = []


def receiver(session):
    while retained:
        retained.pop().close()
    retained.append(session)
    return True


expected = dict(
    exr=("openexr", ["R", "G", "B", "A"], "float"),  # ROP default Precision: float
    png=("png", ["R", "G", "B", "A"], "uint8"),
    jpg=("jpeg", ["R", "G", "B"], "uint8"),
    jpeg=("jpeg", ["R", "G", "B"], "uint8"),
    tif=("tiff", ["R", "G", "B", "A"], "float"),
    tiff=("tiff", ["R", "G", "B", "A"], "float"),
)
images = {}
for ext, (fmt, channels, depth) in expected.items():
    if ext == "png":
        with final_render.retain_for_viewer(rop, receiver):
            render(rop, f"$HIP/render/formats.$F4.{ext}")
    else:
        render(rop, f"$HIP/render/formats.$F4.{ext}")
    for frame in (1, 2, 3):
        path = TMP / f"render/formats.{frame:04d}.{ext}"
        if not path.is_file():
            check(f"{ext}.{frame}.exists", False, path)
            continue
        info = pixels(path)
        images[(ext, frame)] = info
        ok = (
            info["format"] == fmt
            and info["channels"] == channels
            and info["depth"] == depth
            and (info["width"], info["height"]) == (64, 48)
            and (info["alpha"] == 3) == ("A" in channels)
        )
        check(f"{ext}.{frame}.format", ok, {k: v for k, v in info.items() if k not in ("data", "attributes")})
leftovers = sorted(p.name for p in (TMP / "render").glob(".cycles-*"))
check("no staged temporaries left", not leftovers, leftovers)

# --- 2. EXR pixels identical to the pre-change build ------------------------------------
for frame in (1, 2, 3):
    if BASELINE is None:
        checks[f"exr.{frame}.baseline"] = dict(ok=True, detail="skipped: no --baseline")
        continue
    base = BASELINE / f"formats.{frame:04d}.exr"
    new = TMP / f"render/formats.{frame:04d}.exr"
    if not base.is_file():
        check(f"exr.{frame}.baseline", False, f"missing {base}")
        continue
    a, b = pixels(base), pixels(new)
    same = a["channels"] == b["channels"] and a["depth"] == b["depth"] and np.array_equal(a["data"], b["data"])
    check(
        f"exr.{frame}.pixels_identical_to_baseline", same, np.abs(a["data"] - b["data"]).max() if same is False else ""
    )
    ra, rb = exr_header(base), exr_header(new)
    structural = ("channels", "compression", "dataWindow", "displayWindow", "lineOrder", "pixelAspectRatio")
    check(
        f"exr.{frame}.layout_unchanged",
        all(ra.get(k) == rb.get(k) for k in structural),
        {k: (ra.get(k), rb.get(k)) for k in structural if ra.get(k) != rb.get(k)},
    )
    check(f"exr.{frame}.size_bytes", True, (base.stat().st_size, new.stat().st_size))
    changed = {k: (ra[k], rb.get(k)) for k in ra if rb.get(k) != ra[k]}
    check(f"exr.{frame}.old_entries_kept", all(k in rb for k in ra), changed)
    if frame == 1:
        added = sorted(set(rb) - set(ra))
        checks["exr.added_entries"] = dict(ok=True, detail=added)

# --- 3. Header entries: present, correct, private ---------------------------------------
header = exr_header(TMP / "render/formats.0002.exr")
want = {
    "renderTime": "double",
    "cycles_houdini:samples_requested": "int",
    "cycles_houdini:samples_rendered": "int",
    "cycles_houdini:render_devices": "string",
    "cycles_houdini:resolution": "v2i",
    "capDate": "string",
    "utcOffset": "float",
    "framesPerSecond": "rational",
    "frame": "float",
    "cameraLabel": "string",
    "cycles_houdini:camera": "string",
    "pinholeFocalLength": "float",
    "cycles_houdini:camera_aperture": "float",
    "clipNear": "float",
    "clipFar": "float",
    "cycles_houdini:camera_projection": "string",
    "cycles_houdini:motion_blur": "int",
    "Software": "string",
    "cycles_houdini:release_id": "string",
    "cycles_houdini:houdini_version": "string",
    "cycles_houdini:hip_name": "string",
    "pixelAspectRatio": "float",
}
missing = {k: v for k, v in want.items() if header.get(k, (None,))[0] != v}
check("header.keys_and_types", not missing, dict(missing=missing, names=sorted(header)))


def value(name):
    return header.get(name, (None, None))[1]


check("header.renderTime", (value("renderTime") or 0) > 0, value("renderTime"))
check(
    "header.samples",
    value("cycles_houdini:samples_requested") == 8 and value("cycles_houdini:samples_rendered") == 8,
    (value("cycles_houdini:samples_requested"), value("cycles_houdini:samples_rendered")),
)
check(
    "header.devices",
    "CPU" in str(value("cycles_houdini:render_devices")).upper(),
    value("cycles_houdini:render_devices"),
)
check("header.resolution", value("cycles_houdini:resolution") == (64, 48), value("cycles_houdini:resolution"))
check("header.capDate", re.fullmatch(r"\d{4}:\d\d:\d\d \d\d:\d\d:\d\d", str(value("capDate"))), value("capDate"))
check("header.fps", tuple(value("framesPerSecond") or ()) == (24, 1), value("framesPerSecond"))
check("header.frame", value("frame") == 2.0, value("frame"))
check(
    "header.camera",
    value("cameraLabel") == "shot_cam" and value("cycles_houdini:camera") == "/obj/shot_cam",
    (value("cameraLabel"), value("cycles_houdini:camera")),
)
check(
    "header.focal",
    abs((value("pinholeFocalLength") or 0) - 35) < 1e-4
    and abs((value("cycles_houdini:camera_aperture") or 0) - 36) < 1e-4,
    (value("pinholeFocalLength"), value("cycles_houdini:camera_aperture")),
)
check(
    "header.clip",
    abs((value("clipNear") or 0) - 0.05) < 1e-6 and abs((value("clipFar") or 0) - 500) < 1e-3,
    (value("clipNear"), value("clipFar")),
)
check("header.hip_name", value("cycles_houdini:hip_name") == "x3_formats.hip", value("cycles_houdini:hip_name"))
check(
    "header.houdini_version",
    value("cycles_houdini:houdini_version") == hou.applicationVersionString(),
    value("cycles_houdini:houdini_version"),
)
check(
    "header.motion_off",
    value("cycles_houdini:motion_blur") == 0 and "shutterAngle" not in header,
    (value("cycles_houdini:motion_blur"), header.get("shutterAngle")),
)
user = os.environ.get("USERNAME", "") or os.environ.get("USER", "")
host = os.environ.get("COMPUTERNAME", "")
private = [str(TMP), str(TMP).replace("\\", "/"), hou.hipFile.path(), str(Path.home())]
leaks = {}
for path in [TMP / "render/formats.0002.exr"]:
    for name, (kind, data) in exr_header(path).items():
        text = str(data)
        if kind != "string" or name == "cycles_houdini:source_diagnostics" and text == "[]":
            continue
        hits = [p for p in private if p and p.lower() in text.lower()]
        if re.search(r"[A-Za-z]:[\\/]|\\\\", text):
            hits.append("drive/UNC path")
        for word in (user, host):
            if len(word) >= 3 and re.search(r"\b" + re.escape(word) + r"\b", text, re.I):
                hits.append("user/host name")
        if hits:
            leaks[name] = (text[:200], hits)
check("header.no_path_user_host", not leaks, leaks)
check(
    "png.has_text_metadata",
    images.get(("png", 1), {}).get("attributes", {}).get("cycles_houdini:hip_name") == "x3_formats.hip",
    sorted(images.get(("png", 1), {}).get("attributes", {})),
)

# Motion blur: shutter entries.
rop.parm("allowmotionblur").set(1)
render(rop, "$HIP/motion/formats.$F4.exr", (1, 1, 1))
rop.parm("allowmotionblur").set(0)
motion = exr_header(TMP / "motion/formats.0001.exr")
shutter = cam.parm("shutter").eval()
check(
    "header.motion_on",
    motion.get("cycles_houdini:motion_blur", (0, 0))[1] == 1
    and abs(motion.get("shutterAngle", (0, 0))[1] - shutter * 360) < 1e-3
    and abs(motion.get("expTime", (0, 0))[1] - shutter / 24) < 1e-6
    and "cycles_houdini:shutter_offset" in motion,
    {
        k: motion.get(k)
        for k in ("cycles_houdini:motion_blur", "shutterAngle", "expTime", "cycles_houdini:shutter_offset")
    },
)

# --- 4. TIFF is the scene-linear beauty; PNG is the viewer's picture ---------------------
for frame in (1, 3):
    exr, tif = images.get(("exr", frame)), images.get(("tif", frame))
    if exr and tif:
        diff = float(np.abs(exr["data"] - tif["data"]).max())
        check(f"tif.{frame}.scene_linear_equals_exr", diff < 2e-3, diff)  # EXR is half precision.
session = retained[0] if retained else None
check("viewer.session_retained", session is not None)
if session is not None:
    from PySide6 import QtGui

    app = QtGui.QGuiApplication.instance() or QtGui.QGuiApplication([])
    from cycles_houdini.viewer import panel, viewer_save

    policy = color_policy()
    display = policy["default_display"]
    settings = {"pass": "combined", "exposure": 0.0, "gamma": 1.0, "channels": "rgba", "use_alpha": False}
    if display:
        settings.update(display=display, view=policy["default_views"][display])
    frame = session.display_image(**settings)
    image = panel.CanvasBehaviour.merge_frame(None, frame)
    saved = TMP / "viewer_save.png"
    event = viewer_save.begin_image(image, str(saved), "png")
    event.wait(30)
    check("viewer.save_finished", event.is_set() and not event.error, event.error)
    # The viewer's JPEG Save of the live frame: the frame over black in scene-linear
    # light, View(RGB_premult), as the panel converts it at save time.
    over_black = session.display_image(**settings, backgrounds=[(0, 0, 0)])
    check("viewer.jpg_over_black_composite", len(over_black.get("composites", [])) == 1)
    jpg_image = panel.CanvasBehaviour.merge_frame(None, dict(over_black, rgba=over_black["composites"][0]))
    saved_jpg = TMP / "viewer_save.jpg"
    event = viewer_save.begin_image(jpg_image, str(saved_jpg), "jpg")
    event.wait(30)
    if saved.is_file():
        viewer = pixels(saved)
        disk = images.get(("png", 3))
        diff = int(np.abs(viewer["data"].astype(int) - disk["data"].astype(int)).max()) if disk else -1
        raw = np.frombuffer(frame["rgba"], np.uint8).reshape(frame["height"], frame["width"], 4)
        if disk is not None:
            a = raw[..., 3].astype(int)
            ranges = {
                "alpha 255": a == 255,
                "alpha 17-254": (a > 16) & (a < 255),
                "alpha 1-16": (a > 0) & (a <= 16),
                "alpha 0": a == 0,
            }

            def by_alpha(first, second):
                delta = np.abs(first.astype(int) - second.astype(int)).max(axis=2)
                return {name: [int(mask.sum()), int(delta[mask].max(initial=0))] for name, mask in ranges.items()}

            checks["png.difference_by_alpha"] = dict(
                ok=True,
                detail={
                    "[pixels, max code difference]": "",
                    "displayed bytes vs final-render PNG": by_alpha(raw, disk["data"]),
                    "displayed bytes vs viewer Save PNG": by_alpha(raw, viewer["data"]),
                    "viewer Save PNG vs final-render PNG": by_alpha(viewer["data"], disk["data"]),
                },
            )
        # The viewer's Save writes the displayed bytes through Qt; Qt's PNG writer may round-trip
        # low-coverage pixels through premultiplied 8-bit. Compare the picture as shown
        # (premultiplied, what any compositor sees) and the displayed bytes themselves.
        premultiplied = -1
        if disk is not None and viewer["data"].shape == disk["data"].shape:

            def premultiply(d):
                return np.rint(d[..., :3].astype(float) * d[..., 3:4] / 255.0)

            premultiplied = int(np.abs(premultiply(viewer["data"]) - premultiply(disk["data"])).max())
            premultiplied = max(
                premultiplied, int(np.abs(viewer["data"][..., 3].astype(int) - disk["data"][..., 3].astype(int)).max())
            )
        exact = int(np.abs(raw.astype(int) - disk["data"].astype(int)).max()) if disk is not None else -1
        check(
            "png.equals_viewer_save_within_1",
            0 <= premultiplied <= 1 and 0 <= exact <= 1,
            dict(
                premultiplied_or_alpha_max_difference=premultiplied,
                displayed_bytes_max_difference=exact,
                raw_file_max_difference=diff,
            ),
        )
        alpha = disk["data"][..., 3] if disk else None
        check(
            "png.alpha_has_coverage",
            alpha is not None and alpha.min() == 0 and alpha.max() == 255 and ((alpha > 0) & (alpha < 255)).any(),
            alpha is not None and (int(alpha.min()), int(alpha.max())),
        )
        session_png = TMP / "session.png"
        session.write_exr(str(session_png), dict(passes=[dict(source="combined", layer="")], alpha=True))
        same = pixels(session_png)
        check("png.session_write_equals_rop_frame", np.array_equal(same["data"], disk["data"]), "")
    if saved_jpg.is_file():
        viewer = pixels(saved_jpg)
        disk = images.get(("jpg", 3))
        diff = float(np.abs(viewer["data"].astype(int) - disk["data"].astype(int)).mean()) if disk else -1
        check(
            "jpg.close_to_viewer_save",
            0 <= diff < 3,
            dict(mean_abs_code_difference=diff, note="JPEG is lossy; viewer quality 95, disk 100"),
        )
        if disk is not None:
            # The ROP JPEG is View(RGB_premult) (scene-linear over black), not the display-space
            # premultiply of the straight bytes that both used before. Partial pixels are edges,
            # where JPEG's own error is largest: require the stored file to be clearly nearer.
            raw = np.frombuffer(frame["rgba"], np.uint8).reshape(frame["height"], frame["width"], 4).astype(float)
            linear = np.frombuffer(over_black["composites"][0], np.uint8).reshape(raw.shape)[..., :3].astype(float)
            old = np.rint(raw[..., :3] * raw[..., 3:4] / 255.0)
            partial = (raw[..., 3] > 0) & (raw[..., 3] < 255)
            stored = disk["data"][..., :3].astype(float)
            to_linear = float(np.abs(stored - linear)[partial].mean()) if partial.any() else -1
            to_old = float(np.abs(stored - old)[partial].mean()) if partial.any() else -1
            check(
                "jpg.rop_is_scene_linear_over_black",
                partial.any() and to_linear < 0.5 * to_old,
                dict(
                    partial_pixels=int(partial.sum()),
                    mean_to_linear_over_black=to_linear,
                    mean_to_display_premultiply=to_old,
                ),
            )
    for s in retained:
        s.close()

# --- 5. Passes with a non-EXR Output Picture --------------------------------------------
index = passes.add(rop, "normal", name="N", separate=False)
rop.parm("output_separate_passes").set(0)
try:
    render(rop, "$HIP/passes/shared.$F4.png", (1, 1, 1))
    check("passes.shared_refused", False, "rendered")
except hou.OperationFailed as error:
    text = str(error)
    check(
        "passes.shared_refused",
        "holds only the beauty image" in text and "'N'" in text and not (TMP / "passes").exists(),
        text[-600:],
    )
rop.parm("pass_separate" + str(index)).set(1)
render(rop, "$HIP/passes/sep.$F4.png", (1, 1, 1))
files = sorted(p.name for p in (TMP / "passes").glob("*"))
check("passes.separate_go_to_exr", files == ["sep.0001.png", "sep.N.0001.exr"], files)
if (TMP / "passes/sep.N.0001.exr").is_file():
    pass_header = exr_header(TMP / "passes/sep.N.0001.exr")
    check(
        "passes.pass_file_has_metadata",
        all(k in pass_header for k in ("renderTime", "cameraLabel", "cycles_houdini:hip_name")),
        sorted(pass_header),
    )
    check("passes.pass_file_channels", pass_header["channels"][1] is not None, pass_header["channels"])
rop.parm("output_multipart").set(1)
try:
    render(rop, "$HIP/passes/multi.$F4.tif", (1, 1, 1))
    check("passes.multipart_with_tif_refused", False, "rendered")
except hou.OperationFailed as error:
    text = str(error)
    check(
        "passes.multipart_with_tif_refused",
        "Multipart EXR" in text and not list((TMP / "passes").glob("multi*")),
        text[-600:],
    )
rop.parm("output_multipart").set(0)
rop.parm("pass_enable" + str(index)).set(0)

# --- 6. Existing-file policies -------------------------------------------------------------
before = (TMP / "render/formats.0001.png").stat().st_mtime_ns
rop.parm("output_existing").set("skip")
render(rop, "$HIP/render/formats.$F4.png")
summary = rop.cachedUserData(final_render._summary_key)
check(
    "skip.png_frames_skipped",
    summary["skipped_frames"] == 3 and (TMP / "render/formats.0001.png").stat().st_mtime_ns == before,
    summary,
)
rop.parm("output_existing").set("error")
try:
    render(rop, "$HIP/render/formats.$F4.jpg", (1, 1, 1))
    check("error.existing_jpg_refused", False, "rendered")
except hou.OperationFailed as error:
    check("error.existing_jpg_refused", "already exists" in str(error), str(error)[-300:])
rop.parm("output_existing").set("overwrite")
try:
    render(rop, "$HIP/render/formats.$F4.bmp", (1, 1, 1))
    check("unsupported_extension_refused", False, "rendered")
except hou.OperationFailed as error:
    check("unsupported_extension_refused", ".exr, .png, .jpg" in str(error), str(error)[-300:])

# --- 7. Farm path: saved HIP, background hython, PNG frame range -----------------------
if True:
    rop.parm("picture").set("$HIP/render/formats.$F4.exr")
    hou.hipFile.save(str(TMP / "x3_formats.hip"), save_to_recent_files=False)
    script = Path(__import__("cycles_houdini").__file__).resolve().parents[2] / "scripts/render_hip.py"
    report = TMP / "farm.json"
    command = [
        str(Path(hou.getenv("HFS")) / "bin/hython.exe"),
        str(script),
        str(TMP / "x3_formats.hip"),
        "--rop",
        rop.path(),
        "--report",
        str(report),
        "--output",
        "$HIP/farm/formats.$F4.png",
    ]
    result = subprocess.run(command, env=bg._environment(), capture_output=True, text=True, timeout=300)
    farm = [TMP / f"farm/formats.{f:04d}.png" for f in (1, 2, 3)]
    same = all(p.is_file() for p in farm) and all(
        np.array_equal(pixels(p)["data"], images[("png", f)]["data"]) for f, p in zip((1, 2, 3), farm)
    )
    check(
        "farm.png_range_matches_interactive", result.returncode == 0 and same, (result.returncode, result.stderr[-800:])
    )

# --- 8. Samples for the user ---------------------------------------------------------------
SAMPLES = SAMPLES or OUT / "samples"
SAMPLES.mkdir(parents=True, exist_ok=True)
for name in (
    "render/formats.0001.exr",
    "render/formats.0001.png",
    "render/formats.0001.jpg",
    "render/formats.0001.tif",
    "passes/sep.N.0001.exr",
    "viewer_save.png",
    "motion/formats.0001.exr",
):
    if (TMP / name).is_file():
        shutil.copy2(TMP / name, SAMPLES / name.replace("/", "_"))
lines = ["Raw OpenEXR header of render/formats.0002.exr (frame 2 of the 3-frame test range)", ""]
for name, (kind, data) in sorted(exr_header(TMP / "render/formats.0002.exr").items()):
    lines.append(f"{name} ({kind}) = {data}")
lines += ["", "Raw OpenEXR header entries of motion/formats.0001.exr (motion blur on):", ""]
for name in ("cycles_houdini:motion_blur", "shutterAngle", "expTime", "cycles_houdini:shutter_offset"):
    lines.append(f"{name} = {motion.get(name)}")
lines += ["", "PNG text chunks (render/formats.0001.png):", ""]
for name, data in sorted(images.get(("png", 1), {}).get("attributes", {}).items()):
    lines.append(f"{name} = {str(data)[:200]}")
lines += ["", "TIFF tags (render/formats.0001.tif):", ""]
for name, data in sorted(images.get(("tif", 1), {}).get("attributes", {}).items()):
    lines.append(f"{name} = {str(data)[:200]}")
(SAMPLES / "header-dump.txt").write_text("\n".join(lines), encoding="utf-8")

report = dict(result="passed" if not failures else "failed", failures=failures, checks=checks, temp=str(TMP))
(OUT / "report.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
if args.samples:
    (SAMPLES / "report.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
print(json.dumps(dict(result=report["result"], failures={name: checks[name]["detail"] for name in failures})))
raise SystemExit(0 if not failures else 1)
