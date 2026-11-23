# SPDX-License-Identifier: Apache-2.0
"""Real child ownership, automatic handle release, paged history and Stop intent."""

from support.paths import artifacts_root
import ctypes
import gc
from support.package_loader import import_product
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import traceback

root = Path(os.environ.get("CYCLES_HOUDINI_SOURCE_ROOT", Path(__file__).resolve().parents[1]))
try:
    import hou
except ImportError:
    hou = None
if hou:
    from cycles_houdini.rop import background_jobs as jobs

    executable = Path(hou.getenv("HFS")) / "python313/python.exe"
else:
    jobs = import_product("background_jobs")
    executable = Path(sys.executable)

destination = artifacts_root() / "stage3/background-jobs" / str(time.time_ns())
destination.mkdir(parents=True)
registry = jobs.JobRegistry()
owned = []
checks = []
report = dict(result="running", scope=__doc__, checks=checks, native_houdini=bool(hou))
child = destination / "wait.py"
child.write_text(
    "import sys,time\nfrom pathlib import Path\np=Path(sys.argv[1])\nwhile not p.is_file(): time.sleep(.01)\n",
    encoding="utf-8",
)


def until(predicate, seconds=15):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.01)
    raise TimeoutError(registry.counts())


def saved(folder, state="rendering", **extra):
    folder.mkdir(parents=True, exist_ok=True)
    value = dict(
        schema=1,
        id=folder.name,
        state=state,
        rop="/out/test",
        created=time.time(),
        completed_frames=1 if state == "completed" else 0,
        skipped_frames=0,
        error="",
        scene="artist.hiplc",
    )
    value.update(extra)
    jobs.write_json(folder / "status.json", value)
    return value


def spawn(marker):
    return subprocess.Popen(
        [str(executable), str(child), str(marker)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def launch(folder, marker):
    saved(folder)
    process = spawn(marker)
    job = registry.adopt(folder, process)
    jobs.write_json(folder / "process.json", dict(pid=process.pid, identity=jobs.process_identity(process.pid)))
    owned.append(job)
    return job


def handles():
    if os.name != "nt":
        return None
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetProcessHandleCount.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    value = wintypes.DWORD()
    if not kernel.GetProcessHandleCount(kernel.GetCurrentProcess(), ctypes.byref(value)):
        raise ctypes.WinError()
    return value.value


def stable_handles():
    samples = []
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        samples.append(handles())
        if len(samples) >= 5 and len(set(samples[-5:])) == 1:
            return samples[-1], samples
        time.sleep(0.05)
    raise AssertionError(("Host handle count did not stabilize", samples))


def warm_host_process_startup():
    # The Houdini process can initialize Windows/Python resources on its first
    # child launch. Measure the same raw spawn/identity/metadata operations
    # without registry ownership, then require a plateau before measuring the
    # original 144 owned children. A per-child leak fails the second control.
    cold = handles()
    controls = []
    report["host_startup"] = dict(cold_handles=cold, raw_process_controls=controls)
    for batch_index, count in enumerate((48, 32)):
        marker = destination / "host-warmup" / f"release-{batch_index}"
        processes = []
        try:
            for index in range(count):
                folder = marker.parent / f"raw-{batch_index}-{index:04}"
                saved(folder)
                process = spawn(marker)
                processes.append(process)
                jobs.write_json(
                    folder / "process.json", dict(pid=process.pid, identity=jobs.process_identity(process.pid))
                )
            live = handles()
            marker.touch()
            for process in processes:
                assert process.wait(timeout=15) == 0
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=15)
            processes.clear()
            process = None  # The loop variable must not retain the final Popen.
        gc.collect()
        reaped, samples = stable_handles()
        controls.append(dict(children=count, live_handles=live, reaped_handles=reaped, stable_samples=samples))
    assert controls[1]["reaped_handles"] <= controls[0]["reaped_handles"] + 8, controls
    checks.append(
        dict(name="Raw child startup stabilizes independently of registry ownership", **report["host_startup"])
    )
    return controls[-1]["reaped_handles"]


try:
    baseline = warm_host_process_startup() if os.name == "nt" else handles()
    release = destination / "release"
    # Keep more live jobs than the history cap, then release them without any
    # status()/poll() calls. The watcher, not UI activity, must release handles.
    batch = [launch(destination / f"live-{i:04}", release) for i in range(48)]
    assert registry.counts()["active"] == 48
    assert len(registry.page(destination, limit=1).jobs) == 48
    assert all(registry.get(job.directory) is job for job in batch)
    assert len([thread for thread in threading.enumerate() if thread.name == "Cycles background jobs"]) == 1
    open_handles = handles()
    release.touch()
    until(lambda: registry.counts()["active"] == 0)
    assert all(not job.has_process_handle and job.poll() == 0 for job in batch)
    assert registry.counts()["cached_history"] == 32
    gc.collect()
    after_first = handles()
    # Exercise repeated submissions; retained Job references must not retain a
    # Popen or handle even after they leave the bounded registry cache.
    for batch_index in range(3):
        batch = [launch(destination / f"exit-{batch_index}-{i:04}", release) for i in range(32)]
        until(lambda: registry.counts()["active"] == 0)
        assert all(job.wait(timeout=1) == 0 and not job.has_process_handle for job in batch)
    gc.collect()
    after_repeated = handles()
    if baseline is not None:
        assert open_handles >= baseline + 48, (baseline, open_handles)
        assert after_first <= baseline + 8 and after_repeated <= baseline + 8, (baseline, after_first, after_repeated)
    checks.append(
        dict(
            name="144 owned children reaped by one watcher with bounded resident history",
            baseline_handles=baseline,
            handles_48_live=open_handles,
            handles_after_48=after_first,
            handles_after_144=after_repeated,
            registry=registry.counts(),
        )
    )

    history = destination / "history"
    for i in range(1253):
        saved(history / f"20260101T000000-{i:05}", "completed")
    (history / "20260101T000000-01250/status.json").write_text("{broken", encoding="utf-8")
    saved(history / "20260101T000000-01249", "completed", frame="broken")
    (history / "20260101T000000-01248/status.json").write_bytes(b" " * (4 * 1024 * 1024 + 1))
    saved(history / "20260101T000000-01247", [])
    saved(history / "20260101T000000-01246", "completed", created=1e300)
    saved(history / "20260101T000000-01245", "completed", frame=10**400)
    cursor = None
    visited = []
    unavailable = 0
    pages = 0
    while True:
        page = registry.page(history, limit=50, before=cursor)
        assert len(page.jobs) <= 50 and registry.counts()["cached_history"] <= 32
        visited.extend(job.id for job in page.jobs)
        pages += 1
        unavailable += sum(job.status()["state"] == "unavailable" for job in page.jobs)
        if page.older_than is None:
            break
        cursor = page.older_than
    assert len(visited) == len(set(visited)) == 1253 and unavailable == 6
    assert visited == sorted(visited, reverse=True)
    assert (history / "20260101T000000-01250/status.json").read_text() == "{broken"
    for limit in (0, 201, True):
        try:
            registry.page(history, limit=limit)
        except ValueError:
            pass
        else:
            raise AssertionError(limit)
    checks.append(
        dict(
            name="All 1253 saved jobs accessible in bounded pages; malformed entries isolated",
            pages=pages,
            jobs=len(visited),
            unavailable=unavailable,
            cached_history=registry.counts()["cached_history"],
        )
    )

    stop = launch(destination / "stop-intent", destination / "never-release")
    stop.stop()
    request = (stop.directory / "stop").stat().st_mtime_ns
    time.sleep(0.025)
    stop.stop()
    assert stop.status()["state"] == "stopping" and (stop.directory / "stop").stat().st_mtime_ns == request
    assert stop.status()["worker_state"] == "rendering" and stop.is_running()
    # Active jobs from a different configured root remain on the newest page.
    newest = registry.page(history, limit=2)
    assert stop in newest.jobs and len(newest.jobs) == 3
    stop.force_stop()
    stop.wait(timeout=10)
    forced = stop.status()
    assert forced["state"] == "cancelled" and forced["forced"] and forced["completed_frames"] == 0, forced
    unexpected = launch(destination / "unexpected-exit", destination / "never-release")
    unexpected.terminate()
    unexpected.wait(timeout=10)
    assert unexpected.status()["state"] == "failed" and not unexpected.status()["forced"]
    checks.append(
        dict(
            name="Stop is immediate and idempotent; explicit Force Stop differs from unexpected exit",
            forced=forced,
            unexpected=unexpected.status(),
        )
    )

    victim = launch(destination / "identity-victim", destination / "never-release")
    fake = destination / "reused-pid"
    saved(fake)
    identity = dict(jobs.process_identity(victim.pid))
    identity["created"] -= 1
    jobs.write_json(fake / "process.json", dict(pid=victim.pid, identity=identity))
    recovered = jobs.Job(fake)
    assert recovered.status()["state"] == "failed"
    try:
        recovered.force_stop()
    except RuntimeError:
        pass
    else:
        raise AssertionError("Recovered jobs must not acquire termination rights from a PID")
    assert victim.is_running()
    for metadata in (
        [1],
        dict(pid="broken"),
        dict(pid=victim.pid, identity=[]),
        dict(pid=victim.pid, identity=dict(alive=True, created="broken")),
    ):
        saved(fake)
        jobs.write_json(fake / "process.json", metadata)
        malformed = jobs.Job(fake).status()
        assert malformed["state"] == "rendering" and malformed["process_report_error"], malformed
        assert victim.is_running()
    # Deterministically reproduce a final report appearing after the first read
    # but before the parent observes exit; keep the genuine terminal success.
    raced = destination / "completion-race"
    saved(raced)

    class Race(jobs.Job):
        def poll(self):
            saved(self.directory, "completed")
            return 0

    race = Race(raced)
    race._owned = True
    assert race.status()["state"] == "completed"
    registry.shutdown()
    assert victim.is_running() and not registry.counts()["watcher_alive"]
    victim.terminate()
    victim.wait(timeout=10)
    checks.append(dict(name="PID reuse cannot signal unrelated children; completion race and parent shutdown safe"))
    report.update(result="passed")
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    for job in owned:
        if job.is_running():
            job.terminate()
            job.wait(timeout=10)
    registry.shutdown()
    (destination / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (destination.parent / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result=report["result"], checks=len(checks), report=str(destination / "report.json"))))
