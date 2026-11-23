# SPDX-License-Identifier: Apache-2.0
"""The GPU canvas reproduces the classic canvas's QPainter pixels exactly.

viewer_display computes, per screen column and row, which image pixel and
which checker texel QPainter's raster engine would put there, and the shader
blends with Qt's integer arithmetic. Here the same tables and a numpy copy of
the shader's layering are compared with QPainter itself, over random views
(scales, fractional positions, clips, device pixel ratios, spans longer than
Qt's 2048-pixel fetch buffer). In Houdini, tests/houdini_viewer_display_modes_smoke.py
compares the OpenGL framebuffer with the classic canvas.
"""

import importlib.util
import random
import sys
from pathlib import Path
from types import ModuleType

import numpy as np
import pytest

pytestmark = [pytest.mark.pure, pytest.mark.cpu]

QtCore = pytest.importorskip("PySide6.QtCore")
QtGui = pytest.importorskip("PySide6.QtGui")

VIEWER = Path(__file__).resolve().parents[2] / "houdini/python3.13libs/cycles_houdini/viewer"


@pytest.fixture(scope="module")
def display():
    app = QtGui.QGuiApplication.instance() or QtGui.QGuiApplication(["test"])
    package = ModuleType("viewer_raster_pkg")
    package.__path__ = []
    panel = ModuleType("viewer_raster_pkg.panel")
    panel.CanvasBehaviour = type("CanvasBehaviour", (), {})
    saved = {name: sys.modules.get(name) for name in (package.__name__, panel.__name__)}
    sys.modules[package.__name__] = package
    sys.modules[panel.__name__] = panel
    try:
        spec = importlib.util.spec_from_file_location("viewer_raster_pkg.viewer_display", VIEWER / "viewer_display.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value
    del app


def qimage(array, fmt):
    data = np.ascontiguousarray(array).tobytes()
    return QtGui.QImage(data, array.shape[1], array.shape[0], array.shape[1] * 4, fmt).copy()


def pixels(image):
    image = image.convertToFormat(QtGui.QImage.Format_RGBA8888)
    return (
        np.frombuffer(image.constBits(), np.uint8)
        .reshape(image.height(), image.bytesPerLine())[:, : image.width() * 4]
        .reshape(image.height(), image.width(), 4)
        .astype(np.int64)
        .copy()
    )


def mul(x, a):
    t = x * a
    return (t + (t >> 8) + 128) >> 8


def over(src, dst):
    alpha = src[..., 3:4]
    blended = mul(src[..., :3], alpha) + mul(dst, 255 - alpha)
    return np.where(alpha == 255, src[..., :3], np.where(alpha == 0, dst, blended))


def layer(base, fill, pattern, use_pattern, image, lut):
    image_x, image_y, fill_x, fill_y = lut
    out = base.copy()
    filled = (fill_x[None, :] >= 0) & (fill_y[:, None] >= 0)
    under = pattern[np.clip(fill_y, 0, None)[:, None], np.clip(fill_x, 0, None)[None, :], :3] if use_pattern else fill
    out = np.where(filled[..., None], under, out)
    drawn = (image_x[None, :] >= 0) & (image_y[:, None] >= 0)
    texels = image[np.clip(image_y, 0, None)[:, None], np.clip(image_x, 0, None)[None, :]]
    return np.where(drawn[..., None], over(texels, out), out)


def scene(display, case):
    """QPainter (classic) and the GPU canvas's arithmetic for one view."""
    ratio, device, image, compare, rect, fraction, use_pattern = (
        case[k] for k in ("ratio", "device", "image", "compare", "rect", "fraction", "use_pattern")
    )
    canvas_color, fill_color = QtGui.QColor(43, 44, 45), QtGui.QColor(200, 30, 90)
    checker = QtGui.QImage(16, 16, QtGui.QImage.Format_RGB32)
    checker.fill(QtGui.QColor(48, 48, 48))
    painter = QtGui.QPainter(checker)
    painter.fillRect(0, 0, 8, 8, QtGui.QColor(64, 64, 64))
    painter.fillRect(8, 8, 8, 8, QtGui.QColor(64, 64, 64))
    painter.end()
    brush = QtGui.QBrush(QtGui.QPixmap.fromImage(checker))
    target = QtCore.QRectF(*rect)
    clip = QtCore.QRectF(target.left(), target.top(), target.width() * fraction, target.height())

    out = QtGui.QImage(device[0], device[1], QtGui.QImage.Format_ARGB32_Premultiplied)
    out.setDevicePixelRatio(ratio)
    painter = QtGui.QPainter(out)
    painter.fillRect(QtCore.QRectF(0, 0, device[0] / ratio, device[1] / ratio), canvas_color)
    painter.fillRect(target, fill_color)
    if use_pattern:
        painter.fillRect(target, brush)
    painter.drawImage(target, image)
    if compare is not None:
        painter.save()
        painter.setClipRect(clip)
        painter.fillRect(target, fill_color)
        if use_pattern:
            painter.fillRect(target, brush)
        painter.drawImage(target, compare)
        painter.restore()
    painter.end()
    classic = pixels(out)[..., :3]

    pattern = pixels(brush.textureImage())
    base = np.broadcast_to(np.array([43, 44, 45]), (device[1], device[0], 3))
    fill = np.array([200, 30, 90])
    size = (image.width(), image.height())
    image_x, image_y = display.image_lookup(rect, size, ratio, device)
    fill_x, fill_y = display.pattern_lookup(rect, ratio, device)
    gpu = layer(base, fill, pattern, use_pattern, pixels(image), (image_x, image_y, fill_x, fill_y))
    if compare is not None:
        box = (clip.x(), clip.y(), clip.width(), clip.height())
        compare_x, compare_y = display.image_lookup(rect, (compare.width(), compare.height()), ratio, device, box)
        compare_fx, compare_fy = display.pattern_lookup(rect, ratio, device, box)
        gpu = layer(gpu, fill, pattern, use_pattern, pixels(compare), (compare_x, compare_y, compare_fx, compare_fy))
    return classic, gpu


def random_image(rng, width, height):
    values = rng.integers(0, 256, (height, width, 4))
    values[:, : max(1, width // 3), 3] = 255
    values[:, width // 3 : width // 2, 3] = 0
    return qimage(values.astype(np.uint8), QtGui.QImage.Format_RGBA8888)


@pytest.mark.parametrize("ratio", [1, 1.25, 1.5, 2])
def test_compact_preview_matches_expanded_at_centered_actual_size(display, ratio):
    device = (731, 509)
    logical = (1280, 720)
    storage = (640, 360)
    preview = random_image(np.random.default_rng(7), *storage)
    expanded = qimage(pixels(preview).astype(np.uint8).repeat(2, 0).repeat(2, 1), QtGui.QImage.Format_RGBA8888)
    left, top = ((available - size) / (2 * ratio) for available, size in zip(device, logical, strict=True))
    target = (left, top, logical[0] / ratio, logical[1] / ratio)
    sample = display.compact_sample_rect(target, storage, logical, ratio)

    def paint(image, rect):
        out = QtGui.QImage(*device, QtGui.QImage.Format_ARGB32_Premultiplied)
        out.setDevicePixelRatio(ratio)
        out.fill(0)
        painter = QtGui.QPainter(out)
        painter.drawImage(QtCore.QRectF(*rect), image)
        painter.end()
        return bytes(out.constBits())

    assert paint(preview, sample) == paint(expanded, target)
    compact_lookup = display.image_lookup(sample, storage, ratio, device)
    expanded_lookup = display.image_lookup(target, logical, ratio, device)
    for compact_axis, expanded_axis in zip(compact_lookup, expanded_lookup, strict=True):
        np.testing.assert_array_equal(compact_axis, expanded_axis // 2)


def cases(count, seed):
    rng = np.random.default_rng(seed)
    chooser = random.Random(seed)
    for _ in range(count):
        ratio = chooser.choice([1, 1, 1, 1.25, 1.5, 2])
        device = chooser.choice([(320, 200), (2400, 24)])
        width, height = chooser.randint(1, 260), chooser.randint(1, 120)
        scale = chooser.choice([1, 1, 0.5, 2, 3.7, 0.33, 0.05, 1 / ratio, chooser.uniform(0.02, 12)])
        left = chooser.choice([0, 0.5, 10.5, -7.5, chooser.uniform(-60, 200), 17.25])
        top = chooser.choice([0, 0.5, 7.5, chooser.uniform(-30, 80)])
        compare = random_image(rng, chooser.randint(1, 200), chooser.randint(1, 90)) if chooser.random() < 0.4 else None
        yield dict(
            ratio=ratio,
            device=device,
            image=random_image(rng, width, height),
            compare=compare,
            rect=(left, top, width * scale, height * scale),
            fraction=chooser.choice([0.0, 0.37, 0.5, 1.0, chooser.random()]),
            use_pattern=chooser.random() < 0.7,
        )


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_gpu_arithmetic_matches_qpainter(display, seed):
    failures = []
    for index, case in enumerate(cases(25, seed)):
        classic, gpu = scene(display, case)
        differ = int((classic != gpu).any(axis=2).sum())
        if differ:
            failures.append((index, case["ratio"], case["device"], case["rect"], differ))
    assert not failures, failures


def test_fit_and_actual_size_views(display):
    """The views an artist uses most: fit (stretched, fractional origin) and 1:1."""
    rng = np.random.default_rng(9)
    image = random_image(rng, 320, 180)
    for rect in [(0.0, 101.5, 1641.0, 922.5), (660.5, 226.5, 320.0, 180.0), (-37.25, 21.5, 1184.0, 666.0)]:
        classic, gpu = scene(
            display,
            dict(ratio=1, device=(1641, 633), image=image, compare=None, rect=rect, fraction=0.5, use_pattern=True),
        )
        assert (classic == gpu).all(), rect


def test_device_size_rounds_like_qsize(display):
    assert display.device_size((1641, 633), 1.0) == (1641, 633)
    assert display.device_size((301, 201), 1.5) == (452, 302)
