# SPDX-License-Identifier: Apache-2.0
"""Maintain source-derived contracts for every locked production renderer patch.

File overlap is dependency evidence, not a claim of minimal semantic dependency.
Behavioral fixtures are related existing assertions; patch replay is a
deterministic source consistency check for every entry. Backend certification and
feature-off image comparisons remain explicit release evidence.
"""

import argparse
import ast
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def fixture_execution(parsed, fixture):
    """Inventory invocation constraints without treating source as run evidence."""
    required = []
    for node in ast.walk(parsed):
        if (
            not isinstance(node, ast.Call)
            or not isinstance(node.func, ast.Attribute)
            or node.func.attr != "add_argument"
        ):
            continue
        if any(
            keyword.arg == "required" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True
            for keyword in node.keywords
        ):
            required.extend(
                argument.value
                for argument in node.args
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
            )
    return dict(
        status="No execution asserted by this source inventory",
        required_cli_arguments=required,
        verified_reference_subprocess_context=any(
            isinstance(node, ast.ImportFrom) and node.module == "native_test_runtime" for node in ast.walk(parsed)
        ),
        launch_contract="Use the verified current runtime launcher and supply required arguments; GUI fixtures require the GUI runner"
        if fixture
        else "No targeted behavioral fixture",
        historical_evidence="docs/upstream-patches.md is retained context; it does not qualify the current build",
    )


def inventory(root=ROOT):
    lock = json.loads((root / "source.lock.json").read_text(encoding="utf-8-sig"))
    contracts = json.loads((root / "patches/contracts.json").read_text())
    mappings = contracts["fixtures"]
    result = []
    touched = {}
    for name in lock["cycles"]["patches"]:
        # Patch IDs are permanent provenance, not positions in a shrinking stack.
        index = int(Path(name).name.split("-", 1)[0])
        patch = root / name
        content = patch.read_bytes()
        text = content.decode().replace(chr(13) + chr(10), chr(10))
        paths = sorted(set(re.findall(r"^diff --git a/\S+ b/(\S+)$", text, re.M)))
        if not paths:
            raise RuntimeError("No diff paths: " + name)
        fixture = mappings[str(index)]
        fixture_text = (root / fixture).read_text(encoding="utf-8-sig") if fixture else ""
        parsed = ast.parse(fixture_text)
        scope = (
            (ast.get_docstring(parsed) or "See assertions in the linked existing fixture.")
            if fixture
            else "No maintained targeted behavioral fixture established; source replay only. See docs/upstream-patches.md for historical evidence."
        )
        predecessors = sorted(set(previous for path in paths for previous in touched.get(path, [])))
        for path in paths:
            touched.setdefault(path, []).append(index)
        domains = []
        if any(path.startswith("src/kernel/") for path in paths):
            domains.append("Shared CPU/GPU kernel code (backend execution must be qualified separately)")
        if any("/device/" in path or "_gpu." in path for path in paths):
            domains.append("Device/transfer path; inspect listed files for backend guards")
        if any(path.startswith(("src/scene/", "src/subd/", "src/session/", "src/integrator/")) for path in paths):
            domains.append("Host scene/integrator path used by CPU/GPU")
        if not domains:
            domains.append("Host/build integration; see exact paths")
        result.append(
            dict(
                number=index,
                path=name,
                sha256=hashlib.sha256(content).hexdigest(),
                purpose=contracts.get("purposes", {}).get(patch.name, patch.stem.split("-", 1)[1].replace("-", " ")),
                affected_files=paths,
                dependencies=dict(
                    replay_order="Apply after every preceding locked patch",
                    shared_file_predecessors=predecessors,
                    evidence="Source diff path intersection; minimal semantic dependencies have not been established",
                ),
                backend_scope=domains,
                regression=dict(
                    command=["python", "scripts/verify-upstream-patches.py"],
                    related_fixture=fixture,
                    fixture_sha256=hashlib.sha256((root / fixture).read_bytes()).hexdigest() if fixture else None,
                    fixture_contract=scope,
                    assert_statements=sum(isinstance(node, ast.Assert) for node in ast.walk(parsed)),
                    unittest_assert_calls=sum(
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr.startswith("assert")
                        for node in ast.walk(parsed)
                    ),
                    execution=fixture_execution(parsed, fixture),
                    qualification="Related behavioral coverage; this inventory does not report an execution or an individual mutation test",
                ),
                retirement_condition="Retire only when the pinned upstream implements this contract and removing this patch plus replaying successors passes full-source comparison and the related fixture on affected production backends, including the feature-off control.",
            )
        )
    if set(mappings) != {str(row["number"]) for row in result}:
        raise RuntimeError("Fixture mapping differs from locked patch stack")
    return dict(
        schema_version=1,
        engine_commit=lock["cycles"]["commit"],
        patch_count=len(result),
        evidence_scope=__doc__,
        patches=result,
    )


def markdown(data):
    lines = [
        "# Production renderer patch contracts",
        "",
        "The table is generated from the locked patches and existing test sources. The full [machine-readable inventory](../patches/inventory.json) records exact hashes, files, assertion counts, related fixture docstrings and file-overlap predecessors. It covers all "
        + str(data["patch_count"])
        + " locked production patches.",
        "",
        "Every patch has a runnable deterministic source consistency check: `python scripts/verify-upstream-patches.py` replays the whole ordered stack and compares every tracked engine file plus additional source files. The linked behavioral fixtures contain related assertions; their listing does not certify an individual patch or a backend. Maintained reference subprocesses now resolve their executable and manifest through the explicit verified current runtime, with no historical build fallback. The inventory records required CLI arguments and makes no execution claim. See the [coverage audit](renderer-patch-coverage-audit.md) for bounded current qualification and remaining contract limits.",
        "",
        "Dependencies retain the complete locked order. File-overlap predecessors are evidence, not established minimal semantic dependencies. Backend scope is inferred from touched source paths; it is not an execution claim. Retirement requires upstream implementation, successful replay after removal, affected backend runs and feature-off controls. Pixel, alpha, EXR channel/type/metadata comparisons and mutation tests are still needed where a fixture does not assert those contracts.",
        "",
        "Regenerate with `python scripts/patch-inventory.py --write`; validate with `python scripts/patch-inventory.py --check`.",
        "",
        "| Patch | Purpose | Related existing behavioral fixture |",
        "|---|---|---|",
    ]
    for row in data["patches"]:
        lines.append(
            "| ["
            + str(row["number"]).zfill(4)
            + "](../"
            + row["path"]
            + ") | "
            + row["purpose"]
            + " | "
            + (
                "["
                + Path(row["regression"]["related_fixture"]).name
                + "](../"
                + row["regression"]["related_fixture"]
                + ")"
                if row["regression"]["related_fixture"]
                else "No targeted fixture established; historical evidence in upstream-patches.md"
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    data = inventory()
    files = {
        ROOT / "patches/inventory.json": json.dumps(data, indent=2) + "\n",
        ROOT / "docs/renderer-patch-contracts.md": markdown(data),
    }
    for path, expected in files.items():
        if args.write:
            path.write_text(expected, encoding="utf-8")
        elif not path.exists() or path.read_text(encoding="utf-8") != expected:
            raise RuntimeError("Stale patch inventory: " + str(path))
    print(
        json.dumps(
            dict(
                result="passed",
                patches=data["patch_count"],
                scope="source inventory and fixture existence, not native execution",
            )
        )
    )
