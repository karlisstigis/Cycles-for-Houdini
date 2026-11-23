# SPDX-License-Identifier: Apache-2.0
import importlib.util
import json
import subprocess

import pytest

from support.paths import ROOT

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


def catalogue_builder():
    spec = importlib.util.spec_from_file_location("test_catalogue_builder", ROOT / "scripts/catalogue-tests.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_catalogue_accounts_for_every_test_script():
    module = catalogue_builder()
    assert json.loads((ROOT / "tests/test-catalogue.json").read_text()) == module.build(), (
        "Run python scripts/catalogue-tests.py and review changed dispositions"
    )


def test_new_unscheduled_script_requires_a_disposition(tmp_path, monkeypatch):
    module = catalogue_builder()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    tests, scripts = tmp_path / "tests", tmp_path / "scripts"
    tests.mkdir()
    scripts.mkdir()
    for name in (
        "run-tests.ps1",
        "native-test-tiers.ps1",
        "native_test_plan.py",
        "verify-native-release.ps1",
        "run-gui-test.ps1",
        "build-release.ps1",
        "workstation-ci.py",
    ):
        (scripts / name).write_text("$testCases=@(\n)\n" if name == "run-tests.ps1" else "")
    name = "new_artist_check.py"
    (tests / name).write_text("print('fixture')\n")
    with pytest.raises(ValueError, match=f"Unscheduled test {name}"):
        module.build()
    entry = dict(category="manual", purpose="Fixture", reason="Requires a particular scene")
    disposition = tests / "test-dispositions.json"
    disposition.write_text(json.dumps({"entries": {name: entry}}))
    with pytest.raises(ValueError, match="needs instructions"):
        module.build()
    entry["requires_human"] = "Open the fixture scene and run the script from Houdini's Python shell."
    disposition.write_text(json.dumps({"entries": {name: entry}}))
    assert module.build()["manual"] == [name]


def test_full_nightly_covers_each_selectable_backend():
    data = json.loads((ROOT / "tests/test-catalogue.json").read_text())
    assert len(data["headless"]) >= 300
    for case in data["headless"]:
        if case["backend_policy"] == "selectable":
            assert case["backends"] == ["CPU", "OPTIX"]
        else:
            assert case["backends"] == ["script-declared"]


def test_generated_scene_producers_precede_consumers():
    data = json.loads((ROOT / "tests/test-catalogue.json").read_text())
    indices = {case["test"]: index for index, case in enumerate(data["headless"])}
    for case in data["headless"]:
        for producer in case["depends_on"]:
            assert indices[producer] < indices[case["test"]]
    assert indices["houdini_motion_smoke.py"] < indices["houdini_panel_motion_timing_smoke.py"]
    assert "houdini_viewer_memory_smoke.py" not in indices
    assert "two physical OptiX GPUs" in data["nightly_exclusions"]["houdini_viewer_memory_smoke.py"]


def test_actual_powershell_nightly_plan_dispatches_every_compatible_case(tmp_path):
    script = tmp_path / "plan.ps1"
    script.write_text(
        "param([string]$Repo,[string]$Destination)\n"
        '. (Join-Path $Repo "scripts/native-test-tiers.ps1")\n'
        "@(Get-NativeTierCases nightly $Destination) | ConvertTo-Json -Depth 6 -Compress\n"
    )
    process = subprocess.run(
        ["pwsh", "-NoLogo", "-NoProfile", "-File", str(script), str(ROOT), str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    plan = json.loads(process.stdout)
    assert all(not case["gui"] for case in plan)
    assert all(isinstance(case["arguments"], list) for case in plan)
    assert all(value is not None for case in plan for value in case["arguments"])
    actual = {(case["test"], case["device"], tuple(case["arguments"] or [])) for case in plan}
    data = json.loads((ROOT / "tests/test-catalogue.json").read_text())
    for case in data["headless"]:
        for backend in case["backends"]:
            arguments = () if backend == "script-declared" else ("--device", backend)
            assert (case["test"], backend, arguments) in actual
            dispatched = next(row for row in plan if row["test"] == case["test"] and row["device"] == backend)
            expected_report = case.get("reports_by_backend", {}).get(backend, case["report"])
            assert dispatched["report"] == expected_report
    packed = {row["device"]: row["report"] for row in plan if row["test"] == "houdini_packed_scale_smoke.py"}
    assert packed["CPU"].endswith("worker-cpu.json")
    assert packed["OPTIX"].endswith("worker-gpu.json")
