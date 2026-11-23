# SPDX-License-Identifier: Apache-2.0
"""The optional component is independent of finding or launching Blender."""

import re
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cycles_houdini.core import blender_bridge as bridge

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


@pytest.fixture(autouse=True)
def isolated_bridge(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, "_host", lambda: None)
    for name in ("CYCLES_HOUDINI_BLENDER", "CYCLES_HOUDINI_BLENDER_BRIDGE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "empty-program-files"))
    bridge._discover.cache_clear()
    yield
    bridge._discover.cache_clear()


def test_component_disabled_even_when_a_blender_executable_exists(monkeypatch, tmp_path):
    executable = tmp_path / "blender.exe"
    executable.touch()
    monkeypatch.setenv("CYCLES_HOUDINI_BLENDER", str(executable))
    monkeypatch.setenv("CYCLES_HOUDINI_BLENDER_BRIDGE", "0")
    with patch.object(bridge, "_discover", side_effect=AssertionError("disabled component searched for Blender")):
        assert bridge.status() == dict(enabled=False, available=False, executable=None, configured=str(executable))
        with pytest.raises(ValueError, match="Setup again and select"):
            bridge.require_executable()


def test_missing_blender_is_a_quiet_capability_and_actionable_asset_error():
    with patch.object(bridge.shutil, "which", return_value=None) as search:
        for _ in range(8):
            assert bridge.status()["available"] is False
            assert bridge.status()["enabled"] is True  # Legacy/default registration.
        assert search.call_count == 1  # Including negative discovery; no per-frame scan.
        with pytest.raises(ValueError, match="Blender not found at.*CYCLES_HOUDINI_BLENDER.*restart Houdini"):
            bridge.require_executable()
        assert search.call_count == 1


def test_explicit_path_is_authoritative_and_environment_changes_invalidate_lookup(monkeypatch, tmp_path):
    installed = tmp_path / "blender.exe"
    installed.touch()
    with patch.object(bridge.shutil, "which", return_value=str(installed)) as search:
        assert bridge.require_executable() == installed
        missing = tmp_path / "missing.exe"
        monkeypatch.setenv("CYCLES_HOUDINI_BLENDER", str(missing))
        assert bridge.status()["available"] is False
        with pytest.raises(ValueError, match="Blender not found at " + re.escape(str(missing))):
            bridge.require_executable()
        monkeypatch.setenv("CYCLES_HOUDINI_BLENDER", str(installed))
        assert bridge.require_executable() == installed
        installed.unlink()
        assert bridge.status()["available"] is False
        with pytest.raises(ValueError, match="Blender not found at"):
            bridge.require_executable()
        assert search.call_count == 1


def test_bridge_capabilities_are_added_without_diagnostics(monkeypatch):
    import cycles_houdini as ch
    from cycles_houdini.core import sdk_dependencies

    class Native:
        def capabilities(self):
            return {"image_iris": True, "gaussian_splats": True}

    monkeypatch.setattr(ch, "_native", Native())
    monkeypatch.setattr(sdk_dependencies, "validate_runtime", lambda **kwargs: {})
    caps = ch.capabilities()
    assert caps["blender_bridge"] is False and caps["blender_bridge_enabled"] is True
    assert caps["image_iris"] is True and caps["gaussian_splats"] is True


def test_houdini_environment_and_expansion_are_shared_by_capabilities_and_assets(monkeypatch, tmp_path):
    import cycles_houdini as ch
    from cycles_houdini.core import sdk_dependencies

    executable = tmp_path / "blender.exe"
    executable.touch()
    monkeypatch.setenv("CYCLES_HOUDINI_BLENDER", str(tmp_path / "wrong-shell-path.exe"))
    host_values = {"CYCLES_HOUDINI_BLENDER": "$HIP/blender.exe", "CYCLES_HOUDINI_BLENDER_BRIDGE": "1"}
    host = SimpleNamespace(
        getenv=host_values.get,
        text=SimpleNamespace(expandString=lambda value: value.replace("$HIP", tmp_path.as_posix())),
    )
    monkeypatch.setattr(bridge, "_host", lambda: host)
    monkeypatch.setattr(ch, "_native", SimpleNamespace(capabilities=lambda: {}))
    monkeypatch.setattr(sdk_dependencies, "validate_runtime", lambda **kwargs: {})
    assert ch.capabilities()["blender_bridge"] is True
    assert bridge.require_executable() == executable
    host_values["CYCLES_HOUDINI_BLENDER_BRIDGE"] = "0"
    assert ch.capabilities()["blender_bridge"] is False
    assert ch.capabilities()["blender_bridge_enabled"] is False
    with pytest.raises(ValueError, match="bridge is disabled"):
        bridge.require_executable()
