# SPDX-License-Identifier: Apache-2.0
"""Completed/rearmed exchanges cannot fire stale timeouts; hung workers stop.

Long operations extend only while the worker is observably active, never
beyond their ceiling, and a stalled long operation is still killed.
"""

import runpy
import subprocess
import sys
import time
from pathlib import Path

package = Path(__file__).resolve().parents[1] / "houdini/python3.13libs/cycles_houdini"
Watchdog = runpy.run_path(str(package / "ipr/exchange_watchdog.py"))["ExchangeWatchdog"]
ProcessActivity = runpy.run_path(str(package / "ipr/worker_liveness.py"))["ProcessActivity"]


def child():
    return subprocess.Popen(
        [sys.executable, "-c", 'import time; print("ready",flush=True); time.sleep(30)'],
        stdout=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


process = child()
assert process.stdout.readline().strip() == b"ready"
guard = Watchdog(process)
try:
    worker = guard.thread
    for _ in range(10000):
        guard.arm(0.1)
        guard.disarm()
    time.sleep(0.12)
    assert process.poll() is None and guard.thread is worker
    guard.arm(0.02)
    guard.arm(0.15)
    time.sleep(0.05)
    assert process.poll() is None, "A superseded timeout killed a healthy exchange"
    guard.disarm()
    time.sleep(0.12)
    assert process.poll() is None
    guard.arm(0.02)
    process.wait(timeout=2)
    assert process.returncode != 0
finally:
    guard.close()
    if process.poll() is None:
        process.kill()
        process.wait(timeout=2)
    process.stdout.close()
assert not guard.thread.is_alive()
process = child()
process.stdout.readline()
guard = Watchdog(process)
guard.arm(0.02)
guard.close()
time.sleep(0.04)
assert process.poll() is None, "Closing left an armed deadline behind"
process.kill()
process.wait(timeout=2)
process.stdout.close()
try:
    guard.arm(0.01)
except RuntimeError:
    pass
else:
    raise AssertionError("A closed monitor accepted a new exchange")


class Counter:
    """Deterministic activity: advances only while 'active' is set."""

    def __init__(self):
        self.value = 0
        self.active = True

    def sample(self):
        if self.active:
            self.value += 1
        return self.value, 0

    def progressed(self, before, after):
        return before is not None and after is not None and after[0] > before[0]


def stop(process):
    if process.poll() is None:
        process.kill()
    process.wait(timeout=2)
    process.stdout.close()


# A long, active operation survives many windows but not its ceiling.
process = child()
process.stdout.readline()
activity = Counter()
guard = Watchdog(process, activity)
try:
    guard.arm(0.05, ceiling=0.6)
    time.sleep(0.35)
    assert process.poll() is None and guard.extensions >= 3, "An active long operation was killed"
    process.wait(timeout=2)
    assert process.returncode != 0, "The ceiling did not bound an active operation"
finally:
    guard.close()
    stop(process)
# A long operation that stops progressing dies after one more window.
process = child()
process.stdout.readline()
activity = Counter()
guard = Watchdog(process, activity)
try:
    guard.arm(0.05, ceiling=30.0)
    time.sleep(0.17)
    assert process.poll() is None
    extended = guard.extensions
    activity.active = False
    process.wait(timeout=2)
    assert guard.extensions <= extended + 1, "A stalled operation kept extending"
finally:
    guard.close()
    stop(process)
# Strict exchanges ignore activity; completion disarms the ceiling.
process = child()
process.stdout.readline()
activity = Counter()
guard = Watchdog(process, activity)
try:
    guard.arm(0.05, ceiling=10.0)
    guard.disarm()
    guard.arm(0.05)
    process.wait(timeout=2)
    assert guard.extensions == 0, "A strict exchange used the long-operation ceiling"
finally:
    guard.close()
    stop(process)
# Real process accounting: a CPU-busy worker is extended, an idle one is not.
if sys.platform == "win32":
    busy = subprocess.Popen(
        [
            sys.executable,
            "-c",
            'import time\nprint("ready",flush=True)\nend=time.time()+30\nwhile time.time()<end:pass',
        ],
        stdout=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    busy.stdout.readline()
    guard = Watchdog(busy, ProcessActivity(busy, minimum_cpu_seconds=0.02))
    try:
        guard.arm(0.3, ceiling=30.0)
        time.sleep(1.2)
        assert busy.poll() is None and guard.extensions >= 2, "A CPU-active worker was not extended"
    finally:
        guard.close()
        stop(busy)
    idle = child()
    idle.stdout.readline()
    time.sleep(0.2)
    guard = Watchdog(idle, ProcessActivity(idle, minimum_cpu_seconds=0.02))
    try:
        guard.arm(0.3, ceiling=30.0)
        idle.wait(timeout=3)
        assert guard.extensions == 0, "An idle worker was treated as active"
    finally:
        guard.close()
        stop(idle)
print(
    "Passed: 10,000 completed exchanges, superseded deadline, hung worker, closure, thread retirement, "
    "active long operations, ceilings, stalled long operations and real process activity"
)
