# SPDX-License-Identifier: Apache-2.0
"""Account for every test script and derive the full mapped headless plan.

An unmapped new script requires a deliberate disposition. Existing scripts
without a disposition are listed in the policy file. Retirements require a
recorded reason and replacement or evidence.
"""

import argparse
import ast
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
CATALOGUE = ROOT / "tests/test-catalogue.json"
NIGHTLY_EXCLUSIONS = {
    "houdini_memory_smoke.py": "Calibrated telemetry requires an NVIDIA GeForce RTX 4090 and matching Windows DXGI counters. Run with -Tests on that workstation.",
    "houdini_device_selection_smoke.py": "Workstation certification requires two physical GPUs with both CUDA and OptiX; run explicitly with -Tests on that hardware.",
    "houdini_viewer_memory_smoke.py": "Workstation telemetry certification requires two physical OptiX GPUs and working Windows DXGI per-GPU counters; run explicitly with -Tests.",
}
PRODUCERS = {
    # The core harness writes its independent reference-* images, but these
    # readers also require the Houdini-authored native EXRs in the parent tree.
    "core_displacement_smoke.py": ["houdini_displacement_smoke.py"],
    "core_subdivision_smoke.py": ["houdini_subdivision_smoke.py"],
    "core_strand_material_smoke.py": ["houdini_strand_material_smoke.py"],
    "core_volume_material_smoke.py": ["houdini_volume_material_smoke.py"],
    "houdini_panel_motion_timing_smoke.py": ["houdini_motion_smoke.py"],
    "houdini_transparency_batch_smoke.py": ["houdini_transparency_smoke.py"],
    "houdini_texture_cache_smoke.py": ["houdini_texture_smoke.py"],
    "houdini_texture_normal_smoke.py": ["houdini_texture_smoke.py"],
    "houdini_texture_auto_batch_smoke.py": ["houdini_texture_smoke.py"],
    "houdini_queued_worker_smoke.py": ["houdini_showcase_refresh_smoke.py"],
}
SUPPORT_SCRIPTS = {
    "point_instancing_common.py",
    "gui_shutdown.py",
    "camera_intrinsic_reference.py",
    "point_curve_reference.py",
    "native_curve_reference.py",
    "subdivision_test_setup.py",
    "soho_camera_probe.py",
    "gui_startup/bootstrap.py",
    "gui_startup/failure_probe.py",
    "native_compiler.py",
}
REPORTS_BY_BACKEND = {
    "houdini_packed_scale_smoke.py": {
        "CPU": "artifacts/packed-scale/houdini-packed-scale-worker-cpu.json",
        "OPTIX": "artifacts/packed-scale/houdini-packed-scale-worker-gpu.json",
    },
}


def cli_options(path):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    return sorted(
        {
            arg.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument"
            for arg in node.args
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value.startswith("--")
        }
    )


def ignores_cli_result(path):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    return any(
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "parse_args"
        for node in ast.walk(tree)
    )


def build():
    runner = (ROOT / "scripts/run-tests.ps1").read_text(encoding="utf-8-sig")
    block = runner.split("$testCases=@(", 1)[1].split("\n)\n", 1)[0]
    mapped = re.findall(r"@\('([^']+\.py)',(\$null|'[^']*')\)", block)
    headless = []
    for name, report in mapped:
        source = ROOT / "tests" / name
        text = source.read_text(encoding="utf-8-sig")
        options = cli_options(source)
        if name in NIGHTLY_EXCLUSIONS:
            continue
        if "CYCLES_HOUDINI_GUI_REPORT" in text:
            raise ValueError("GUI-only test entered the headless table: " + name)
        selectable = "--device" in options and not ignores_cli_result(source)
        headless.append(
            dict(
                test=name,
                report="" if report == "$null" else report[1:-1],
                backends=["CPU", "OPTIX"] if selectable else ["script-declared"],
                backend_policy="selectable" if selectable else "script-declared",
                options=options,
                depends_on=PRODUCERS.get(name, []),
                prerequisites=["verified-build", "houdini-sdk"]
                + (["blender"] if "blend" in name else [])
                + (["generated-examples"] if "artifacts/examples" in text else []),
            )
        )
        if name in REPORTS_BY_BACKEND:
            headless[-1]["reports_by_backend"] = REPORTS_BY_BACKEND[name]
    pending = {item["test"]: item for item in headless}
    ordered, visiting = [], set()

    def emit(name):
        if name not in pending:
            return
        if name in visiting:
            raise ValueError("Cyclic nightly fixture dependency: " + name)
        visiting.add(name)
        item = pending[name]
        for producer in item["depends_on"]:
            emit(producer)
        visiting.remove(name)
        ordered.append(pending.pop(name))

    for name in tuple(pending):
        emit(name)
    headless = ordered
    review_path = ROOT / "tests/test-dispositions.json"
    dispositions = json.loads(review_path.read_text(encoding="utf-8")) if review_path.is_file() else {}
    reviewed = dispositions.get("entries", {})
    legacy = set(dispositions.get("legacy_without_disposition", ()))
    for name, record in reviewed.items():
        if not (ROOT / "tests" / name).is_file() or not record.get("reason") or not record.get("purpose"):
            raise ValueError("Incomplete manual disposition: " + name)
        if record["category"] not in ("automated", "support", "manual", "gui", "benchmark"):
            raise ValueError("Invalid reviewed disposition: " + name)
        if record["category"] == "automated" and not record.get("automation"):
            raise ValueError("Automated disposition needs an automation recipe: " + name)
        if record["category"] in ("manual", "gui", "benchmark") and not (
            record.get("requires_human") or record.get("automation")
        ):
            raise ValueError("Unscheduled disposition needs instructions or an automation recipe: " + name)
    scheduled = {name for name, report in mapped}
    scheduled.update(name for name, record in reviewed.items() if record.get("automation"))
    for source in (ROOT / "tests/pytest_cases").glob("*.py"):
        scheduled.update(
            re.findall(r"['\"](?:tests/)?([^'\"/]+\.(?:py|ps1))['\"]", source.read_text(encoding="utf-8-sig"))
        )
    for script in (
        "run-tests.ps1",
        "native-test-tiers.ps1",
        "native_test_plan.py",
        "verify-native-release.ps1",
        "run-gui-test.ps1",
        "build-release.ps1",
        "workstation-ci.py",
    ):
        scheduled.update(
            re.findall(
                r"['\"](?:tests/)?([^'\"/]+\.py)['\"]", (ROOT / "scripts" / script).read_text(encoding="utf-8-sig")
            )
        )
    catalogue = dict(
        schema_version=1,
        policy="tests/TESTING.md",
        nightly_exclusions=NIGHTLY_EXCLUSIONS,
        headless=headless,
        pytest=[],
        scheduled=[],
        support=[],
        manual=[],
        gui=[],
        benchmark=[],
        review="tests/test-dispositions.json",
        retired={},
    )
    retirements = ROOT / "tests/retired-probes.json"
    if retirements.is_file():
        for name, record in json.loads(retirements.read_text(encoding="utf-8"))["retired"].items():
            if (ROOT / "tests" / name).exists():
                raise ValueError("Retired test is still present: " + name)
            if not record.get("reason") or not record.get("replacements") or len(record.get("sha256", "")) != 64:
                raise ValueError("Incomplete retirement evidence: " + name)
            if not all((ROOT / item).is_file() for item in record["replacements"]):
                raise ValueError("Retired probe replacement is missing: " + name)
            catalogue["retired"][name] = record
    for source in sorted((ROOT / "tests").rglob("*")):
        if source.suffix not in (".py", ".ps1", ".cpp", ".cmake"):
            continue
        name = source.relative_to(ROOT / "tests").as_posix()
        if name.startswith("pytest_cases/"):
            category = "pytest"
        elif (
            name in SUPPORT_SCRIPTS
            or name.startswith("support/")
            or name.startswith("fixtures/")
            or source.name.endswith(("_fixture.py", "_fixtures.py"))
            or source.name
            in ("conftest.py", "native_test_entry.py", "native_test_runtime.py", "native_runtime_identity.py")
        ):
            category = "support"
        elif source.name in scheduled:
            category = "scheduled"
        else:
            if name not in reviewed and name not in legacy:
                raise ValueError(
                    f"Unscheduled test {name}: add a reason and instructions to tests/test-dispositions.json "
                    "or wire it into a runner before regenerating the catalogue."
                )
            category = reviewed.get(name, {}).get("category", "manual")
            if category == "manual" and name.endswith("_gui_smoke.py"):
                category = "gui"
            if category == "manual" and name.endswith("_benchmark.py"):
                category = "benchmark"
        catalogue[category].append(name)
    return catalogue


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = json.dumps(build(), indent=2) + "\n"
    if args.check:
        if not CATALOGUE.is_file() or CATALOGUE.read_text(encoding="utf-8") != expected:
            raise SystemExit(
                "Test catalogue is stale; run python scripts/catalogue-tests.py and review the dispositions."
            )
    else:
        CATALOGUE.write_text(expected, encoding="utf-8")
    data = json.loads(expected)
    print(
        json.dumps(
            {
                name: len(data[name])
                for name in ("headless", "pytest", "scheduled", "support", "manual", "gui", "benchmark", "retired")
            }
        )
    )


if __name__ == "__main__":
    main()
