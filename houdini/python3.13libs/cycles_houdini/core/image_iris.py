# SPDX-License-Identifier: Apache-2.0
"""Optional native aperture-image controls; decoding stays in the renderer worker."""

import os
from functools import lru_cache


@lru_cache(maxsize=1)
def supported():
    from .. import _native

    return bool(_native.capabilities().get("image_iris", False))


def controls():
    import hou
    from ..scene.object_properties import _toggle, _menu, _condition, _section

    enabled = _condition(
        _toggle(
            "image_iris",
            "Enable",
            help="Use an image to shape depth-of-field bokeh. Image intensity is normalized, so changing the mask does not change exposure in a uniform scene. Requires Depth of Field.",
        ),
        "{ cycles_dof == 0 }",
    )
    image = hou.StringParmTemplate(
        "cycles_image_iris_file",
        "Image",
        1,
        default_value=("",),
        string_type=hou.stringParmType.FileReference,
        help="A nonnegative image describes the aperture intensity. Read as raw/non-color data with bilinear filtering and black outside the image. Image size maps to the aperture square; Rotation and Ratio still apply. Invalid replacements retain the last accepted render until repaired.",
    )
    method = _menu(
        "image_iris_method",
        "Sampling Method",
        [("cdf", "CDF"), ("hierarchical", "Hierarchical")],
        "cdf",
        "Both methods sample the same normalized aperture image. Their sample placement and finite-sample noise differ; compare at equal samples or equal render time. The image and method are held at the requested frame during shutter sampling.",
    )
    color = _toggle(
        "image_iris_color",
        "Use Color",
        help="Use raw linear RGB aperture transmission to color bokeh. Overall exposure is compensated with one luminance normalization; filter tint is retained, without per-channel white balance. This RGB approximation does not simulate a spectral lens. Grayscale images remain neutral.",
    )
    fields = [
        enabled,
        _condition(image, "{ cycles_dof == 0 } { cycles_image_iris == 0 }"),
        _condition(color, "{ cycles_dof == 0 } { cycles_image_iris == 0 }"),
        _condition(method, "{ cycles_dof == 0 } { cycles_image_iris == 0 }"),
    ]
    available = supported()
    for parm in fields:
        parm.hide(not available)
    panel = _section("camera_image_iris", "Image Iris", fields, toggle="image_iris", expanded=True)
    panel.hide(not available)
    return [panel]


def source(value):
    """Capture cheap source values only; shutter samples never load files."""
    if not value("cycles_image_iris", False):
        return None
    return (
        value("cycles_image_iris_file", ""),
        value("cycles_image_iris_method", "cdf"),
        bool(value("cycles_image_iris_color", False)),
    )


def resolve(lens):
    """Resolve only nominal image controls after independent optics recovery."""
    result = dict(lens)
    request = result.pop("_image_iris_source", None)
    result.update(image_iris="", image_iris_method=0, image_iris_color=False)
    if request is None or result["aperture_size"] == 0:
        return result
    if not supported():
        raise ValueError(
            "Image Iris is enabled on this camera, but this Cycles build does not include the optional Image Iris feature"
        )
    filename, method, color = request
    methods = {"cdf": 1, "hierarchical": 2}
    if method not in methods:
        raise ValueError("Unknown Image Iris sampling method: " + str(method))
    if not isinstance(filename, str) or not filename.strip():
        raise ValueError("Image Iris requires an image file")
    import hou

    # HOM/SOHO evaluation has already expanded time-dependent expressions at
    # the requested time. Relative paths follow the HIP, also on farm workers.
    if not os.path.isabs(filename):
        filename = os.path.join(hou.getenv("HIP") or os.getcwd(), filename)
    result.update(
        image_iris=os.path.normpath(os.path.abspath(filename)),
        image_iris_method=methods[method],
        image_iris_color=color,
    )
    return result
