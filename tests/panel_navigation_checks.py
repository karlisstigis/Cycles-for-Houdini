# SPDX-License-Identifier: Apache-2.0
"""Real widget input checks, shared by headless and Houdini GUI workflows."""

import math
from PySide6 import QtCore, QtGui, QtWidgets, QtTest


def check_layout(panel):
    def bounds(widget):
        return QtCore.QRect(widget.mapTo(panel, QtCore.QPoint()), widget.size())

    def visible(widget):
        return widget.isVisibleTo(panel)

    buttons = [bounds(b) for b in (panel.render_button, panel.pause_button, panel.stop_button)]
    assert max(r.center().y() for r in buttons) - min(r.center().y() for r in buttons) <= 2
    if visible(panel.rop_path):
        assert abs(bounds(panel.rop_path).center().y() - buttons[0].center().y()) <= 3
    if visible(panel.preview_resolution):
        resolution_controls = (
            panel.keep_image_size,
            panel.preview_resolution,
            panel.resolution_link,
            panel.zoom_menu,
            panel.actual_pixels,
            panel.fit_button,
        )
        for left, right in zip(resolution_controls, resolution_controls[1:]):
            assert bounds(right).left() > bounds(left).right(), (bounds(left), bounds(right))
    controls = (
        panel.rop_path,
        panel.camera,
        panel.camera_lock,
        panel.focus_pick,
        panel.render_button,
        panel.pause_button,
        panel.stop_button,
        panel.refresh_button,
        panel.mode_menu,
        panel.auto,
        panel.preview_denoise,
        panel.tessellation_update,
        panel.tessellation_refresh,
        panel.preview_resolution,
        panel.keep_image_size,
        panel.resolution_link,
        panel.pass_menu,
        *panel.channel_buttons.values(),
        panel.checker,
        panel.show_noisy,
        panel.region,
        panel.exposure_label,
        panel.exposure,
        panel.gamma_label,
        panel.gamma,
        panel.reset_display,
        panel.actual_pixels,
        panel.fit_button,
        panel.zoom_menu,
        panel.fullscreen_button,
    )
    rectangles = [bounds(w) for w in controls if visible(w)]
    for index, rect in enumerate(rectangles):
        assert panel.rect().contains(rect), (rect, panel.rect())
        assert rect.bottom() < bounds(panel.canvas).top(), rect
        for other in rectangles[index + 1 :]:
            assert not rect.intersects(other), (rect, other)
    if visible(panel.tessellation_update):
        # Subdivision dicing sits right of the Denoise cluster, before the ROP dropdown.
        assert bounds(panel.show_noisy).right() < bounds(panel.tessellation_update).left()
        assert bounds(panel.tessellation_refresh).right() < bounds(panel.rop_path).left()
    # Both rows have one control height (20 px buttons, 22 px dropdowns at a 12 px font).
    assert len({row.height() for row in panel.toolbar_rows}) == 1, [row.height() for row in panel.toolbar_rows]
    assert panel.render_button.height() == panel.checker.height() == panel.tessellation_update.height()
    for row in panel.toolbar_rows:
        margins = row.row.contentsMargins()
        assert (
            row.height()
            <= max(group.sizeHint().height() for group, *_ in row.groups) + margins.top() + margins.bottom() + 2
        )
        assert all(group.isVisibleTo(panel) for group, *_ in row.groups)
        assert row.width() >= row.required_width()
    assert not hasattr(panel, "zoom_info")


def select_zoom(panel, percent):
    index = panel.zoom_menu.findData(float(percent))
    assert index >= 0
    panel.zoom_menu.setFocus()
    QtTest.QTest.keyClick(panel.zoom_menu, QtCore.Qt.Key_Home)
    for _ in range(index):
        QtTest.QTest.keyClick(panel.zoom_menu, QtCore.Qt.Key_Down)
    assert panel.zoom_menu.currentData() == float(percent)


def check_navigation(panel):
    app = QtWidgets.QApplication.instance()
    canvas = panel.canvas
    session = panel.entry.session
    initial = session.status()
    timer_running = panel.timer.isActive()
    panel.timer.stop()
    try:
        check_layout(panel)
        QtTest.QTest.mouseClick(panel.fit_button, QtCore.Qt.LeftButton)
        app.processEvents()
        # Progressive frames refresh this control without changing its scale.
        # Rebuilding the native popup model on each frame causes UI churn.
        model = panel.zoom_menu.model()
        mutations = []

        def inserted(*args):
            mutations.append("insert")

        def removed(*args):
            mutations.append("remove")

        def changed(*args):
            mutations.append("change")

        model.rowsInserted.connect(inserted)
        model.rowsRemoved.connect(removed)
        model.dataChanged.connect(changed)
        try:
            for _ in range(100):
                panel.update_view_controls()
            assert not mutations, mutations
            canvas.set_zoom(123.7)
            mutations.clear()
            for _ in range(100):
                panel.update_view_controls()
            assert not mutations, mutations
        finally:
            model.rowsInserted.disconnect(inserted)
            model.rowsRemoved.disconnect(removed)
            model.dataChanged.disconnect(changed)
        canvas.fit_image()
        app.processEvents()
        geometry = canvas.geometry()
        fitted = canvas.image_rect()
        status_height = panel.info.height()
        messages = [
            "Ready",
            "Rendering sample 4 / 1024",
            "Uploading geometry and preparing shaders: " + "details " * 200,
            "First line\nSecond line\nThird line",
            "Complete",
        ]
        for _ in range(12):
            for message in messages:
                panel.info.setText(message)
                app.processEvents()
                assert canvas.geometry() == geometry, (canvas.geometry(), geometry, message)
                assert canvas.image_rect() == fitted, "Progress text resized the image"
                assert panel.info.height() == status_height
                assert panel.info.toolTip() == message

        anchor = QtCore.QPointF(canvas.width() * 0.63, canvas.height() * 0.43)
        before = canvas.image_rect()
        scale = canvas.view_scale()
        source_before = (anchor - before.topLeft()) / scale
        event = QtGui.QWheelEvent(
            anchor,
            QtCore.QPointF(canvas.mapToGlobal(anchor.toPoint())),
            QtCore.QPoint(),
            QtCore.QPoint(0, 120),
            QtCore.Qt.NoButton,
            QtCore.Qt.NoModifier,
            QtCore.Qt.NoScrollPhase,
            False,
        )
        app.sendEvent(canvas, event)
        app.processEvents()
        assert math.isclose(canvas.view_scale(), scale * 1.2, rel_tol=1e-6)
        source_after = (anchor - canvas.image_rect().topLeft()) / canvas.view_scale()
        assert (source_after - source_before).manhattanLength() < 1e-6, "Wheel zoom moved the pixel under the cursor"
        assert not panel.fit_button.isChecked()
        assert panel.zoom_menu.currentText() == f"{canvas.view_scale() * canvas.devicePixelRatioF() * 100:.1f}%"

        def drag(button, start, end, modifiers=QtCore.Qt.NoModifier):
            QtTest.QTest.mousePress(canvas, button, modifiers, start)
            QtTest.QTest.mouseMove(canvas, end, delay=2)
            QtTest.QTest.mouseRelease(canvas, button, modifiers, end)
            app.processEvents()

        start = QtCore.QPoint(canvas.width() // 2, canvas.height() // 2)
        before = canvas.image_rect()
        drag(QtCore.Qt.MiddleButton, start, start + QtCore.QPoint(37, 19))
        assert (canvas.image_rect().topLeft() - before.topLeft() - QtCore.QPointF(37, 19)).manhattanLength() < 1e-6
        assert canvas.image_rect().size() == before.size()
        scale = canvas.view_scale()
        drag(QtCore.Qt.RightButton, start, start + QtCore.QPoint(36, -24))
        assert canvas.view_scale() > scale

        QtTest.QTest.keyPress(canvas, QtCore.Qt.Key_Space)
        before = canvas.image_rect()
        drag(QtCore.Qt.MiddleButton, start, start + QtCore.QPoint(-17, -11))
        QtTest.QTest.keyRelease(canvas, QtCore.Qt.Key_Space)
        assert (canvas.image_rect().topLeft() - before.topLeft() - QtCore.QPointF(-17, -11)).manhattanLength() < 1e-6
        QtTest.QTest.keyPress(canvas, QtCore.Qt.Key_Space)
        scale = canvas.view_scale()
        drag(QtCore.Qt.RightButton, start, start + QtCore.QPoint(-20, 10))
        QtTest.QTest.keyRelease(canvas, QtCore.Qt.Key_Space)
        assert canvas.view_scale() < scale

        QtTest.QTest.mouseClick(panel.fit_button, QtCore.Qt.LeftButton)
        scale = canvas.view_scale()
        source_center = (QtCore.QPointF(start) - canvas.image_rect().topLeft()) / scale
        drag(
            QtCore.Qt.RightButton,
            start - QtCore.QPoint(50, 40),
            start + QtCore.QPoint(50, 40),
            QtCore.Qt.ControlModifier,
        )
        assert canvas.view_scale() > scale, "Ctrl+right-drag did not box zoom"
        selected_center = canvas.image_rect().topLeft() + source_center * canvas.view_scale()
        assert (selected_center - QtCore.QPointF(canvas.width() / 2, canvas.height() / 2)).manhattanLength() < 1e-6
        for percent in (50, 200, 100):
            select_zoom(panel, percent)
            app.processEvents()
            assert math.isclose(
                canvas.image_rect().width() * canvas.devicePixelRatioF(),
                canvas.full_size[0] * percent / 100,
                abs_tol=1e-6,
            )
            assert math.isclose(
                canvas.image_rect().height() * canvas.devicePixelRatioF(),
                canvas.full_size[1] * percent / 100,
                abs_tol=1e-6,
            )
            assert panel.zoom_menu.currentText() == f"{percent}%"
            assert not panel.fit_button.isChecked()
        QtTest.QTest.keyClick(canvas, QtCore.Qt.Key_Home)
        assert canvas.view_mode == "fit"
        QtTest.QTest.keyClick(canvas, QtCore.Qt.Key_F)
        assert canvas.view_mode == "actual"
        QtTest.QTest.keyClick(canvas, QtCore.Qt.Key_F, QtCore.Qt.ShiftModifier)
        assert canvas.view_mode == "fit"
        select_zoom(panel, 100)
        drag(QtCore.Qt.MiddleButton, start, start + QtCore.QPoint(22, -14))
        # Progressive tiles, pass/exposure changes and snapshots must not home
        # the image after the artist has chosen a view.
        target = canvas.image_rect()
        panel.display_changed()
        panel.poll()
        assert canvas.image_rect() == target
        assert panel.checker.text() == "" and not panel.checker.icon().isNull()
        checked = panel.checker.isChecked()
        QtTest.QTest.mouseClick(panel.checker, QtCore.Qt.LeftButton)
        assert panel.canvas.checker_enabled == (not checked)
        QtTest.QTest.mouseClick(panel.checker, QtCore.Qt.LeftButton)
        assert panel.canvas.checker_enabled == checked
        check_layout(panel)
        assert not hasattr(panel, "device") and not hasattr(panel, "settings_button")
        assert not any(button.text().lower() == "full frame" for button in panel.findChildren(QtWidgets.QPushButton))
        after = session.status()
        for name in ("generation", "geometry_conversions"):
            assert after[name] == initial[name], f"Image navigation changed {name}"
        return dict(
            status_changes=60,
            status_height=status_height,
            unchanged_zoom_refreshes=200,
            zoom_model_mutations=0,
            canvas_size=[canvas.width(), canvas.height()],
            pixel_ratio=canvas.devicePixelRatioF(),
            inputs=[
                "wheel at cursor",
                "middle pan",
                "right zoom",
                "Space+middle",
                "Space+right",
                "Ctrl+right box zoom",
                "Fit",
                "zoom dropdown 50/200/100%",
                "Home",
                "F",
                "Shift+F",
            ],
            generation=after["generation"],
            geometry_conversions=after["geometry_conversions"],
            view_rect=[target.x(), target.y(), target.width(), target.height()],
        )
    finally:
        if timer_running:
            panel.timer.start()


def draw_region(panel):
    """Draw a screen-space region after pan/zoom and return expected render ROI."""
    canvas = panel.canvas
    target = canvas.image_rect()
    visible = target.intersected(QtCore.QRectF(canvas.rect()))
    start = visible.center().toPoint() - QtCore.QPoint(35, 20)
    end = visible.center().toPoint() + QtCore.QPoint(35, 20)
    w, h = canvas.full_size
    scale = canvas.view_scale()
    x0 = math.floor((start.x() - target.left()) / scale)
    x1 = math.ceil((end.x() - target.left()) / scale)
    y0 = h - math.ceil((end.y() - target.top()) / scale)
    y1 = h - math.floor((start.y() - target.top()) / scale)
    QtTest.QTest.mousePress(canvas, QtCore.Qt.LeftButton, QtCore.Qt.ShiftModifier, start)
    QtTest.QTest.mouseMove(canvas, end, delay=2)
    QtTest.QTest.mouseRelease(canvas, QtCore.Qt.LeftButton, QtCore.Qt.ShiftModifier, end)
    return (x0, y0, x1 - x0, y1 - y0)
