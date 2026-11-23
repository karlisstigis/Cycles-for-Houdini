# SPDX-License-Identifier: Apache-2.0
"""Pure, shared ownership of native tiers, backend selection and producer order."""

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def headless_recipes():
    return json.loads((ROOT / "tests/test-catalogue.json").read_text(encoding="utf-8-sig"))["headless"]


def catalogue_cases(recipes=None):
    recipes = headless_recipes() if recipes is None else recipes
    return [dict(test=row["test"], device=device) for row in recipes for device in row["backends"]]


def producer_cases(case, recipes=None):
    """Direct prerequisites match the consumer backend or its internal backend policy."""
    recipes = headless_recipes() if recipes is None else recipes
    by_name = {row["test"]: row for row in recipes}
    recipe = by_name.get(case["test"])
    if recipe is None:
        return []
    result = []
    for name in recipe["depends_on"]:
        producer = by_name[name]
        backends = producer["backends"] if case["device"] == "script-declared" else [case["device"]]
        for backend in backends:
            if backend not in producer["backends"]:
                if producer["backends"] != ["script-declared"]:
                    raise ValueError("No compatible producer backend for " + name + ": " + backend)
                backend = "script-declared"
            result.append(dict(test=name, device=backend))
    return result


def required_cases(selected, recipes=None):
    """Deduplicated topological closure, retaining the official order when valid."""
    recipes = headless_recipes() if recipes is None else recipes
    by_name = {row["test"]: row for row in recipes}
    requested, visiting, ordered = set(), set(), []

    def include(case):
        name, backend = case["test"], case["device"]
        if name not in by_name:
            raise ValueError("Unknown catalogue test: " + name)
        if backend not in by_name[name]["backends"]:
            raise ValueError("No compatible catalogue backend for " + name + ": " + backend)
        key = name, backend
        if key in visiting:
            raise ValueError("Cyclic catalogue producer dependency: " + name)
        if key in requested:
            return
        visiting.add(key)
        for producer in producer_cases(case, recipes):
            include(producer)
        visiting.remove(key)
        requested.add(key)
        ordered.append(dict(test=name, device=backend))

    positions = {(case["test"], case["device"]): index for index, case in enumerate(catalogue_cases(recipes))}
    for case in selected:
        if (case["test"], case["device"]) not in positions:
            include(case)
    for case in sorted(selected, key=lambda case: positions[(case["test"], case["device"])]):
        include(case)
    return ordered


def tier_cases(tier, destination):
    if tier not in ("fast", "extended", "nightly"):
        raise ValueError("Unknown native tier: " + tier)
    destination = Path(destination)
    result = []

    def case(test, device="CPU", report="", arguments=(), gui=False, prerequisites=("verified-build", "houdini-sdk")):
        result.append(
            dict(
                test=test,
                device=device,
                report=str(report),
                arguments=list(arguments),
                gui=gui,
                prerequisites=list(prerequisites),
            )
        )

    pytest_arguments = ("--native", "--markers", "pure or hython") if tier == "nightly" else ("--markers", "pure")
    case(
        "pytest_contracts.py",
        arguments=pytest_arguments,
        prerequisites=("verified-build", "houdini-sdk", "pytest-local-target"),
    )
    report = destination / "native-review-contracts.json"
    case(
        "native_review_contracts.py",
        report=report,
        arguments=("--report", str(report)),
        prerequisites=("verified-build", "houdini-sdk", "msvc", "cmake"),
    )
    case("engine_fork_test.py", prerequisites=("git",))
    case("community_build_test.py", prerequisites=("git",))
    case("patch_maintenance_test.py", prerequisites=("git",))
    report = destination / "no-blender-first-image.json"
    case("houdini_no_blender_smoke.py", report=report, arguments=("--device", "CPU", "--report", str(report)))
    if tier == "nightly":
        recipes = headless_recipes()
        by_name = {row["test"]: row for row in recipes}
        for selected in required_cases(catalogue_cases(recipes), recipes):
            recipe, backend = by_name[selected["test"]], selected["device"]
            case(
                recipe["test"],
                backend,
                recipe.get("reports_by_backend", {}).get(backend, recipe["report"]),
                () if backend == "script-declared" else ("--device", backend),
                prerequisites=recipe["prerequisites"],
            )
        report = destination / "binding-contract.json"
        case("houdini_status_conversion_contract.py", report=report, arguments=("--report", str(report)))
        for backend in ("CPU", "OPTIX"):
            reference = destination / ("blender-reference-" + backend)
            case(
                "houdini_blend_instances_smoke.py",
                backend,
                reference / ("comparison-" + backend + ".json"),
                ("--generate-reference", "--device", backend, "--out", str(reference)),
            )
        return result
    for name, report in (
        ("release_workflow_test.py", ""),
        ("staging_runtime_inventory_test.py", ""),
        ("release_package_test.py", "artifacts/release-package-unit.json"),
        ("patch_replay_parent_git_test.py", ""),
        ("source_history_test.py", ""),
        ("blend_cache_test.py", ""),
        ("queued_rejected_navigation_test.py", ""),
        ("core_snapshot_retention_smoke.py", "artifacts/stage1/snapshots/core-snapshot-retention-smoke.json"),
        ("core_session_lifecycle_smoke.py", "artifacts/stage3/session-lifecycle/core-session-lifecycle-smoke.json"),
        ("houdini_transaction_contract_smoke.py", "artifacts/stage3/transactions/contract-report.json"),
    ):
        case(name, report=report)
    report = destination / "binding-contract.json"
    case("houdini_status_conversion_contract.py", report=report, arguments=("--report", str(report)))
    if tier == "extended":
        case(
            "houdini_navigation_snapshot_smoke.py",
            report="artifacts/stage1/navigation/houdini-navigation-snapshot-smoke.json",
        )
        case(
            "houdini_navigation_output_dependency_smoke.py",
            report="artifacts/stage1/navigation/houdini-navigation-output-dependency-smoke.json",
        )
        case(
            "houdini_color_epoch_smoke.py",
            "CPU and default device",
            "artifacts/stage3/color/epochs/houdini-color-epoch-smoke.json",
        )
        case(
            "houdini_color_config_smoke.py",
            "CPU and default device",
            "artifacts/stage3/color/houdini-config/report.json",
        )
        for backend in ("CPU", "OPTIX"):
            styles = destination / ("blend-styles-" + backend)
            case(
                "houdini_blend_direct_styles_smoke.py",
                backend,
                styles / "report.json",
                ("--device", backend, "--out", str(styles)),
            )
            report = destination / ("blend-reseed-" + backend) / "report.json"
            case(
                "houdini_blend_instance_reseed_smoke.py",
                backend,
                report,
                ("--device", backend, "--report", str(report)),
            )
            reference = destination / ("blender-reference-" + backend)
            case(
                "houdini_blend_instances_smoke.py",
                backend,
                reference / ("comparison-" + backend + ".json"),
                ("--generate-reference", "--device", backend, "--out", str(reference)),
            )
        for name in ("houdini_worker_active_failure_smoke.py", "houdini_release_response_gui_smoke.py"):
            case(name, report="GUI lifecycle report", gui=True)
    return result


def select_plan(plan, *, names=None, cases=None, recipes=None):
    if names is not None and cases is not None:
        raise ValueError("Select names or catalogue cases, not both")
    if names is not None:
        known = {row["test"] for row in plan}
        if not names or len(set(names)) != len(names) or set(names) - known:
            raise ValueError("Invalid nightly test selection")
        selected = [row for row in plan if row["test"] in names]
    elif cases is not None:
        keys = [(row["test"], row["device"]) for row in cases]
        if not keys or len(set(keys)) != len(keys):
            raise ValueError("Invalid catalogue case selection")
        required = required_cases(cases, recipes)
        selected_keys = {(row["test"], row["device"]) for row in required}
        selected = [row for row in plan if (row["test"], row["device"]) in selected_keys]
        if len(selected) != len(required):
            raise ValueError("Catalogue selection did not resolve exactly")
    else:
        return plan
    keys = {(row["test"], row["device"]) for row in selected}
    for row in selected:
        if any((item["test"], item["device"]) not in keys for item in producer_cases(row, recipes)):
            raise ValueError("Nightly selection omits a required producer for " + row["test"])
    return selected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier", required=True, choices=("fast", "extended", "nightly"))
    parser.add_argument("--destination", required=True)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--nightly-selection", type=Path)
    selection.add_argument("--catalogue-selection", type=Path)
    args = parser.parse_args()
    if args.tier != "nightly" and (args.nightly_selection or args.catalogue_selection):
        parser.error("Partitions and catalogue selections require the nightly tier")

    def read(path):
        return json.loads(path.read_text(encoding="utf-8-sig")) if path else None

    print(
        json.dumps(
            select_plan(
                tier_cases(args.tier, args.destination),
                names=read(args.nightly_selection),
                cases=read(args.catalogue_selection),
            )
        )
    )


if __name__ == "__main__":
    main()
