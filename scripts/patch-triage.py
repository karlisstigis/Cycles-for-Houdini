# SPDX-License-Identifier: Apache-2.0
"""Check complete, hash-bound renderer patch triage and generate the review table."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def build_or_reviewed_glue(content, reviewed, integrator_seam):
    # Read both sides, including deleted and renamed files. Unknown or quoted
    # diff headers cannot establish that a change is confined to build glue.
    headers = [line for line in content.splitlines() if line.startswith(b"diff --git ")]
    if not headers:
        return False
    for header in headers:
        match = re.fullmatch(rb"diff --git a/([^\s]+) b/([^\s]+)", header)
        if not match:
            return False
        for path in match.groups():
            if path.startswith(b"src/kernel/") or b".." in path.split(b"/"):
                return False
            if path.startswith(b"src/integrator/") and not integrator_seam:
                return False
            if not reviewed and not (
                path == b"src/CMakeLists.txt" or re.fullmatch(rb"src/cmake/[\w./-]+\.cmake", path)
            ):
                return False
    return True


def check_classification(data, row, content):
    name = row["path"]
    category = row.get("classification", "")
    exception = data["reviewed_exceptions"].get(name, {})
    reviewed = exception.get("classification") == category and exception.get("sha256") == row["sha256"]
    if category == "glue":
        valid = build_or_reviewed_glue(
            content, reviewed, reviewed and exception.get("scope") == "integrator-session-output"
        )
    elif category == "fix":
        valid = name in data["upstream_queue"]
    elif category.startswith("feature/"):
        valid = category.removeprefix("feature/") in data["approved_features"]
    else:
        # Historical patch filenames stay stable when a preceding patch retires.
        # Future exports cannot opt back into a transition by copying its tag.
        original = re.fullmatch(r"patches/(\d{4})-[^/]+\.patch", name)
        valid = (
            reviewed
            and original
            and int(original[1]) <= 86
            and category
            in {
                "pending-measurement/ris",
                "planned-retirement/ocio",
            }
        )
    if not valid:
        raise RuntimeError(f"Unapproved fork policy classification {category!r} for {name}")


def validate(root):
    lock = json.loads((root / "source.lock.json").read_text(encoding="utf-8-sig"))["cycles"]
    data = json.loads((root / "patches/triage.json").read_text(encoding="utf-8"))
    rows = data["patches"]
    if data["upstream"] != lock["commit"] or [row["path"] for row in rows] != lock["patches"]:
        raise RuntimeError("Patch triage must cover the complete locked stack in order")
    names = set(lock["patches"])
    if len(names) != len(rows) or set(lock.get("patch_sha256", {})) != names:
        raise RuntimeError("Expected one lock digest per distinct patch")
    for feature, adr in data["approved_features"].items():
        if not feature or not re.fullmatch(r"docs/decisions/\d{4}-[\w-]+\.md", adr) or not (root / adr).is_file():
            raise RuntimeError(f"Missing feature decision for {feature}: {adr}")
    fixes = {row["path"] for row in rows if row.get("classification") == "fix"}
    if fixes != set(data["upstream_queue"]) or set(data["upstream_queue"].values()) - {
        "first",
        "queued",
        "held-for-stock-reproducer",
    }:
        raise RuntimeError("Fix classification must match the upstream queue")
    for name, exception in data["reviewed_exceptions"].items():
        if name not in names or exception["sha256"] != lock["patch_sha256"][name]:
            raise RuntimeError(f"Stale reviewed exception for {name}")
        if exception.get("scope") == "integrator-session-output":
            grant = exception.get("grant", "")
            if not re.fullmatch(r"docs/decisions/\d{4}-[\w-]+\.md", grant):
                raise RuntimeError(f"Missing explicit integrator grant for {name}")
            decision = (root / grant).read_text(encoding="utf-8").splitlines()
            if (
                f"Upstream Cycles commit `{lock['commit']}`" not in decision
                or f"| `{name}` | `{exception['sha256']}` |" not in decision
            ):
                raise RuntimeError(f"Expired integrator grant for {name} on {lock['commit']}: full review required")
    for row in rows:
        number = int(Path(row["path"]).name.split("-", 1)[0])
        if row["number"] != number or row["disposition"] not in ("upstreamable", "fork-permanent", "integration-only"):
            raise RuntimeError("Invalid triage number or disposition: " + row["path"])
        content = (root / row["path"]).read_bytes()
        if not hashlib.sha256(content).hexdigest() == row["sha256"] == lock["patch_sha256"][row["path"]]:
            raise RuntimeError("Triage source bytes changed: " + row["path"])
        if row["path"] in data["reviewed_exceptions"] and (
            data["reviewed_exceptions"][row["path"]]["classification"] != row.get("classification")
        ):
            raise RuntimeError("Stale exception classification: " + row["path"])
        check_classification(data, row, content)
        if any(len(row[key].strip()) < 45 for key in ("rationale", "next_action", "retirement_condition")):
            raise RuntimeError("Triage must contain substantive per-patch reasoning and follow-up: " + row["path"])
        if row["related_fixture"] and not (root / row["related_fixture"]).is_file():
            raise RuntimeError("Triage fixture no longer exists: " + row["related_fixture"])
    retired = data.get("retired_patches", [])
    retired_names = [row["path"] for row in retired]
    if len(retired_names) != len(set(retired_names)) or names.intersection(retired_names):
        raise RuntimeError("Retired patches must be distinct and absent from the production lock")
    for row in retired:
        original = re.fullmatch(r"patches/(\d{4})-[^/]+\.patch", row["path"])
        if (
            row.get("classification") != "retired/parked"
            or not original
            or row.get("number") != int(original[1])
            or (root / row["path"]).exists()
            or not re.fullmatch(r"[0-9a-f]{64}", row.get("sha256", ""))
            or not re.fullmatch(r"experiments/[\w/-]+", row.get("experiments_branch", ""))
            or any(
                not re.fullmatch(r"[0-9a-f]{40}", row.get(key, ""))
                for key in ("integration_archive_commit", "engine_archive_commit")
            )
        ):
            raise RuntimeError("Invalid parked-patch recovery record: " + row["path"])
        decision = row.get("decision", "")
        if not re.fullmatch(r"docs/[\w./-]+\.md", decision) or not (root / decision).is_file():
            raise RuntimeError(
                "Parked patch must name a decision document under docs/: " + row["path"] + " -> " + decision
            )
    return data


def markdown(data):
    counts = Counter(row["disposition"] for row in data["patches"])
    lines = [
        "# Renderer patch triage",
        "",
        data["scope"],
        "",
        "Every row is bound to its exact patch SHA-256 in [triage.json](../patches/triage.json). "
        "The [patch inventory](../patches/inventory.json) records touched files, overlap predecessors and related fixture contracts. "
        f"The local fork contains all {len(data['patches'])} listed commits.",
        "",
        ", ".join(f"{name}: **{counts[name]}**" for name in ("upstreamable", "fork-permanent", "integration-only"))
        + ".",
        "",
        "**Upstreamable** identifies a generic correction or optimization to extract and review. "
        "**Fork-permanent** means a maintained feature/API until an agreed replacement exists; it is not a claim that upstream will never want it. "
        "**Integration-only** contains host build/provider/owned-data/policy seams. "
        "All dispositions require the inventory retirement gate before removal: upstream equivalent, independent successor replay, affected backend checks and feature-off controls.",
        "",
        "Local proposal drafts: [light-tree cache and reuse](dev/upstream-light-tree-proposal.md), "
        "[nested medium transport](dev/upstream-nested-medium-proposal.md), "
        "[small correctness fixes](dev/upstream-correctness-proposals.md). "
        "These are source-backed review packets with explicit evidence gaps, not submissions or upstream approvals. "
        "No external service was contacted or message transmitted.",
        "",
        "Validate with `python scripts/patch-triage.py --check`; regenerate this table with `--write`.",
        "",
        "The policy column implements [ADR 0005](decisions/0005-cycles-fork-policy.md). "
        "Features reference its whitelist and fixes reference the upstream queue. "
        "Measurements, retirements and historical exceptions apply only to the recorded bytes. "
        "Runtime glue outside build files requires an explicit hash-bound review. "
        "Kernel changes cannot enter as glue. The eight approved session/output patches under src/integrator/ "
        "have a separate hash-bound scope.",
        "",
        "Patch IDs remain stable when earlier patches retire; gaps are intentional. "
        "The nine approved features, including interactive light-tree updates, remain in production.",
        "",
        "K33 parks the exact ramps-and-curves pair outside the production patch stack. "
        "The integration archive preserves the complete adapter, original lock and patch bytes; "
        "the engine archive preserves the matching fork. The branch and commits refer to private archives and are absent from public clones. "
        "The pair can return only together under one kernel mechanism, ADR and kernel-surface budget.",
        "",
        "| Parked patch | SHA-256 | Private recovery branch | Integration archive commit | Engine archive commit |",
        "|---|---|---|---|---|",
        *[
            f"| `{row['path']}` | `{row['sha256']}` | `{row['experiments_branch']}` | `{row['integration_archive_commit']}` | `{row['engine_archive_commit']}` |"
            for row in data.get("retired_patches", [])
        ],
        "",
        "| Patch | Policy | Historical disposition | Why | Next action |",
        "|---|---|---|---|---|",
    ]
    for row in data["patches"]:
        values = [
            f"[{row['number']:04d}](../{row['path']})",
            row["classification"],
            row["disposition"],
            row["rationale"],
            row["next_action"]
            + (
                " Any byte change or rebase expires this grant. Full review required. "
                f"[Granted bytes and base](../{data['reviewed_exceptions'][row['path']]['grant']})."
                if data["reviewed_exceptions"].get(row["path"], {}).get("scope") == "integrator-session-output"
                else ""
            ),
        ]
        lines.append("| " + " | ".join(value.replace("|", "\\|") for value in values) + " |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    data = validate(args.source_root)
    expected = markdown(data)
    target = args.source_root / "docs/patch-triage.md"
    if args.write:
        target.write_text(expected, encoding="utf-8")
    elif not target.exists() or target.read_text(encoding="utf-8") != expected:
        raise RuntimeError("Stale patch triage table: " + str(target))
    print(
        json.dumps(
            dict(
                result="passed",
                patches=len(data["patches"]),
                dispositions=dict(Counter(row["disposition"] for row in data["patches"])),
            )
        )
    )
