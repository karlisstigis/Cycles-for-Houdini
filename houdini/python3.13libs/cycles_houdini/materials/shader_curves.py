# SPDX-License-Identifier: Apache-2.0
"""Inline native curve controls, host-baked to stock 257-entry Cycles curves."""

import math

KINDS = {
    "float_curve": ("float", ("value",)),
    "rgb_curves": ("rgb", ("c", "r", "g", "b")),
    "vector_curves": ("vector", ("x", "y", "z")),
}
LABELS = {"value": "Curve", "c": "Combined", "r": "R", "g": "G", "b": "B", "x": "X", "y": "Y", "z": "Z"}
_HOU_BASES = ("Constant", "Linear", "CatmullRom", "MonotoneCubic", "Bezier", "BSpline", "Hermite")


def kind_of(node):
    return node.type().name().split("::")[1]


def ramp_controls(node, channel, now, evaluation=None):
    """Owned controls of one channel ramp, following promotion references."""
    import hou
    from .ramps import BASES, RampSourceError

    name = "curve_" + channel
    if evaluation:
        return evaluation.ramp(node, now, name)
    parm = node.parm(name)
    if parm is None:
        raise RampSourceError(7, "Missing curve ramp: " + name)
    target = parm.getReferencedParm()
    template = target.parmTemplate()
    if template.type() != hou.parmTemplateType.Ramp or template.parmType() != hou.rampParmType.Float:
        raise RampSourceError(7, "The referenced curve ramp must be a native float ramp")
    try:
        ramp = parm.evalAsRampAtFrame(hou.timeToFrame(now))
    except hou.OperationFailed as error:
        raise RampSourceError(7, "Unusable curve ramp: " + str(error)) from error
    tokens = {getattr(hou.rampBasis, b): t for b, t in zip(_HOU_BASES, BASES)}
    return dict(
        bases=tuple(tokens.get(basis, "unsupported") for basis in ramp.basis()),
        keys=ramp.keys(),
        values=ramp.values(),
        color=ramp.isColor(),
    )


def end_slopes(bases, keys, values):
    """d(value)/d(position) at the first and last key, from the native basis.

    Linear uses its exact chord, Constant is flat; smooth bases use a local
    cubic fit of Houdini's own ramp evaluation at the end key.
    """
    import hou
    from .ramps import BASES

    if len(keys) < 2:
        return 0.0, 0.0

    def slope(basis, a, b, at_start):
        if basis == "constant":
            return 0.0
        if basis == "linear":
            return (values[b] - values[a]) / (keys[b] - keys[a])
        ramp = hou.Ramp(
            tuple(getattr(hou.rampBasis, _HOU_BASES[BASES.index(x)]) for x in bases), tuple(keys), tuple(values)
        )
        # Least-squares cubic through samples of Houdini's own evaluation on
        # the last/first 5% of the end interval: robust to float32 lookups
        # and to bases whose control points are not on the curve.
        import numpy

        origin, direction = (keys[a], 1.0) if at_start else (keys[b], -1.0)
        offsets = numpy.linspace(0.0, (keys[b] - keys[a]) * 0.05, 17)
        samples = [ramp.lookup(origin + direction * float(o)) for o in offsets]
        fit = numpy.polynomial.Polynomial.fit(offsets, samples, 3)
        return direction * float(fit.deriv()(0.0))

    first = slope(bases[0], 0, 1, True)
    # The last interval uses the basis of its left key. A Constant interval
    # ends with a jump to the last value and is flat afterwards.
    last = slope(bases[-2], len(keys) - 2, len(keys) - 1, False)
    return first, last


def _snapshot(node, now, evaluation=None):
    """Authored UI controls used to detect edits to an imported CurveMapping."""

    def read(parm):
        return evaluation.parm(parm, now) if evaluation else parm.evalAtTime(now)

    channels = {}
    for channel in KINDS[kind_of(node)][1]:
        data = ramp_controls(node, channel, now, evaluation)
        channels[channel] = dict(
            bases=list(data["bases"]),
            keys=list(data["keys"]),
            values=list(data["values"]),
            color=data.get("color", False),
        )
    return dict(
        domain=list(
            evaluation.tuple(node.parmTuple("curve_domain"), now)
            if evaluation
            else node.parmTuple("curve_domain").evalAtTime(now)
        ),
        extension=int(read(node.parm("curve_extension"))),
        channels=channels,
    )


def lower(node, identity, now, evaluation, value, fac, factor_constant=None):
    """Transport numeric controls; host preparation creates one stock curve."""
    import json
    import struct
    import hou
    from .ramps import lower_ramp, RampSourceError, _float32
    from .material_overrides import UnsupportedOverride, _MissingChannel

    kind, channels = KINDS[kind_of(node)]
    if factor_constant is not None and math.isfinite(factor_constant) and _float32(factor_constant) == 0:
        return [], [], value

    def bits(v):
        return struct.unpack("i", struct.pack("f", _float32(v)))[0]

    key = identity + "/table"
    try:
        snapshot = _snapshot(node, now, evaluation)
        saved = node.userData("cycles_blender_curve") if hasattr(node, "userData") else None
        imported = json.loads(saved) if saved else None
        if imported and snapshot == imported["snapshot"]:
            source = imported["source"]
        else:
            start, end = map(_float32, snapshot["domain"])
            if not end > start or not math.isfinite(end - start):
                if evaluation:
                    evaluation.reject_if_overridden(
                        tuple(node.parmTuple("curve_domain")), "Curve Range requires finite increasing bounds"
                    )
                raise RampSourceError(3, "Curve Range requires finite increasing bounds")
            extrapolate = snapshot["extension"] == 0
            source = [
                0x43435231,
                2,
                ("float", "rgb", "vector").index(kind),
                int(extrapolate),
                bits(start),
                bits(end),
                0,
                len(channels),
            ]
            for channel in ("r", "g", "b", "c") if kind == "rgb" else channels:
                data = snapshot["channels"][channel]
                if data["color"]:
                    raise RampSourceError(7, "Curve ramps must be float ramps")
                lowered, _, _ = lower_ramp(key, data["bases"], data["keys"], data["values"], False)
                controls = lowered[0]["parameters"]["source"]
                first, last = end_slopes(data["bases"], data["keys"], data["values"]) if extrapolate else (0, 0)
                source.extend(
                    [
                        len(controls),
                        bits(first),
                        bits(last),
                        bits(data["keys"][0]),
                        bits(data["keys"][-1]),
                        bits(data["values"][0]),
                        bits(data["values"][-1]),
                        *controls,
                    ]
                )
    except (UnsupportedOverride, _MissingChannel):
        raise
    except (RampSourceError, ValueError, hou.OperationFailed) as error:
        # Typed local failure uses the established recovery graph.
        from .ramps import unavailable_ramp

        fault = error if isinstance(error, RampSourceError) else RampSourceError(7, str(error))
        return unavailable_ramp(identity, fault, kind != "float", value)
    node_data = dict(id=key, definition="cycles/" + kind_of(node) + "/1", parameters={"curve_source": source})
    links = [
        {"from": value.copy(), "to": dict(node=key, socket="value")},
        {"from": fac.copy(), "to": dict(node=key, socket="fac")},
    ]
    return [node_data], links, dict(node=key, socket="value")
