# SPDX-License-Identifier: Apache-2.0
import math
import os

import pytest

from support.images import assert_image_close, rmse
from support.paths import ROOT, artifact_path, artifact_label, artifacts_root, pytest_artifacts_root
from support import workers

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


def test_pure_children_use_matching_sdk_python_without_host_configuration(monkeypatch, tmp_path):
    hython = tmp_path / "sdk/bin/hython.exe"
    hython.parent.mkdir(parents=True)
    hython.touch()
    version = workers.sys.version_info
    python = tmp_path / f"sdk/python{version.major}{version.minor}/python.exe"
    python.parent.mkdir()
    python.touch()
    monkeypatch.setattr(workers.sys, "executable", str(hython))
    assert workers.ordinary_python() == python
    monkeypatch.setenv("HOUDINI_PACKAGE_DIR", "artist-packages")
    monkeypatch.setenv("CYCLES_HOUDINI_HOST_ROOT", "artist-host")
    monkeypatch.setenv("PYTHONHOME", "embedded-host")
    monkeypatch.setenv("PYTHONPATH", "artist-modules")
    env = workers.pure_python_environment([tmp_path / "test-modules"])
    assert not any(key.startswith(("HOUDINI_", "CYCLES_HOUDINI_")) for key in env)
    assert "PYTHONHOME" not in env
    assert env["PYTHONPATH"] == str(tmp_path / "test-modules")
    python.unlink()
    with pytest.raises(RuntimeError, match="Ordinary Python interpreter is missing"):
        workers.ordinary_python()


def test_image_comparison_rejects_corruption_and_shape_mismatch():
    assert rmse([0, 1], [0, 1]) == 0
    assert math.isclose(rmse([0, 1], [0, 0]), math.sqrt(0.5))
    for a, b in [([], []), ([0], [0, 1]), ([math.nan], [0]), ([0], [math.inf])]:
        with pytest.raises(ValueError):
            rmse(a, b)
    with pytest.raises(AssertionError, match="RMSE"):
        assert_image_close([0, 1], [0, 0], tolerance=0.1)


def test_external_artifacts_and_traversal(monkeypatch, tmp_path):
    monkeypatch.setenv("CYCLES_HOUDINI_TEST_ARTIFACTS", str(tmp_path))
    assert artifact_path("case", "report.json") == tmp_path / "case/report.json"
    with pytest.raises(ValueError, match="leaves"):
        artifact_path("..", "outside.json")


def test_relocated_runtime_uses_its_own_data_directory(monkeypatch, tmp_path):
    monkeypatch.delenv("CYCLES_HOUDINI_TEST_ARTIFACTS", raising=False)
    monkeypatch.setenv("CYCLES_HOUDINI_SOURCE_ROOT", str(tmp_path))
    assert artifact_path("farm", "report.json") == tmp_path / "artifacts/farm/report.json"


def test_device_artifact_labels_are_safe_bounded_and_stable():
    assert artifact_label("CPU") == "CPU"
    assert artifact_label("CON") == "_CON"
    assert artifact_label("..") == "case"
    first = artifact_label("CUDA_gpu_0000:01:00_OptiX" * 3, limit=24)
    second = artifact_label("CUDA_gpu_0000:02:00_OptiX" * 3, limit=24)
    assert len(first) == 24 and ":" not in first and "/" not in first and first != second
    assert first == artifact_label("CUDA_gpu_0000:01:00_OptiX" * 3, limit=24)


def test_bare_pytest_default_is_in_the_checkouts_work_folder_while_legacy_data_paths_stay_compatible(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("CYCLES_HOUDINI_TEST_ARTIFACTS", raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "user"))
    monkeypatch.setenv("CYCLES_HOUDINI_SOURCE_ROOT", str(tmp_path / "legacy-data"))
    default = pytest_artifacts_root()
    assert default.is_relative_to(ROOT / "_work/test-runs/pytest")
    assert not default.is_relative_to(tmp_path / "user")
    assert default != pytest_artifacts_root()  # fresh evidence, even in one second
    assert artifacts_root() == tmp_path / "legacy-data/artifacts"
    selected = tmp_path / "explicit"
    monkeypatch.setenv("CYCLES_HOUDINI_TEST_ARTIFACTS", str(selected))
    assert pytest_artifacts_root() == selected
    assert pytest_artifacts_root(tmp_path / "option") == tmp_path / "option"


def test_bare_legacy_scripts_share_the_work_folder_default_for_producer_lookup(monkeypatch, tmp_path):
    from support import paths

    monkeypatch.delenv("CYCLES_HOUDINI_TEST_ARTIFACTS", raising=False)
    monkeypatch.delenv("CYCLES_HOUDINI_SOURCE_ROOT", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(paths, "ROOT", tmp_path / "checkout")
    producer = artifact_path("fixtures", "image.exr")
    producer.write_bytes(b"producer output")
    assert artifacts_root() == tmp_path / "checkout/_work/test-artifacts"
    assert artifact_path("fixtures", "image.exr").read_bytes() == b"producer output"
    assert not (tmp_path / "local").exists()


def test_development_package_source_root_keeps_evidence_in_the_work_folder(monkeypatch, tmp_path):
    from support import paths

    monkeypatch.delenv("CYCLES_HOUDINI_TEST_ARTIFACTS", raising=False)
    monkeypatch.setattr(paths, "ROOT", tmp_path / "checkout")
    monkeypatch.setenv("CYCLES_HOUDINI_SOURCE_ROOT", str(tmp_path / "checkout"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    # Inside the checkout only its ignored working folder, never artifacts/ or the source tree.
    assert artifacts_root() == tmp_path / "checkout/_work/test-artifacts"


@pytest.mark.skipif(os.name != "nt", reason="directory junctions need Windows")
def test_link_safe_tree_removal_leaves_a_junction_canary_intact(tmp_path):
    import shutil
    import subprocess

    canary = tmp_path / "canary"
    canary.mkdir()
    (canary / "keep.txt").write_text("must stay")
    tree = tmp_path / "work" / "wt" / "x"
    (tree / "s/third_party").mkdir(parents=True)
    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    script = (
        ". $env:CH_LAYOUT; New-Item -ItemType Junction -Path (Join-Path $env:CH_TREE 's/third_party/optix') "
        "-Target $env:CH_CANARY | Out-Null; Remove-ProjectTree -Root $env:CH_TREE"
    )
    env = dict(os.environ, CH_LAYOUT=str(ROOT / "scripts/project-layout.ps1"), CH_TREE=str(tree), CH_CANARY=str(canary))
    result = subprocess.run([pwsh, "-NoProfile", "-Command", script], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not tree.exists()
    assert (canary / "keep.txt").read_text() == "must stay"
