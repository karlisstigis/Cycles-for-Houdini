# SPDX-License-Identifier: Apache-2.0
"""Bounded, authenticated loopback control channel; never runs host operations on socket threads."""

from __future__ import annotations

import json
import queue
import secrets
import socket
import threading
import time
import uuid


class ControlBridge:
    def __init__(self):
        self.events = queue.Queue(maxsize=64)
        self.token = secrets.token_hex(32)
        self._channels = {}
        self._connections = set()
        self._bound = {}
        self._closed = set()
        self._readers = set()
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._slots = threading.BoundedSemaphore(8)
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self._listener.settimeout(0.25)
        self.endpoint = self._listener.getsockname()
        self._accept_thread = threading.Thread(target=self._listen, name="Cycles control accept", daemon=True)
        self._accept_thread.start()

    def bind(self, key):
        channel = uuid.uuid4().hex
        with self._lock:
            self._channels[channel] = key
        return dict(protocol=1, event="bind", endpoint=self.endpoint, token=self.token, channel=channel)

    def _listen(self):
        while not self._stopped.is_set():
            try:
                connection, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not self._slots.acquire(blocking=False):
                connection.close()
                continue
            with self._lock:
                self._connections.add(connection)
                reader = threading.Thread(
                    target=self._read, args=(connection,), name="Cycles control read", daemon=True
                )
                self._readers.add(reader)
                reader.start()

    def _read(self, connection):
        channel = None
        buffer = bytearray()
        handshake_deadline = time.monotonic() + 5
        try:
            # Raw recv keeps shutdown bounded even if a peer leaves a partial
            # line open. Buffered socket files cannot be safely retried on timeout.
            connection.settimeout(0.25)
            while not self._stopped.is_set():
                try:
                    chunk = connection.recv(4096)
                except socket.timeout:
                    if channel is None and time.monotonic() > handshake_deadline:
                        raise ValueError("Control handshake timed out")
                    continue
                if not chunk:
                    break
                buffer.extend(chunk)
                while b"\n" in buffer:
                    length = buffer.index(b"\n")
                    if length > 16384:
                        raise ValueError("Control message exceeds size limit")
                    message = json.loads(buffer[:length])
                    del buffer[: length + 1]
                    if not isinstance(message, dict):
                        raise ValueError("Control message must be an object")
                    if channel is None:
                        candidate, pid = message.get("channel"), message.get("pid")
                        if not isinstance(candidate, str) or type(pid) is not int or not 0 < pid <= 0xFFFFFFFF:
                            raise ValueError("Invalid control channel or process identifier")
                        if (
                            message.get("protocol") != 1
                            or message.get("event") != "bind"
                            or not secrets.compare_digest(message.get("token", ""), self.token)
                        ):
                            raise ValueError("Invalid control handshake")
                        with self._lock:
                            if candidate not in self._channels or candidate in self._bound:
                                raise ValueError("Control channel is unknown or already connected")
                            channel = candidate
                            self._bound[channel] = connection
                        self.events.put_nowait((channel, "connected", pid))
                    elif (
                        message.get("protocol") != 1
                        or message.get("channel") != channel
                        or message.get("event") != "update"
                    ):
                        raise ValueError("Invalid control update")
                if len(buffer) > 16384:
                    raise ValueError("Control message exceeds size limit")
        except (OSError, ValueError, TypeError, queue.Full):
            pass
        finally:
            if channel is not None:
                with self._lock:
                    self._closed.add(channel)
                    self._bound.pop(channel, None)
            with self._lock:
                self._connections.discard(connection)
            connection.close()
            self._slots.release()
            with self._lock:
                self._readers.discard(threading.current_thread())

    def drain(self):
        result = []
        while True:
            try:
                channel, event, pid = self.events.get_nowait()
            except queue.Empty:
                break
            with self._lock:
                key = self._channels.get(channel)
            if key is not None:
                result.append((key, channel, event, pid))
        with self._lock:
            for channel in self._closed:
                key = self._channels.pop(channel, None)
                if key is not None:
                    result.append((key, channel, "closed", None))
            self._closed.clear()
        return result

    def unbind(self, channel):
        with self._lock:
            self._channels.pop(channel, None)
            connection = self._bound.get(channel)
        if connection:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def close(self):
        self._stopped.set()
        try:
            self._listener.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._listener.close()
        self._accept_thread.join(timeout=1)
        with self._lock:
            connections = list(self._connections)
            readers = list(self._readers)
            self._channels.clear()
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        deadline = time.monotonic() + 1
        for reader in readers:
            reader.join(timeout=max(0, deadline - time.monotonic()))
        if self._accept_thread.is_alive() or any(reader.is_alive() for reader in readers):
            raise RuntimeError(
                "Control bridge threads did not stop before the shutdown deadline: "
                + ", ".join(thread.name for thread in [self._accept_thread, *readers] if thread.is_alive())
            )
