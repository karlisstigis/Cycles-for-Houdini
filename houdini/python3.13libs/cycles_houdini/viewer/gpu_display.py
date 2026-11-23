# SPDX-License-Identifier: Apache-2.0
"""Bounded offscreen OCIO display conversion with an exact CPU fallback."""

import time
import numpy as np
import shiboken6
from PySide6 import QtCore, QtGui, QtOpenGL
from .. import _native


def make_surface():
    surface = QtGui.QOffscreenSurface()
    fmt = QtGui.QSurfaceFormat()
    fmt.setVersion(4, 1)
    fmt.setProfile(QtGui.QSurfaceFormat.CoreProfile)
    surface.setFormat(fmt)
    surface.create()
    if not surface.isValid():
        surface.destroy()
        raise RuntimeError("Offscreen OpenGL surface unavailable")
    return surface


class SurfaceOwner(QtCore.QObject):
    releaseRequested = QtCore.Signal()

    def __init__(self):
        super().__init__(QtGui.QGuiApplication.instance())
        try:
            self.surface = make_surface()
            self.surface.setParent(self)
        except Exception:
            self.deleteLater()
            raise
        self.releaseRequested.connect(self.release)

    @QtCore.Slot()
    def release(self):
        # The queued signal destroys the native surface on the GUI thread.
        self.surface.destroy()
        self.deleteLater()


# Display readbacks (GPU to CPU) since start; read by benchmarks.
readbacks = 0


class GpuContext:
    """Create, use and destroy this object exclusively on its conversion thread."""

    def __init__(self, surface):
        self.surface = surface
        self.context = QtGui.QOpenGLContext()
        self.context.setFormat(surface.format())
        if not self.context.create() or not self.context.makeCurrent(surface):
            raise RuntimeError("OpenGL conversion context unavailable")
        self.gl = QtOpenGL.QOpenGLFunctions_4_1_Core()
        if not self.gl.initializeOpenGLFunctions():
            raise RuntimeError("OpenGL 4.1 functions unavailable")
        self.vao = QtOpenGL.QOpenGLVertexArrayObject()
        if not self.vao.create():
            raise RuntimeError("OpenGL vertex array unavailable")
        self.vao.bind()
        value = self.gl.glGetString(0x1F01)
        self.renderer = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
        self.program = self.input = self.fbo = None
        self.luts = []
        self.shape = None
        self.key = ""
        self.gl.glDisable(0x0BD0)
        self.gl.glDisable(0x8DB9)  # Dither, automatic sRGB encoding.
        self.max_size = 16384
        try:
            size = np.zeros(1, np.int32)
            self.gl.glGetIntegerv(0x0D33, shiboken6.VoidPtr(size.ctypes.data))  # GL_MAX_TEXTURE_SIZE
            self.max_size = int(size[0]) or self.max_size
        except Exception:
            pass

    def texture(self, pixels, channels, width, height):
        T = QtOpenGL.QOpenGLTexture
        texture = T(T.Target2D)
        texture.setFormat({1: T.R32F, 3: T.RGB32F, 4: T.RGBA32F}[channels])
        texture.setSize(width, height)
        texture.setMipLevels(1)
        texture.setMinMagFilters(T.Nearest, T.Nearest)
        texture.setWrapMode(T.ClampToEdge)
        texture.allocateStorage({1: T.Red, 3: T.RGB, 4: T.RGBA}[channels], T.Float32)
        data = np.ascontiguousarray(pixels, dtype=np.float32)
        texture.setData({1: T.Red, 3: T.RGB, 4: T.RGBA}[channels], T.Float32, shiboken6.VoidPtr(data.ctypes.data))
        return texture

    def configure(self, plan):
        if self.program:
            self.program.release()
            self.program = None
        for texture, _ in self.luts:
            texture.destroy()
        self.luts = []
        vertex = """#version 400 core
out vec2 uv;
void main(){vec2 p=vec2((gl_VertexID << 1) & 2,gl_VertexID & 2);
uv=p;gl_Position=vec4(p*2.0-1.0,0.0,1.0);}"""
        fragment = (
            """#version 400 core
in vec2 uv;out vec4 output_pixel;
uniform sampler2D source_image;
uniform float exposure_gain;uniform float display_gamma;uniform int opaque;
uniform int layer_height;uniform vec3 background0;uniform vec3 background1;
"""
            + plan["shader"]
            + """
void main(){
// Layer 0 (the first layer_height rows): straight colour and coverage, or with
// opaque (RGB without alpha) the image over black, View(exposure * RGB_premult).
// Layer k > 0: opaque composite over background k-1 in scene-linear light,
// before the view: View(exposure * RGB_premult + (1 - alpha) * B_linear).
int row=int(gl_FragCoord.y);int layer=row/layer_height;row-=layer*layer_height;
vec4 p=texelFetch(source_image,ivec2(int(gl_FragCoord.x),layer_height-1-row),0);vec3 rgb=p.rgb;
float alpha=opaque!=0?1.0:p.a;
if(layer==0){if(opaque==0&&p.a!=0.0)rgb/=p.a;rgb*=exposure_gain;}  // RGB without alpha: over black.
else{rgb=rgb*exposure_gain+(1.0-p.a)*(layer==1?background0:background1);alpha=1.0;}
rgb=cycles_view(vec4(rgb,1.0)).rgb;
if(display_gamma!=1.0)rgb=pow(max(rgb,vec3(0.0)),vec3(1.0/display_gamma));
rgb=mix(rgb,vec3(0.0),isnan(rgb));rgb=mix(rgb,vec3(0.0),isinf(rgb));
if(isnan(alpha)||isinf(alpha))alpha=0.0;
output_pixel=vec4(clamp(rgb,0.0,1.0),clamp(alpha,0.0,1.0));}"""
        )
        self.program = QtOpenGL.QOpenGLShaderProgram()
        for stage, source in [(QtOpenGL.QOpenGLShader.Vertex, vertex), (QtOpenGL.QOpenGLShader.Fragment, fragment)]:
            if not self.program.addShaderFromSourceCode(stage, source):
                raise RuntimeError(self.program.log())
        if not self.program.link():
            raise RuntimeError(self.program.log())
        self.program.bind()
        self.gl.glUniform1i(self.program.uniformLocation("source_image"), 0)
        self.exposure_location = self.program.uniformLocation("exposure_gain")
        self.gamma_location = self.program.uniformLocation("display_gamma")
        self.opaque_location = self.program.uniformLocation("opaque")
        self.layer_location = self.program.uniformLocation("layer_height")
        self.background_locations = [self.program.uniformLocation(n) for n in ("background0", "background1")]
        for unit, lut in enumerate(plan["textures"], 1):
            values = np.frombuffer(lut["values"], np.float32)
            texture = self.texture(values, lut["channels"], lut["width"], lut["height"])
            filtering = QtOpenGL.QOpenGLTexture.Linear if lut["linear"] else QtOpenGL.QOpenGLTexture.Nearest
            texture.setMinMagFilters(filtering, filtering)
            self.gl.glUniform1i(self.program.uniformLocation(lut["sampler"]), unit)
            self.luts.append((texture, unit))
        self.key = plan["key"]

    def convert(self, pixels, exposure=0.0, gamma=1.0, opaque=False, backgrounds=()):
        """RGBA8 rows: the straight image, then one opaque composite per background (one draw, one readback)."""
        height, width, channels = pixels.shape
        if channels not in (3, 4) or pixels.dtype != np.float32 or not pixels.flags.c_contiguous:
            raise ValueError("GPU display requires packed float32 RGB/RGBA")
        layers = 1 + len(backgrounds)
        if self.shape is None or self.shape[:3] != (width, height, channels):
            if self.input:
                self.input.destroy()
            self.input = self.texture(pixels, channels, width, height)
            self.shape = None
        else:
            T = QtOpenGL.QOpenGLTexture
            self.input.setData(T.RGBA if channels == 4 else T.RGB, T.Float32, shiboken6.VoidPtr(pixels.ctypes.data))
        if self.shape != (width, height, channels, layers):
            fmt = QtOpenGL.QOpenGLFramebufferObjectFormat()
            fmt.setInternalTextureFormat(0x8058)
            self.fbo = QtOpenGL.QOpenGLFramebufferObject(width, height * layers, fmt)
            if not self.fbo.isValid():
                raise RuntimeError("RGBA8 framebuffer unavailable")
            self.shape = width, height, channels, layers
        height_total = height * layers
        self.fbo.bind()
        self.gl.glViewport(0, 0, width, height_total)
        self.vao.bind()
        self.program.bind()
        self.gl.glUniform1f(self.exposure_location, float(2.0**exposure))
        self.gl.glUniform1f(self.gamma_location, float(gamma))
        self.gl.glUniform1i(self.opaque_location, int(opaque))
        self.gl.glUniform1i(self.layer_location, height)
        for location, value in zip(self.background_locations, list(backgrounds) + [(0.0, 0.0, 0.0)] * 2):
            self.gl.glUniform3f(location, *(float(v) for v in value))
        self.input.bind(0)
        for texture, unit in self.luts:
            texture.bind(unit)
        self.gl.glDrawArrays(0x0004, 0, 3)
        # QFBO.toImage labels bytes premultiplied. Direct readback preserves
        # straight display RGB and coverage, including RGB at zero coverage.
        result = np.empty((height_total, width, 4), np.uint8)
        self.gl.glReadPixels(0, 0, width, height_total, 0x1908, 0x1401, shiboken6.VoidPtr(result.ctypes.data))
        global readbacks
        readbacks += 1
        self.fbo.release()
        error = self.gl.glGetError()
        if error:
            raise RuntimeError("OpenGL error " + hex(error))
        return result

    def close(self):
        self.context.makeCurrent(self.surface)
        if self.input:
            self.input.destroy()
        for texture, _ in self.luts:
            texture.destroy()
        self.vao.destroy()
        self.program = self.fbo = self.input = None
        self.luts = []
        self.context.doneCurrent()
        self.gl = self.vao = self.context = None


class Converter:
    """Surface on GUI thread; context, upload, transform and readback on worker."""

    def __init__(self):
        self.owner = self.context = None
        self.failure = ""
        self.released = False
        try:
            self.owner = SurfaceOwner()
        except Exception as error:
            self.failure = str(error)

    def __call__(self, frame, settings):
        result = self._convert(frame, settings)
        if result is not None and result.get("composites"):
            # The backgrounds these composites were made over, as the canvas compares them.
            result["backgrounds"] = tuple(tuple(int(v) for v in color) for color in settings.get("backgrounds", ()))
        return result

    def _convert(self, frame, settings):
        # Component inspection is a display-only operation on the conversion
        # thread. Retain the same GPU OCIO path, then isolate the displayed
        # component without adding work to paintEvent or changing renderer data.
        component = {"r": 0, "g": 1, "b": 2}.get(settings.get("channels"))
        if component is not None:
            # Component views keep their display-space meaning: no composites.
            settings = dict(settings, channels="rgba" if settings.get("use_alpha", False) else "rgb", backgrounds=())
            result = self._convert(frame, settings)
            if result is not None:
                rgba = np.frombuffer(result["rgba"], np.uint8).reshape(-1, 4).copy()
                value = rgba[:, component].copy()
                rgba[:, :3] = value[:, None]
                rgba.setflags(write=False)
                result["rgba"] = memoryview(rgba).cast("B").toreadonly()
            return result
        if settings.get("channels") == "rgb" and settings.get("use_alpha", False):
            settings = dict(settings, channels="rgba")
        started = time.perf_counter()
        plan = None
        if not self.failure:
            try:
                plan = _native.display_gpu_input(frame, settings, self.context.key if self.context else "", False)
                prepared = time.perf_counter()
                backgrounds = plan["backgrounds_linear"] if plan is not None else []
                if plan is not None and plan["buffer_height"] * (1 + len(backgrounds)) > (
                    self.context.max_size if self.context else 16384
                ):
                    plan = None  # Taller than one framebuffer: this frame converts on the CPU.
                if plan is not None:
                    if self.context is None:
                        self.context = GpuContext(self.owner.surface)
                    if self.context.key != plan["key"]:
                        self.context.configure(plan)
                    configured = time.perf_counter()
                    pixels = np.frombuffer(plan["pixels"], np.float32).reshape(
                        plan["buffer_height"], plan["buffer_width"], plan["channels"]
                    )
                    rgba = self.context.convert(pixels, plan["exposure"], plan["gamma"], plan["opaque"], backgrounds)
                    transformed = time.perf_counter()
                    result = {
                        key: plan[key]
                        for key in (
                            "generation",
                            "sequence",
                            "x",
                            "y",
                            "width",
                            "height",
                            "full_width",
                            "full_height",
                            "pixel_aspect",
                            "rendered_width",
                            "rendered_height",
                            "samples",
                            "published_seconds",
                        )
                    }
                    # Owned readback views keep display pixels at the converted
                    # size. Expand only for callers needing logical-size bytes.
                    rgba.setflags(write=False)
                    rows = plan["buffer_height"]
                    layers = [
                        memoryview(rgba[k * rows : (k + 1) * rows]).cast("B").toreadonly()
                        for k in range(1 + len(backgrounds))
                    ]
                    if plan.get("compact"):
                        result.update(buffer_width=plan["buffer_width"], buffer_height=rows)
                    else:
                        layers = [
                            _native.expand_display_rgba(
                                layer, plan["buffer_width"], rows, plan["width"], plan["height"]
                            )
                            for layer in layers
                        ]
                    result["rgba"] = layers[0]
                    if backgrounds:
                        result["composites"] = layers[1:]
                    if settings.get("collect_diagnostics"):
                        result["diagnostics"] = dict(
                            backend="opengl",
                            prepare_seconds=prepared - started,
                            renderer=self.context.renderer,
                            configure_seconds=configured - prepared,
                            transform_readback_seconds=transformed - configured,
                            display_expansion_seconds=time.perf_counter() - transformed,
                            input_bytes=len(plan["pixels"]),
                            input_copied_bytes=len(plan["pixels"]) if plan["pixels_copied"] else 0,
                            readback_bytes=rgba.nbytes,
                            readback_copied_bytes=0,
                        )
                    return result
            except Exception as error:
                self.failure = str(error)
                if self.context:
                    self.context.close()
                    self.context = None
        try:
            result = _native.display_image(frame, settings)
        except _native.PreviousColorConfiguration:
            result = _native.display_image(frame, dict(settings, raw=True))
            if result is not None:
                result["previous_color_notice"] = (
                    "Showing raw color from the previous configuration (missing aces_interchange role)"
                )
        if result is not None and settings.get("collect_diagnostics"):
            result.setdefault("diagnostics", {}).update(
                backend="cpu", fallback=self.failure or "Pass or color policy uses CPU display"
            )
        return result

    def close(self):
        if self.context:
            self.context.close()
            self.context = None
        if self.owner and not self.released:
            self.released = True
            self.owner.releaseRequested.emit()
