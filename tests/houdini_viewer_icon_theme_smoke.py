# SPDX-License-Identifier: Apache-2.0
"""Supplied SVG geometry, aspect ratio and real Houdini light/dark themes."""

import json, os, time, traceback
from pathlib import Path
import hou
from PySide6 import QtCore, QtGui, QtWidgets
from hutil.qt.pluto import themes, colors
from cycles_houdini.panel import RenderPanel
from cycles_houdini import viewer_icons, viewer_theme

dest = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
dest.parent.mkdir(parents=True, exist_ok=True)
root = Path(__file__).resolve().parents[1]
provided = root / "UI_mockup/cycles-houdini-icons-new/cycles-houdini-icons"
report = dict(result="running", checks=[], themes=[], runtime=str(viewer_icons.__file__))
p = RenderPanel()
p.resize(1500, 700)
p.show()
p.timer.stop()
available = {md.name: md for md in themes.readMetadatas()}
names = ["Default", "Polar Bear", "Default"]
phase = 0
since = time.monotonic()
busy = False
timer = QtCore.QTimer(hou.qt.mainWindow())
timer.setInterval(100)


def bounds(image):
    points = [
        (x, y) for y in range(image.height()) for x in range(image.width()) if image.pixelColor(x, y).alpha() > 127
    ]
    assert points
    return (min(x for x, y in points), min(y for x, y in points), max(x for x, y in points), max(y for x, y in points))


def finish(error=None):
    timer.stop()
    p.dispose()
    p.close()
    report.update(result="failed" if error else "passed", error=error)
    dest.write_text(json.dumps(report, indent=2))
    hou.ui.postEventCallback(lambda: hou.exit(1 if error else 0, suppress_save_prompt=True))


def tick():
    global phase, since, busy
    if busy:
        return
    busy = True
    try:
        if phase == 0:
            assert len(set(viewer_icons._ASSETS.values())) == 27
            for asset in set(viewer_icons._ASSETS.values()):
                assert (viewer_icons.ASSET_ROOT / (asset + ".svg")).read_bytes() == (
                    provided / "svg/plain" / (asset + ".svg")
                ).read_bytes()
                actual = viewer_icons.rasterize((viewer_icons.ASSET_ROOT / (asset + ".svg")).read_text(), 64).toImage()
                expected = QtGui.QImage(str(provided / "png/plain" / (asset + "_64.png")))
                a, b = bounds(actual), bounds(expected)
                assert all(abs(x - y) <= 2 for x, y in zip(a, b)), (asset, a, b)
            report["checks"].append(
                "All 26 supplied SVGs are exact copies; ink bounds agree with supplied 64px PNG references"
            )
            svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 20"><circle cx="20" cy="10" r="8" fill="white"/></svg>'
            for w, h in ((64, 64), (80, 32), (24, 48)):
                b = bounds(viewer_icons.rasterize(svg, QtCore.QSize(w, h)).toImage())
                assert abs((b[2] - b[0]) - (b[3] - b[1])) <= 1, (w, h, b)
            report["checks"].append(
                "Non-square viewBoxes and non-square output slots preserve circles without squashing"
            )
            colors.setApplicationTheme(available[names[0]].theme)
            since = time.monotonic()
            phase = 1
            return
        if time.monotonic() - since < 0.3:
            return
        name = names[phase - 1]
        p.monitor_scale.update()
        theme = viewer_theme.read_theme()
        node_icons = sorted(viewer_icons.ASSET_ROOT.parents[1].glob("cycles_*.svg"))
        atlas = QtGui.QPixmap(128 * len(node_icons), 192)
        atlas.fill(QtCore.Qt.transparent)
        pixmaps = []
        for path in node_icons:
            pixmap = hou.qt.Icon(path.stem).pixmap(56, 56)
            image = pixmap.toImage()
            backing = image.pixelColor(1, 28)
            # Houdini applies its global icon contrast; inspect opacity and
            # contrast rather than demanding unchanged file RGB values.
            assert backing.alpha() == 255 and max(backing.getRgb()[:3]) < 100, (
                name,
                path.name,
                backing.name(),
                backing.alpha(),
            )
            ink = max(
                (image.pixelColor(x, y) for y in range(8, 48) for x in range(8, 48)), key=lambda c: c.lightnessF()
            )
            assert viewer_theme.contrast_ratio(ink, backing) >= 3, (path.name, ink.name(), backing.name())
            pixmaps.append(pixmap)
        painter = QtGui.QPainter(atlas)
        try:
            for row, bg in enumerate(("#eeeeee", "#202020", "#ffcc00")):
                painter.fillRect(0, row * 64, atlas.width(), 64, QtGui.QColor(bg))
                for column, pixmap in enumerate(pixmaps):
                    painter.drawPixmap(column * 128 + 36, row * 64 + 4, pixmap)
        finally:
            painter.end()
        atlas.save(str(dest.with_name(f"node-badges-{phase}-{name.replace(' ', '_')}.png")))
        if phase == 1:
            rop = hou.node("/out").createNode("cycles_ipr", "icon_asset_check")
            assert rop.type().icon() == "cycles_render_rop", rop.type().icon()
            report["checks"].append(
                "Cycles node icons have an opaque dark badge on light, dark and yellow node colors; ROP resolves its named icon asset"
            )
        # Keep controls available in this no-render UI fixture, then exercise
        # actual QIcon states and actual toolbar geometry at both UI scales.
        from cycles_houdini.viewer_layout import redesign_style

        p.setStyleSheet(redesign_style(theme))
        viewer_theme.apply_theme(p, theme)
        for button in (p.region, p.preview_denoise, p.render_button, p.pause_button, p.stop_button):
            button.setEnabled(True)
        p.preview_denoise.setChecked(True)
        modes = (
            (QtGui.QIcon.Normal, QtGui.QIcon.Off),
            (QtGui.QIcon.Active, QtGui.QIcon.Off),
            (QtGui.QIcon.Normal, QtGui.QIcon.On),
            (QtGui.QIcon.Active, QtGui.QIcon.On),
            (QtGui.QIcon.Disabled, QtGui.QIcon.Off),
            (QtGui.QIcon.Disabled, QtGui.QIcon.On),
        )
        from cycles_houdini.viewer_chrome import apply_metrics

        records = []
        for scale in (1.0, 1.5):
            apply_metrics(p, scale)
            viewer_theme.apply_theme(p, theme)
            for button in (p.region, p.preview_denoise, p.resolution_link, p.keep_image_size, p.render_button):
                assert button.iconSize().width() == button.iconSize().height()
                for mode, state in modes:
                    image = button.icon().pixmap(QtCore.QSize(32, 32), mode, state).toImage()
                    tint = viewer_theme.icon_tint(button, theme, mode, state)
                    ink = [
                        image.pixelColor(x, y)
                        for y in range(image.height())
                        for x in range(image.width())
                        if image.pixelColor(x, y).alpha() > 245
                    ]
                    assert (
                        ink
                        and max(max(abs(a - b) for a, b in zip(c.getRgb()[:3], tint.getRgb()[:3])) for c in ink) <= 1
                    ), (name, button.accessibleName(), mode, state, tint.name())
            for button in (p.resolution_link, p.fit_button, p.keep_image_size, p.rop_jump, p.final_render_button):
                mode = (
                    QtGui.QIcon.Disabled
                    if not button.isEnabled()
                    else QtGui.QIcon.Active
                    if button.underMouse() or button.isDown()
                    else QtGui.QIcon.Normal
                )
                state = QtGui.QIcon.On if button.isChecked() else QtGui.QIcon.Off
                tint = viewer_theme.icon_tint(button, theme, mode, state)
                target = QtWidgets.QStyle.alignedRect(
                    QtCore.Qt.LeftToRight, QtCore.Qt.AlignCenter, button.iconSize(), button.rect()
                )
                expected = QtGui.QPixmap(button.size() * button.devicePixelRatioF())
                expected.setDevicePixelRatio(button.devicePixelRatioF())
                expected.fill(QtCore.Qt.transparent)
                painter = QtGui.QPainter(expected)
                try:
                    button.icon().paint(painter, target, QtCore.Qt.AlignCenter, mode, state)
                finally:
                    painter.end()
                actual = button.grab().toImage()
                expected = expected.toImage()

                def ink_bounds(image, transparent=False):
                    background = image.pixelColor(2, image.height() // 2).getRgb()[:3]
                    direction = [f - b for f, b in zip(tint.getRgb()[:3], background)]
                    norm = max(1, sum(v * v for v in direction))
                    points = []
                    for y in range(2, image.height() - 2):
                        for x in range(2, image.width() - 2):
                            pixel = image.pixelColor(x, y)
                            amount = (
                                pixel.alphaF()
                                if transparent
                                else sum((v - b) * d for v, b, d in zip(pixel.getRgb()[:3], background, direction))
                                / norm
                            )
                            if amount > 0.4:
                                points.append((x, y))
                    assert points, (name, button.accessibleName())
                    return (
                        min(x for x, y in points),
                        min(y for x, y in points),
                        max(x for x, y in points),
                        max(y for x, y in points),
                    )

                a, b = ink_bounds(actual), ink_bounds(expected, True)
                assert all(abs(x - y) <= 2 for x, y in zip(a, b)), (
                    "Off-centre or clipped icon",
                    name,
                    button.accessibleName(),
                    scale,
                    a,
                    b,
                )
            p.toolbar_rows[0].grab().save(str(dest.with_name(f"toolbar-{phase}-{name.replace(' ', '_')}-{scale}.png")))
            p.toolbar_rows[1].grab().save(
                str(dest.with_name(f"image-controls-{phase}-{name.replace(' ', '_')}-{scale}.png"))
            )
            records.append(dict(scale=scale, icon_extent=p.region.iconSize().width()))
        # Even a custom scheme with foreground == background stays readable.
        for bg in ("#ffffff", "#eeeeee", "#777777", "#222222", "#000000"):
            background = QtGui.QColor(bg)
            assert viewer_theme.contrast_ratio(viewer_theme.contrasting(background, background), background) >= 2.99
        report["themes"].append(
            dict(name=name, background=theme["toolbar"].name(), foreground=theme["text"].name(), scales=records)
        )
        if phase == len(names):
            report["checks"].append(
                "Actual Houdini dark/light/dark theme changes update normal/hover/checked/disabled pixels at 100% and 150% UI scale"
            )
            report["checks"].append("Custom low-contrast colors are corrected without changing SVG geometry")
            report["checks"].append(
                "Rendered toolbar icon bounds match centred references, without native style offsets or clipping"
            )
            finish()
            return
        colors.setApplicationTheme(available[names[phase]].theme)
        phase += 1
        since = time.monotonic()
    except BaseException:
        finish(traceback.format_exc())
    finally:
        busy = False


timer.timeout.connect(tick)
timer.start()
