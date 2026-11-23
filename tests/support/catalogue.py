# SPDX-License-Identifier: Apache-2.0
"""Incremental pytest selection using the existing native catalogue and runner."""

import json
import hashlib
from pathlib import Path

from support.paths import ROOT


# The production planner is host-independent; support code uses that same owner.
import sys

sys.path.insert(0, str(ROOT / "scripts"))
from native_test_plan import catalogue_cases, headless_recipes, required_cases


def case_id(case):
    return case["test"].removesuffix(".py") + "-" + case["device"]


def read_queue_result(directory, expected, *, runner_failed=False):
    summaries = list(Path(directory).glob("*/summary.json"))
    if len(summaries) != 1:
        raise AssertionError("Expected one catalogue runner summary in " + str(directory))
    summary = json.loads(summaries[0].read_text(encoding="utf-8-sig"))
    if summary["scope"] != "native-catalogue-selection":
        raise AssertionError("Catalogue run has incorrect qualification scope")
    ordered = [(case["test"], case["device"]) for case in expected]
    plan = [(row["test"], row["device"]) for row in summary.get("plan", [])]
    if plan != ordered:
        raise AssertionError("Catalogue runner plan differs from selected producer closure")
    entries = {(row["test"], row.get("device", "")): row for row in summary["tests"]}
    executed = [(row["test"], row.get("device", "")) for row in summary["tests"]]
    if executed != ordered or len(entries) != len(ordered):
        raise AssertionError("Catalogue runner did not execute exactly its selected producer closure")
    failed = any(row["result"] != "passed" or row["exit_code"] != 0 for row in entries.values())
    if (
        summary.get("result") != ("failed" if failed else "passed")
        or not summary.get("finished_utc")
        or summary.get("runner_error")
        or (runner_failed and not failed)
    ):
        raise AssertionError("Catalogue runner did not complete consistently: " + str(summary.get("runner_error", "")))
    manifest = summaries[0].parent / "build-manifest.json"
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    runtime = summary["runtime_identity"]
    if digest != summary["build_manifest_sha256"].lower() or digest != runtime["manifest_sha256"].lower():
        raise AssertionError("Catalogue runtime identity differs from the archived build manifest")
    for key, row in entries.items():
        if row["result"] != "passed":
            continue
        identity_path = (summaries[0].parent / row["runtime_identity"]).resolve()
        if not identity_path.is_relative_to(summaries[0].parent.resolve()):
            raise AssertionError("Catalogue child identity escapes its evidence directory")
        identity = json.loads(identity_path.read_text(encoding="utf-8-sig"))
        if (
            identity.get("result") != "passed"
            or identity.get("test") != key[0]
            or identity.get("manifest_sha256", "").lower() != digest
            or identity.get("houdini") != runtime["houdini"]
        ):
            raise AssertionError("Catalogue child runtime identity is invalid: " + key[0])
        for field in ("python", "native", "hda", "resources"):
            if not Path(identity[field]).resolve().is_relative_to(Path(runtime["host"]).resolve()):
                raise AssertionError("Catalogue child loaded outside the verified host: " + field)
        if key[1] in ("CPU", "OPTIX") and identity.get("selected_device", {}).get("backend") != key[1]:
            raise AssertionError("Catalogue child used a different device backend: " + key[0])
    return entries
