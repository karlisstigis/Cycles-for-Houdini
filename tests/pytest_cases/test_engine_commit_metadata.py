# SPDX-License-Identifier: Apache-2.0
"""Commit notes survive offline reconstruction without changing shipped history."""

import hashlib
import json
import os
import runpy
import subprocess

import pytest

from support.paths import ROOT

pytestmark = [pytest.mark.pure, pytest.mark.cpu]
METADATA = runpy.run_path(str(ROOT / "scripts/engine_commit_metadata.py"))
HISTORY = runpy.run_path(str(ROOT / "scripts/source-history.py"))


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args])


@pytest.fixture
def engine(tmp_path, monkeypatch):
    root = tmp_path / "integration"
    repo = tmp_path / "engine"
    root.mkdir()
    git(tmp_path, "init", "-q", str(repo))
    git(repo, "config", "user.name", "Metadata fixture")
    git(repo, "config", "user.email", "metadata@example.invalid")
    git(repo, "config", "core.autocrlf", "false")
    source = repo / "source.cpp"
    source.write_bytes(b"base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "upstream")
    base = git(repo, "rev-parse", "HEAD").decode().strip()
    source.write_bytes(b"changed\n")
    git(repo, "commit", "-qam", "Already shipped title-only change")
    tip = git(repo, "rev-parse", "HEAD").decode().strip()
    content = git(repo, "format-patch", "-1", "--stdout", "--no-signature", "--full-index")
    name = "patches/0001-change.patch"
    (root / "patches").mkdir()
    (root / name).write_bytes(content)
    (root / "tests").mkdir()
    (root / "tests/regression.py").write_text("assert True\n")
    digest = hashlib.sha256(content).hexdigest()
    data = {
        "source.lock.json": dict(cycles=dict(commit=base, authoring_tip=tip, patches=[name])),
        "patches/inventory.json": dict(
            patches=[
                dict(
                    path=name,
                    sha256=digest,
                    purpose="Preserve the independent sampled geometry contract.",
                    regression=dict(related_fixture="tests/regression.py"),
                )
            ]
        ),
        "patches/triage.json": dict(
            patches=[
                dict(
                    path=name,
                    sha256=digest,
                    rationale="The renderer previously discarded an independently owned sample.",
                )
            ]
        ),
    }
    for path, value in data.items():
        (root / path).write_text(json.dumps(value))
    monkeypatch.setitem(METADATA["historical_notes"].__globals__, "HISTORICAL_EXCEPTIONS", {tip})
    return root, repo, tip, content


def test_notes_reconstructed_from_portable_sources_preserve_commits_and_other_notes(engine, tmp_path, monkeypatch):
    root, repo, tip, content = engine
    git(repo, "notes", "--ref=review", "add", "-m", "Existing independent review", tip)
    git(repo, "config", "--add", "notes.displayRef", "refs/notes/review")
    before = git(repo, "rev-list", "HEAD")
    assert METADATA["restore_notes"](root, repo) == 1
    log = git(repo, "log", "-1").decode()
    for text in ("Rationale:", "Contract:", "Tests: tests/regression.py", "Existing independent review"):
        assert text in log
    notes_tip = git(repo, "rev-parse", METADATA["NOTES_REF"])
    METADATA["restore_notes"](root, repo)
    assert git(repo, "rev-parse", METADATA["NOTES_REF"]) == notes_tip
    clone = tmp_path / "offline-clone"
    git(tmp_path, "clone", "-q", str(repo), str(clone))  # Ordinary clone omits the notes ref.
    assert "Rationale:" not in git(clone, "log", "-1").decode()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    METADATA["restore_notes"](root, clone)
    assert "Rationale:" in git(clone, "log", "-1").decode()
    assert git(repo, "rev-list", "HEAD") == before == git(clone, "rev-list", "HEAD")
    assert (root / "patches/0001-change.patch").read_bytes() == content
    assert not git(repo, "status", "--porcelain").strip()


def test_notes_reject_drift_before_writing(engine):
    root, repo, _, _ = engine
    (root / "patches/0001-change.patch").write_bytes(b"changed accepted patch")
    with pytest.raises(RuntimeError, match="patch bytes"):
        METADATA["restore_notes"](root, repo)
    assert subprocess.run(["git", "-C", str(repo), "show-ref", METADATA["NOTES_REF"]], capture_output=True).returncode


def test_locked_metadata_reads_history_in_one_process(engine, monkeypatch):
    root, repo, _, _ = engine
    original = METADATA["git"]
    calls = []

    def counted(repo, *args, **kwargs):
        calls.append(args)
        return original(repo, *args, **kwargs)

    monkeypatch.setitem(METADATA["locked_metadata"].__globals__, "git", counted)
    assert len(METADATA["locked_metadata"](root, repo)["commits"]) == 1
    assert len(calls) == 1 and calls[0][0] == "log"


def test_new_commit_accepts_nonempty_concise_metadata(engine):
    root, repo, _, _ = engine
    git(
        repo,
        "commit",
        "--allow-empty",
        "-qm",
        "Fix normals\n\nRationale: Avoid stale normals.\nContract: Retain motion.\nTests: tests/regression.py",
    )
    assert METADATA["future_metadata"](root, repo, "HEAD")["Contract"] == "Retain motion."


@pytest.mark.parametrize("field", ["Rationale", "Contract", "Tests"])
def test_new_commit_requires_each_metadata_field(engine, field):
    root, repo, _, _ = engine
    fields = dict(
        Rationale="Preserve the independent source sample instead of silently dropping it.",
        Contract="Each requested shutter sample retains the authored geometry values.",
        Tests="tests/regression.py",
    )
    fields.pop(field)
    message = "New change\n\n" + "\n".join(name + ": " + value for name, value in fields.items())
    git(repo, "commit", "--allow-empty", "-qm", message)
    with pytest.raises(RuntimeError, match=field):
        METADATA["future_metadata"](root, repo, "HEAD")


def test_new_commit_rejects_duplicate_metadata(engine):
    root, repo, _, _ = engine
    git(
        repo,
        "commit",
        "--allow-empty",
        "-qm",
        "New change\n\n"
        "Rationale: Preserve independent motion data during a scene update.\n"
        "Contract: Every authored sample remains represented in the renderer.\n"
        "Tests: tests/regression.py\nTests: tests/regression.py",
    )
    with pytest.raises(RuntimeError, match="duplicate Tests"):
        METADATA["future_metadata"](root, repo, "HEAD")


@pytest.mark.parametrize("tests", ["tests/missing.py", "../engine/source.cpp", "/outside.py", "source.lock.json"])
def test_new_commit_rejects_missing_or_unbounded_test_references(engine, tests):
    root, repo, _, _ = engine
    git(
        repo,
        "commit",
        "--allow-empty",
        "-qm",
        "New change\n\n"
        "Rationale: Preserve independent motion data during a scene update.\n"
        "Contract: Every authored sample remains represented in the renderer.\nTests: " + tests,
    )
    with pytest.raises(RuntimeError, match="Tests reference"):
        METADATA["future_metadata"](root, repo, "HEAD")


def test_snapshot_default_prefers_local_evidence_then_historical_path(tmp_path):
    root = tmp_path / "source"
    relative = "releases/blend-batch-466-20261108/source-snapshot.zip"
    assert HISTORY["default_snapshot"](root) == root / "artifacts" / relative
    local = root / "_work" / relative
    local.parent.mkdir(parents=True)
    local.write_bytes(b"preserved source snapshot")
    assert HISTORY["default_snapshot"](root) == local


def test_explicit_snapshot_overrides_default(tmp_path, monkeypatch, capsys):
    selected = tmp_path / "explicit.zip"
    globals_ = HISTORY["main"].__globals__
    monkeypatch.setitem(globals_, "init_integration", lambda root, snapshot: dict(snapshot=str(snapshot)))
    monkeypatch.setitem(globals_, "default_snapshot", lambda root: tmp_path / "default.zip")
    monkeypatch.setattr("sys.argv", ["source-history.py", "init-integration", "--snapshot", str(selected)])
    HISTORY["main"]()
    assert json.loads(capsys.readouterr().out)["snapshot"] == str(selected)
