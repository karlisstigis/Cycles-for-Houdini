# SPDX-License-Identifier: Apache-2.0
"""Node-owned cosmetic Blender previews; render references retain Source Frame."""

import json
import math

from .blend_viewport import DEFAULT_HULL_VERTICES, hull_vertex_limit, validate_viewport_hulls

MAX_RECORD_BYTES = 2 * 1024 * 1024 + 128 * 1024
MAX_NODE_BYTES = 16 * 1024 * 1024
MAX_RECORDS = 128


def settings(node, time=None):
    def value(name, default):
        try:
            parm = node.parm(name)
        except KeyError:
            parm = None
        return (parm.evalAtTime(time) if time is not None else parm.eval()) if parm is not None else default

    frame = float(value("shape_frame", 1))
    if not math.isfinite(frame):
        raise ValueError("Shape Frame must be finite")
    return (
        frame,
        hull_vertex_limit(value("max_hull_vertices", DEFAULT_HULL_VERTICES)),
        value("viewport_shape", "bounds"),
    )


def preview_rows(node, time=None):
    from .blend_catalogue import resolved_reference_rows
    from .blend_targets import row_reference

    frame = settings(node, time)[0]
    return list(
        dict.fromkeys(
            (row_reference(row), frame, row["file"])
            for row in resolved_reference_rows(node, time)[0]
            if row["file"] and row["target"]
        )
    )


def valid_record(record):
    from .node_bounds import valid_record as valid_bounds

    if not isinstance(record, dict):
        return False
    base = {key: value for key, value in record.items() if key not in ("viewport_hulls", "viewport_hull_reason")}
    try:
        if len(
            json.dumps(record, separators=(",", ":"), allow_nan=False).encode()
        ) > MAX_RECORD_BYTES or not valid_bounds(base):
            return False
        cap = hull_vertex_limit(record["max_hull_vertices"])
        hulls = record.get("viewport_hulls")
        return hulls is None or (validate_viewport_hulls(hulls) and hulls.get("max_vertices") == cap)
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def decode_records(text):
    try:
        if not isinstance(text, str) or len(text.encode()) > MAX_NODE_BYTES:
            return []
        envelope = json.loads(text)
        records = envelope["records"]
        if (
            type(envelope.get("schema_version")) is not int
            or envelope["schema_version"] != 1
            or not isinstance(records, list)
            or len(records) > MAX_RECORDS
        ):
            return []
        if not all(map(valid_record, records)):
            return []
        return records if len({record_key(record) for record in records}) == len(records) else []
    except (ValueError, TypeError, KeyError):
        return []


def encode_records(records):
    records = list(records)[-MAX_RECORDS:]
    while records:
        text = json.dumps(dict(schema_version=1, records=records), separators=(",", ":"), allow_nan=False)
        if len(text.encode()) <= MAX_NODE_BYTES:
            return text
        records.pop(0)
    return '{"schema_version":1,"records":[]}'


def record_key(record):
    return record["reference"], float(record["frame"]), record["max_hull_vertices"]


def saved_records(node):
    try:
        parm = node.parm("saved_viewport_hulls")
    except KeyError:
        parm = None
    return decode_records(parm.unexpandedString()) if parm else []


def matching_record(node, reference, time=None):
    from .blend_catalogue import _path

    frame, cap, _ = settings(node, time)
    for record in reversed(saved_records(node)):
        if record_key(record) != (reference, frame, cap):
            continue
        source = reference.rsplit("#", 1)[0]
        if _path(source) != _path(record["source"]):
            continue
        from .. import _native

        valid = getattr(_native, "blender_reference_preview_valid", None)
        if valid is not None and not valid(reference, frame, record["source_identity"]):
            continue
        return record
    return None


def persist(node, records):
    previous = {record_key(record): record for record in saved_records(node)}
    previous.update((record_key(record), record) for record in records if valid_record(record))
    text = encode_records(previous.values())
    parm = node.parm("saved_viewport_hulls")
    if parm.unexpandedString() != text:
        import hou

        with hou.undos.disabler():
            parm.set(text)


def has_current(node):
    _, _, shape = settings(node)
    for reference, _, _ in preview_rows(node):
        record = matching_record(node, reference)
        if record is None or (shape == "hull" and not validate_viewport_hulls(record.get("viewport_hulls"))):
            return False
    return True
