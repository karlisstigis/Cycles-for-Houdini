# SPDX-License-Identifier: Apache-2.0
"""Actual HDA startup handlers latch one known SDK error across HIP replacement.

Run only in a new headless hython process. The injected fault is missing staged
metadata in an artifact-only fake host, not DLL replacement. No renderer worker
should start. A test-only latch reset after restoring metadata checks healthy
initialization again; this does not promise in-place repair of loaded DLLs.
"""

from support.paths import artifacts_root
import argparse
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import time
import traceback

import hou
import cycles_houdini_startup as startup


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--device", default="CPU", help="Accepted for the verification runner; no rendering device is started"
)
parser.add_argument("--report", type=Path)
args = parser.parse_args()
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"]).resolve()
args.report = args.report or artifacts_root() / "stage0/startup-dependency/report.json"
args.report.parent.mkdir(parents=True, exist_ok=True)
folder = args.report.parent / ("run-" + str(os.getpid()) + "-" + str(time.time_ns()))
folder.mkdir()
report = dict(
    result="running",
    scope=__doc__,
    folder=str(folder),
    phases=[],
    validator_calls={},
    houdini=hou.applicationVersionString(),
    pid=os.getpid(),
    device_argument=args.device,
)
environment_keys = ("CYCLES_HOUDINI_HOST_ROOT", "CYCLES_HOUDINI_RESOURCES")
saved_environment = {name: os.environ.get(name) for name in environment_keys}
saved_error = startup.startup_error()
saved_profile = sys.getprofile()
original_package = None
original_modules = {}
profile_phase = None
validator_code = None


def save():
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")


def environment(values):
    for name, value in values.items():
        if value is None:
            os.environ.pop(name, None)
            hou.unsetenv(name)
        else:
            os.environ[name] = str(value)
            hou.putenv(name, str(value))


def workers():
    """Inspect this private test process's children without spawning a helper."""

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = kernel.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    processes = []
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        more = kernel.Process32FirstW(handle, ctypes.byref(entry))
        if not more:
            raise ctypes.WinError(ctypes.get_last_error())
        while more:
            processes.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile.lower()))
            more = kernel.Process32NextW(handle, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(handle)
    descendants = {os.getpid()}
    while True:
        found = {pid for pid, parent, _ in processes if parent in descendants}
        if found <= descendants:
            break
        descendants.update(found)
    return [pid for pid, _, name in processes if pid in descendants and name == "ch_worker.exe"]


@contextmanager
def capture(label, *, automatic=True):
    """Capture Python and native host callback output, restoring both FDs."""
    global profile_phase
    row = dict(
        label=label,
        stdout=str(folder / (label + ".stdout.log")),
        stderr=str(folder / (label + ".stderr.log")),
        automatic_callbacks=automatic,
    )
    report["phases"].append(row)
    profile_phase = label
    sys.stdout.flush()
    sys.stderr.flush()
    saved = [os.dup(fd) for fd in (1, 2)]
    try:
        with Path(row["stdout"]).open("wb") as stdout, Path(row["stderr"]).open("wb") as stderr:
            try:
                os.dup2(stdout.fileno(), 1)
                os.dup2(stderr.fileno(), 2)
                yield row
            finally:
                sys.stdout.flush()
                sys.stderr.flush()
                for fd, original in zip((1, 2), saved):
                    os.dup2(original, fd)
    finally:
        for original in saved:
            os.close(original)
        profile_phase = None
        text = "\n".join(Path(row[key]).read_text(encoding="utf-8", errors="replace") for key in ("stdout", "stderr"))
        row.update(
            diagnostic_messages=text.count("Cycles renderer unavailable:"),
            traceback_headers=text.count("Traceback (most recent call last)"),
            callback_error_mentions=text.count("Python Callback Error"),
            worker_pids=workers(),
        )
        row["sha256"] = {key: hashlib.sha256(Path(row[key]).read_bytes()).hexdigest() for key in ("stdout", "stderr")}
        save()
    assert not row["worker_pids"], row
    if automatic:
        assert row["traceback_headers"] == row["callback_error_mentions"] == 0, row


def profile(frame, event, argument):
    if event == "call" and frame.f_code is validator_code and profile_phase:
        report["validator_calls"][profile_phase] = report["validator_calls"].get(profile_phase, 0) + 1


def fresh_package():
    """Rebind cached children to the fresh lazy runtime, retaining the SDK error type.

    Device selection and the IPR registry import package API functions directly.
    Keeping those children would keep using the already validated original
    runtime and bypass the metadata fault this fresh-package scenario tests.
    The loaded native extension/DLLs remain untouched.
    """
    for name in tuple(sys.modules):
        if (name == "cycles_houdini" or name.startswith("cycles_houdini.")) and name != sdk.__name__:
            sys.modules.pop(name)
    return importlib.import_module("cycles_houdini")


def nodes():
    return [hou.node("/out/startup_dependency_" + str(i).zfill(2)) for i in range(20)]


def verify_editable(values, error=None):
    result = []
    for index, node in enumerate(values):
        assert node is not None and not node.errors(), (node, node.errors() if node else None)
        assert node.cachedUserData("cycles_startup_error") == error, node.path()
        width = node.parm("resx")
        assert width is not None and not width.isLocked()
        width.set(32 + index)
        assert width.eval() == 32 + index, node.path()
        result.append(node.path())
    return result


try:
    assert not hou.isUIAvailable(), "Use a new headless hython process, never an artist session"
    assert Path(hou.hipFile.name()).stem.lower() == "untitled", "Start a new hython without loading an artist HIP file"
    assert saved_error is None, saved_error
    assert workers() == []
    hou.hipFile.clear(suppress_save_prompt=True)
    with capture("healthy_created") as phase:
        original_package = importlib.import_module("cycles_houdini")
        sdk = importlib.import_module("cycles_houdini.core.sdk_dependencies")
        validator_code = sdk.validate_runtime.__code__
        sys.setprofile(profile)
        from cycles_houdini.rop import final_render
        from cycles_houdini.ipr import registry

        camera = hou.node("/obj").createNode("cam", "startup_camera")
        subject = hou.node("/obj").createNode("geo", "startup_subject", run_init_scripts=False)
        subject.createNode("box").setRenderFlag(True)
        for index in range(20):
            rop = hou.node("/out").createNode("cycles_ipr", "startup_dependency_" + str(index).zfill(2))
            rop.setParms(
                dict(
                    camera=camera.path(),
                    resx=16,
                    resy=16,
                    samples=1,
                    use_denoise=0,
                    device_mode="cpu",
                    picture=str(folder / "must-not-be-written.exr"),
                )
            )
            # Keep the real SOHO entry points reachable after faulting HOST_ROOT;
            # the explicit render must reach the SDK guard, not a missing script.
            soho_program = Path(hou.findFile("soho/CHRender.py")).resolve()
            assert soho_program.is_file(), soho_program
            rop.parm("soho_program").set('"' + soho_program.as_posix() + '"')
            rop.parm("soho_pipecmd").set(rop.parm("soho_pipecmd").evalAsString())
            if index == 0:
                report["soho_program"] = dict(
                    path=str(soho_program),
                    sha256=hashlib.sha256(soho_program.read_bytes()).hexdigest(),
                    pipe_command=rop.parm("soho_pipecmd").evalAsString(),
                )
        definition = nodes()[0].type().definition()
        sections = {name: definition.sections()[name].contents() for name in ("OnCreated", "OnLoaded")}
        assert all("cycles_houdini_startup" in code and "initialize_rop" in code for code in sections.values()), (
            sections
        )
        report["handler_sha256"] = {name: hashlib.sha256(code.encode()).hexdigest() for name, code in sections.items()}
        phase["initialized"] = verify_editable(nodes())
        assert all(
            any(callback is final_render._node_event for _, callback in node.eventCallbacks()) for node in nodes()
        )
        assert not registry.entries() and not final_render.active_sequences()
        scene = folder / "twenty-cycles-rops.hiplc"
        hou.hipFile.save(str(scene))
        report["scene"] = dict(path=str(scene), sha256=hashlib.sha256(scene.read_bytes()).hexdigest())
        original_modules = {
            name: module
            for name, module in sys.modules.items()
            if name == "cycles_houdini" or name.startswith("cycles_houdini.")
        }

    fake_host = folder / "missing-sdk-host"
    (fake_host / "resources").mkdir(parents=True)
    environment(dict(CYCLES_HOUDINI_HOST_ROOT=str(fake_host), CYCLES_HOUDINI_RESOURCES=str(fake_host / "resources")))
    # Recreate the package's lazy native proxy while retaining the already
    # imported SDK child/error class and real loaded DLL. Import itself is pure;
    # the automatic startup callback must reject the first native operation.
    fresh_package()
    with capture("automatic_failure") as phase:
        hou.hipFile.load(str(scene), suppress_save_prompt=True, ignore_load_warnings=True)
        known = startup.startup_error()
        assert known and "staged SDK dependency policy is missing" in known, known
        assert sys.modules.get("cycles_houdini.core.sdk_dependencies") is sdk
        faulted_package = importlib.import_module("cycles_houdini")
        assert faulted_package._native._module is None, "Failed startup committed a native runtime"
        phase["lazy_package_importable"] = True
        phase["loaded_editable"] = verify_editable(nodes(), known)
        extra = [hou.node("/out").createNode("cycles_ipr", "failed_created_" + str(i)) for i in range(5)]
        phase["created_editable"] = verify_editable(extra, known)
        from cycles_houdini.rop import final_render
        from cycles_houdini.ipr import registry

        assert startup.startup_error() == known and not registry.entries() and not final_render.active_sequences()
        report["latched_error"] = known
    assert phase["diagnostic_messages"] == 1, phase
    assert report["validator_calls"].get("automatic_failure", 0) == 1, report["validator_calls"]

    with capture("explicit_native_load_rejection", automatic=False) as phase:
        try:
            importlib.import_module("cycles_houdini").load()
        except sdk.SDKDependencyError as error:
            phase["rejected"] = str(error)
            assert str(error) == known
            assert faulted_package._native._module is None
        else:
            raise AssertionError("Explicit native load accepted missing SDK metadata")

    with capture("explicit_render_rejection", automatic=False) as phase:
        render_log = folder / "explicit-render.log"
        nodes()[0].parm("soho_diskfile").set(str(render_log))
        try:
            nodes()[0].render(frame_range=(1, 1), ignore_inputs=True)
        except Exception as error:
            phase["exception"] = dict(type=type(error).__name__, message=str(error))
        phase["node_errors"] = list(nodes()[0].errors())
        if render_log.is_file():
            phase["soho_log"] = str(render_log)
            phase["soho_log_sha256"] = hashlib.sha256(render_log.read_bytes()).hexdigest()
    rejection = (
        json.dumps(phase)
        + Path(phase["stdout"]).read_text(errors="replace")
        + Path(phase["stderr"]).read_text(errors="replace")
        + (render_log.read_text(errors="replace") if render_log.is_file() else "")
    )
    assert "Cycles Houdini SDK dependency" in rejection and "staged SDK dependency policy is missing" in rejection, (
        rejection
    )
    assert phase.get("exception") or phase["node_errors"], phase
    assert not (folder / "must-not-be-written.exr").exists()

    environment(saved_environment)
    with capture("healthy_reloaded") as phase:
        recovered = fresh_package()
        assert sdk.validate_runtime(require_loaded=True)["provider"] == "houdini-sdk"
        # Metadata is healthy again. Reset only this process's automatic-startup
        # latch; no DLL is unloaded or replaced and no artist state is touched.
        startup._startup_error = None
        hou.hipFile.load(str(scene), suppress_save_prompt=True, ignore_load_warnings=True)
        from cycles_houdini.rop import final_render
        from cycles_houdini.ipr import registry

        assert startup.startup_error() is None
        phase["initialized"] = verify_editable(nodes())
        assert all(
            any(callback is final_render._node_event for _, callback in node.eventCallbacks()) for node in nodes()
        )
        assert not registry.entries() and not final_render.active_sequences()
    assert phase["diagnostic_messages"] == 0, phase
    assert hashlib.sha256(scene.read_bytes()).hexdigest() == report["scene"]["sha256"]
    report.update(
        result="passed",
        saved_rops=20,
        failed_created_rops=5,
        automatic_diagnostics=1,
        uncaught_automatic_tracebacks=0,
        workers_started=False,
        recovery_scope="Test-only reset after restoring metadata; loaded SDK DLL replacement still requires host restart",
    )
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
finally:
    sys.setprofile(saved_profile)
    environment(saved_environment)
    if original_modules:
        for name in tuple(sys.modules):
            if name == "cycles_houdini" or name.startswith("cycles_houdini."):
                sys.modules.pop(name)
        sys.modules.update(original_modules)
    elif original_package is not None:
        sys.modules["cycles_houdini"] = original_package
    startup._startup_error = saved_error
    report["remaining_worker_pids"] = workers()
    save()
assert report["result"] == "passed", report.get("error")
assert report["remaining_worker_pids"] == []
print(
    json.dumps(
        dict(
            result=report["result"],
            saved_rops=report["saved_rops"],
            failed_created_rops=report["failed_created_rops"],
            automatic_diagnostics=report["automatic_diagnostics"],
            report=str(args.report),
        )
    )
)
