# SPDX-License-Identifier: Apache-2.0
"""Artist-facing, local-only diagnostics export from the normal viewer menu."""

import hashlib
import json
from pathlib import Path
import platform
import sys

import hou
from PySide6 import QtCore, QtWidgets
from ..core.support_diagnostics import snapshot
from ..ipr import session_log


def capture(panel):
    import cycles_houdini as ch

    session = panel.entry.session if panel.entry else None
    collection_errors = {}
    try:
        status = session.status() if session else dict(state="idle")
    except Exception as error:
        status = dict(state="failed", support_status_unavailable=True)
        collection_errors["status"] = str(error)
    try:
        diagnostics = session.diagnostics() if session else {}
    except Exception as error:
        diagnostics = {}
        status["support_diagnostics_unavailable"] = True
        collection_errors["diagnostics"] = str(error)
    identity = dict(
        python_version=list(sys.version_info[:3]),
        houdini_version=list(hou.applicationVersion()),
        os=platform.system(),
        release_id=hou.getenv("CYCLES_HOUDINI_RELEASE_ID"),
    )
    private = dict(
        python_module=ch.__file__,
        host_root=hou.getenv("CYCLES_HOUDINI_HOST_ROOT"),
        resources=hou.getenv("CYCLES_HOUDINI_RESOURCES"),
        scene=hou.hipFile.path(),
        rop=panel._rop.path() if panel._rop else None,
        collection_errors=collection_errors,
    )
    # Only fixed, installed metadata files are inspected; no directory traversal.
    root = Path(private["host_root"]) if private["host_root"] else None
    if root:
        # Installed packages keep host/ beside provenance/ and release metadata.
        # Development hosts may instead carry metadata directly in their root.
        package_root = root.parent if root.name == "host" else root
        for relative in ("release-manifest.json", "provenance/build-manifest.json", "build-manifest.json"):
            path = package_root / relative
            try:
                with path.open("rb") as stream:
                    data = stream.read(4 * 1024 * 1024 + 1)
                if len(data) > 4 * 1024 * 1024:
                    continue
                if "build-manifest" in relative:
                    identity["build_manifest_sha256"] = hashlib.sha256(data).hexdigest()
                else:
                    manifest = json.loads(data)
                    identity["release_manifest_sha256"] = hashlib.sha256(data).hexdigest()
                    identity["package_id"] = manifest.get("package_id")
                    identity["runtime_fingerprint"] = manifest.get("runtime_fingerprint")
                    identity["release_purpose"] = manifest.get("purpose")
                    version = manifest.get("houdini", "")
                    if isinstance(version, str) and all(part.isdecimal() for part in version.split(".")):
                        identity["built_houdini_version"] = [int(part) for part in version.split(".")[:4]]
            except (OSError, ValueError):
                pass
    # Older packages may lack a capability; keep unavailable explicit in preview.
    try:
        capabilities = ch.capabilities()
    except Exception:
        capabilities = {}
    try:
        devices = ch.devices()
    except Exception:
        devices = []
    worker_log = diagnostics.get("controller", {}).get("worker_log")
    return snapshot(
        status=status,
        diagnostics=diagnostics,
        capabilities=capabilities,
        identity=identity,
        private_identity=private,
        worker_log=worker_log,
        session_log=session_log.path(),
        devices=devices,
    )


class CollectDiagnostics(QtWidgets.QDialog):
    def __init__(self, panel, captured=None):
        super().__init__(panel)
        self.panel = panel
        self.captured = captured if captured is not None else capture(panel)
        self.setWindowTitle("Collect Cycles diagnostics")
        self.setObjectName("cycles_collect_diagnostics")
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        self.resize(820, 620)
        layout = QtWidgets.QVBoxLayout(self)
        explanation = QtWidgets.QLabel(
            "Review the exact snapshot below, then save a local ZIP. Nothing is sent automatically. "
            "Scene files, images, textures and environment variables are excluded."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        self.include_private = QtWidgets.QCheckBox("Include private paths, details, and logs")
        self.include_private.setChecked(False)
        self.include_private.setToolTip(
            "Includes scene and node names, installation and resource paths, raw status and diagnostic text, "
            "and up to the last 64 KiB of each worker and session log. Review before sharing."
        )
        layout.addWidget(self.include_private)
        self.preview = QtWidgets.QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setAccessibleName("Exact diagnostics archive contents")
        layout.addWidget(self.preview, 1)
        self.message = QtWidgets.QLabel("Snapshot captured on opening. Reopen to capture the latest status.")
        self.message.setWordWrap(True)
        self.message.setTextFormat(QtCore.Qt.PlainText)
        layout.addWidget(self.message)
        log_path = session_log.path()
        self.session_log = QtWidgets.QLabel("Private session log: " + (str(log_path) if log_path else "off"))
        self.session_log.setToolTip("May contain project paths, node names and stack traces. Review before sharing.")
        self.session_log.setObjectName("cycles_session_log_path")
        self.session_log.setWordWrap(True)
        self.session_log.setTextFormat(QtCore.Qt.PlainText)
        self.session_log.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        layout.addWidget(self.session_log)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        self.save_button = buttons.addButton("Save ZIP…", QtWidgets.QDialogButtonBox.ActionRole)
        buttons.rejected.connect(self.close)
        self.save_button.clicked.connect(self.save)
        layout.addWidget(buttons)
        self.include_private.toggled.connect(self.update_preview)
        self.update_preview()

    def update_preview(self):
        self.preview.setPlainText(self.captured.preview(self.include_private.isChecked()))

    def save_to(self, destination):
        """Save the already-previewed snapshot without recollecting mutable state."""
        self.captured.write(destination, self.include_private.isChecked())
        self.message.setText("Saved local diagnostics ZIP: " + str(destination))

    def save(self):
        destination, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Save local diagnostics",
            "cycles-diagnostics.zip",
            "ZIP archives (*.zip)",
            options=QtWidgets.QFileDialog.DontConfirmOverwrite,
        )
        if not destination:
            return
        if not destination.lower().endswith(".zip"):
            destination += ".zip"
        try:
            self.save_to(destination)
        except Exception as error:
            self.message.setText("Could not save diagnostics: " + str(error))


def show(panel):
    try:
        dialog = CollectDiagnostics(panel)
        dialog.show()
    except Exception as error:
        panel.notify("Could not collect diagnostics: " + str(error))
