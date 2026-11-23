# SPDX-License-Identifier: Apache-2.0
"""Owned ramp controls baked into Blender-equivalent 257-entry tables."""

import math
import struct


class RampSourceError(ValueError):
    """Known authored-control failure, distinct from malformed owned data."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def unavailable_ramp(identity, error, color, factor=None):
    """A bounded typed fault keeps failures local to this node's consumers."""
    if not isinstance(error, RampSourceError) or error.code not in range(1, 8):
        raise ValueError("Invalid ramp source fault ownership")
    key = identity + "/native"
    source = [0x43525331, 3, 0, error.code, int(color), 0, 0, 0]
    node = dict(id=key, definition="cycles/owned_ramp/1", parameters=dict(source=source, factor=0.5))
    links = [] if factor is None else [{"from": factor.copy(), "to": dict(node=key, socket="factor")}]
    return [node], links, dict(node=key, socket="color" if color else "value")


def _float32(value):
    try:
        result = struct.unpack("f", struct.pack("f", float(value)))[0]
    except (OverflowError, ValueError, TypeError) as error:
        raise RampSourceError(2, "Ramp keys and values require finite float32 components") from error
    if not math.isfinite(result):
        raise RampSourceError(2, "Ramp keys and values require finite float32 components")
    return result


BASES = ("constant", "linear", "catmull_rom", "monotone_cubic", "bezier", "bspline", "hermite")
RGB_INTERPOLATIONS = ("constant", "linear", "ease", "b_spline", "cardinal")


def lower_ramp(
    identity,
    bases,
    keys,
    values,
    color,
    factor=None,
    factor_value=0.5,
    color_space="rgb",
    hue_interpolation="near",
    force_owned=False,
    linear_stops=False,
    rgb_interpolation=None,
):
    """Return owned controls for the host baker and a stock Cycles ramp."""
    if not 1 <= len(keys) <= 32 or len(keys) != len(values) or len(keys) != len(bases):
        raise RampSourceError(1, "Cycles ramps require 1..32 matching keys, values and bases")
    hue = color and color_space in ("hsv", "hsl")
    rgb = rgb_interpolation is not None
    if rgb:
        if color_space != "rgb":
            raise ValueError("Global RGB interpolation requires owned RGB controls")
        if rgb_interpolation not in RGB_INTERPOLATIONS:
            raise RampSourceError(4, "Unsupported Color Ramp Interpolation")
        bases = ("linear",) * len(keys)
    if linear_stops:
        if color or color_space != "rgb":
            raise ValueError("Linear scalar stop ownership requires scalar RGB controls")
        bases = ("linear",) * len(keys)
    if hue:
        # Blender's hue spaces use linear stop blending; dormant native basis
        # controls do not contribute and must not invalidate the material.
        bases = ("linear",) * len(keys)
        if hue_interpolation not in ("near", "far", "cw", "ccw"):
            raise RampSourceError(6, "Unsupported Color Ramp Hue Interpolation")
    if any(basis not in BASES for basis in bases):
        raise RampSourceError(4, "Unknown native ramp interpolation")
    if color_space not in ("rgb", "hsv", "hsl", "lab") or (not color and color_space != "rgb"):
        raise RampSourceError(5, "Unsupported native ramp color space")
    positions = tuple(_float32(key) for key in keys)
    if any(key < 0 or key > 1 for key in positions) or any(
        a > b if hue or linear_stops or rgb else a >= b for a, b in zip(positions, positions[1:])
    ):
        raise RampSourceError(
            3,
            "Hue ramp keys must be ordered in [0,1]"
            if hue
            else "Ramp keys must be strictly increasing in [0,1] at float32 precision",
        )
    if color:
        try:
            samples = tuple(tuple(_float32(component) for component in value) for value in values)
        except TypeError as error:
            raise RampSourceError(7, "Color ramps require three components per value") from error
        if any(len(value) != 3 for value in samples):
            raise RampSourceError(7, "Color ramps require three components per value")
    else:
        samples = tuple(_float32(value) for value in values)

    # Transport authored controls, never host callbacks or pre-sampled UI
    # buffers. Worker preparation uses only the certified native math API.
    def bits(v):
        return struct.unpack("i", struct.pack("f", v))[0]

    source = [
        0x43525331,
        5 if rgb else 4 if linear_stops else 2 if hue else 1,
        len(keys),
        ("rgb", "hsv", "lab", "hsl").index(color_space),
        int(color),
        RGB_INTERPOLATIONS.index(rgb_interpolation)
        if rgb
        else ("near", "far", "cw", "ccw").index(hue_interpolation)
        if hue
        else 0,
        0,
        0,
    ]
    for basis, key, sample in zip(bases, positions, samples):
        source.extend((BASES.index(basis), bits(key), *(bits(v) for v in (sample if color else (sample,) * 3))))
    key = identity + "/native"
    node = dict(id=key, definition="cycles/owned_ramp/1", parameters=dict(source=source, factor=_float32(factor_value)))
    links = [] if factor is None else [{"from": factor.copy(), "to": dict(node=key, socket="factor")}]
    return [node], links, dict(node=key, socket="color" if color else "value")


class ColorRampControls:
    """Resolve the Color and Alpha ramp controls through Houdini references.

    Color stops are a native RGB ramp and Alpha stops an independent native
    float ramp, each possibly promoted. Material ramps promoted before the
    inline ramps keep their hidden RGBA multiparm bundle; it still renders
    with its original shared-stop semantics until it is converted.
    """

    def __init__(self, target, evaluation=None):
        if isinstance(target, ColorRampControls):
            self.origin = target.origin
        elif hasattr(target, "parm"):
            self.origin = target.parm("ramp")
        else:
            self.origin = target
        if self.origin is None:
            raise ValueError("Missing Color Ramp controls")
        self.evaluation = evaluation or getattr(target, "evaluation", None)
        self.used = getattr(target, "used", [])

    def storage(self):
        import hou

        target = self.origin.getReferencedParm()
        tags = target.parmTemplate().tags()
        if (
            tags.get("cycles_rgba_ramp") == "1"
            and isinstance(target.parmTemplate(), hou.FolderParmTemplate)
            and target.parmTemplate().folderType() == hou.folderType.MultiparmBlock
        ):
            return target, True
        if (
            target.parmTemplate().type() == hou.parmTemplateType.Ramp
            and target.parmTemplate().parmType() == hou.rampParmType.Color
        ):
            return target, False
        if self.origin.node().type().name() == "cycles::color_ramp::1" and self.origin.name() == "ramp":
            if target.node().type().name() == "cycles::color_ramp::1" and target.name() == "ramp":
                return target, False
            if target != self.origin:
                raise RampSourceError(7, "The referenced ramp must provide native Color Ramp controls")
            return self.origin, False
        raise RampSourceError(7, "The promoted Color Ramp controls are missing or incompatible")

    def legacy(self):
        """True for a material bundle promoted with the retired RGBA storage."""
        return self.storage()[1]

    def alpha_storage(self):
        import hou

        node = self.origin.node()
        origin = (
            node.parm("alpha_ramp")
            if self.origin.name() == "ramp" and node.type().name() == "cycles::color_ramp::1"
            else None
        )
        if origin is None:
            raise RampSourceError(7, "The Alpha ramp controls are missing")
        target = origin.getReferencedParm()
        template = target.parmTemplate()
        if template.type() == hou.parmTemplateType.Ramp and template.parmType() == hou.rampParmType.Float:
            return target
        raise RampSourceError(7, "The referenced Alpha ramp must be a float ramp")

    def alpha_parm(self, name):
        count = self.alpha_storage()
        if name == "alpha_ramp":
            return count
        identifier = count.name() + name[len("alpha_ramp") :]
        return self.evaluation.parameter(count.node(), identifier) if self.evaluation else count.node().parm(identifier)

    def parm(self, name):
        if name in ("ramp_color_space", "ramp_hue_interpolation", "ramp_interpolation"):
            # Global menus stay readable for Alpha when the Color storage is
            # unusable; only a retired RGBA bundle owns its own menus.
            try:
                count, promoted = self.storage()
            except RampSourceError:
                count, promoted = None, False
            owner, identifier = (count.node(), count.name() + name[4:]) if promoted else (self.origin.node(), name)
        else:
            count, promoted = self.storage()
            if name == "ramp":
                return count
            owner, identifier = count.node(), count.name() + name[4:]
        parameter = self.evaluation.parameter(owner, identifier) if self.evaluation else owner.parm(identifier)
        return parameter

    def parmTuple(self, name):
        count, _ = self.storage()
        identifier = count.name() + name[4:]
        parameters = (
            self.evaluation.parameters(count.node(), identifier)
            if self.evaluation
            else count.node().parmTuple(identifier)
        )
        return parameters

    def path(self):
        count, _ = self.storage()
        return count.path()

    def evalParm(self, name):
        p = self.parm(name)
        if p is None:
            raise RampSourceError(7, "Missing Color Ramp channel: " + name)
        return p.eval()

    def title(self):
        return self.path()


def node_controls(node, now=None, evaluation=None, read_hue=True, read_color=True):
    """Read only owned, animatable controls on Houdini's host thread."""
    import hou

    now = hou.time() if now is None else now
    controls = ColorRampControls(node, evaluation)

    def scalar(p):
        if p is None:
            raise RampSourceError(7, "A Color Ramp channel is missing")
        controls.used.append(p)
        return evaluation.parm(p, now, controls.origin.node()) if evaluation else p.evalAtTime(now)

    count_parm = controls.parm("ramp")
    controls.used.append(count_parm)
    count = (
        evaluation.count(count_parm, now, controls.origin.node())
        if evaluation
        else count_parm.evalAsIntAtFrame(hou.timeToFrame(now))
        if count_parm.parmTemplate().type() == hou.parmTemplateType.Ramp
        else scalar(count_parm)
    )
    if not isinstance(count, (int, float)) or int(count) != count or not 1 <= count <= 32:
        raise RampSourceError(1, "Cycles ramps require 1..32 matching keys, values and bases")
    count = int(count)
    space = str(scalar(controls.parm("ramp_color_space"))).lower()
    interpolation = str(scalar(controls.parm("ramp_interpolation"))).lower() if space == "rgb" else "native"
    bases = (
        tuple(
            BASES[int(p)]
            if isinstance(p, (int, float)) and math.isfinite(p) and int(p) == p and 0 <= p < len(BASES)
            else "unsupported"
            for p in (scalar(controls.parm(f"ramp{i}interp")) for i in range(1, count + 1))
        )
        if interpolation == "native" and space in ("rgb", "lab")
        else ("linear",) * count
    )
    positions = [controls.parm(f"ramp{i}pos") for i in range(1, count + 1)]
    keys = tuple(scalar(p) for p in positions)
    rounded = tuple(_float32(key) for key in keys)
    if any(not 0 <= key <= 1 for key in rounded) or any(
        a > b if space in ("hsv", "hsl") or interpolation != "native" else a >= b for a, b in zip(rounded, rounded[1:])
    ):
        if evaluation:
            evaluation.reject_if_overridden(
                [controls.parm("ramp"), *positions], "Color Ramp positions must be ordered in [0,1]"
            )
        raise RampSourceError(3, "Color Ramp positions must be ordered in [0,1]")
    values = []
    for i in range(1, count + 1):
        if not read_color:
            values.append((0.0, 0.0, 0.0))
            continue
        channels = controls.parmTuple(f"ramp{i}c")
        if channels is None or len(channels) < 3:
            raise RampSourceError(7, "A Color Ramp color tuple is missing or incompatible")
        values.append(tuple(scalar(p) for p in tuple(channels)[:3]))
    return dict(
        bases=bases,
        keys=keys,
        values=values,
        color_space=space,
        hue_interpolation=str(scalar(controls.parm("ramp_hue_interpolation"))).lower()
        if read_hue and space in ("hsv", "hsl")
        else "near",
        rgb_interpolation=None if interpolation == "native" else interpolation,
    )


def alpha_controls(node, now=None, evaluation=None):
    """Read the independent Alpha ramp.

    Native stop bases apply, except that RGB mode's Blender interpolation
    (Linear/Constant/Ease/B-Spline/Cardinal) applies to Alpha as to Color.
    All-linear stops in HSV/HSL keep the coincident-stop tolerance that
    legacy shared alpha had there. Color stops are never read.
    """
    import hou

    now = hou.time() if now is None else now
    controls = ColorRampControls(node, evaluation)

    def scalar(p):
        if p is None:
            raise RampSourceError(7, "An Alpha ramp channel is missing")
        controls.used.append(p)
        return evaluation.parm(p, now, controls.origin.node()) if evaluation else p.evalAtTime(now)

    space = str(scalar(controls.parm("ramp_color_space"))).lower()
    if space not in ("rgb", "hsv", "hsl", "lab"):
        raise RampSourceError(5, "Unsupported Color Mode")
    interpolation = str(scalar(controls.parm("ramp_interpolation"))).lower() if space == "rgb" else "native"
    if interpolation != "native" and interpolation not in RGB_INTERPOLATIONS:
        raise RampSourceError(4, "Unsupported Color Ramp Interpolation")
    count_parm = controls.alpha_parm("alpha_ramp")
    controls.used.append(count_parm)
    count = (
        evaluation.count(count_parm, now, controls.origin.node())
        if evaluation
        else count_parm.evalAsIntAtFrame(hou.timeToFrame(now))
    )
    if not isinstance(count, (int, float)) or int(count) != count or not 1 <= count <= 32:
        raise RampSourceError(1, "Cycles ramps require 1..32 matching keys, values and bases")
    count = int(count)
    positions = [controls.alpha_parm(f"alpha_ramp{i}pos") for i in range(1, count + 1)]
    keys = tuple(scalar(p) for p in positions)
    values = tuple(_float32(scalar(controls.alpha_parm(f"alpha_ramp{i}value"))) for i in range(1, count + 1))
    linear = False
    if interpolation == "native":
        bases = tuple(
            BASES[int(p)]
            if isinstance(p, (int, float)) and math.isfinite(p) and int(p) == p and 0 <= p < len(BASES)
            else "unsupported"
            for p in (scalar(controls.alpha_parm(f"alpha_ramp{i}interp")) for i in range(1, count + 1))
        )
        linear = space in ("hsv", "hsl") and all(basis == "linear" for basis in bases)
    else:
        bases = ("linear",) * count
    rounded = tuple(_float32(key) for key in keys)
    if any(not 0 <= key <= 1 for key in rounded) or any(
        a > b if linear or interpolation != "native" else a >= b for a, b in zip(rounded, rounded[1:])
    ):
        if evaluation:
            evaluation.reject_if_overridden([count_parm, *positions], "Alpha ramp positions must be ordered in [0,1]")
        raise RampSourceError(3, "Alpha ramp positions must be ordered in [0,1]")
    return dict(
        bases=bases,
        keys=keys,
        values=values,
        color_space="rgb",
        hue_interpolation="near",
        linear_stops=linear,
        rgb_interpolation=None if interpolation == "native" else interpolation,
    )


def lower_color_output(node, identity, output, now=None, evaluation=None, factor=None, factor_value=0.5):
    """Color and Alpha have independent resources, faults and dependencies."""
    import hou

    now = hou.time() if now is None else now
    alpha = output == "alpha"
    if output not in ("color", "alpha"):
        raise ValueError("Invalid Color Ramp output")
    channel = ColorRampControls(node, evaluation)
    try:
        legacy = channel.legacy()
    except (RampSourceError, hou.Error):
        legacy = False

    def owner():
        if alpha and not legacy:
            try:
                return channel.alpha_storage()
            except (RampSourceError, hou.Error):
                pass
        return channel.origin.getReferencedParm()

    try:
        if alpha and not legacy:
            controls = alpha_controls(channel, now, evaluation)
            color = False
        else:
            controls = node_controls(channel, now, evaluation, not alpha, not alpha)
            color = not alpha
        if alpha and legacy:
            # Retired promoted RGBA bundle: Alpha shares the Color stops.
            if controls["color_space"] not in ("rgb", "hsv", "hsl", "lab"):
                raise RampSourceError(5, "Unsupported Color Mode")

            def scalar(p):
                channel.used.append(p)
                return evaluation.parm(p, now, channel.origin.node()) if evaluation else p.evalAtTime(now)

            values = []
            for i in range(1, len(controls["keys"]) + 1):
                p = channel.parm(f"ramp{i}ca")
                if p is None:
                    raise RampSourceError(7, "An RGBA ramp Alpha channel is missing")
                values.append(_float32(scalar(p)))
            linear = controls["color_space"] in ("hsv", "hsl")
            controls.update(values=values, color_space="rgb", hue_interpolation="near", linear_stops=linear)
        result = lower_ramp(
            identity + ("/alpha" if alpha else ""),
            color=color,
            factor=factor,
            factor_value=factor_value,
            force_owned=True,
            **controls,
        )
        if alpha:
            result[2]["socket"] = "value"
        # Imported ColorBand tables are Blender's own RGBA bake. Preserve its
        # stock interpolation flag, including CONSTANT in HSV/HSL, until this
        # output's native controls are edited.
        saved = node.userData("cycles_blender_ramp") if hasattr(node, "userData") else None
        if saved:
            import json

            imported = json.loads(saved)
            index = 1 if alpha else 0
            source = result[0][0]["parameters"]["source"]
            if source == imported["sources"][index]:

                def bits(v):
                    return struct.unpack("i", struct.pack("f", float(v)))[0]

                result[0][0]["parameters"]["source"] = [
                    0x43525331,
                    8,
                    257,
                    0,
                    int(not alpha),
                    int(imported["interpolate"]),
                    0,
                    0,
                    *(bits(v) for row in imported["table"] for v in ((row[3],) * 3 if alpha else row[:3])),
                ]
        return result
    except RampSourceError as error:
        # A known authored source fault belongs only to this output. It is not
        # evidence that independent, valid override fields should be removed.
        return unavailable_ramp(identity + ("/alpha" if alpha else ""), error, not alpha, factor)
    except ValueError as error:
        if evaluation:
            evaluation.bundle_error(owner(), channel.used, error)
        if not isinstance(error, RampSourceError):
            error = RampSourceError(7, str(error))
        return unavailable_ramp(identity + ("/alpha" if alpha else ""), error, not alpha, factor)
    except hou.Error as error:
        if evaluation:
            evaluation.bundle_error(owner(), channel.used, error)
        return unavailable_ramp(
            identity + ("/alpha" if alpha else ""),
            RampSourceError(7, "Unusable Color Ramp channel: " + str(error)),
            not alpha,
            factor,
        )


def preview_sources(node, now=None):
    return tuple(
        lower_color_output(node, "preview", output, now)[0][0]["parameters"]["source"] for output in ("color", "alpha")
    )
