# SPDX-License-Identifier: Apache-2.0
"""Report retries preserve old evidence and never turn a publication failure into a pass."""

import errno
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from support import reports

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


def windows_lock(code):
    error = PermissionError(errno.EACCES, "report is temporarily held by a reader")
    error.winerror = code
    return error


@pytest.mark.parametrize("code", [5, 32, 33])
def test_retry_keeps_previous_report_until_complete_atomic_replacement(tmp_path, monkeypatch, code):
    path = tmp_path / "report.json"
    path.write_text('{"phase":"old"}')
    original_replace = Path.replace
    attempts, sleeps = [], []

    def locked_replace(temporary, target):
        assert target == path
        assert json.loads(path.read_text()) == {"phase": "old"}
        assert json.loads(temporary.read_text()) == {"phase": "new", "checks": [1, 2, 3]}
        attempts.append(temporary)
        if len(attempts) < 3:
            raise windows_lock(code)
        return original_replace(temporary, target)

    monkeypatch.setattr(Path, "replace", locked_replace)
    monkeypatch.setattr(reports.time, "sleep", sleeps.append)
    reports.atomic_json(path, dict(phase="new", checks=[1, 2, 3]))
    assert json.loads(path.read_text()) == {"phase": "new", "checks": [1, 2, 3]}
    assert len(attempts) == 3 and len(set(attempts)) == 1
    assert sleeps == [0.025, 0.05]
    assert not list(tmp_path.glob("*.writing"))


def test_persistent_windows_lock_is_bounded_and_still_raises(tmp_path, monkeypatch):
    path = tmp_path / "report.json"
    path.write_text('{"result":"running"}')
    attempts, sleeps = [], []
    failure = windows_lock(5)

    def locked_replace(temporary, target):
        attempts.append(temporary)
        raise failure

    monkeypatch.setattr(Path, "replace", locked_replace)
    monkeypatch.setattr(reports.time, "sleep", sleeps.append)
    with pytest.raises(PermissionError) as raised:
        reports.atomic_json(path, dict(result="passed"))
    assert raised.value is failure
    assert len(attempts) == 10 and len(sleeps) == 9
    assert sum(sleeps) == pytest.approx(1.375)
    assert json.loads(path.read_text()) == {"result": "running"}
    assert not list(tmp_path.glob("*.writing"))


@pytest.mark.parametrize(
    "failure",
    [OSError(errno.ENOSPC, "disk full"), PermissionError("permanent denial"), AssertionError("contract failure")],
)
def test_unrelated_errors_are_immediate_and_preserve_previous_evidence(tmp_path, monkeypatch, failure):
    path = tmp_path / "report.json"
    path.write_text('{"phase":"old"}')
    attempts, sleeps = [], []

    def fail(temporary, target):
        attempts.append(temporary)
        raise failure

    monkeypatch.setattr(Path, "replace", fail)
    monkeypatch.setattr(reports.time, "sleep", sleeps.append)
    with pytest.raises(type(failure)) as raised:
        reports.atomic_json(path, dict(phase="new"))
    assert raised.value is failure
    assert len(attempts) == 1 and not sleeps
    assert json.loads(path.read_text()) == {"phase": "old"}
    assert not list(tmp_path.glob("*.writing"))


def test_invalid_numerical_evidence_never_replaces_previous_report(tmp_path):
    path = tmp_path / "report.json"
    path.write_text('{"phase":"old"}')
    with pytest.raises(ValueError):
        reports.atomic_json(path, dict(measured=float("nan")))
    assert json.loads(path.read_text()) == {"phase": "old"}
    assert not list(tmp_path.glob("*.writing"))


def test_each_publication_owns_a_unique_temporary_path(tmp_path, monkeypatch):
    path = tmp_path / "report.json"
    original_replace = Path.replace
    temporary_paths = []

    def replace(temporary, target):
        temporary_paths.append(temporary)
        return original_replace(temporary, target)

    monkeypatch.setattr(Path, "replace", replace)
    reports.atomic_json(path, dict(phase="one"))
    reports.atomic_json(path, dict(phase="two"))
    assert len(set(temporary_paths)) == 2
    assert json.loads(path.read_text()) == {"phase": "two"}


def test_temporary_name_collision_never_deletes_unowned_file(tmp_path, monkeypatch):
    path = tmp_path / "report.json"
    path.write_text('{"phase":"old"}')
    temporary = tmp_path / "report.json.collision.writing"
    temporary.write_text("another writer owns this evidence")
    monkeypatch.setattr(reports.uuid, "uuid4", lambda: SimpleNamespace(hex="collision"))
    with pytest.raises(FileExistsError):
        reports.atomic_json(path, dict(phase="new"))
    assert temporary.read_text() == "another writer owns this evidence"
    assert json.loads(path.read_text()) == {"phase": "old"}


@pytest.mark.skipif(sys.platform != "win32", reason="Windows FILE_SHARE_DELETE contract")
def test_real_windows_reader_lock_is_retried_without_truncating_report(tmp_path, monkeypatch):
    import ctypes
    from ctypes import wintypes

    path = tmp_path / "report.json"
    path.write_text('{"phase":"old"}')
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    # Permit reads/writes but withhold delete sharing, as a transient report
    # reader or scanner can do. This denies atomic replace on Windows.
    handle = kernel.CreateFileW(str(path), 0x80000000, 3, None, 3, 0x80, None)
    assert handle != ctypes.c_void_p(-1).value, ctypes.WinError(ctypes.get_last_error())
    sleeps = []

    def release_reader(delay):
        nonlocal handle
        assert json.loads(path.read_text()) == {"phase": "old"}
        sleeps.append(delay)
        assert kernel.CloseHandle(handle)
        handle = None

    monkeypatch.setattr(reports.time, "sleep", release_reader)
    try:
        reports.atomic_json(path, dict(phase="new"))
    finally:
        if handle is not None:
            kernel.CloseHandle(handle)
    assert sleeps == [0.025]
    assert json.loads(path.read_text()) == {"phase": "new"}
    assert not list(tmp_path.glob("*.writing"))
