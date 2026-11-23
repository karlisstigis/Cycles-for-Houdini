# SPDX-License-Identifier: Apache-2.0
"""Persistent external-renderer controller for native Cycles IPR.

Only host capture touches Houdini. One controller owns the backend; one complete
desired snapshot may be pending. Diffing against ACK preserves coalesced edits.
"""

from __future__ import annotations

import atexit
import json
import mmap
import os
from pathlib import Path
import struct
import subprocess
import threading
import time
import uuid
import weakref
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from functools import partial
from .. import Session, _native, default_device
from . import session_log
from .exchange_watchdog import ExchangeWatchdog
from .worker_liveness import ProcessActivity
from .worker_log import WorkerLog

# Main-thread budget for running consecutive capture stages in one host poll.
# CYCLES_HOUDINI_CAPTURE_CHAIN_MS=0 restores one stage per poll (for comparison).
_CAPTURE_CHAIN_SECONDS = max(0.0, float(os.environ.get("CYCLES_HOUDINI_CAPTURE_CHAIN_MS", "4"))) / 1000.0


class _CaptureInterrupted(Exception):
    """An artist-cancelled host acquisition, with the accepted render retained."""


@contextmanager
def _motion_capture_stage(update, stage, submission, *, interactive):
    motion = update.get("motion") or {}
    geometry = update.get("geometry")
    if not geometry or not (motion.get("vectors") or (motion.get("enabled") and motion.get("shutter_frames"))):
        yield
        return
    from ..scene.instancing import nested_requests

    if not any(
        source.get("motion") == "deformation" and source.get("motion_blur", True)
        for source in nested_requests(geometry)
    ):
        yield
        return

    started = time.perf_counter()
    result = "complete"
    hou = None
    session_log.write("motion blur acquisition began", stage=stage, submission=submission)
    try:
        operation = nullcontext()
        if interactive:
            import hou

            if hou.isUIAvailable():
                operation = hou.InterruptableOperation("Reading the scene for motion blur")
        with operation as progress:
            if progress is not None:
                progress.updateProgress()
            yield
    except Exception as error:
        if (hou is not None and isinstance(error, hou.OperationInterrupted)) or isinstance(
            error, getattr(_native, "MotionCaptureInterrupted", _CaptureInterrupted)
        ):
            result = "cancelled"
            raise _CaptureInterrupted("Motion blur scene acquisition cancelled") from error
        result = "failed"
        raise
    finally:
        session_log.write(
            "motion blur acquisition ended",
            stage=stage,
            submission=submission,
            seconds=time.perf_counter() - started,
            result=result,
        )


def _copy_update(update):
    """Copy mutable request metadata while retaining immutable native meshes.

    Mesh capsules own shared const MeshData. They are deliberately neither
    pickleable nor copyable; native acquisition takes another shared owner
    before asynchronous conversion or worker snapshot serialization begins.
    """
    from ..blender.prototype_leases import split_update

    update, _ = split_update(update)
    memo, visited = {}, set()

    def request(value):
        if not isinstance(value, dict) or id(value) in visited:
            return
        visited.add(id(value))
        for kind in ("point_instancing", "packed_blender"):
            for prototype in value.get(kind, {}).get("prototypes", ()):
                for member in prototype.get("direct_instances", ()):
                    mesh = member.get("mesh")
                    if mesh is not None:
                        memo[id(mesh)] = mesh
                request(prototype.get("request"))

    for value in update.get("geometry", ()):
        request(value)
    return deepcopy(update, memo)


_HEADER = struct.Struct("<4sHHQQI")
_ACK = 0x7FFF
_PACKET_LIMIT = 8 * 1024**3


class _InputMapping:
    """B1: one reused, grow-only input mapping per renderer process.

    Packets are written straight into it (WirePacket.write_into, on all cores).
    It is rewritten only after the renderer replied to the request that used it:
    write() marks it busy, release() follows a complete reply, and any failed
    exchange closes it (abandon) so a renderer that may still read it never sees
    new bytes. The renderer keeps its view between requests; ExternalBackend
    sends the release command before this mapping is freed or replaced, so the
    old section is gone on both sides before a new one is created. It is kept
    only while updates follow each other: the first payload-free exchange (the
    controller's idle poll) more than IDLE seconds after the last reply frees
    it, so nothing stays committed during a long render. While it lives it has
    25 % headroom (64 MiB steps; 1 MiB steps below 64 MiB); after SHRINK_AFTER
    consecutive packets that use at most a quarter of it (when it is larger than
    256 MiB) it is recreated at the needed size.
    """

    PREFIX = "Local\\CyclesHoudiniInputR-"
    STEP = 64 * 1024**2
    SHRINK_AFTER = 8
    IDLE = 2.0

    def __init__(self):
        self.view = None
        self.name = ""
        self.capacity = 0
        self.small = 0
        self.busy = False
        self.created = 0
        self.replied = 0.0

    def _create(self, capacity):
        name = self.PREFIX + str(os.getpid()) + "-" + uuid.uuid4().hex
        self.view = mmap.mmap(-1, capacity, tagname=name, access=mmap.ACCESS_WRITE)
        self.name, self.capacity = name, capacity
        self.created += 1

    def needs_replacement(self, size):
        """Whether the live mapping must go before a packet of this size (counts small packets)."""
        if self.view is None:
            return False
        if self.capacity > 4 * self.STEP and size <= self.capacity // 4:
            self.small += 1
        else:
            self.small = 0
        return size > self.capacity or self.small >= self.SHRINK_AFTER

    def idle(self):
        return self.view is not None and not self.busy and time.monotonic() - self.replied > self.IDLE

    def write(self, payload):
        if self.busy:
            raise RuntimeError("Cycles renderer input is still in use")
        size = len(payload)
        if self.view is not None and size > self.capacity:
            raise RuntimeError("Cycles renderer input was not released before it grows")
        if self.view is None:
            headroom = size + size // 4
            step = self.STEP if headroom >= self.STEP else 1024**2
            wanted = min(_PACKET_LIMIT, max(step, -(-headroom // step) * step))
            try:
                self._create(max(wanted, size))
            except OSError:
                if wanted <= size:
                    raise
                # Same refusal as before B1 when even the exact size cannot be mapped.
                self._create(size)
        self.busy = True
        try:
            if hasattr(payload, "write_into"):
                payload.write_into(self.view)
            else:
                self.view[:size] = payload
        except BaseException:
            self.close()
            raise
        return self.name

    def release(self):
        # Only the reply to a request that used the mapping restarts the idle time.
        if self.busy:
            self.busy = False
            self.replied = time.monotonic()

    def close(self):
        view, self.view = self.view, None
        self.name, self.capacity, self.small, self.busy = "", 0, 0, False
        if view is not None:
            view.close()


# Initialization, scene application, engine controls, output writing and
# closing can legitimately wait for non-interruptible engine work (kernel
# loading, uploads, BVH/light-tree builds, dicing). They keep the 60 s hang
# detection window, extended only while the worker is observably active.
# Polls and pure preview/memory queries stay strict.
_INIT_DISK = 13
_RELEASE_INPUT = 14
_LONG_COMMANDS = frozenset((1, 2, 4, 5, 6, 7, 10, 11, _INIT_DISK))
_LONG_CEILING_SECONDS = 3600.0

# Cycles reports these while it loads or compiles programs for the scene's features (scene.cpp, denoiser.cpp).
# On a GPU the first use of a feature (an Ambient Occlusion or Bevel node, motion blur, hair, ...) compiles
# programs once: measured 190-210 s on an RTX 3080 Ti; later renders reuse the driver's cache (0.2 s).
_KERNEL_LOADING = ("Loading render kernels", "Loading denoising kernels")
KERNEL_LOADING_NOTE = {
    "Loading render kernels": (
        ". Done once per new GPU feature (first Ambient Occlusion or Bevel node, motion blur); fast afterwards."
    ),
    "Loading denoising kernels": ". Done once per GPU; fast afterwards.",
}


def artist_message(message, pending_update):
    """(status text for the viewer, the engine's own text when it was extended, else None).

    The engine's loading text stays as it is, with one plain sentence added. A queued edit used to replace
    every renderer text with "Scene update queued", so a first-time kernel compile of several minutes
    showed nothing but a running clock.
    """
    message = str(message or "")
    loading = next((prefix for prefix in _KERNEL_LOADING if message.startswith(prefix)), None)
    if loading:
        return message + KERNEL_LOADING_NOTE[loading] + (" An edit is queued." if pending_update else ""), message
    return ("Scene update queued" if pending_update else message), None


_controllers = weakref.WeakSet()
_retiring = set()
_retirement_polling = False


def _poll_retirements():
    global _retirement_polling
    import hou

    for session in tuple(_retiring):
        session._poll_retirement()
    if not _retiring and _retirement_polling:
        hou.ui.removeEventLoopCallback(_poll_retirements)
        _retirement_polling = False


def _worker_log_directory():
    explicit = os.environ.get("CYCLES_HOUDINI_WORKER_LOG_DIR")
    if explicit:
        return Path(explicit)
    return Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"]) / "artifacts/workers"


class ExternalBackend:
    def __init__(self, capture, desired, *, disk_only=False, on_start=None):
        self.request = 0
        self.last_snapshot = desired
        self.metrics = dict(
            input_bytes=0, output_bytes=0, encode_seconds=0.0, decode_seconds=0.0, mapping_seconds=0.0, exchanges=0
        )
        self.last_exchange_profile = self.last_apply_profile = None
        # CYCLES_HOUDINI_WIRE_LEGACY=1: the previous transfer (bytes, one new mapping per request).
        self.input = None if os.environ.get("CYCLES_HOUDINI_WIRE_LEGACY") == "1" else _InputMapping()
        self.log_path = _worker_log_directory() / (uuid.uuid4().hex + ".log")
        executable = Path(os.environ["CYCLES_HOUDINI_HOST_ROOT"]) / "bin/ch_worker.exe"
        from ..core.sdk_dependencies import worker_environment, wait_for_worker_provider

        try:
            self.process = subprocess.Popen(
                [str(executable), "--parent-pid", str(os.getpid())],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                close_fds=True,
                shell=False,
                env=worker_environment(),
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if on_start is not None:
                on_start(self.process)
            self.log = WorkerLog(self.log_path, self.process.stderr)
            started = time.perf_counter()
            session_log.write(
                "worker process started", self.process.pid, worker_log=self.log_path.name, disk_only=disk_only
            )
            self.oidn_provider = wait_for_worker_provider(self.process)
            pid = self.process.pid
            self.watchdog = ExchangeWatchdog(
                self.process,
                ProcessActivity(self.process),
                on_expire=lambda: session_log.write("watchdog stopped the worker (no reply, no activity)", pid),
            )
            payload = _native.encode_config(capture, desired, os.environ["CYCLES_HOUDINI_RESOURCES"])
            self.initial = self.exchange(_INIT_DISK if disk_only else 1, payload, desired)
            session_log.write(
                "worker ready",
                pid,
                seconds=time.perf_counter() - started,
                devices=",".join(self.initial.get("status", {}).get("render_device_ids", ()) or ()),
                success=self.initial.get("success"),
            )
            try:
                self.check(self.initial)
            except RuntimeError as error:
                error.worker_devices = self.initial.get("devices", [])
                error.worker_startup_failure = str(error)
                raise
            self.gpu_scheduling = dict(applied=False, reason="disk render" if disk_only else "CPU render")
            if not disk_only and any(device != "CPU" for device in self.initial["status"].get("render_device_ids", ())):
                from .worker_scheduling import prioritize_interaction

                self.gpu_scheduling = prioritize_interaction(self.process)
        except BaseException as error:
            error.worker_log = str(self.log_path)
            process = getattr(self, "process", None)
            session_log.write(
                "worker start failed",
                process.pid if process is not None else None,
                error=error,
                exit_code=process.poll() if process is not None else None,
            )
            self.abort()
            raise

    @staticmethod
    def check(reply, *, allow_rejection=False):
        if not reply["success"] and not (allow_rejection and reply.get("update_rejected")):
            raise RuntimeError(reply["error"])
        return reply

    def read_exact(self, count):
        data = bytearray()
        while len(data) < count:
            part = self.process.stdout.read(count - len(data))
            if not part:
                raise RuntimeError(
                    "Cycles worker disconnected (exit " + str(self.process.poll()) + "); log: " + str(self.log_path)
                )
            data.extend(part)
        return bytes(data)

    def exchange(self, command, payload=b"", desired=None):
        exchange_started = time.perf_counter()
        before_mapping, before_decode = self.metrics["mapping_seconds"], self.metrics["decode_seconds"]
        if self.input is not None and command != _RELEASE_INPUT:
            # Free or replace the reused input on both sides first (renderer view, then ours).
            if self.input.needs_replacement(len(payload)) if len(payload) else self.input.idle():
                self._release_input()
        self.request += 1
        request = self.request
        source = target = None
        self.watchdog.arm(ceiling=_LONG_CEILING_SECONDS if command in _LONG_COMMANDS else None)
        try:
            if len(payload) > _PACKET_LIMIT:
                raise ValueError("Worker packet exceeds allocation limit")
            name = ""
            if len(payload):
                started = time.perf_counter()
                if self.input is not None:
                    name = self.input.write(payload)
                else:
                    name = "Local\\CyclesHoudiniInput-" + str(os.getpid()) + "-" + uuid.uuid4().hex
                    source = mmap.mmap(-1, len(payload), tagname=name, access=mmap.ACCESS_WRITE)
                    source.write(payload)
                self.metrics["mapping_seconds"] += time.perf_counter() - started
            encoded_name = name.encode("utf-8")
            self.process.stdin.write(_HEADER.pack(b"CHWP", 1, command, request, len(payload), len(encoded_name)))
            self.process.stdin.write(encoded_name)
            self.process.stdin.flush()
            magic, version, reply_command, reply_id, size, name_length = _HEADER.unpack(self.read_exact(_HEADER.size))
            if (
                magic != b"CHWP"
                or version != 1
                or reply_command != command | 0x8000
                or reply_id != request
                or not 0 < size <= 8 * 1024**3
                or not 0 < name_length <= 256
            ):
                raise RuntimeError("Invalid worker reply header")
            reply_name = self.read_exact(name_length).decode("utf-8")
            if not reply_name.startswith("Local\\CyclesHoudiniReply-" + str(self.process.pid) + "-"):
                raise RuntimeError("Invalid worker output mapping identity")
            target = mmap.mmap(-1, size, tagname=reply_name, access=mmap.ACCESS_READ)
            started = time.perf_counter()
            reply = _native.decode_reply(target, desired if desired is not None else self.last_snapshot)
            self.metrics["decode_seconds"] += time.perf_counter() - started
            if self.input is not None:
                # The renderer replies only after it finished reading the input.
                self.input.release()
            # The renderer can release output memory only after complete decode.
            # Input remains mapped until this commit/rejection ACK proves consumption.
            self.process.stdin.write(_HEADER.pack(b"CHWP", 1, _ACK, request, 0, 0))
            self.process.stdin.flush()
            self.metrics["input_bytes"] += len(payload)
            self.metrics["output_bytes"] += size
            self.metrics["exchanges"] += 1
            self.last_exchange_profile = dict(
                request=request,
                command=command,
                input_bytes=len(payload),
                output_bytes=size,
                exchange_seconds=time.perf_counter() - exchange_started,
                input_mapping_seconds=self.metrics["mapping_seconds"] - before_mapping,
                reply_decode_seconds=self.metrics["decode_seconds"] - before_decode,
            )
            self.metrics["deadline_extensions"] = self.watchdog.extensions
            reply["transport"] = dict(self.metrics)
            seconds = self.last_exchange_profile["exchange_seconds"]
            if seconds >= session_log.SLOW_EXCHANGE_SECONDS:
                session_log.write(
                    "slow reply",
                    self.process.pid,
                    command=command,
                    seconds=seconds,
                    extensions=self.watchdog.extensions,
                )
            return reply
        except OSError as error:
            # Windows may report EINVAL on a pipe after the renderer exits.
            # Keep the failing process and its log visible in the ROP error.
            exit_code = self.process.poll()
            session_log.write("worker connection failed", self.process.pid, command=command, exit_code=exit_code)
            context = f"exited with code {exit_code}" if exit_code is not None else "communication failed"
            raise RuntimeError(f"Cycles worker {self.process.pid} {context}: {error}. Log: {self.log_path}") from error
        finally:
            self.watchdog.disarm()
            if self.input is not None and self.input.busy:
                # Failed or interrupted exchange: never rewrite a mapping the renderer may still read.
                self.input.close()
            if target is not None:
                target.close()
            if source is not None:
                source.close()

    def _release_input(self):
        try:
            if self.process.poll() is None:
                self.check(self.exchange(_RELEASE_INPUT))
                self.metrics["input_releases"] = self.metrics.get("input_releases", 0) + 1
        finally:
            self.input.close()

    def apply(self, desired, acknowledged):
        started = time.perf_counter()
        payload = _native.encode_snapshot(desired, acknowledged)
        encode_seconds = time.perf_counter() - started
        self.metrics["encode_seconds"] += encode_seconds
        reply = self.check(self.exchange(2, payload, desired), allow_rejection=True)
        if (
            not reply["success"]
            and reply.get("update_rejected")
            and str(reply.get("error", "")).startswith("Root pose update refused")
        ):
            # Protocol 91: the renderer could not derive the root poses (it keeps
            # its scene). Send the same update with every instance pose instead.
            self.metrics["root_pose_fallbacks"] = self.metrics.get("root_pose_fallbacks", 0) + 1
            payload = _native.encode_snapshot(desired, acknowledged, 1, 0)
            reply = self.check(self.exchange(2, payload, desired), allow_rejection=True)
        self.last_apply_profile = dict(self.last_exchange_profile, snapshot_encode_seconds=encode_seconds)
        if reply["success"]:
            self.last_snapshot = desired
        return reply

    def poll(self):
        return self.check(self.exchange(3))

    def control(self, command, value=None):
        if command == "color":
            return self.check(self.exchange(10, value[1]))
        command_id, payload = {
            "pause": (4, bytes([bool(value)])),
            "cancel": (5, b""),
            "memory": (8, b""),
            "write": (11, _native.encode_output(*value) if command == "write" else b""),
            "preview": (
                12,
                _native.encode_preview(*((value, False) if isinstance(value, str) else value))
                if command == "preview"
                else b"",
            ),
        }[command]
        reply = self.exchange(command_id, payload)
        # The controller reports output rejection to the saving caller without
        # tearing down a healthy renderer. Transport failures still raise here.
        return reply if command == "write" else self.check(reply, allow_rejection=command == "preview")

    def abort(self):
        watchdog = getattr(self, "watchdog", None)
        if watchdog is not None:
            watchdog.close()
        process = getattr(self, "process", None)
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5.0)
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()
        mapping = getattr(self, "input", None)
        if mapping is not None:
            mapping.close()
        if hasattr(self, "log"):
            self.log.finish()
        elif process is not None:
            process.stderr.close()

    def close(self):
        status = None
        try:
            if self.process.poll() is None:
                reply = self.check(self.exchange(7))
                status = reply["status"]
                self.process.wait(timeout=5.0)
        finally:
            self.abort()
        return status


class QueuedController:
    def __init__(self, capture, *, disk_only=False, startup_snapshot=None):
        self.startup_snapshot = startup_snapshot
        self.starting_process = None
        self.backend_factory = partial(ExternalBackend, disk_only=True) if disk_only else ExternalBackend
        self.capture = capture
        self.condition = threading.Condition()
        self.pending = self.backend = self.acknowledged = None
        self.rejected_scene = None
        self.scene_repair_retries = 0
        self.navigation = self.pending_navigation = None
        self.preparing = None
        self.preparation_epoch = self.applied_roots_revision = self.previewed_submission = 0
        self.preparation = ThreadPoolExecutor(max_workers=1, thread_name_prefix="Cycles scene preparation")
        self.controls = []
        self.preview_pass = "combined"
        self.focus_requested = False
        self.preview_submission = 0
        self.requested = self.committed = self.coalesced = 0
        self.required_scene = self.completed_scene = 0
        self.cancelled_through = 0
        self.rejection = None
        self.capture_rejection = None
        self.latest_capture = 0
        self.epoch = 0
        self.physical = 0
        self.image = None
        self.image_generation = self.image_sequence = 0
        self.frame_identity = None
        self.focus_image = self.focus_identity = None
        self.feedback_viewers = set()
        self.blocking_waiters = 0
        self.frame_wakeups = {}
        self.presented_generation = 0
        self.presented_revision = None
        self.feedback_deadline = 0.0
        self.first_frame_poll_deadline = 0.0
        self.first_frame_fast_polls = 0
        self.backend_memory = None
        # Before the lazy worker exists, no renderer collector is active yet.
        # Keep that query explicit instead of returning an absent enabled flag.
        self.cached = dict(
            status=_native.status(capture),
            diagnostics={"enabled": False},
            texture_inputs=[],
            textures_changed=False,
            renderer_pid=0,
        )
        self.closed = self.closing = False
        self.shutdown_error = ""
        self.failed_worker_log = None
        # close() stopped a worker that did not finish its shutdown in time.
        self.forced_stop = False
        self.color_identity = None
        # Session log: why the next worker starts, and the last logged stage (state, text without digits).
        self.start_reason = "first render" if startup_snapshot is None else "start-up (device prepared early)"
        self._logged_stage = None
        self._logged_stage_at = time.monotonic()
        session_log.write("render session created", disk_only=disk_only)
        self.events = []
        self.control_rejections = []
        self.pose_preparation_refreshes = 0
        self.thread = threading.Thread(target=self.run, name="Cycles queued controller", daemon=True)
        _controllers.add(self)
        self.thread.start()

    def reserve(self):
        with self.condition:
            if self.closing:
                raise RuntimeError("Queued Cycles session is closed")
            self.requested += 1
            self.rejection = None
            if self.cached["status"]["state"] in ("failed", "cancelled"):
                self.failed_worker_log = None
                self.cached["status"].update(state="rendering", error="", message="Renderer recovery queued")
            return self.requested

    def submit(self, desired, submission=None):
        with self.condition:
            if submission is None:
                submission = self.reserve()
            if self.closing or submission <= self.cancelled_through:
                return submission
            if self.pending is not None:
                self.coalesced += 1
            self.rejected_scene = None
            if self.capture_rejection is not None and submission >= self.capture_rejection[0]:
                self.capture_rejection = None
            self.pending = (submission, desired, time.perf_counter())
            self.condition.notify()
            return submission

    def _retry_rejected_scene(self):
        # Called under condition. Only input newer than the rejected apply can
        # repair it; reusing the same navigation would loop on invalid assets.
        rejected = self.rejected_scene
        if (
            rejected is not None
            and self.pending is None
            and self.navigation is not None
            and self.navigation[0] > rejected[3]
            and rejected[0] >= self.required_scene
            and rejected[0] > self.completed_scene
        ):
            self.pending, self.rejected_scene = rejected[:3], None
            self.scene_repair_retries += 1

    def submit_navigation(self, navigation, submission, *, final=False, preview=True):
        with self.condition:
            if self.closing or submission <= self.cancelled_through:
                return
            self.navigation = (submission, navigation, _native.navigation_info(navigation)["root_revision"])
            if preview:
                if self.pending_navigation is not None:
                    self.coalesced += 1
                self.pending_navigation = (submission, navigation, time.perf_counter(), final)
            elif self.pending_navigation is not None and self.pending_navigation[0] <= submission:
                self.pending_navigation = None
            # A camera preview can repair an iris without applying geometry
            # from a rejected complete scene. Retry that owned complete target
            # with the new navigation before acknowledging its scene obligation.
            # A newer source requirement must wait for its own full capture.
            self._retry_rejected_scene()
            self.condition.notify()

    def require_scene(self, submission, *, capture=True):
        with self.condition:
            self.required_scene = max(self.required_scene, submission)
            if capture:
                self.latest_capture = max(self.latest_capture, submission)
            if capture and self.capture_rejection and submission > self.capture_rejection[0]:
                self.capture_rejection = None

    def reject_capture(self, submission, error):
        with self.condition:
            if not self.closing and submission > self.cancelled_through and submission >= self.latest_capture:
                # Navigation reserves newer logical submissions but cannot
                # repair a failed source capture. Keep its terminal diagnostic
                # until complete source input arrives or Stop retires it.
                self.capture_rejection = (submission, error)
                self.condition.notify()

    def control(self, command, value=None):
        event = threading.Event()
        event.error = ""
        with self.condition:
            if self.closing:
                raise RuntimeError("Queued Cycles session is closed")
            if command == "cancel":
                self.rejection = None
                self.capture_rejection = None
                # Stop retires only submissions that already exist. A later
                # Render may be queued before the backend acknowledges Stop.
                self.cancelled_through = self.requested
                self.pending = None
                self.rejected_scene = None
                self.navigation = self.pending_navigation = None
                # A pass preference for a discarded scene cannot be applied to
                # the retained backend's older pass list.
                retained = []
                for queued in self.controls:
                    if queued[0] == "preview" and queued[3] > self.completed_scene:
                        queued[2].set()
                    else:
                        retained.append(queued)
                self.controls = retained
                if self.preview_submission > self.committed:
                    self.preview_pass, self.preview_submission = "combined", self.committed
                    self.focus_requested = False
                    self.focus_image = self.focus_identity = None
            if command == "preview":
                self.preview_pass = value
                self.focus_requested = not isinstance(value, str) and bool(value[1])
                if not self.focus_requested:
                    self.focus_image = self.focus_identity = None
                self.preview_submission = self.requested
                # Interactive pass selection is replaceable and bounded.
                retained = []
                for queued in self.controls:
                    if queued[0] == "preview":
                        queued[2].set()
                    else:
                        retained.append(queued)
                self.controls = retained
            if command == "color":
                self.color_identity = value[0]
                retained = []
                for queued in self.controls:
                    if queued[0] == "color":
                        queued[2].set()
                    else:
                        retained.append(queued)
                self.controls = retained
            # Scene-dependent preview commands wait for the scene current at
            # submission, including when later snapshots coalesce it away.
            required = self.required_scene if command == "preview" else 0
            self.controls.append((command, value, event, required))
            self.condition.notify()
        return event

    def set_feedback(self, viewer, enabled):
        with self.condition:
            previous = bool(self.feedback_viewers)
            if enabled:
                self.feedback_viewers.add(viewer)
            else:
                self.feedback_viewers.discard(viewer)
            if previous != bool(self.feedback_viewers):
                self.condition.notify()

    def presented(self, generation, render_revision=None):
        with self.condition:
            revision = self.physical if render_revision is None else render_revision
            if generation == self.committed and revision == self.physical:
                self.presented_generation = generation
                self.presented_revision = (self.epoch, revision)
                self.condition.notify()

    def set_frame_wakeup(self, viewer, callback):
        with self.condition:
            if callback is None or self.closing:
                self.frame_wakeups.pop(viewer, None)
            else:
                self.frame_wakeups[viewer] = callback

    def feedback_waiting(self):
        # Called under the controller lock. Coalesce desired edits while the
        # current accepted scene produces one visible preview. A hidden/broken
        # viewer cannot indefinitely block updates; controls bypass this gate.
        return bool(
            self.feedback_viewers
            and (self.pending is not None or self.pending_navigation is not None or self.preparing is not None)
            and self.committed
            and self.presented_revision != (self.epoch, self.physical)
            and self.cached["status"]["state"] == "rendering"
            and time.perf_counter() < self.feedback_deadline
        )

    def accept(self, reply, submission=None):
        if not reply["success"]:
            raise RuntimeError(reply["error"])
        if reply["status"]["state"] == "failed":
            # A successful command/transport reply does not mean the rendering
            # session is healthy. Runtime device/kernel failures arrive through
            # status as well as through exceptions; neither is a safe rejection.
            raise RuntimeError(reply["status"]["error"] or reply["status"]["message"] or "Renderer reported failure")
        with self.condition:
            previous_sequence = self.image_sequence
            if submission is not None:
                self.committed = submission
                if self.physical != reply["status"]["generation"]:
                    # Probe briefly for the first image of an edit. A fixed
                    # 16 ms poll otherwise hides an already finished preview.
                    # Controls still wake this interruptible condition wait.
                    self.first_frame_poll_deadline = time.perf_counter() + 0.08
                    self.focus_image = self.focus_identity = None
                self.physical = reply["status"]["generation"]
                self.feedback_deadline = time.perf_counter() + 1.0
                # A retained/no-op scene has no new physical image to send.
                # Associate the already-owned image with this request only
                # when it belongs to this exact backend and physical revision.
                if (
                    self.image is not None
                    and self.frame_identity is not None
                    and self.frame_identity[:2] == (self.epoch, self.physical)
                    and self.image_generation != submission
                ):
                    self.image_generation = submission
                    self.image_sequence += 1
                    self.frame_identity = (*self.frame_identity[:3], submission)
            self.cached = {k: v for k, v in reply.items() if k not in ("frame", "focus_frame")}
            if reply.get("process_memory"):
                self.backend_memory = json.loads(reply["process_memory"])
            image = reply.get("frame")
            if image is not None:
                info = _native.frame_info(image)
                if info["generation"] == self.physical:
                    identity = (self.epoch, info["generation"], info["sequence"], self.committed)
                    if identity != self.frame_identity:
                        self.frame_identity = identity
                        self.image = image
                        self.image_generation = self.committed
                        self.image_sequence += 1
                        if (
                            self.feedback_viewers
                            and self.presented_revision != (self.epoch, self.physical)
                            and os.environ.get("CYCLES_HOUDINI_FEEDBACK_GATE") != "paint"
                        ):
                            # The current revision's first picture is here and goes to the viewer's
                            # conversion now: let the next coalesced edit start rendering while it is
                            # converted and painted. One update stays in flight; the viewer keeps only the
                            # newest picture. CYCLES_HOUDINI_FEEDBACK_GATE=paint: wait for the paint.
                            self.presented_generation = self.committed
                            self.presented_revision = (self.epoch, self.physical)
                            self.condition.notify()
            focus = reply.get("focus_frame")
            if focus is not None and self.focus_requested:
                info = _native.frame_info(focus)
                if info["generation"] == self.physical:
                    self.focus_image = focus
                    self.focus_identity = self.epoch, self.physical
            notify = tuple(self.frame_wakeups.values()) if self.image_sequence != previous_sequence else ()
        self._log_stage(reply)
        # Subscribers retain immutable frames and enqueue bounded conversion/
        # GUI notifications. Never enter widgets or host capture from this
        # thread, and never run callbacks under the controller lock.
        for callback in notify:
            callback()

    def _log_stage(self, reply):
        status = reply.get("status") or {}
        key = session_log.stage_key(status.get("state"), status.get("message"))
        previous = getattr(self, "_logged_stage", None)
        if key == previous:
            return
        now = time.monotonic()
        held = now - getattr(self, "_logged_stage_at", now)
        self._logged_stage, self._logged_stage_at = key, now
        session_log.write(
            f"{key[0]}: {status.get('message', '')}",
            reply.get("renderer_pid"),
            previous_stage_seconds=held if previous is not None else None,
            error=status.get("error") or None,
        )

    def _awaiting_first_frame(self):
        """Called under condition; never change normal progressive pacing."""
        return bool(
            (self.feedback_viewers or self.blocking_waiters)
            and self.cached["status"]["state"] == "rendering"
            and time.perf_counter() < self.first_frame_poll_deadline
            and (self.frame_identity is None or self.frame_identity[:2] != (self.epoch, self.physical))
        )

    def reject(self, reply, submission):
        # The engine explicitly guarantees that no scene transaction began.
        # A new render-color policy carried by the candidate remains active
        # (it is the host's current policy); the retained scene then waits for
        # the next accepted update. Refresh diagnostics without acknowledging
        # the candidate. Polling the old render may continue, but must not
        # erase the failed-edit message.
        self.accept(dict(reply, success=True))
        with self.condition:
            if submission == self.requested and submission > self.cancelled_through:
                self.rejection = (submission, reply["error"])

    def _preparation_finished(self, future):
        with self.condition:
            self.condition.notify()

    def _record_control_rejection(self, command, value, required, reply):
        with self.condition:
            self.control_rejections.append(
                dict(
                    command=command,
                    value=value,
                    required_scene=required,
                    accepted_scene=self.completed_scene,
                    error=reply["error"],
                    action="Keeping the current display pass",
                )
            )
            self.control_rejections = self.control_rejections[-64:]

    @staticmethod
    def _prepare_owned(target, navigation, scene):
        started = time.perf_counter()
        result = _native.navigation_snapshot(target, navigation) if navigation is not None else None
        return (target if result is None and scene else result), time.perf_counter() - started

    def _start_preparation(self, defer_poses=False):
        # Under the controller lock. One active owned preparation is bounded;
        # new complete scenes/navigation replace only pending desired input.
        if self.preparing is not None or (defer_poses and self.pending is None):
            return
        latest = self.navigation
        if self.pending is not None:
            source, target, submitted = self.pending
            self.pending = None
            scene = True
        elif latest is not None and latest[2] != self.applied_roots_revision and self.acknowledged is not None:
            source, target, submitted = latest[0], self.acknowledged, time.perf_counter()
            scene = False
        else:
            return
        navigation = latest[1] if latest is not None else None
        revision = latest[2] if latest is not None else 0
        future = self.preparation.submit(self._prepare_owned, target, navigation, scene)
        self.preparing = dict(
            future=future,
            source=source,
            target=target,
            submitted=submitted,
            scene=scene,
            revision=revision,
            epoch=self.preparation_epoch,
            camera_opportunities=0,
        )
        future.add_done_callback(self._preparation_finished)

    def _can_refresh_prepared_poses(self):
        # Under the controller lock. Failed/running jobs and complete source
        # snapshots must reach the existing commit/error path unchanged.
        prepared = self.preparing
        if prepared is None or prepared["scene"] or self.pending is not None or self.navigation is None:
            return False
        future = prepared["future"]
        return (
            future.done()
            and not future.cancelled()
            and future.exception() is None
            and self.navigation[2] != prepared["revision"]
        )

    def _observe_start(self, process):
        with self.condition:
            if self.closing:
                process.kill()
            else:
                self.starting_process = process

    def _launch_backend(self, desired, *, standby=False):
        with self.condition:
            if self.closing:
                return None
        backend = None
        try:
            backend = self.backend_factory(self.capture, desired, on_start=self._observe_start)
        finally:
            with self.condition:
                self.starting_process = None
                closing = self.closing
                # Publish a real backend in the same turn that clears its
                # starting state. Close always knows which process owns it.
                if not standby and not closing:
                    self.backend = backend
        if closing and backend is not None:
            backend.abort()
            return None
        return backend

    def run(self):
        # Start the device concurrently with host acquisition. The controller
        # alone owns this process, including close-before-first-submission.
        standby = None
        startup_error = None
        if self.startup_snapshot is not None:
            try:
                session_log.write("starting a worker", reason=getattr(self, "start_reason", None))
                standby = self._launch_backend(self.startup_snapshot, standby=True)
            except Exception as error:
                # Deliver device/startup errors to the first submission. The
                # ordinary recovery path can retry on a later submission.
                startup_error = error
        while True:
            retired_preparation = None
            with self.condition:
                if self.closing:
                    break
                control = work = navigation_work = None
                eligible = next((i for i, c in enumerate(self.controls) if c[3] <= self.completed_scene), None)
                if eligible is not None:
                    control = self.controls.pop(eligible)
                elif self.controls and (
                    self.controls[0][3] <= self.cancelled_through or self.rejection or self.capture_rejection
                ):
                    control = self.controls.pop(0)
                waiting = self.feedback_waiting()
                if (
                    control is None
                    and waiting
                    and os.environ.get("CYCLES_HOUDINI_POSE_REFRESH") == "1"
                    and self._can_refresh_prepared_poses()
                ):
                    # Old behaviour (switch): discard a finished pose preparation
                    # while the current image is awaited and prepare fresher poses.
                    retired_preparation, self.preparing = self.preparing, None
                    self.pose_preparation_refreshes += 1
                if control is None and not waiting:
                    ready = self.preparing is not None and self.preparing["future"].done()
                    camera_limit = 1
                    if (
                        ready
                        and self.required_scene <= self.preparing["source"]
                        and (self.navigation[2] if self.navigation is not None else 0) == self.preparing["revision"]
                    ):
                        camera_limit = 2
                    if self.pending_navigation is not None and (
                        not ready or self.preparing["camera_opportunities"] < camera_limit
                    ):
                        # Once heavy work is ready, allow a camera opportunity
                        # plus one bounded catch-up for input arriving during it.
                        # Spend the extra catch-up only while this prepared
                        # object/geometry state is current. If newer scene/roots
                        # are pending, one camera turn lets that work progress.
                        # Earlier cameras during preparation do not spend the
                        # budget. A camera flood cannot withhold geometry forever.
                        navigation_work, self.pending_navigation = self.pending_navigation, None
                        if ready:
                            self.preparing["camera_opportunities"] += 1
                    elif ready:
                        work, self.preparing = self.preparing, None
                if work is None:
                    # Pose-only preparations wait for the gate: a root-pose
                    # preparation is cheap, so the newest poses are taken when
                    # the current image is out instead of being prepared twice.
                    self._start_preparation(
                        defer_poses=waiting and os.environ.get("CYCLES_HOUDINI_POSE_REFRESH") != "1"
                    )
                if control is None and work is None and navigation_work is None:
                    # A trivial preparation can finish before add_done_callback
                    # returns. Its notification then precedes this wait.
                    if not waiting and self.preparing is not None and self.preparing["future"].done():
                        continue
                    self.condition.wait(
                        0.002 if self._awaiting_first_frame() else 0.016 if self.feedback_viewers else 0.05
                    )
                    # Return to selection after a wakeup. Polling below keeps
                    # progressive output moving while owned work is running.
                    if self.closing:
                        continue
                    if any(c[3] <= self.completed_scene for c in self.controls):
                        continue
                    if not self.feedback_waiting() and (
                        self.pending_navigation is not None
                        or (self.preparing is not None and self.preparing["future"].done())
                    ):
                        continue
            # Snapshot retirement may free a large owned object map. Keep it
            # off both the GUI thread and the lock used by GUI status reads.
            retired_preparation = None
            try:
                if work is not None or navigation_work is not None:
                    navigation_started = time.perf_counter()
                    preparation_seconds = 0.0
                    source_submission = 0
                    newer_scene = changed_roots = False
                    if navigation_work is not None:
                        submission, navigation, submitted, final = navigation_work
                        with self.condition:
                            base = self.acknowledged
                            obsolete = submission <= self.cancelled_through or submission < self.committed
                        if obsolete or base is None:
                            continue
                        desired = _native.navigation_snapshot(base, navigation, False, True)
                        if desired is None:
                            if final and self.backend is not None:
                                reply = self.backend.poll()
                                if reply["status"]["state"] == "cancelled":
                                    # Stop (including a rejected host capture)
                                    # retires tracing, not the accepted scene.
                                    # An identical subsequent request must enter
                                    # apply so the engine can restart it.
                                    desired = base
                                else:
                                    self.accept(reply, submission)
                            if desired is None:
                                continue
                        kind = "camera"
                    else:
                        desired, preparation_seconds = work["future"].result()
                        with self.condition:
                            latest = self.navigation
                            newer_scene = self.pending is not None and (
                                not work["scene"] or self.pending[0] > work["source"]
                            )
                            obsolete = (
                                work["epoch"] != self.preparation_epoch or work["source"] <= self.cancelled_through
                            )
                            changed_roots = (latest[2] if latest is not None else 0) != work["revision"]
                        if obsolete:
                            continue
                        # This is the newest prepared state, even if more input
                        # arrived during its preparation. Render it as a preview
                        # and leave the newest pending state intact. Repeatedly
                        # abandoning it prevents any object/geometry feedback.
                        if not work["scene"]:
                            if self.acknowledged is None or not _native.navigation_basis_equal(
                                work["target"], self.acknowledged
                            ):
                                continue
                            if desired is None:
                                self.applied_roots_revision = work["revision"]
                                continue
                        submission, submitted = work["source"], work["submitted"]
                        kind = "scene" if work["scene"] else "objects"
                        source_submission = submission if work["scene"] else 0
                        if latest is not None and _native.navigation_context_equal(desired, latest[1]):
                            nav_submission, navigation, _ = latest
                            # Reapply only camera/crop after the background job.
                            # Object origins are already resolved. A newer root
                            # revision remains queued for the next prepared image.
                            retain = work["scene"] and self.previewed_submission >= source_submission
                            rebased = _native.navigation_snapshot(desired, navigation, retain, True)
                            if rebased is not None:
                                desired = rebased
                            submission = max(submission, nav_submission)
                    navigation_seconds = time.perf_counter() - navigation_started
                    with self.condition:
                        desired_color_identity = _native.snapshot_color_identity(desired)
                        obsolete_color = (
                            self.color_identity is not None and self.color_identity != desired_color_identity
                        )
                        # A full capture can queue a speculative camera preview
                        # from the accepted scene's older color policy. Retire
                        # that preview while its new-color capture finishes;
                        # it must not reject the shared source submission.
                        speculative_preview = (
                            self.required_scene >= submission
                            and self.required_scene > self.completed_scene
                            and (
                                (navigation_work is not None and not navigation_work[3])
                                or (work is not None and not work["scene"])
                            )
                        )
                        if obsolete_color and not speculative_preview and submission == self.requested:
                            self.rejection = (
                                submission,
                                "Captured working color policy was superseded; recapture scene "
                                f"(queued {kind}, captured={desired_color_identity}, expected={self.color_identity}, "
                                f"submission={submission}, required_scene={self.required_scene}, "
                                f"completed_scene={self.completed_scene}, navigation={navigation_work is not None})",
                            )
                    if obsolete_color:
                        continue
                    if self.backend is None:
                        with self.condition:
                            self.backend_memory = None
                        self.epoch += 1
                        if startup_error is not None:
                            failure, startup_error = startup_error, None
                            raise failure
                        if standby is not None:
                            if _native.snapshot_color_identity(
                                self.startup_snapshot
                            ) == _native.snapshot_color_identity(desired):
                                self.backend = standby
                            else:
                                session_log.write(
                                    "start-up worker discarded: the first scene has other colour settings",
                                    getattr(getattr(standby, "process", None), "pid", None),
                                )
                                standby.close()
                                self.start_reason = "replaces the discarded start-up worker"
                            standby = None
                        elif getattr(self, "start_reason", "").startswith("start-up"):
                            self.start_reason = "first render (start-up worker unavailable)"
                        self.startup_snapshot = None
                        if self.backend is None:
                            session_log.write("starting a worker", reason=getattr(self, "start_reason", None))
                            if self._launch_backend(desired) is None:
                                continue
                    started = time.perf_counter()
                    reply = self.backend.apply(desired, self.acknowledged)
                    rejected = not reply["success"] and reply.get("update_rejected", False)
                    if rejected:
                        if kind == "scene":
                            with self.condition:
                                if not newer_scene and source_submission >= self.required_scene:
                                    self.rejected_scene = (source_submission, desired, submitted, submission)
                                    self._retry_rejected_scene()
                        current_rejection = not (newer_scene or changed_roots)
                        if current_rejection:
                            self.reject(reply, submission)
                        else:
                            # A safe pre-transaction rejection from older work
                            # cannot turn a newer, repaired desired state Failed.
                            self.accept(dict(reply, success=True))
                    else:
                        first_scene = self.acknowledged is None
                        self.acknowledged = desired
                        # Accept the apply reply itself: the worker hands each
                        # frame over exactly once, and a short render can finish
                        # before this reply is built. Replacing the reply with
                        # the preview control's reply used to drop that frame,
                        # leaving a complete render without an image.
                        self.accept(reply, submission)
                        if first_scene:
                            with self.condition:
                                preview = self.preview_pass if self.preview_submission <= submission else "combined"
                            preview_reply = self.backend.control("preview", preview)
                            if not preview_reply["success"] and preview_reply.get("update_rejected"):
                                self._record_control_rejection("preview", preview, source_submission, preview_reply)
                                preview_reply = dict(preview_reply, success=True)
                            self.accept(preview_reply)
                        with self.condition:
                            if kind == "scene":
                                self.completed_scene = max(self.completed_scene, source_submission)
                                if self.rejected_scene is not None and self.rejected_scene[0] <= self.completed_scene:
                                    self.rejected_scene = None
                            else:
                                self.previewed_submission = max(self.previewed_submission, submission)
                            if kind != "camera":
                                self.applied_roots_revision = work["revision"]
                    with self.condition:
                        self.events.append(
                            dict(
                                submission=submission,
                                apply_seconds=time.perf_counter() - started,
                                submission_to_commit_seconds=time.perf_counter() - submitted,
                                navigation_preview=kind != "scene",
                                navigation_kind=kind,
                                scene_submission=source_submission,
                                navigation_seconds=navigation_seconds,
                                preparation_seconds=preparation_seconds,
                                physical_generation=self.physical,
                                intermediate_preview=newer_scene or changed_roots,
                                prepared_root_revision=work["revision"] if work is not None else None,
                                camera_opportunities_before_commit=work["camera_opportunities"]
                                if work is not None
                                else None,
                                request_profile=getattr(self.backend, "last_apply_profile", None),
                                accepted=not rejected,
                                error=reply.get("error", "") if rejected else "",
                            )
                        )
                        self.events = self.events[-256:]
                elif control is not None:
                    command, value, event, required = control
                    if command == "preview" and required > self.completed_scene:
                        event.set()
                        continue
                    if self.backend is not None:
                        reply = self.backend.control(command, value)
                        if (
                            (command == "write" and reply["status"]["state"] != "failed")
                            or (command == "preview" and reply.get("update_rejected"))
                        ) and not reply["success"]:
                            event.error = reply["error"]
                            if command == "preview":
                                self._record_control_rejection(command, value, required, reply)
                            self.accept(dict(reply, success=True))
                        else:
                            self.accept(reply)
                    elif command == "write":
                        event.error = "No rendered frame is available to save"
                    event.set()
                elif self.backend is not None:
                    with self.condition:
                        if self._awaiting_first_frame():
                            self.first_frame_fast_polls += 1
                    self.accept(self.backend.poll())
            except BaseException as error:
                if self.closing and control is None and self.backend is None:
                    continue  # Closing interrupted a launch before any control command was sent.
                failed_backend, self.backend = self.backend, None
                failed_process = getattr(failed_backend, "process", None)
                session_log.write(
                    "renderer failed; the next edit or Render starts a new worker",
                    failed_process.pid if failed_process is not None else None,
                    error=error,
                    exit_code=failed_process.poll() if failed_process is not None else None,
                    worker_log=getattr(getattr(failed_backend, "log_path", None), "name", None),
                )
                self.start_reason = "recovery after failure: " + str(error)[:300]
                closed_status = None
                if failed_backend is not None:
                    try:
                        closed_status = failed_backend.close()
                    except BaseException:
                        pass
                self.acknowledged = None
                with self.condition:
                    log_path = getattr(failed_backend, "log_path", None)
                    self.failed_worker_log = (
                        str(log_path) if log_path is not None else getattr(error, "worker_log", None)
                    )
                    if hasattr(error, "worker_startup_failure"):
                        self.cached["devices"] = error.worker_devices
                        self.cached["worker_startup_failure"] = error.worker_startup_failure
                    self.rejection = None
                    self.capture_rejection = None
                    self.rejected_scene = None
                    self.preparation_epoch += 1
                    self.navigation = self.pending_navigation = None
                    # When teardown reports its final state, do not continue
                    # displaying GPU/geometry allocation from the old render.
                    self.cached["status"] = dict(
                        closed_status or self.cached["status"], state="failed", error=str(error), message=str(error)
                    )
                    self.condition.notify_all()
                if control is not None:
                    control[2].set()
        # Pure owned preparation may outlive Stop/close. Join only on this
        # controller thread; no GUI or Houdini object is accessed by the job.
        self.preparation.shutdown(wait=True)
        self.preparing = None
        if standby is not None:
            standby.abort()
        if self.backend is not None:
            try:
                closed_status = self.backend.close()
                if closed_status is not None:
                    with self.condition:
                        self.cached["status"] = closed_status
            except BaseException as error:
                with self.condition:
                    # After close() killed the worker, the interrupted shutdown
                    # exchange is our own stop, not a renderer failure.
                    if not self.forced_stop:
                        self.cached["status"]["error"] = str(error)
                        self.shutdown_error = str(error)
        self.acknowledged = None
        self.backend = None
        with self.condition:
            self.closed = True
            if self.shutdown_error:
                self.cached["status"]["state"] = "failed"
            elif self.cached["status"]["state"] != "closed":
                # A failed backend has already been destroyed or reaped.
                self.cached["status"].update(state="closed", owned_geometry_bytes=0, device_allocation_bytes=0)

    def status(self):
        with self.condition:
            result = dict(self.cached["status"])
            rejection = self.capture_rejection or self.rejection
            result.update(
                backend_state=result["state"],
                backend_alive=self.backend is not None,
                update_rejected=bool(rejection) and not self.closed,
            )
            if result["update_rejected"]:
                result.update(state="failed", error=rejection[1], message="Scene update rejected: " + rejection[1])
            result.update(
                generation=self.requested,
                committed_generation=self.committed,
                render_revision=self.physical,
                requested_scene_submission=self.required_scene,
                committed_scene_submission=self.completed_scene,
                pending_update=(
                    self.requested > max(self.committed, self.cancelled_through)
                    or self.required_scene > max(self.completed_scene, self.cancelled_through)
                )
                and result["state"] not in ("failed", "closed"),
                coalesced_updates=self.coalesced,
                execution="worker",
                renderer_pid=self.cached["renderer_pid"],
                backend_epoch=self.epoch,
            )
            result["worker_devices"] = self.cached.get("devices", [])
            if result["state"] == "failed" and self.failed_worker_log is not None:
                result["worker_log"] = self.failed_worker_log
            result["waiting_for_presentation"] = self.feedback_waiting()
            message, engine_message = artist_message(result.get("message", ""), result["pending_update"])
            if engine_message is not None:
                result["engine_message"] = engine_message
            result["message"] = message
            if result["pending_update"]:
                result.update(state="paused" if result["backend_state"] == "paused" else "rendering", progress=0.0)
            return result

    def frame(self):
        with self.condition:
            return self.image, self.image_generation, self.image_sequence

    def frame_is_current(self, image):
        with self.condition:
            return (
                image is self.image
                and self.frame_identity is not None
                and self.frame_identity[0] == self.epoch
                and self.frame_identity[1] == self.physical
                and self.image_generation == self.committed
            )

    def focus_sample(self, x, y):
        with self.condition:
            image = self.focus_image
            identity = self.epoch, self.physical
            generation = self.committed
            if image is None or self.focus_identity != identity or self.closing:
                return None
        result = _native.sample_focus(image, x, y)
        with self.condition:
            if (
                image is not self.focus_image
                or identity != (self.epoch, self.physical)
                or generation != self.committed
                or self.closing
            ):
                return None
        result.update(render_revision=identity[1], generation=generation, current_scene=True)
        return result

    def display_state(self, after_sequence=0):
        """Atomically pair an owned frame with its accepted scene identity."""
        with self.condition:
            owned = None
            if (
                after_sequence is not None
                and self.image is not None
                and self.image_sequence > after_sequence
                and self.frame_is_current(self.image)
            ):
                owned = self.image, self.image_generation, self.image_sequence
            return self.committed, self.physical, owned

    def diagnostics(self):
        with self.condition:
            result = dict(self.cached["diagnostics"])
            result["controller"] = dict(
                execution="worker",
                events=list(self.events),
                requested_scene_submission=self.required_scene,
                committed_scene_submission=self.completed_scene,
                pending_navigation=int(self.pending_navigation is not None),
                rejected_scene_submission=self.rejected_scene[0] if self.rejected_scene is not None else None,
                scene_repair_retries=self.scene_repair_retries,
                preparing=int(self.preparing is not None),
                applied_roots_revision=self.applied_roots_revision,
                pose_preparation_refreshes=self.pose_preparation_refreshes,
                first_frame_fast_polls=self.first_frame_fast_polls,
                gpu_scheduling=getattr(self.backend, "gpu_scheduling", None),
                control_rejections=list(self.control_rejections),
                worker_log=str(self.backend.log_path) if hasattr(self.backend, "log_path") else self.failed_worker_log,
                coalesced_updates=self.coalesced,
                backend_epoch=self.epoch,
                transport=dict(self.cached.get("transport", {})),
            )
            return result

    def close(self, wait=True):
        if not self.closing:
            session_log.write("render session closing", self.cached.get("renderer_pid") or None)
        with self.condition:
            self.closing = True
            if self.starting_process is not None and self.starting_process.poll() is None:
                self.starting_process.kill()
            self.frame_wakeups.clear()
            self.focus_image = self.focus_identity = None
            self.pending = None
            self.rejected_scene = None
            self.pending_navigation = self.navigation = None
            self.condition.notify()
        if wait:
            self.thread.join(timeout=5.0)
            if self.thread.is_alive() and isinstance(self.backend, ExternalBackend):
                # The worker is still busy (a scene update or BVH build it
                # cannot interrupt answers the shutdown command only after it).
                self.forced_stop = True
                self.backend.process.kill()
                self.thread.join(timeout=5.0)
            if self.thread.is_alive():
                raise RuntimeError("External renderer controller shutdown is still waiting")
            if self.shutdown_error:
                raise RuntimeError("Renderer shutdown failed: " + self.shutdown_error)


class WorkerSession(Session):
    def wait(self, generation=None, timeout=180):
        # A synchronous consumer also needs prompt delivery of the first image.
        # Use the existing bounded fast-poll window without activating viewer
        # acknowledgement or pacing when no viewer is attached.
        controller = self._controller
        with controller.condition:
            controller.blocking_waiters += 1
            controller.condition.notify()
        try:
            return super().wait(generation, timeout)
        finally:
            with controller.condition:
                controller.blocking_waiters -= 1

    def __init__(
        self,
        device=None,
        width=640,
        height=360,
        samples=64,
        interactive=True,
        collect_diagnostics=False,
        use_texture_cache=True,
        auto_texture_cache=False,
        denoising=None,
        execution=None,
        threads=0,
        passes=None,
        disk_only=False,
        progressive_display=False,
        use_tiling=False,
        tile_size=2048,
        hair_subdivisions=2,
        refit_meshes=False,
    ):
        from ..core.execution import resolve

        self.execution = resolve(execution)
        if self.execution != "worker":
            raise ValueError("WorkerSession requires the persistent external renderer")
        self._interactive = interactive
        self.disk_only = disk_only
        self.device = device if device is not None else default_device()
        self.width, self.height = width, height
        self._preview_pass, self._native_preview = "combined", False
        self._preview_focus = False
        self._handle = _native.create(
            self.device,
            width,
            height,
            samples,
            interactive,
            collect_diagnostics,
            use_texture_cache,
            auto_texture_cache,
            denoising,
            True,
            threads,
            passes,
            progressive_display,
            use_tiling,
            tile_size,
            hair_subdivisions,
            refit_meshes,
        )
        self._controller = QueuedController(
            self._handle, disk_only=disk_only, startup_snapshot=_native.worker_startup_snapshot()
        )
        self._memory_request = None
        self.last_capture_seconds = 0.0
        self.material_paths_revision = 0
        self._notified_color_identity = None
        self._conversion = ThreadPoolExecutor(max_workers=1, thread_name_prefix="Cycles scene conversion")
        self._active_capture = self._pending_capture = None
        self._capture_events = []
        self._capture_coalesced = 0
        self._capture_closed = False
        self._in_host_capture = False
        self._retirement = None
        self._retirement_done = False
        self._capture_retirement = None
        self._capture_failure = ""

    @staticmethod
    def _merge_updates(previous, current):
        """Coalesce deltas, retaining edits omitted by a later camera-only update."""
        result = dict(previous)
        result.update(current)
        for field, deleted in (("materials", "deleted_materials"), ("lights", "deleted_lights")):
            values = {item["key"]: item for item in previous.get(field, [])}
            removed = set(previous.get(deleted, [])) - values.keys()
            for key in current.get(deleted, []):
                values.pop(key, None)
                removed.add(key)
            for item in current.get(field, []):
                values[item["key"]] = item
                removed.discard(item["key"])
            if field in previous or field in current:
                result[field] = list(values.values())
            if deleted in previous or deleted in current:
                result[deleted] = sorted(removed)
        for deleted, key in (("deleted_objects", "object_key"), ("deleted_geometry", "geometry_key")):
            removed = set(previous.get(deleted, [])) | set(current.get(deleted, []))
            removed.difference_update(item[key] for item in current.get("geometry", []))
            if deleted in previous or deleted in current:
                result[deleted] = sorted(removed)
        if "camera" in current:
            result.pop("camera_path", None)
        elif "camera_path" in current:
            result.pop("camera", None)
        return result

    def _start_capture(self, request):
        update, at_time, reuse, submission = request
        started = time.perf_counter()
        scan = "geometry" in update
        previous = self._in_host_capture
        self._in_host_capture = True
        try:
            with _motion_capture_stage(update, "scene roots", submission, interactive=self._interactive):
                job = (
                    _native.acquire_apply(self._handle, update, at_time)
                    if scan
                    else _native.prepare_apply(self._handle, update, at_time, reuse)
                )
        finally:
            self._in_host_capture = previous
        self.last_capture_seconds = time.perf_counter() - started
        future = self._conversion.submit(_native.prewarm_apply if scan else _native.convert_apply, job)
        self._active_capture = dict(
            job=job,
            future=future,
            submission=submission,
            acquisition=self.last_capture_seconds,
            stage="scan" if scan else "convert",
            request=request,
            scan=0.0,
            preparation=0.0,
            preparation_max=0.0,
            scan_batches=0,
            preparation_yields=0,
        )

    def _pump_capture(self):
        # Only host polling may finalize policies or acquire another scene.
        if threading.current_thread() is not threading.main_thread() or self._capture_closed or self._in_host_capture:
            return
        self._in_host_capture = True
        try:
            # Each capture stage (scan, convert, images, finish) used to wait for
            # the next host poll (at most 25 per second), although a stage of an
            # unchanged scene takes well under a millisecond: about 110 ms of
            # waiting per light edit. Continue with the next stage while it
            # finishes within a small main-thread budget; longer stages still
            # return to Houdini's event loop and resume on the next poll.
            deadline = time.perf_counter() + _CAPTURE_CHAIN_SECONDS
            while True:
                before = self._active_capture
                before_future = before["future"] if before is not None else None
                self._advance_capture()
                active = self._active_capture
                if active is None or self._capture_failure or active.get("not_before"):
                    break
                if active is before and active["future"] is before_future:
                    break  # No stage finished in this pass.
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    break
                try:
                    active["future"].result(timeout=remaining)
                except FutureTimeout:
                    break
                except Exception:
                    pass  # The next _advance_capture owns error handling.
        finally:
            self._in_host_capture = False

    def _advance_capture(self):
        if self._capture_failure:
            return
        active = self._active_capture
        if active is not None:
            if time.perf_counter() < active.get("not_before", 0.0):
                return
            job, future, submission = active["job"], active["future"], active["submission"]
            if not future.done():
                return
            self._active_capture = None
            started = time.perf_counter()
            conversion = 0.0
            try:
                duration = future.result()
                if active["stage"] == "scan":
                    active["scan"] += duration
                    active["scan_batches"] += 1
                elif active["stage"] == "convert":
                    active["geometry_conversion"] = conversion = duration
                elif active["stage"] == "images":
                    active["image_conversion"] = duration
                if active["stage"] == "discard":
                    active["discard"] = duration
                elif submission <= self._controller.cancelled_through:
                    active["stage"] = "discard"
                    active["future"] = self._conversion.submit(_native.discard_apply, job)
                    active["discard_submission_seconds"] = time.perf_counter() - started
                    self._active_capture = active
                    return
                elif active["stage"] in ("scan", "styles"):
                    update, at_time, reuse, _ = active["request"]
                    with _motion_capture_stage(update, "shutter samples", submission, interactive=self._interactive):
                        more = _native.expand_apply(job) if active["stage"] == "scan" else False
                        ready = False if more else _native.prepare_apply(self._handle, update, at_time, reuse, job)
                    elapsed = time.perf_counter() - started
                    active["preparation"] += elapsed
                    active["preparation_max"] = max(active["preparation_max"], elapsed)
                    if not more and not ready:
                        active["stage"] = "styles"
                        active["preparation_yields"] += 1
                        active["not_before"] = time.perf_counter() + 0.005
                        active["future"] = Future()
                        active["future"].set_result(0.0)
                    else:
                        active["stage"] = "scan" if more else "convert"
                        active.pop("not_before", None)
                        active["future"] = self._conversion.submit(
                            _native.prewarm_apply if more else _native.convert_apply, job
                        )
                    self._active_capture = active
                    return
                else:
                    if active["stage"] == "convert":
                        active["geometry_conversion"] = conversion
                        if _native.acquire_images(job):
                            active["stage"] = "images"
                            active["image_acquisition"] = time.perf_counter() - started
                            active["future"] = self._conversion.submit(_native.convert_images, job)
                            self._active_capture = active
                            return
                    desired = _native.finish_apply(job)
                    if "geometry" in active["request"][0]:
                        # Assignment paths become available only after the
                        # asynchronous capture has published its native ledger.
                        self.material_paths_revision += 1
                    self.notify_color_configuration()
                    # Active owned work must make progress during continuous
                    # input. Coalesce pending requests, rather than repeatedly
                    # restarting scans or withholding every completed scene.
                    with self._controller.condition:
                        if self._controller.closing or submission <= self._controller.cancelled_through:
                            active["stage"] = "discard"
                            active["future"] = self._conversion.submit(_native.discard_apply, job)
                            active["discard_submission_seconds"] = time.perf_counter() - started
                            self._active_capture = active
                            return
                        self._controller.submit(desired, submission)
                    from ..blender.prototype_leases import session_leases

                    session_leases(self).commit(submission)
            except Exception as error:
                if active["stage"] == "discard":
                    # Failed cleanup is an internal failure. Never start a new
                    # acquisition against possibly unretired mutable state.
                    if self._pending_capture is not None:
                        from ..blender.prototype_leases import session_leases

                        session_leases(self).drop(self._pending_capture[3])
                    self._pending_capture = None
                    self._active_capture = active
                    self._capture_failure = "Scene cleanup failed: " + str(error)
                    self._controller.close(wait=False)
                    return
                if "geometry" in active["request"][0]:
                    # Failed lookup paths also need observation for repair.
                    self.material_paths_revision += 1
                with self._controller.condition:
                    if self._pending_capture is None:
                        message = (
                            str(error)
                            if isinstance(error, _CaptureInterrupted)
                            else "Scene capture failed: " + str(error)
                        )
                        self._controller.reject_capture(submission, message)
                active["stage"] = "discard"
                active["future"] = self._conversion.submit(_native.discard_apply, job)
                active["discard_submission_seconds"] = time.perf_counter() - started
                self._active_capture = active
                return
            if active["stage"] == "discard":
                from ..blender.prototype_leases import session_leases

                session_leases(self).drop(submission)
            self._capture_events.append(
                dict(
                    submission=submission,
                    acquisition_seconds=active["acquisition"],
                    scan_seconds=active["scan"],
                    preparation_seconds=active["preparation"],
                    maximum_preparation_seconds=active["preparation_max"],
                    scan_batches=active["scan_batches"],
                    preparation_yields=active["preparation_yields"],
                    conversion_seconds=active.get("geometry_conversion", conversion),
                    image_acquisition_seconds=active.get("image_acquisition", 0.0),
                    image_conversion_seconds=active.get("image_conversion", 0.0),
                    completion_seconds=time.perf_counter() - started,
                    discard=active.get("discard"),
                    discard_submission_seconds=active.get("discard_submission_seconds", 0.0),
                )
            )
            self._capture_events = self._capture_events[-256:]
        if self._pending_capture is not None:
            request, self._pending_capture = self._pending_capture, None
            try:
                self._start_capture(request)
            except Exception as error:
                from ..blender.prototype_leases import session_leases

                session_leases(self).drop(request[3])
                message = (
                    str(error) if isinstance(error, _CaptureInterrupted) else "Scene acquisition failed: " + str(error)
                )
                self._controller.reject_capture(request[3], message)

    def preview_object_poses(self, update, at_time=None):
        """Freeze accepted OBJ poses; never pump the source adapter or acknowledge it."""
        import hou

        if self._capture_closed or self._in_host_capture:
            return None
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("Pose acquisition must run on Houdini's host thread")
        at_time = hou.time() if at_time is None else at_time
        with self._controller.condition:
            if (
                self._controller.closing
                or self._controller.requested <= self._controller.cancelled_through
                or self._controller.cached["status"]["state"] in ("idle", "paused", "cancelled", "closed", "failed")
            ):
                return None
            baseline = self._controller.acknowledged
            previous = self._controller.navigation
            cancelled = self._controller.cancelled_through
            epoch = self._controller.epoch
        if baseline is None:
            return None
        # Transform expressions can call back into the host. A nested poll must
        # not advance the source adapter or start another acquisition here.
        self._in_host_capture = True
        try:
            navigation = _native.prepare_object_navigation(
                self._handle, baseline, update, at_time, previous[1] if previous is not None else None
            )
        finally:
            self._in_host_capture = False
        if navigation is None:
            return None
        with self._controller.condition:
            if (
                self._capture_closed
                or self._controller.closing
                or cancelled != self._controller.cancelled_through
                or epoch != self._controller.epoch
                or self._controller.cached["status"]["state"] in ("idle", "paused", "cancelled", "closed", "failed")
            ):
                return None
            submission = self._controller.reserve()
            # A preview cannot discharge source work. The host still owes a
            # complete capture, including when this acquisition observed new poses.
            self._controller.require_scene(submission, capture=False)
            self._controller.submit_navigation(navigation, submission)
        return submission

    def prepare_material_edit(self, paths, resolver, at_time):
        """Check a value edit in the material networks at paths against the accepted scene (host thread).

        Returns (ok, reason, keys). With ok the edited graphs are kept for the next materials-only apply
        (update without geometry, material_edit=True); otherwise the complete read is needed.
        """
        if self._capture_closed or self._in_host_capture or self._capture_failure:
            return False, "capture busy or closed", ()
        if self._active_capture is not None or self._pending_capture is not None:
            return False, "another read is in progress", ()
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("Material acquisition must run on Houdini's host thread")
        self._in_host_capture = True
        try:
            return _native.prepare_material_edit(self._handle, resolver, list(paths), at_time)
        finally:
            self._in_host_capture = False

    def commit_pose_previews(self):
        """Accept the OBJ poses already sent as previews as the scene; False if source work is outstanding.

        Pose previews never discharge the scene obligation on their own. After a
        transform-only gesture whose affected set is proven to be those poses alone
        (Entry.pose_commit), the previews ARE the complete change: mark the
        obligation met instead of reading the whole scene again.
        """
        if self._active_capture is not None or self._pending_capture is not None or self._capture_failure:
            return False
        controller = self._controller
        with controller.condition:
            if (
                controller.closing
                or controller.pending is not None
                or controller.rejected_scene is not None
                or (controller.preparing is not None and controller.preparing["scene"])
                or controller.latest_capture > controller.completed_scene
            ):
                return False
            controller.completed_scene = max(controller.completed_scene, controller.required_scene)
            controller.condition.notify()
        return True

    def apply(self, update, at_time=None, *, reuse_if_unchanged=False):
        if self._capture_failure:
            raise RuntimeError(self._capture_failure)
        import hou

        if self._in_host_capture:
            raise RuntimeError("A native scene transaction cannot be reentered")
        if hou.isUIAvailable() and threading.current_thread() is not threading.main_thread():
            raise RuntimeError("Scene extraction must run on Houdini's host thread.")
        if self._capture_closed:
            raise RuntimeError("Cycles session is closed")
        from ..blender.prototype_leases import session_leases, split_update

        payload, metadata = split_update(update)
        leases = session_leases(self)
        staging = object()
        try:
            leases.begin(staging, metadata)
            return self._apply_registered(payload, at_time, reuse_if_unchanged, leases, staging)
        finally:
            leases.drop(staging)

    def _apply_registered(self, update, at_time, reuse_if_unchanged, leases, staging):
        import hou

        payload = _copy_update(update)
        _native.validate_update_metadata(payload)
        at_time = hou.time() if at_time is None else at_time
        submission = self._controller.reserve()
        navigation_only = set(payload) <= {
            "camera",
            "camera_path",
            "camera_dof",
            "camera_sampling",
            "motion",
            "pixel_aspect",
            "dicing",
            "crop",
            "defer_camera_subdivision",
        }
        if not navigation_only:
            # Register the new source obligation before publishing its camera
            # preview. Otherwise submit_navigation can retry an older rejected
            # scene under this submission while the fresh capture is pending.
            self._controller.require_scene(submission)
        with self._controller.condition:
            baseline = self._controller.acknowledged
            previous_navigation = self._controller.navigation
        # Freeze only camera and outer OBJ poses. This deliberately avoids the
        # exclusively owned adapter and all SOP/material/texture evaluation.
        if baseline is not None and any(
            key in payload
            for key in ("camera", "camera_path", "geometry", "lights", "crop", "defer_camera_subdivision")
        ):
            started = time.perf_counter()
            navigation = _native.prepare_navigation(
                self._handle,
                baseline,
                payload,
                at_time,
                previous_navigation[1] if previous_navigation is not None else None,
            )
            if navigation is not None:
                # Even a noninteractive worker can have explicit pose previews.
                # Refresh only the fields present in this source packet so a
                # complete light capture supersedes old overrides while a
                # material-only capture retains the latest accepted camera.
                self._controller.submit_navigation(
                    navigation, submission, final=navigation_only, preview=self._interactive
                )
                if navigation_only and self._interactive:
                    self.last_capture_seconds = time.perf_counter() - started
                    self._capture_events.append(
                        dict(
                            submission=submission,
                            acquisition_seconds=self.last_capture_seconds,
                            scan_seconds=0.0,
                            preparation_seconds=0.0,
                            maximum_preparation_seconds=0.0,
                            scan_batches=0,
                            conversion_seconds=0.0,
                            completion_seconds=0.0,
                            navigation_only=True,
                        )
                    )
                    self._capture_events = self._capture_events[-256:]
                    return submission
        self._controller.require_scene(submission)
        if self._active_capture is not None:
            if self._pending_capture is not None:
                payload = self._merge_updates(self._pending_capture[0], payload)
                previous_submission = self._pending_capture[3]
                leases.inherit(staging, previous_submission)
                leases.drop(previous_submission)
                self._capture_coalesced += 1
            # Failed coalescing is still an unaccepted staging transaction;
            # the caller's finally block can release it without leaking a slot.
            leases.rename(staging, submission)
            self._pending_capture = (payload, at_time, reuse_if_unchanged, submission)
            self._pump_capture()
        else:
            leases.rename(staging, submission)
            try:
                self._start_capture((payload, at_time, reuse_if_unchanged, submission))
            except _CaptureInterrupted as error:
                leases.drop(submission)
                self._controller.reject_capture(submission, str(error))
                return submission
            except Exception as error:
                leases.drop(submission)
                self._controller.reject_capture(submission, "Scene acquisition failed: " + str(error))
                raise
            # Run the following stages now (within the chain budget) instead of
            # waiting for the next host poll: a small read then reaches the
            # renderer in this call.
            self._pump_capture()
        return submission

    def calibration(self):
        desired = _native.fixture(self._handle)
        self.notify_color_configuration()
        result = self._controller.submit(desired)
        leases = getattr(self, "_prototype_leases", None)
        if leases is not None:
            keep = [self._active_capture["submission"]] if self._active_capture is not None else []
            if self._pending_capture is not None:
                keep.append(self._pending_capture[3])
            leases.clear(keep=keep)
        return result

    def notify_color_configuration(self):
        identity = _native.color_policy()["render_policy_id"]
        if identity == self._notified_color_identity:
            return False
        self._controller.control("color", (identity, _native.encode_color_configuration()))
        self._notified_color_identity = identity
        return True

    def status(self):
        self._pump_capture()
        if self._capture_closed:
            self._poll_retirement()
            result = dict(self._controller.status(), capture_pending=False, capture_restart_required=False)
            if not self._retirement_done:
                result.update(state="closing", message="Retiring scene conversion")
            else:
                result.update(_native.native_stats(self._handle))
            return result
        capture = _native.status(self._handle)
        result = self._controller.status()
        for name in (
            "cooking_seconds",
            "extraction_seconds",
            "extractions",
            "cache_hits",
            "packed_instances",
            "index_instance_ids",
        ):
            result[name] = capture[name]
        for name in (
            "point_instance_syntheses",
            "point_instance_cache_hits",
            "point_instances_synthesized",
            "point_instance_seconds",
        ):
            result[name] = capture.get(name, 0)
        result.update(_native.native_stats(self._handle))
        result["last_capture_seconds"] = self.last_capture_seconds
        result["capture_pending"] = self._active_capture is not None or self._pending_capture is not None
        result["capture_restart_required"] = bool(self._capture_failure)
        if self._capture_failure:
            result.update(
                state="failed", error=self._capture_failure, message=self._capture_failure, update_rejected=False
            )
        # A navigation preview may already have the latest user generation while
        # its geometry is still being prepared. Expose that distinction honestly.
        if result["capture_pending"] and result["state"] not in ("failed", "closed", "cancelled"):
            result["pending_update"] = True
        result["coalesced_updates"] += self._capture_coalesced
        return result

    def diagnostics(self):
        result = self._controller.diagnostics()
        result["capture"] = dict(
            events=list(self._capture_events),
            coalesced_updates=self._capture_coalesced,
            active=int(self._active_capture is not None),
            pending=int(self._pending_capture is not None),
            retirement=self._capture_retirement,
            failure=self._capture_failure,
        )
        return result

    def memory_snapshot(self):
        """Queue at most one opt-in query; the GUI never waits for worker I/O."""
        controller = self._controller
        with controller.condition:
            if controller.closing or controller.closed or controller.backend is None:
                return None
            if self._memory_request is None or self._memory_request.is_set():
                self._memory_request = controller.control("memory")
            value = controller.backend_memory
            return value if value and value.get("pid") == controller.cached.get("renderer_pid") else None

    def textures_changed(self):
        with self._controller.condition:
            return self._controller.cached["textures_changed"]

    def texture_inputs(self):
        with self._controller.condition:
            return list(self._controller.cached["texture_inputs"])

    def image_value(self, function, after_sequence=0, *, current_scene=True):
        image, generation, sequence = self._controller.frame()
        if image is None or sequence <= after_sequence:
            return None
        result = function(image)
        if result is not None:
            result.update(render_revision=result.get("generation"), generation=generation, sequence=sequence)
            if current_scene:
                result["current_scene"] = self._controller.frame_is_current(image)
        return result

    def frame(self, after_sequence=0):
        return self.image_value(_native.frame, after_sequence)

    def frame_info(self, after_sequence=0):
        return self.image_value(_native.frame_info, after_sequence)

    def focus_sample(self, x, y):
        if not self._preview_focus:
            return None
        return self._controller.focus_sample(x, y)

    def output_metadata(self):
        image, _, _ = self._controller.frame()
        return _native.output_metadata(image) if image is not None else {}

    def display_image(self, **settings):
        return self.image_value(lambda image: _native.display_image(image, settings), current_scene=False)

    def display_frame(self, after_sequence=0):
        return self._controller.display_state(after_sequence)[2]

    def display_state(self, after_sequence=0):
        return self._controller.display_state(after_sequence)

    def set_frame_wakeup(self, viewer, callback):
        self._controller.set_frame_wakeup(viewer, callback)

    def set_feedback(self, viewer, enabled):
        self._controller.set_feedback(viewer, enabled)

    def notify_presented(self, generation, render_revision=None):
        self._controller.presented(generation, render_revision)

    def _set_preview_pass(self, name, focus=None):
        focus = self._preview_focus if focus is None else focus
        self._controller.control("preview", (name, True) if focus else name)

    def present_native(self):
        self.set_feedback("native-display", True)
        image, generation, sequence = self._controller.frame()
        if image is not None and self._controller.frame_is_current(image):
            try:
                _native.present_frame(self._handle, image, sequence)
            except _native.PreviousColorConfiguration:
                # Release feedback for this old frame so the replacement can render.
                self.notify_presented(generation, _native.frame_info(image)["generation"])
                raise
            self.notify_presented(generation, _native.frame_info(image)["generation"])

    def close_native_display(self):
        self.set_feedback("native-display", False)
        was_native = self._native_preview
        _native.close_display(self._handle)
        self._native_preview = False
        # Interpreter shutdown may retire controllers before registry owners.
        # Closing an unused/retired display must not send a preview command.
        with self._controller.condition:
            if was_native and not self._capture_closed and not self._controller.closing and not self._controller.closed:
                self._set_preview_pass(self._preview_pass)

    def pause(self, paused=True):
        self._controller.control("pause", paused)

    def cancel(self):
        self._pending_capture = None
        self._controller.control("cancel")
        leases = getattr(self, "_prototype_leases", None)
        if leases is not None:
            keep = [self._active_capture["submission"]] if self._active_capture is not None else []
            leases.clear(keep=keep)

    def begin_write_exr(self, filename, options=None):
        """Queue an output write without waiting; poll the event, then finish."""
        return self._controller.control("write", (os.fspath(filename), options))

    def write_finished(self, event):
        """True once the write completed, failed, or can no longer run."""
        if event.is_set():
            return True
        controller = self._controller
        with controller.condition:
            return controller.closed or (controller.closing and not controller.thread.is_alive())

    def finish_write_exr(self, event):
        if not event.is_set():
            raise RuntimeError("The render session closed before the output was written")
        if event.error:
            raise RuntimeError(event.error)
        status = self.status()
        if status["state"] == "failed":
            raise RuntimeError(status["error"])

    def write_exr(self, filename, options=None):
        # Scripts may block; the long-operation watchdog bounds a hung worker
        # and the controller sets the event on failure or shutdown.
        event = self.begin_write_exr(filename, options)
        while not self.write_finished(event):
            event.wait(0.25)
        self.finish_write_exr(event)

    def close(self, *, wait=None):
        # A final sequence releases its worker before the next sequence starts.
        self._close(wait=not self._interactive if wait is None else wait, exiting=False)

    def _close(self, *, wait, exiting):
        global _retirement_polling
        if self._in_host_capture:
            raise RuntimeError("Cannot close a session during native scene preparation")
        if self._capture_closed:
            if wait:
                self._poll_retirement(wait=True)
            return
        self._capture_closed = True
        self._pending_capture = None
        active, self._active_capture = self._active_capture, None
        self.close_native_display()
        self._controller.close(wait=False)

        def retire():
            try:
                if active is not None:
                    job, future = active["job"], active["future"]
                    try:
                        future.result()
                    except Exception:
                        pass
                    _native.discard_apply(job)
            finally:
                try:
                    self._controller.close()
                finally:
                    self._capture_retirement = _native.retire_capture(self._handle)

        gui = False
        if not wait:
            import hou

            gui = hou.isUIAvailable()
        if gui:
            # Queued behind the sole active converter; never join it on the UI.
            self._retirement = self._conversion.submit(retire)
            if not _retirement_polling:
                hou.ui.addEventLoopCallback(_poll_retirements)
                _retirement_polling = True
            _retiring.add(self)
            self._conversion.shutdown(wait=False)
        else:
            if exiting:
                # Python joins executor workers before atexit callbacks. Do
                # not submit new tasks after that shutdown barrier.
                self._conversion.shutdown(wait=True)
                retire()
            else:
                self._conversion.submit(retire).result()
                self._conversion.shutdown(wait=True)
            _native.close(self._handle)
            leases = getattr(self, "_prototype_leases", None)
            if leases is not None:
                leases.close()
            self._retirement_done = True

    def shutdown(self):
        """Interpreter exit may wait, but must not call HOM or register UI callbacks."""
        self._close(wait=True, exiting=True)

    def _poll_retirement(self, wait=False):
        if threading.current_thread() is not threading.main_thread() or self._retirement is None:
            return
        if not wait and not self._retirement.done():
            return
        try:
            self._retirement.result()
        finally:
            # The Houdini-facing handle must retire on its host thread, and
            # 'closed' cannot be reported before this final release completes.
            _native.close(self._handle)
            leases = getattr(self, "_prototype_leases", None)
            if leases is not None:
                leases.close()
            self._retirement = None
            self._retirement_done = True
            _retiring.discard(self)


def _shutdown():
    for session in tuple(_retiring):
        session._poll_retirement(wait=True)
    for controller in list(_controllers):
        controller.close(wait=False)
    for controller in list(_controllers):
        try:
            controller.close()
        except RuntimeError:
            pass


atexit.register(_shutdown)
