# SPDX-License-Identifier: Apache-2.0
"""Ordered SOHO final frames, owned by public Houdini ROP lifecycle events.

Final rendering is independent of the interactive registry. No frame is
coalesced: completion and successful output precede the next submission.
"""

from __future__ import annotations


import atexit
from contextlib import nullcontext, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
import re
from threading import Event, Thread
import time

import hou
from ..ipr import registry
from ..scene import native_scene
from . import output


_active = {}
_summary_key = "cycles_houdini.final_sequence"
_shutdown_registered = False
_observers = set()
_viewer_receivers = {}
_frame_outputs = {}
_output_validation = None
_active_writes = []
_output_retirements = []


@contextmanager
def output_to(rop, target):
    """Scope an alternate final-frame destination to this ROP's native render."""
    key = rop.sessionId()
    if key in _frame_outputs or key in _active:
        raise RuntimeError("This ROP is already rendering")
    _frame_outputs[key] = target
    try:
        yield
    finally:
        _frame_outputs.pop(key, None)
        finish(rop, reason="output-closed")


@contextmanager
def retain_for_viewer(rop, receiver):
    """Transfer this render's display session to an explicit viewer owner.

    ROP execution, frame ordering and output remain unchanged. The receiver
    owns closing the transferred session after the finished image is dismissed.
    """
    key = rop.sessionId()
    if key in _viewer_receivers:
        raise RuntimeError("This ROP already has a final-render viewer")
    _viewer_receivers[key] = receiver
    try:
        yield
    finally:
        _viewer_receivers.pop(key, None)


class RenderCancelled(InterruptedError):
    """An artist stopped an ordered final render."""


class RenderGPUFailure(RuntimeError):
    """A failed renderer status with a short message for the artist."""

    def __init__(self, status, kind, message, worker_log):
        super().__init__(status["error"])
        self.status = status
        self.info = dict(kind=kind, message=message, details=status["error"], worker_log=worker_log)


_GPU_ILLEGAL_ADDRESS = re.compile(
    r"\bcuda_error_illegal_address\b|illegal address in cu|\billegal memory access was encountered\b"
    r"|\bcuda error code:\s*700\b",
    re.IGNORECASE,
)
_GPU_OUT_OF_MEMORY = re.compile(
    r"\bout of (?:gpu(?: and shared host)?|device) memory|\bcuda_error_out_of_memory\b"
    r"|\boptix_error_device_out_of_memory\b|\bout of memory (?:in cu|\(cuda\b)|\bcuda error code:\s*2\b",
    re.IGNORECASE,
)


def _gpu_error_kind(text, *, ordered=False):
    illegal = _GPU_ILLEGAL_ADDRESS.search(text)
    oom = _GPU_OUT_OF_MEMORY.search(text)
    if illegal is not None and (oom is None or not ordered or illegal.start() < oom.start()):
        return "illegal_address"
    if oom is not None:
        return "out_of_memory"
    return None


def _first_gpu_log_error(worker_log):
    if not worker_log:
        return None, None
    try:
        # Normal worker logs are quiet until a fault. Bound the GUI-side read
        # even when explicit tracing has produced a large file.
        with Path(worker_log).open("rb") as stream:
            beginning = stream.read(512 * 1024).decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return None, None
    for line in beginning.splitlines():
        kind = _gpu_error_kind(line, ordered=True)
        if kind is not None:
            return kind, line
    return None, None


def _render_gpu_failure(session, status):
    if status.get("update_rejected") or status.get("capture_restart_required"):
        return None
    details = status["error"]
    kind = _gpu_error_kind(details)
    if kind is None and status.get("denoiser") == "oidn_gpu" and "denois" in status.get("message", "").lower():
        if re.search(r"\bout of memory\b", details, re.IGNORECASE):
            kind = "out_of_memory"
    if kind is None and "cycles worker disconnected" not in details.lower():
        return None
    worker_log = status.get("worker_log")
    if worker_log is None and status.get("execution") == "worker":
        try:
            worker_log = session.diagnostics().get("controller", {}).get("worker_log")
        except Exception:
            # Diagnostics may already be unavailable if the worker exited.
            pass
    # The live status can concatenate errors by device. Only a worker log
    # establishes the order, including an OOM reported after a CUDA fault.
    logged_kind, first_error = _first_gpu_log_error(worker_log)
    kind = logged_kind or kind
    if kind is None:
        return None
    if kind == "illegal_address":
        message = "Render Stopped - Illegal GPU Memory Access."
    else:
        message = "Render Stopped - Out of GPU Memory."
    failure = RenderGPUFailure(status, kind, message, worker_log)
    if first_error is not None:
        failure.info["first_error"] = first_error
    return failure


@contextmanager
def observe(callback):
    """Batch progress hook; called synchronously on the rendering main thread."""
    _observers.add(callback)
    try:
        yield
    finally:
        _observers.discard(callback)


def _notify(event, value):
    for callback in tuple(_observers):
        callback(event, value)


@dataclass
class Sequence:
    rop: object
    pause_token: object = None
    session: object = None
    pending_write: Event | None = None
    configuration: tuple = None
    created_sessions: int = 0
    waiting_for_output: bool = False
    completed_frames: int = 0
    skipped_frames: int = 0
    last_frame: float = None
    last_generation: int = None
    last_status: dict = None
    error: str = ""
    gpu_failure: dict | None = None
    error_phase: str = ""
    error_frame: float = None
    hook_phase: str = "pre-render"
    hook_frame: float = None
    cancelled: bool = False
    last_outputs: tuple = ()
    warned: dict = None
    source_diagnostics: list = field(default_factory=list)

    def release_session(self):
        session, self.session = self.session, None
        self.configuration = None
        if session is not None:
            write, self.pending_write = self.pending_write, None
            if write is not None:
                # A scene/node event can end the sequence while a HOM progress
                # update is on the stack. The writer still owns its atomic file.
                self.cancelled = True
                _retire_output(session, write)
                return
            try:
                if self.gpu_failure is None:
                    self.last_status = session.status()
            finally:
                receiver = _viewer_receivers.get(self.rop.sessionId())
                try:
                    retained = receiver(session) if receiver is not None else False
                except BaseException:
                    session.close()
                    raise
                if not retained:
                    session.close()

    def release_pause(self):
        token, self.pause_token = self.pause_token, None
        if token is not None:
            registry.end_local_final(token)

    def publish(self, reason):
        # Sequence diagnostic history, with no image/resource ownership.
        summary = dict(
            reason=reason,
            error=self.error,
            gpu_failure=self.gpu_failure,
            error_phase=self.error_phase,
            error_frame=self.error_frame,
            cancelled=self.cancelled,
            session_alive=self.session is not None,
            created_sessions=self.created_sessions,
            completed_frames=self.completed_frames,
            last_frame=self.last_frame,
            last_generation=self.last_generation,
            last_status=self.last_status,
            skipped_frames=self.skipped_frames,
            last_outputs=list(self.last_outputs),
            warnings=list((self.warned or {}).values()),
            source_diagnostics=list(self.source_diagnostics),
        )
        self.rop.setCachedUserData(_summary_key, summary)
        _notify("sequence", (self.rop, summary))


def _record_diagnostics(state, issues, at_time, *, existing=False):
    frame = hou.timeToFrame(at_time)
    for issue in issues:
        record = dict(issue, frame=frame, rop=state.rop.path(), existing_output=existing)
        if record not in state.source_diagnostics:
            state.source_diagnostics.append(record)
        if str(issue.get("severity", "warning")).lower() == "info":
            continue
        if state.warned is None:
            state.warned = {}
        code = str(issue.get("code", ""))
        source = str(issue.get("source", ""))
        resource = str(issue.get("resource", ""))
        key = (frame, code, source, resource, str(issue.get("attribute", "")))
        if key not in state.warned:
            state.warned[key] = (
                f"Cycles warning [{code}]: {issue.get('message', '')} "
                f"{issue.get('action', '')} ({state.rop.path()}, frame {frame:g}, source {source or resource})"
            )


def _missing_input(issue):
    code = str(issue.get("code", ""))
    return (
        str(issue.get("severity", "warning")).lower() != "info"
        and not code.startswith("render.")
        and code != "volume.resampled"
    )


def active_sequences():
    """Lifecycle diagnostic; interactive sessions have separate ownership."""
    return tuple(_active)


def request_cancel(rop):
    """Request cancellation on the render thread, without closing it in a UI callback."""
    state = _active.get(rop.sessionId())
    if state is None:
        return False
    state.cancelled = True
    return True


def finish(rop, reason="post-render", error=None):
    state = _active.pop(rop.sessionId(), None)
    if state is None:
        return
    if error is not None and not state.error:
        state.error = str(error)
    if error is not None and isinstance(error, (RenderCancelled, hou.OperationInterrupted)):
        state.cancelled = True
        reason = "cancelled"
    elif state.waiting_for_output:
        state.cancelled = True
    try:
        state.release_session()
    finally:
        try:
            state.publish(reason)
            _report_warnings(state)
        finally:
            state.release_pause()


def _report_warnings(state):
    """Report the sequence's render warnings once, outside SOHO (PostRender or later). In the Houdini
    interface they go to the status bar only: printing would open the Houdini console over the artist's
    work after the render is done. Without the interface (hython, background renders) stdout is the
    render log and keeps every line. Never raises; a warning never fails a render."""
    try:
        if not state.warned:
            return
        if hou.isUIAvailable():
            hou.ui.setStatusMessage(list(state.warned.values())[-1], severity=hou.severityType.Warning)
        else:
            for text in state.warned.values():
                print(text, flush=True)
    except Exception:
        pass


def close_all():
    for state in tuple(_active.values()):
        finish(state.rop, reason="host-shutdown")


def _scene_event(event_type, **kwargs):
    if event_type in (hou.hipFileEventType.BeforeClear, hou.hipFileEventType.BeforeLoad):
        close_all()


def _node_event(node, event_type, **kwargs):
    if event_type == hou.nodeEventType.BeingDeleted:
        finish(node, reason="node-deleted")


def _render_event(rop, event_type, at_time):
    if event_type == hou.ropRenderEventType.PreRender:
        finish(rop, reason="next-render")
        # Houdini also fires PreRender for native IPR. SOHO knows the
        # render mode when it enters render_frame for a local final.
        _active[rop.sessionId()] = Sequence(rop, hook_frame=hou.timeToFrame(at_time))
    elif event_type in (hou.ropRenderEventType.PreFrame, hou.ropRenderEventType.PostFrame):
        state = _active.get(rop.sessionId())
        if state is not None:
            state.hook_phase = "pre-frame" if event_type == hou.ropRenderEventType.PreFrame else "post-frame"
            state.hook_frame = hou.timeToFrame(at_time)
    elif event_type == hou.ropRenderEventType.PostRender:
        state = _active.get(rop.sessionId())
        if state is not None:
            errors = rop.errors()
            if errors and not state.error:
                # Houdini executes the HDA's standard script parameters. Its
                # native error lacks node/frame context, but the preceding ROP
                # event identifies the script phase without running it twice.
                state.error_phase = state.hook_phase
                state.error_frame = state.hook_frame
                state.error = f"{rop.path()}, frame {state.hook_frame:g}, {state.hook_phase}: {errors[0].strip()}"
                state.cancelled = "OperationInterrupted" in errors[0]
                print("Cycles ROP hook error: " + state.error, flush=True)
        reason = "post-render"
        if state is not None and state.error:
            reason = "cancelled" if state.cancelled else "failed"
        finish(rop, reason=reason)


def _after_render_script(rop, event_type, at_time):
    if event_type != hou.ropRenderEventType.PostRender:
        return
    # The ordinary callback must release the worker before Houdini runs the
    # user's post-render script. This second observation records its failure
    # afterward; it never executes a hook or changes Houdini's result.
    summary = rop.cachedUserData(_summary_key)
    errors = rop.errors()
    if not summary or summary["error"] or not errors:
        return
    frame = summary["last_frame"] if summary["last_frame"] is not None else hou.frame()
    error = f"{rop.path()}, frame {frame:g}, post-render: {errors[0].strip()}"
    summary = dict(
        summary,
        reason="cancelled" if "OperationInterrupted" in errors[0] else "failed",
        error=error,
        error_phase="post-render",
        error_frame=frame,
        cancelled="OperationInterrupted" in errors[0],
    )
    rop.setCachedUserData(_summary_key, summary)
    _notify("sequence", (rop, summary))
    print("Cycles ROP hook error: " + error, flush=True)


def register(rop):
    """Called by asset OnCreated/OnLoaded, before any sequence starts."""
    global _shutdown_registered
    # HOM explicitly deduplicates identical ROP callbacks. Node callbacks need
    # an explicit check when OnLoaded/registration runs more than once.
    rop.addRenderEventCallback(_render_event, run_before_script=True)
    rop.addRenderEventCallback(_after_render_script, run_before_script=False)
    if not any(callback is _node_event for _, callback in rop.eventCallbacks()):
        rop.addEventCallback((hou.nodeEventType.BeingDeleted,), _node_event)
    if not _shutdown_registered:
        hou.hipFile.addEventCallback(_scene_event)
        if hou.isUIAvailable():
            from PySide6 import QtWidgets

            QtWidgets.QApplication.instance().aboutToQuit.connect(close_all)
        _shutdown_registered = True


def wait_for_completion(
    session, generation, *, label="Cycles final render", timeout=None, cancel_requested=None, on_frame=None
):
    """Poll metadata only; full render planes stay out of Python during waits."""
    started = time.monotonic()
    # Nest in Houdini's existing render operation. Its native Interrupt window
    # owns progress, Pause and cancellation; a separate Qt dialog duplicates it.
    operation = hou.InterruptableOperation(label, open_interrupt_dialog=True) if hou.isUIAvailable() else nullcontext()
    try:
        with operation as progress:
            while True:
                status = session.status()
                _notify("progress", (session, status))
                if cancel_requested is not None and cancel_requested():
                    raise RenderCancelled("Render cancelled")
                if status["state"] == "failed":
                    if status["generation"] == generation:
                        gpu_failure = _render_gpu_failure(session, status)
                        if gpu_failure is not None:
                            raise gpu_failure
                    raise RuntimeError(status["error"])
                if status["state"] == "cancelled":
                    raise RenderCancelled("Render cancelled")
                if status["state"] == "closed":
                    raise RuntimeError(f"Render {status['state']}")
                if status["generation"] != generation:
                    raise RuntimeError(f"Final render generation {generation} was superseded by {status['generation']}")
                if status.get("color_update_pending"):
                    raise RuntimeError("Rendering stopped for a color configuration change")
                if progress is not None:
                    progress.updateProgress(status["progress"])
                if getattr(session, "disk_only", False):
                    # The worker certifies that its completed image belongs to
                    # this generation. Disk output never needs the pixels here.
                    if status["state"] == "complete" and status["committed_generation"] == generation:
                        return
                else:
                    frame = session.frame_info()
                    if on_frame is not None:
                        on_frame(session, frame, generation)
                    if frame and frame["generation"] == generation and frame["complete"]:
                        return frame
                if timeout is not None and time.monotonic() - started > timeout:
                    raise TimeoutError(status["message"])
                time.sleep(0.02)
    except RenderGPUFailure:
        # The renderer already stopped. Leave its display available until the
        # caller reports the failure and retires the final session.
        raise
    except BaseException:
        session.cancel()
        raise


@dataclass
class OutputValidation:
    finished: Event = field(default_factory=Event)
    stopped: Event = field(default_factory=Event)
    results: list = field(default_factory=list)
    error: BaseException | None = None

    def run(self, paths, contracts, strict):
        def checkpoint():
            if self.stopped.is_set():
                raise RenderCancelled("Render cancelled")

        try:
            _inspect_paths(paths, contracts, strict, self.results, checkpoint)
        except BaseException as error:
            self.error = error
        finally:
            self.finished.set()


def _inspect_paths(paths, contracts, strict, results, checkpoint):
    for path, contract in zip(paths, contracts):
        checkpoint()
        value = output.inspect_existing(path, contract, require_clean_inputs=strict, before_read=checkpoint)
        checkpoint()
        results.append(value)


def _inspect_output_set(state, paths, contracts, at_time, strict):
    global _output_validation
    results = []

    def checkpoint():
        _notify("output-validation", state.rop)
        if state.cancelled:
            raise RenderCancelled("Render cancelled")

    try:
        if hou.isUIAvailable():
            # A previous cancelled network read can still be inside the OS.
            # Only one task can own I/O per process, including across retries.
            while (previous := _output_validation) is not None and not previous.finished.is_set():
                with hou.InterruptableOperation(
                    "Waiting for cancelled Cycles output check", open_interrupt_dialog=True
                ) as progress:
                    while not previous.finished.wait(0.02):
                        checkpoint()
                        progress.updateProgress(0.0)

            task = _output_validation = OutputValidation()
            results = task.results
            thread = Thread(
                target=task.run,
                args=(paths, contracts, strict),
                name="Cycles output validation",
                daemon=True,
            )
            try:
                thread.start()
            except BaseException:
                _output_validation = None
                raise
            try:
                checkpoint()
                if not task.finished.wait(0.02):
                    with hou.InterruptableOperation(
                        "Checking existing Cycles output", open_interrupt_dialog=True
                    ) as progress:
                        while not task.finished.is_set():
                            checkpoint()
                            progress.updateProgress(len(results) / max(1, len(paths)))
                            task.finished.wait(0.02)
                        progress.updateProgress(1.0)
                checkpoint()
                if task.error is not None:
                    raise task.error
            finally:
                task.stopped.set()
                if task.finished.is_set() and _output_validation is task:
                    _output_validation = None
        else:
            _inspect_paths(paths, contracts, strict, results, checkpoint)
            checkpoint()
    finally:
        # Record provenance on the HOM thread, including paths already checked
        # if a later output or the operation failed.
        for issues in tuple(results):
            if issues is not None:
                _record_diagnostics(state, issues, at_time, existing=True)
    return [issues is not None for issues in results]


def _retire_output(session, event):
    """Keep an issued write alive until its atomic publication or rejection."""
    if not _output_retirements:
        hou.ui.addEventLoopCallback(_poll_output_retirements)
    _output_retirements.append((session, event))


def _poll_output_retirements():
    if not _output_retirements:
        return
    try:
        for session, event in tuple(_output_retirements):
            if session.write_finished(event):
                _output_retirements.remove((session, event))
                try:
                    session.finish_write_exr(event)
                except Exception as error:
                    # The sequence was already cancelled. Retain its terminal
                    # reason while making a late worker failure diagnosable.
                    registry.trace("cancelled_output_write_failed", error=str(error))
                finally:
                    session.close(wait=False)
    finally:
        if not _output_retirements:
            hou.ui.removeEventLoopCallback(_poll_output_retirements)


def _check_output_cancelled(state):
    if state.cancelled or (state.session is not None and state.session.status()["state"] == "cancelled"):
        raise RenderCancelled("Render cancelled")


def _wait_for_previous_outputs(state):
    _poll_output_retirements()

    def pending():
        return _output_retirements or any(not session.write_finished(event) for session, event in _active_writes)

    if not pending():
        return
    # One process-wide barrier also covers another ROP reaching the same path
    # through an alias. Keep the wait interruptible before pausing any IPR.
    state.waiting_for_output = True
    try:
        with hou.InterruptableOperation("Finishing previous Cycles output", open_interrupt_dialog=True) as progress:
            while pending():
                _check_output_cancelled(state)
                progress.updateProgress(0.0)
                time.sleep(0.02)
                _poll_output_retirements()
        _check_output_cancelled(state)
    finally:
        state.waiting_for_output = False


def _wait_for_write(state, session, event):
    state.pending_write = event
    item = (session, event)
    _active_writes.append(item)
    try:
        _check_output_cancelled(state)
        # Most saves finish before the next UI update. Give them a short wait
        # without opening the native Interrupt dialog for every pass.
        if not session.write_finished(event):
            event.wait(0.02)
            if not session.write_finished(event):
                with hou.InterruptableOperation("Saving Cycles output", open_interrupt_dialog=True) as progress:
                    while not session.write_finished(event):
                        _check_output_cancelled(state)
                        progress.updateProgress(0.0)
                        event.wait(0.02)
                    progress.updateProgress(1.0)
        _check_output_cancelled(state)
    finally:
        _active_writes.remove(item)
    state.pending_write = None


def render_frame(rop, configuration, update, at_time):
    key = rop.sessionId()
    state = _active.get(key)
    ephemeral = state is None
    if ephemeral:
        # Old assets/direct SOHO invocation without a PreRender boundary must
        # remain safe: never infer sequence ownership from frame numbers.
        state = Sequence(rop)
    retain = rop.parm("persistent_data")
    retain = bool(retain and retain.evalAtTime(at_time)) and not ephemeral
    writing_outputs = False
    target = None
    try:
        gui = hou.isUIAvailable()
        if gui:
            _wait_for_previous_outputs(state)
            if state.pause_token is None:
                state.pause_token = registry.begin_local_final()
        if state.cancelled:
            raise RenderCancelled("Render cancelled")
        target = _frame_outputs.get(rop.sessionId())
        jobs = output.plan(rop, at_time)
        paths = [Path(job["filename"]) for job in jobs]
        state.last_outputs = tuple(str(path) for path in paths)
        policy = output.value(rop, "output_existing", at_time)
        if policy not in ("overwrite", "skip", "error"):
            raise ValueError("Unknown existing files policy: " + str(policy))
        strict_parm = rop.parm("fail_on_missing_inputs")
        strict = bool(strict_parm and strict_parm.evalAtTime(at_time))
        metadata = output.header_metadata(rop, at_time)
        for job in jobs:
            job["options"].update(existing_files=policy, fail_on_missing_inputs=strict, metadata=metadata)
        if policy == "error" and any(path.exists() or path.is_symlink() for path in paths):
            raise FileExistsError(
                "Render output already exists: "
                + str(next(path for path in paths if path.exists() or path.is_symlink()))
            )
        existing = []
        if policy == "skip":
            contracts = output.expected_headers(rop, at_time, jobs, render_crop=update.get("crop"))
            existing = _inspect_output_set(state, paths, contracts, at_time, strict)
            if all(existing) and not strict and target is None:
                state.skipped_frames += 1
                state.last_frame = hou.timeToFrame(at_time)
                state.publish("frame-skipped")
                return
        if state.session is not None and native_scene.session_identity(
            state.configuration
        ) != native_scene.session_identity(configuration):
            state.release_session()
        if state.session is None:
            options = (
                {"disk_only": True}
                if hou.isUIAvailable() and rop.sessionId() not in _viewer_receivers and target is None
                else {}
            )
            if target is not None:
                options["progressive_display"] = True
            state.session = registry.session_from_configuration(configuration, interactive=False, **options)
            state.configuration = configuration
            state.created_sessions += 1
        _notify("session", state.session)
        if target is not None:
            target.prepare(state.session, at_time)
        generation = state.session.apply(update, at_time=at_time)
        wait_for_completion(
            state.session,
            generation,
            label=f"Cycles {rop.path()} frame {hou.timeToFrame(at_time):g}",
            cancel_requested=lambda: state.cancelled,
            on_frame=target.progress if target is not None else None,
        )
        state.last_status = state.session.status()
        _record_diagnostics(state, state.last_status.get("source_diagnostics", ()), at_time)
        if strict:
            failures = [issue for issue in state.last_status.get("source_diagnostics", ()) if _missing_input(issue)]
            if failures:
                raise RuntimeError(
                    "Missing render inputs: "
                    + "; ".join(
                        f"[{issue.get('code', '')}] {issue.get('source') or issue.get('resource', '')}: {issue.get('message', '')}"
                        for issue in failures
                    )
                    + f" ({rop.path()}, frame {hou.timeToFrame(at_time):g})"
                )

        # Queued/worker writes return only after the encoder's acknowledgement.
        def before_write():
            _check_output_cancelled(state)
            _notify("progress", (state.session, state.session.status()))
            _check_output_cancelled(state)

        if target is not None:
            target.present(state.session, before_write=before_write)
        writing_outputs = True
        wait_for_write = (lambda session, event: _wait_for_write(state, session, event)) if gui else None
        output.write_outputs(state.session, jobs, before_write=before_write, wait_for_write=wait_for_write)
        state.last_outputs = tuple(str(path) for path in paths)
        if policy == "skip" and existing and all(existing):
            state.skipped_frames += 1
        else:
            state.completed_frames += 1
        state.last_frame = hou.timeToFrame(at_time)
        state.last_generation = generation
        state.last_status = state.session.status()
        if not retain:
            state.release_session()
        state.publish("frame-written")
    except BaseException as error:
        if not ephemeral and _active.get(key) is not state:
            # A reentrant node or scene event already retired this ownership.
            raise
        # A competing writer may have published after preflight. Recover its
        # diagnostic provenance for the structured farm report on failure too.
        state.cancelled = state.cancelled or isinstance(error, (RenderCancelled, hou.OperationInterrupted))
        if writing_outputs and not state.cancelled and "contracts" in locals():
            for path, contract in zip(paths, contracts):
                try:
                    issues = output.inspect_existing(path, contract, require_clean_inputs=strict, read_pixels=False)
                    if issues is not None:
                        _record_diagnostics(state, issues, at_time, existing=True)
                except Exception as inspection_error:
                    _record_diagnostics(
                        state, getattr(inspection_error, "source_diagnostics", ()), at_time, existing=True
                    )
        _record_diagnostics(
            state, getattr(error, "source_diagnostics", ()), at_time, existing=getattr(error, "existing_output", False)
        )
        state.error = str(error)
        if isinstance(error, RenderGPUFailure):
            state.gpu_failure = error.info
            state.error_phase = "render"
            state.error_frame = hou.timeToFrame(at_time)
            state.last_status = error.status
            report_gpu_failure = getattr(target, "report_gpu_failure", None)
            if report_gpu_failure is not None:
                try:
                    report_gpu_failure(state.session, error.info["message"])
                except Exception as display_error:
                    state.gpu_failure["display_error"] = str(display_error)
        elif state.session is not None:
            state.last_status = state.session.status()
        if state.last_status is not None:
            _record_diagnostics(state, state.last_status.get("source_diagnostics", ()), at_time)
        try:
            state.release_session()
        except Exception as cleanup_error:
            if state.gpu_failure is None:
                raise
            state.gpu_failure["cleanup_error"] = str(cleanup_error)
        finally:
            try:
                state.publish("cancelled" if state.cancelled else "failed")
            finally:
                state.release_pause()
            if not ephemeral:
                _active.pop(key, None)
        raise
    finally:
        if ephemeral:
            try:
                state.release_session()
            finally:
                state.release_pause()


def _shutdown_resources():
    # Interpreter teardown cannot traverse HOM/Qt nodes.
    for state in tuple(_active.values()):
        if state.session is not None:
            state.session.close()
    _active.clear()
    for session, _ in _output_retirements:
        session.shutdown()
    _output_retirements.clear()


atexit.register(_shutdown_resources)
