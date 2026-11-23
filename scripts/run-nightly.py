# SPDX-License-Identifier: Apache-2.0
"""Run independent nightly fixture groups in parallel or qualify a completed nightly."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts/run-tests.ps1"


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write(path, data):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temp.replace(path)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def manifest_digest(value):
    require(
        isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value) is not None,
        "Malformed build manifest SHA-256",
    )
    return value.lower()


# Also support loading this CLI through runpy in contract tests.
sys.path.insert(0, str(ROOT / "scripts"))
from native_test_plan import tier_cases


def plan_for(destination):
    return tier_cases("nightly", destination)


def case_key(entry):
    return entry["test"], entry["device"]


def partition(plan, count):
    """Keep report producers, consumers and both backends in one ordered queue."""
    components = {case["test"]: {case["test"]} for case in plan}
    catalogue = read(ROOT / "tests/test-catalogue.json")
    for case in catalogue["headless"]:
        for producer in case["depends_on"]:
            group = components[producer] | components[case["test"]]
            for name in group:
                components[name] = group
    groups = list(dict.fromkeys(frozenset(group) for group in components.values()))
    sizes = {group: sum(case["test"] in group for case in plan) for group in groups}
    # The nested native pytest suite is a queue by itself. Other workers can
    # run the independent fixtures while it evaluates its child contracts.
    sizes[next(group for group in groups if "pytest_contracts.py" in group)] += len(plan) // count
    buckets = [[] for _ in range(count)]
    loads = [0] * count
    for group in sorted(groups, key=sizes.get, reverse=True):
        index = loads.index(min(loads))
        buckets[index].extend(sorted(group))
        loads[index] += sizes[group]
    return buckets


def run_part(part, names, build, houdini, optix_device):
    part.mkdir()
    selection = part / "selection.json"
    write(selection, names)
    command = [
        "pwsh",
        "-NoProfile",
        "-File",
        str(RUNNER),
        "-Tier",
        "nightly",
        "-BuildDirectory",
        str(build),
        "-Artifacts",
        str(part),
        "-NightlySelectionFile",
        str(selection),
    ]
    if houdini:
        command += ["-HoudiniRoot", str(houdini)]
    env = os.environ.copy()
    if optix_device:
        env["CYCLES_HOUDINI_TEST_OPTIX_DEVICE"] = optix_device
    with (part / "runner.log").open("wb") as log:
        result = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    paths = list(part.glob("*/summary.json"))
    require(len(paths) == 1, f"Expected one partition summary in {part}, exit {result.returncode}")
    summary = read(paths[0])
    require(summary["scope"] == "native-nightly-partition", f"Unexpected scope in {part}")
    expected = [case for case in plan_for(paths[0].parent) if case["test"] in names]
    require(summary.get("plan") == expected, f"Partition plan changed in {part}")
    require(result.returncode == 0 or summary.get("result") != "passed", f"Runner failed after passing in {part}")
    return paths[0].parent, summary


def aggregate(run, builds, summary):
    expected_digest = manifest_digest(summary["build_manifest_sha256"])
    records = {}
    for directory, part in builds:
        require(part["result"] in ("passed", "failed"), f"Incomplete partition {directory}")
        require(
            manifest_digest(part["build_manifest_sha256"]) == expected_digest,
            "Mixed build manifests",
        )
        if "runtime_identity" in summary:
            require(part["runtime_identity"] == summary["runtime_identity"], "Mixed loaded runtime identities")
        summary["runtime_identity"] = part["runtime_identity"]
        require(
            [case_key(entry) for entry in part["tests"]] == [case_key(case) for case in part["plan"]],
            f"Missing or reordered results in {directory}",
        )
        for original in part["tests"]:
            entry = dict(original)
            if "producer_inputs" in original:
                entry["producer_inputs"] = [dict(record) for record in original["producer_inputs"]]
            key = case_key(entry)
            require(key not in records, f"Duplicate result {key}")
            for field in ("evidence", "runtime_identity"):
                if field in entry:
                    path = (directory / entry[field]).resolve()
                    require(path.is_relative_to(directory) and path.is_file(), f"Missing {field} for {key}")
                    entry[field] = path.relative_to(run).as_posix()
            if "fixture_root" in entry:
                fixture = (directory / entry["fixture_root"]).resolve()
                require(fixture.is_relative_to(directory) and fixture.is_dir(), f"Missing private fixture for {key}")
                entry["fixture_root"] = fixture.relative_to(run).as_posix()
            for record in entry.get("producer_inputs", []):
                if "source_root" in record:
                    source = Path(record["source_root"])
                    if not source.is_absolute():
                        source = (directory / source).resolve()
                        require(
                            source.is_relative_to(directory) and source.is_dir(),
                            f"Missing private producer source for {key}",
                        )
                        record["source_root"] = source.relative_to(run).as_posix()
                    else:
                        require(
                            source.resolve() == Path(part["fixture_artifacts"]).resolve(),
                            f"Unexpected shared producer source for {key}",
                        )
            records[key] = entry
    keys = [case_key(case) for case in summary["plan"]]
    require(len(keys) == len(set(keys)) and set(keys) == set(records), "Nightly cases differ from the full plan")
    summary["tests"] = [records[key] for key in keys]
    return all(part["result"] == "passed" for _, part in builds) and all(
        e["result"] == "passed" for e in records.values()
    )


def run_parallel(args):
    devices = args.optix_device or [None] * args.jobs
    require(len(devices) == args.jobs, "Supply one OptiX device ID per worker")
    if args.optix_device:
        require(len(set(devices)) == len(devices), "Use a distinct OptiX device for each worker")
    run = args.run.resolve()
    run.mkdir(parents=True)
    build = args.build_directory.resolve()
    shutil.copyfile(build / "build-manifest.json", run / "build-manifest.json")
    summary = dict(
        scope="native-nightly",
        result="running",
        started_utc=datetime.now(timezone.utc).isoformat(),
        build_directory=str(build),
        build_manifest_sha256=hashlib.sha256((run / "build-manifest.json").read_bytes()).hexdigest().upper(),
        plan=plan_for(run),
        tests=[],
    )
    write(run / "summary.json", summary)
    try:
        with ThreadPoolExecutor(args.jobs) as pool:
            jobs = [
                pool.submit(run_part, run / f"part-{index + 1}", names, build, args.houdini_root, devices[index])
                for index, names in enumerate(partition(summary["plan"], args.jobs))
            ]
            parts = [job.result() for job in jobs]
        passed = aggregate(run, parts, summary)
        summary["result"] = "passed" if passed else "failed"
    except Exception as error:
        summary.update(result="failed", error=str(error))
        raise
    finally:
        summary["finished_utc"] = datetime.now(timezone.utc).isoformat()
        write(run / "summary.json", summary)
    return 0 if passed else 1


def verify(args):
    run = args.run.resolve()
    summary = read(run / "summary.json")
    build = args.build_directory.resolve()
    require(
        summary.get("scope") == "native-nightly" and summary.get("result") == "passed",
        "Need a complete passing nightly",
    )
    require(Path(summary["build_directory"]).resolve() == build, "Nightly exercised a different build directory")
    expected = plan_for(run)
    require(summary["plan"] == expected, "Archived plan differs from the current official nightly")
    keys = [case_key(case) for case in expected]
    require(
        len(set(keys)) == len(keys) and keys == [case_key(entry) for entry in summary["tests"]],
        "Missing, duplicate, or unexpected nightly case",
    )
    host = Path(summary["runtime_identity"]["host"]).resolve()
    manifest = read(build / "build-manifest.json")
    staged_host = (build / manifest["resource_root"]).resolve().parent
    require(host == staged_host, "The nightly did not load the host staged for packaging")
    for case, entry in zip(expected, summary["tests"], strict=True):
        require(entry["result"] == "passed" and entry["exit_code"] == 0, f"Failed case {case_key(case)}")

        def evidence(field):
            path = (run / entry[field]).resolve()
            require(path.is_relative_to(run), f"Evidence outside nightly for {case_key(case)}")
            return read(path)

        identity = evidence("runtime_identity")
        require(
            identity["result"] == "passed"
            and identity.get("test") == case["test"]
            and identity.get("manifest_sha256", "").upper() == summary["build_manifest_sha256"].upper()
            and identity["houdini"] == summary["runtime_identity"]["houdini"]
            and all(
                Path(identity[field]).resolve().is_relative_to(host)
                for field in ("python", "native", "hda", "resources")
            ),
            f"Unexpected loaded runtime for {case_key(case)}",
        )
        if "--device" in case["arguments"] and case["test"] != "houdini_blend_instances_smoke.py":
            require(
                identity.get("selected_device", {}).get("backend") == case["device"],
                f"Missing or incorrect selected backend for {case_key(case)}",
            )
        if case["report"]:
            report = evidence("evidence")
            require(
                report.get("result") == "passed"
                if isinstance(report, dict) and "result" in report
                else entry.get("report_contract") == "legacy assertions, exit status and fresh report",
                f"Report contradicts result for {case_key(case)}",
            )
    subprocess.run([sys.executable, str(ROOT / "scripts/verify-verification.py"), str(run)], check=True)
    print(f"Verified {len(keys)} nightly cases against {summary['build_manifest_sha256']}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "verify"))
    parser.add_argument(
        "--run", required=True, type=Path, help="New parallel run directory, or completed nightly to verify"
    )
    parser.add_argument("--build-directory", required=True, type=Path)
    parser.add_argument("--houdini-root", type=Path)
    parser.add_argument("--jobs", type=int, choices=range(1, 5), default=1)
    parser.add_argument(
        "--optix-device", action="append", help="Exact Cycles device ID for each parallel worker, in order"
    )
    args = parser.parse_args()
    raise SystemExit(run_parallel(args) if args.action == "run" else verify(args))
