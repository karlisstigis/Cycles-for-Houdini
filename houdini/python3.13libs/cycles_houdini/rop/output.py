# SPDX-License-Identifier: Apache-2.0
"""Shared output policy for native final rendering and interactive viewers."""

import json
import stat
import math
import os
import re
from pathlib import Path
import threading
from .rop_controls import value


def scaled_dimensions(base, percent):
    return tuple(max(1, int(n * percent / 100 + 1e-9)) for n in base)


def ensure_viewer_resolution_precision(rop):
    """Correct saved per-node integer templates so viewer percentages retain fractions."""
    import hou

    parm = rop.parm("ipr_resolution_percent")
    if parm is None or parm.parmTemplate().type() != hou.parmTemplateType.Int:
        return
    previous = parm.parmTemplate()
    keys = parm.keyframes()
    number = parm.eval()
    template = hou.FloatParmTemplate(
        previous.name(),
        previous.label(),
        1,
        default_value=(0,),
        min=0,
        max=400,
        min_is_strict=True,
        max_is_strict=True,
        help=previous.help(),
    )
    template.setTags(previous.tags())
    template.hide(previous.isHidden())
    template.setJoinWithNext(previous.joinsWithNext())
    template.hideLabel(previous.isLabelHidden())
    for kind, expression in previous.conditionals().items():
        template.setConditional(kind, expression)
    group = rop.parmTemplateGroup()
    group.replace(previous.name(), template)
    rop.setParmTemplateGroup(group)
    replacement = rop.parm(previous.name())
    if keys:
        replacement.setKeyframes(keys)
    else:
        replacement.set(number)


def dimensions(rop, now, *, interactive=True):
    percent = value(rop, "resolution_percent", now)
    # Viewer dimensions are independent of its Interactive/Final sampling mode.
    # Disk/farm/clapper rendering explicitly requests interactive=False.
    preview = value(rop, "ipr_resolution_percent", now) if interactive else 0
    if preview:
        percent = preview
    if not math.isfinite(percent) or not 0 < percent <= 400:
        raise ValueError("Resolution percentage must be positive and at most 400")
    base = [value(rop, name, now) for name in ("resx", "resy")]
    if any(n < 1 for n in base):
        raise ValueError("Image dimensions must be positive")
    return scaled_dimensions(base, percent)


def _base_options(rop, now):
    codec = value(rop, "output_compression", now)
    result = dict(
        half_color=value(rop, "output_precision", now) == "half",
        compression=codec,
        dwa_level=value(rop, "output_dwa_level", now) if codec in ("dwaa", "dwab") else 45.0,
        crop_to_region=bool(value(rop, "crop_to_region", now)),
        pixel_aspect=value(rop, "pixel_aspect", now),
    )
    if value(rop, "output_color_override", now):
        from .. import color_policy

        policy = color_policy()
        target = value(rop, "output_color_space", now)
        if not target or target not in policy["output_spaces"]:
            raise ValueError("Choose an output color space from the active OCIO configuration")
        result.update(color_space=target, color_configuration=policy["configuration_id"])
    return result


LOSSLESS = {"none", "rle", "zips", "zip", "piz"}
CODECS = LOSSLESS | {"dwaa", "dwab", "pxr24", "b44", "b44a"}
# As in Mantra and Karma, the Output Picture's extension selects the format. PNG and
# JPEG are display-referred 8-bit (the viewer's picture), TIFF is scene-linear 32-bit
# float. Only EXR holds passes: with another format they must go to EXR files.
FORMATS = {".exr": "openexr", ".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".tif": "tiff", ".tiff": "tiff"}
FORMAT_LABELS = {"openexr": "EXR", "png": "PNG", "jpeg": "JPEG", "tiff": "TIFF"}


def image_format(path):
    """OpenImageIO format name for an output filename; unknown extensions are refused."""
    result = FORMATS.get(Path(path).suffix.lower())
    if result is None:
        raise ValueError("Output Picture must end in .exr, .png, .jpg, .jpeg, .tif or .tiff: " + str(path))
    return result


def _pass_filename(main, name, rop, now, filename=None):
    """Insert the layer before a known frame field, keeping static numbers literal."""
    if not filename:
        import hou

        parm = rop.parm("picture")
        try:
            template = parm.unexpandedString() if parm is not None else ""
        except hou.OperationFailed:
            template = ""  # Keyframed/Python string expressions keep the static fallback.
        basename = template.replace("\\", "/").rsplit("/", 1)[-1]
        tokens = list(re.finditer(r"\$(?:\{(?:FF|F\d*)\}|FF(?![A-Za-z])|F\d*(?![A-Za-z]))", basename))
        if tokens:
            token = tokens[-1]
            suffix = basename[token.end() :]
            # Only literal suffixes can be located without evaluating the user's
            # expression again. Directory tokens and static numeric suffixes do
            # not determine a frame field in the evaluated basename.
            if not any(c in suffix for c in "$`"):
                # Expand only the recognized frame token, never the user's
                # path/expression. Its exact text disambiguates fractional
                # frames from numeric camera/version components and signs.
                frame = hou.text.expandStringAtFrame(token.group(), hou.timeToFrame(now))
                preceding = basename[token.start() - 1 : token.start()]
                separator = preceding if preceding in ("_", ".", "-") else ""
                ending = separator + frame + suffix
                if main.name.endswith(ending):
                    prefix = main.name.removesuffix(ending)
                    return main.with_name((prefix + "." if prefix else "") + name + (separator or ".") + frame + suffix)
    return main.with_name(main.stem + "." + name + main.suffix)


def plan(rop, now, filename=None):
    """Resolve one ordered set of EXRs before starting a render or save."""
    from . import passes

    base = _base_options(rop, now)
    main = Path(filename or value(rop, "picture", now)).expanduser().absolute()
    if not str(filename or value(rop, "picture", now)).strip():
        raise ValueError("Set Output Picture")
    picture_format = image_format(main)
    exr_picture = picture_format == "openexr"
    multipart = (
        exr_picture and bool(value(rop, "output_multipart", now)) and not value(rop, "output_separate_passes", now)
    )
    base["multipart"] = multipart
    pass_folders = bool(value(rop, "output_pass_folders", now))
    beauty = main.parent / "beauty" / main.name if pass_folders else main
    files = {}
    part_names = {"Beauty"}

    def append(path, settings, selection):
        settings = dict(settings)
        if path.suffix.lower() != ".exr" and not (path == beauty and not exr_picture):
            raise ValueError("Cycles output requires an .exr filename: " + str(path))
        codec = settings["compression"]
        if codec not in CODECS:
            raise ValueError("Unsupported EXR compression: " + codec)
        if codec not in ("dwaa", "dwab"):
            settings["dwa_level"] = 45.0
        if multipart:
            selection = dict(selection, compression=codec, dwa_level=settings["dwa_level"])
            settings = dict(base)
        key = os.path.normcase(os.path.normpath(str(path)))
        if key not in files:
            files[key] = dict(filename=str(path), options={**settings, "passes": []})
        job = files[key]
        ignored = {"passes"}
        if {key: value for key, value in job["options"].items() if key not in ignored} != {
            key: value for key, value in settings.items() if key not in ignored
        }:
            raise ValueError("Passes sharing one file must use matching compression and color settings: " + str(path))
        existing = {item["layer"] for item in job["options"]["passes"]}
        if selection["layer"] in existing:
            raise ValueError("Duplicate output layer in " + str(path) + ": " + selection["layer"])
        job["options"]["passes"].append(selection)

    append(
        beauty,
        base if exr_picture else dict(base, alpha=bool(value(rop, "transparent_background", now))),
        dict(source="combined", layer="", precision="inherit", part="Beauty")
        if multipart
        else dict(source="combined", layer="", precision="inherit"),
    )
    for entry in passes.entries(rop, now, output=True):
        kind = entry["type"]
        name = entry["name"]
        if entry["precision"] not in ("inherit", "half", "float"):
            raise ValueError("Unknown pass precision: " + entry["precision"])
        if not exr_picture and not entry["separate"]:
            # Never drop a requested pass: it cannot live in a PNG/JPEG/TIFF beauty file.
            raise ValueError(
                f"The {FORMAT_LABELS[picture_format]} Output Picture holds only the beauty image, but pass "
                f"'{name}' is set to share it (Multipart EXR or no Separate File). Turn on Passes to "
                f"Separate Files, or use an .exr Output Picture: {main}"
            )
        if multipart:
            if name in part_names:
                raise ValueError("Duplicate multipart EXR part name: " + name)
            part_names.add(name)
        settings = dict(base)
        path = beauty
        if entry["separate"]:
            path = (
                Path(entry["path"]).expanduser().absolute()
                if entry["path"].strip()
                else _pass_filename(main, name, rop, now, filename)
            )
            if not exr_picture and not entry["path"].strip():
                path = path.with_suffix(".exr")  # Passes beside a PNG/JPEG/TIFF stay EXR.
            if pass_folders and not entry["path"].strip():
                path = path.parent / name / path.name
        if (entry["separate"] or multipart) and entry["compression"] != "inherit":
            settings.update(compression=entry["compression"], dwa_level=entry["dwa_level"])
        part = dict(part=name) if multipart else {}
        if kind.startswith("crypto_"):
            if not 2 <= entry["levels"] <= 16:
                raise ValueError("Cryptomatte Levels must be between 2 and 16")
            for index in range((entry["levels"] + 1) // 2):
                append(
                    path,
                    settings,
                    dict(
                        source="Crypto" + kind[7:].title() + f"{index:02}",
                        layer=name + f"{index:02}",
                        precision=entry["precision"],
                        **part,
                    ),
                )
        else:
            source = (
                "lightgroup_" + entry["source"]
                if kind == "lightgroup"
                else entry["source"]
                if kind.startswith("aov_")
                else kind
            )
            append(path, settings, dict(source=source, layer=name, precision=entry["precision"], **part))
    return [dict(filename=job["filename"], options=job["options"]) for job in files.values()]


def options(rop, now):
    """Main output only; final rendering uses the entire output plan."""
    return plan(rop, now)[0]["options"]


def expected_headers(rop, now, jobs, *, render_crop=None):
    """Mirror the writer's public pass naming contract, using the native catalogue."""
    from . import passes

    descriptors = {entry["id"]: entry for entry in passes.catalogue()}
    for entry in passes.entries(rop, now):
        kind = entry["type"]
        source = entry["source"]
        if kind in ("aov_color", "aov_value"):
            descriptors[source] = dict(
                channels=["R", "G", "B", "A"] if kind == "aov_color" else ["X"], cryptomatte=False
            )
        elif kind == "lightgroup":
            descriptors["lightgroup_" + source] = dict(channels=["R", "G", "B"], cryptomatte=False)
        elif kind.startswith("crypto_"):
            for index in range((entry["levels"] + 1) // 2):
                descriptors["Crypto" + kind[7:].title() + f"{index:02}"] = dict(
                    channels=["R", "G", "B", "A"], cryptomatte=True
                )
    width, height = dimensions(rop, now, interactive=False)
    x, y, w, h = render_crop if render_crop is not None else crop(rop, now, width, height)
    result = []
    for job in jobs:
        options = job["options"]
        multipart = options.get("multipart", False)
        kind = FORMATS.get(Path(job["filename"]).suffix.lower(), "openexr")
        if kind != "openexr":
            # PNG/JPEG/TIFF: the beauty image only, padded to the full frame unless cropped.
            size = (w, h) if options.get("crop_to_region") else (width, height)
            alpha = options.get("alpha", True) and kind != "jpeg"
            channels = ["R", "G", "B"] + (["A"] if alpha else [])
            result.append(dict(window=(0, 0, *size, 0, 0, *size), parts={"": channels}, multipart=False, format=kind))
            continue
        window = (
            (0, 0, w, h, 0, 0, w, h)
            if options.get("crop_to_region")
            else (x, height - y - h, w, h, 0, 0, width, height)
        )
        parts = {}
        for selected in options["passes"]:
            source = selected["source"]
            descriptor = descriptors[source]
            channels = descriptor["channels"]
            crypto = descriptor.get("cryptomatte", False)
            layer = selected.get("layer", "") or (source if source != "combined" else "")
            name = (selected.get("part") or (layer[:-2] if crypto else layer or "Beauty")) if multipart else ""
            target = parts.setdefault(name, [])
            standalone = not multipart and len(options["passes"]) == 1 and not crypto
            for index, channel in enumerate(channels):
                target.append(
                    ("Y" if len(channels) == 1 else "RGB"[index] if index < 3 else channel)
                    if standalone
                    else (layer + "." + channel if layer else channel)
                )
        result.append(dict(window=window, parts=parts, multipart=multipart))
    return result


# Houdini focal/aperture units (Focal Units) in millimetres.
_FOCAL_MM = dict(m=1000.0, km=1e6, cm=10.0, mm=1.0, nm=1e-6, pm=1e-9, yd=914.4, ft=304.8, cubit=462.788)
_FOCAL_MM["in"] = 25.4


def header_metadata(rop, now):
    """Render information for every output header of a final frame.

    Typed entries for the native writer, which adds render time, samples, devices,
    resolution and capture date itself. OpenEXR standard names where one exists
    (pinholeFocalLength, shutterAngle, framesPerSecond, utcOffset, capDate via DateTime, expTime
    via ExposureTime, cameraLabel), Karma's names for clipNear/clipFar/frame, and the
    cycles_houdini: prefix otherwise. Never the host, the user or a file path: frames
    go to clients, so the scene is named by its HIP file name only.
    A header can never fail a render: anything that cannot be read is left out.
    """
    import hou
    from fractions import Fraction

    entries = []

    def add(name, kind, data):
        entries.append(dict(name=name, type=kind, value=data))

    try:
        release = os.environ.get("CYCLES_HOUDINI_RELEASE_ID") or "development"
        version = hou.applicationVersionString()
        add("Software", "string", f"Cycles for Houdini {release}, Houdini {version}")
        add("cycles_houdini:release_id", "string", release)
        add("cycles_houdini:houdini_version", "string", version)
        add("cycles_houdini:hip_name", "string", hou.hipFile.basename())
        add("frame", "float", float(hou.timeToFrame(now)))
        fps = Fraction(hou.fps()).limit_denominator(1001)
        add("framesPerSecond", "rational", [fps.numerator, fps.denominator])
        camera_parm = rop.parm("camera")
        camera = hou.node(camera_parm.evalAtTime(now)) if camera_parm is not None else None
        if camera is not None:

            def read(name, default=None):
                parm = camera.parm(name)
                return parm.evalAtTime(now) if parm is not None else default

            add("cameraLabel", "string", camera.name())
            add("cycles_houdini:camera", "string", camera.path())  # Node path, not a file path.
            scale = _FOCAL_MM.get(read("focalunits", "mm"))
            if scale is not None and read("focal") is not None:
                add("pinholeFocalLength", "float", read("focal") * scale)
            if scale is not None and read("aperture") is not None:
                add("cycles_houdini:camera_aperture", "float", read("aperture") * scale)  # Horizontal, mm.
            for name, key in (("near", "clipNear"), ("far", "clipFar")):
                if read(name) is not None:
                    add(key, "float", read(name))
            projection = camera.parm("projection")
            if projection is not None:
                add("cycles_houdini:camera_projection", "string", projection.evalAsStringAtFrame(hou.timeToFrame(now)))
        from ..scene import native_scene

        motion = native_scene.motion_configuration(rop, now)
        blur = bool(motion["enabled"]) and motion["shutter_frames"] > 0
        add("cycles_houdini:motion_blur", "int", int(blur))
        if blur:
            add("shutterAngle", "float", motion["shutter_frames"] * 360.0)
            add("ExposureTime", "float", motion["shutter_frames"] / motion["fps"])
            add("cycles_houdini:shutter_offset", "float", motion["offset"])
    except Exception as error:  # noqa: BLE001 - incomplete information never fails a frame.
        print(f"Cycles: render information header incomplete ({type(error).__name__}: {error})", flush=True)
    return entries


def _existing_header(spec):
    return dict(
        window=(spec.x, spec.y, spec.width, spec.height, spec.full_x, spec.full_y, spec.full_width, spec.full_height),
        flat=spec.depth == 1 and spec.z == 0 and spec.full_z == 0 and spec.full_depth == 1,
        name=spec.get_string_attribute("oiio:subimagename"),
        channels=spec.channelnames,
        validation_version=spec.get_int_attribute("cycles_houdini:input_validation_version", 0),
        missing_inputs=spec.get_int_attribute("cycles_houdini:missing_input_count", -1),
        diagnostics=spec.get_string_attribute("cycles_houdini:source_diagnostics"),
    )


def _inspect_headers(headers, contract, path, label, require_clean_inputs):
    diagnostics = []
    uncertified = False
    missing_inputs = False
    actual = {}
    for header in headers:
        if tuple(header["window"]) != tuple(contract["window"]) or not header["flat"]:
            raise ValueError(f"Existing {label} dimensions or windows do not match: {path}")
        name = header["name"] if contract["multipart"] else ""
        if name in actual:
            raise ValueError(f"Existing {label} has unexpected/duplicate parts: {path}")
        actual[name] = sorted(header["channels"])
        report = header["diagnostics"]
        if require_clean_inputs:
            version, count = header["validation_version"], header["missing_inputs"]
            uncertified = uncertified or not report or version != 1 or count < 0
            missing_inputs = missing_inputs or count > 0
        if report:
            try:
                values = json.loads(report)
                if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
                    raise ValueError()
                for item in values:
                    if item not in diagnostics:
                        diagnostics.append(item)
            except (ValueError, TypeError):
                raise ValueError(f"Existing {label} has invalid diagnostic provenance: {path}")
    if actual != {name: sorted(channels) for name, channels in contract["parts"].items()}:
        raise ValueError(f"Existing {label} channels or parts do not match: {path}")
    if require_clean_inputs and (uncertified or missing_inputs):
        if uncertified:
            message = f"Existing {label} has uncertified render-input provenance; use Overwrite to replace it: {path}"
            diagnostics.append(
                dict(
                    code="output.uncertified_inputs",
                    source=str(path),
                    resource="output:" + str(path),
                    message="Existing pixels have no certified render-input provenance",
                    action="Use Overwrite to replace them with a verified render",
                    severity="warning",
                )
            )
        else:
            message = f"Existing {label} contains missing render inputs; use Overwrite after repairing inputs: {path}"
            details = "; ".join(
                "[" + str(issue.get("code", "")) + "] " + str(issue.get("source") or issue.get("resource", ""))
                for issue in diagnostics
            )
            if details:
                message += ": " + details
        error = ValueError(message)
        error.source_diagnostics = diagnostics
        error.existing_output = True
        raise error
    return diagnostics


def inspect_existing(filename, contract, *, require_clean_inputs=False, before_read=None, read_pixels=True):
    """Return diagnostics for an existing output with the expected layout and readable EXR pixels."""
    path = Path(filename)
    kind = contract.get("format", "openexr")
    label = FORMAT_LABELS.get(kind, kind)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(mode):
        raise ValueError("Existing output is not a regular file: " + str(path))

    if kind == "openexr":
        from .. import _native

        diagnostics = []

        def validate(headers):
            diagnostics.extend(_inspect_headers(headers, contract, path, label, require_clean_inputs))
            return read_pixels

        try:
            _native.inspect_existing_exr(str(path), len(contract["parts"]), validate, before_read)
        except RuntimeError as error:
            raise ValueError(str(error)) from error
        return diagnostics

    # Other formats retain the existing header-only checks.
    import OpenImageIO as oiio

    image = oiio.ImageInput.open(str(path))
    if image is None:
        detail = oiio.geterror()
        raise ValueError(f"Existing output is not a readable {label}: {path}: {detail}")
    try:
        if image.format_name() != kind:
            raise ValueError(f"Existing output is not a {label} file: {path}")
        headers = []
        for index in range(len(contract["parts"]) + 1):
            if not image.seek_subimage(index, 0):
                image.geterror()
                break
            headers.append(_existing_header(image.spec()))
        return _inspect_headers(headers, contract, path, label, require_clean_inputs)
    finally:
        image.close()


def write_outputs(session, jobs, *, before_write=None, wait_for_write=None):
    generation = session.status()["generation"]
    for job in jobs:
        if before_write is not None:
            before_write()
        if session.status()["generation"] != generation:
            raise RuntimeError("The render changed while saving its output files")
        path = Path(job["filename"])
        path.parent.mkdir(parents=True, exist_ok=True)
        begin = getattr(session, "begin_write_exr", None)
        if begin is None:
            session.write_exr(path, job["options"])
        else:
            event = begin(str(path), job["options"])
            if wait_for_write is None:
                while not session.write_finished(event):
                    event.wait(0.1)
            else:
                wait_for_write(session, event)
            session.finish_write_exr(event)


def begin_write_outputs(session, jobs):
    """One bounded filesystem/encoding task; never call HOM from its thread."""
    from copy import deepcopy

    jobs = deepcopy(jobs)
    event = threading.Event()
    event.error = None

    def run():
        try:
            write_outputs(session, jobs)
        except Exception as error:
            event.error = error
        finally:
            event.set()

    threading.Thread(target=run, name="Cycles output files", daemon=True).start()
    return event


def crop(rop, now, width, height):
    if not value(rop, "render_region", now):
        return (0, 0, width, height)
    left, right, bottom, top = rop.parmTuple("render_region_bounds").evalAtTime(now)
    if not all(math.isfinite(n) for n in (left, right, bottom, top)) or not (
        0 <= left < right <= 1 and 0 <= bottom < top <= 1
    ):
        raise ValueError("Render region must have 0 <= Left < Right <= 1 and 0 <= Bottom < Top <= 1")
    x, y = math.floor(left * width), math.floor(bottom * height)
    return (x, y, math.ceil(right * width) - x, math.ceil(top * height) - y)
