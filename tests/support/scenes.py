# SPDX-License-Identifier: Apache-2.0
"""Small owned HOM fixtures. Importing helpers does not import hou."""

from contextlib import contextmanager
import math
from struct import iter_unpack


def check_box_pixels(data):
    """Read the linear 32×24 RGBA fixture with the box centred in the camera."""
    width, height = 32, 24
    assert len(data) == width * height * 4 * 4, "Unexpected box image byte count"
    pixels = list(iter_unpack("<4f", data))
    assert all(math.isfinite(channel) for pixel in pixels for channel in pixel), "Box image has nonfinite pixels"

    def mean(x_range, y_range):
        region = [pixels[y * width + x] for y in y_range for x in x_range]
        return tuple(sum(pixel[channel] for pixel in region) / len(region) for channel in range(3))

    interior = mean(range(14, 18), range(10, 14))
    corners = [mean(xs, ys) for xs in (range(3), range(29, 32)) for ys in (range(3), range(21, 24))]
    red, green, blue = interior
    assert red > max(green, blue) * 2 + 0.002, ("Box centre is not red", interior)
    for corner in corners:
        assert max(abs(channel - 0.05) for channel in corner) < 0.01, ("Corner is not the grey world", corner)
        assert max(abs(a - b) for a, b in zip(interior, corner)) > 0.01, ("Box matches the world", interior, corner)
    return dict(interior_rgb=interior, corners_rgb=corners)


@contextmanager
def box_scene(name="contract_scene"):
    import hou

    geometry = hou.node("/obj").createNode("geo", name, run_init_scripts=False)
    camera = None
    try:
        box = geometry.createNode("box", "source")
        box.setDisplayFlag(True)
        box.setRenderFlag(True)
        camera = hou.node("/obj").createNode("cam", name + "_camera")
        camera.parmTuple("t").set((0, 0, 5))
        yield geometry, box, camera
    finally:
        if camera is not None:
            camera.destroy()
        geometry.destroy()
