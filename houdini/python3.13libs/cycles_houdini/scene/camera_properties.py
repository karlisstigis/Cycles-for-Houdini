# SPDX-License-Identifier: Apache-2.0
"""Opt-in native camera controls and shared OBJ/SOHO depth-of-field semantics."""

import math
import struct
import hou
from .object_properties import install_tabs, _section, _toggle, _float, _integer, _menu, _condition

PANORAMA_TYPES = (
    ("equirectangular", "Equirectangular"),
    ("equiangular_cubemap_face", "Equiangular Cubemap Face"),
    ("fisheye_equidistant", "Fisheye Equidistant"),
    ("fisheye_equisolid", "Fisheye Equisolid"),
    ("fisheye_lens_polynomial", "Fisheye Lens Polynomial"),
    ("mirrorball", "Mirror Ball"),
    ("central_cylindrical", "Central Cylindrical"),
)
POLYNOMIAL_DEFAULTS = (-1.17351e-5, -0.0199887, -3.3525e-6, 3.0993e-6, -2.61e-8)


class CameraDofSourceError(ValueError):
    def __init__(self, message, attribute):
        super().__init__(message)
        self.attribute = attribute


def install(node):
    if node.type().category() != hou.objNodeTypeCategory() or not all(
        node.parm(n) for n in ("focal", "focus", "fstop")
    ):
        return False
    group = node.parmTemplateGroup()
    existing = bool(group.find("cycles_dof"))
    from ..core import image_iris

    iris_visible = image_iris.supported()
    iris_template = group.find("cycles_camera_image_iris")
    dof_template = group.find("cycles_camera_dof")
    iris_method = group.find("cycles_image_iris_method")
    if (
        existing
        and group.find("cycles_rolling_shutter")
        and group.find("cycles_shutter_curve")
        and group.find("cycles_projection")
        and iris_template
        and iris_template.isHidden() != iris_visible
        and iris_method
        and iris_method.defaultValue() == ("cdf",)
        and group.find("cycles_image_iris_color")
        and dof_template
        and dof_template.tags().get("cycles_dof_help_version") == "1"
        and dof_template.tags().get("sidefx::header_toggle") == "cycles_dof"
        and iris_template.tags().get("sidefx::header_toggle") == "cycles_image_iris"
    ):
        return False
    controls = [
        _toggle(
            "dof",
            "Enable",
            help="Use a finite aperture. Focus Distance sets the plane of focus and F-Stop controls how quickly objects fall out of focus.",
        ),
        _float(
            "focus_distance",
            "Focus Distance",
            10,
            0.00001,
            1000,
            "Focus distance in scene units: radial distance for panoramic lenses, distance to the focus plane otherwise. Initially linked to the native camera Focus Distance.",
        ),
        _float(
            "fstop",
            "F-Stop",
            5.6,
            0.001,
            64,
            "Lower values give stronger defocus. Initially linked to the native camera F-Stop.",
        ),
        _integer(
            "aperture_blades",
            "Blades",
            0,
            0,
            32,
            "Zero gives a circular aperture. Polygonal apertures require at least three blades. Ignored while Image Iris is enabled.",
        ),
        _float("aperture_rotation", "Rotation", 0, -360, 360, "Aperture rotation in degrees."),
        _float(
            "aperture_ratio",
            "Ratio",
            1,
            0.01,
            2,
            "Aperture aspect ratio. One keeps the original shape; other values produce anamorphic bokeh. The slider follows Blender: 0.01 to 2; larger values can be typed.",
        ),
    ]
    controls[4].setMinIsStrict(False)
    controls[5].setMaxIsStrict(False)
    for parm in controls[1:]:
        disable = "{ cycles_dof == 0 }"
        if parm.name() == "cycles_aperture_blades":
            disable += " { cycles_image_iris == 1 }"
        _condition(parm, disable)
    controls.extend(image_iris.controls())
    new_curve = not bool(group.find("cycles_shutter_curve"))
    curve = hou.RampParmTemplate(
        "cycles_shutter_curve",
        "Exposure",
        hou.rampParmType.Float,
        default_basis=hou.rampBasis.Linear,
        tags={
            "rampfloatdefault": "1pos ( 0 ) 1value ( 1 ) 1interp ( linear ) 2pos ( 1 ) 2value ( 1 ) 2interp ( linear )"
        },
        help="Relative exposure across the shutter interval. Uniform values give a global rectangular exposure. Only nonnegative finite weights are usable.",
    )
    curve.setConditional(
        hou.parmCondType.DisableWhen,
        "{ cycles_use_shutter_curve == 0 } { cycles_rolling_shutter == top cycles_rolling_duration == 0 }",
    )
    sampling = [
        _menu(
            "rolling_shutter",
            "Rolling Shutter",
            [("none", "None"), ("top", "Top to Bottom")],
            "none",
            "Cycles scans rows from top to bottom. This affects shutter blur, not the animation motion-vector pass.",
        ),
        _condition(
            _float(
                "rolling_duration",
                "Duration",
                0.1,
                0,
                1,
                "Zero gives a scan without exposure blur per row. One gives a global shutter; intermediate values mix the two.",
            ),
            "{ cycles_rolling_shutter == none }",
        ),
        _section(
            "camera_shutter_curve",
            "Shutter Curve",
            [
                _toggle(
                    "use_shutter_curve",
                    "Enable",
                    help="Weight exposure over the shutter interval using the ramp below.",
                ),
                curve,
            ],
            toggle="use_shutter_curve",
        ),
    ]

    def visible(parm, types):
        condition = "{ cycles_projection != panorama }"
        if types:
            condition += " { " + " ".join("cycles_panorama_type != " + kind for kind in types) + " }"
        parm.setConditional(hou.parmCondType.HideWhen, condition)
        return parm

    def scalar(name, label, default, minimum, maximum, help="", strict=False):
        parm = _float(name, label, default, minimum, maximum, help)
        parm.setMinIsStrict(strict)
        return parm

    lens = [
        _menu(
            "projection",
            "Projection",
            [("native", "Use Houdini"), ("panorama", "Panoramic")],
            "native",
            "Use Houdini perspective/orthographic parameters, or the Cycles panoramic lens below.",
        ),
        visible(
            _menu(
                "panorama_type",
                "Type",
                PANORAMA_TYPES,
                "fisheye_equisolid",
                "Choose how incoming ray directions map onto the rendered panorama.",
            ),
            (),
        ),
        visible(
            _float("fisheye_fov", "Field of View", 180, 0.001, 360, "Full fisheye angle in degrees, up to 360."),
            ("fisheye_equidistant", "fisheye_equisolid", "fisheye_lens_polynomial"),
        ),
        visible(
            _float("fisheye_lens", "Lens", 10.5, 0.001, 100, "Fisheye focal length in millimetres."),
            ("fisheye_equisolid",),
        ),
        visible(
            _section(
                "camera_latlong",
                "Equirectangular",
                [
                    scalar(
                        "latitude_min",
                        "Latitude Min",
                        -90,
                        -90,
                        90,
                        "Lower limit of latitude in degrees, measured from the equator.",
                    ),
                    scalar(
                        "latitude_max",
                        "Latitude Max",
                        90,
                        -90,
                        90,
                        "Upper limit of latitude in degrees, measured from the equator.",
                    ),
                    scalar(
                        "longitude_min",
                        "Longitude Min",
                        -180,
                        -180,
                        180,
                        "Lower limit of longitude in degrees around the camera.",
                    ),
                    scalar(
                        "longitude_max",
                        "Longitude Max",
                        180,
                        -180,
                        180,
                        "Upper limit of longitude in degrees around the camera.",
                    ),
                ],
                expanded=True,
            ),
            ("equirectangular",),
        ),
        visible(
            _section(
                "camera_polynomial",
                "Polynomial",
                [
                    scalar(
                        "fisheye_k" + str(i),
                        "K" + str(i),
                        default,
                        -1,
                        1,
                        "Angular polynomial coefficient in radians for radius in millimetres.",
                    )
                    for i, default in enumerate(POLYNOMIAL_DEFAULTS)
                ],
                expanded=True,
            ),
            ("fisheye_lens_polynomial",),
        ),
        visible(
            _section(
                "camera_cylindrical",
                "Central Cylindrical",
                [
                    scalar(
                        "cylindrical_u_min",
                        "Longitude Min",
                        -180,
                        -180,
                        180,
                        "Lower limit of longitude in degrees around the cylinder.",
                    ),
                    scalar(
                        "cylindrical_u_max",
                        "Longitude Max",
                        180,
                        -180,
                        180,
                        "Upper limit of longitude in degrees around the cylinder.",
                    ),
                    scalar(
                        "cylindrical_v_min",
                        "Height Min",
                        -1,
                        -10,
                        10,
                        "Lower limit of height on the projection cylinder, in the same units as Radius.",
                    ),
                    scalar(
                        "cylindrical_v_max",
                        "Height Max",
                        1,
                        -10,
                        10,
                        "Upper limit of height on the projection cylinder, in the same units as Radius.",
                    ),
                    _float(
                        "cylindrical_radius",
                        "Radius",
                        1,
                        0.00001,
                        10,
                        "Height and radius use the same units; their ratio sets the vertical projection.",
                    ),
                ],
                expanded=True,
            ),
            ("central_cylindrical",),
        ),
        visible(
            _section(
                "camera_sensor",
                "Sensor",
                [
                    _menu(
                        "sensor_fit",
                        "Fit",
                        [("auto", "Auto"), ("horizontal", "Horizontal"), ("vertical", "Vertical")],
                        "auto",
                        "Auto chooses the fitting axis from output resolution and pixel aspect, using the sensor width as its size.",
                    ),
                    _condition(
                        _float("sensor_width", "Width", 36, 0.001, 100, "Sensor width in millimetres."),
                        "{ cycles_sensor_fit == vertical }",
                    ),
                    _condition(
                        _float("sensor_height", "Height", 24, 0.001, 100, "Sensor height in millimetres."),
                        "{ cycles_sensor_fit != vertical }",
                    ),
                ],
                expanded=True,
            ),
            ("fisheye_equisolid", "fisheye_lens_polynomial"),
        ),
    ]
    dof_panel = _section("camera_dof", "Depth of Field", controls, toggle="dof", expanded=True)
    dof_panel.setTags(dict(dof_panel.tags(), cycles_dof_help_version="1"))
    tab = hou.FolderParmTemplate(
        "cycles_camera",
        "Cycles",
        [
            _section("camera_lens", "Lens", lens, expanded=True),
            dof_panel,
            _section("camera_sampling", "Motion Blur", sampling, expanded=True),
        ],
        folder_type=hou.folderType.Tabs,
        tags={"cycles_camera_properties_version": "6"},
    )

    # Houdini renames adjacent tab folders to the native switcher's names
    # (for example stdswitcher3_4). The ownership tag survives that rename.
    def owned_tab(entries, prefix=()):
        for index, parm in enumerate(entries):
            if not isinstance(parm, hou.FolderParmTemplate):
                continue
            indices = prefix + (index,)
            if parm.tags().get("cycles_camera_properties_version"):
                return indices
            found = owned_tab(parm.parmTemplates(), indices)
            if found is not None:
                return found
        return None

    owner = owned_tab(group.entries())
    if owner is not None:
        group.replace(owner, tab)
    elif group.find("cycles_camera"):
        group.replace("cycles_camera", tab)
    else:
        if existing:
            for parm in controls:
                if group.find(parm.name()):
                    group.remove(parm.name())
        group = install_tabs(group, tab)
    node.setParmTemplateGroup(group)
    if new_curve:
        node.parm("cycles_shutter_curve").revertToRampDefaults()
    if not existing:
        for custom, native in (("cycles_focus_distance", "focus"), ("cycles_fstop", "fstop")):
            node.parm(custom).setExpression('ch("' + native + '")', hou.exprLanguage.Hscript)
    return True


def panorama_source(value):
    """Active projection inputs, in engine units; shared by SOHO and signatures."""
    if value("cycles_projection", "native") != "panorama":
        return None
    kind = value("cycles_panorama_type", "fisheye_equisolid")
    p = dict(type=kind)
    if kind == "equirectangular":
        for name, default in (
            ("latitude_min", -90),
            ("latitude_max", 90),
            ("longitude_min", -180),
            ("longitude_max", 180),
        ):
            p[name] = math.radians(value("cycles_" + name, default))
    elif kind == "central_cylindrical":
        p["cylindrical_u_min"] = math.radians(value("cycles_cylindrical_u_min", -180))
        p["cylindrical_u_max"] = math.radians(value("cycles_cylindrical_u_max", 180))
        radius = value("cycles_cylindrical_radius", 1)
        divisor = radius if math.isfinite(radius) and radius > 0 else math.nan
        p["cylindrical_v_min"] = value("cycles_cylindrical_v_min", -1) / divisor
        p["cylindrical_v_max"] = value("cycles_cylindrical_v_max", 1) / divisor
    elif kind in ("fisheye_equidistant", "fisheye_equisolid", "fisheye_lens_polynomial"):
        p["fisheye_fov"] = math.radians(value("cycles_fisheye_fov", 180))
        if kind == "fisheye_equisolid":
            p["fisheye_lens"] = value("cycles_fisheye_lens", 10.5)
        if kind == "fisheye_lens_polynomial":
            p["polynomial"] = [value("cycles_fisheye_k" + str(i), v) for i, v in enumerate(POLYNOMIAL_DEFAULTS)]
        if kind != "fisheye_equidistant":
            p["sensor_fit"] = value("cycles_sensor_fit", "auto")
            p["sensor_height"] = value("cycles_sensor_height", 24)
            p["sensor_width"] = value("cycles_sensor_width", 36)
    return p


DOF_CONTINUOUS = ("aperture_size", "focus_distance", "aperture_rotation", "aperture_ratio")
_FLOAT_MAX = 3.4028234663852886e38


def _dof_numeric_bounds(values, near_clip):
    """Bound all interpolated samples before entering the strict native API."""
    radius = max(v["aperture_size"] for v in values)
    ratio = min(v["aperture_ratio"] for v in values)
    focus = max(min(v["focus_distance"] for v in values), 1e-5)
    inverse = 1 / ratio
    if not math.isfinite(inverse) or inverse > _FLOAT_MAX:
        raise CameraDofSourceError(
            "Depth-of-field inverse aperture ratio exceeds the engine numeric range", "aperture_ratio"
        )
    radius *= max(1.0, inverse)
    slope = radius / focus
    extension = radius + max(near_clip, slope * near_clip)
    if any(not math.isfinite(v) or v > _FLOAT_MAX for v in (radius, slope, extension)):
        raise CameraDofSourceError(
            "Depth-of-field ray origin or conservative shutter bound exceeds the engine numeric range",
            "aperture_size focus_distance aperture_ratio",
        )


def evaluate(value, mode="camera", unit_length=None, near_clip=0.01):
    """value(name, default) reads either HOM or evaluated SOHO camera values."""
    if mode not in ("camera", "off", "on"):
        raise ValueError("Unknown depth of field mode: " + mode)
    if mode == "off" or (mode == "camera" and not value("cycles_dof", False)):
        return dict(
            aperture_size=0.0, focus_distance=10.0, aperture_blades=0, aperture_rotation=0.0, aperture_ratio=1.0
        )
    focus = float(value("cycles_focus_distance", value("focus", 10.0)))
    fstop = float(value("cycles_fstop", value("fstop", 5.6)))
    focal = float(value("focal", 50.0))
    units = value("focalunits", "mm")
    scales = {
        "m": 1.0,
        "km": 1000.0,
        "cm": 0.01,
        "mm": 0.001,
        "nm": 1e-9,
        "pm": 1e-12,
        "yd": 0.9144,
        "ft": 0.3048,
        "in": 0.0254,
        "cubit": 0.462788,
    }
    if units not in scales:
        raise CameraDofSourceError("Unsupported camera focal units: " + str(units), "focalunits")
    unit_length = float(hou.hscriptExpression("$UNITLENGTH")) if unit_length is None else unit_length
    from ..core import image_iris

    iris = image_iris.source(value)
    # The image defines the pupil. Do not validate or capture an unused blade
    # parameter: saved values 1/2 must not disable otherwise valid image DOF.
    raw_blades = 0.0 if iris is not None else float(value("cycles_aperture_blades", 0))
    rotation = float(value("cycles_aperture_rotation", 0.0))
    if not math.isfinite(raw_blades) or not raw_blades.is_integer():
        raise CameraDofSourceError("Aperture blades require a finite integer", "aperture_blades")
    blades = int(raw_blades)
    ratio = float(value("cycles_aperture_ratio", 1.0))
    for name, value_ in (
        ("focus_distance", focus),
        ("fstop", fstop),
        ("focal", focal),
        ("scene_units", unit_length),
        ("aperture_ratio", ratio),
    ):
        if not math.isfinite(value_) or value_ <= 0:
            raise CameraDofSourceError("Depth of field requires finite positive " + name, name)
    if not math.isfinite(rotation):
        raise CameraDofSourceError("Aperture rotation must be finite", "aperture_rotation")
    if blades not in (0, *range(3, 33)):
        raise CameraDofSourceError("Aperture blades must be zero or between 3 and 32", "aperture_blades")
    result = dict(
        aperture_size=focal * scales[units] / unit_length / (2 * fstop),
        focus_distance=focus,
        aperture_blades=blades,
        aperture_rotation=math.radians(rotation),
        aperture_ratio=ratio,
    )
    for name in DOF_CONTINUOUS:
        v = result[name]
        if (
            not math.isfinite(v)
            or abs(v) > _FLOAT_MAX
            or (name in ("focus_distance", "aperture_ratio") and v < 1.401298464324817e-45)
        ):
            raise CameraDofSourceError("Depth-of-field " + name + " exceeds the engine numeric range", name)
        result[name] = struct.unpack("<f", struct.pack("<f", v))[0]
    # Clipping is an essential projection input, resolved separately. It cannot
    # turn an otherwise unused DOF control into the primary camera failure.
    near_clip = near_clip if math.isfinite(near_clip) and near_clip > 0 else 0.01
    _dof_numeric_bounds([result], near_clip)
    if iris is not None:
        result["_image_iris_source"] = iris
    return result


def resolve_lens(sample, now, times, source, near_clip=0.01):
    """Recover known source optics before strict engine validation; preserve other motion."""
    from ..core import image_iris

    def diagnosed(value, code, message, action, attribute):
        return image_iris.resolve(
            dict(
                **value,
                motion=[],
                source_diagnostics=[
                    dict(
                        code=code,
                        source=source,
                        resource="camera:" + source,
                        message=message,
                        action=action,
                        severity="warning",
                        attribute=attribute,
                    )
                ],
            )
        )

    try:
        requested = sample(now)
    except CameraDofSourceError as error:
        return diagnosed(
            evaluate(lambda name, default: default, mode="off"),
            "camera.invalid_dof",
            str(error),
            "Rendering with a pinhole camera; retaining valid camera and object motion",
            error.attribute,
        )
    try:
        values = [sample(t) for t in times]
        _dof_numeric_bounds([requested] + values, near_clip if math.isfinite(near_clip) and near_clip > 0 else 0.01)
    except CameraDofSourceError as error:
        return diagnosed(
            requested,
            "camera.shutter_dof",
            str(error) + " during the shutter",
            "Using requested-frame depth of field; retaining supported projection and transform motion",
            error.attribute,
        )
    if len(values) > 1 and (len(values) < 3 or len(values) > 129 or len(values) % 2 != 1):
        raise ValueError("Depth-of-field source requires odd 3..129 complete shutter samples")
    motion = [{name: value[name] for name in DOF_CONTINUOUS} for value in values] if len(values) > 1 else []
    if not any(value[name] != requested[name] for value in values for name in DOF_CONTINUOUS):
        motion = []
    result = dict(**requested, motion=motion, source_diagnostics=[])
    if any(value["aperture_blades"] != requested["aperture_blades"] for value in values):
        result["source_diagnostics"].append(
            dict(
                code="camera.shutter_dof",
                source=source,
                resource="camera:" + source,
                message="Aperture blade count is discrete and changes during the shutter",
                action="Holding the requested-frame blade count; retaining valid continuous optics and independent motion",
                severity="warning",
                attribute="aperture_blades",
            )
        )
    return image_iris.resolve(result)


def configuration(rop, now, times=None):
    from ..rop.rop_controls import value as rop_value

    mode = rop_value(rop, "dof_mode", now)
    camera = hou.node(rop.parm("camera").evalAtTime(now))
    near_clip = camera.parm("near").evalAtTime(now) if camera and camera.parm("near") else 0.01

    def sample(t):
        return evaluate(
            lambda name, default: camera.parm(name).evalAtTime(t) if camera and camera.parm(name) else default,
            mode,
            near_clip=near_clip,
        )

    return resolve_lens(
        sample, now, times or [now], camera.path() if camera else rop.parm("camera").evalAtTime(now), near_clip
    )


class CameraSamplingSourceError(ValueError):
    pass


def sampling_source(value, ramp):
    """Freeze only contributing host parameters; no geometry or shader evaluation."""
    result = dict(rolling_shutter=value("cycles_rolling_shutter", "none"), rolling_duration=0.1, shutter_curve=[])
    if result["rolling_shutter"] == "top":
        result["rolling_duration"] = value("cycles_rolling_duration", 0.1)
    if value("cycles_use_shutter_curve", False):
        try:
            shape = ramp()
            if shape is None or shape.isColor():
                raise CameraSamplingSourceError("A scalar shutter exposure ramp is required")
            result["shutter_curve"] = [shape.lookup(i / 255) for i in range(256)]
        except (CameraSamplingSourceError, hou.OperationFailed) as error:
            result["curve_error"] = str(error)
    return result


def resolve_sampling(sample, now, times, source):
    """Resolve rolling and exposure independently; optional source errors stay local."""
    nominal = sample(now)
    values = [sample(t) for t in times if t != now]
    result = dict(rolling_shutter=False, rolling_duration=0.1, shutter_curve=[], source_diagnostics=[])

    def issue(code, message, action, attribute):
        result["source_diagnostics"].append(
            dict(
                code=code,
                source=source,
                resource="camera:" + source,
                message=message,
                action=action,
                severity="warning",
                attribute=attribute,
            )
        )

    def rolling(value):
        mode = value.get("rolling_shutter", "none")
        if mode not in ("none", "top"):
            raise CameraSamplingSourceError("Unsupported rolling shutter direction: " + str(mode))
        duration = float(value.get("rolling_duration", 0.1)) if mode == "top" else 0.1
        if not math.isfinite(duration) or not 0 <= duration <= 1:
            raise CameraSamplingSourceError("Rolling shutter duration must be finite and between zero and one")
        return mode == "top", duration

    def curve(value):
        if value.get("curve_error"):
            raise CameraSamplingSourceError(value["curve_error"])
        weights = value.get("shutter_curve", [])
        if not weights:
            return []
        if not 2 <= len(weights) <= 256 or any(not math.isfinite(v) or v < 0 for v in weights) or max(weights) <= 0:
            raise CameraSamplingSourceError(
                "Exposure requires 2..256 finite nonnegative weights and a positive exposure"
            )
        peak = max(weights)
        normalized = [v / peak for v in weights]
        return [] if all(v == 1 for v in normalized) else normalized

    def same(a, b):
        return len(a) == len(b) and all(math.isclose(x, y, rel_tol=1e-6, abs_tol=1e-6) for x, y in zip(a, b))

    for name, resolve, invalid, animated, attribute, action in (
        (
            "rolling",
            rolling,
            "camera.invalid_rolling_shutter",
            "camera.shutter_rolling",
            "rolling_shutter rolling_duration",
            "Rendering without rolling shutter; retaining exposure shape and geometry/camera motion",
        ),
        (
            "curve",
            curve,
            "camera.invalid_shutter_curve",
            "camera.shutter_curve",
            "shutter_curve",
            "Using uniform exposure; retaining rolling shutter and geometry/camera motion",
        ),
    ):
        # Relevance follows the resolved exposure, not an off-frame scan value
        # that may itself be held/recovered at the requested frame.
        if name == "curve" and result["rolling_shutter"] and result["rolling_duration"] == 0:
            continue
        try:
            requested = resolve(nominal)
        except CameraSamplingSourceError as error:
            issue(invalid, str(error), action, attribute)
            continue
        if name == "rolling":
            result["rolling_shutter"], result["rolling_duration"] = requested
        else:
            result["shutter_curve"] = requested
        try:
            if any(not same(resolve(value), requested) for value in values):
                raise CameraSamplingSourceError("Animated " + attribute + " cannot vary within one exposure")
        except CameraSamplingSourceError as error:
            issue(
                animated,
                str(error),
                "Using requested-frame " + attribute + "; retaining independent exposure and motion settings",
                attribute,
            )
    return result


def sampling_configuration(rop, now, motion):
    if not motion["enabled"]:
        return dict(rolling_shutter=False, rolling_duration=0.1, shutter_curve=[], source_diagnostics=[])
    from .native_scene import motion_times

    camera = hou.node(rop.parm("camera").evalAtTime(now))

    def sample(t):
        return sampling_source(
            lambda name, default: camera.parm(name).evalAtTime(t) if camera and camera.parm(name) else default,
            lambda: camera.parm("cycles_shutter_curve").evalAtTime(t)
            if camera and camera.parm("cycles_shutter_curve")
            else None,
        )

    return resolve_sampling(
        sample, now, motion_times(motion, now), camera.path() if camera else rop.parm("camera").evalAtTime(now)
    )
