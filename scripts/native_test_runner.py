# SPDX-License-Identifier: Apache-2.0
"""Fresh-process headless execution with run-local producer and domain evidence."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

from native_test_plan import ROOT, headless_recipes, producer_cases

sys.path.insert(0, str(ROOT / "tests"))
from support.recipe_contracts import audit_recipe
from support.workers import terminate_process

# Each consumer names its producer and every cross-case file it may read.
# The texture HIP resolves its image through $HIP; the queued worker explicitly
# loads the OCIO configuration beside the showcase HIP. A successful sibling
# backend may regenerate the shared HIP/OCIO before the consumer is staged.
CROSS_CASE_INPUTS = {
    "houdini_panel_motion_timing_smoke.py": {"houdini_motion_smoke.py": ["stage2/motion/native-velocity-motion.hiplc"]},
    "houdini_transparency_batch_smoke.py": {
        "houdini_transparency_smoke.py": ["stage3/color/transparency/native-alpha.hiplc"]
    },
    "houdini_queued_worker_smoke.py": {
        "houdini_showcase_refresh_smoke.py": [
            "examples/sop-fx-courtyard/sop-fx-courtyard-current.hiplc",
            "examples/sop-fx-courtyard/showcase.ocio",
        ]
    },
    "houdini_texture_cache_smoke.py": {
        "houdini_texture_smoke.py": ["stage3/textures/native-textures.hiplc", "stage3/textures/gui-texture.exr"]
    },
    "houdini_texture_normal_smoke.py": {
        "houdini_texture_smoke.py": ["stage3/textures/native-textures.hiplc", "stage3/textures/gui-texture.exr"]
    },
    "houdini_texture_auto_batch_smoke.py": {
        "houdini_texture_smoke.py": ["stage3/textures/native-textures.hiplc", "stage3/textures/gui-texture.exr"]
    },
    "core_strand_material_smoke.py": {
        "houdini_strand_material_smoke.py": [
            "stage3/materials/strands/{backend}-points.exr",
            "stage3/materials/strands/{backend}-curves.exr",
        ]
    },
    "core_volume_material_smoke.py": {
        "houdini_volume_material_smoke.py": [
            "stage3/materials/volumes/{backend}-absorption.exr",
            "stage3/materials/volumes/{backend}-emission.exr",
            "stage3/materials/volumes/{backend}-blackbody.exr",
        ]
    },
    "core_displacement_smoke.py": {
        "houdini_displacement_smoke.py": [
            "stage2/displacement/{backend}.exr",
            "stage2/displacement/{backend}-seams.exr",
        ]
    },
    "core_subdivision_smoke.py": {"houdini_subdivision_smoke.py": ["stage2/subdivision/{backend}-cube.exr"]},
}


def declared_outputs(inputs=CROSS_CASE_INPUTS):
    outputs = {}
    for dependencies in inputs.values():
        for producer, paths in dependencies.items():
            outputs.setdefault(producer, [])
            outputs[producer].extend(path for path in paths if path not in outputs[producer])
    return outputs


PRODUCER_OUTPUTS = declared_outputs()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(path)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def report_path(report, artifacts, root=ROOT):
    path = Path(report)
    if path.is_absolute():
        return path, None
    if artifacts:
        return Path(artifacts) / report.removeprefix("artifacts/"), "shared-resolver"
    return root / report, None


def validate_identity(path, case, runtime, manifest_digest, started_ns, run_id=None):
    require(
        path.is_file() and path.stat().st_mtime_ns >= started_ns,
        "Missing fresh runtime identity from selected Houdini process",
    )
    identity = read(path)
    if run_id is not None:
        require(identity.get("run_id") == run_id, "Child identity belongs to a stale or different run")
    require(
        identity.get("result") == "passed" and identity.get("test") == case["test"], "Incorrect child test identity"
    )
    require(
        identity.get("manifest_sha256", "").lower() == manifest_digest.lower(),
        "Child build manifest differs from selected runtime",
    )
    require(identity.get("houdini") == runtime["houdini"], "Child Houdini differs from selected runtime")
    require(identity.get("release") == runtime.get("release"), "Child release differs from selected runtime")
    host = Path(runtime["host"]).resolve()
    for field in ("python", "native", "hda", "resources"):
        require(Path(identity[field]).resolve().is_relative_to(host), "Child loaded outside verified host: " + field)
    if "--device" in case["arguments"] and case["test"] != "houdini_blend_instances_smoke.py":
        require(
            identity.get("selected_device", {}).get("backend") == case["device"],
            "Child selected a different device backend",
        )
    return identity


def output_paths(case, outputs=PRODUCER_OUTPUTS):
    patterns = outputs.get(case["test"], [])
    backends = ("cpu", "optix") if case["device"] == "script-declared" else (case["device"].lower(),)
    return list(dict.fromkeys(pattern.format(backend=backend) for pattern in patterns for backend in backends))


def capture_outputs(case, artifacts, started_ns, outputs=PRODUCER_OUTPUTS):
    records = []
    for relative in output_paths(case, outputs):
        path = Path(artifacts) / relative
        require(
            path.is_file() and path.stat().st_mtime_ns >= started_ns,
            "Producer did not write fresh required output: " + relative,
        )
        records.append(dict(path=relative, sha256=digest(path), bytes=path.stat().st_size))
    return records


def output_writers(completed):
    """Latest successful writer per tree; sibling private trees never alias."""
    latest = {}
    for row in completed.values():
        if row["result"] == "passed" and row["exit_code"] == 0:
            for output in row.get("producer_outputs", []):
                latest[(row["test"], output["path"], row.get("fixture_root", ""))] = (row, output)
    return latest


def producer_root(row, artifacts, destination=None):
    """Resolve a producer's own output tree within its verified evidence root."""
    relative = row.get("fixture_root")
    if not relative:
        return Path(artifacts).resolve()
    require(destination is not None, "Private producer requires an evidence root")
    evidence = Path(destination).resolve()
    root = (evidence / relative).resolve()
    require(
        not Path(relative).is_absolute() and root != evidence and root.is_relative_to(evidence),
        "Producer fixture escaped evidence root",
    )
    return root


def check_producers(case, completed, artifacts, *, destination=None, recipes=None, outputs=PRODUCER_OUTPUTS):
    required = producer_cases(case, recipes)
    # Later successful sibling backends can intentionally regenerate shared HIPs.
    latest = output_writers(completed)
    for producer in required:
        key = producer["test"], producer["device"]
        row = completed.get(key)
        require(
            row is not None and row["result"] == "passed" and row["exit_code"] == 0,
            "Required producer failed or was not executed in this run: " + "|".join(key),
        )
        require(bool(row.get("runtime_identity")), "Producer has no verified runtime identity")
        expected = output_paths(producer, outputs)
        require(
            {record["path"] for record in row.get("producer_outputs", [])} == set(expected),
            "Producer output receipt is incomplete",
        )
        for relative in expected:
            owner, record = latest[(producer["test"], relative, row.get("fixture_root", ""))]
            root = producer_root(owner, artifacts, destination)
            path = (root / relative).resolve()
            require(path.is_relative_to(root), "Producer output escaped fixture root")
            require(
                path.is_file() and path.stat().st_size == record["bytes"] and digest(path) == record["sha256"],
                "Producer output is missing or changed after success: " + relative,
            )
    return required


def input_records(case, required, completed, *, inputs=CROSS_CASE_INPUTS, artifacts=None, destination=None):
    """Bind each declared input to its required producer and latest successful writer."""
    declarations = inputs.get(case["test"])
    require(declarations is not None, "Missing cross-case input declaration: " + case["test"])
    require(
        {item["test"] for item in required} == set(declarations), "Cross-case input declaration differs from producers"
    )
    latest = output_writers(completed)
    records = []
    for producer in required:
        patterns = declarations[producer["test"]]
        declared_paths = set()
        for pattern in patterns:
            # Static files are shared outputs, even when their producer tests
            # both devices. Expand only explicitly backend-qualified paths.
            backends = (
                ("cpu", "optix")
                if producer["device"] == "script-declared" and "{backend}" in pattern
                else (producer["device"].lower(),)
            )
            for backend in backends:
                relative = pattern.format(backend=backend)
                require(relative not in declared_paths, "Cross-case input declaration duplicates a path")
                declared_paths.add(relative)
                required_row = completed.get((producer["test"], producer["device"]), {})
                key = (producer["test"], relative, required_row.get("fixture_root", ""))
                require(key in latest, "Missing producer output receipt: " + relative)
                owner, receipt = latest[key]
                provenance = {}
                if artifacts is not None:
                    root = producer_root(owner, artifacts, destination)
                    provenance["source_root"] = owner.get("fixture_root") or str(root)
                record = dict(
                    path=relative,
                    sha256=receipt["sha256"],
                    bytes=receipt["bytes"],
                    producer=producer["test"],
                    required_backend=producer["device"],
                    effective_backend=owner["device"],
                    **provenance,
                )
                previous = next((item for item in records if item["path"] == relative), None)
                if previous is not None:
                    # Two required backend runs can write the same shared HIP.
                    # Their prerequisite checks remain independent, but copy
                    # the latest verified shared output only once. Distinct
                    # producers or private trees must never silently collide.
                    require(
                        all(
                            previous.get(field) == record.get(field)
                            for field in ("producer", "effective_backend", "source_root", "sha256", "bytes")
                        ),
                        "Cross-case input declaration duplicates a path from different producer outputs",
                    )
                else:
                    records.append(record)
    require(
        bool(records) and len({record["path"] for record in records}) == len(records),
        "Cross-case input declaration is empty or duplicates a path",
    )
    return records


def stage_inputs(case, required, completed, artifacts, private_root, *, destination=None, inputs=CROSS_CASE_INPUTS):
    """Copy only verified prerequisites into a fresh consumer-owned fixture tree."""
    records = input_records(case, required, completed, inputs=inputs, artifacts=artifacts, destination=destination)
    private = Path(private_root).resolve()
    require(not private.exists(), "Consumer fixture root already exists")
    private.mkdir(parents=True)
    for record in records:
        relative = record["path"]
        source_root = Path(record["source_root"])
        if not source_root.is_absolute():
            source_root = Path(destination) / source_root
        source_root = source_root.resolve()
        source = (source_root / relative).resolve()
        target = (private / relative).resolve()
        require(
            source.is_relative_to(source_root) and target.is_relative_to(private),
            "Cross-case input escaped fixture root",
        )
        require(
            source.is_file() and source.stat().st_size == record["bytes"],
            "Producer input changed during staging: " + relative,
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        require(digest(target) == record["sha256"], "Staged producer input changed: " + relative)
    return records


def timeout_for(case, tier, requested):
    timeout = max(requested, 900) if tier == "nightly" or case["test"] == "native_review_contracts.py" else requested
    minima = {
        "houdini_mesh_payload_transport_smoke.py": 480,
        "houdini_mesh_field_transport_smoke.py": 480,
        "houdini_scene_query_displacement_smoke.py": 300,
        "houdini_oidn_provider_smoke.py": 300,
        "core_instance_motion_smoke.py": 660,
        "houdini_sampled_instance_attribute_smoke.py": 360,
        "houdini_material_ramp_override_smoke.py": 360,
        "houdini_geometry_key_bindings_smoke.py": 360,
        "core_camera_dof_motion_smoke.py": 900,
        "core_camera_intrinsic_motion_smoke.py": 900,
        "houdini_material_capacity_smoke.py": 900,
        "houdini_camera_intrinsic_motion_smoke.py": 600,
        "houdini_point_subject_stylesheets_smoke.py": 600,
        "houdini_tapered_volume_smoke.py": 600,
        "houdini_camera_dof_motion_smoke.py": 360,
        "houdini_material_expression_smoke.py": 360,
    }
    if tier == "nightly" and case["test"] == "pytest_contracts.py":
        timeout = max(timeout, 10800)
    return max(timeout, minima.get(case["test"], 0))


def run_child(command, *, stdout, stderr, timeout, env):
    with stdout.open("wb") as out, stderr.open("wb") as error:
        options = {"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.CREATE_NO_WINDOW}
        # Do not use Popen's context manager here: its __exit__ waits without
        # a timeout if the tree-termination helper itself raises or hangs.
        child = subprocess.Popen(command, stdout=out, stderr=error, env=env, **options)
        try:
            return child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            terminate_process(child)
            return -1


def snapshot_execution_sources(root, destination, plan):
    paths = {root / "tests" / case["test"] for case in plan if not case["gui"]}
    for name in ("native_test_entry.py", "native_runtime_identity.py", "native_test_runtime.py", "test-catalogue.json"):
        paths.add(root / "tests" / name)
    paths.update((root / "tests/support").glob("*.py"))
    paths.update((root / "examples").rglob("*.py"))
    for name in (
        "native_test_plan.py",
        "native_test_runner.py",
        "run-tests.ps1",
        "native-test-tiers.ps1",
        "native-test-context.ps1",
        "native-test-arguments.ps1",
        "test-dependency-context.ps1",
        "test-artifact-context.ps1",
        "archive-test-manifest.ps1",
        "build-layout.ps1",
    ):
        paths.add(root / "scripts" / name)
    records = []
    for path in sorted(paths):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        target = destination / "execution-sources" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        records.append(dict(path=relative, snapshot=target.relative_to(destination).as_posix(), sha256=digest(target)))
    return records


def audit_execution(summary_path):
    """Audit current execution inputs separately from the selected build's historical source."""
    summary_path = Path(summary_path).resolve()
    destination = summary_path.parent
    summary = read(summary_path)
    require(
        summary.get("result") in ("passed", "failed") and summary.get("finished_utc"),
        "Execution must be complete before audit",
    )
    indexed_plan = [(index, case) for index, case in enumerate(summary["plan"]) if not case["gui"]]
    plan = [case for _, case in indexed_plan]
    require(
        [(case["test"], case["device"]) for case in plan]
        == [(row["test"], row.get("device", "")) for row in summary["tests"]],
        "Execution differs from its ordered plan",
    )
    failed = any(row["result"] != "passed" or row["exit_code"] != 0 for row in summary["tests"])
    require(summary["result"] == ("failed" if failed else "passed"), "Execution summary contradicts child outcomes")
    source_root = Path(summary["execution_source_root"])
    for record in summary["execution_sources"]:
        archived = (destination / record["snapshot"]).resolve()
        require(archived.is_relative_to(destination), "Source snapshot escaped evidence root")
        require(
            digest(archived) == record["sha256"] == digest(source_root / record["path"]),
            "Execution source changed: " + record["path"],
        )
    runtime = summary["runtime_identity"]
    manifest_digest = summary["build_manifest_sha256"].lower()
    manifest_path = destination / "build-manifest.json"
    require(digest(manifest_path) == manifest_digest == digest(runtime["manifest"]), "Archived build manifest changed")
    manifest = read(manifest_path)
    build = Path(summary["build_directory"])
    host = Path(runtime["host"]).resolve()
    resource_prefix = str(Path(manifest["resource_root"]).parent).replace("\\", "/").rstrip("/") + "/"
    verified = []
    for record in manifest.get("host_runtime", []) + manifest.get("binary_artifacts", []):
        relative = record["path"].replace("\\", "/")
        if "/backup/" in relative or relative.endswith((".pyc", ".pdb", ".lib", ".exp")):
            continue
        path = host / relative[len(resource_prefix) :] if relative.startswith(resource_prefix) else build / relative
        require(digest(path).lower() == record["sha256"].lower(), "Selected runtime payload changed: " + relative)
        verified.append(relative)
    for record in manifest["kernels"]:
        require(
            digest(host / "resources/lib" / record["name"]).lower() == record["sha256"].lower(),
            "Selected kernel changed",
        )
    sources = {record["path"]: record["sha256"] for record in summary["execution_sources"]}
    if (source_root / "scripts/native_test_runner.py").is_file():
        require("scripts/native_test_runner.py" in sources, "Missing archived runner source")
    snapshot_runner = destination / "execution-sources/scripts/native_test_runner.py"
    private_contract_source = snapshot_runner.is_file() and b"private-fixtures-v1" in snapshot_runner.read_bytes()
    source_roots_contract = snapshot_runner.is_file() and b"private-fixtures-v2" in snapshot_runner.read_bytes()
    contract = summary.get("cross_case_input_contract")
    require(contract in (None, "private-fixtures-v1", "private-fixtures-v2"), "Unknown private fixture contract")
    if source_roots_contract:
        require(contract == "private-fixtures-v2", "Missing private fixture contract with source roots")
    elif private_contract_source:
        require(contract == "private-fixtures-v1", "Missing private fixture contract")
    strict_inputs = contract in ("private-fixtures-v1", "private-fixtures-v2")
    declarations = summary.get("cross_case_inputs", {}) if strict_inputs else {}
    if strict_inputs:
        for case in plan:
            consumer = case["test"]
            if consumer in CROSS_CASE_INPUTS:
                require(
                    declarations.get(consumer) == CROSS_CASE_INPUTS[consumer],
                    "Production cross-case input declaration changed: " + consumer,
                )
    for result_index, ((plan_index, case), row) in enumerate(zip(indexed_plan, summary["tests"], strict=True)):
        require(
            row["test_sha256"].lower() == sources["tests/" + row["test"]].lower(),
            "Executed test differs from source snapshot",
        )
        if row["result"] != "passed":
            continue
        require(row["exit_code"] == 0, "Passing entry has nonzero exit")
        if strict_inputs:
            prior = {(item["test"], item["device"]): item for item in summary["tests"][:result_index]}
            required = (
                producer_cases(case) if case["test"] in CROSS_CASE_INPUTS else row.get("producer_dependencies", [])
            )
            require(row.get("producer_dependencies", []) == required, "Producer dependencies differ from catalogue")
            if required:
                require(case["test"] in declarations, "Missing cross-case input declaration: " + case["test"])
                for producer in required:
                    previous = prior.get((producer["test"], producer["device"]))
                    require(
                        previous is not None
                        and previous["result"] == "passed"
                        and previous["exit_code"] == 0
                        and previous.get("runtime_identity"),
                        "Missing preceding verified producer",
                    )
                expected_inputs = input_records(
                    case,
                    required,
                    prior,
                    inputs=declarations,
                    artifacts=summary["fixture_artifacts"] if contract == "private-fixtures-v2" else None,
                    destination=destination,
                )
                require(
                    row.get("producer_inputs") == expected_inputs and bool(expected_inputs),
                    "Archived producer inputs differ from declared receipts",
                )
                expected_root = f"private-fixtures/{plan_index:03d}-{Path(case['test']).stem}-{case['device']}"
                require(row.get("fixture_root") == expected_root, "Private fixture root differs from plan")
            else:
                require(
                    not row.get("producer_inputs") and not row.get("fixture_root"),
                    "Independent case has undeclared staged inputs",
                )
        if row.get("producer_inputs"):
            fixture = (destination / row["fixture_root"]).resolve()
            require(fixture.is_relative_to(destination), "Private fixture escaped evidence root")
            for record in row["producer_inputs"]:
                source_root = Path(record["source_root"]) if contract == "private-fixtures-v2" else None
                if source_root is not None and not source_root.is_absolute():
                    source_root = (destination / source_root).resolve()
                    source = (source_root / record["path"]).resolve()
                    require(
                        source_root.is_relative_to(destination) and source.is_relative_to(source_root),
                        "Archived producer source escaped evidence root",
                    )
                    require(
                        source.is_file()
                        and source.stat().st_size == record["bytes"]
                        and digest(source) == record["sha256"],
                        "Archived private producer output changed: " + record["path"],
                    )
                staged = (fixture / record["path"]).resolve()
                require(staged.is_relative_to(fixture), "Staged input escaped private fixture")
                require(
                    staged.stat().st_size == record["bytes"] and digest(staged) == record["sha256"],
                    "Archived private input changed: " + record["path"],
                )
        identity_path = (destination / row["runtime_identity"]).resolve()
        require(identity_path.is_relative_to(destination), "Child identity escaped evidence root")
        require(digest(identity_path) == row["runtime_identity_sha256"], "Archived child identity changed")
        validate_identity(identity_path, case, runtime, manifest_digest, 0, summary["execution_id"])
        if case["report"]:
            evidence = (destination / row["evidence"]).resolve()
            require(evidence.is_relative_to(destination), "Child report escaped evidence root")
            require(digest(evidence) == row["evidence_sha256"], "Archived child report changed")
            report = read(evidence)
            if isinstance(report, dict) and "result" in report:
                require(report["result"] == "passed", "Archived report contradicts pass")
            else:
                require(
                    row.get("report_contract") == "legacy assertions, exit status and fresh report",
                    "Invalid legacy report contract",
                )
            audit_recipe(case["test"], report)
    audit = dict(
        result="passed",
        verification_result=summary["result"],
        audited_utc=datetime.now(timezone.utc).isoformat(),
        scope="Archived/current execution sources and reports; selected immutable host, auxiliary binaries, kernels and child identities. Build integration source belongs to the archived build manifest and is not claimed to match the changed working tree.",
        build_manifest_sha256=manifest_digest,
        execution_source_count=len(sources),
        runtime_payload_count=len(verified),
        kernel_count=len(manifest["kernels"]),
        test_count=len(summary["tests"]),
    )
    write(destination / "execution-audit.json", audit)
    return audit


def execute_headless(
    summary_path,
    *,
    tier,
    timeout=120,
    stop_on_failure=False,
    root=ROOT,
    executable=None,
    entrypoint=None,
    recipes=None,
    outputs=PRODUCER_OUTPUTS,
    inputs=CROSS_CASE_INPUTS,
):
    summary_path = Path(summary_path)
    summary = read(summary_path)
    destination = summary_path.parent
    runtime = summary["runtime_identity"]
    manifest = Path(runtime["manifest"])
    manifest_digest = summary["build_manifest_sha256"]
    artifacts = summary["fixture_artifacts"]
    summary["execution_id"] = uuid.uuid4().hex
    summary["cross_case_input_contract"] = "private-fixtures-v2"
    summary["cross_case_inputs"] = inputs
    summary["execution_source_root"] = str(root.resolve())
    summary["execution_sources"] = snapshot_execution_sources(root, destination, summary["plan"])
    write(summary_path, summary)
    completed = {}
    for index, case in enumerate(summary["plan"]):
        if case["gui"]:
            continue
        name = Path(case["test"]).stem + ("-" + case["device"] if case["device"] else "")
        source = root / "tests" / case["test"]
        started, started_ns = time.monotonic(), time.time_ns()
        entry = dict(
            test=case["test"],
            device=case["device"],
            seconds=0,
            exit_code=-1,
            result="failed",
            test_sha256=digest(source),
        )
        try:
            require(
                digest(manifest).lower() == manifest_digest.lower(), "Build identity changed during native test queue"
            )
            required = check_producers(
                case, completed, artifacts, destination=destination, recipes=recipes, outputs=outputs
            )
            case_artifacts = Path(artifacts)
            if required:
                entry["producer_dependencies"] = required
                case_artifacts = destination / "private-fixtures" / f"{index:03d}-{name}"
                staged = stage_inputs(
                    case, required, completed, artifacts, case_artifacts, destination=destination, inputs=inputs
                )
                entry["producer_inputs"] = staged
                entry["fixture_root"] = case_artifacts.relative_to(destination).as_posix()
            output_files = [case_artifacts / relative for relative in output_paths(case, outputs)]
            require(
                all(path.resolve().is_relative_to(case_artifacts.resolve()) for path in output_files),
                "Producer output escaped fixture root",
            )
            prerequisites = {(case_artifacts / record["path"]).resolve() for record in entry.get("producer_inputs", [])}
            require(
                not prerequisites.intersection(path.resolve() for path in output_files),
                "Declared producer output overlaps a staged prerequisite",
            )
            identity_path = destination / (name + ".runtime-identity.json")
            # A receipt belongs to this invocation, even if the caller selected
            # a shared fixture root containing future-dated stale files.
            identity_path.unlink(missing_ok=True)
            if case["report"]:
                report_path(case["report"], case_artifacts, root)[0].unlink(missing_ok=True)
            for output_file in output_files:
                output_file.unlink(missing_ok=True)
            env = dict(
                os.environ,
                CYCLES_HOUDINI_TEST_IDENTITY_REPORT=str(identity_path),
                CYCLES_HOUDINI_TEST_RUN_ID=summary["execution_id"],
                CYCLES_HOUDINI_TEST_ARTIFACTS=str(case_artifacts),
            )
            if required:
                env["CYCLES_HOUDINI_WORKER_LOG_DIR"] = str(case_artifacts / "workers")
            command = [
                str(executable or Path(runtime["houdini_root"]) / "bin/hython.exe"),
                str(entrypoint or root / "tests/native_test_entry.py"),
                str(source),
                *case["arguments"],
            ]
            code = run_child(
                command,
                stdout=destination / (name + ".stdout.log"),
                stderr=destination / (name + ".stderr.log"),
                timeout=timeout_for(case, tier, timeout),
                env=env,
            )
            entry["exit_code"] = code
            require(code == 0, "Native child exited " + str(code))
            validate_identity(identity_path, case, runtime, manifest_digest, started_ns, summary["execution_id"])
            entry["runtime_identity"] = identity_path.name
            entry["runtime_identity_sha256"] = digest(identity_path)
            for record in entry.get("producer_inputs", []):
                staged_path = case_artifacts / record["path"]
                require(
                    staged_path.stat().st_size == record["bytes"] and digest(staged_path) == record["sha256"],
                    "Consumer changed a verified prerequisite: " + record["path"],
                )
            if case["report"]:
                evidence, route = report_path(case["report"], case_artifacts, root)
                if route:
                    entry["artifact_route"] = "private-resolver" if required else route
                require(
                    evidence.is_file() and evidence.stat().st_mtime_ns >= started_ns,
                    "Test exited without fresh required evidence",
                )
                reported = read(evidence)
                if isinstance(reported, dict) and "result" in reported:
                    require(reported["result"] == "passed", "The required report did not record a pass")
                else:
                    require(tier == "nightly", "The required report did not record a pass")
                    entry["report_contract"] = "legacy assertions, exit status and fresh report"
                archived = destination / (name + ".json")
                shutil.copyfile(evidence, archived)
                entry["evidence"] = archived.name
                entry["evidence_sha256"] = digest(archived)
                domain = audit_recipe(case["test"], reported)
                if domain:
                    entry["domain_contract"] = domain
            captured = capture_outputs(case, case_artifacts, started_ns, outputs)
            if captured:
                entry["producer_outputs"] = captured
            entry["result"] = "passed"
        except (ValueError, KeyError, OSError) as error:
            entry["error"] = str(error)
        entry["seconds"] = time.monotonic() - started
        completed[(case["test"], case["device"])] = entry
        summary["tests"].append(entry)
        write(summary_path, summary)
        print(f"{entry['result']}: {case['test']} ({entry['seconds']:.2f} s)", flush=True)
        if stop_on_failure and entry["result"] != "passed":
            summary["execution_status"] = "stopped_on_failure"
            write(summary_path, summary)
            break
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", required=True, type=Path)
    parser.add_argument("--tier", choices=("fast", "extended", "nightly"))
    parser.add_argument("--audit", action="store_true")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--stop-on-failure", action="store_true")
    args = parser.parse_args()
    if args.audit:
        print(json.dumps(audit_execution(args.summary)))
    else:
        if not args.tier:
            parser.error("--tier is required for execution")
        execute_headless(args.summary, tier=args.tier, timeout=args.timeout, stop_on_failure=args.stop_on_failure)


if __name__ == "__main__":
    main()
