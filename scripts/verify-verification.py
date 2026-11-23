# SPDX-License-Identifier: Apache-2.0
"""Audit a completed run against the currently frozen development package.

Run before subsequent source/documentation changes. Test results and provenance
are separate: a failed regression run can still have fully verified provenance.
Older manifests may omit binary artifacts; that limitation is recorded.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def contained(base, relative):
    path = (base / relative).resolve()
    require(path.is_relative_to(base), f"Artifact path leaves its root: {relative}")
    return path


def validate_outcome(summary, report=None):
    """A provenance audit must not endorse a contradictory success claim."""
    if summary["result"] != "passed":
        return
    for entry in summary["tests"]:
        require(entry["result"] == "passed", "A successful summary contains a failed test")
        require(
            type(entry.get("exit_code")) is int and entry["exit_code"] == 0,
            "A successful test needs a recorded native exit code of zero",
        )
    if report is not None:
        require(report.get("result") == "passed", "The test report contradicts the successful summary")
        if "process_exit_code" in report:
            require(
                type(report["process_exit_code"]) is int and report["process_exit_code"] == 0,
                "A successful GUI report needs a recorded native exit code of zero",
            )


def recorded_build_directory(summary):
    """Select only a build explicitly recorded by this run's launcher."""
    direct = summary.get("build_directory")
    nested = (summary.get("runtime_identity") or {}).get("build_directory")
    require(direct or nested, "Verification summary has no recorded build directory; no historical fallback is allowed")
    paths = [Path(value) for value in (direct, nested) if value]
    require(all(path.is_absolute() for path in paths), "Recorded build directories must be absolute")
    require(all(path.resolve() == paths[0].resolve() for path in paths), "Recorded build contexts disagree")
    require(paths[0].is_dir(), "Recorded build directory does not exist")
    return paths[0].resolve()


def audit(run):
    root = Path(__file__).resolve().parents[1]
    summary = json.loads((run / "summary.json").read_text(encoding="utf-8-sig"))
    build = recorded_build_directory(summary)
    require(
        summary["result"] in ("passed", "failed") and "finished_utc" in summary,
        "The verification run must have completed before auditing",
    )
    outcome = run / "report.json"
    validate_outcome(summary, json.loads(outcome.read_text(encoding="utf-8-sig")) if outcome.is_file() else None)
    manifest_path = run / "build-manifest.json"
    manifest_hash = digest(manifest_path)
    require(manifest_hash == summary["build_manifest_sha256"].lower(), "Archived manifest hash differs from the run")
    require(digest(build / "build-manifest.json") == manifest_hash, "The current package manifest differs from the run")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))

    def verify_records(records, base, key):
        verified = []
        for entry in records:
            path = contained(base, entry[key])
            actual = digest(path)
            require(actual == entry["sha256"].lower(), f"Artifact changed: {entry[key]}")
            if "bytes" in entry:
                require(path.stat().st_size == entry["bytes"], f"Artifact size changed: {entry[key]}")
            verified.append({key: entry[key], "sha256": actual})
        return verified

    sources = verify_records(manifest["integration_source"], root, "path")
    patches = verify_records(manifest["applied_patches"], root, "path")
    resources = contained(build, manifest.get("resource_root", "resources"))
    kernels = verify_records(manifest["kernels"], resources / "lib", "name")
    binaries = verify_records(manifest.get("binary_artifacts", []), build, "path")
    host_runtime = verify_records(manifest.get("host_runtime", []), build, "path")
    tests = []
    for entry in summary["tests"]:
        if "test_sha256" not in entry:
            require(
                entry["result"] == "failed" and entry.get("error"), f"Executed test has no source hash: {entry['test']}"
            )
            tests.append(
                dict(
                    test=entry["test"],
                    result=entry["result"],
                    source="Not executed; runner diagnostic",
                    error=entry["error"],
                )
            )
            continue
        actual = digest(contained(root / "tests", entry["test"]))
        require(actual == entry["test_sha256"].lower(), f"Test source changed: {entry['test']}")
        tests.append(
            dict(
                test=entry["test"],
                device=entry.get("device"),
                repeat=entry.get("repeat"),
                result=entry["result"],
                sha256=actual,
            )
        )
    report = dict(
        result="passed",
        audited_utc=datetime.now(timezone.utc).isoformat(),
        scope="Completed run manifest, integration sources, executed test entries, patches, kernels and recorded compiled artifacts before later documentation changes.",
        verification_result=summary["result"],
        build_manifest_sha256=manifest_hash,
        binary_provenance="recorded" if binaries else "not recorded in this older manifest",
        test_entry_count=len(tests),
        test_source_count=sum("sha256" in entry for entry in tests),
        integration_source_count=len(sources),
        patch_count=len(patches),
        kernel_count=len(kernels),
        binary_artifact_count=len(binaries),
        tests=tests,
        host_runtime_count=len(host_runtime),
        host_runtime=host_runtime,
        integration_source=sources,
        patches=patches,
        kernels=kernels,
        binary_artifacts=binaries,
    )
    (run / "source-audit.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "result",
                    "verification_result",
                    "test_entry_count",
                    "integration_source_count",
                    "patch_count",
                    "kernel_count",
                    "binary_artifact_count",
                    "build_manifest_sha256",
                )
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path, help="Completed artifacts/verification/<run> directory")
    audit(parser.parse_args().run.resolve())
