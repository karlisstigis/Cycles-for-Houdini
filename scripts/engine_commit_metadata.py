# SPDX-License-Identifier: Apache-2.0
"""Restore historical engine notes and require metadata on future exports.

The ordered lock, inventory and triage remain the portable source of truth.
Notes supplement two shipped commits; they never amend renderer history.
"""

import hashlib
import json
from pathlib import Path
import subprocess

NOTES_REF = "refs/notes/cycles-houdini"
HISTORICAL_EXCEPTIONS = {
    "1b2ecd0d0747f08bdcc6f886710fe74a35b0a97b",
    "151c8b056dce274686e268589fa10aa25a0beaa9",
}


def git(repo, *arguments, data=None):
    return subprocess.check_output(["git", "-C", str(repo), *arguments], input=data)


def commit_history(repo, base, tip):
    """Read messages and parents in one process, preserving the locked order."""
    fields = git(repo, "log", "--reverse", "--format=%H%x00%P%x00%B%x00", base + ".." + tip).decode().split("\0")
    if len(fields) % 3 != 1 or fields[-1].strip():
        raise RuntimeError("Invalid engine commit metadata stream")
    return [(fields[i].strip(), fields[i + 1].split(), fields[i + 2]) for i in range(0, len(fields) - 1, 3)]


def future_metadata(root, repo, commit, message=None):
    """Require explanatory trailers and concrete integration test references."""
    message = git(repo, "show", "-s", "--format=%B", commit) if message is None else message.encode()
    trailers = subprocess.check_output(["git", "interpret-trailers", "--parse"], input=message).decode()
    fields = {}
    for line in trailers.splitlines():
        name, _, value = line.partition(":")
        if name in ("Rationale", "Contract", "Tests"):
            if name in fields:
                raise RuntimeError(f"Engine commit {commit}: duplicate {name} trailer")
            fields[name] = value.strip()
    for name in ("Rationale", "Contract"):
        if not fields.get(name):
            raise RuntimeError(f"Engine commit {commit}: add a nonempty {name}: trailer")
    tests = [name.strip() for name in fields.get("Tests", "").split(",") if name.strip()]
    if not tests:
        raise RuntimeError(f"Engine commit {commit}: add Tests: with comma-separated integration test/script paths")
    for name in tests:
        path = Path(name)
        resolved = (root / path).resolve()
        if path.is_absolute() or not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
            raise RuntimeError(
                f"Engine commit {commit}: Tests reference must name an existing integration file: {name}"
            )
        if not name.startswith(("tests/", "scripts/")):
            raise RuntimeError(f"Engine commit {commit}: Tests references must be under tests/ or scripts/: {name}")
    return fields


def locked_metadata(root, repo):
    """Validate provenance and bytes once; derive only the two missing bodies."""
    lock = json.loads((root / "source.lock.json").read_text(encoding="utf-8-sig"))["cycles"]
    inventory = json.loads((root / "patches/inventory.json").read_text(encoding="utf-8-sig"))["patches"]
    history = commit_history(repo, lock["commit"], lock["authoring_tip"])
    if len(history) != len(lock["patches"]) or [row["path"] for row in inventory] != lock["patches"]:
        raise RuntimeError("Commit metadata requires the complete ordered locked commit/patch inventory")
    notes = {}
    previous = lock["commit"]
    for (commit, parents, message), row in zip(history, inventory):
        if parents != [previous]:
            raise RuntimeError("Commit metadata requires one linear commit per locked patch")
        previous = commit
        name = row["path"]
        content = (root / name).read_bytes()
        if hashlib.sha256(content).hexdigest() != row["sha256"]:
            raise RuntimeError("Commit metadata patch bytes differ from inventory: " + name)
        imported = all(
            value in message.splitlines()
            for value in ("Locked-Patch: " + name, "Locked-Patch-SHA256: " + row["sha256"])
        )
        if imported and "Contract: " in message and "Related-Fixture: " in message:
            continue  # The original 84 bodies already expose the source/test contract.
        if commit not in HISTORICAL_EXCEPTIONS:
            future_metadata(root, repo, commit, message)
            if not content.startswith(("From " + commit + " ").encode()):
                raise RuntimeError("Commit metadata provenance differs from exported patch: " + name)
            continue
        if not content.startswith(("From " + commit + " ").encode()):
            raise RuntimeError("Historical note commit differs from exported patch: " + name)
        triage = json.loads((root / "patches/triage.json").read_text(encoding="utf-8-sig"))
        matches = [item for item in triage["patches"] if item["path"] == name and item["sha256"] == row["sha256"]]
        if len(matches) != 1:
            raise RuntimeError("Historical note requires exact hash-bound triage: " + name)
        tests = row["regression"]["related_fixture"] or "scripts/verify-upstream-patches.py"
        if not (root / tests).is_file():
            raise RuntimeError("Historical note test reference is missing: " + tests)
        notes[commit] = (
            "Cycles Houdini historical commit metadata\n\n"
            f"Rationale: {matches[0]['rationale']}\n"
            f"Contract: {row['purpose']}\n"
            f"Tests: {tests}\n"
            f"Locked-Patch: {name}\n"
            f"Locked-Patch-SHA256: {row['sha256']}\n"
            "Qualification: Metadata and fixture pointers; no runtime execution asserted.\n"
            "Source: source.lock.json, patches/inventory.json, patches/triage.json\n"
        )
    return dict(notes=notes, commits=[commit for commit, _, _ in history])


def historical_notes(root, repo):
    return locked_metadata(root, repo)["notes"]


def restore_notes(root, repo):
    notes = historical_notes(root, repo)  # Validate everything before writing notes.
    if not notes:
        return 0
    for commit, content in notes.items():
        old = subprocess.run(
            ["git", "-C", str(repo), "notes", "--ref=" + NOTES_REF, "show", commit], capture_output=True
        )
        if old.returncode not in (0, 1):
            old.check_returncode()
        if old.returncode or old.stdout.decode().rstrip() != content.rstrip():
            git(
                repo,
                "-c",
                "user.name=Cycles Houdini metadata",
                "-c",
                "user.email=metadata@localhost",
                "notes",
                "--ref=" + NOTES_REF,
                "add",
                "-f",
                "-F",
                "-",
                commit,
                data=content.encode(),
            )
    display = subprocess.run(["git", "-C", str(repo), "config", "--get-all", "notes.displayRef"], capture_output=True)
    if display.returncode not in (0, 1):
        display.check_returncode()
    if NOTES_REF not in display.stdout.decode().splitlines():
        git(repo, "config", "--local", "--add", "notes.displayRef", NOTES_REF)
    return len(notes)
