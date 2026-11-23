# SPDX-License-Identifier: Apache-2.0
import hashlib
import importlib
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import zipfile

import pytest

from cycles_houdini.core.support_diagnostics import DETAIL_LIMIT, LOG_LIMIT, snapshot

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


def captured(tmp_path):
    secret = r"C:\Users\Artist\Secret Project\shot.hip"
    log = tmp_path / "private worker.log"
    log.write_bytes(b"ignored prefix\n" * 10000 + secret.encode())
    session = tmp_path / "private session.log"
    session.write_bytes(b"session prefix\n" * 10000 + b"private shutter-cook details")
    return snapshot(
        status=dict(
            state="failed",
            message=secret,
            error="../relative-secret",
            progress=0.5,
            device_allocation_bytes=100,
            source_diagnostics=[dict(source="/obj/private-node", message=secret)],
            rendering=dict(width=64, samples=8, camera=secret),
            elapsed_seconds=secret,
        ),
        diagnostics=dict(controller=dict(worker_log=str(log), events=[dict(message=secret)])),
        capabilities=dict(image_iris=True, gaussian_splats=True, engine_commit="a" * 40, unsafe=secret),
        identity=dict(
            python_version=[3, 13, 1, secret],
            houdini_version=[22, 0, 466],
            os="Windows",
            release_id="b" * 64,
            package_id=secret,
        ),
        private_identity=dict(scene=secret, resource="//server/project/private", relative="../asset.exr"),
        worker_log=log,
        session_log=session,
    ), secret


def test_safe_omission_and_opt_in_are_real_archive_boundaries(tmp_path):
    value, secret = captured(tmp_path)
    safe = value.files()
    assert set(safe) == {
        "identity.json",
        "capabilities.json",
        "latest-status.json",
        "worker-log-summary.json",
        "diagnostics-summary.json",
        "devices.json",
        "manifest.json",
    }
    text = value.preview()
    for private in (secret, "private-node", "relative-secret", "server/project", "asset.exr", "private worker.log"):
        assert private not in text
    assert "image_iris" in text and '"device_allocation_bytes": 100' in text
    assert "not total GPU usage" in text
    assert json.loads(safe["latest-status.json"])["source_diagnostic_count"] == 1
    assert json.loads(safe["identity.json"])["python_version"] == [3, 13, 1]
    assert secret in value.preview(True).replace("\\\\", "\\")
    for include in (False, True):
        path = tmp_path / f"support-{include}.zip"
        value.write(path, include)
        with zipfile.ZipFile(path) as archive:
            assert {name: archive.read(name) for name in archive.namelist()} == value.files(include)
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["include_private_paths"] is include
            assert manifest["external_transmission"] is False
            for entry in manifest["entries"]:
                data = archive.read(entry["name"])
                assert len(data) == entry["bytes"] and hashlib.sha256(data).hexdigest() == entry["sha256"]
    assert len(value.files(True)["private/worker-tail.log"]) <= LOG_LIMIT
    session_tail = "private/session-tail.log"
    assert session_tail not in safe and "private shutter-cook details" not in text
    assert "private session.log" not in value.preview(True)
    assert value.files(True)[session_tail].endswith(b"private shutter-cook details")
    assert len(value.files(True)[session_tail]) <= LOG_LIMIT
    with pytest.raises(FileExistsError):
        value.write(path)


def test_snapshot_freezes_mutable_inputs_and_bounds_details(tmp_path):
    status = dict(state="paused", message="original")
    value = snapshot(status=status, diagnostics={}, capabilities={}, identity={}, worker_log=tmp_path / "missing")
    preview = value.preview(True)
    status["message"] = "changed"
    assert value.preview(True) == preview
    assert json.loads(value.files()["worker-log-summary.json"])["available"] is False
    huge = snapshot(
        status={}, diagnostics={str(i): ["x" * 4096] * 64 for i in range(128)}, capabilities={}, identity={}
    )
    details = huge.files(True)["private/details.json"]
    assert len(details) <= DETAIL_LIMIT and "limit" in details.decode()


def test_log_preview_normalizes_invalid_utf8_without_exceeding_limit(tmp_path):
    log = tmp_path / "binary.log"
    log.write_bytes(b"\xff" * LOG_LIMIT)
    value = snapshot(status={}, diagnostics={}, capabilities={}, identity={}, worker_log=log)
    data = value.files(True)["private/worker-tail.log"]
    assert len(data) <= LOG_LIMIT
    assert data.decode("utf-8") in value.preview(True)


def test_capture_uses_bounded_installed_provenance_and_latest_status(tmp_path, monkeypatch):
    pytest.importorskip("PySide6.QtWidgets")
    import cycles_houdini as ch

    package = tmp_path / "versions" / ("f" * 64)
    host = package / "host"
    host.mkdir(parents=True)
    (package / "provenance").mkdir(parents=True)
    build = b'{"source_root":"C:/private/source","host_sdk":{"houdini":"22.0.466"}}'
    release = json.dumps(
        dict(
            package_id="d" * 64,
            runtime_fingerprint="e" * 64,
            purpose="internal",
            houdini="22.0.466",
            ignored_private_path="C:/private/artist",
        )
    ).encode()
    (package / "provenance/build-manifest.json").write_bytes(build)
    (package / "release-manifest.json").write_bytes(release)
    fake_hou = ModuleType("hou")
    fake_hou.applicationVersion = lambda: (22, 0, 466)
    fake_hou.getenv = lambda name: {"CYCLES_HOUDINI_HOST_ROOT": str(host), "CYCLES_HOUDINI_RELEASE_ID": "f" * 64}.get(
        name
    )
    fake_hou.hipFile = SimpleNamespace(path=lambda: "C:/private/scene.hip")
    monkeypatch.setitem(sys.modules, "hou", fake_hou)
    module = importlib.import_module("cycles_houdini.viewer.support_dialog")
    monkeypatch.setattr(module, "hou", fake_hou)
    monkeypatch.setattr(ch, "capabilities", lambda: dict(image_iris=True, engine_commit="a" * 40))
    monkeypatch.setattr(ch, "devices", lambda: [dict(id="CPU", backend="CPU", name="private-device-name")])
    session = SimpleNamespace(
        status=lambda: dict(state="complete", current_sample=37, render_device_ids=["CPU"]),
        diagnostics=lambda: dict(display_updates=7, private="C:/private/diagnostic"),
    )
    value = module.capture(SimpleNamespace(entry=SimpleNamespace(session=session), _rop=None))
    identity = json.loads(value.files()["identity.json"])
    assert identity["package_id"] == "d" * 64 and identity["release_id"] == "f" * 64
    assert identity["build_manifest_sha256"] == hashlib.sha256(build).hexdigest()
    assert identity["release_manifest_sha256"] == hashlib.sha256(release).hexdigest()
    assert identity["built_houdini_version"] == [22, 0, 466]
    assert json.loads(value.files()["latest-status.json"])["current_sample"] == 37
    assert json.loads(value.files()["diagnostics-summary.json"])["display_updates"] == 7
    assert json.loads(value.files()["devices.json"]) == [dict(index=0, backend="CPU", selected=True)]
    for secret in ("C:/private", "installed-private-root", "private-device-name", "source_root"):
        assert secret not in value.preview()
    assert "C:/private/scene.hip" in value.preview(True)
    (package / "release-manifest.json").write_bytes(b"x" * (4 * 1024 * 1024 + 1))
    bounded = module.capture(SimpleNamespace(entry=None, _rop=None))
    assert "package_id" not in json.loads(bounded.files()["identity.json"])

    def crashed():
        raise RuntimeError("Worker disconnected; log: C:/private/crash.log")

    session.status = session.diagnostics = crashed
    crashed_value = module.capture(SimpleNamespace(entry=SimpleNamespace(session=session), _rop=None))
    crashed_status = json.loads(crashed_value.files()["latest-status.json"])
    assert crashed_status["support_status_unavailable"] and crashed_status["support_diagnostics_unavailable"]
    assert "crash.log" not in crashed_value.preview() and "crash.log" in crashed_value.preview(True)


def test_global_traversal_budget_and_write_failure_cleanup(tmp_path, monkeypatch):
    visited = []

    class CountingDict(dict):
        def items(self):
            for item in super().items():
                visited.append(1)
                yield item

    branch = "a" * 4096
    for level in range(8):
        branch = CountingDict({str(i): branch for i in range(128)})
    value = snapshot(status={}, diagnostics=branch, capabilities={}, identity={})
    assert len(visited) < 2048
    assert len(value.files(True)["private/details.json"]) <= DETAIL_LIMIT

    def disk_full(*args, **kwargs):
        raise OSError("Disk full")

    monkeypatch.setattr(zipfile.ZipFile, "writestr", disk_full)
    destination = tmp_path / "partial.zip"
    with pytest.raises(OSError, match="Disk full"):
        value.write(destination)
    assert not destination.exists()
    destination.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        value.write(destination)
    assert destination.read_bytes() == b"existing"


def test_dialog_choice_preview_and_saved_bytes_agree(tmp_path, monkeypatch):
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    from PySide6 import QtGui

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    # The offscreen Windows plugin has no system font discovery on some hosts.
    font_file = Path("C:/Windows/Fonts/segoeui.ttf")
    if font_file.is_file():
        QtGui.QFontDatabase.addApplicationFont(str(font_file))
        app.setFont(QtGui.QFont("Segoe UI", 9))
    fake_hou = ModuleType("hou")
    monkeypatch.setitem(sys.modules, "hou", fake_hou)
    module = importlib.import_module("cycles_houdini.viewer.support_dialog")
    # The dialog itself needs no host calls when given the captured snapshot.
    parent = QtWidgets.QWidget()
    value, _ = captured(tmp_path)
    dialog = module.CollectDiagnostics(parent, captured=value)
    dialog.show()
    app.processEvents()
    assert not dialog.include_private.isChecked()
    assert dialog.preview.toPlainText() == value.preview()
    assert dialog.grab().save(str(tmp_path / "diagnostics-dialog.png"))
    for include in (True, False):
        dialog.include_private.setChecked(include)
        app.processEvents()
        assert dialog.preview.toPlainText() == value.preview(include)
        destination = tmp_path / f"dialog-{include}.zip"
        dialog.save_to(destination)
        with zipfile.ZipFile(destination) as archive:
            assert {name: archive.read(name) for name in archive.namelist()} == value.files(include)
    assert dialog.width() >= 800 and dialog.preview.isReadOnly()
    dialog.close()
    parent.close()
    app.processEvents()
