# SPDX-License-Identifier: Apache-2.0
"""Compact render viewer chrome matching the artist's two-row reference."""

try:
    import hou
except ImportError:  # Pure-Qt tests of Cycles panels.
    hou = None
from PySide6 import QtCore, QtGui, QtWidgets
from .viewer_toolbar import ToolbarRow
from .viewer_chrome import cluster, dimensions, apply_metrics, ViewerCombo, ViewerToolButton


def icon(button, name, title, fallback=QtWidgets.QStyle.SP_FileDialogDetailedView, label=""):
    button.setAccessibleName(title)
    if not button.toolTip():
        button.setToolTip(title)
    button.setText(label)
    button.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon if label else QtCore.Qt.ToolButtonIconOnly)
    from .viewer_icons import glyph

    source = glyph(name)
    if source.isNull():
        source = hou.qt.Icon(name) if hou is not None and hou.isUIAvailable() else button.style().standardIcon(fallback)
    button.setIcon(source)
    button.setIconSize(QtCore.QSize(14, 14))
    button.setProperty("cycles_glyph", name)
    button.setProperty("cycles_view_toggle", True)
    button.setAttribute(QtCore.Qt.WA_Hover, True)
    button.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)


def redesign_style(theme=None):
    from .viewer_theme import read_theme
    from string import Template

    theme = theme or read_theme()
    return Template("""
    QWidget[cycles_render_panel="true"], QWidget[cycles_render_panel="true"] QWidget { font-family: "Segoe UI"; font-size: 12px; }
    QWidget[cycles_render_panel="true"] { background: $background; color: $text; }
    QWidget[cycles_toolbar="true"] { background: $toolbar; border-bottom: 1px solid $divider; }
    QWidget[cycles_render_panel="true"] QToolButton { background: transparent; border: 1px solid transparent;
        border-radius: 4px; margin: 0px; padding: 0px 4px; color: $text; }
    QWidget[cycles_render_panel="true"] QToolButton:hover:enabled { background: $hover; border-color: $hover_border; }
    QWidget[cycles_render_panel="true"] QToolButton:checked:enabled { background: $active; border-color: $active_border; color: $active_text; }
    QWidget[cycles_render_panel="true"] QToolButton:checked:hover:enabled { background: $active_hover; }
    QWidget[cycles_render_panel="true"] QToolButton:pressed:enabled { background: $pressed; border-color: $active_border; color: $active_text; }
    QWidget[cycles_render_panel="true"] QToolButton:disabled { color: $disabled; }
    QWidget[cycles_render_panel="true"] QToolButton::menu-indicator { image: none; width: 0px; height: 0px; }
    QWidget[cycles_render_panel="true"] QToolButton[cycles_primary="true"] { font-weight: bold; background: $button; border-color: $border; }
    QWidget[cycles_render_panel="true"] QToolButton[cycles_primary="true"]:hover:enabled { background: $hover; border-color: $hover_border; }
    QWidget[cycles_render_panel="true"] QToolButton[cycles_primary="true"]:disabled { background: $toolbar; color: $disabled; }
    QWidget[cycles_render_panel="true"] QComboBox, QWidget[cycles_render_panel="true"] QLineEdit {
        background: $field; border: 1px solid $border; border-radius: 4px; padding: 0px 4px; color: $text; }
    QWidget[cycles_render_panel="true"] QComboBox { padding-right: 16px; }
    QWidget[cycles_render_panel="true"] QComboBox::drop-down { width: 16px; border: none; background: transparent; }
    QWidget[cycles_cluster="field"] { background: $numeric; border: 1px solid $border; border-radius: 4px; }
    QWidget[cycles_cluster="dropdown"] { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 $dropdown_hi, stop:1 $dropdown_lo);
        border: 1px solid $border; border-radius: 4px; }
    QWidget[cycles_cluster="segment"] { background: $toolbar; border: 1px solid $border; border-radius: 5px; }
    QWidget[cycles_cluster="transport"] { background: $divider; border: none; border-radius: 4px; }
    QWidget[cycles_cluster="transport"] QToolButton { background: $field; border: none; border-radius: 0px; padding: 0px 5px; }
    QWidget[cycles_cluster="transport"] QComboBox { background: $field; border: none; border-radius: 0px; }
    QWidget[cycles_cluster="transport"] QToolButton[cycles_glyph="IMAGE_zoom_100"] { padding: 0px; }
    QWidget[cycles_cluster="transport"] QToolButton:hover:enabled { background: $hover; }
    QWidget[cycles_cluster="transport"] QToolButton[cycles_glyph="BUTTONS_resize_mode_space"]:checked:disabled { background: $field; color: $disabled; }
    QWidget[cycles_cluster="transport"] QToolButton[cycles_transport_start="true"] { border-top-left-radius: 4px; border-bottom-left-radius: 4px; }
    QWidget[cycles_cluster="transport"] QToolButton[cycles_primary="true"] { background: $active; color: $active_text; border-top-left-radius: 4px; border-bottom-left-radius: 4px; }
    QWidget[cycles_cluster="transport"] QToolButton[cycles_primary="true"]:hover:enabled { background: $active_hover; }
    QWidget[cycles_cluster="transport"] QToolButton[cycles_primary="true"]:disabled { background: $pressed; }
    QWidget[cycles_cluster="transport"] QToolButton[cycles_transport_end="true"] { border-top-right-radius: 4px; border-bottom-right-radius: 4px; }
    QWidget[cycles_cluster="transport"][cycles_compact="true"] { background: $toolbar; border: 1px solid $border; border-radius: 5px; }
    QWidget[cycles_cluster="transport"][cycles_compact="true"] QToolButton { padding: 0px; }
    QWidget[cycles_render_panel="true"] QToolButton[cycles_glyph="IMAGE_final_render"] { background: $button; border: none; padding: 0px; }
    QWidget[cycles_render_panel="true"] QLineEdit[cycles_embedded="true"],
    QWidget[cycles_render_panel="true"] QComboBox[cycles_embedded="true"],
    QWidget[cycles_cluster="field"] QLineEdit, QWidget[cycles_cluster="field"] QDoubleSpinBox,
    QWidget[cycles_cluster="dropdown"] QLineEdit {
        background: transparent; border: none; border-radius: 0px; padding: 0px; }
    QWidget[cycles_numeric="true"], QWidget[cycles_numeric="true"] QWidget {
        background: $numeric; color: $text; border: none; border-radius: 0px; padding: 0px; }
    QWidget[cycles_render_panel="true"] QComboBox[cycles_embedded="true"] { padding-right: 16px; }
    QWidget[cycles_cluster="segment"] QToolButton { border-radius: 3px; padding: 0px 3px; }
    QWidget[cycles_render_panel="true"] QComboBox:disabled, QWidget[cycles_render_panel="true"] QLineEdit:disabled { color: $disabled; }
    QWidget[cycles_render_panel="true"] QLabel { background: transparent; border: none; color: $text; }
    QWidget[cycles_render_panel="true"] QLabel[cycles_warning="true"] { color: $warning; }
    QWidget[cycles_render_panel="true"] QDoubleSpinBox { background: $field; border: 1px solid $border; color: $text; border-radius: 4px; }
    QWidget[cycles_render_panel="true"] QProgressBar { background: $border; border: none; border-radius: 2px; }
    QWidget[cycles_render_panel="true"] QProgressBar::chunk { background: $progress; border-radius: 2px; }
    QWidget[cycles_render_panel="true"] QAbstractItemView { background: $menu; color: $text; border: 1px solid $border;
        selection-background-color: $selection; selection-color: $selection_text; }
    """).substitute({key: value.name() for key, value in theme.items()})


def scoped_style(scope, theme=None):
    """The render viewer's chrome rules for another Cycles panel root marked with property `scope`.

    One design language: every Cycles panel uses the same theme roles, shapes
    and metrics; only the selector that scopes the rules to a panel differs.
    """
    return redesign_style(theme).replace('[cycles_render_panel="true"]', f'[{scope}="true"]')


class ViewerStatus(QtWidgets.QWidget):
    def __init__(self, p):
        super().__init__()
        self.panel = p
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(6, 0, 6, 0)
        row.setSpacing(10)
        p.info.setProperty("cycles_status_headline", True)
        p.info.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Fixed)
        row.addWidget(p.info)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress.setFixedSize(90, 4)
        self.warning = QtWidgets.QLabel()
        self.warning.setProperty("cycles_warning", True)
        row.addWidget(self.warning)
        self.warning.hide()
        from .panel import StatusLine

        self.metrics = StatusLine()
        self.metrics.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Fixed)
        row.addWidget(self.metrics)
        row.addWidget(self.progress)
        row.addStretch(1)
        p.memory_info.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Fixed)
        row.addWidget(p.memory_info)
        self.denoiser = QtWidgets.QLabel()
        row.addWidget(self.denoiser)
        row.addWidget(p.diagnostics_menu)
        p.info.textChanged.connect(self.message)

    def message(self, text):
        # Keep the full diagnostic on hover; visible status is a short headline.
        from .viewer_timing import summary

        headline = summary(text)
        self.panel.info.setAccessibleName(headline)

    def update_status(self, status):
        self.progress.setValue(round(status.get("progress", 0) * 1000))
        issues = status.get("source_diagnostics", [])
        motion = status.get("motion_warnings", [])
        count = sum(issue.get("severity", "warning") != "info" for issue in issues) + len(motion)
        self.warning.setText(f"⚠ {count}" if count else "")
        self.warning.setVisible(bool(count))
        self.warning.setToolTip(
            "\n\n".join(f"{issue['source']}\n{issue['message']}\n{issue['action']}" for issue in issues)
            + "\n"
            + "\n".join(motion)
        )
        samples = status.get("rendering", {}).get("samples", 0)
        text = (
            f"Samples {status.get('current_sample', 0)} / {samples}    "
            f"First image {status.get('first_image_seconds', 0):.3f} s    "
            f"Cook {status.get('cooking_seconds', 0):.3f} s    "
            f"Extract {status.get('extraction_seconds', 0):.3f} s    "
            f"Buffers {status.get('device_allocation_bytes', 0) / 2**20:.1f} MiB    "
            f"Geo conversions {status.get('geometry_conversions', 0)}"
        )
        # Before the first sample the stage text takes the row (Blender's way); counters return with sampling.
        self._waiting = status.get("state") in ("rendering", "paused") and not status.get("current_sample")
        self.metrics.setText(
            "" if self._waiting else f"Samples {status.get('current_sample', 0)} / {samples}", tooltip=text
        )
        self.progress.setVisible(self.width() > 640 and not self._waiting)
        self.denoiser.setText(
            {"oidn_gpu": "OIDN · GPU", "oidn_cpu": "OIDN · CPU", "dlss": "DLSS · GPU", "optix": "OptiX"}.get(
                status.get("denoiser"), ""
            )
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.progress.setVisible(self.width() > 640 and not getattr(self, "_waiting", False))
        self.metrics.setVisible(self.width() > 640)


def apply(p):
    root = p.layout()

    def clear(layout):
        while layout.count():
            item = layout.takeAt(0)
            if item.layout():
                clear(item.layout())
                item.layout().deleteLater()
            elif item.widget():
                item.widget().hide()

    clear(root)
    root.setContentsMargins(0, 0, 0, 0)
    root.setSpacing(0)
    p.setProperty("cycles_render_panel", True)
    p.setStyleSheet(redesign_style())

    def toolbar():
        result = ToolbarRow()
        result.setProperty("cycles_toolbar", True)
        # One compact gap between wrappers everywhere: between groups as inside them.
        result.row.setSpacing(3)
        root.addWidget(result)
        return result

    def button(name, title, callback, label=""):
        result = ViewerToolButton()
        icon(result, name, title, label=label)
        result.clicked.connect(callback)
        return result

    for control, name, title, label in (
        (p.final_render_button, "IMAGE_final_render", "Final render ROP frame range to disk", ""),
        (p.render_button, "BUTTONS_render", "Start IPR", ""),
        (p.pause_button, "BUTTONS_pause", "Pause rendering", ""),
        (p.stop_button, "BUTTONS_stop", "Stop rendering", ""),
        (p.refresh_button, "BUTTONS_reload", "Refresh IPR", ""),
        (p.auto, "BUTTONS_cook", "Auto update", "Auto"),
        (p.region, "IMAGE_region", "Render region", ""),
        (p.preview_denoise, "IMAGE_denoise", "Denoise", ""),
        (p.clay, "SHOP_material", "Clay render", ""),
        (p.camera_lock, "BUTTONS_lock_closed", "Lock render camera", ""),
        (p.focus_pick, "BUTTONS_image_color_picker", "Pick focus distance", ""),
        (p.show_noisy, "IMAGE_diff_compare", "Show noisy Beauty", ""),
        (p.tessellation_update, "IMAGE_tessellation_update", "Adaptive Subdivision Update", ""),
        (p.tessellation_refresh, "BUTTONS_reload", "Refresh Tessellation", ""),
        (p.fit_button, "IMAGE_fit_to_window", "Fit image", ""),
        (p.snapshot_button, "IMAGE_snapshot_add", "Take snapshot", ""),
        (p.save_button, "BUTTONS_save_image", "Save image to the ROP output folder", ""),
    ):
        icon(control, name, title, label=label)
    icon(p.fullscreen_button, "BUTTONS_maximize", "Fullscreen image")
    p.render_button.setProperty("cycles_primary", True)
    p.auto.setProperty("cycles_transport_end", True)
    # Joined buttons are segmented controls: the first rounds its left corners, the
    # last its right corners (style sheet rules on these two properties).
    p.preview_denoise.setProperty("cycles_transport_start", True)
    p.show_noisy.setProperty("cycles_transport_end", True)
    # Every top-row control has the second row's control height: 20 px
    # buttons in compact clusters (1 px inset in a 24 px wrapper), 22 px
    # dropdowns. Standalone buttons take 20 px too, not the 24 px default.
    for control in (p.final_render_button, p.render_button, p.pause_button, p.stop_button, p.refresh_button):
        dimensions(control, 20, 20)
    for control in (p.region, p.clay):
        dimensions(control, 20, 20)
        # Framed like Denoise: a one-button compact transport cluster, both ends rounded.
        control.setProperty("cycles_transport_start", True)
        control.setProperty("cycles_transport_end", True)
    transport = toolbar()
    transport.group(
        p.final_render_button,
        cluster(
            p.render_button, p.pause_button, p.stop_button, p.refresh_button, p.auto, kind="transport", compact=True
        ),
        cluster(p.region, kind="transport", compact=True),
        priority=100,
    )
    transport.group(
        cluster(p.preview_denoise, p.show_noisy, kind="transport", compact=True),
        cluster(p.clay, kind="transport", compact=True),
        priority=80,
    )
    # Subdivision dicing follows Denoise, in the same wrapper as the Render and Denoise clusters.
    p.tessellation_update.setProperty("cycles_transport_start", True)
    p.tessellation_refresh.setProperty("cycles_transport_end", True)
    transport.group(cluster(p.tessellation_update, p.tessellation_refresh, kind="transport", compact=True), priority=20)

    def glyph_label(name, tooltip):
        from .viewer_icons import glyph

        label = QtWidgets.QLabel()
        label.setProperty("cycles_glyph", name)
        label.setAlignment(QtCore.Qt.AlignCenter)
        label.setPixmap(glyph(name).pixmap(14, 14))
        label.setToolTip(tooltip)
        dimensions(label, 14, 14)
        return label

    for edit, width in ((p.rop_path, 144), (p.camera, 128)):
        dimensions(edit, width, 22)
        edit.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)
    for jump, title in ((p.rop_jump, "Jump to render node"), (p.camera_jump, "Jump to camera")):
        icon(jump, "BUTTONS_jump_to_node", title)
        dimensions(jump, 20, 20)
    for control in (p.camera_lock, p.focus_pick):
        dimensions(control, 20, 20)
    p.rop_path.setToolTip("Choose an available Cycles render node.")
    p.camera.setToolTip("Choose a scene camera for the selected render node.")
    transport.group(cluster(glyph_label("IMAGE_rop", "Render node (ROP)"), p.rop_path, p.rop_jump), priority=90)
    # Lock and Pick Focus sit inside the camera wrapper, after its jump button:
    # the wrapper's left end is at the camera dropdown, its right end after Focus.
    transport.group(
        cluster(glyph_label("IMAGE_camera", "Render camera"), p.camera, p.camera_jump, p.camera_lock, p.focus_pick),
        priority=30,
    )
    p.mode_menu = ViewerCombo()
    p.mode_menu.addItems(["Interactive", "Final"])
    p.mode_menu.setToolTip(
        "Use the selected ROP Interactive or Final sampling settings. Viewer resolution stays independent; the clapper uses final output resolution."
    )
    p.mode_menu.currentIndexChanged.connect(lambda index: p.preview_interactive.setChecked(index == 0))

    def mode_changed(enabled):
        p.mode_menu.blockSignals(True)
        p.mode_menu.setCurrentIndex(0 if enabled else 1)
        p.mode_menu.blockSignals(False)

    p.preview_interactive.toggled.connect(mode_changed)
    mode_changed(p.preview_interactive.isChecked())
    dimensions(p.mode_menu, 82, 22)
    transport.group(
        cluster(glyph_label("IMAGE_settings", "Render settings"), QtWidgets.QLabel("Mode"), p.mode_menu), priority=20
    )
    p.help_button = button(
        "BUTTONS_help", "Cycles Viewer Help (F1)", lambda: hou.ui.curDesktop().displayHelpPath("/cycles/viewer")
    )
    for control in (p.help_button, p.fullscreen_button):
        dimensions(control, 20, 20)
    transport.group(p.help_button, p.fullscreen_button, align_right=True, priority=60)
    image = toolbar()
    for combo, width in (
        (p.pass_menu, 112),
        (p.zoom_menu, 68),
        (p.preview_resolution, 68),
        (p.display, 174),
        (p.view, 232),
        (p.snapshot_menu, 84),
    ):
        dimensions(combo, width, 22)
        combo.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
    p.channel_buttons = {}
    p.channel_group = QtWidgets.QButtonGroup(p)
    for label, token in (("RGBA", "rgba"), ("RGB", "rgb"), ("R", "r"), ("G", "g"), ("B", "b"), ("A", "alpha")):
        control = ViewerToolButton()
        control.setText(label)
        control.setCheckable(True)
        control.setProperty("cycles_view_toggle", True)
        control.setAccessibleName(label + " channel")
        control.setToolTip(
            "Show " + ("alpha coverage without a color transform" if token == "alpha" else label + " display channels")
        )
        control.clicked.connect(lambda checked=False, key=token: p.channels.setCurrentIndex(p.channels.findData(key)))
        p.channel_group.addButton(control)
        p.channel_buttons[token] = control

    def sync_channels(*_):
        for token, control in p.channel_buttons.items():
            control.setChecked(p.channels.currentData() == token)
            control.setEnabled(p.channels.isEnabled() and (token != "alpha" or p._pass_alpha))

    p.sync_channel_buttons = sync_channels
    p.channels.currentIndexChanged.connect(sync_channels)
    sync_channels()
    icon(p.checker, "IMAGE_checker", "Checkerboard")
    p.checker.setProperty("cycles_transport_start", True)
    p.background_button.swatch.setProperty("cycles_transport_end", True)
    p.channel_wrapper = cluster(*p.channel_buttons.values(), p.channel_alpha, kind="segment")
    image.group(
        cluster(glyph_label("IMAGE_aov", "Render pass"), QtWidgets.QLabel("AOV"), p.pass_menu),
        p.channel_wrapper,
        cluster(p.checker, p.background_button, p.background_button.swatch, kind="transport", compact=True),
        priority=90,
    )
    p.reset_display = button("BUTTONS_revert", "Reset exposure and gamma", p.reset_display_adjustments)
    dimensions(p.reset_display, 20, 20)
    for field in (p.exposure, p.gamma):
        field.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)
    image.group(
        cluster(glyph_label("IMAGE_exposure", "Exposure"), p.exposure_label, p.exposure),
        cluster(glyph_label("IMAGE_gamma", "Gamma"), p.gamma_label, p.gamma),
        p.reset_display,
        priority=20,
    )
    p.keep_image_size = ViewerToolButton()
    p.keep_image_size.setCheckable(True)
    icon(p.keep_image_size, "BUTTONS_resize_mode_space", "Keep image size when resolution changes")
    p.keep_image_size.setToolTip(
        "Keep display size and pan when render resolution changes. Zoom is relative to the reference resolution; output files are unchanged."
    )
    p.keep_image_size.toggled.connect(p.canvas.set_keep_image_size)
    p.resolution_link = ViewerToolButton()
    p.resolution_link.setCheckable(True)
    icon(p.resolution_link, "BUTTONS_resolution_link", "Link render resolution to display zoom")
    p.resolution_link.setToolTip(
        "Match viewer render resolution to the displayed zoom, including Fit and fractional percentages, using ROP Width/Height as the reference. Updates after navigation settles. Holds display size while linked; does not change disk or clapper resolution. Uses the supported viewer range up to 400%."
    )
    p.zoom_menu.setToolTip(
        "Display zoom. 100% uses one physical display pixel per render pixel, or the reference resolution when Keep Image Size is enabled."
    )
    p.actual_pixels = button("IMAGE_zoom_100", "Actual pixels (1:1). F.", lambda: p.canvas.set_zoom(100), label="1:1")
    p.actual_pixels.setIcon(QtGui.QIcon())
    p.actual_pixels.setToolButtonStyle(QtCore.Qt.ToolButtonTextOnly)
    p.actual_pixels.setCheckable(True)
    p.keep_image_size.setProperty("cycles_transport_start", True)
    p.fit_button.setProperty("cycles_transport_end", True)
    p.resolution_group = cluster(
        p.keep_image_size,
        p.preview_resolution,
        p.resolution_link,
        p.zoom_menu,
        p.actual_pixels,
        p.fit_button,
        kind="transport",
        compact=True,
    )
    dimensions(p.preview_resolution, 68, 20)
    dimensions(p.zoom_menu, 68, 20)
    image.group(p.resolution_group, align_right=True, priority=100)
    p.toolbar_rows = (transport, image)
    root.addWidget(p.canvas, 1)
    p.canvas.show()
    color = toolbar()
    color.group(
        cluster(glyph_label("IMAGE_display", "Display"), QtWidgets.QLabel("Display"), p.display),
        cluster(glyph_label("IMAGE_view", "View transform"), QtWidgets.QLabel("View"), p.view),
        priority=100,
    )
    p.display.setToolTip("Display from Houdini’s active OCIO configuration. Display only; saved output is unaffected.")
    p.view.setToolTip("View transform from Houdini’s active OCIO configuration.")
    history = toolbar()
    p.snapshot_compare = button(
        "IMAGE_diff_compare", "Compare snapshot with Live", p.toggle_snapshot_compare, label="Compare"
    )
    p.snapshot_compare.setCheckable(True)
    p.snapshot_compare.setEnabled(False)
    p.snapshot_compare.setToolTip(
        "Compare a snapshot on the left with Live on the right. Drag the divider. Select the reference with the history slider. Display only; never restarts rendering."
    )
    history.group(p.snapshot_button, cluster(p.snapshot_menu), p.snapshot_compare, priority=90)
    history.group(p.history_slider, p.history_number, stretch=True, priority=100)
    p.output_folder_button = button("BUTTONS_folder", "Open saved images folder", p.open_output_folder)
    p.output_folder_button.setToolTip("Open the latest saved image folder for this ROP, or its Output Picture folder.")
    dimensions(p.save_format, 68, 24)
    history.group(p.save_button, p.save_format, p.output_folder_button, priority=50)
    p.diagnostics_menu = ViewerToolButton()
    p.diagnostics_menu.setPopupMode(QtWidgets.QToolButton.InstantPopup)
    icon(p.diagnostics_menu, "BUTTONS_info", "Viewer diagnostics")
    menu = QtWidgets.QMenu(p.diagnostics_menu)
    for control in (p.memory_details, p.texture_details):
        action = menu.addAction(control.text())
        action.setCheckable(True)
        action.setChecked(control.isChecked())
        action.toggled.connect(control.setChecked)
        control.toggled.connect(action.setChecked)
    from ..materials.material_previews import show_warnings

    menu.addSeparator()
    menu.addAction("Material Preview Warnings", show_warnings)
    from .support_dialog import show as collect_diagnostics

    menu.addAction("Collect diagnostics…", lambda: collect_diagnostics(p))
    from .viewer_diagnostics import copy as copy_diagnostics

    p.copy_diagnostics_action = copy_action = menu.addAction("Copy diagnostics", lambda: copy_diagnostics(p))
    copy_action.setObjectName("cycles_copy_diagnostics")
    copy_action.setToolTip(
        "Copy a plain-text report of viewers, render sessions, renderer processes and recent update times."
    )
    p.diagnostics_menu.setMenu(menu)
    p.viewer_status = ViewerStatus(p)
    root.addWidget(p.viewer_status)
    p.info.show()
    root.addWidget(p.texture_info)
    p.all_toolbar_rows = (transport, image, color, history)
    for widget, name in (
        (p.rop_path, "Render node"),
        (p.camera, "Render camera"),
        (p.mode_menu, "Render settings mode"),
        (p.pass_menu, "Render pass"),
        (p.preview_resolution, "Render resolution"),
        (p.zoom_menu, "Display zoom"),
        (p.display, "OCIO display"),
        (p.view, "OCIO view transform"),
        (p.snapshot_menu, "Snapshot"),
        (p.exposure.input, "Exposure"),
        (p.gamma.input, "Gamma"),
        (p.history_slider, "Snapshot history"),
    ):
        widget.setAccessibleName(name)
    p.render_controls.hide()
    p.channels.hide()
    p.preview_interactive.hide()
    apply_metrics(p)
    from .viewer_theme import apply_theme, read_theme

    apply_theme(p, read_theme())
