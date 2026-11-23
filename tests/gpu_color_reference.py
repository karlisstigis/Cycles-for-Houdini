# SPDX-License-Identifier: Apache-2.0
"""Feed independently constructed OCIO shaders into the installed GL executor."""

import numpy as np
import PyOpenColorIO as OCIO
from cycles_houdini.gpu_display import GpuContext, make_surface


class GpuColorReference(GpuContext):
    def configure(self, processor):
        desc = OCIO.GpuShaderDesc.CreateShaderDesc()
        desc.setLanguage(OCIO.GPU_LANGUAGE_GLSL_4_0)
        desc.setFunctionName("cycles_view")
        desc.setResourcePrefix("cycles_")
        desc.setAllowTexture1D(False)
        processor.getDefaultGPUProcessor().extractGpuShaderInfo(desc)
        if len(desc.getUniforms()) or len(desc.get3DTextures()):
            raise RuntimeError("Reference requires static uniforms and 2D LUTs")
        textures = []
        for lut in desc.getTextures():
            textures.append(
                dict(
                    sampler=lut.samplerName,
                    width=lut.width,
                    height=lut.height,
                    channels=1 if lut.channel == OCIO.GpuShaderCreator.TEXTURE_RED_CHANNEL else 3,
                    linear=lut.interpolation != OCIO.INTERP_NEAREST,
                    values=np.asarray(lut.getValues(), dtype=np.float32).tobytes(),
                )
            )
        super().configure(dict(key=desc.getCacheID(), shader=desc.getShaderText(), textures=textures))

    def convert(self, pixels, exposure=0.0, gamma=1.0):
        # The installed executor returns top-down display bytes; this fixture's
        # independent CPU arrays retain their original bottom-up source order.
        return super().convert(pixels, exposure, gamma)[::-1]
