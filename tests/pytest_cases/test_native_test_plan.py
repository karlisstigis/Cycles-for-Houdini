# SPDX-License-Identifier: Apache-2.0
"""Frozen plan payloads and actual fresh-process producer/failure contracts."""

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from support.paths import ROOT
from support import workers
from support.workers import ordinary_python, pure_python_environment

sys.path.insert(0, str(ROOT / "scripts"))
import native_test_plan as planner
import native_test_runner as runner

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


@pytest.mark.parametrize(
    "defect,expected",
    [
        (None, None),
        ("truncated", "Archived plan differs"),
        ("incomplete", "Fast CI results differ"),
        ("duplicate", "Fast CI results differ"),
        ("substituted", "Fast CI results differ"),
        ("wrong-tier", "completed passing fast-tier run"),
        ("wrong-evidence-path", "Archived plan differs"),
        ("changed-report", "Fast CI report changed"),
        ("swapped-reports", "Fast CI report does not belong"),
        ("reused-report", "Fast CI report does not belong"),
        ("changed-manifest", "Archived fast CI manifest hash differs"),
    ],
)
def test_fast_qualification_requires_the_official_plan_and_reports(tmp_path, defect, expected):
    plan = planner.tier_cases("fast", tmp_path)
    entries = []
    for case in plan:
        entry = dict(test=case["test"], device=case["device"], result="passed", exit_code=0)
        if case["report"]:
            report = tmp_path / f"{Path(entry['test']).stem}-{entry['device']}.json"
            report.write_text('{"result":"passed"}', encoding="utf-8")
            entry.update(evidence=report.name, evidence_sha256=hashlib.sha256(report.read_bytes()).hexdigest())
        entries.append(entry)
    scope = "native-extended" if defect == "wrong-tier" else "native-fast"
    if defect == "truncated":
        plan.pop()
        entries.pop()
    elif defect == "incomplete":
        entries.pop()
    elif defect == "duplicate":
        entries[-1] = entries[0]
    elif defect == "substituted":
        entries[-1]["test"] = "offline_archive_test.py"
    elif defect == "wrong-evidence-path":
        next(case for case in plan if case["report"])["report"] = "unverified.json"
    elif defect == "changed-report":
        (tmp_path / entries[-1]["evidence"]).write_text('{"result":"failed"}', encoding="utf-8")
    elif defect in ("swapped-reports", "reused-report"):
        first, second = [entry for entry in entries if "evidence" in entry][:2]
        for field in ("evidence", "evidence_sha256"):
            if defect == "swapped-reports":
                first[field], second[field] = second[field], first[field]
            else:
                second[field] = first[field]
    manifest = b'{"identity":"ci"}'
    (tmp_path / "build-manifest.json").write_bytes(b"{}" if defect == "changed-manifest" else manifest)
    summary = dict(
        result="passed",
        scope=scope,
        finished_utc="2026-11-20T00:00:00Z",
        build_manifest_sha256=hashlib.sha256(manifest).hexdigest(),
        plan=plan,
        tests=entries,
    )
    (tmp_path / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/workstation-ci.py"), "verify-fast", str(tmp_path / "summary.json")],
        capture_output=True,
        text=True,
        check=False,
    )
    if expected:
        assert result.returncode != 0 and expected in result.stderr
    else:
        assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("tier,count", [("fast", 17), ("extended", 29), ("nightly", 531)])
def test_full_ordered_payload_preserves_pre_migration_plan_with_readiness_case(tier, count):
    before = json.loads((ROOT / "tests/fixtures/runner-plan-before-stage4.json").read_text())
    actual = planner.tier_cases(tier, before["destination"])
    assert len(actual) == count
    # Preserve the frozen migration oracle. Readiness, K22 and volume bounds add
    # explicit gates; every old entry and its order stay identical.
    additions = [row for row in actual if row["test"] == "houdini_no_blender_smoke.py"]
    report = str(Path(before["destination"]) / "no-blender-first-image.json")
    assert additions == [
        dict(
            test="houdini_no_blender_smoke.py",
            device="CPU",
            report=report,
            arguments=["--device", "CPU", "--report", report],
            gui=False,
            prerequisites=["verified-build", "houdini-sdk"],
        )
    ]
    color = [row for row in actual if row["test"] == "houdini_color_config_smoke.py"]
    assert color == (
        []
        if tier == "fast"
        else [
            dict(
                test="houdini_color_config_smoke.py",
                device="script-declared" if tier == "nightly" else "CPU and default device",
                report="artifacts/stage3/color/houdini-config/report.json",
                arguments=[],
                gui=False,
                prerequisites=["verified-build", "houdini-sdk"],
            )
        ]
    )
    additions += color
    volume_bounds = [row for row in actual if row["test"] == "core_object_volume_bounds_smoke.py"]
    assert volume_bounds == (
        [
            dict(
                test="core_object_volume_bounds_smoke.py",
                device=backend,
                report="artifacts/stage2/objects/volume-bounds/report.json",
                arguments=["--device", backend],
                gui=False,
                prerequisites=["verified-build", "houdini-sdk"],
            )
            for backend in ("CPU", "OPTIX")
        ]
        if tier == "nightly"
        else []
    )
    additions += volume_bounds
    new_contract_recipes = (
        ("houdini_shader_preview_displacement_smoke.py", "artifacts/stage3/materials/preview-displacement.json"),
        (
            "houdini_material_motion_mesh_gate_smoke.py",
            "artifacts/stage3/stylesheet-storage/material-motion-mesh-gate/report.json",
        ),
        ("houdini_packed_alias_conversion_smoke.py", "artifacts/stage3/packed-alias-conversion/report.json"),
    )
    new_contracts = (
        [
            dict(
                test=name,
                device=backend,
                report=report,
                arguments=["--device", backend],
                gui=False,
                prerequisites=["verified-build", "houdini-sdk"],
            )
            for name, report in new_contract_recipes
            for backend in ("CPU", "OPTIX")
        ]
        if tier == "nightly"
        else []
    )
    names = {name for name, _ in new_contract_recipes}
    assert [row for row in actual if row["test"] in names] == new_contracts
    additions += new_contracts
    # The fatal GPU reply check (installer candidate 7429a7a0) runs once, script-declared, in nightly.
    fatal_gpu = (
        [
            dict(
                test="houdini_fatal_gpu_render_smoke.py",
                device="script-declared",
                report="artifacts/stage3/fatal-gpu-render/report.json",
                arguments=[],
                gui=False,
                prerequisites=["verified-build", "houdini-sdk"],
            )
        ]
        if tier == "nightly"
        else []
    )
    assert [row for row in actual if row["test"] == "houdini_fatal_gpu_render_smoke.py"] == fatal_gpu
    additions += fatal_gpu
    # The curve-mapping table check (independent curve-mapping rewrite) runs once, script-declared, in nightly.
    curve_mapping = (
        [
            dict(
                test="houdini_curve_mapping_table_smoke.py",
                device="script-declared",
                report="artifacts/stage3/materials/curve-mapping/report.json",
                arguments=[],
                gui=False,
                prerequisites=["verified-build", "houdini-sdk"],
            )
        ]
        if tier == "nightly"
        else []
    )
    assert [row for row in actual if row["test"] == "houdini_curve_mapping_table_smoke.py"] == curve_mapping
    additions += curve_mapping
    # The community build checks (identity, Setup transactions, engine edit loop; git only) run in every tier.
    community = [
        dict(
            test="community_build_test.py",
            device="CPU",
            report="",
            arguments=[],
            gui=False,
            prerequisites=["git"],
        )
    ]
    assert [row for row in actual if row["test"] == "community_build_test.py"] == community
    additions += community
    assert [row for row in actual if row not in additions] == before["plans"][tier]


def test_all_cross_case_inputs_are_declared_against_catalogue_producers():
    recipes = {row["test"]: row for row in planner.headless_recipes()}
    dependent = {name for name, row in recipes.items() if row["depends_on"]}
    assert set(runner.CROSS_CASE_INPUTS) == dependent and len(dependent) == 10
    for consumer, declarations in runner.CROSS_CASE_INPUTS.items():
        assert set(declarations) == set(recipes[consumer]["depends_on"])
        for producer, paths in declarations.items():
            assert paths and set(paths) <= set(runner.PRODUCER_OUTPUTS[producer])
    assert (
        "stage3/textures/gui-texture.exr"
        in runner.CROSS_CASE_INPUTS["houdini_texture_auto_batch_smoke.py"]["houdini_texture_smoke.py"]
    )
    assert (
        "examples/sop-fx-courtyard/showcase.ocio"
        in runner.CROSS_CASE_INPUTS["houdini_queued_worker_smoke.py"]["houdini_showcase_refresh_smoke.py"]
    )


def test_missing_production_input_declaration_fails_closed():
    case = dict(test="houdini_texture_auto_batch_smoke.py", device="OPTIX")
    required = planner.producer_cases(case)
    with pytest.raises(ValueError, match="Missing cross-case input declaration"):
        runner.input_records(case, required, {}, inputs={})


@pytest.mark.parametrize(
    "consumer",
    ["houdini_transparency_batch_smoke.py", "houdini_texture_cache_smoke.py", "houdini_texture_normal_smoke.py"],
)
def test_dual_backend_producer_stages_each_shared_input_once(tmp_path, consumer):
    case = dict(test=consumer, device="script-declared")
    required = planner.producer_cases(case)
    completed = {}
    expected = []
    for producer in required:
        paths = runner.output_paths(producer)
        expected.extend(paths)
        for relative in paths:
            path = tmp_path / "shared" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(relative.encode())
        completed[(producer["test"], producer["device"])] = dict(
            producer,
            result="passed",
            exit_code=0,
            runtime_identity="verified.json",
            producer_outputs=runner.capture_outputs(producer, tmp_path / "shared", 0),
        )
    assert runner.check_producers(case, completed, tmp_path / "shared", destination=tmp_path) == required
    missing = dict(completed)
    missing.pop((required[0]["test"], required[0]["device"]))
    with pytest.raises(ValueError, match="Required producer failed"):
        runner.check_producers(case, missing, tmp_path / "shared", destination=tmp_path)
    records = runner.stage_inputs(
        case, required, completed, tmp_path / "shared", tmp_path / "private", destination=tmp_path
    )
    assert [row["path"] for row in records] == list(dict.fromkeys(expected))
    for row in records:
        assert (tmp_path / "private" / row["path"]).read_bytes() == row["path"].encode()
        assert row["required_backend"] == required[0]["device"]
        assert row["effective_backend"] == required[-1]["device"]


@pytest.mark.parametrize("patterns", [["shared.hiplc", "shared.hiplc"], ["{backend}.exr", "cpu.exr"]])
def test_explicit_duplicate_input_declarations_remain_rejected(patterns):
    producer = dict(test="producer.py", device="script-declared")
    completed = {
        ("producer.py", "script-declared"): dict(
            producer,
            result="passed",
            exit_code=0,
            producer_outputs=[
                dict(path=name, sha256="receipt", bytes=1) for name in ("shared.hiplc", "cpu.exr", "optix.exr")
            ],
        )
    }
    with pytest.raises(ValueError, match="duplicates a path"):
        runner.input_records(
            dict(test="consumer.py", device="script-declared"),
            [producer],
            completed,
            inputs={"consumer.py": {"producer.py": patterns}},
        )


def test_dual_backend_input_patterns_keep_both_device_receipts():
    producer = dict(test="producer.py", device="script-declared")
    completed = {
        ("producer.py", "script-declared"): dict(
            producer,
            result="passed",
            exit_code=0,
            producer_outputs=[dict(path=backend + ".exr", sha256=backend, bytes=1) for backend in ("cpu", "optix")],
        )
    }
    records = runner.input_records(
        dict(test="consumer.py", device="script-declared"),
        [producer],
        completed,
        inputs={"consumer.py": {"producer.py": ["{backend}.exr"]}},
    )
    assert [(row["path"], row["sha256"]) for row in records] == [("cpu.exr", "cpu"), ("optix.exr", "optix")]


@pytest.mark.parametrize("collision", ["different-producer", "private-siblings"])
def test_overlapping_inputs_from_distinct_output_trees_are_rejected(tmp_path, collision):
    required = [dict(test="producer.py", device="CPU"), dict(test="producer.py", device="OPTIX")]
    if collision == "different-producer":
        required[1]["test"] = "other-producer.py"
    completed = {}
    for producer in required:
        row = dict(
            producer,
            result="passed",
            exit_code=0,
            producer_outputs=[dict(path="scene.hiplc", sha256="same-bytes", bytes=1)],
        )
        if collision == "private-siblings":
            row["fixture_root"] = "private-fixtures/" + producer["device"]
        completed[(producer["test"], producer["device"])] = row
    with pytest.raises(ValueError, match="different producer outputs"):
        runner.input_records(
            dict(test="consumer.py", device="script-declared"),
            required,
            completed,
            inputs={"consumer.py": {row["test"]: ["scene.hiplc"] for row in required}},
            artifacts=tmp_path / "shared",
            destination=tmp_path,
        )


def test_every_argument_bearing_nightly_case_matches_its_real_cli(tmp_path):
    """Port of 1b15956: validate parser AST declarations without render side effects."""
    for case in planner.tier_cases("nightly", tmp_path):
        if not case["arguments"]:
            continue
        tree = ast.parse((ROOT / "tests" / case["test"]).read_text(encoding="utf-8-sig"))
        parser = argparse.ArgumentParser(add_help=False, conflict_handler="resolve", exit_on_error=False)
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument"
            ):
                continue
            if not all(isinstance(arg, ast.Constant) and isinstance(arg.value, str) for arg in node.args):
                continue
            keywords = {}
            for keyword in node.keywords:
                if keyword.arg in ("action", "choices", "nargs", "required", "dest", "const"):
                    keywords[keyword.arg] = ast.literal_eval(keyword.value)
                elif keyword.arg == "type" and isinstance(keyword.value, ast.Name):
                    value_type = {"int": int, "float": float, "str": str, "Path": Path}.get(keyword.value.id)
                    if value_type is not None:
                        keywords["type"] = value_type
            parser.add_argument(*(arg.value for arg in node.args), **keywords)
        try:
            parser.parse_args(case["arguments"])
        except (argparse.ArgumentError, SystemExit) as error:
            pytest.fail(f"{case['test']}[{case['device']}] has incompatible dispatched arguments: {error}")


def test_selection_expands_producers_and_rejects_invalid_partitions(tmp_path):
    plan = planner.tier_cases("nightly", tmp_path)
    selected = [dict(test="houdini_texture_auto_batch_smoke.py", device="OPTIX")]
    actual = planner.select_plan(plan, cases=selected)
    assert [(row["test"], row["device"]) for row in actual] == [
        ("houdini_texture_smoke.py", "OPTIX"),
        (selected[0]["test"], "OPTIX"),
    ]
    with pytest.raises(ValueError, match="required producer"):
        planner.select_plan(plan, names=[selected[0]["test"]])
    for cases in (
        [],
        selected * 2,
        [dict(test="unknown.py", device="CPU")],
        [dict(test=selected[0]["test"], device="CUDA")],
    ):
        with pytest.raises(ValueError):
            planner.select_plan(plan, cases=cases)


def test_dependency_planning_sorts_a_misordered_input_and_rejects_missing_backend():
    recipes = [
        dict(test="consumer.py", backends=["CPU"], depends_on=["producer.py"]),
        dict(test="producer.py", backends=["CPU"], depends_on=[]),
    ]
    assert planner.required_cases([dict(test="consumer.py", device="CPU")], recipes) == [
        dict(test="producer.py", device="CPU"),
        dict(test="consumer.py", device="CPU"),
    ]
    recipes[1]["backends"] = ["OPTIX"]
    with pytest.raises(ValueError, match="compatible producer"):
        planner.required_cases([dict(test="consumer.py", device="CPU")], recipes)


def fake_queue(tmp_path, monkeypatch, mode="pass", *, sabotage=False, source_revision="modern", gui_in_plan=False):
    root = tmp_path / "root"
    (root / "tests").mkdir(parents=True)
    (root / "scripts").mkdir()
    runner_source = root / "scripts/native_test_runner.py"
    if source_revision == "modern":
        runner_source.write_bytes(Path(runner.__file__).read_bytes())
    elif source_revision == "private-v1":
        runner_source.write_bytes(b'CROSS_CASE_INPUT_CONTRACT = "private-fixtures-v1"\n')
    else:
        # Older receipts archived the runner source but had no private-input
        # schema. Keep this fixture usable in a source ZIP or tagless checkout.
        runner_source.write_bytes(b'PRODUCER_OUTPUTS = {"producer.py": ["scene.hiplc"]}\n')
    destination = tmp_path / "run"
    destination.mkdir()
    artifacts = tmp_path / "fixtures"
    artifacts.mkdir()
    names = ["producer.py", "consumer.py"]
    if sabotage:
        names.insert(1, "sabotage.py")
    if mode.startswith("chain"):
        if mode == "chain-tamper":
            names.append("chain_sabotage.py")
        names.append("downstream.py")
    for name in names:
        (root / "tests" / name).write_text("# source identity for " + name)
    manifest = tmp_path / "build-manifest.json"
    manifest.write_text(
        '{"build":"one","resource_root":"host/resources","host_runtime":[],"binary_artifacts":[],"kernels":[]}'
    )
    runtime = dict(
        host=str(tmp_path / "host"), houdini="22.0.466", manifest=str(manifest), houdini_root="unused", release=None
    )
    plan = [
        dict(
            test=name,
            device="CPU",
            arguments=["--device", "CPU", "--label", ""],
            report="artifacts/" + name + ".json",
            gui=False,
        )
        for name in names
    ]
    if gui_in_plan:
        plan.insert(1, dict(test="gui.py", device="CPU", arguments=[], report="", gui=True))
    summary = dict(
        build_directory=str(tmp_path),
        runtime_identity=runtime,
        build_manifest_sha256=runner.digest(manifest),
        fixture_artifacts=str(artifacts),
        tests=[],
        plan=plan,
    )
    runner.write(destination / "summary.json", summary)
    (destination / "build-manifest.json").write_bytes(manifest.read_bytes())
    config = dict(
        runtime=runtime,
        digest=runner.digest(manifest),
        artifacts=str(artifacts),
        mode=mode,
        destination=str(destination),
    )
    monkeypatch.setenv("CH_QUEUE_PROBE", json.dumps(config))
    native_launcher = runner.run_child

    def run_fake_child(command, **arguments):
        # The probe simulates native receipts; ordinary Python needs only its
        # explicit test token/path, not inherited Houdini package startup.
        env = pure_python_environment()
        for name in (
            "CYCLES_HOUDINI_TEST_IDENTITY_REPORT",
            "CYCLES_HOUDINI_TEST_RUN_ID",
            "CYCLES_HOUDINI_TEST_ARTIFACTS",
        ):
            env[name] = arguments["env"][name]
        arguments["env"] = env
        return native_launcher(command, **arguments)

    monkeypatch.setattr(runner, "run_child", run_fake_child)
    probe = tmp_path / "real child entry.py"
    probe.write_text("""import json,os,sys,time
from pathlib import Path
config=json.loads(os.environ["CH_QUEUE_PROBE"])
name=Path(sys.argv[1]).name
shared=Path(config["artifacts"])
root=Path(os.environ["CYCLES_HOUDINI_TEST_ARTIFACTS"])
(shared/(name+".started")).write_text(json.dumps(dict(pid=os.getpid(),arguments=sys.argv[2:],fixture=str(root))))
if name=="producer.py" and config["mode"]=="exit":
    raise SystemExit(7)
if name=="producer.py" and config["mode"]=="timeout":
    time.sleep(60)
identity=dict(result="passed",run_id=os.environ.get("CYCLES_HOUDINI_TEST_RUN_ID"),test=name,manifest_sha256=config["digest"],houdini=config["runtime"]["houdini"],release=None,selected_device=dict(backend="CPU"))
identity.update({field:str(Path(config["runtime"]["host"])/field) for field in ("python","native","hda","resources")})
if name=="producer.py":
    if config["mode"]=="identity-manifest":identity["manifest_sha256"]="bad"
    if config["mode"]=="identity-backend":identity["selected_device"]["backend"]="OPTIX"
    if config["mode"]=="identity-host":identity["native"]=str(root/"outside.pyd")
    if config["mode"]=="identity-test":identity["test"]="someone_else.py"
    if config["mode"]=="identity-run":identity["run_id"]="another-run"
if not(name=="producer.py" and config["mode"]=="stale-identity"):
    Path(os.environ["CYCLES_HOUDINI_TEST_IDENTITY_REPORT"]).write_text(json.dumps(identity))
if not(name=="producer.py" and config["mode"]=="stale-report"):
    (root/(name+".json")).write_text(json.dumps(dict(result="failed" if config["mode"]=="report-failed" and name=="producer.py" else "passed")))
if name=="producer.py" and config["mode"]!="stale-output":
    (root/"scene.hiplc").write_bytes(b"fresh producer output")
if name=="sabotage.py":
    (shared/"scene.hiplc").write_bytes(b"corrupt between producer and consumer")
if name=="consumer.py" and config["mode"]=="composed-read":
    (root/("old"+"-only.json")).read_text()
if name=="consumer.py" and config["mode"].startswith("chain"):
    assert (root/"scene.hiplc").read_bytes()==b"fresh producer output"
    if config["mode"]!="chain-missing":
        (root/"derived.hiplc").write_bytes(b"fresh derived output")
    if config["mode"]=="chain-mutate":
        (root/"scene.hiplc").write_bytes(b"consumer mutation")
if name=="chain_sabotage.py":
    (Path(config["destination"])/"private-fixtures/001-consumer-CPU/derived.hiplc").write_bytes(b"wrong derived output")
if name=="downstream.py":
    assert (root/"derived.hiplc").read_bytes()==b"fresh derived output"
    assert not (root/"scene.hiplc").exists()
""")
    for path in (artifacts / "producer.py.json", artifacts / "scene.hiplc"):
        path.write_text('{"result":"passed"}')
        os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    recipes = [
        dict(test=name, backends=["CPU"], depends_on=["producer.py"] if name == "consumer.py" else []) for name in names
    ]
    outputs = {"producer.py": ["scene.hiplc"]}
    inputs = {"consumer.py": {"producer.py": ["scene.hiplc"]}}
    if mode.startswith("chain"):
        recipes[-1]["depends_on"] = ["consumer.py"]
        outputs["consumer.py"] = ["derived.hiplc"]
        inputs["downstream.py"] = {"consumer.py": ["derived.hiplc"]}
        stale = artifacts / "derived.hiplc"
        stale.write_bytes(b"stale derived output")
        os.utime(stale, ns=(9_000_000_000_000_000_000, 9_000_000_000_000_000_000))
    return (
        destination,
        artifacts,
        dict(
            root=root,
            executable=ordinary_python(),
            entrypoint=probe,
            recipes=recipes,
            outputs=outputs,
            inputs=inputs,
        ),
    )


def test_actual_child_queue_has_fresh_distinct_processes_and_preserves_empty_cli_values(tmp_path, monkeypatch):
    destination, artifacts, options = fake_queue(tmp_path, monkeypatch)
    result = runner.execute_headless(destination / "summary.json", tier="fast", **options)
    assert [row["result"] for row in result["tests"]] == ["passed", "passed"]
    children = [runner.read(artifacts / (name + ".started")) for name in ("producer.py", "consumer.py")]
    assert children[0]["pid"] != children[1]["pid"]
    assert all(child["arguments"] == ["--device", "CPU", "--label", ""] for child in children)
    assert result["tests"][1]["producer_dependencies"] == [dict(test="producer.py", device="CPU")]
    assert result["tests"][0]["producer_outputs"][0]["sha256"] == runner.digest(artifacts / "scene.hiplc")
    private = destination / result["tests"][1]["fixture_root"]
    assert (private / "scene.hiplc").read_bytes() == b"fresh producer output"
    assert (private / "consumer.py.json").is_file()
    assert result["tests"][1]["artifact_route"] == "private-resolver"
    assert children[1]["fixture"] == str(private)


def test_composed_undeclared_read_cannot_see_old_file_in_shared_root(tmp_path, monkeypatch):
    destination, artifacts, options = fake_queue(tmp_path, monkeypatch, "composed-read")
    (artifacts / "old-only.json").write_text('{"result":"passed"}')
    result = runner.execute_headless(destination / "summary.json", tier="fast", **options)
    assert [row["result"] for row in result["tests"]] == ["passed", "failed"]
    private = destination / result["tests"][1]["fixture_root"]
    assert not (private / "old-only.json").exists()
    assert (private / "scene.hiplc").is_file()


@pytest.mark.parametrize("mode", ["chain", "chain-locked-shared", "chain-missing", "chain-mutate", "chain-tamper"])
def test_private_consumer_producer_chain_uses_verified_own_outputs(tmp_path, monkeypatch, mode):
    destination, shared, options = fake_queue(tmp_path, monkeypatch, mode)
    stale = shared / "derived.hiplc"
    stale_mtime = stale.stat().st_mtime_ns
    if mode == "chain-locked-shared":
        unlink = Path.unlink

        def protected_unlink(path, *args, **kwargs):
            if path.resolve() == stale.resolve():
                raise PermissionError("Unrelated shared output is locked")
            return unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", protected_unlink)
    summary_path = destination / "summary.json"
    result = runner.execute_headless(summary_path, tier="fast", **options)
    producer, intermediate, *_, downstream = result["tests"]
    assert producer["result"] == "passed"
    assert stale.read_bytes() == b"stale derived output"
    assert stale.stat().st_mtime_ns == stale_mtime
    if mode in ("chain", "chain-locked-shared"):
        assert all(row["result"] == "passed" for row in result["tests"])
        receipt = downstream["producer_inputs"][0]
        assert receipt["source_root"] == intermediate["fixture_root"]
        assert receipt["sha256"] == intermediate["producer_outputs"][0]["sha256"]
        assert receipt["sha256"] == runner.digest(destination / intermediate["fixture_root"] / receipt["path"])
        assert receipt["sha256"] != runner.digest(stale)
        assert intermediate["producer_inputs"][0]["source_root"] == str(shared.resolve())
        assert (destination / downstream["fixture_root"] / receipt["path"]).read_bytes() == b"fresh derived output"
        result.update(result="passed", finished_utc="2026-11-09T00:00:00Z")
        runner.write(summary_path, result)
        assert runner.audit_execution(summary_path)["test_count"] == 3
    else:
        assert downstream["result"] == "failed"
        assert not (shared / "downstream.py.started").exists()
        if mode == "chain-tamper":
            assert intermediate["result"] == "passed"
            assert "missing or changed after success" in downstream["error"]
        elif mode == "chain-missing":
            assert "fresh required output" in intermediate["error"]
        else:
            assert "changed a verified prerequisite" in intermediate["error"]


def test_private_producer_rejects_overlapping_output_before_deleting_staged_input(tmp_path, monkeypatch):
    destination, shared, options = fake_queue(tmp_path, monkeypatch, "chain")
    options["outputs"]["consumer.py"] = ["scene.hiplc"]
    result = runner.execute_headless(destination / "summary.json", tier="fast", **options)
    assert [row["result"] for row in result["tests"]] == ["passed", "failed", "failed"]
    intermediate = result["tests"][1]
    assert "overlaps a staged prerequisite" in intermediate["error"]
    assert (destination / intermediate["fixture_root"] / "scene.hiplc").read_bytes() == b"fresh producer output"
    assert (shared / "scene.hiplc").read_bytes() == b"fresh producer output"
    assert not (shared / "consumer.py.started").exists()
    assert not (shared / "downstream.py.started").exists()


@pytest.mark.parametrize("forgery", [None, "../outside", "private-fixtures/other", "shared", "missing"])
def test_chain_audit_binds_source_roots_to_the_preceding_writer(tmp_path, monkeypatch, forgery):
    destination, shared, options = fake_queue(tmp_path, monkeypatch, "chain")
    summary_path = destination / "summary.json"
    result = runner.execute_headless(summary_path, tier="fast", **options)
    result.update(result="passed", finished_utc="2026-11-09T00:00:00Z")
    receipt = result["tests"][-1]["producer_inputs"][0]
    if forgery == "missing":
        receipt.pop("source_root")
    else:
        receipt["source_root"] = str(shared) if forgery == "shared" else forgery
    runner.write(summary_path, result)
    with pytest.raises(ValueError, match="producer inputs differ"):
        runner.audit_execution(summary_path)


@pytest.mark.parametrize("missing", [False, True])
def test_chain_audit_rechecks_retained_private_producer_bytes(tmp_path, monkeypatch, missing):
    destination, _, options = fake_queue(tmp_path, monkeypatch, "chain")
    summary_path = destination / "summary.json"
    result = runner.execute_headless(summary_path, tier="fast", **options)
    result.update(result="passed", finished_utc="2026-11-09T00:00:00Z")
    source = destination / result["tests"][1]["fixture_root"] / "derived.hiplc"
    if missing:
        source.unlink()
    else:
        source.write_bytes(b"wrong derived output")
    runner.write(summary_path, result)
    with pytest.raises(ValueError, match="Archived private producer output changed"):
        runner.audit_execution(summary_path)


@pytest.mark.parametrize("private_producers", [False, True])
def test_sibling_rewrite_is_verified_and_backend_mismatch_is_rejected(tmp_path, private_producers):
    shared = tmp_path / "shared"
    shared.mkdir()
    path = shared / "scene.hiplc"
    path.write_bytes(b"cpu")
    cpu = dict(
        test="producer.py",
        device="CPU",
        result="passed",
        exit_code=0,
        runtime_identity="cpu.json",
        producer_outputs=[dict(path="scene.hiplc", sha256=runner.digest(path), bytes=3)],
    )
    path.write_bytes(b"optix")
    optix = dict(
        test="producer.py",
        device="OPTIX",
        result="passed",
        exit_code=0,
        runtime_identity="optix.json",
        producer_outputs=[dict(path="scene.hiplc", sha256=runner.digest(path), bytes=5)],
    )
    if private_producers:
        for row, content in ((cpu, b"cpu"), (optix, b"optix")):
            row["fixture_root"] = "private-fixtures/producer-" + row["device"]
            fixture = tmp_path / row["fixture_root"]
            fixture.mkdir(parents=True)
            (fixture / "scene.hiplc").write_bytes(content)
    recipes = [
        dict(test="producer.py", backends=["CPU", "OPTIX"], depends_on=[]),
        dict(test="consumer.py", backends=["CPU"], depends_on=["producer.py"]),
    ]
    consumer = dict(test="consumer.py", device="CPU")
    with pytest.raises(ValueError, match="Required producer failed"):
        runner.check_producers(
            consumer,
            {("producer.py", "OPTIX"): optix},
            shared,
            destination=tmp_path,
            recipes=recipes,
            outputs={"producer.py": ["scene.hiplc"]},
        )
    completed = {("producer.py", "CPU"): cpu, ("producer.py", "OPTIX"): optix}
    required = runner.check_producers(
        consumer, completed, shared, destination=tmp_path, recipes=recipes, outputs={"producer.py": ["scene.hiplc"]}
    )
    records = runner.stage_inputs(
        consumer,
        required,
        completed,
        shared,
        tmp_path / "private",
        destination=tmp_path,
        inputs={"consumer.py": {"producer.py": ["scene.hiplc"]}},
    )
    expected_backend = "CPU" if private_producers else "OPTIX"
    assert (tmp_path / "private/scene.hiplc").read_bytes() == expected_backend.lower().encode()
    assert records[0]["required_backend"] == "CPU" and records[0]["effective_backend"] == expected_backend


@pytest.mark.parametrize("fixture_root", ["../outside", ".", "absolute"])
def test_private_producer_root_cannot_escape_selected_evidence(tmp_path, fixture_root):
    if fixture_root == "absolute":
        fixture_root = str(tmp_path / "private")
    with pytest.raises(ValueError, match="escaped evidence root"):
        runner.producer_root(dict(fixture_root=fixture_root), tmp_path / "shared", tmp_path)


@pytest.mark.parametrize(
    "mode",
    [
        "exit",
        "stale-report",
        "stale-output",
        "report-failed",
        "identity-manifest",
        "identity-backend",
        "identity-host",
        "identity-test",
        "identity-run",
        "stale-identity",
        "timeout",
    ],
)
def test_actual_queue_propagates_failure_and_rejects_stale_outputs_and_wrong_runtime(tmp_path, monkeypatch, mode):
    destination, artifacts, options = fake_queue(tmp_path, monkeypatch, mode)
    if mode == "timeout":
        monkeypatch.setattr(runner, "timeout_for", lambda *args: 0.2)
    result = runner.execute_headless(destination / "summary.json", tier="fast", **options)
    assert [row["result"] for row in result["tests"]] == ["failed", "failed"]
    assert "Required producer failed" in result["tests"][1]["error"]
    assert not (artifacts / "consumer.py.started").exists()


def test_actual_queue_blocks_changed_producer_output_even_after_success(tmp_path, monkeypatch):
    destination, artifacts, options = fake_queue(tmp_path, monkeypatch, sabotage=True)
    result = runner.execute_headless(destination / "summary.json", tier="fast", **options)
    assert [row["result"] for row in result["tests"]] == ["passed", "passed", "failed"]
    assert "changed after success" in result["tests"][-1]["error"]
    assert not (artifacts / "consumer.py.started").exists()


def test_stop_on_failure_preserves_partial_evidence_and_records_stop(tmp_path, monkeypatch):
    destination, artifacts, options = fake_queue(tmp_path, monkeypatch, "exit")
    result = runner.execute_headless(destination / "summary.json", tier="fast", stop_on_failure=True, **options)
    assert len(result["tests"]) == 1 and result["execution_status"] == "stopped_on_failure"
    assert not (artifacts / "consumer.py.started").exists()


def test_opt_in_domain_and_generic_recipes_collect_each_backend_identity_once(tmp_path):
    result = subprocess.run(
        [
            str(ordinary_python()),
            "-m",
            "pytest",
            "-c",
            "tests/pytest.ini",
            "--collect-only",
            "-q",
            "--catalogue",
            "tests/pytest_cases/test_catalogue_native.py",
            "tests/pytest_cases/test_dependent_recipes.py",
            "--artifacts",
            str(tmp_path / "evidence"),
        ],
        cwd=ROOT,
        env=pure_python_environment([Path(pytest.__file__).resolve().parent.parent]),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    ids = [line.rsplit("[", 1)[1].removesuffix("]") for line in result.stdout.splitlines() if "::test_" in line]
    expected = [row["test"].removesuffix(".py") + "-" + row["device"] for row in planner.catalogue_cases()]
    assert len(ids) == len(set(ids)) == 522
    assert {
        "houdini_shader_preview_displacement_smoke-CPU",
        "houdini_shader_preview_displacement_smoke-OPTIX",
    } <= set(ids)
    assert set(ids) == set(expected)
    assert not list((tmp_path / "evidence").rglob("selection.json"))


@pytest.mark.parametrize(
    "corruption",
    [
        None,
        "identity",
        "report",
        "source",
        "outside-run",
        "outcome",
        "inputs-missing",
        "inputs-forged",
        "inputs-backend",
        "fixture",
    ],
)
def test_execution_audit_binds_archived_reports_identity_paths_and_source_bytes(tmp_path, monkeypatch, corruption):
    destination, artifacts, options = fake_queue(tmp_path, monkeypatch)
    summary_path = destination / "summary.json"
    result = runner.execute_headless(summary_path, tier="fast", **options)
    result.update(result="passed", finished_utc="2026-11-09T00:00:00Z")
    row = result["tests"][0]
    if corruption == "identity":
        identity = runner.read(destination / row["runtime_identity"])
        identity["test"] = "different.py"
        runner.write(destination / row["runtime_identity"], identity)
    elif corruption == "report":
        runner.write(destination / row["evidence"], dict(result="passed", forged=True))
    elif corruption == "source":
        (options["root"] / "tests/producer.py").write_text("changed after execution")
    elif corruption == "outside-run":
        row["runtime_identity"] = "../another-run/identity.json"
    elif corruption == "outcome":
        row["result"] = "failed"
    elif corruption == "inputs-missing":
        result["tests"][1].pop("producer_inputs")
    elif corruption == "inputs-forged":
        result["tests"][1]["producer_inputs"][0]["sha256"] = "0" * 64
    elif corruption == "inputs-backend":
        result["tests"][1]["producer_inputs"][0]["effective_backend"] = "OPTIX"
    elif corruption == "fixture":
        result["tests"][1]["fixture_root"] = "../shared"
    runner.write(summary_path, result)
    if corruption is None:
        audit = runner.audit_execution(summary_path)
        assert audit["result"] == "passed" and audit["test_count"] == 2
    else:
        with pytest.raises(ValueError):
            runner.audit_execution(summary_path)


def test_modern_archive_cannot_discard_private_input_contract(tmp_path, monkeypatch):
    destination, _, options = fake_queue(tmp_path, monkeypatch)
    summary_path = destination / "summary.json"
    result = runner.execute_headless(summary_path, tier="fast", **options)
    result.update(result="passed", finished_utc="2026-11-09T00:00:00Z")
    result.pop("cross_case_input_contract")
    result.pop("cross_case_inputs")
    result["tests"][1].pop("producer_inputs")
    result["tests"][1].pop("fixture_root")
    runner.write(summary_path, result)
    with pytest.raises(ValueError, match="Missing private fixture contract"):
        runner.audit_execution(summary_path)
    result["cross_case_input_contract"] = "private-fixtures-v1"
    runner.write(summary_path, result)
    with pytest.raises(ValueError, match="Missing private fixture contract"):
        runner.audit_execution(summary_path)
    result["cross_case_input_contract"] = "private-fixtures-v3"
    runner.write(summary_path, result)
    with pytest.raises(ValueError, match="Unknown private fixture contract"):
        runner.audit_execution(summary_path)


def test_historical_archive_without_private_input_contract_still_audits(tmp_path, monkeypatch):
    destination, _, options = fake_queue(tmp_path, monkeypatch, source_revision="legacy")
    summary_path = destination / "summary.json"
    result = runner.execute_headless(summary_path, tier="fast", **options)
    result.update(result="passed", finished_utc="2026-11-09T00:00:00Z")
    result.pop("cross_case_input_contract")
    result.pop("cross_case_inputs")
    result["tests"][1].pop("producer_inputs")
    result["tests"][1].pop("fixture_root")
    runner.write(summary_path, result)
    assert runner.audit_execution(summary_path)["result"] == "passed"


def test_historical_private_v1_archive_without_source_roots_still_audits(tmp_path, monkeypatch):
    destination, _, options = fake_queue(tmp_path, monkeypatch, source_revision="private-v1")
    summary_path = destination / "summary.json"
    result = runner.execute_headless(summary_path, tier="fast", **options)
    result.update(result="passed", finished_utc="2026-11-09T00:00:00Z", cross_case_input_contract="private-fixtures-v1")
    for receipt in result["tests"][1]["producer_inputs"]:
        receipt.pop("source_root")
    runner.write(summary_path, result)
    assert runner.audit_execution(summary_path)["result"] == "passed"


def test_private_fixture_archive_uses_full_plan_index_with_gui_between_cases(tmp_path, monkeypatch):
    destination, _, options = fake_queue(tmp_path, monkeypatch, gui_in_plan=True)
    summary_path = destination / "summary.json"
    result = runner.execute_headless(summary_path, tier="fast", **options)
    assert result["tests"][1]["fixture_root"].startswith("private-fixtures/002-")
    result.update(result="passed", finished_utc="2026-11-09T00:00:00Z")
    runner.write(summary_path, result)
    assert runner.audit_execution(summary_path)["result"] == "passed"


def test_future_dated_stale_receipt_is_removed_before_real_child_launch(tmp_path, monkeypatch):
    destination, artifacts, options = fake_queue(tmp_path, monkeypatch, "stale-identity")
    identity = destination / "producer-CPU.runtime-identity.json"
    identity.write_text('{"result":"passed"}')
    os.utime(identity, ns=(9_000_000_000_000_000_000, 9_000_000_000_000_000_000))
    result = runner.execute_headless(destination / "summary.json", tier="fast", **options)
    assert result["tests"][0]["result"] == "failed"
    assert "fresh runtime identity" in result["tests"][0]["error"]
    assert not (artifacts / "consumer.py.started").exists()


@pytest.mark.parametrize("helper_error", ["timeout", "oserror"])
def test_tree_termination_helper_failure_still_kills_and_reaps_direct_child(tmp_path, monkeypatch, helper_error):
    class Child:
        pid = 123

        def __init__(self):
            self.killed = False
            self.waits = []

        def wait(self, *, timeout):
            self.waits.append(timeout)
            if not self.killed:
                raise subprocess.TimeoutExpired("native-child", timeout)
            return -9

        def poll(self):
            return -9 if self.killed else None

        def kill(self):
            self.killed = True

    child = Child()
    from types import SimpleNamespace

    monkeypatch.setattr(runner, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(workers, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(runner.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: child)

    def failed_helper(*args, **kwargs):
        if helper_error == "timeout":
            raise subprocess.TimeoutExpired("taskkill", kwargs["timeout"])
        raise OSError("taskkill unavailable")

    monkeypatch.setattr(runner.subprocess, "run", failed_helper)
    assert (
        runner.run_child(["native-child"], stdout=tmp_path / "out", stderr=tmp_path / "err", timeout=0.1, env={}) == -1
    )
    assert child.killed and child.waits == [0.1, 15]

    # The lifetime/owned-worker fixture helper must have the same bounded
    # fallback, including when its taskkill process cannot start or times out.
    lifetime_child = Child()
    monkeypatch.setattr(workers.subprocess, "Popen", lambda *args, **kwargs: lifetime_child)
    with pytest.raises(subprocess.TimeoutExpired):
        workers.run_process(["lifetime-child"], timeout=0.1, destination=tmp_path / "lifetime")
    assert lifetime_child.killed and lifetime_child.waits == [0.1, 15]


@pytest.mark.parametrize("platform,expected", [("nt", {"creationflags": 0x08000000}), ("posix", {})])
def test_fixture_console_options_preserve_non_windows_behavior(monkeypatch, platform, expected):
    from types import SimpleNamespace

    monkeypatch.setattr(workers, "os", SimpleNamespace(name=platform))
    monkeypatch.setattr(workers.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    assert workers.console_options() == expected


@pytest.mark.skipif(os.name != "nt", reason="Windows console ownership contract")
def test_pythonw_fixture_keeps_powershell_and_its_python_child_consoleless(tmp_path):
    import shutil

    python = ordinary_python()
    pythonw = python.with_name("pythonw.exe")
    pwsh = shutil.which("pwsh")
    if not pythonw.is_file() or not pwsh:
        pytest.skip("A GUI Python interpreter and PowerShell 7 are required")
    report = tmp_path / "python-console.txt"
    child = tmp_path / "child.py"
    child.write_text(
        "import ctypes,sys\nfrom pathlib import Path\n"
        "Path(sys.argv[1]).write_text(str(ctypes.windll.kernel32.GetConsoleWindow()))\n"
    )
    powershell = tmp_path / "parent.ps1"
    powershell.write_text(
        "param($Python,$Child,$Report)\n"
        "Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; "
        'public static class ConsoleProbe {[DllImport("kernel32.dll")] '
        "public static extern IntPtr GetConsoleWindow();}'\n"
        "[ConsoleProbe]::GetConsoleWindow().ToInt64()\n"
        "& $Python $Child $Report\nexit $LASTEXITCODE\n"
    )
    owner = tmp_path / "owner.py"
    owner.write_text(
        "import sys\nsys.path.insert(0,sys.argv[1])\nfrom support.workers import run_process\n"
        "run_process(sys.argv[3:],timeout=20,destination=sys.argv[2])\n"
    )
    destination = tmp_path / "powershell-output"
    workers.run_process(
        [
            str(pythonw),
            str(owner),
            str(Path(workers.__file__).parents[1]),
            str(destination),
            pwsh,
            "-NoLogo",
            "-NoProfile",
            "-File",
            str(powershell),
            str(python),
            str(child),
            str(report),
        ],
        timeout=45,
        destination=tmp_path / "owner-output",
    )
    assert (destination / "stdout.log").read_text(encoding="utf-8-sig").strip() == "0"
    assert report.read_text() == "0"
