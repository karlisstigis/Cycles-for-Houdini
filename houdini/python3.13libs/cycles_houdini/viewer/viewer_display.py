# SPDX-License-Identifier: Apache-2.0
"""How the viewer puts its image on screen, chosen once per panel.

CYCLES_HOUDINI_VIEWER_DISPLAY selects, when a viewer panel is created:

- ``gpu`` (default): like Houdini's own viewers, the whole viewer (toolbars
  and image) is one native window, as RE_Window is, and an ordinary (non-native)
  OpenGL widget inside it draws the frame as a texture. Repaints flush only
  the viewer's window, never Houdini's main window.
- ``native``: the same native viewer window, the image painted by the classic
  QPainter canvas.
- ``classic``: the viewer and canvas are ordinary child widgets painted with
  QPainter, so docked repaints are flushed through Houdini's main window.

Only the viewer's root is native, never the canvas: Houdini's pane layout
moves the native child windows directly under its pane drawable itself, and
places a native viewer root exactly as it places RE_Window. A native canvas
there was moved out of its slot (tests/houdini_viewer_native_geometry_gui_smoke.py).

All three draw the same converted 8-bit image with nearest sampling, and the
same QPainter code draws the background, checker and overlays.
"""

import math
import os

import numpy as np
import shiboken6
from PySide6 import QtCore, QtGui

from .panel import CanvasBehaviour

ENVIRONMENT = "CYCLES_HOUDINI_VIEWER_DISPLAY"
MODES = ("gpu", "native", "classic")
DEFAULT = "gpu"


def mode(environ=None):
    """The requested mode and a note when the variable held something else."""
    value = (os.environ if environ is None else environ).get(ENVIRONMENT, "")
    value = value.strip().lower()
    if not value:
        return DEFAULT, ""
    if value in MODES:
        return value, ""
    return DEFAULT, f"{ENVIRONMENT}={value!r} is not one of {', '.join(MODES)}"


def make_native(widget):
    """Give widget its own native child window without nativizing its ancestors (the viewer root)."""
    widget.setAttribute(QtCore.Qt.WA_DontCreateNativeAncestors, True)
    widget.setAttribute(QtCore.Qt.WA_NativeWindow, True)


_opengl_problem = None


def opengl_problem():
    """Why this process cannot create an OpenGL context, or "" when it can.

    A QOpenGLWidget whose context cannot be created never calls paintGL, so the
    GPU canvas's own QPainter fallback never runs and the view stays black.
    That happens on a Qt platform without OpenGL (the offscreen platform hython
    tests use, a remote session without a driver). Checked once per process,
    without making the probe context current, and only on Qt's windowless
    platforms: on a real display platform (Houdini's interface) nothing is
    created before the canvas, so the interface path stays exactly as it was.
    """
    global _opengl_problem
    if _opengl_problem is None:
        app = QtGui.QGuiApplication.instance()
        platform = app.platformName() if app is not None else ""
        if platform not in ("offscreen", "minimal"):
            _opengl_problem = ""
            return _opengl_problem
        context = QtGui.QOpenGLContext()
        context.setFormat(QtGui.QSurfaceFormat.defaultFormat())
        if not context.create() or not context.isValid():
            _opengl_problem = f"no OpenGL context on the Qt platform '{platform}'"
        else:
            _opengl_problem = ""
        del context
    return _opengl_problem


def create_canvas(environ=None):
    from .panel import ImageCanvas

    requested, note = mode(environ)
    canvas = None
    if requested == "gpu":
        problem = opengl_problem()
        if problem:
            note = (note + "; " if note else "") + "OpenGL unavailable, QPainter used: " + problem
            requested = "native"
        else:
            try:
                canvas = gpu_canvas_class()()
            except Exception as error:  # QtOpenGLWidgets missing: keep the native window.
                note = (note + "; " if note else "") + "OpenGL widget unavailable: " + str(error)
                requested = "native"
    if canvas is None:
        canvas = ImageCanvas()
    canvas.display_mode = requested
    canvas.display_note = note
    return canvas


def describe(canvas):
    """One line for the diagnostics tooltip."""
    mode_name = getattr(canvas, "display_mode", "classic")
    text = {
        "gpu": "viewer in its own native window, image drawn by OpenGL",
        "native": "viewer in its own native window, image drawn by QPainter",
        "classic": "QPainter, drawn through the parent window",
    }.get(mode_name, mode_name)
    extra = []
    info = getattr(canvas, "gl_info", "")
    if info:
        extra.append(info)
    failure = getattr(canvas, "gl_failure", "")
    if failure:
        extra.append("OpenGL image drawing failed, QPainter used: " + failure)
    note = getattr(canvas, "display_note", "")
    if note:
        extra.append(note)
    return f"Viewer display: {mode_name} ({text})" + "".join(" · " + item for item in extra)


# Exact QPainter emulation ---------------------------------------------------
#
# The classic canvas draws with Qt's raster engine; the GPU canvas must put the
# same bytes on screen. Which image pixel lands on which screen pixel, and
# which screen pixels a fill covers, follow Qt's rules (Qt 6 qpaintengine_raster.cpp,
# qdrawhelper.cpp, qrasterizer.cpp): rounded edges, the aliased line rasterizer
# for stretched images, and a 16.16 fixed-point nearest fetch restarted every
# 2048 pixels of a span. They are computed here, once per view change, as
# per-column and per-row lookup tables the shader reads; blending repeats Qt's
# integer arithmetic. tests/pytest_cases/test_viewer_display_raster.py checks
# these tables against QPainter itself.

FIXED = 65536.0
BUFFER = 2048  # qdrawhelper_p.h BufferSize


def qround(value):
    """qRound: half away from zero."""
    return int(value + math.copysign(0.5, value))


def compact_sample_rect(rect, storage, logical, ratio):
    # At DPR 1, Qt normally blits a 1:1 image at its rounded origin. Match
    # that placement when a compact preview needs scaled painting instead.
    if ratio == 1 and storage != logical and rect[2:] == logical:
        return qround(rect[0]), qround(rect[1]), rect[2], rect[3]
    return rect


def _truncate(values):
    """C++ int(double): toward zero."""
    return np.trunc(values).astype(np.int64)


def _fast_matrix(inverse):
    m11, m12, m21, m22 = inverse.m11(), inverse.m12(), inverse.m21(), inverse.m22()
    f1, f2 = m11 * m11 + m21 * m21, m12 * m12 + m22 * m22
    return (
        f1 < 1e4
        and f2 < 1e4
        and f1 > 1.0 / 65536
        and f2 > 1.0 / 65536
        and abs(inverse.dx()) < 1e4
        and abs(inverse.dy()) < 1e4
    )


def _bound(values, size, tiled):
    return np.mod(values, size) if tiled else np.clip(values, 0, size - 1)


def _row_axis(lo, hi, m11, dx, size, tiled, fast):
    """Source column for device columns lo..hi-1 of spans starting at lo (fetchTransformed)."""
    xs = np.arange(lo, hi, dtype=np.int64)
    piece = lo + BUFFER * ((xs - lo) // BUFFER)
    if fast:
        start = _truncate((m11 * (piece + 0.5) + dx) * FIXED)
        return _bound((start + (xs - piece) * int(m11 * FIXED)) >> 16, size, tiled)
    out = np.empty(len(xs), np.int64)
    for first in np.unique(piece):
        chosen = piece == first
        steps = np.full(int(chosen.sum()), m11)
        steps[0] = m11 * (first + 0.5) + dx
        out[chosen] = np.floor(np.cumsum(steps)).astype(np.int64)
    return _bound(out, size, tiled)


def _column_axis(lo, hi, m22, dy, size, tiled, fast):
    ys = np.arange(lo, hi, dtype=np.int64)
    if fast:
        return _bound(_truncate((m22 * (ys + 0.5) + dy) * FIXED) >> 16, size, tiled)
    return _bound(np.floor(m22 * (ys + 0.5) + dy).astype(np.int64), size, tiled)


def _device_clip(ratio, clip):
    """qt_mapFillRect of a logical clip rectangle: (x1, x2, y1, y2) half-open, or None."""
    if clip is None:
        return None
    x1, x2 = qround(clip[0] * ratio), qround((clip[0] + clip[2]) * ratio)
    y1, y2 = qround(clip[1] * ratio), qround((clip[1] + clip[3]) * ratio)
    if x1 == x2 or y1 == y2:
        return (0, 0, 0, 0)
    return min(x1, x2), max(x1, x2), min(y1, y2), max(y1, y2)


def _clip_bounds(device, clip):
    x1, x2, y1, y2 = 0, device[0], 0, device[1]
    if clip is not None:
        x1, x2, y1, y2 = max(x1, clip[0]), min(x2, clip[1]), max(y1, clip[2]), min(y2, clip[3])
    return x1, x2, y1, y2


def fill_coverage(rect, ratio, device, clip=None):
    """Device pixels [x1, x2) x [y1, y2) an aliased QPainter.fillRect(rect) covers."""
    if ratio == 1:
        x1, x2 = qround(rect[0]), qround(rect[0] + rect[2])
        y1, y2 = qround(rect[1]), qround(rect[1] + rect[3])
    else:  # QTransform::mapRect of a scale, then toNormalizedFillRect.
        x, y = ratio * rect[0], ratio * rect[1]
        x1, x2 = qround(x), qround(x + ratio * rect[2])
        y1, y2 = qround(y), qround(y + ratio * rect[3])
    cx1, cx2, cy1, cy2 = _clip_bounds(device, _device_clip(ratio, clip))
    return max(min(x1, x2), cx1), min(max(x1, x2), cx2), max(min(y1, y2), cy1), min(max(y1, y2), cy2)


def _line_coverage(rect, device, clip):
    """QRasterizer::rasterizeLine, aliased, as drawImage uses it for a stretched image at scale 1."""
    x, y, w, h = rect
    clip_left, clip_right, clip_top, clip_bottom = _clip_bounds(device, clip)
    clip_right -= 1
    clip_bottom -= 1
    if clip_right < clip_left or clip_bottom < clip_top:
        return None
    bottom = y + h
    ay = (y + bottom) * 0.5
    ax = (x + x) * 0.5
    bx = ((x + w) + (x + w)) * 0.5
    width = h / w
    pax, pbx = ax, bx
    offs = abs(bx - ax) * width * 0.5
    xmin, xmax = float(clip_left), float(clip_right + 1)
    ymin = clip_top - offs
    ymax = ymin + ((clip_bottom + 1 + offs) - ymin)
    if pax < xmin:
        if pbx <= xmin:
            return None
        pax = xmin
    elif pax > xmax:
        if pbx >= xmax:
            return None
        pax = xmax
    if pbx < xmin:
        pbx = xmin
    elif pbx > xmax:
        pbx = xmax
    if ay < ymin or ay > ymax:
        return None
    clipped = (pax - pbx) * (pax - pbx)
    if clipped == 0:
        return None
    width *= math.sqrt(((ax - bx) * (ax - bx)) / clipped)
    centre = (pax + pbx) * 0.5
    dy = width * (abs(pbx - pax) * 0.5)
    top, bottom = ay - dy, ay + dy
    half = 0.5 * (1 / width) * (bottom - top)
    left, right = centre - half, centre + half
    left = min(max(float(clip_left), left), float(clip_right + 1))
    right = min(max(float(clip_left), right), float(clip_right + 1))
    top = min(max(float(clip_top), top), float(clip_bottom + 1))
    bottom = min(max(float(clip_top), bottom), float(clip_bottom + 1))
    if int((right - left) * 64.0) == 0 or int((bottom - top) * 64.0) == 0:
        return None
    i_top, i_left = int(top + 0.5), int(left + 0.5)
    i_bottom = -1 if bottom < 0.5 else int(bottom - 0.5)
    i_right = -1 if right < 0.5 else int(right - 0.5)
    if i_right < i_left or i_bottom < i_top:
        return None
    return i_left, i_right + 1, i_top, i_bottom + 1


def image_lookup(rect, image_size, ratio, device, clip=None):
    """Per device column and row, the image pixel QPainter.drawImage(rect, image) puts there, or -1.

    rect and clip are logical (x, y, w, h); device is the target size in device
    pixels. For a non-premultiplied RGBA8888 image without smooth transform.
    """
    W, H = image_size
    lut_x = np.full(device[0], -1, np.int32)
    lut_y = np.full(device[1], -1, np.int32)
    if not W or not H or not rect[2] > 0 or not rect[3] > 0:
        return lut_x, lut_y
    device_clip = _device_clip(ratio, clip)
    stretch = rect[2] != W or rect[3] != H
    if W == 1 and H == 1:  # Drawn as a fill of its one colour.
        x1, x2, y1, y2 = fill_coverage(rect, ratio, device, clip)
        lut_x[x1:x2] = 0
        lut_y[y1:y2] = 0
        return lut_x, lut_y
    if ratio == 1 and stretch:
        covered = _line_coverage(rect, device, device_clip)
        if covered is None:
            return lut_x, lut_y
        x1, x2, y1, y2 = covered
    else:
        x1, x2, y1, y2 = fill_coverage(rect, ratio, device, clip)
    if x2 <= x1 or y2 <= y1:
        return lut_x, lut_y
    if ratio == 1 and not stretch:
        # Translation only: blend_untransformed at the rounded position, inside the image.
        left, top = qround(rect[0]), qround(rect[1])
        x1, x2, y1, y2 = max(x1, left), min(x2, left + W), max(y1, top), min(y2, top + H)
        if x2 > x1 and y2 > y1:
            lut_x[x1:x2] = np.arange(x1, x2) - left
            lut_y[y1:y2] = np.arange(y1, y2) - top
        return lut_x, lut_y
    matrix = QtGui.QTransform.fromScale(ratio, ratio)
    matrix.translate(rect[0], rect[1])
    if stretch:
        matrix.scale(rect[2] / W, rect[3] / H)
    inverse, _ = (QtGui.QTransform.fromTranslate(1.0 / 65536, 1.0 / 65536) * matrix).inverted()
    fast = _fast_matrix(inverse)
    lut_x[x1:x2] = _row_axis(x1, x2, inverse.m11(), inverse.dx(), W, False, fast)
    lut_y[y1:y2] = _column_axis(y1, y2, inverse.m22(), inverse.dy(), H, False, fast)
    return lut_x, lut_y


def pattern_lookup(rect, ratio, device, clip=None, size=(16, 16)):
    """Per device column and row, the texel of a texture brush (origin 0) QPainter.fillRect(rect) puts there, or -1."""
    lut_x = np.full(device[0], -1, np.int32)
    lut_y = np.full(device[1], -1, np.int32)
    x1, x2, y1, y2 = fill_coverage(rect, ratio, device, clip)
    if x2 <= x1 or y2 <= y1:
        return lut_x, lut_y
    if ratio == 1:
        lut_x[x1:x2] = np.arange(x1, x2) % size[0]
        lut_y[y1:y2] = np.arange(y1, y2) % size[1]
        return lut_x, lut_y
    inverse, _ = (
        QtGui.QTransform.fromTranslate(1.0 / 65536, 1.0 / 65536) * QtGui.QTransform.fromScale(ratio, ratio)
    ).inverted()
    fast = _fast_matrix(inverse)
    lut_x[x1:x2] = _row_axis(x1, x2, inverse.m11(), inverse.dx(), size[0], True, fast)
    lut_y[y1:y2] = _column_axis(y1, y2, inverse.m22(), inverse.dy(), size[1], True, fast)
    return lut_x, lut_y


def device_size(size, ratio):
    """QOpenGLWidget framebuffer size: QSize * qreal rounds each side."""
    return qround(size[0] * ratio), qround(size[1] * ratio)


# Texture bookkeeping --------------------------------------------------------


def damage_union(pending, before_key, after_key, rect):
    """Chain image changes between paints: (base key, current key, union rect) or None.

    A change that does not continue the pending chain starts a new one; the
    texture then needs a full upload unless it holds the new base.
    """
    if before_key is None:
        return None
    if pending is not None and pending[1] == before_key:
        x, y, w, h = pending[2]
        left, top = min(x, rect[0]), min(y, rect[1])
        right, bottom = max(x + w, rect[0] + rect[2]), max(y + h, rect[1] + rect[3])
        return pending[0], after_key, (left, top, right - left, bottom - top)
    return before_key, after_key, tuple(rect)


def upload_plan(slot_key, slot_size, image_key, image_size, damage):
    """None (texture current), "full", or the (x, y, w, h) rectangle to update."""
    if slot_size != image_size or slot_key is None:
        return "full"
    if slot_key == image_key:
        return None
    if damage is not None and damage[0] == slot_key and damage[1] == image_key:
        x, y, w, h = damage[2]
        width, height = image_size
        left, top = max(0, x), max(0, y)
        right, bottom = min(width, x + w), min(height, y + h)
        if right <= left or bottom <= top:
            return None
        return left, top, right - left, bottom - top
    return "full"


def _text(value):
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value or "")


VERTEX = """#version 330 core
void main(){vec2 p=vec2(float((gl_VertexID&1)*2-1),float((gl_VertexID>>1)*2-1));gl_Position=vec4(p,0.0,1.0);}"""

# Layers as QPainter paints them: canvas colour; under the image its fill
# colour or checker; the image (straight alpha, Qt's premultiply and SourceOver
# in integers); the A/B compare image in its clip; QPainter-drawn overlays
# (premultiplied). Lookup channels: r image, g fill/pattern, b compare image,
# a compare fill/pattern; -1 where not covered. Over each image, its opaque
# scene-linear composites as draw_composites paints them: the first everywhere,
# the second (light checker colour) again on the light cells of the pattern.
FRAGMENT = """#version 330 core
uniform isampler2D lut_x;
uniform isampler2D lut_y;
uniform sampler2D image;
uniform sampler2D compare_image;
uniform sampler2D pattern;
uniform sampler2D overlay;
uniform int height;
uniform ivec3 canvas_color;
uniform ivec3 fill_color;
uniform int use_pattern;
uniform int has_compare;
uniform int has_overlay;
uniform sampler2D composite0;
uniform sampler2D composite1;
uniform sampler2D compare_composite0;
uniform sampler2D compare_composite1;
uniform int composites;
uniform int compare_composites;
out vec4 color;
ivec4 texel(sampler2D s, ivec2 p){return ivec4(round(texelFetch(s,p,0)*255.0));}
int mul(int x,int a){int t=x*a;return (t+(t>>8)+128)>>8;}
ivec3 over(ivec4 s,ivec3 d){
  if(s.a==255)return s.rgb;
  if(s.a==0)return d;
  return ivec3(mul(s.r,s.a),mul(s.g,s.a),mul(s.b,s.a))+ivec3(mul(d.r,255-s.a),mul(d.g,255-s.a),mul(d.b,255-s.a));
}
ivec3 layer(ivec3 c,int fx,int fy,int ix,int iy,sampler2D img,int count,sampler2D k0,sampler2D k1){
  if(fx>=0&&fy>=0)c=use_pattern!=0?texel(pattern,ivec2(fx,fy)).rgb:fill_color;
  if(ix>=0&&iy>=0){
    c=over(texel(img,ivec2(ix,iy)),c);
    if(count>0)c=over(texel(k0,ivec2(ix,iy)),c);
    if(count>1&&fx>=0&&fy>=0&&((fx%16<8)==(fy%16<8)))c=over(texel(k1,ivec2(ix,iy)),c);
  }
  return c;
}
void main(){
  int x=int(gl_FragCoord.x);
  int y=height-1-int(gl_FragCoord.y);
  ivec4 lx=texelFetch(lut_x,ivec2(x,0),0);
  ivec4 ly=texelFetch(lut_y,ivec2(y,0),0);
  ivec3 c=layer(canvas_color,lx.g,ly.g,lx.r,ly.r,image,composites,composite0,composite1);
  if(has_compare!=0)c=layer(c,lx.a,ly.a,lx.b,ly.b,compare_image,compare_composites,compare_composite0,compare_composite1);
  if(has_overlay!=0){
    ivec4 o=texel(overlay,ivec2(x,y));
    if(o.a==255)c=o.rgb;
    else if(o.a!=0)c=o.rgb+ivec3(mul(c.r,255-o.a),mul(c.g,255-o.a),mul(c.b,255-o.a));
  }
  color=vec4(vec3(c)/255.0,1.0);
}"""


def _rgb(color):
    return color.red(), color.green(), color.blue()


def _address(array):
    return shiboken6.VoidPtr(array.ctypes.data)


class _Slot:
    def __init__(self):
        self.texture = None
        self.size = None
        self.key = None


class GlResources:
    """Shader, textures and lookup tables of one QOpenGLWidget context.

    Created on first paint in the widget's context; destroyed with that
    context (bind_to_context) or by release_display.
    """

    def __init__(self, context):
        from PySide6 import QtOpenGL

        self.QtOpenGL = QtOpenGL
        fmt = context.format()
        if context.isOpenGLES() or (fmt.majorVersion(), fmt.minorVersion()) < (3, 3):
            raise RuntimeError(f"OpenGL 3.3 required, context is {fmt.majorVersion()}.{fmt.minorVersion()}")
        self.program = QtOpenGL.QOpenGLShaderProgram()
        for stage, source in ((QtOpenGL.QOpenGLShader.Vertex, VERTEX), (QtOpenGL.QOpenGLShader.Fragment, FRAGMENT)):
            if not self.program.addShaderFromSourceCode(stage, source):
                raise RuntimeError(self.program.log())
        if not self.program.link():
            raise RuntimeError(self.program.log())
        self.vao = QtOpenGL.QOpenGLVertexArrayObject()
        if not self.vao.create():
            raise RuntimeError("OpenGL vertex array unavailable")
        self.buffer = None
        self.slots = {
            name: _Slot()
            for name in (
                "image",
                "compare",
                "pattern",
                "overlay",
                "lut_x",
                "lut_y",
                "blank",
                "composite0",
                "composite1",
                "compare_composite0",
                "compare_composite1",
            )
        }
        self.lookup_key = self.overlay_key = None
        self.uploads = 0
        self.upload_bytes = 0
        functions = context.functions()
        version, renderer = (_text(functions.glGetString(name)) for name in (0x1F02, 0x1F01))
        self.info = f"OpenGL {version} · {renderer}" if version else ""

    def _texture(self, slot, size, integer):
        T = self.QtOpenGL.QOpenGLTexture
        if slot.texture is not None and slot.size == size:
            return False
        if slot.texture is not None:
            slot.texture.destroy()
        texture = T(T.Target2D)
        texture.setFormat(T.RGBA32I if integer else T.RGBA8_UNorm)
        texture.setSize(*size)
        texture.setMipLevels(1)
        texture.setMinMagFilters(T.Nearest, T.Nearest)
        texture.setWrapMode(T.ClampToEdge)
        texture.allocateStorage(T.RGBA_Integer if integer else T.RGBA, T.Int32 if integer else T.UInt8)
        if not texture.isStorageAllocated():
            raise RuntimeError("OpenGL texture storage unavailable")
        slot.texture, slot.size, slot.key = texture, size, None
        return True

    def sync_image(self, name, image, damage=None):
        """Bring the named RGBA8 texture up to date with image; return it."""
        T = self.QtOpenGL.QOpenGLTexture
        slot = self.slots[name]
        size = (image.width(), image.height())
        plan = upload_plan(slot.key, slot.size, image.cacheKey(), size, damage)
        if plan is None and slot.texture is not None:
            return slot.texture
        source = image
        if source.format() != QtGui.QImage.Format_RGBA8888:
            source = source.convertToFormat(QtGui.QImage.Format_RGBA8888)
        if self._texture(slot, size, False) or plan is None:
            plan = "full"
        x, y, w, h = (0, 0, *size) if plan == "full" else plan
        stride = source.bytesPerLine()
        bits = np.frombuffer(source.constBits(), np.uint8)
        options = self.QtOpenGL.QOpenGLPixelTransferOptions()
        options.setAlignment(4)
        options.setRowLength(stride // 4)
        address = shiboken6.VoidPtr(bits.ctypes.data + y * stride + x * 4)
        slot.texture.setData(x, y, 0, w, h, 1, T.RGBA, T.UInt8, address, options)
        del bits
        slot.key = image.cacheKey()
        self.uploads += 1
        self.upload_bytes += w * h * 4
        return slot.texture

    def _lookup(self, name, columns):
        T = self.QtOpenGL.QOpenGLTexture
        slot = self.slots[name]
        data = np.ascontiguousarray(np.stack(columns, axis=1).astype(np.int32))
        self._texture(slot, (data.shape[0], 1), True)
        slot.texture.setData(T.RGBA_Integer, T.Int32, _address(data))

    def update_lookups(self, canvas, device, ratio, pattern_size):
        """Lookup tables for the current view; recomputed only when the view changes."""
        image = canvas.image
        rect = canvas.image_rect()
        target = (rect.x(), rect.y(), rect.width(), rect.height())
        storage = image.size().toTuple()
        sample_rect = compact_sample_rect(target, storage, canvas.logical_size(image), ratio)
        wipe = canvas.compare_geometry()
        compare = None
        if wipe is not None:
            clip = wipe[1]
            compare = ((clip.x(), clip.y(), clip.width(), clip.height()), canvas.compare_image.size().toTuple())
        key = (device, ratio, target, storage, compare, pattern_size, sample_rect)
        if key == self.lookup_key:
            return
        none_x, none_y = np.full(device[0], -1, np.int32), np.full(device[1], -1, np.int32)
        image_x, image_y = image_lookup(sample_rect, storage, ratio, device) if not image.isNull() else (none_x, none_y)
        fill_x, fill_y = (
            pattern_lookup(target, ratio, device, size=pattern_size) if not image.isNull() else (none_x, none_y)
        )
        compare_x = compare_y = compare_fill_x = compare_fill_y = None
        if compare is not None:
            compare_x, compare_y = image_lookup(target, compare[1], ratio, device, compare[0])
            compare_fill_x, compare_fill_y = pattern_lookup(target, ratio, device, compare[0], pattern_size)
        else:
            compare_x = compare_fill_x = none_x
            compare_y = compare_fill_y = none_y
        self._lookup("lut_x", (image_x, fill_x, compare_x, compare_fill_x))
        self._lookup("lut_y", (image_y, fill_y, compare_y, compare_fill_y))
        self.lookup_key = key

    def update_overlay(self, canvas, theme, device, ratio):
        """QPainter-drawn overlays on a transparent image the size of the view, or None."""
        empty = canvas.image.isNull()
        wipe = canvas.compare_geometry()
        drag = None
        if canvas.drag_start is not None and canvas.drag_end is not None:
            drag = QtCore.QRectF(canvas.drag_start, canvas.drag_end).normalized()
        if not empty and wipe is None and drag is None:
            self.overlay_key = None
            return None
        key = (
            device,
            ratio,
            empty,
            None if wipe is None else (wipe[0].getRect(), wipe[2]),
            None if drag is None else drag.getRect(),
            tuple(sorted((name, color.rgba()) for name, color in theme.items() if hasattr(color, "rgba"))),
            canvas.font().key(),
        )
        slot = self.slots["overlay"]
        if key == self.overlay_key and slot.texture is not None:
            return slot.texture
        layer = QtGui.QImage(device[0], device[1], QtGui.QImage.Format_ARGB32_Premultiplied)
        layer.setDevicePixelRatio(ratio)
        # Font sizes follow the paint device's logical DPI: use the widget's.
        layer.setDotsPerMeterX(round(canvas.logicalDpiX() / 0.0254))
        layer.setDotsPerMeterY(round(canvas.logicalDpiY() / 0.0254))
        layer.fill(0)
        painter = QtGui.QPainter(layer)
        painter.setFont(canvas.font())
        if empty:  # The whole view, opaque, exactly as the classic canvas paints it.
            canvas.paint_scene(painter)
        else:
            canvas.paint_overlays(painter, theme)
        painter.end()
        self.sync_image("overlay", layer)
        self.overlay_key = key
        return slot.texture

    def blank(self):
        slot = self.slots["blank"]
        if slot.texture is None:
            image = QtGui.QImage(1, 1, QtGui.QImage.Format_RGBA8888)
            image.fill(0)
            self.sync_image("blank", image)
        return slot.texture

    def render(self, canvas, theme, background, device, ratio, damage, functions):
        """Draw the whole view into the current framebuffer."""
        brush = canvas._checker.textureImage()
        pattern_size = (brush.width(), brush.height())
        self.update_lookups(canvas, device, ratio, pattern_size)
        image = self.sync_image("image", canvas.image, damage) if not canvas.image.isNull() else self.blank()
        wipe = canvas.compare_geometry()
        compare = self.sync_image("compare", canvas.compare_image) if wipe is not None else self.blank()
        pattern = self.slots["pattern"]
        if pattern.key != brush.cacheKey():
            self.sync_image("pattern", brush)
            pattern.key = brush.cacheKey()
        overlay = self.update_overlay(canvas, theme, device, ratio)
        own = canvas.composite_images(canvas.image) or []
        other = (canvas.composite_images(canvas.compare_image) or []) if wipe is not None else []
        composite_textures = [
            self.sync_image(prefix + str(index), images[index]) if index < len(images) else self.blank()
            for prefix, images in (("composite", own), ("compare_composite", other))
            for index in range(2)
        ]
        functions.glViewport(0, 0, device[0], device[1])
        for capability in (0x0BE2, 0x0B71, 0x0B90, 0x0C11):  # Blend, depth, stencil, scissor.
            functions.glDisable(capability)
        functions.glColorMask(True, True, True, True)
        self.vao.bind()
        self.program.bind()
        location = self.program.uniformLocation
        units = (
            ("image", image),
            ("compare_image", compare),
            ("pattern", pattern.texture),
            ("overlay", overlay if overlay is not None else self.blank()),
            ("lut_x", self.slots["lut_x"].texture),
            ("lut_y", self.slots["lut_y"].texture),
            ("composite0", composite_textures[0]),
            ("composite1", composite_textures[1]),
            ("compare_composite0", composite_textures[2]),
            ("compare_composite1", composite_textures[3]),
        )
        for unit, (name, texture) in enumerate(units):
            texture.bind(unit)
            self.program.setUniformValue1i(location(name), unit)
        self.program.setUniformValue1i(location("height"), device[1])
        functions.glUniform3i(location("canvas_color"), *_rgb(theme["canvas"]))
        functions.glUniform3i(location("fill_color"), *_rgb(background))
        self.program.setUniformValue1i(location("use_pattern"), int(bool(canvas.checker_enabled)))
        self.program.setUniformValue1i(location("has_compare"), int(wipe is not None))
        self.program.setUniformValue1i(location("has_overlay"), int(overlay is not None))
        self.program.setUniformValue1i(location("composites"), len(own))
        self.program.setUniformValue1i(location("compare_composites"), len(other))
        functions.glDrawArrays(0x0005, 0, 4)  # GL_TRIANGLE_STRIP
        for unit, (_, texture) in enumerate(units):
            texture.release(unit)
        functions.glActiveTexture(0x84C0)
        self.program.release()
        self.vao.release()
        error = functions.glGetError()
        if error:
            raise RuntimeError("OpenGL error " + hex(error))

    def destroy(self):
        """The owning context must be current."""
        for slot in self.slots.values():
            if slot.texture is not None:
                slot.texture.destroy()
            slot.texture = slot.size = slot.key = None
        if self.vao is not None and self.vao.isCreated():
            self.vao.destroy()
        if self.program is not None:
            self.program.removeAllShaders()
        self.program = self.vao = None
        self.lookup_key = self.overlay_key = None


def _same(first, second):
    return (
        first is not None
        and second is not None
        and shiboken6.getCppPointer(first)[0] == shiboken6.getCppPointer(second)[0]
    )


def bind_to_context(resources, context, holder):
    """Destroy resources with their context: on release(), or when the context goes.

    QOpenGLWidget deletes its context when it is destroyed and when it moves
    to another top-level window (image-only fullscreen), after making it
    current. The handler touches only Python objects, never the widget, which
    may already be half destroyed.
    """

    def release():
        if holder[0] is resources:
            holder[0] = None
        if resources.program is None:
            return
        surface = None
        if not _same(QtGui.QOpenGLContext.currentContext(), context):
            surface = QtGui.QOffscreenSurface()
            surface.setFormat(context.format())
            surface.create()
            if not context.makeCurrent(surface):
                surface.destroy()
                resources.program = resources.vao = None
                return
        try:
            resources.destroy()
        finally:
            if surface is not None:
                context.doneCurrent()
                surface.destroy()

    resources.release = release
    context.aboutToBeDestroyed.connect(release, QtCore.Qt.DirectConnection)
    return resources


_gpu_canvas_class = None


def gpu_canvas_class():
    """GpuImageCanvas, defined on first use so classic viewers never import QtOpenGLWidgets."""
    global _gpu_canvas_class
    if _gpu_canvas_class is not None:
        return _gpu_canvas_class
    from PySide6 import QtOpenGLWidgets

    class GpuImageCanvas(CanvasBehaviour, QtOpenGLWidgets.QOpenGLWidget):
        """Draws the view with one OpenGL pass that reproduces the classic canvas's pixels."""

        regionSelected = QtCore.Signal(tuple)
        regionCleared = QtCore.Signal()
        viewChanged = QtCore.Signal()
        fullscreenExitRequested = QtCore.Signal()
        framePainted = QtCore.Signal(object)
        focusPicked = QtCore.Signal(int, int)
        focusCancelled = QtCore.Signal()
        nodePicked = QtCore.Signal(int, int, str)
        pixelInspected = QtCore.Signal(int, int)
        inspectionEnded = QtCore.Signal()

        display_mode = "gpu"

        def __init__(self, parent=None):
            super().__init__(parent)
            self.gl_failure = ""
            self.gl_info = ""
            self.gl_contexts = 0
            self._resources = [None]  # GlResources of the current context.
            self._damage = None
            self._painter_fallback = False

        def note_damage(self, before_key, after_key, rect):
            self._damage = damage_union(self._damage, before_key, after_key, rect)

        def release_display(self):
            resources = self._resources[0]
            if resources is None:
                return
            self.makeCurrent()
            try:
                resources.release()
            finally:
                self.doneCurrent()

        def gl_resources(self):
            """Resources of the current context, or None (QPainter draws instead)."""
            if self._painter_fallback:
                return None
            if self._resources[0] is None:
                try:
                    self._resources[0] = bind_to_context(GlResources(self.context()), self.context(), self._resources)
                    self.gl_info = self._resources[0].info
                    self.gl_contexts += 1
                except Exception as error:
                    self.gl_failure = str(error)
                    self._painter_fallback = True
                    return None
            return self._resources[0]

        def paintEvent(self, event):
            # Defined here, not only inherited: QOpenGLWidget.paintEvent runs
            # paintGL synchronously, so wrapping type(canvas).paintEvent
            # observes a finished paint, as for the classic canvas (see
            # CanvasBehaviour). An inherited C++ paintEvent is not dispatched
            # to a wrapper assigned to the class after the widget exists.
            super().paintEvent(event)

        def paintGL(self):
            resources = self.gl_resources()
            if resources is not None:
                theme, background = self.scene_colors()
                ratio = self.devicePixelRatioF()
                try:
                    damage, self._damage = self._damage, None
                    resources.render(
                        self,
                        theme,
                        background,
                        device_size((self.width(), self.height()), ratio),
                        ratio,
                        damage,
                        self.context().functions(),
                    )
                except Exception as error:
                    self.gl_failure = str(error)
                    self._painter_fallback = True
                    resources.release()
                    resources = None
            if resources is None:  # Qt's OpenGL paint engine: close, not identical.
                painter = QtGui.QPainter(self)
                self.paint_scene(painter)
                painter.end()
            if not self.image.isNull():
                self.framePainted.emit(self.generation)

    _gpu_canvas_class = GpuImageCanvas
    return GpuImageCanvas
