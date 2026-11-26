# SPDX-License-Identifier: Apache-2.0
"""Write explicit current-runtime recipes; generating a plan executes no tests.

Related fixtures are not independent proof of every associated patch. Legacy
dual-backend fixtures execute CPU plus ch.default_device() once, regardless of
the launcher's --device. Selected-device fixtures can use any supported exact
GPU identity. GUI and host-only contracts retain their actual execution scope.
"""

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
COUPLED = {
    "houdini_release_runtime_smoke.py",
    "houdini_points_smoke.py",
    "houdini_volume_motion_smoke.py",
    "houdini_shader_retirement_smoke.py",
    "houdini_subdivision_smoke.py",
    "houdini_texture_cache_smoke.py",
    "houdini_color_interop_smoke.py",
    "houdini_texture_alpha_smoke.py",
    "houdini_texture_normal_smoke.py",
    "houdini_unchanged_update_smoke.py",
    "houdini_progressive_smoke.py",
    "houdini_volume_smoke.py",
    "core_subdivision_smoke.py",
}
GUI = {
    "houdini_camera_sampling_gui_smoke.py",
    "houdini_subdivision_ipr_smoke.py",
    "houdini_camera_intrinsic_motion_gui_smoke.py",
    "houdini_image_iris_color_smoke.py",
}
HOST = {
    "core_light_tree_cache_smoke.py",
    "core_gpu_concurrency_smoke.py",
    "core_subdivision_shading_smoke.py",
    "core_subdivision_count_overflow_smoke.py",
    "core_object_volume_bounds_smoke.py",
}
PURE = {"sdk_dependency_policy_test.py", "multi_device_range_fault_test.py"}
BOUNDARY = {"core_subdivision_count_overflow_smoke.py", "core_point_lifetime_smoke.py", "core_mesh_sharing_smoke.py"}
PREREQUISITES = {
    "houdini_texture_normal_smoke.py": ["houdini_texture_smoke.py"],
    "houdini_texture_cache_smoke.py": ["houdini_texture_smoke.py"],
    "houdini_subdivision_ipr_smoke.py": ["houdini_subdivision_smoke.py", "core_subdivision_smoke.py"],
    "core_subdivision_smoke.py": ["houdini_subdivision_smoke.py"],
}


def recipes():
    contracts = json.loads((ROOT / "patches/contracts.json").read_text(encoding="utf-8-sig"))
    grouped = {}
    for patch, fixture in contracts["fixtures"].items():
        if fixture:
            grouped.setdefault(Path(fixture).name, []).append(int(patch))
    catalog = dict(
        re.findall(
            r"@\('([^']+\.py)','(artifacts/[^']+)'\)", (ROOT / "scripts/run-tests.ps1").read_text(encoding="utf-8-sig")
        )
    )
    catalog.update(
        {
            "houdini_world_smoke.py": "artifacts/stage3/world/houdini-world-smoke.json",
            "houdini_world_sampling_smoke.py": "artifacts/stage3/world/sampling/houdini-world-sampling-smoke.json",
            "houdini_panorama_smoke.py": "artifacts/stage2/cameras/panorama/report.json",
            "core_image_iris_smoke.py": "{output}/image-iris-{backend}/report.json",
            "houdini_camera_dof_motion_smoke.py": "{output}/camera-dof-{backend}.json",
            "houdini_subdivision_topology_cache_smoke.py": "{output}/subdivision-topology-cache-{backend}/report.json",
            "houdini_light_material_smoke.py": "artifacts/light-material-20261026/material-{device_file}.json",
            "houdini_adaptive_target_smoke.py": "artifacts/stage3/render-settings/houdini-adaptive-target-smoke-{backend}.json",
            "houdini_release_runtime_smoke.py": "{output}/release-runtime.json",
            "houdini_rls_hair_smoke.py": "{output}/rls-hair-{backend}.json",
            "houdini_spectral_nested_smoke.py": "{output}/spectral-{backend}/report.json",
            "houdini_blender53_gsplats_smoke.py": "{output}/splats-{backend}/report.json",
            "houdini_dlss_worker_smoke.py": "{output}/dlss-{backend}.json",
            "houdini_dlss_sample_count_smoke.py": "{output}/dlss-sample-count-{backend}.json",
            "houdini_temporal_sample_count_smoke.py": "{output}/temporal-sample-count-{backend}.json",
            "multi_device_range_fault_test.py": "{output}/multi-device-range-fault/report.json",
            "houdini_curve_subdivisions_smoke.py": "artifacts/stage2/hair/curve-subdivisions/report-{device_kind}.json",
            "houdini_top_level_incremental_smoke.py": "artifacts/engine-contracts/houdini_top_level_incremental_smoke-{device_kind}.json",
            "houdini_object_flags_partial_smoke.py": "artifacts/engine-contracts/houdini_object_flags_partial_smoke-{device_kind}.json",
            "houdini_object_table_partial_smoke.py": "artifacts/engine-contracts/houdini_object_table_partial_smoke-{device_kind}.json",
            "houdini_attribute_tables_kept_smoke.py": "artifacts/engine-contracts/houdini_attribute_tables_kept_smoke-{device_kind}.json",
            "houdini_light_list_smoke.py": "artifacts/engine-contracts/houdini_light_list_smoke-{device_kind}.json",
            "houdini_device_update_memory_smoke.py": "artifacts/engine-contracts/houdini_device_update_memory_smoke-{device_kind}.json",
            "core_gpu_concurrency_smoke.py": None,
            "houdini_selective_rls_smoke.py": "{output}/selective-{backend}/report.json",
            "houdini_nested_dielectrics_plumbing_smoke.py": "{output}/nested-{backend}/houdini-nested-dielectrics-plumbing.json",
        }
    )
    result = []
    for filename, patches in grouped.items():
        source = (ROOT / "tests" / filename).read_text(encoding="utf-8-sig")
        policy = (
            "gpu_only"
            if filename
            in (
                "houdini_dlss_worker_smoke.py",
                "houdini_temporal_sample_count_smoke.py",
                "houdini_dlss_sample_count_smoke.py",
            )
            else "pure_host"
            if filename in PURE
            else "native_host_only"
            if filename in HOST
            else "cpu_and_default_gpu"
            if filename in COUPLED
            else "selected_device"
        )
        assert filename in GUI or filename in catalog, "Missing output recipe: " + filename
        result.append(
            dict(
                test=filename,
                patches=patches,
                gui=filename in GUI,
                backend_policy=policy,
                report_template=catalog.get(filename),
                source_contract=ast.get_docstring(ast.parse(source)),
                fixture_sha256=hashlib.sha256((ROOT / "tests" / filename).read_bytes()).hexdigest(),
                qualification="Runnable related contract; not patch isolation, mutation evidence or an execution claim",
            )
        )
    return result


def plan(
    build_directory,
    gpu_device="",
    default_gpu_device="",
    host=None,
    backends=("CPU", "GPU"),
    selection=None,
    all_primary=False,
    label="",
):
    build = Path(build_directory)
    if not build.is_absolute() or not build.is_dir():
        raise ValueError("Build directory must be an absolute existing directory")
    manifest_path = build / "build-manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8-sig"))
    version = manifest["host_sdk"]["houdini"]
    host = Path(host) if host else (build / manifest["resource_root"]).parent
    if not host.is_absolute() or not host.is_dir():
        raise ValueError("Selected host must be an absolute existing directory")
    label = label or "patch-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", label):
        raise ValueError("Label must contain letters, numbers, underscore or hyphen")
    selected_names = set(selection) if selection else None
    available = recipes()
    known = {recipe["test"] for recipe in available}
    if selected_names and selected_names - known:
        raise ValueError("Unknown primary fixtures: " + ", ".join(sorted(selected_names - known)))
    primary_chosen = (
        [recipe for recipe in available if recipe["test"] in selected_names]
        if selected_names
        else (available if all_primary else [recipe for recipe in available if recipe["test"] in BOUNDARY])
    )
    lookup = {recipe["test"]: recipe for recipe in available}
    for name, report, policy in (
        (
            "core_subdivision_smoke.py",
            "artifacts/stage2/subdivision/core-subdivision-smoke.json",
            "cpu_and_default_gpu",
        ),
        ("houdini_texture_smoke.py", "artifacts/stage3/textures/houdini-texture-smoke.json", "supplemental_cpu"),
    ):
        path = ROOT / "tests" / name
        lookup[name] = dict(
            test=name,
            patches=[],
            gui=False,
            backend_policy=policy,
            report_template=report,
            source_contract=ast.get_docstring(ast.parse(path.read_text(encoding="utf-8-sig"))),
            fixture_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            qualification="Fresh prerequisite fixture generation; not an additional primary patch association",
        )
    chosen = []
    emitted = set()

    def include(recipe):
        if recipe["test"] in emitted:
            return
        for prerequisite in PREREQUISITES.get(recipe["test"], []):
            include(lookup[prerequisite])
        chosen.append(recipe)
        emitted.add(recipe["test"])

    for recipe in primary_chosen:
        include(recipe)
    needs_coupled = any(recipe["backend_policy"] == "cpu_and_default_gpu" for recipe in chosen)
    if ("GPU" in backends or needs_coupled) and not gpu_device:
        raise ValueError("GPU cases require an exact --gpu-device from the selected runtime")
    default_gpu_device = default_gpu_device or gpu_device
    output = "artifacts/patch-matrix/" + label
    cases = []
    pure_commands = []
    # A coupled fixture cannot become CPU-only by passing --device CPU. Check
    # its actual default GPU before dispatch, and expose both devices in plan.
    check_devices = set([gpu_device] if "GPU" in backends and gpu_device else [])
    if needs_coupled:
        check_devices.add(default_gpu_device)
    if not check_devices:
        check_devices.add("CPU")
    for index, device in enumerate(sorted(check_devices)):
        report = f"{output}/devices-{index}.json"
        arguments = ["--report", str(ROOT / report), "--manifest-sha256", hashlib.sha256(manifest_bytes).hexdigest()]
        if needs_coupled and device == default_gpu_device:
            arguments += ["--require-default"]
        cases.append(
            dict(
                test="houdini_patch_matrix_devices_smoke.py",
                device=device,
                gui=False,
                execution="worker",
                report=report,
                arguments=arguments,
                scope="Device availability/default-selection preflight, not a rendering contract",
            )
        )
    for recipe in chosen:
        filename = recipe["test"]
        policy = recipe["backend_policy"]
        if policy == "pure_host":
            arguments = (
                ["--output", str(ROOT / output / "multi-device-range-fault")]
                if filename == "multi_device_range_fault_test.py"
                else ["--report", str(ROOT / output / "sdk-policy.json")]
            )
            pure_commands.append(["python", str(ROOT / "tests" / filename), *arguments])
            continue
        targets = (
            [("CPU", "CPU")]
            if policy in ("native_host_only", "cpu_and_default_gpu", "supplemental_cpu")
            else [
                (backend, "CPU" if backend == "CPU" else gpu_device)
                for backend in backends
                if policy != "gpu_only" or backend == "GPU"
            ]
        )
        for backend, device in targets:
            values = dict(
                output=output,
                backend="CPU" if backend == "CPU" else "OPTIX",
                device=device,
                device_kind="cpu" if backend == "CPU" else "gpu",
                device_file=re.sub(r"[^A-Za-z0-9_.-]", "_", device),
            )
            report = recipe["report_template"].format(**values) if recipe["report_template"] else None
            arguments = []
            if filename == "houdini_release_runtime_smoke.py":
                arguments += ["--houdini", version, "--expected-host", str(host), "--report", str(ROOT / report)]
            elif filename in ("houdini_rls_hair_smoke.py", "houdini_selective_rls_smoke.py"):
                prefix = "rls-hair" if filename == "houdini_rls_hair_smoke.py" else "selective"
                arguments += ["--label", prefix + "-" + values["backend"], "--output", str(ROOT / output)]
            elif filename == "houdini_nested_dielectrics_plumbing_smoke.py":
                arguments += ["--out", str(ROOT / output / ("nested-" + values["backend"]))]
            elif filename in (
                "houdini_camera_dof_motion_smoke.py",
                "core_image_iris_smoke.py",
                "houdini_subdivision_topology_cache_smoke.py",
            ):
                arguments += ["--report", str(ROOT / report)]
            elif filename == "houdini_spectral_nested_smoke.py":
                arguments += ["--output", str(ROOT / output / ("spectral-" + values["backend"]))]
            elif filename == "houdini_blender53_gsplats_smoke.py":
                arguments += ["--out", str(ROOT / output / ("splats-" + values["backend"]))]
            elif filename in (
                "houdini_dlss_worker_smoke.py",
                "houdini_temporal_sample_count_smoke.py",
                "houdini_dlss_sample_count_smoke.py",
            ):
                arguments += ["--report", str(ROOT / report)]
            case = dict(
                test=filename,
                device=device,
                gui=recipe["gui"],
                execution="worker",
                patches=recipe["patches"],
                backend_policy=policy,
                qualification="planned_not_executed",
                environment=dict(CYCLES_HOUDINI_SOURCE_ROOT=str(ROOT)),
            )
            if report:
                case["report"] = report
            if arguments:
                case["arguments"] = arguments
            if policy == "cpu_and_default_gpu":
                case["actual_device_scope"] = ["CPU", default_gpu_device]
            elif policy == "native_host_only":
                case["actual_device_scope"] = ["host arithmetic/storage; no GPU rendering"]
            else:
                case["actual_device_scope"] = [device]
            cases.append(case)
    return dict(
        schema_version=1,
        qualification="Planned recipes only; no tests executed by generation",
        build_directory=str(build),
        host=str(host),
        houdini=version,
        build_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        label=label,
        patch_count=len(json.loads((ROOT / "patches/contracts.json").read_text())["fixtures"]),
        associated_patch_count=sum(len(recipe["patches"]) for recipe in available),
        unique_primary_fixture_count=len(available),
        selected_fixture_count=len(primary_chosen),
        supplemental_fixture_count=sum(not recipe["patches"] for recipe in chosen),
        cases=cases,
        pure_commands=pure_commands,
        recipes=chosen,
        unassigned_patches=[
            dict(
                number=2,
                scope="SDK link/build and delegate load are separate release checks; no targeted behavioral fixture",
            )
        ],
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--catalog",
        action="store_true",
        help="Print the complete related-fixture recipe catalog without selecting a runtime",
    )
    parser.add_argument("--build-directory", type=Path)
    parser.add_argument("--host", type=Path)
    parser.add_argument("--gpu-device", default="")
    parser.add_argument("--default-gpu-device", default="")
    parser.add_argument("--backends", nargs="+", choices=("CPU", "GPU"), default=["CPU", "GPU"])
    parser.add_argument("--fixtures", nargs="+")
    parser.add_argument(
        "--all-primary", action="store_true", help="Opt in to the complete primary related-fixture catalog"
    )
    parser.add_argument("--label", default="")
    parser.add_argument(
        "--output", type=Path, help="Cases JSON for verify-native-release.ps1; adjacent .plan.json records scope"
    )
    args = parser.parse_args()
    if args.catalog:
        print(json.dumps(dict(result="catalog_only_not_executed", recipes=recipes()), indent=2))
        return
    if not args.build_directory or not args.output:
        parser.error("Generating cases requires --build-directory and --output")
    result = plan(
        args.build_directory,
        args.gpu_device,
        args.default_gpu_device,
        args.host,
        args.backends,
        args.fixtures,
        args.all_primary,
        args.label,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result["cases"], indent=2) + "\n", encoding="utf-8")
    args.output.with_suffix(".plan.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            dict(
                result="planned_not_executed",
                cases=len(result["cases"]),
                selected_fixtures=result["selected_fixture_count"],
                cases_file=str(args.output.resolve()),
                plan_file=str(args.output.with_suffix(".plan.json").resolve()),
            )
        )
    )


if __name__ == "__main__":
    main()
