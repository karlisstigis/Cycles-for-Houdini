# SPDX-License-Identifier: Apache-2.0
"""Bounded node-owned bounds persistence; host calls stay on the main thread.

SOP cooks only subscribe and enqueue validation. Saved boxes are viewport-only
until a filesystem-only worker validates their dependencies. Render preparation
and explicit refresh notify these subscribers, so HIP saving needs no evaluation.
Headless callers call drain_pending_node_bounds() after background jobs finish.
"""

import hashlib
import json
import math
import os
import queue
import threading
import time

from .blend_targets import INCLUDE_DISABLED_COLLECTIONS, reference_kind

MAX_NODE_BYTES = 1024 * 1024
MAX_RECORDS = 128
MAX_RECORD_BYTES = 128 * 1024
MAX_DEPENDENCIES = 256
_subscribers = {}
_completed = queue.Queue(maxsize=128)
_publications = {}
_publication_lock = threading.Lock()
MAX_SUBSCRIBERS = 1024
_pending = queue.Queue(maxsize=128)
_worker = None
_poll_installed = False
_epoch = 0
_hip_root = None
_relocated = False
_shape_subscribers = {}
_shape_pending = queue.Queue(maxsize=128)
_shape_completed = queue.Queue(maxsize=128)
_shape_worker = None


def decode_records(text):
    """Reject malformed/unbounded envelopes before any filesystem work."""
    if not isinstance(text, str) or len(text.encode()) > MAX_NODE_BYTES:
        return []
    try:
        envelope = json.loads(text)
        records = envelope["records"]
        if type(envelope["schema_version"]) is not int or envelope["schema_version"] != 1:
            return []
        if not isinstance(records, list) or len(records) > MAX_RECORDS:
            return []
        accepted, keys = [], set()
        for record in records:
            if not valid_record(record):
                return []
            key = record["reference"], record["frame"]
            if key in keys:
                return []
            keys.add(key)
            accepted.append(record)
        return accepted
    except (ValueError, TypeError, KeyError, OverflowError):
        return []


def valid_record(record):
    try:
        if not isinstance(record, dict) or len(json.dumps(record, allow_nan=False).encode()) > MAX_RECORD_BYTES:
            return False
        frame, box, deps = record["frame"], record["bounds"], record["dependency_versions"]
        kind = {"OBJECT": "object", "COLLECTION": "collection"}.get(record["selector_type"])
        # A record evaluated with excluded child collections belongs only to the
        # path.blend#collection+disabled=Name spelling (see blend_targets).
        disabled = record.get("include_excluded_collections", False)
        if disabled is not False:
            kind = INCLUDE_DISABLED_COLLECTIONS if disabled is True and kind == "collection" else None
        suffix = "#" + kind + "=" + record["selector"] if kind else ""
        return (
            type(record["schema_version"]) is int
            and record["schema_version"] == 1
            and record["known"] is True
            and type(frame) in (int, float)
            and math.isfinite(frame)
            and isinstance(record["reference"], str)
            and len(record["reference"]) <= 32768
            and suffix
            and record["selector"]
            and record["reference"].endswith(suffix)
            and record["reference"][: -len(suffix)].lower().endswith(".blend")
            and isinstance(box, list)
            and len(box) == 6
            and all(type(v) in (int, float) and math.isfinite(v) for v in box)
            and all(box[i] <= box[i + 3] for i in range(3))
            and isinstance(record["source_identity"], str)
            and len(record["source_identity"]) == 64
            and all(c in "0123456789abcdef" for c in record["source_identity"])
            and isinstance(deps, dict)
            and 0 < len(deps) <= MAX_DEPENDENCIES
            and record["source"] in deps
            and all(
                isinstance(p, str)
                and 0 < len(p) <= 32768
                and "\0" not in p
                and isinstance(v, list)
                and len(v) == 4
                and all(type(i) is int for i in v)
                for p, v in deps.items()
            )
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def encode_records(records):
    records = list(records)[-MAX_RECORDS:]
    while records:
        encoded = json.dumps(
            dict(schema_version=1, records=records), sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        if len(encoded.encode()) <= MAX_NODE_BYTES:
            return encoded
        records.pop(0)
    return '{"schema_version":1,"records":[]}'


def _canonical(path):
    return os.path.normcase(os.path.abspath(path))


def validate_record(record, resolved_source):
    """Plain filesystem-only worker contract; no hou, native SDK or Blender."""
    if not valid_record(record) or _canonical(record["source"]) != _canonical(resolved_source):
        return False
    signatures = record.get("native_signatures", {})
    if os.name == "nt" and (not isinstance(signatures, dict) or set(signatures) != set(record["dependency_versions"])):
        return False
    for path, expected in record["dependency_versions"].items():
        try:
            stat = os.stat(path)
            observed = [stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino]
        except OSError:
            return False
        if observed != expected:
            return False
        if os.name == "nt":
            from .file_signature import native_signature

            if not isinstance(signatures[path], str) or native_signature(path) != signatures[path]:
                return False
    return True


def _run():
    while True:
        request = _pending.get()
        ident, epoch, fingerprint, token, records, resolved = request
        result = [
            (record, validate_record(record, resolved.get((record["reference"], record["frame"]), "")))
            for record in records
        ]
        _completed.put((ident, epoch, fingerprint, token, result))
        _pending.task_done()


def _start_worker():
    global _worker
    if _worker is None:
        _worker = threading.Thread(target=_run, daemon=True, name="CH saved bounds validation")
        _worker.start()


def _shape_run():
    while True:
        ident, epoch, token, records, resolved = _shape_pending.get()
        results, checked = [], {}
        for record in records:
            key = record["reference"], record["frame"]
            source = resolved.get(record["reference"], "")
            identity = record["source_identity"], source
            if identity not in checked:
                base = {k: v for k, v in record.items() if k not in ("viewport_hulls", "viewport_hull_reason")}
                checked[identity] = validate_record(base, source)
            results.append((key, record["source_identity"], checked[identity]))
        _shape_completed.put((ident, epoch, token, results))
        _shape_pending.task_done()


def _queue_shape_validation(ident, state):
    global _shape_worker
    if _shape_worker is None:
        _shape_worker = threading.Thread(target=_shape_run, daemon=True, name="CH saved shape validation")
        _shape_worker.start()
    records = [r for r in state["records"] if r["reference"] in state["resolved"]]
    if not records:
        return
    try:
        _shape_pending.put_nowait((ident, _epoch, state["token"], records, state["resolved"]))
        state["pending"] = True
    except queue.Full:
        pass


def _cook_shape_node(node):
    from . import blend_preview

    try:
        parm = node.parm("saved_viewport_hulls")
    except KeyError:
        return
    if parm is None:
        return
    import hou

    rows = blend_preview.preview_rows(node)
    resolved = {}
    for reference, _, path in rows:
        source = hou.expandString(path)
        if not os.path.isabs(source):
            source = os.path.join(hou.expandString("$HIP"), source)
        resolved[reference] = _canonical(source)
    text = parm.unexpandedString()
    fingerprint = hashlib.sha256(repr((rows, sorted(resolved.items()), text, _epoch)).encode()).hexdigest()
    ident = node.sessionId()
    old = _shape_subscribers.get(ident)
    if old is not None and old["fingerprint"] == fingerprint:
        return
    if len(_shape_subscribers) >= MAX_SUBSCRIBERS and ident not in _shape_subscribers:
        for key in list(_shape_subscribers):
            if hou.nodeBySessionId(key) is None:
                _shape_subscribers.pop(key, None)
        if len(_shape_subscribers) >= MAX_SUBSCRIBERS:
            return
    state = dict(
        node=node,
        fingerprint=fingerprint,
        token=object(),
        records=blend_preview.decode_records(text),
        resolved=resolved,
        pending=False,
        last_checked=0,
    )
    _shape_subscribers[ident] = state
    _queue_shape_validation(ident, state)


def _drain_shape_validation(*, recook=True):
    import hou
    from .. import _native
    from . import blend_preview

    dirty = set()
    while not _shape_completed.empty():
        ident, epoch, token, results = _shape_completed.get()
        state = _shape_subscribers.get(ident)
        if epoch != _epoch or state is None or state["token"] is not token:
            continue
        node = hou.nodeBySessionId(ident)
        if node is None or node != state["node"]:
            _shape_subscribers.pop(ident, None)
            continue
        _cook_shape_node(node)
        if _shape_subscribers.get(ident) is not state:
            continue
        state["pending"], state["last_checked"] = False, time.monotonic()
        stale = {(key[0], key[1], identity) for key, identity, valid in results if not valid}
        for reference, frame, identity in stale:
            _native.invalidate_blender_reference_preview(reference, frame, identity)
        if stale:
            state["records"] = [
                r for r in state["records"] if (r["reference"], r["frame"], r["source_identity"]) not in stale
            ]
            _set(node, "saved_viewport_hulls", blend_preview.encode_records(state["records"]))
            dirty.add(ident)
    for ident, state in list(_shape_subscribers.items()):
        if hou.nodeBySessionId(ident) is None:
            _shape_subscribers.pop(ident, None)
        elif not state["pending"] and time.monotonic() - state["last_checked"] >= 2:
            _queue_shape_validation(ident, state)
    if recook:
        for ident in dirty:
            node = hou.nodeBySessionId(ident)
            if node is not None:
                node.cook(force=True)


def notify_publication(reference, frame):
    """Thread-safe notification only. Native/HOM reads happen when drained."""
    with _publication_lock:
        key = reference, float(frame)
        if len(_publications) >= 1024 and key not in _publications:
            _publications.pop(next(iter(_publications)))
        _publications[key] = _epoch


def _rows(node):
    from .blend_catalogue import resolved_reference_rows

    result = []
    for row in resolved_reference_rows(node)[0]:
        file = row["file"]
        kind = row.get("kind", row.get("target_kind", "object"))
        target = row["target"]
        frame = float(row["frame"])
        if not file or not target or kind not in ("object", "collection") or not math.isfinite(frame):
            continue
        kind = reference_kind(kind, row.get("include_disabled_collections", False))
        result.append((file + "#" + kind + "=" + target, frame, file))
    return result


def _set(node, name, value):
    import hou

    parm = node.parm(name)
    if parm is not None and parm.unexpandedString() != value:
        with hou.undos.disabler():
            parm.set(value)
        return True
    return False


def context_epoch():
    return _epoch


def _synchronize_context(hou):
    global _hip_root, _relocated, _epoch
    current_root = hou.expandString("$HIP")
    changed = _hip_root is not None and _canonical(current_root) != _canonical(_hip_root)
    _hip_root = current_root
    if changed:
        from .. import _native

        # Called at the end of SOP cook, before geometry can be consumed. Even
        # leases attached earlier in that cook lose rendering authority now.
        _native.clear_blender_reference_bounds()
        _epoch += 1
        _relocated = True
        with _publication_lock:
            _publications.clear()
        for state in _subscribers.values():
            state["pending"] = False
            state["fingerprint"] = ""
        for state in _shape_subscribers.values():
            state["pending"] = False
            state["fingerprint"] = ""
    return changed


def cook_node(node):
    """Idempotent end-of-cook hook. Does not request node geometry or evaluate Blender."""
    import hou

    global _poll_installed
    _synchronize_context(hou)
    _cook_shape_node(node)
    rows = _rows(node)
    keys = {(ref, frame) for ref, frame, _ in rows}
    resolved = {}
    for reference, frame, path in rows:
        expanded = hou.expandString(path)
        if not os.path.isabs(expanded):
            expanded = os.path.join(hou.expandString("$HIP"), expanded)
        resolved[reference, frame] = _canonical(expanded)
    parm = node.parm("saved_bounds")
    text = parm.unexpandedString() if parm is not None else ""
    fingerprint = hashlib.sha256(repr((rows, sorted(resolved.items()), text, _epoch)).encode()).hexdigest()
    ident = node.sessionId()
    old = _subscribers.get(ident)
    if old is None or old["fingerprint"] != fingerprint:
        if len(_subscribers) >= MAX_SUBSCRIBERS and ident not in _subscribers:
            for old_ident in list(_subscribers):
                if hou.nodeBySessionId(old_ident) is None:
                    _subscribers.pop(old_ident, None)
            if len(_subscribers) >= MAX_SUBSCRIBERS:
                _set(node, "saved_bounds_status", "Bounds subscription budget exceeded (1024 live nodes)")
                return
        state = dict(
            node=node,
            token=object(),
            fingerprint=fingerprint,
            keys=keys,
            records=decode_records(text),
            resolved=resolved,
            pending=False,
            last_checked=0,
        )
        _subscribers[ident] = state
        records = [r for r in state["records"] if (r["reference"], r["frame"]) in keys]
        if records:
            _start_worker()
            try:
                _pending.put_nowait((ident, _epoch, fingerprint, state["token"], records, resolved))
                state["pending"] = True
            except queue.Full:
                state["fingerprint"] = ""  # next cook retries; never accumulate jobs
    # Reopening another HIP invalidates raw-reference cache publications.
    if _hip_event not in hou.hipFile.eventCallbacks():
        hou.hipFile.addEventCallback(_hip_event)
    if hou.isUIAvailable() and not _poll_installed:
        hou.ui.addEventLoopCallback(drain_pending_node_bounds)
        _poll_installed = True


def drain_pending_node_bounds(*, publications_only=False, recook=True):
    """Main-thread pump, also available to headless refresh/test callers."""
    import hou
    from .. import _native

    if not publications_only:
        _drain_shape_validation(recook=recook)

    dirty = set()
    while not publications_only and not _completed.empty():
        ident, epoch, fingerprint, token, results = _completed.get()
        state = _subscribers.get(ident)
        if epoch != _epoch or state is None or state["fingerprint"] != fingerprint or state["token"] is not token:
            continue
        node = hou.nodeBySessionId(ident)
        if node is None or node != state["node"]:
            _subscribers.pop(ident, None)
            continue
        cook_node(node)
        if _subscribers.get(ident) is not state:
            continue
        state["pending"] = False
        state["last_checked"] = time.monotonic()
        changed = False
        valid_keys, stale_keys = set(), set()
        validated_records = {}
        for record, valid in results:
            key = record["reference"], record["frame"]
            if not valid:
                # A failed old filesystem check cannot retire a newer source
                # revision published while that check was in flight.
                current = _native.serialized_blender_reference_bounds(*key)
                resident = json.loads(current) if current else {}
                if valid_record(resident) and resident["source_identity"] != record["source_identity"]:
                    record, valid = resident, True
            if valid:
                try:
                    current = _native.serialized_blender_reference_bounds(*key)
                    resident = json.loads(current) if current else {}
                    # Validation may have started before a hull refresh enriched
                    # this evaluation (or a newer identity at the same sample).
                    # Validation restores saved metadata only when no current
                    # authoritative publication exists; it never downgrades one.
                    if valid_record(resident):
                        record = resident
                    elif resident != record:
                        _native.publish_blender_reference_bounds(*key, json.dumps(record, allow_nan=False))
                        changed = True
                    validated_records[key] = record
                    valid_keys.add(key)
                except Exception:
                    stale_keys.add(key)
            else:
                stale_keys.add(key)
                _native.publish_blender_reference_bounds(
                    *key, json.dumps(dict(known=False, reference=key[0], frame=key[1]))
                )
        # Stale persisted boxes are removed; no subsequent cook can reinstate them.
        state["records"] = [
            validated_records.get((r["reference"], r["frame"]), r)
            for r in state["records"]
            if (r["reference"], r["frame"]) not in stale_keys
        ]
        _set(node, "saved_bounds", encode_records(state["records"]))
        _set(node, "saved_bounds_status", "stale" if stale_keys else "validated" if valid_keys else "unknown")
        if changed or stale_keys:
            dirty.add(ident)
    with _publication_lock:
        publications = {key for key, epoch in _publications.items() if epoch == _epoch}
        _publications.clear()
    for ident, state in list(_subscribers.items()):
        if hou.nodeBySessionId(ident) is None:
            _subscribers.pop(ident, None)
            continue
        # Recheck external dependencies without polling the filesystem on the UI
        # thread. Repeated unchanged validation does not force geometry recooks.
        if not publications_only and not state["pending"] and time.monotonic() - state["last_checked"] >= 2:
            records_to_check = [r for r in state["records"] if (r["reference"], r["frame"]) in state["keys"]]
            if records_to_check:
                try:
                    _pending.put_nowait(
                        (ident, _epoch, state["fingerprint"], state["token"], records_to_check, state["resolved"])
                    )
                    state["pending"] = True
                except queue.Full:
                    pass
        relevant = state["keys"] & publications
        if not relevant:
            continue
        node = hou.nodeBySessionId(ident)
        if node is None or node != state["node"]:
            _subscribers.pop(ident, None)
            continue
        # Re-read current rows to reject a late render result after a parm edit.
        current = {(r, f) for r, f, _ in _rows(node)}
        records = {(r["reference"], r["frame"]): r for r in state["records"]}
        previous_records = dict(records)
        for key in relevant & current:
            payload = _native.serialized_blender_reference_bounds(*key)
            if payload:
                record = json.loads(payload)
                resolved = hou.expandString(next(path for ref, frame, path in _rows(node) if (ref, frame) == key))
                if not os.path.isabs(resolved):
                    resolved = os.path.join(hou.expandString("$HIP"), resolved)
                if valid_record(record) and _canonical(record["source"]) == _canonical(resolved):
                    records[key] = record
            else:
                records.pop(key, None)
        state["records"] = list(records.values())
        _set(node, "saved_bounds", encode_records(state["records"]))
        status_changed = _set(node, "saved_bounds_status", "validated" if current & records.keys() else "unknown")
        # Cached render preparation republishes the same immutable record.
        # Its notification must not invalidate SOP data and queue another IPR
        # capture. Changed boxes and preview/authority transitions still cook.
        if records != previous_records or status_changed:
            dirty.add(ident)
    for ident in dirty if recook else ():
        node = hou.nodeBySessionId(ident)
        if node is not None:
            node.cook(force=True)


def _hip_event(event):
    import hou

    global _epoch, _hip_root, _relocated
    if event in (hou.hipFileEventType.BeforeClear, hou.hipFileEventType.BeforeLoad):
        _epoch += 1
        _subscribers.clear()
        _shape_subscribers.clear()
        _hip_root = None
        with _publication_lock:
            _publications.clear()
    elif event in (hou.hipFileEventType.BeforeSave, hou.hipFileEventType.AfterSave):
        _synchronize_context(hou)
        if event == hou.hipFileEventType.BeforeSave:
            # Drain accepted publications only; never wait, stat or evaluate.
            drain_pending_node_bounds(publications_only=True, recook=False)
        elif _relocated:
            _relocated = False
            for ident in list(_subscribers):
                node = hou.nodeBySessionId(ident)
                if node is not None:
                    node.cook(force=True)
