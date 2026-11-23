# SPDX-License-Identifier: Apache-2.0
"""Authenticated local final-render display. All pipe I/O stays off the GUI."""

import json
import queue
import threading
import time
from multiprocessing.connection import Client, Listener
from .. import _native
from .background_jobs import TERMINAL, process_identity


LIMIT = 8 * 1024**3


class Server:
    """Runs in hython. Only its main thread executes HOM and ROP callbacks."""

    def __init__(self, directory, spec):
        self.directory, self.spec = directory, spec
        self.lock = threading.Lock()
        self.session = None
        self.epoch = 0
        self.completed = None
        self.owned = False
        self.error = ""
        self.paused = False
        self.closed = threading.Event()
        self.connected = threading.Event()
        self.listener = Listener(spec["address"], family="AF_PIPE", authkey=bytes.fromhex(spec["key"]))
        self.connection = None
        self.thread = threading.Thread(target=self.serve, name="Cycles final display server", daemon=True)
        self.thread.start()

    def bind(self, session):
        retired = None
        with self.lock:
            if self.session is not None and self.session is not session:
                if not self.owned:
                    raise RuntimeError("Final display session changed before ownership was transferred")
                retired = self.session
            if self.session is not session:
                self.session = session
                self.owned = False
                self.epoch += 1
                if self.paused:
                    session.pause(True)
        # serve holds the same lock while accessing a session. Once detached,
        # the previous frame's engine can be retired without a display race.
        if retired is not None:
            retired.close()

    def retain(self, session):
        with self.lock:
            if self.closed.is_set() or self.session is not session:
                return False
            self.owned = True
            return True

    def capture(self):
        """Keep one immutable finished packet across engine/frame replacement."""
        with self.lock:
            if self.session is None:
                return
            frame = self.session.frame_info()
            if frame and frame["complete"]:
                self.completed = (self.epoch, frame["sequence"], _native.encode_render_reply(self.session._handle, 0))

    def serve(self):
        try:
            connection = self.connection = self.listener.accept()
            self.connected.set()
            preview = None
            while not self.closed.is_set():
                request = json.loads(connection.recv_bytes(65536))
                if not isinstance(request, dict) or request.get("op") not in (
                    "poll",
                    "pause",
                    "cancel",
                    "write",
                    "memory",
                    "close",
                ):
                    raise ValueError("Malformed final-render control message")
                operation = request["op"]
                if operation == "close":
                    self.closed.set()
                    break
                try:
                    with self.lock:
                        session = self.session
                        if operation == "cancel":
                            (self.directory / "stop").touch(exist_ok=True)
                            if session is not None:
                                session.cancel()
                        elif operation == "pause":
                            if type(request.get("value")) is not bool:
                                raise ValueError("Invalid pause state")
                            self.paused = request["value"]
                            if session is not None:
                                session.pause(request["value"])
                        elif operation == "write":
                            if session is None:
                                raise ValueError("No final image is available")
                            session.write_exr(request["filename"], request.get("options"))
                        packet = b""
                        live_status = None
                        epoch = self.epoch
                        if session is not None:
                            selected = request.get("pass", "combined")
                            after = request.get("after", 0)
                            if (
                                not isinstance(selected, str)
                                or len(selected) > 256
                                or type(after) is not int
                                or after < 0
                            ):
                                raise ValueError("Invalid final display request")
                            if (self.epoch, selected) != preview:
                                session.set_preview_pass(selected)
                                preview = self.epoch, selected
                                after = 0
                            if request.get("epoch") != self.epoch:
                                after = 0
                            live_status = session.status()
                            frame = session.frame_info()
                            if self.completed is not None and (
                                not frame or frame["generation"] != live_status["generation"]
                            ):
                                epoch, sequence, completed = self.completed
                                if request.get("pixels", True) and (
                                    request.get("epoch") != epoch or request.get("after", 0) < sequence
                                ):
                                    packet = completed
                            else:
                                packet = _native.encode_render_reply(
                                    session._handle, after if request.get("pixels", True) else 2**64 - 1
                                )
                    memory = None
                    if operation == "memory":
                        from .. import process_memory

                        memory = process_memory()
                    connection.send_bytes(
                        json.dumps(
                            dict(ok=True, packet=bool(packet), epoch=epoch, status=live_status, memory=memory)
                        ).encode()
                    )
                    if packet:
                        connection.send_bytes(packet)
                except Exception as error:
                    connection.send_bytes(json.dumps(dict(ok=False, error=str(error))).encode())
        except (EOFError, OSError):
            pass  # Parent closed the authenticated connection.
        except Exception as error:
            self.error = str(error)
        finally:
            self.closed.set()
            with self.lock:
                session = self.session
            if session is not None and session.status()["state"] in ("rendering", "paused"):
                if not self.error:
                    (self.directory / "stop").touch(exist_ok=True)
                session.cancel()

    def wait(self):
        began = time.monotonic()
        while not self.closed.wait(0.2):
            parent = process_identity(self.spec["parent_pid"])
            expected = self.spec["parent_identity"]
            if (parent and (not parent["alive"] or expected and parent.get("created") != expected.get("created"))) or (
                not self.connected.is_set() and time.monotonic() - began > 60
            ):
                self.closed.set()
                break

    def close(self):
        self.closed.set()
        if self.connection is not None:
            self.connection.close()
        self.listener.close()
        # Joining happens only in the disposable batch process. Never destroy
        # a native session while the display thread can still be reading it.
        self.thread.join(timeout=3)
        if self.thread.is_alive():
            return
        if self.owned and self.session is not None:
            self.session.close()
            self.owned = False


class RemoteSession:
    """Immutable cached pixels/status on the GUI; one bounded I/O producer."""

    def __init__(self, job, spec):
        self.job, self.spec = job, spec
        self.receiver = _native.frame_receiver()
        self.lock = threading.Lock()
        self.commands = queue.Queue()
        self.closing = threading.Event()
        self.wake = threading.Event()
        self.callbacks = {}
        self.image = None
        self.textures = []
        self.sequence = self.after = self.generation = 0
        self.epoch = 0
        self.source_identity = None
        self.image_generation = 0
        self.preview = "combined"
        self.feedback = True
        self.stop_requested = False
        self.transport_error = ""
        self.backend_memory = None
        self._memory_request = None
        self.cached = dict(
            state="rendering",
            generation=0,
            progress=0.0,
            message="Starting final render",
            error="",
            first_image_seconds=0.0,
            device_allocation_bytes=0,
            geometry_conversions=0,
        )
        self.job_value = dict(state="starting", error="")
        self.pending = set()
        self.thread = threading.Thread(target=self.run, name="Cycles final display client", daemon=True)
        self.thread.start()

    def run(self):
        connection = None
        try:
            while not self.closing.is_set():
                try:
                    connection = Client(self.spec["address"], family="AF_PIPE", authkey=bytes.fromhex(self.spec["key"]))
                    break
                except (FileNotFoundError, OSError):
                    value = self.job.status()
                    with self.lock:
                        self.job_value = value
                    if value["state"] in TERMINAL:
                        return
                    self.closing.wait(0.05)
            while connection is not None and not self.closing.is_set():
                try:
                    operation, data, event = self.commands.get_nowait()
                except queue.Empty:
                    operation, data, event = "poll", {}, None
                with self.lock:
                    preview, after, feedback, epoch = self.preview, self.after, self.feedback, self.epoch
                request = dict(op=operation, after=after, epoch=epoch, pixels=feedback, **{"pass": preview}, **data)
                connection.send_bytes(json.dumps(request).encode())
                # Long output writes may take time. The GUI only reads caches.
                while not connection.poll(0.1):
                    if self.closing.is_set():
                        raise EOFError("Final viewer closed")
                    if self.job.poll() is not None:
                        raise EOFError("Final render process exited")
                envelope = json.loads(connection.recv_bytes(65536))
                if not envelope.get("ok"):
                    if event is None:
                        raise RuntimeError(envelope.get("error", "Invalid final render reply"))
                    event.error = RuntimeError(envelope["error"])
                elif envelope.get("packet"):
                    packet = connection.recv_bytes(LIMIT)
                    reply = _native.decode_render_reply(packet, self.receiver)
                    callbacks = ()
                    with self.lock:
                        image = reply.get("frame")
                        identity = envelope["epoch"], reply["status"]["generation"]
                        if identity != self.source_identity and (
                            image is not None or self.source_identity is None and identity[1] > 0
                        ):
                            self.source_identity = identity
                            self.generation += 1
                            self.after = 0
                        self.epoch = envelope["epoch"]
                        self.cached = reply["status"]
                        self.cached["generation"] = self.generation
                        self.textures = reply["texture_inputs"]
                        if image is not None and self.preview == preview:
                            self.image = image
                            self.sequence += 1
                            self.image_generation = self.generation
                            self.after = _native.frame_info(image)["sequence"]
                            callbacks = tuple(self.callbacks.values())
                    for callback in callbacks:
                        callback()
                if envelope.get("status") is not None:
                    with self.lock:
                        # The visible image may belong to the preceding frame
                        # while final output has no progressive pixels yet.
                        self.cached.update(
                            {key: value for key, value in envelope["status"].items() if key != "generation"}
                        )
                if envelope.get("memory") is not None:
                    with self.lock:
                        self.backend_memory = envelope["memory"]
                value = self.job.status()
                with self.lock:
                    self.job_value = value
                    if event is not None:
                        self.pending.discard(event)
                if event is not None:
                    event.set()
                self.wake.wait(0.08)
                self.wake.clear()
        except Exception as error:
            value = self.job.status()
            with self.lock:
                self.job_value = value
                if (
                    not self.closing.is_set()
                    and not self.stop_requested
                    and value["state"] not in ("failed", "cancelled")
                    and not value.get("skipped_frames")
                ):
                    self.transport_error = str(error)
                    self.cached = dict(self.cached, state="failed", error=str(error), message="Final display failed")
        finally:
            if connection is not None:
                try:
                    if self.closing.is_set():
                        connection.send_bytes(b'{"op":"close"}')
                except OSError:
                    pass
                connection.close()
            with self.lock:
                for event in self.pending:
                    event.error = RuntimeError("Final render connection closed")
                    event.set()
                self.pending.clear()

    def status(self):
        with self.lock:
            value, job = dict(self.cached), self.job_value
        if self.closing.is_set():
            return dict(value, state="closed", device_allocation_bytes=0)
        if self.transport_error:
            return dict(value, state="failed", error=self.transport_error, message="Final display failed")
        if job["state"] in ("cancelled", "failed"):
            value.update(state=job["state"], error=job.get("error", ""), message=job.get("error") or job["state"])
        elif job["state"] == "completed":
            value.update(
                state="complete",
                progress=1.0,
                message="Finished" if value["generation"] else "Output skipped: file already exists",
            )
        return value

    def job_status(self):
        with self.lock:
            value = dict(self.job_value)
            if self.transport_error:
                value.update(state="failed", error=self.transport_error)
            return value

    def memory_snapshot(self):
        if self.closing.is_set() or not self.thread.is_alive():
            return None
        if self._memory_request is None or self._memory_request.is_set():
            self._memory_request = self.command("memory")
        with self.lock:
            return self.backend_memory

    def display_state(self, after_sequence=0):
        with self.lock:
            owned = (
                (self.image, self.generation, self.sequence)
                if (
                    self.image is not None
                    and self.image_generation == self.generation
                    and after_sequence is not None
                    and self.sequence > after_sequence
                )
                else None
            )
            return self.generation, self.generation, owned

    def set_preview_pass(self, name, *, focus=False):
        with self.lock:
            changed = self.preview != name
            if changed:
                self.preview = name
                self.after = 0
        if changed:
            self.wake.set()

    def set_frame_wakeup(self, viewer, callback):
        with self.lock:
            if callback is None:
                self.callbacks.pop(viewer, None)
            else:
                self.callbacks[viewer] = callback

    def command(self, operation, **data):
        event = threading.Event()
        event.error = None
        with self.lock:
            if self.closing.is_set():
                raise RuntimeError("Final viewer is closed")
            self.pending.add(event)
        self.commands.put((operation, data, event))
        self.wake.set()
        return event

    def pause(self, paused=True):
        self.command("pause", value=bool(paused))

    def cancel(self):
        # Latch Stop even during HIP load/cooking before the pipe is connected.
        if self.stop_requested:
            return
        self.stop_requested = True

        def stop():
            self.job.stop()
            deadline = time.monotonic() + 3
            while self.job.is_running() and self.job.status()["state"] not in TERMINAL and time.monotonic() < deadline:
                time.sleep(0.05)
            # A disposable cook or device initialization can be uninterruptible.
            # Never wait for it on the GUI; retire only this owned child.
            if self.job.is_running() and self.job.status()["state"] not in TERMINAL:
                self.job.force_stop()
                # TerminateProcess returns before Windows signals process exit.
                # Wait off the GUI for the owned job to acknowledge termination.
                forced_deadline = time.monotonic() + 1
                while self.job.is_running() and time.monotonic() < forced_deadline:
                    time.sleep(0.02)
            # Startup can still be blocked inside the pipe's authentication
            # handshake. Publish the acknowledged stop independently of that
            # I/O thread so the viewer's Stop state remains bounded too.
            value = self.job.status()
            with self.lock:
                self.job_value = value

        threading.Thread(target=stop, name="Cycles final stop", daemon=True).start()
        self.command("cancel")

    def begin_write_exr(self, filename, options=None):
        return self.command("write", filename=str(filename), options=options)

    def write_finished(self, event):
        return event.is_set()

    def finish_write_exr(self, event):
        if event.error:
            raise event.error

    def set_feedback(self, viewer, enabled):
        with self.lock:
            self.feedback = bool(enabled)

    def notify_presented(self, *args):
        pass

    def notify_color_configuration(self):
        pass  # FrameReceiver owns the captured working-space configuration.

    def native_material_paths(self):
        return ()  # The snapshot is intentionally independent of artist edits.

    def native_stylesheet_resources(self):
        return ()

    def texture_inputs(self):
        with self.lock:
            return list(self.textures)

    def frame_info(self):
        with self.lock:
            image, sequence, generation = self.image, self.sequence, self.image_generation
        if image is None:
            return None
        return dict(_native.frame_info(image), sequence=sequence, generation=generation)

    def close(self):
        if self.closing.is_set():
            return
        self.closing.set()
        self.wake.set()
        with self.lock:
            self.callbacks.clear()
            self.image = None

        def retire():
            self.job.stop()
            self.thread.join(timeout=3)
            deadline = time.monotonic() + 3
            while self.job.is_running() and time.monotonic() < deadline:
                time.sleep(0.05)
            if self.job.is_running():
                if self.job.status()["state"] in TERMINAL:
                    self.job.terminate()
                else:
                    self.job.force_stop()

        threading.Thread(target=retire, name="Cycles final retirement", daemon=True).start()
