# SPDX-License-Identifier: Apache-2.0
"""Queue immutable revisions and run the Houdini tiers in a private workstation checkout."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import os
import re
import subprocess
import sys
import tempfile
import traceback
import runpy

ROOT = Path(__file__).resolve().parents[1]

# The OS, rather than Python shutdown callbacks, owns cleanup. Explicitly closing
# this self-assigned job would terminate the drain before its normal exit code
# is delivered. The non-inheritable handle is closed by Windows on process exit,
# including Task Scheduler's TerminateProcess path and interpreter crashes.
_drain_job_handle = None


def hidden_child_options():
    """Keep console tools silent on Windows without changing their I/O or jobs."""
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def console_python():
    """Use the console interpreter so CREATE_NO_WINDOW reaches tool descendants.

    Windows ignores CREATE_NO_WINDOW for GUI-subsystem pythonw.exe. Launching
    another pythonw would let its ordinary Git children reopen a console.
    """
    executable = Path(sys.executable)
    if os.name == "nt" and executable.name.lower() == "pythonw.exe":
        executable = executable.with_name("python.exe")
        if not executable.is_file():
            raise RuntimeError("The scheduled CI runner requires python.exe alongside pythonw.exe")
    return str(executable)


def _create_and_assign_drain_job():
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimits),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    api.CreateJobObjectW.restype = wintypes.HANDLE
    api.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    api.SetInformationJobObject.restype = wintypes.BOOL
    api.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    api.AssignProcessToJobObject.restype = wintypes.BOOL
    api.GetCurrentProcess.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = (wintypes.HANDLE,)
    api.CloseHandle.restype = wintypes.BOOL
    # An unnamed job cannot be opened by unrelated processes. NULL security
    # attributes make its sole handle non-inheritable even when a child enables
    # handle inheritance. No breakaway flags are set: descendants stay contained.
    job = api.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not api.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        # Windows 8+ nests this empty job inside Task Scheduler's existing job.
        # Reject incompatible ancestor restrictions instead of running unowned.
        if not api.AssignProcessToJobObject(job, api.GetCurrentProcess()):
            raise ctypes.WinError(ctypes.get_last_error())
    except BaseException:
        api.CloseHandle(job)  # Assignment never succeeded, so closing is safe.
        raise
    return job


def contain_drain_process():
    global _drain_job_handle
    if os.name != "nt":
        raise RuntimeError("The workstation CI drain requires Windows process-job containment")
    if _drain_job_handle is None:
        try:
            _drain_job_handle = _create_and_assign_drain_job()
        except OSError as error:
            raise RuntimeError("Cannot contain the CI drain and its descendants; no work was started") from error


def git(*args, cwd=ROOT):
    return subprocess.check_output(["git", "-C", str(cwd), *args], text=True, **hidden_child_options()).strip()


def publish(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        json.dump(data, stream, indent=2)
        name = stream.name
    os.replace(name, path)


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def enqueue(state, tier, revision, repository=ROOT, houdini=()):
    commit = git("rev-parse", "--verify", revision + "^{commit}", cwd=repository)
    if tier == "compatibility" and not houdini:
        raise ValueError("Select at least one exact Houdini build with --houdini")
    if tier != "compatibility" and houdini:
        raise ValueError("--houdini applies only to the compatibility job")
    for target in houdini if tier == "compatibility" else (None,):
        if target is not None and not re.fullmatch(r"\d+\.\d+\.\d+", target):
            raise ValueError("An exact Houdini build is required")
        day = datetime.now().strftime("-%Y%m%d") if tier == "nightly" else ""
        key = tier + "-" + (target + "-" if target else "") + commit + day
        if not (state / "results" / key / "receipt.json").exists():
            publish(
                state / "pending" / (key + ".json"),
                {"commit": commit, "tier": tier, **({"houdini": target} if target else {})},
            )
        print(f"{tier} {target or ''} {commit}")


@contextmanager
def exclusive(path):
    # Windows releases the byte-range lock even if the runner is terminated.
    import msvcrt

    with path.open("a+b") as handle:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def fetch_archived_integration_commits(workspace):
    triage = read(workspace / "patches/triage.json")
    archives = sorted(
        {
            row["integration_archive_commit"]
            for row in triage.get("retired_patches", [])
            if row.get("integration_archive_commit")
        }
    )
    for revision in archives:
        git("fetch", "--quiet", "origin", revision, cwd=workspace)


def checkout(state, config, commit, log):
    workspace = state / "checkout"
    if not workspace.exists():
        subprocess.run(
            [
                "git",
                "-c",
                "core.autocrlf=false",
                "clone",
                "--local",
                "--no-hardlinks",
                "--no-checkout",
                config["repository"],
                str(workspace),
            ],
            check=True,
            stdout=log,
            stderr=log,
            **hidden_child_options(),
        )
        git("config", "core.autocrlf", "false", cwd=workspace)
    if Path(git("remote", "get-url", "origin", cwd=workspace)).resolve() != Path(config["repository"]).resolve():
        raise RuntimeError("The CI checkout has a different origin")
    git("fetch", "--quiet", "origin", commit, cwd=workspace)
    git("checkout", "--quiet", "--detach", commit, cwd=workspace)
    if git("status", "--porcelain", "--untracked-files=normal", cwd=workspace):
        raise RuntimeError("The CI checkout contains local changes")
    fetch_archived_integration_commits(workspace)
    dependencies = workspace / "third_party"
    source = Path(config["repository"]) / "third_party"
    if not dependencies.exists():
        subprocess.run(
            [
                config["pwsh"],
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "New-Item -ItemType Junction -Path $env:CH_CI_LINK -Target $env:CH_CI_DEPS | Out-Null",
            ],
            env=dict(os.environ, CH_CI_LINK=str(dependencies), CH_CI_DEPS=str(source)),
            check=True,
            **hidden_child_options(),
        )
    if not dependencies.samefile(source):
        raise RuntimeError("Unexpected CI dependency directory")
    # build.ps1 verifies the clean, locked engine; the other dependency revisions
    # are matched by environment-report.py while staging this exact build.
    return workspace


def checkout_build_directory(workspace, target):
    # The installed scheduler copies this runner alone and starts Python -I.
    # Resolve policy from the exact checked-out revision, not import search paths
    # or the mutable production checkout. Old revisions keep their legacy cache.
    helper = workspace / "scripts/build_layout.py"
    if helper.is_file():
        return runpy.run_path(str(helper))["default_build_directory"](workspace, target)
    return workspace / "build" / f"windows-h{target}-release"


def verify_fast_summary(summary_path, workspace):
    """Check qualification evidence against the tested revision's own planner."""
    summary_path = summary_path.resolve()
    run = summary_path.parent
    summary = read(summary_path)
    if summary.get("scope") != "native-fast" or summary.get("result") != "passed" or not summary.get("finished_utc"):
        raise RuntimeError("Fast CI requires a completed passing fast-tier run")
    plan = json.loads(
        subprocess.check_output(
            [
                console_python(),
                str(workspace / "scripts/native_test_plan.py"),
                "--tier",
                "fast",
                "--destination",
                str(run),
            ],
            cwd=workspace,
            **hidden_child_options(),
        )
    )
    if not plan or summary.get("plan") != plan:
        raise RuntimeError("Archived plan differs from the official fast tier")
    results = summary["tests"]
    if [(entry["test"], entry["device"]) for entry in results] != [(case["test"], case["device"]) for case in plan]:
        raise RuntimeError("Fast CI results differ from the ordered official fast tier")

    manifest = (run / "build-manifest.json").read_bytes()
    if hashlib.sha256(manifest).hexdigest() != summary["build_manifest_sha256"].lower():
        raise RuntimeError("Archived fast CI manifest hash differs from the run")
    for case, entry in zip(plan, results, strict=True):
        if entry.get("result") != "passed" or type(entry.get("exit_code")) is not int or entry["exit_code"] != 0:
            raise RuntimeError(f"Fast CI case did not record a successful exit: {case['test']}")
        if case["report"]:
            stem = Path(case["test"]).stem + ("-" + case["device"] if case["device"] else "")
            name = stem + ".json"
            report = (run / name).resolve()
            if not report.is_relative_to(run) or entry.get("evidence") != name:
                raise RuntimeError(f"Fast CI report does not belong to the recorded case: {case['test']}")
            payload = report.read_bytes()
            if hashlib.sha256(payload).hexdigest() != entry["evidence_sha256"].lower():
                raise RuntimeError(f"Fast CI report changed: {case['test']}")
            reported = json.loads(payload)
            code = reported.get("process_exit_code", 0)
            if reported.get("result") != "passed" or type(code) is not int or code != 0:
                raise RuntimeError(f"Fast CI report contradicts the successful result: {case['test']}")


def run_case(state, config, key, job):
    dest = state / "results" / key
    dest.mkdir(exist_ok=True, parents=True)
    receipt = dict(job, started=datetime.now(timezone.utc).isoformat(), result="failed")
    with (dest / "ci.log").open("a", encoding="utf-8") as log:
        try:
            workspace = checkout(state, config, job["commit"], log)
            if git("rev-parse", "HEAD", cwd=workspace) != job["commit"]:
                raise RuntimeError("CI selected a different integration commit")
            # The source-derived sibling keeps this private checkout's cache
            # separate from production; staging checks CMake source ownership.
            source_lock = read(workspace / "source.lock.json")
            if "patch_sha256" in source_lock["cycles"]:
                for checker in ("patch-triage.py", "patch-inventory.py"):
                    subprocess.run(
                        [console_python(), str(workspace / "scripts" / checker), "--check"],
                        cwd=workspace,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        check=True,
                        **hidden_child_options(),
                    )
            target = job.get("houdini") or source_lock["certification_candidate"]["houdini"]
            build = checkout_build_directory(workspace, target)
            houdini_root = Path(os.environ["ProgramFiles"]) / "Side Effects Software" / ("Houdini " + target)
            if not (houdini_root / "bin/hython.exe").is_file():
                raise RuntimeError(f"Houdini {target} SDK/runtime is not installed at {houdini_root}")
            # Nested test and build processes use the same PowerShell as the scheduler.
            environment = os.environ.copy()
            environment["PATH"] = str(Path(config["pwsh"]).parent) + os.pathsep + environment.get("PATH", "")

            def script(name, *args):
                subprocess.run(
                    [
                        config["pwsh"],
                        "-NoProfile",
                        "-NonInteractive",
                        "-File",
                        str(workspace / "scripts" / name),
                        *map(str, args),
                    ],
                    cwd=workspace,
                    env=environment,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    check=True,
                    **hidden_child_options(),
                )

            subprocess.run(
                [
                    console_python(),
                    str(workspace / "scripts/format-source.py"),
                    "--ruff",
                    config["ruff"],
                    "--clang-format",
                    config["clang"],
                ],
                cwd=workspace,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=True,
                **hidden_child_options(),
            )
            if job["tier"] != "compatibility":
                required = hashlib.sha256((workspace / "tests/requirements.txt").read_bytes()).hexdigest()
                dependencies = state / "test-dependencies" / required
                if not (dependencies / "pytest/__init__.py").is_file():
                    script("bootstrap-test-deps.ps1", "-Wheelhouse", config["wheelhouse"], "-Target", dependencies)
            script(
                "build.ps1",
                "-HoudiniRoot",
                houdini_root,
                "-BuildDirectory",
                build,
                "-UserPrefDirectory",
                dest / "prefs/houdini__HVER__",
                "-Jobs",
                config["jobs"],
            )
            manifest = (build / "build-manifest.json").read_bytes()
            (dest / "build-manifest.json").write_bytes(manifest)
            receipt["manifest_sha256"] = hashlib.sha256(manifest).hexdigest()
            receipt["houdini"] = target
            if job["tier"] == "compatibility":
                for device in ("CPU", "OPTIX"):
                    script(
                        "run-prototype-test.ps1",
                        "-Test",
                        "houdini_build_compatibility_smoke.py",
                        "-Device",
                        device,
                        "-Execution",
                        "worker",
                        "-HoudiniRoot",
                        houdini_root,
                        "-BuildDirectory",
                        build,
                        "-Artifacts",
                        dest / "tests" / device.lower(),
                        "-Report",
                        f"compatibility/houdini-build-smoke-{device.lower()}.json",
                        "-TimeoutSeconds",
                        300,
                    )
                script(
                    "run-prototype-test.ps1",
                    "-Test",
                    "core_image_iris_smoke.py",
                    "-Device",
                    "CPU",
                    "-Execution",
                    "sync",
                    "-HoudiniRoot",
                    houdini_root,
                    "-BuildDirectory",
                    build,
                    "-Artifacts",
                    dest / "tests" / "iris",
                    "-Report",
                    "image-iris/core-report.json",
                    "-TimeoutSeconds",
                    600,
                )
                receipt["checks"] = [
                    "native module",
                    "worker handshake",
                    "CPU image",
                    "OptiX image",
                    "clean exit",
                    "Image Iris render",
                ]
                receipt["qualification"] = "exercised on this exact SDK and integration revision"
                receipt["signed"] = False
                receipt["activated"] = False
                receipt["packaged"] = False
                receipt["result"] = "passed"
            else:
                script(
                    "run-tests.ps1",
                    "-Tier",
                    job["tier"],
                    "-BuildDirectory",
                    build,
                    "-Artifacts",
                    dest / "tests",
                    "-TestDependencies",
                    dependencies,
                    "-TimeoutSeconds",
                    900,
                )
                summaries = list((dest / "tests").glob("*/summary.json"))
                if len(summaries) != 1:
                    raise RuntimeError("Expected exactly one tier summary in this run")
                summary = read(summaries[0])
                if (
                    summary["result"] != "passed"
                    or not summary["tests"]
                    or summary["build_manifest_sha256"].lower() != receipt["manifest_sha256"]
                ):
                    raise RuntimeError("Tier evidence does not identify a passing run of the built manifest")
                if job["tier"] == "fast":
                    verify_fast_summary(summaries[0], workspace)
                subprocess.run(
                    [console_python(), str(workspace / "scripts/verify-verification.py"), str(summaries[0].parent)],
                    cwd=workspace,
                    check=True,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    **hidden_child_options(),
                )
                receipt["summary"] = str(summaries[0])
                receipt["result"] = "passed"
        except Exception:
            traceback.print_exc(file=log)
        receipt["finished"] = datetime.now(timezone.utc).isoformat()
        publish(dest / "receipt.json", receipt)
    return receipt["result"] == "passed"


def recover_green_fast(config, receipt_path):
    destination = config.get("recovery_directory")
    if not destination:
        return
    receipt = read(receipt_path)
    if receipt["result"] != "passed" or receipt["tier"] != "fast":
        return
    repo = Path(config["repository"])
    # Workstation recovery is a local tool of the maintainer's workstation and is not published; skip it
    # where it does not exist.
    tool = repo / "scripts/workstation-recovery.py"
    if not tool.is_file():
        return
    with (receipt_path.parent / "recovery.log").open("a", encoding="utf-8") as log:
        subprocess.run(
            [
                console_python(),
                "-I",
                str(tool),
                "capture",
                "--source",
                str(repo),
                "--destination",
                destination,
                "--ci",
                config["state"],
                "--revision",
                receipt["commit"],
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
            **hidden_child_options(),
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=ROOT)
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("enqueue")
    add.add_argument("tier", choices=("fast", "nightly", "compatibility"))
    add.add_argument("revision")
    add.add_argument("--houdini", action="append", default=[])
    sub.add_parser("drain")
    verify = sub.add_parser("verify-fast", help="Verify a recorded fast-tier summary and reports")
    verify.add_argument("summary", type=Path)
    args = parser.parse_args()
    if args.command == "verify-fast":
        verify_fast_summary(args.summary, args.repository.resolve())
        return 0
    if args.command == "drain":
        # Contain before even the first git subprocess or checkout mutation.
        contain_drain_process()
    # Shared by the project's worktrees. Other repositories have independent queues.
    common = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=args.repository))
    config_file = common / "houdini-ci.json"
    config = read(config_file)
    state = Path(config["state"])
    if args.command == "enqueue":
        enqueue(state, args.tier, args.revision, args.repository, args.houdini)
        return 0
    ok = True
    with exclusive(state / "worker.lock") as acquired:
        if acquired:
            for path in sorted((state / "pending").glob("*.json"), key=lambda p: p.stat().st_mtime):
                receipt_path = state / "results" / path.stem / "receipt.json"
                if not receipt_path.exists():
                    ok = run_case(state, config, path.stem, read(path)) and ok
                try:
                    recover_green_fast(config, receipt_path)
                except (OSError, subprocess.CalledProcessError):
                    # Leave this job queued. On the next drain only recovery is retried.
                    traceback.print_exc()
                    ok = False
                    continue
                path.unlink(missing_ok=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
