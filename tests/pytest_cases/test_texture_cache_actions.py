# SPDX-License-Identifier: Apache-2.0
"""Destructive-boundary tests use disposable directories and synthetic bytes."""

import hashlib
from pathlib import Path

import pytest

from cycles_houdini import texture_cache as cache

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


def derivative(source, revision="a" * 32, *, portable=False, owned=True):
    suffix = ".portable-" if portable else ".7-"
    output = source.parent / "cycles_houdini" / revision / (source.name + suffix + "b" * 32 + ".tx")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"generated derivative bytes")
    if owned:
        marker(source, output)
    return output


def marker(source, output):
    source_id = hashlib.md5(str(source).replace("\\", "/").encode("utf-8")).hexdigest()
    Path(str(output) + ".ch-cache").write_text(
        "\n".join((cache._PRODUCER, source_id, cache._digest(output), str(output.stat().st_size))) + "\n",
        encoding="ascii",
    )


def test_clear_removes_only_owned_stock_and_portable_across_revisions(tmp_path):
    source = tmp_path / "image.exr"
    source.write_bytes(b"original image")
    stock = derivative(source)
    portable = derivative(source, "c" * 32, portable=True)
    legacy = derivative(source, "d" * 32, owned=False)
    other_source = tmp_path / "other.exr"
    other_source.write_bytes(b"unrelated original")
    unrelated = derivative(other_source)
    beside = tmp_path / "image.tx"
    beside.write_bytes(b"original user tx")

    result = cache.clear([dict(source=str(source), destination=str(stock))])

    assert set(result["cleared"]) == {str(stock), str(portable)}
    assert not result["errors"]
    assert source.read_bytes() == b"original image"
    assert beside.read_bytes() == b"original user tx"
    assert legacy.exists() and unrelated.exists()
    assert str(legacy) in result["skipped"]
    assert not Path(str(stock) + ".ch-cache").exists()
    assert stock.parent.is_dir()  # No recursive delete or folder cleanup.


def test_clear_keeps_replaced_contents_and_forged_source_marker(tmp_path):
    source = tmp_path / "image.exr"
    source.write_bytes(b"original")
    changed = derivative(source)
    changed.write_bytes(b"replaced original texture")
    forged = derivative(source, "c" * 32)
    marker(tmp_path / "wrong-source.exr", forged)

    result = cache.clear([dict(source=str(source))])

    assert not result["cleared"]
    assert set(result["skipped"]) == {str(changed), str(forged)}
    assert changed.read_bytes() == b"replaced original texture"


def test_clear_never_deletes_any_scene_source_even_if_it_has_marker(tmp_path):
    source = tmp_path / "image.exr"
    source.write_bytes(b"original")
    generated = derivative(source)
    result = cache.clear([dict(source=str(source)), dict(source=str(generated))])
    assert not result["cleared"]
    assert generated.exists()


def test_generate_uses_opaque_native_plan_and_preserves_unowned_targets(tmp_path):
    source = tmp_path / "image.exr"
    source.write_bytes(b"original")
    unowned = derivative(source, owned=False)
    target = unowned.parent / (source.name + ".portable-" + "c" * 32 + ".tx")
    seen = []

    def native(handle):
        seen.append(handle)
        target.write_bytes(b"new derivative")
        marker(source, target)

    result = cache.generate(
        [
            dict(source=str(source), destination=str(unowned), handle="unowned"),
            dict(source=str(source), destination=str(target), handle="opaque native handle"),
        ],
        native,
    )
    assert seen == ["opaque native handle"]
    assert result["generated"] == [str(target)]
    assert result["skipped"] == [str(unowned)]
    assert not result["errors"]
    assert source.read_bytes() == b"original"


@pytest.mark.parametrize("name", ["image.exr.tx", "image.exr.7-hijack.tx", "other.exr.7-" + "a" * 32 + ".tx"])
def test_generate_rejects_non_native_names(tmp_path, name):
    source = tmp_path / "image.exr"
    target = tmp_path / "cycles_houdini" / ("a" * 32) / name
    result = cache.generate([dict(source=str(source), destination=str(target), handle=None)], lambda _: pytest.fail())
    assert result["errors"] and not target.exists()


def test_clear_rejects_hardlinks(tmp_path, monkeypatch):
    source = tmp_path / "image.exr"
    source.write_bytes(b"original")
    target = derivative(source)
    real_lstat = Path.lstat

    def hardlinked_lstat(path):
        info = real_lstat(path)
        if path == target:
            from types import SimpleNamespace

            return SimpleNamespace(st_mode=info.st_mode, st_nlink=2, st_file_attributes=0)
        return info

    monkeypatch.setattr(Path, "lstat", hardlinked_lstat)
    result = cache.clear([dict(source=str(source))])
    assert not result["cleared"] and result["errors"]
    assert target.exists()


def test_clear_rejects_reparse_directory(tmp_path, monkeypatch):
    source = tmp_path / "image.exr"
    source.write_bytes(b"original")
    target = derivative(source)
    real_lstat = Path.lstat

    def reparse_lstat(path):
        info = real_lstat(path)
        if path == target.parent:
            from types import SimpleNamespace

            return SimpleNamespace(st_mode=info.st_mode, st_nlink=1, st_file_attributes=0x400)
        return info

    monkeypatch.setattr(Path, "lstat", reparse_lstat)
    result = cache.clear([dict(source=str(source))])
    assert not result["cleared"] and result["errors"] and target.exists()


def test_progress_cancellation_stops_before_delete(tmp_path):
    source = tmp_path / "image.exr"
    source.write_bytes(b"original")
    target = derivative(source)

    class Cancelled(Exception):
        pass

    def cancel(*_):
        raise Cancelled()

    with pytest.raises(Cancelled):
        cache.clear([dict(source=str(source))], cancel)
    assert target.exists()


def test_button_reports_plan_failure_without_mutation(monkeypatch):
    from types import SimpleNamespace
    import sys

    class Interrupted(Exception):
        pass

    hou = SimpleNamespace(isUIAvailable=lambda: False, OperationInterrupted=Interrupted)
    monkeypatch.setitem(sys.modules, "hou", hou)
    records = {}
    rop = SimpleNamespace(path=lambda: "/out/cycles", setCachedUserData=lambda key, value: records.update({key: value}))

    def failed_plan(_):
        raise RuntimeError("Missing world image; fix the input and retry")

    monkeypatch.setattr(cache, "plan_from_rop", failed_plan)
    report = cache.clear_from_rop(dict(node=rop))
    assert not report["cleared"]
    assert report["errors"] == [dict(source="/out/cycles", message="Missing world image; fix the input and retry")]
    assert records["cycles_houdini.texture_cache"] is report

    def interrupted_plan(_):
        raise Interrupted()

    monkeypatch.setattr(cache, "plan_from_rop", interrupted_plan)
    with pytest.raises(Interrupted):
        cache.clear_from_rop(dict(node=rop))
