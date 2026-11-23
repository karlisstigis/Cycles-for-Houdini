# SPDX-License-Identifier: Apache-2.0
"""Exercise commit queue identity and failure reporting without a renderer installation."""

import ast
import ctypes
from ctypes import wintypes
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("workstation_ci", ROOT / "scripts/workstation-ci.py")
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)
# The workstation recovery tool backs up the maintainer's local folders. It is not published,
# so its tests skip where it does not exist.
RECOVERY = ROOT / "scripts/workstation-recovery.py"
needs_recovery = unittest.skipUnless(
    RECOVERY.is_file(), "scripts/workstation-recovery.py is a private workstation tool"
)


class WorkstationCIContracts(unittest.TestCase):
    def test_checkout_fetches_retired_nonancestor_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            origin, checkout = Path(temp) / "origin", Path(temp) / "checkout"

            def git(where, *args):
                return (
                    subprocess.check_output(["git", "-C", str(where), *args], stderr=subprocess.STDOUT).decode().strip()
                )

            origin.mkdir()
            git(origin, "init", "-q")
            git(origin, "config", "user.name", "CI fixture")
            git(origin, "config", "user.email", "ci@example.invalid")
            (origin / "old.txt").write_text("retired source")
            git(origin, "add", ".")
            git(origin, "commit", "-qm", "archive")
            archive = git(origin, "rev-parse", "HEAD")
            git(origin, "checkout", "--orphan", "current")
            git(origin, "rm", "-f", "old.txt")
            (origin / "patches").mkdir()
            (origin / "patches/triage.json").write_text(
                json.dumps(
                    {
                        "retired_patches": [
                            {"integration_archive_commit": archive},
                            {"integration_archive_commit": archive},
                        ]
                    }
                )
            )
            git(origin, "add", ".")
            git(origin, "commit", "-qm", "current")
            checkout.mkdir()
            git(checkout, "init", "-q")
            git(checkout, "remote", "add", "origin", str(origin))
            git(checkout, "fetch", "origin", "current")
            git(checkout, "checkout", "--detach", "FETCH_HEAD")
            with self.assertRaises(subprocess.CalledProcessError):
                git(checkout, "show", archive + ":old.txt")
            ci.fetch_archived_integration_commits(checkout)
            self.assertEqual(git(checkout, "show", archive + ":old.txt"), "retired source")
            self.assertEqual(git(checkout, "status", "--porcelain"), "")

    def test_compatibility_jobs_are_opt_in_and_keyed_by_exact_sdk(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            with patch.object(ci, "git", return_value="a" * 40):
                with self.assertRaisesRegex(ValueError, "Select at least one"):
                    ci.enqueue(state, "compatibility", "HEAD")
                with self.assertRaisesRegex(ValueError, "only to the compatibility"):
                    ci.enqueue(state, "fast", "HEAD", houdini=("22.0.429",))
                ci.enqueue(state, "compatibility", "HEAD", houdini=("22.0.429", "22.0.466"))
            pending = [ci.read(path) for path in (state / "pending").glob("*.json")]
            self.assertEqual({job["houdini"] for job in pending}, {"22.0.429", "22.0.466"})
            self.assertTrue(all(job["tier"] == "compatibility" for job in pending))

    def test_compatibility_run_uses_selected_sdk_and_focused_checks(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            workspace = state / "checkout"
            workspace.mkdir()
            (workspace / "source.lock.json").write_text(
                json.dumps(dict(cycles={}, certification_candidate=dict(houdini="22.0.466")))
            )
            program_files = state / "Program Files"
            sdk = program_files / "Side Effects Software/Houdini 22.0.429"
            (sdk / "bin").mkdir(parents=True)
            (sdk / "bin/hython.exe").touch()
            build = state / "build429"
            build.mkdir()
            commands = []

            def run(command, **_):
                commands.append([str(part) for part in command])
                if any(part.endswith("build.ps1") for part in commands[-1]):
                    (build / "build-manifest.json").write_text('{"host_sdk":{"houdini":"22.0.429"}}')

            job = dict(tier="compatibility", commit="a" * 40, houdini="22.0.429")
            with (
                patch.object(ci, "checkout", return_value=workspace),
                patch.object(ci, "checkout_build_directory", return_value=build),
                patch.object(ci, "git", return_value=job["commit"]),
                patch.object(ci.subprocess, "run", side_effect=run),
                patch.dict(os.environ, ProgramFiles=str(program_files)),
            ):
                self.assertTrue(
                    ci.run_case(state, dict(pwsh="pwsh", ruff="ruff", clang="clang", jobs=2), "matrix429", job)
                )
            scripts = [Path(command[4]).name for command in commands if len(command) > 4 and command[3] == "-File"]
            self.assertEqual(
                scripts, ["build.ps1", "run-prototype-test.ps1", "run-prototype-test.ps1", "run-prototype-test.ps1"]
            )
            self.assertIn(str(sdk), commands[1])
            receipt = ci.read(state / "results/matrix429/receipt.json")
            self.assertEqual((receipt["result"], receipt["houdini"]), ("passed", "22.0.429"))
            self.assertFalse(receipt["signed"] or receipt["activated"] or receipt["packaged"])

    def test_unclassified_patch_stops_ci_before_build(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            (workspace / "scripts").mkdir()
            (workspace / "scripts/patch-triage.py").write_bytes((ROOT / "scripts/patch-triage.py").read_bytes())
            (workspace / "patches").mkdir()
            lock = dict(commit="base", patches=["patches/new.patch"], patch_sha256={})
            (workspace / "source.lock.json").write_text(json.dumps(dict(cycles=lock)))
            (workspace / "patches/triage.json").write_text(json.dumps(dict(upstream="base", patches=[])))
            job = dict(tier="fast", commit="a" * 40)
            with (
                patch.object(ci, "checkout", return_value=workspace),
                patch.object(ci, "git", return_value=job["commit"]),
            ):
                self.assertFalse(ci.run_case(workspace, {}, "policy", job))
            self.assertIn("complete locked stack", (workspace / "results/policy/ci.log").read_text())

    @needs_recovery
    def test_recovery_verifies_copied_bytes_and_rejects_external_paths(self):
        import runpy

        recovery = runpy.run_path(str(RECOVERY))
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            asset = directory / "asset"
            asset.write_bytes(b"original")
            record = dict(bytes=asset.stat().st_size, sha256=recovery["digest"](asset))
            receipt = directory / "restore-receipt.json"
            receipt.write_text(json.dumps(dict(result="passed", files={"asset": record})))
            recovery["verify"](directory)
            asset.write_bytes(b"modified")
            with self.assertRaisesRegex(RuntimeError, "Failed digest"):
                recovery["verify"](directory)
            receipt.write_text(json.dumps(dict(result="passed", files={"../outside": record})))
            with self.assertRaisesRegex(RuntimeError, "external file"):
                recovery["verify"](directory)

    @needs_recovery
    def test_recovery_finds_companions_of_a_linked_worktree_in_the_main_checkout(self):
        # Installers are built from a linked worktree (the reviewed tree). The engine authoring
        # history, the local evidence folders and the releases (<main>/_work/releases) belong to
        # the main checkout, so a capture of the worktree has to look for them there.
        import runpy

        recovery = runpy.run_path(str(RECOVERY))
        with tempfile.TemporaryDirectory() as temp:
            main = Path(temp).resolve() / "project"
            main.mkdir()

            def run(*arguments):
                subprocess.run(["git", "-C", str(main), *arguments], check=True, capture_output=True)

            run("init", "-q")
            identity = ("-c", "user.name=test", "-c", "user.email=test@example.invalid")
            run(*identity, "commit", "-q", "--allow-empty", "-m", "root")
            linked = main / "_work" / "reviewed"
            run("worktree", "add", "-q", "--detach", str(linked))
            self.assertEqual(recovery["workspace_root"](main), main)
            self.assertEqual(recovery["workspace_root"](linked), main)
            self.assertTrue((linked / ".git").is_file())
            self.assertEqual(
                recovery["git_path"](linked, "shallow").resolve(), recovery["git_path"](main, "shallow").resolve()
            )
            self.assertEqual(recovery["git_path"](main, "shallow").resolve(), main / ".git/shallow")

    @needs_recovery
    def test_recovery_leaves_out_agent_scratch_but_keeps_review_notes(self):
        import runpy

        recovery = runpy.run_path(str(RECOVERY))
        scratch, folders = recovery["scratch"], recovery["SCRATCH"]
        # The review folder's name lives only in the recovery tool; the probes and the
        # runner output inside it are scratch, the review notes beside them are kept.
        review = folders[0][0]
        self.assertEqual(sorted(folders), [(review, "probes"), (review, "runner")])
        for prefix in folders:
            for inside in (Path(*prefix), Path(*prefix, "E4/out/a.json"), Path(*prefix, "results")):
                self.assertTrue(scratch(inside), inside)
        for kept in (Path(review, "review-notes.md"), Path(review), Path("UI_mockup/probes/a.png"), Path("probes/x")):
            self.assertFalse(scratch(kept), kept)

    @needs_recovery
    def test_recovery_only_runs_for_passing_fast_receipts_and_uses_tested_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            receipt = Path(temp) / "receipt.json"
            config = dict(recovery_directory=temp, repository=str(ROOT), state=temp)
            with patch.object(ci.subprocess, "run") as run:
                for status, tier in [("failed", "fast"), ("passed", "nightly"), ("passed", "fast")]:
                    receipt.write_text(json.dumps(dict(result=status, tier=tier, commit="tested-revision")))
                    ci.recover_green_fast(config, receipt)
                run.assert_called_once()
                assert run.call_args.args[0][-2:] == ["--revision", "tested-revision"]

    def test_queued_commit_survives_a_branch_advance(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, state = root / "repository", root / "state"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)

            def git(*args, cwd=None):
                return subprocess.check_output(
                    [
                        "git",
                        "-C",
                        str(repo),
                        "-c",
                        "user.name=fixture",
                        "-c",
                        "user.email=fixture@example.invalid",
                        *args,
                    ],
                    text=True,
                ).strip()

            git("commit", "-q", "--allow-empty", "-m", "first")
            first = git("rev-parse", "HEAD")
            with patch.object(ci, "git", git):
                ci.enqueue(state, "fast", "HEAD")
                git("commit", "-q", "--allow-empty", "-m", "second")
                ci.enqueue(state, "fast", "HEAD")
                ci.enqueue(state, "fast", first)
            self.assertEqual(
                {ci.read(p)["commit"] for p in (state / "pending").glob("*.json")}, {first, git("rev-parse", "HEAD")}
            )

    def test_failed_build_publishes_failed_receipt_and_diagnostic(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(ci, "checkout", side_effect=RuntimeError("missing SDK")),
        ):
            state = Path(temp)
            job = {"tier": "fast", "commit": "a" * 40}
            self.assertFalse(ci.run_case(state, {}, "fixture", job))
            out = state / "results/fixture"
            receipt = json.loads((out / "receipt.json").read_text())
            self.assertEqual((receipt["commit"], receipt["result"]), (job["commit"], "failed"))
            self.assertIn("missing SDK", (out / "ci.log").read_text())


MODULES = ("workstation-ci.py",)


def load(filename):
    spec = importlib.util.spec_from_file_location(filename.replace("-", "_"), ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HiddenChildren(unittest.TestCase):
    def test_pythonw_tools_use_console_sibling_and_fail_if_missing(self):
        module = load("workstation-ci.py")
        if os.name != "nt":
            self.assertEqual(module.console_python(), __import__("sys").executable)
            return
        with tempfile.TemporaryDirectory() as directory:
            python = Path(directory) / "python.exe"
            pythonw = Path(directory) / "pythonw.exe"
            with patch.object(module.sys, "executable", str(pythonw)):
                with self.assertRaisesRegex(RuntimeError, "alongside pythonw.exe"):
                    module.console_python()
                python.touch()
                self.assertEqual(module.console_python(), str(python))
            with patch.object(module.sys, "executable", str(python)):
                self.assertEqual(module.console_python(), str(python))

    def test_every_direct_console_child_has_windows_options(self):
        for name in MODULES:
            tree = ast.parse((ROOT / "scripts" / name).read_text())
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "subprocess"
                    and node.func.attr in ("run", "check_output", "Popen")
                ):
                    with self.subTest(module=name, line=node.lineno):
                        self.assertTrue(
                            any(
                                k.arg is None
                                and isinstance(k.value, ast.Call)
                                and isinstance(k.value.func, ast.Name)
                                and k.value.func.id == "hidden_child_options"
                                for k in node.keywords
                            )
                        )

    def test_windows_flag_does_not_break_away_from_job(self):
        for name in MODULES:
            # Extract only the helper, avoiding CLI execution in environment-report.
            tree = ast.parse((ROOT / "scripts" / name).read_text())
            helper = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "hidden_child_options")
            code = compile(ast.Module(body=[helper], type_ignores=[]), str(ROOT / "scripts" / name), "exec")
            scope = {"os": os, "subprocess": subprocess}
            exec(code, scope)
            with patch.object(os, "name", "nt"), patch.object(subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True):
                self.assertEqual(scope["hidden_child_options"](), {"creationflags": 0x08000000})
            with patch.object(os, "name", "posix"):
                self.assertEqual(scope["hidden_child_options"](), {})

    def test_runner_git_output_and_failures_survive(self):
        module = load("workstation-ci.py")
        with patch.object(module.subprocess, "check_output", return_value="revision\n") as child:
            self.assertEqual(module.git("rev-parse", "HEAD", cwd=Path(".")), "revision")
        self.assertEqual(child.call_args.args[0], ["git", "-C", ".", "rev-parse", "HEAD"])
        self.assertTrue(child.call_args.kwargs["text"])
        self.assertEqual(
            child.call_args.kwargs.get("creationflags"), subprocess.CREATE_NO_WINDOW if os.name == "nt" else None
        )
        error = subprocess.CalledProcessError(23, ["git"], output=b"failure")
        with patch.object(module.subprocess, "check_output", side_effect=error):
            with self.assertRaises(subprocess.CalledProcessError) as caught:
                module.git("rev-parse", "HEAD", cwd=Path("."))
        self.assertIs(caught.exception, error)

    def test_runner_only_refresh_has_no_scheduler_or_configuration_mutations(self):
        content = (ROOT / "scripts/refresh-workstation-ci-runner.ps1").read_text()
        for forbidden in (
            "Register-ScheduledTask",
            "Disable-ScheduledTask",
            "Enable-ScheduledTask",
            "Start-ScheduledTask",
            "Stop-ScheduledTask",
            "Set-ScheduledTask",
            "New-ScheduledTask",
            "Set-Content",
            "git config",
        ):
            self.assertNotIn(forbidden, content)
        self.assertIn("[IO.File]::Move($pendingWorker, $installedWorker, $true)", content)
        self.assertIn("SupportsShouldProcess", content)
        self.assertIn("Get-FileHash", content)


class WindowsProcesses:
    def __init__(self):
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        for name, args, result in [
            ("CreateJobObjectW", (ctypes.c_void_p, wintypes.LPCWSTR), wintypes.HANDLE),
            ("AssignProcessToJobObject", (wintypes.HANDLE, wintypes.HANDLE), wintypes.BOOL),
            ("TerminateJobObject", (wintypes.HANDLE, wintypes.UINT), wintypes.BOOL),
            ("OpenProcess", (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD), wintypes.HANDLE),
            ("WaitForSingleObject", (wintypes.HANDLE, wintypes.DWORD), wintypes.DWORD),
            ("TerminateProcess", (wintypes.HANDLE, wintypes.UINT), wintypes.BOOL),
            ("CloseHandle", (wintypes.HANDLE,), wintypes.BOOL),
            ("GetHandleInformation", (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)), wintypes.BOOL),
        ]:
            function = getattr(self.api, name)
            function.argtypes, function.restype = args, result

    def checked(self, value):
        if not value:
            raise ctypes.WinError(ctypes.get_last_error())
        return value


def wait_file(path, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(0.02)
    raise TimeoutError("Disposable CI fixture did not create " + str(path))


def job_fixture(role, folder, executable, exit_code):
    folder = Path(folder)
    if role == "grandchild":
        ci.publish(folder / "grandchild.json", {"pid": os.getpid()})
        wait_file(folder / "finish", 30)
        return 0
    if role in ("owner", "owner-exit"):
        wait_file(folder / "release")
        ci.contain_drain_process()
        # Calling twice must retain the same owner handle, not leak extra jobs.
        handle = ci._drain_job_handle
        ci.contain_drain_process()
        assert handle == ci._drain_job_handle
        api = WindowsProcesses()
        flags = wintypes.DWORD()
        api.checked(api.api.GetHandleInformation(handle, ctypes.byref(flags)))
        ci.publish(folder / "owner.json", {"pid": os.getpid(), "inheritable": bool(flags.value & 1)})
        child_role = "child"
    else:
        child_role = "grandchild"
    child = subprocess.Popen(
        [executable, __file__, "--job-fixture", child_role, str(folder), executable, str(exit_code)],
        # Deliberately exercise native handle inheritance; the job handle must
        # still stay private to its owner, just as with PowerShell descendants.
        close_fds=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if role == "child":
        wait_file(folder / "grandchild.json")
        ci.publish(
            folder / "descendants.json",
            {"child": os.getpid(), "grandchild": ci.read(folder / "grandchild.json")["pid"]},
        )
    if role == "owner-exit":
        wait_file(folder / "descendants.json")
        wait_file(folder / "owner-exit", 30)
        return exit_code  # Leave both descendants alive for the OS job to reap.
    result = child.wait(timeout=35)
    if result:
        return result
    return exit_code if role == "owner" else 0


@unittest.skipUnless(os.name == "nt", "Windows job containment")
class DrainContainmentContracts(unittest.TestCase):
    def tree(self, *, terminate, nested, exit_code=0, leave_children=False):
        api = WindowsProcesses()
        # Hython's bundled console Python exercises the same OS behavior without
        # starting multiple full Houdini hosts or claiming render-device work.
        executable = Path(os.environ["HFS"]) / "python313/python.exe" if os.environ.get("HFS") else Path(sys.executable)
        owner_executable = executable.with_name("pythonw.exe")
        if not owner_executable.is_file():
            owner_executable = executable
        with tempfile.TemporaryDirectory(prefix="ch-ci-job-") as temporary:
            folder = Path(temporary)
            handles = []
            outer = None
            with (folder / "process.log").open("w") as log:
                owner = subprocess.Popen(
                    [
                        str(owner_executable),
                        __file__,
                        "--job-fixture",
                        "owner-exit" if leave_children else "owner",
                        str(folder),
                        str(executable),
                        str(exit_code),
                    ],
                    stdout=log,
                    stderr=log,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                try:
                    if nested:
                        # Emulate Task Scheduler: a different process retains
                        # the existing ancestor job while the drain nests its own.
                        outer = api.checked(api.api.CreateJobObjectW(None, None))
                        api.checked(api.api.AssignProcessToJobObject(outer, int(owner._handle)))
                    (folder / "release").touch()
                    wait_file(folder / "descendants.json")
                    descendants = ci.read(folder / "descendants.json")
                    self.assertFalse(ci.read(folder / "owner.json")["inheritable"])
                    for pid in descendants.values():
                        handles.append(api.checked(api.api.OpenProcess(0x100000 | 0x1000 | 1, False, pid)))
                    if terminate:
                        owner.kill()  # Exact disposable parent handle, no process scan.
                    else:
                        (folder / ("owner-exit" if leave_children else "finish")).touch()
                    result = owner.wait(timeout=10)
                    if not terminate:
                        self.assertEqual(result, exit_code)
                    for handle in handles:
                        self.assertEqual(
                            api.api.WaitForSingleObject(handle, 5000), 0, "Owner exit left its child/grandchild alive"
                        )
                finally:
                    if owner.poll() is None:
                        owner.kill()
                        owner.wait(timeout=10)
                    # Failure cleanup is restricted to the exact disposable job
                    # and handles retained above. Never enumerate workstation PIDs.
                    if outer:
                        api.api.TerminateJobObject(outer, 1)
                        api.api.CloseHandle(outer)
                    for handle in handles:
                        if api.api.WaitForSingleObject(handle, 0) == 0x102:
                            api.api.TerminateProcess(handle, 1)
                            api.api.WaitForSingleObject(handle, 5000)
                        api.api.CloseHandle(handle)
                log.flush()
                if owner.returncode and not terminate and owner.returncode != exit_code:
                    self.fail((folder / "process.log").read_text())

    def test_terminated_pythonw_drain_kills_child_and_grandchild(self):
        self.tree(terminate=True, nested=False)

    def test_terminated_drain_inside_scheduler_job_kills_descendants(self):
        self.tree(terminate=True, nested=True)

    def test_normal_drain_exit_codes_are_preserved_inside_nested_job(self):
        for code in (0, 23):
            with self.subTest(exit_code=code):
                self.tree(terminate=False, nested=True, exit_code=code)

    def test_normal_owner_exit_reaps_live_descendants_without_changing_exit_code(self):
        self.tree(terminate=False, nested=True, exit_code=17, leave_children=True)

    def test_containment_failure_precedes_git_or_checkout(self):
        with (
            patch.object(sys, "argv", [str(ROOT / "scripts/workstation-ci.py"), "drain"]),
            patch.object(ci, "_drain_job_handle", None),
            patch.object(ci, "_create_and_assign_drain_job", side_effect=OSError(5, "denied")),
            patch.object(ci, "git") as git,
            patch.object(ci, "run_case") as run_case,
        ):
            with self.assertRaisesRegex(RuntimeError, "no work was started"):
                ci.main()
            git.assert_not_called()
            run_case.assert_not_called()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--job-fixture":
        raise SystemExit(job_fixture(*sys.argv[2:5], int(sys.argv[5])))
    unittest.main()
