# SPDX-License-Identifier: Apache-2.0
"""Persistent Blender evaluation over authenticated, framed local pipes.

Only JSON and raw numeric buffers cross this connection; pickle is never used.
Blender's console output has a separate, bounded diagnostic stream. Geometry is
never written to a bundle, archive or Houdini geometry file by this module.

This module is the Houdini side (the client and the pool of Blender workers) and
the pipe protocol both sides share. It needs no Blender. The Blender side, which
runs inside Blender, is blender/cycles_blender/worker_server.py (GPL-3.0-or-later,
started through blender/worker_main.py); it imports the protocol from here.
"""

import atexit
from collections import deque, OrderedDict
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import struct
import threading
import time

import numpy as np

_MAX_HEADER = 16 * 1024 * 1024
_MAX_ARRAY = 2 * 1024 * 1024 * 1024
_MAX_TOTAL = 8 * 1024 * 1024 * 1024
_MAX_WORKERS = 4
_IDLE_SECONDS = 600
_workers = OrderedDict()
_pool_lock = threading.RLock()
_epoch_lock = threading.Lock()
_epochs = {}
_timing_root = os.environ.get("CH_BLEND_TIMING_SIDECAR")


def _timing_record(role, value):
    """Append outside the measured operation, in a file private to this process."""
    if not _timing_root:
        return
    try:
        path = Path(_timing_root) / (f"{role}-{os.getpid()}.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(dict(pid=os.getpid(), **value), separators=(",", ":")) + "\n")
    except Exception:
        # Diagnostics must not change the framed protocol or fail a render.
        pass


class StaleEvaluationError(RuntimeError):
    """Inputs changed during evaluation; this result must never be published."""


def _epoch(source):
    with _epoch_lock:
        return _epochs.get(os.path.normcase(str(Path(source).resolve())), 0)


def _cache_types():
    from .blend_cache import ByteBudgetCache, budget_bytes

    return ByteBudgetCache, budget_bytes


class _Pipe:
    """Framed binary messages over private inherited anonymous pipe handles."""

    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer

    def _read(self, count):
        data = bytearray(count)
        view = memoryview(data)
        offset = 0
        while offset < count:
            received = self.reader.readinto(view[offset:])
            if not received:
                raise EOFError("Blender worker closed its pipe")
            offset += received
        return data

    def send_bytes(self, data):
        self.writer.write(struct.pack("!Q", len(data)))
        view = memoryview(data)
        while view:
            written = self.writer.write(view)
            if not written:
                raise EOFError("Blender worker pipe write failed")
            view = view[written:]
        self.writer.flush()

    def recv_bytes(self, limit):
        count = struct.unpack("!Q", self._read(8))[0]
        if count > limit:
            raise ValueError("Blender pipe frame exceeds limit")
        return self._read(count)

    def close(self):
        self.reader.close()
        self.writer.close()


def _send_json(connection, value):
    data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(data) > _MAX_HEADER:
        raise ValueError("Blender protocol header exceeds limit")
    connection.send_bytes(data)


def _recv_json(connection):
    return json.loads(connection.recv_bytes(_MAX_HEADER).decode("utf-8"))


_inflight = None


class _Result(tuple):
    def __new__(cls, manifest, arrays, reservation):
        value = super().__new__(cls, (manifest, arrays))
        value.reservation = reservation
        return value

    def release(self):
        if self.reservation:
            self.reservation[0].release(self.reservation[1])
            self.reservation = None

    __del__ = release


def _array_id(values):
    digest = hashlib.sha256()
    digest.update(json.dumps([values.dtype.str, list(values.shape)], separators=(",", ":")).encode())
    if values.size:
        digest.update(memoryview(values).cast("B"))
    return digest.hexdigest()


class _DigestCache:
    """Reuse SHA identities only after comparing every byte with an owned copy.

    Render depsgraphs may be rebuilt and modifier state is temporarily changed
    by the exporter. Neither absent ID update flags nor matching element counts
    prove that evaluated attributes are unchanged. Byte equality does, including
    signed zero and same-count topology edits. Retention is per worker, bounded,
    and independent of the host's transfer cache.
    """

    def __init__(self, target_bytes, minimum_bytes=4096):
        self.target_bytes = target_bytes
        self.minimum_bytes = minimum_bytes
        self.entries = OrderedDict()
        self.resident_bytes = 0
        self.context = None

    def clear(self):
        self.entries.clear()
        self.resident_bytes = 0
        self.context = None

    def set_context(self, context):
        if context != self.context:
            self.clear()
            self.context = context

    def identity(self, key, values):
        compared = 0
        previous = self.entries.pop(key, None)
        if previous is not None:
            dtype, shape, snapshot, identity = previous
            self.resident_bytes -= len(snapshot)
            if dtype == values.dtype.str and shape == values.shape:
                current = values.view(np.uint8).reshape(-1)
                old = np.frombuffer(snapshot, dtype=np.uint8)
                equal = True
                # Bound the NumPy equality scratch allocation to 1 MiB.
                for start in range(0, values.nbytes, 1024 * 1024):
                    end = min(start + 1024 * 1024, values.nbytes)
                    compared += end - start
                    if not np.array_equal(current[start:end], old[start:end]):
                        equal = False
                        break
                if equal:
                    self.entries[key] = previous
                    self.resident_bytes += len(snapshot)
                    return identity, 0, compared, True
                del current, old
            del previous, snapshot
        identity = _array_id(values)
        if self.minimum_bytes <= values.nbytes <= self.target_bytes:
            while self.entries and (
                self.resident_bytes + values.nbytes > self.target_bytes or len(self.entries) >= 65536
            ):
                _, evicted = self.entries.popitem(last=False)
                self.resident_bytes -= len(evicted[2])
                del evicted
            # An immutable owned snapshot prevents later array mutation from
            # making an old digest appear valid for new bytes.
            snapshot = values.tobytes()
            self.entries[key] = (values.dtype.str, values.shape, snapshot, identity)
            self.resident_bytes += len(snapshot)
        return identity, values.nbytes, compared, False


def _make_digest_cache():
    # This toggle bypasses budget parsing entirely. The MiB budget itself uses
    # the same positive finite range as the host's other Blender cache budgets.
    if os.environ.get("CH_BLEND_DIGEST_REUSE", "1") == "0":
        return None
    _, budget = _cache_types()
    return _DigestCache(budget("CYCLES_HOUDINI_BLEND_DIGEST_MB", 128))


def _array_owners(manifest):
    owners = {}
    for instance in manifest.get("instances", ()):
        owners.setdefault(instance["mesh_key"], (instance.get("source_library", ""), instance["source_name"]))
    return owners


def _send_result(connection, manifest, arrays, phases=None, digest_cache=None):
    descriptors, payloads, total = [], {}, 0
    owners = _array_owners(manifest) if phases is not None or digest_cache is not None else {}
    array_rows = [] if phases is not None else None
    start = time.perf_counter() if phases is not None else None
    for name, values in arrays.items():
        contiguous_start = time.perf_counter() if phases is not None else None
        values = np.ascontiguousarray(values)
        if phases is not None:
            phases["contiguity"] = phases.get("contiguity", 0) + time.perf_counter() - contiguous_start
        if values.dtype.kind not in "biuf" or values.nbytes > _MAX_ARRAY:
            raise ValueError("Unsupported Blender geometry array: " + name)
        total += values.nbytes
        if total > _MAX_TOTAL:
            raise ValueError("Blender geometry exceeds transport limit")
        hash_start = time.perf_counter() if phases is not None else None
        mesh_key, _, field = name.partition("/")
        library, owner = owners.get(mesh_key, ("", mesh_key))
        if digest_cache is None:
            identity, hashed, compared, reused = _array_id(values), values.nbytes, 0, False
        else:
            identity, hashed, compared, reused = digest_cache.identity((library, owner, field), values)
        if phases is not None:
            phases["hashing"] = phases.get("hashing", 0) + time.perf_counter() - hash_start
            array_rows.append(
                dict(
                    name=name,
                    object=owner,
                    source_library=library,
                    identity=identity,
                    dtype=values.dtype.str,
                    shape=list(values.shape),
                    nbytes=values.nbytes,
                    hashed_bytes=hashed,
                    compared_bytes=compared,
                    digest_reused=reused,
                )
            )
        descriptors.append(
            dict(name=name, dtype=values.dtype.str, identity=identity, shape=list(values.shape), nbytes=values.nbytes)
        )
        payloads.setdefault(identity, values)
    if phases is not None:
        phases["array_prep"] = time.perf_counter() - start
        header_start = time.perf_counter()
    _send_json(connection, dict(ok=True, manifest=manifest, arrays=descriptors))
    if phases is not None:
        phases["header_encode_write"] = time.perf_counter() - header_start
        ack_start = time.perf_counter()
    missing = _recv_json(connection).get("missing")
    if phases is not None:
        phases["ack_wait_decode"] = time.perf_counter() - ack_start
    if not isinstance(missing, list) or len(missing) != len(set(missing)) or any(k not in payloads for k in missing):
        raise ValueError("Invalid Blender chunk acknowledgement")
    chunk_start = time.perf_counter() if phases is not None else None
    for identity in missing:
        values = payloads[identity]
        connection.send_bytes(memoryview(values).cast("B") if values.size else b"")
    if phases is not None:
        phases["chunk_send"] = time.perf_counter() - chunk_start
        phases["sent_chunks"] = len(missing)
        phases["sent_bytes"] = sum(payloads[identity].nbytes for identity in missing)
        requested = set(missing)
        unsent = requested.copy()
        for row in array_rows:
            row["requested"] = row["identity"] in requested
            row["sent_bytes"] = row["nbytes"] if row["identity"] in unsent else 0
            unsent.discard(row["identity"])
        phases["arrays"] = array_rows
        phases["hashed_bytes"] = sum(row["hashed_bytes"] for row in array_rows)
        phases["compared_bytes"] = sum(row["compared_bytes"] for row in array_rows)
        phases["digest_cache_bytes"] = digest_cache.resident_bytes if digest_cache is not None else 0


def _recv_result(connection, chunks, metrics, phases=None):
    if phases is None:
        header = _recv_json(connection)
    else:
        start = time.perf_counter()
        raw_header = connection.recv_bytes(_MAX_HEADER)
        phases["header_read"] = time.perf_counter() - start
        start = time.perf_counter()
        header = json.loads(raw_header.decode("utf-8"))
        phases["header_decode"] = time.perf_counter() - start
    if not header.get("ok"):
        error = header.get("error", "Blender evaluation failed")
        return StaleEvaluationError(error) if header.get("stale") else RuntimeError(error)
    descriptors = header.get("arrays", [])
    validation_start = time.perf_counter() if phases is not None else None
    if len(descriptors) > 65536:
        raise ValueError("Too many Blender geometry arrays")
    total, names, records = 0, set(), {}
    for record in descriptors:
        dtype = np.dtype(record["dtype"])
        shape = record["shape"]
        identity = record["identity"]
        if (
            dtype.kind not in "biuf"
            or dtype.hasobject
            or len(shape) > 8
            or any(type(size) is not int or size < 0 for size in shape)
            or not isinstance(identity, str)
            or len(identity) != 64
            or any(c not in "0123456789abcdef" for c in identity)
            or record["name"] in names
        ):
            raise ValueError("Invalid Blender numeric array descriptor")
        expected = math.prod(shape) * dtype.itemsize
        total += expected
        if expected != record["nbytes"] or expected > _MAX_ARRAY or total > _MAX_TOTAL:
            raise ValueError("Invalid Blender numeric array size")
        names.add(record["name"])
        if identity in records and records[identity] != (dtype, shape, expected):
            raise ValueError("Conflicting Blender chunk descriptors")
        records[identity] = dtype, shape, expected
    # Pin every referenced chunk before requesting bytes. Eviction can therefore
    # never remove a payload between the acknowledgement and reconstruction.
    global _inflight
    if _inflight is None:
        Cache, budget = _cache_types()
        from .blend_cache import InFlightBudget

        _inflight = InFlightBudget(budget("CYCLES_HOUDINI_BLEND_INFLIGHT_MB", 2048))
    _inflight.reserve(total)
    if phases is not None:
        phases["validation_reserve"] = time.perf_counter() - validation_start
    reserved = True
    owner = object()
    chunks.pin(owner, records)
    try:
        lookup_start = time.perf_counter() if phases is not None else None
        missing = [identity for identity in records if identity not in chunks]
        if phases is not None:
            phases["cache_lookup"] = time.perf_counter() - lookup_start
            phases["missing_chunks"] = len(missing)
            ack_start = time.perf_counter()
        _send_json(connection, dict(missing=missing))
        if phases is not None:
            phases["ack_encode_write"] = time.perf_counter() - ack_start
            receive_start = time.perf_counter()
        for identity in missing:
            dtype, shape, expected = records[identity]
            data = bytes(connection.recv_bytes(expected))
            if len(data) != expected:
                raise ValueError("Invalid Blender numeric array payload")
            values = np.frombuffer(data, dtype=dtype).reshape(shape)
            if _array_id(values) != identity:
                raise ValueError("Blender chunk digest mismatch")
            values.setflags(write=False)
            chunks[identity] = values
            metrics["transferred_bytes"] += expected
            metrics["transferred_chunks"] += 1
        if phases is not None:
            phases["chunk_read_validate"] = time.perf_counter() - receive_start
            phases["received_bytes"] = sum(records[identity][2] for identity in missing)
            reconstruct_start = time.perf_counter()
        arrays = {record["name"]: chunks[record["identity"]] for record in descriptors}
        manifest = header["manifest"]
        manifest["array_identities"] = {record["name"]: record["identity"] for record in descriptors}
        metrics["referenced_bytes"] += total
        metrics["reused_chunks"] += len(records) - len(missing)
        result = _Result(manifest, arrays, (_inflight, total))
        if phases is not None:
            phases["reconstruct"] = time.perf_counter() - reconstruct_start
        reserved = False
        return result
    finally:
        if reserved:
            _inflight.release(total)
        chunks.unpin(owner)
        chunks.trim()


def _signature(path):
    try:
        stat = os.stat(path)
        return stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino
    except OSError:
        return None


def launch_signature(executable, source_config=None):
    """Launch inputs which require a fresh binary/configuration process.

    OCIO files are small and can be replaced while keeping file timestamps;
    hash their contents. Returns (binary_stat_or_none, config_identity), where
    binary_stat is (mtime_ns, ctime_ns, size, inode), and config_identity is ''
    when unset or (path, sha256_or_none) for a supplied configuration.
    Executable metadata includes identity and change time
    as well as size/mtime, avoiding a full Blender executable read per frame.
    """
    try:
        stat = os.stat(executable)
        binary = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)
    except OSError:
        binary = None
    config = str(source_config or "")
    if config:
        try:
            with open(config, "rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            config = (config, digest)
        except OSError:
            config = (config, None)
    return binary, config


# Houdini's own variables must not reach Blender: its Python (PYTHONPATH, PYTHONHOME) and
# its OCIO configuration. Under Houdini's ACES config Blender lacks "Non-Color", "sRGB"
# and the other names Blender files use, so images resolve to the wrong colour space and
# a file saved there keeps ACES names. Blender runs with its own configuration, or with
# CYCLES_HOUDINI_BLENDER_OCIO when the studio sets one.
_HOST_ONLY_VARIABLES = ("PYTHONPATH", "PYTHONHOME", "OCIO")


def _blender_script(name):
    """An entry script of the Blender component (blender/, GPL-3.0-or-later; it runs only inside
    Blender): <host root>/blender/<name> in a staged or installed host, <checkout>/blender/<name>
    in a source checkout, where this file is under houdini/python3.13libs."""
    here = Path(__file__).resolve()
    for root in (here.parents[3], here.parents[4]):
        if (root / "blender" / name).is_file():
            return root / "blender" / name
    return here.parents[3] / "blender" / name


def blender_environment(source_config=None, base=None):
    """Environment for every Blender process the bridge starts (workers, listing, Open in
    Blender): ``base`` (default os.environ) without Houdini's Python and OCIO variables;
    ``OCIO`` = ``source_config`` when given."""
    base = os.environ if base is None else base
    env = {key: value for key, value in base.items() if key.upper() not in _HOST_ONLY_VARIABLES}
    if source_config:
        env["OCIO"] = str(source_config)
    return env


class _Worker:
    def __init__(self, source, executable, source_config, timeout, cancelled=None):
        self.source = str(Path(source).resolve())
        self.lock = threading.RLock()
        self.used = time.monotonic()
        self.dependencies = {self.source: _signature(self.source)}
        self.logs = deque(maxlen=128)
        self.claimed = False
        self.connection = None
        self.process = None
        self._active_rpc = None
        self._diagnostics_thread = None
        self.executable, self.source_config = executable, source_config
        self.launch_signature = launch_signature(executable, source_config)
        Cache, budget = _cache_types()
        self.chunks = Cache(budget("CYCLES_HOUDINI_BLEND_CHUNKS_MB", 256), label="Blender transfer chunks")
        self.metrics = dict(transferred_bytes=0, referenced_bytes=0, transferred_chunks=0, reused_chunks=0, batches=0)
        token = os.urandom(32)
        env = blender_environment(source_config)
        env["CH_BLEND_PIPE_KEY"] = token.hex()
        command = [
            str(executable),
            "--background",
            "--factory-startup",
            "--disable-autoexec",
            "--python-exit-code",
            "1",
            "--python",
            str(_blender_script("worker_main.py")),
            "--",
            "--worker",
        ]
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                bufsize=0,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

            def drain():
                try:
                    while True:
                        chunk = self.process.stderr.read(4096)
                        if not chunk:
                            break
                        self.logs.append(chunk.decode("utf-8", errors="replace"))
                except (OSError, ValueError):
                    pass  # The owner can close these handles during teardown.

            self._diagnostics_thread = threading.Thread(target=drain, daemon=True, name="CH Blender diagnostics")
            self._diagnostics_thread.start()
            self.connection = _Pipe(self.process.stdout, self.process.stdin)

            def handshake():
                # Blender can print startup banners before running our script.
                # Once this unpredictable marker arrives, stdout is exclusively
                # the protocol fd; Python and C/C++ logs are redirected to stderr.
                pending = bytearray()
                for _ in range(1024 * 1024):
                    pending.extend(self.connection._read(1))
                    if pending.endswith(token):
                        if len(pending) > len(token):
                            self.logs.append(pending[: -len(token)].decode("utf-8", errors="replace"))
                        return _recv_json(self.connection)
                raise RuntimeError("Blender startup marker was not received")

            hello = self._bounded(handshake, timeout, cancelled)
            if hello != dict(protocol="ch-blend-arrays", version=2, pid=self.process.pid):
                raise RuntimeError("Unexpected Blender worker handshake")
        except BaseException as error:
            self.close()
            diagnostic = "".join(self.logs)[-8192:]
            if diagnostic and isinstance(error, Exception):
                raise RuntimeError(str(error) + "\nBlender diagnostics:\n" + diagnostic) from error
            raise

    def _bounded(self, operation, timeout, cancelled=None, progress=None):
        completed = queue.Queue(maxsize=1)
        acknowledged = queue.Queue(maxsize=1)

        def emit(value):
            completed.put((None, value))
            okay, result = acknowledged.get()
            if not okay:
                raise result
            return result

        def run():
            try:
                completed.put((True, operation(emit) if progress is not None else operation()))
            except BaseException as error:
                completed.put((False, error))

        thread = threading.Thread(target=run, daemon=True, name="CH Blender pipe")
        self._active_rpc = thread
        thread.start()
        try:
            deadline = time.monotonic() + timeout
            while True:
                if cancelled is not None and cancelled.is_set():
                    self.close()
                    raise RuntimeError("Blender prewarm cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise queue.Empty
                try:
                    okay, value = completed.get(timeout=min(remaining, 0.05) if cancelled is not None else remaining)
                    if okay is None:
                        try:
                            converted = progress(value)
                            value = None
                        except BaseException as error:
                            acknowledged.put((False, error))
                            raise
                        acknowledged.put((True, converted))
                        continue
                    break
                except queue.Empty:
                    if time.monotonic() >= deadline:
                        raise
        except queue.Empty:
            self.close()
            raise TimeoutError("Blender evaluation exceeded %.1f seconds" % timeout)
        except BaseException:
            # An interrupted caller must never leave an outstanding reader on
            # a worker that another request can reuse.
            self.close()
            raise
        thread.join(timeout=1)
        self._active_rpc = None
        if not okay:
            raise value
        return value

    def evaluate_many(self, requests, texture_directory, timeout, force_reload=False, consume=None):
        with self.lock:
            self.used = time.monotonic()
            self.claimed = True
            generation = _epoch(self.source)
            signatures = {path: _signature(path) for path in self.dependencies}
            reload_source = force_reload or signatures != self.dependencies
            launch_before = launch_signature(self.executable, self.source_config)

            def operation(emit=None):
                _send_json(
                    self.connection,
                    dict(
                        command="evaluate_many",
                        requests=[dict(request, source=self.source) for request in requests],
                        texture_directory=str(Path(texture_directory).resolve()),
                        reload=reload_source,
                    ),
                )
                results, snapshots = [], []
                for index in range(len(requests)):
                    phases = {} if _timing_root else None
                    result = _recv_result(self.connection, self.chunks, self.metrics, phases)
                    if phases is not None:
                        _timing_record(
                            "host", dict(source=self.source, batch=self.metrics["batches"] + 1, ordinal=index, **phases)
                        )
                    if not isinstance(result, Exception):
                        snapshots.append(result[0].get("dependency_versions", {}))
                    if emit is not None:
                        try:
                            results.append(emit((index, result)))
                        finally:
                            if isinstance(result, _Result):
                                result.release()
                            del result
                    else:
                        results.append(result)
                return results, snapshots

            def progress(value):
                index, result = value
                return consume(index, result)

            results, snapshots = self._bounded(operation, timeout, progress=progress if consume else None)
            self.metrics["batches"] += 1
            if generation != _epoch(self.source) or launch_before != launch_signature(
                self.executable, self.source_config
            ):
                raise StaleEvaluationError("Blender inputs were invalidated during evaluation")
            dependencies = dict(signatures)
            for snapshot in snapshots:
                for path, expected in snapshot.items():
                    if _signature(path) != (tuple(expected) if expected is not None else None):
                        raise StaleEvaluationError("Blender dependency changed before publish: " + path)
                dependencies.update(
                    {path: tuple(value) if value is not None else None for path, value in snapshot.items()}
                )
            self.dependencies = dependencies
            return results

    def evaluate(self, request, texture_directory, timeout):
        return self.evaluate_many([request], texture_directory, timeout)[0]

    def close(self):
        # Terminate first: a child blocked in evaluation or a pipe read must also
        # be bounded. Blender owns no saved project state in this process.
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        # Process death closes the private pipe endpoints and unblocks readers.
        # Join before closing Python streams to avoid racing read/close calls.
        for thread in (self._active_rpc, self._diagnostics_thread):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=3)
        if self.connection is not None:
            self.connection.close()
        if self.process is not None and self.process.stderr:
            self.process.stderr.close()


def evaluate_many(source, executable, requests, texture_directory=None, timeout=300, source_config=None, consume=None):
    """Return one (manifest, numeric-arrays) pair or Exception per request.

    Processes persist across calls and selector/frame changes. File dependencies
    trigger a main-file reload inside the existing process. A crashed or timed
    out worker is removed so the next request can recover with a new process.
    The supplied directory owns only texture payloads, never geometry bundles.
    """
    if texture_directory is None:
        raise ValueError("A texture_directory is required for Blender materials")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Blender timeout must be positive and finite")
    source = str(Path(source).resolve())
    key = (os.path.normcase(source), str(Path(executable).resolve()), str(source_config or ""))
    with _epoch_lock:
        _epochs.setdefault(key[0], 0)
    requests = list(requests)
    if not requests:
        return []
    for attempt in range(3):
        worker = None
        try:
            with _pool_lock:
                for old_key, old_worker in list(_workers.items()):
                    if old_key[0] == key[0] and old_key != key:
                        with old_worker.lock:
                            _workers.pop(old_key).close()
                    elif time.monotonic() - old_worker.used > _IDLE_SECONDS and old_worker.lock.acquire(False):
                        try:
                            _workers.pop(old_key).close()
                        finally:
                            old_worker.lock.release()
                worker = _workers.get(key)
                if (
                    worker is None
                    or worker.process.poll() is not None
                    or worker.launch_signature != launch_signature(executable, source_config)
                ):
                    if worker is not None:
                        _workers.pop(key).close()
                    worker = _Worker(source, executable, source_config, timeout)
                    _workers[key] = worker
                _workers.move_to_end(key)
                while len(_workers) > _MAX_WORKERS:
                    old_key, old_worker = next(iter(_workers.items()))
                    if old_key == key:
                        break
                    old_worker.lock.acquire()
                    try:
                        _workers.pop(old_key).close()
                    finally:
                        old_worker.lock.release()
                worker.lock.acquire()
            try:
                results = worker.evaluate_many(
                    requests, texture_directory, timeout, force_reload=attempt > 0, consume=consume
                )
                for stable_attempt in range(2):
                    if not any(isinstance(result, StaleEvaluationError) for result in results):
                        break
                    results = worker.evaluate_many(
                        requests, texture_directory, timeout, force_reload=True, consume=consume
                    )
                if any(isinstance(result, StaleEvaluationError) for result in results):
                    raise StaleEvaluationError("Blender dependencies kept changing during batch evaluation")
                return results
            finally:
                worker.lock.release()
        except Exception as error:
            if worker is not None:
                with _pool_lock:
                    if _workers.get(key) is worker:
                        _workers.pop(key)
                    worker.close()
                diagnostic = "".join(worker.logs)[-8192:]
                if diagnostic:
                    error = type(error)(str(error) + "\nBlender diagnostics:\n" + diagnostic)
            if isinstance(error, StaleEvaluationError) and attempt < 2:
                continue
            return [error for _ in requests]


def catalogue_many(sources, executable, timeout=60, cancelled=None):
    """Read only library ID-name metadata in one bounded, disposable worker.

    This command never opens a main file, links data, builds a dependency graph,
    or exports geometry. It deliberately leaves render workers untouched.
    """
    sources = list(dict.fromkeys(str(Path(source).resolve()) for source in sources))
    if not sources:
        return []
    if len(sources) > 128 or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Catalogue request exceeds its bounds")
    worker = _Worker(sources[0], executable, None, timeout, cancelled)
    try:

        def operation():
            _send_json(worker.connection, dict(command="catalogue_many", sources=sources))
            return _recv_json(worker.connection)

        result = worker._bounded(operation, timeout, cancelled)
        if not isinstance(result, list) or len(result) != len(sources):
            raise ValueError("Malformed Blender catalogue response")
        return result
    finally:
        worker.close()


_MAX_ASSET_SOURCES = 64
_MAX_MATERIAL_NAMES = 64
_MAX_ID_NAME = 1024
_ASSET_COMMANDS = ("assets_many", "export_library_materials", "export_library_worlds", "list_blend_contents")
_MAX_LISTING_COLLECTIONS = 20000
_MAX_LISTING_OBJECTS = 200000
_MAX_LISTING_SCENES = 1024


def _bounded_timeout(timeout):
    return isinstance(timeout, (int, float)) and math.isfinite(timeout) and timeout > 0


def _disposable_request(command, source, executable, timeout, cancelled, source_config=None):
    """One JSON request/reply in a fresh worker that is always closed."""
    worker = _Worker(source, executable, source_config, timeout, cancelled)
    try:

        def operation():
            _send_json(worker.connection, command)
            return _recv_json(worker.connection)

        try:
            return worker._bounded(operation, timeout, cancelled)
        except Exception as error:
            diagnostic = "".join(worker.logs)[-8192:]
            if diagnostic:
                raise type(error)(str(error) + "\nBlender diagnostics:\n" + diagnostic) from error
            raise
    finally:
        worker.close()


def _command_failure(reply, what):
    if isinstance(reply, dict) and reply.get("ok") is False:
        raise RuntimeError(what + " failed in Blender: " + str(reply.get("error", "unknown error")))


def _checked_asset(asset, thumbnail_dir):
    if not isinstance(asset, dict):
        return None
    name, id_type, meta = asset.get("name"), asset.get("id_type"), asset.get("meta")
    if not isinstance(name, str) or not name or not isinstance(id_type, str) or not isinstance(meta, dict):
        return None
    tags = meta.get("tags")
    clean_meta = dict(
        catalog_id=str(meta.get("catalog_id") or ""),
        tags=[str(tag) for tag in tags] if isinstance(tags, list) else [],
        **{key: str(meta.get(key) or "") for key in ("author", "description", "license", "copyright")},
    )
    preview, size = asset.get("preview"), asset.get("preview_size")
    if (
        not isinstance(preview, str)
        or not preview
        or os.path.basename(preview) != preview
        or preview in (".", "..")
        or not (thumbnail_dir / preview).is_file()
        or not isinstance(size, list)
        or len(size) != 2
        or any(type(v) is not int or v <= 0 for v in size)
    ):
        preview, size = None, None
    object_type = asset.get("object_type")
    return {
        "name": name,
        "id_type": id_type,
        "meta": clean_meta,
        "catalog_simple_name": str(asset.get("catalog_simple_name") or ""),
        "object_type": object_type if isinstance(object_type, str) and id_type == "OBJECT" else None,
        "preview": preview,
        "preview_size": size,
    }


def _checked_asset_entry(source, entry, thumbnail_dir):
    if not isinstance(entry, dict) or entry.get("source") != source:
        raise ValueError("Malformed Blender asset response")
    if "error" in entry:
        return {
            "source": source,
            "error": str(entry["error"]),
            "error_kind": "invalid_file" if entry.get("error_kind") == "invalid_file" else "transient",
        }
    signature = entry.get("signature")
    assets = entry.get("assets")
    if (
        not isinstance(signature, list)
        or len(signature) != 4
        or any(type(v) is not int for v in signature)
        or not isinstance(assets, list)
    ):
        raise ValueError("Malformed Blender asset response")
    # The worker compared before/after itself; this also catches a change
    # between the worker's read and this host-side publication.
    if _signature(source) != tuple(signature):
        return {"source": source, "error": "Blender file changed during asset scan", "error_kind": "transient"}
    checked = [value for value in (_checked_asset(asset, thumbnail_dir) for asset in assets) if value is not None]
    result = {"source": source, "signature": signature, "assets": checked}
    warnings = entry.get("warnings")
    if isinstance(warnings, list) and warnings:
        result["warnings"] = [str(value) for value in warnings[:64]]
    return result


def assets_many(sources, executable, thumbnail_dir, timeout=120, cancelled=None):
    """List marked assets of up to 64 .blend files in one bounded, disposable worker.

    Previews are written as PNG files into thumbnail_dir; nothing is written
    next to the sources. Returns one entry per (deduplicated, resolved) source,
    in order: {"source", "signature", "assets": [...]} or {"source", "error"}.
    """
    sources = list(dict.fromkeys(str(Path(source).resolve()) for source in sources))
    if not sources:
        return []
    if len(sources) > _MAX_ASSET_SOURCES or not _bounded_timeout(timeout):
        raise ValueError("Asset scan request exceeds its bounds")
    thumbnail_dir = Path(thumbnail_dir).resolve()
    thumbnail_dir.mkdir(parents=True, exist_ok=True)
    reply = _disposable_request(
        {"command": "assets_many", "sources": sources, "thumbnail_dir": str(thumbnail_dir)},
        sources[0],
        executable,
        timeout,
        cancelled,
    )
    _command_failure(reply, "Asset scan")
    if not isinstance(reply, list) or len(reply) != len(sources):
        raise ValueError("Malformed Blender asset response")
    return [_checked_asset_entry(source, entry, thumbnail_dir) for source, entry in zip(sources, reply)]


def _material_names(names, kind="material"):
    """Distinct, bounded datablock names of an export request; `kind` names it in errors."""
    if isinstance(names, str):
        names = [names]
    names = list(dict.fromkeys(names))
    if not names or len(names) > _MAX_MATERIAL_NAMES:
        raise ValueError(kind.capitalize() + " export request exceeds its bounds")
    for name in names:
        if not isinstance(name, str) or not name or len(name) > _MAX_ID_NAME or "\0" in name:
            raise ValueError(f"Invalid Blender {kind} name: " + repr(name)[:80])
    return names


_TEXTURE_MODES = ("copy", "in_place")


def _texture_mode(textures):
    if textures not in _TEXTURE_MODES:
        raise ValueError("Invalid material texture mode: " + repr(textures)[:80])
    return textures


def export_library_materials(
    source, names, output_dir, executable, timeout=300, cancelled=None, source_config=None, textures="copy"
):
    """Append and export named materials of one library file in a disposable worker.

    Returns {"records": [blend_material_export records], "errors": {name: message}}.
    Textures are written below output_dir; the library folder is never written.
    textures="in_place" writes only packed images there and records files on disk by
    their absolute path (blend_material_export._Images).
    """
    names = _material_names(names)
    textures = _texture_mode(textures)
    if not _bounded_timeout(timeout):
        raise ValueError("Material export request exceeds its bounds")
    source = str(Path(source).resolve())
    if not Path(source).is_file() or Path(source).suffix.lower() != ".blend":
        raise ValueError("Blender source must be an existing .blend file: " + source)
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    command = {"command": "export_library_materials", "source": source, "names": names, "output_dir": str(output_dir)}
    if textures != "copy":  # the copy-mode command stays exactly as before
        command["textures"] = textures
    reply = _disposable_request(
        command,
        source,
        executable,
        timeout,
        cancelled,
        source_config=source_config,
    )
    _command_failure(reply, "Material export")
    if not isinstance(reply, dict) or not isinstance(reply.get("records"), list):
        raise ValueError("Malformed Blender material export response")
    errors = reply.get("errors", {})
    if not isinstance(errors, dict):
        raise ValueError("Malformed Blender material export response")
    records = []
    for record in reply["records"]:
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("key"), str)
            or record.get("name") not in names
            or not isinstance(record.get("nodes"), list)
            or not isinstance(record.get("outputs"), dict)
        ):
            raise ValueError("Malformed Blender material record")
        records.append(record)
    errors = {str(name): str(message) for name, message in errors.items()}
    produced = {record["name"] for record in records}
    for name in names:
        if name not in produced and name not in errors:
            errors[name] = "Blender returned no material record for " + name
    return {"records": records, "errors": errors}


_WORLD_KINDS = ("simple", "material", "unsupported")


def _checked_world(record, names):
    """A world record from blend_world_export.export_world, or ValueError."""
    kind = record.get("kind") if isinstance(record, dict) else None
    if (
        kind not in _WORLD_KINDS
        or record.get("name") not in names
        or not isinstance(record.get("key"), str)
        or not isinstance(record.get("reasons"), list)
        or not isinstance(record.get("warnings", []), list)
        or (kind == "simple" and not isinstance((record.get("simple") or {}).get("image"), dict))
        or (kind == "material" and not isinstance((record.get("material") or {}).get("nodes"), list))
        or (kind == "unsupported" and not isinstance(record.get("unsupported"), str))
    ):
        raise ValueError("Malformed Blender world record")
    return record


def export_library_worlds(
    source, names, output_dir, executable, timeout=300, cancelled=None, textures="in_place", source_config=None
):
    """Append and export named worlds of one library file in a disposable worker.

    Returns {"records": [blend_world_export records], "errors": {name: message}}, with the
    same bounds, signature checks and per-name errors as export_library_materials. Only
    packed images are written below output_dir with textures="in_place" (the default).
    source_config: Blender's OCIO configuration, as for export_library_materials.
    """
    names = _material_names(names, "world")
    textures = _texture_mode(textures)
    if not _bounded_timeout(timeout):
        raise ValueError("World export request exceeds its bounds")
    source = str(Path(source).resolve())
    if not Path(source).is_file() or Path(source).suffix.lower() != ".blend":
        raise ValueError("Blender source must be an existing .blend file: " + source)
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    command = {
        "command": "export_library_worlds",
        "source": source,
        "names": names,
        "output_dir": str(output_dir),
        "textures": textures,
    }
    reply = _disposable_request(command, source, executable, timeout, cancelled, source_config=source_config)
    _command_failure(reply, "World export")
    if not isinstance(reply, dict) or not isinstance(reply.get("records"), list):
        raise ValueError("Malformed Blender world export response")
    errors = reply.get("errors", {})
    if not isinstance(errors, dict):
        raise ValueError("Malformed Blender world export response")
    records = [_checked_world(record, names) for record in reply["records"]]
    errors = {str(name): str(message) for name, message in errors.items()}
    produced = {record["name"] for record in records}
    for name in names:
        if name not in produced and name not in errors:
            errors[name] = "Blender returned no world record for " + name
    return {"records": records, "errors": errors}


def dump_asset_library_preferences(executable, timeout=60):
    """Read the user's Blender asset libraries (a short, non-factory Blender run).

    User preferences are needed, so --factory-startup is deliberately absent;
    --disable-autoexec still blocks scripts embedded in startup files.
    Returns [{"name", "path", "import_method", "enabled", "remote"}].
    """
    if not _bounded_timeout(timeout):
        raise ValueError("Blender preference timeout must be positive and finite")
    marker = "CH-ASSET-PREFS-" + os.urandom(16).hex() + ":"
    env = blender_environment()
    env["CH_ASSET_PREFS_MARKER"] = marker
    command = [
        str(executable),
        "--background",
        "--disable-autoexec",
        "--python-exit-code",
        "1",
        "--python",
        str(_blender_script("asset_prefs_main.py")),
    ]
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=env,
            timeout=timeout,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as error:
        raise TimeoutError(f"Blender preference read exceeded {timeout:.1f} seconds") from error
    stdout = (completed.stdout or b"").decode("utf-8", errors="replace")
    position = stdout.rfind(marker)
    if position < 0:
        diagnostic = (completed.stderr or b"").decode("utf-8", errors="replace")[-4096:]
        raise RuntimeError(
            f"Blender did not report its asset libraries (exit code {completed.returncode})\n{diagnostic}"
        )
    line = stdout[position + len(marker) :].splitlines()[0] if stdout[position + len(marker) :] else ""
    try:
        payload = json.loads(line)
    except ValueError as error:
        raise ValueError("Malformed Blender asset library report") from error
    if not isinstance(payload, dict):
        raise ValueError("Malformed Blender asset library report")
    if not payload.get("ok"):
        raise RuntimeError("Blender asset library read failed: " + str(payload.get("error", "unknown error")))
    libraries = payload.get("libraries")
    if not isinstance(libraries, list):
        raise ValueError("Malformed Blender asset library report")
    result = []
    for library in libraries:
        if not isinstance(library, dict) or not isinstance(library.get("path"), str) or not library["path"]:
            continue
        result.append(
            {
                "name": str(library.get("name") or Path(library["path"]).name),
                "path": library["path"],
                "import_method": str(library.get("import_method") or "APPEND"),
                "enabled": bool(library.get("enabled", True)),
                "remote": bool(library.get("remote", False)),
            }
        )
    return result


def _listing_name(value, what):
    if not isinstance(value, str) or not value or len(value) > _MAX_ID_NAME or "\0" in value:
        raise ValueError("Malformed Blender listing: invalid " + what + " name")
    return value


def _listing_names(values, what, limit):
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > limit:
        raise ValueError("Malformed Blender listing: invalid " + what + " list")
    return [_listing_name(value, what) for value in values]


def _listing_optional(value, what):
    return None if value is None else _listing_name(value, what)


def _listing_flag(value, default=False):
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError("Malformed Blender listing: invalid flag")
    return value


def _listing_library(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 4096 or "\0" in value:
        raise ValueError("Malformed Blender listing: invalid library path")
    return value


def checked_listing(reply):
    """Validate and normalize a worker listing reply (docs/dev/blend-import.md, "Worker listing").

    Structural errors raise ValueError. Names are unique per (name, library);
    references to collections or objects that are not listed are dropped, so
    the tree built from the result is always consistent. Optional extension
    fields (parents, in_scene, instance_collection, library) get defaults.
    """
    if not isinstance(reply, dict):
        raise ValueError("Malformed Blender listing")
    scenes, collections, objects = reply.get("scenes"), reply.get("collections"), reply.get("objects")
    if not isinstance(scenes, list) or not isinstance(collections, list) or not isinstance(objects, list):
        raise ValueError("Malformed Blender listing")
    if (
        len(scenes) > _MAX_LISTING_SCENES
        or len(collections) > _MAX_LISTING_COLLECTIONS
        or len(objects) > _MAX_LISTING_OBJECTS
    ):
        raise ValueError("Blender listing exceeds its bounds")

    clean_objects, object_names = [], set()
    for entry in objects:
        if not isinstance(entry, dict):
            raise ValueError("Malformed Blender listing: invalid object")
        name = _listing_name(entry.get("name"), "object")
        library = _listing_library(entry.get("library"))
        if (name, library) in object_names:
            continue
        object_names.add((name, library))
        kind = entry.get("type")
        if not isinstance(kind, str) or not kind or len(kind) > 64:
            raise ValueError("Malformed Blender listing: invalid object type")
        slots = entry.get("material_slots") or []
        if not isinstance(slots, list) or len(slots) > 4096:
            raise ValueError("Malformed Blender listing: invalid material slots")
        clean_slots = []
        for slot in slots:
            if not isinstance(slot, str) or len(slot) > _MAX_ID_NAME or "\0" in slot:
                raise ValueError("Malformed Blender listing: invalid material slot")
            clean_slots.append(slot)
        clean_objects.append(
            {
                "name": name,
                "type": kind,
                "parent": _listing_optional(entry.get("parent"), "parent object"),
                "collections": _listing_names(entry.get("collections"), "collection", _MAX_LISTING_COLLECTIONS),
                "has_geometry_nodes": _listing_flag(entry.get("has_geometry_nodes")),
                "material_slots": clean_slots,
                "hide_render": _listing_flag(entry.get("hide_render")),
                "instance_collection": _listing_optional(entry.get("instance_collection"), "instanced collection"),
                "library": library,
            }
        )

    clean_collections, collection_names = [], set()
    for entry in collections:
        if not isinstance(entry, dict):
            raise ValueError("Malformed Blender listing: invalid collection")
        name = _listing_name(entry.get("name"), "collection")
        library = _listing_library(entry.get("library"))
        if (name, library) in collection_names:
            continue
        collection_names.add((name, library))
        parent = _listing_optional(entry.get("parent"), "parent collection")
        parents = _listing_names(entry.get("parents"), "parent collection", _MAX_LISTING_COLLECTIONS)
        if parent is not None and parent not in parents:
            parents.insert(0, parent)
        clean_collections.append(
            {
                "name": name,
                "parent": parent if parent is not None else (parents[0] if parents else None),
                "parents": parents,
                "children": _listing_names(entry.get("children"), "collection", _MAX_LISTING_COLLECTIONS),
                "objects": _listing_names(entry.get("objects"), "object", _MAX_LISTING_OBJECTS),
                "excluded": _listing_flag(entry.get("excluded")),
                "in_scene": _listing_flag(entry.get("in_scene"), True),
                "hide_render": _listing_flag(entry.get("hide_render")),
                "library": library,
            }
        )

    known_collections = {name for name, _library in collection_names}
    known_objects = {name for name, _library in object_names}
    for entry in clean_collections:
        entry["children"] = list(dict.fromkeys(c for c in entry["children"] if c in known_collections))
        entry["objects"] = list(dict.fromkeys(o for o in entry["objects"] if o in known_objects))
        entry["parents"] = list(dict.fromkeys(p for p in entry["parents"] if p in known_collections))
        if entry["parent"] not in known_collections:
            entry["parent"] = entry["parents"][0] if entry["parents"] else None
    for entry in clean_objects:
        entry["collections"] = [c for c in entry["collections"] if c in known_collections]
        if entry["parent"] not in known_objects:
            entry["parent"] = None
        if entry["instance_collection"] not in known_collections:
            entry["instance_collection"] = None

    clean_scenes = []
    for entry in scenes:
        if not isinstance(entry, dict):
            raise ValueError("Malformed Blender listing: invalid scene")
        layers = _listing_names(entry.get("view_layers"), "view layer", 4096)
        active = entry.get("active_view_layer")
        clean_scenes.append(
            {
                "name": _listing_name(entry.get("name"), "scene"),
                "view_layers": layers,
                "active_view_layer": active if isinstance(active, str) and active in layers else (layers or [""])[0],
                "collections": [
                    c
                    for c in _listing_names(entry.get("collections"), "collection", _MAX_LISTING_COLLECTIONS)
                    if c in known_collections
                ],
                "objects": [
                    o
                    for o in _listing_names(entry.get("objects"), "object", _MAX_LISTING_OBJECTS)
                    if o in known_objects
                ],
            }
        )
    scene_names = [scene["name"] for scene in clean_scenes]
    active_scene = reply.get("active_scene")
    if active_scene not in scene_names:
        active_scene = scene_names[0] if scene_names else None

    frame_range = reply.get("frame_range")
    if (
        isinstance(frame_range, list)
        and len(frame_range) == 2
        and all(type(value) is int and abs(value) < 2**31 for value in frame_range)
    ):
        frame_range = sorted(frame_range)
    else:
        frame_range = [1, 1]
    fps = reply.get("fps")
    if not isinstance(fps, (int, float)) or isinstance(fps, bool) or not math.isfinite(fps) or not 0 < fps <= 10000:
        fps = 24.0
    result = {
        "active_scene": active_scene,
        "scenes": clean_scenes,
        "collections": clean_collections,
        "objects": clean_objects,
        "frame_range": frame_range,
        "fps": float(fps),
    }
    signature = reply.get("signature")
    if isinstance(signature, list) and len(signature) == 4 and all(type(v) is int for v in signature):
        result["signature"] = list(signature)
    return result


def list_blend_contents(source, executable, timeout=60, cancelled=None):
    """Collections, objects, scenes, frame range and fps of one .blend (disposable worker, read-only).

    The file is opened without UI or scripts and never saved. Returns the
    normalized listing of `checked_listing` plus "source" (resolved path).
    """
    if not _bounded_timeout(timeout):
        raise ValueError("Blender listing timeout must be positive and finite")
    source = str(Path(source).resolve())
    if not Path(source).is_file() or Path(source).suffix.lower() != ".blend":
        raise ValueError("Blender source must be an existing .blend file: " + source)
    reply = _disposable_request(
        {"command": "list_blend_contents", "source": source}, source, executable, timeout, cancelled
    )
    _command_failure(reply, "Blender file listing")
    result = checked_listing(reply)
    if "signature" in result and _signature(source) != tuple(result["signature"]):
        raise StaleEvaluationError("Blender file changed during listing: " + source)
    result["source"] = source
    return result


def prewarm(source, executable, timeout=300, source_config=None, cancelled=None):
    """Start a source worker without evaluating geometry (safe off Houdini's thread)."""
    source = str(Path(source).resolve())
    key = (os.path.normcase(source), str(Path(executable).resolve()), str(source_config or ""))
    with _epoch_lock:
        _epochs.setdefault(key[0], 0)
    with _pool_lock:
        worker = _workers.get(key)
        if (
            worker is None
            or worker.process.poll() is not None
            or worker.launch_signature != launch_signature(executable, source_config)
        ):
            if worker is not None:
                _workers.pop(key).close()
            worker = _Worker(source, executable, source_config, timeout, cancelled)
            _workers[key] = worker
        _workers.move_to_end(key)
        while len(_workers) > _MAX_WORKERS:
            old_key = next(k for k in _workers if k != key)
            old_worker = _workers[old_key]
            with old_worker.lock:
                _workers.pop(old_key).close()
        return worker.process.pid


def discard_prewarm(source, executable, source_config=None):
    key = (os.path.normcase(str(Path(source).resolve())), str(Path(executable).resolve()), str(source_config or ""))
    with _pool_lock:
        worker = _workers.get(key)
        if worker is not None and not worker.claimed:
            with worker.lock:
                _workers.pop(key).close()


def invalidate(source=None):
    """Discard workers for a changed source, or all workers when source is None."""
    normalized = os.path.normcase(str(Path(source).resolve())) if source is not None else None
    with _epoch_lock:
        affected = set(_epochs) | {key[0] for key in _workers} if normalized is None else {normalized}
        for key in affected:
            _epochs[key] = _epochs.get(key, 0) + 1
    with _pool_lock:
        for key, worker in list(_workers.items()):
            if normalized is None or key[0] == normalized:
                with worker.lock:
                    _workers.pop(key).close()


def shutdown_all():
    invalidate()


atexit.register(shutdown_all)
