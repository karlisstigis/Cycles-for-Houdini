# SPDX-License-Identifier: Apache-2.0
"""Image colour spaces named by another OCIO configuration (blend_material_export.image_colorspace)."""

from types import SimpleNamespace


class _Image:
    def __init__(self, stored, loaded, is_float=False):
        self.colorspace_settings = SimpleNamespace(name=stored, is_data=False)
        self._loaded, self.is_float = loaded, is_float

    @property
    def size(self):
        # Blender resolves an unknown stored name to its fallback when the image loads.
        self.colorspace_settings.name = self._loaded
        return (16, 16)


def test_known_name_is_used_as_is():
    from cycles_blender.blend_material_export import image_colorspace

    assert image_colorspace(_Image("Non-Color", "unused")) == "Non-Color"


def test_unknown_name_reads_the_space_blender_loads_the_image_with():
    from cycles_blender.blend_material_export import image_colorspace

    # A file saved under Houdini's ACES config: RNA reads "" until the image loads.
    assert image_colorspace(_Image("", "sRGB")) == "sRGB"


def test_unloadable_image_falls_back_to_the_default_role(monkeypatch):
    import PyOpenColorIO as ocio

    from cycles_blender.blend_material_export import image_colorspace

    config = ocio.Config.CreateRaw()
    linear = ocio.ColorSpace(name="Linear Test")
    config.addColorSpace(linear)
    config.setRole("default_float", "Linear Test")
    monkeypatch.setattr(ocio, "GetCurrentConfig", lambda: config)
    assert image_colorspace(_Image("", "", is_float=True)) == "Linear Test"
