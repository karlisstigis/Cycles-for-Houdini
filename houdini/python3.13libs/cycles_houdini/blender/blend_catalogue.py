# SPDX-License-Identifier: Apache-2.0
"""Cached target menus and explicit, asynchronous Blender ID catalogues.

Menu reads and HIP reopen only read saved JSON. All filesystem probes and Blender
work happen in a single cancellable background job. HOM stays on its caller's
thread; the UI event loop publishes completed results after a generation check.
"""

from collections import OrderedDict
import json
import hashlib
import os
import threading
import time

_MAX_BYTES = 1024 * 1024
_MAX_CACHE_BYTES = 8 * 1024 * 1024
_cache = OrderedDict()
_cache_times = {}
_cache_sizes = {}
_jobs = {}
_generation = 0
_lock = threading.Lock()
_active = None
_pending = OrderedDict()
_poller = False


def _valid_record(record, source):
    if not isinstance(record, dict) or not isinstance(record.get("source"), str) or "\0" in record["source"]:
        return False
    if os.path.normcase(os.path.abspath(record["source"])) != source:
        return False
    signature = record.get("signature")
    if not isinstance(signature, (list, tuple)) or len(signature) != 4 or any(type(v) is not int for v in signature):
        return False
    if len(json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > _MAX_BYTES:
        return False
    return all(
        isinstance(record.get(kind), list)
        and len(record[kind]) <= 10000
        and all(
            isinstance(name, str) and "\0" not in name and len(name.encode("utf-8")) <= 65536 for name in record[kind]
        )
        for kind in ("objects", "collections")
    )


def _saved_text(parm):
    try:
        return parm.unexpandedString()
    except AttributeError:
        return parm.evalAsString()


def _read(node):
    parm = node.parm("saved_catalogue")
    try:
        text = _saved_text(parm) if parm else ""
        value = json.loads(text) if len(text.encode()) <= _MAX_BYTES and text else {}
        if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
            return {}
        files = value.get("files", {})
        if not isinstance(files, dict) or len(files) > 128:
            return {}
        return {
            source: record
            for source, record in files.items()
            if isinstance(source, str) and _valid_record(record, source)
        }
    except (ValueError, TypeError, AttributeError, UnicodeError):
        return {}


def _path(value):
    import hou

    # Resolve variables only; never stat or discover Blender on menu/cook paths.
    if not value:
        return ""
    expanded = hou.text.expandString(value)
    if not os.path.isabs(expanded):
        expanded = os.path.join(hou.text.expandString("$HIP"), expanded)
    return os.path.normcase(os.path.abspath(expanded))


def _reference_file(node, token, time=None):
    try:
        parm = node.parm(token)
    except KeyError:
        return node.evalParm(token)
    try:
        raw = parm.unexpandedString()
        return (parm.evalAsStringAtTime(time) if time is not None else parm.evalAsString()) if "`" in raw else raw
    except Exception:
        return parm.evalAsString()


def reference_rows(node, time=None):
    """Snapshot active rows on Houdini's thread. Disabled rows are not evaluated."""
    evaluate = node.evalParm if time is None else lambda name: node.parm(name).evalAtTime(time)
    shared = _reference_file(node, "file", time)
    if not evaluate("multiple_references"):
        return [
            dict(
                file=shared,
                kind=evaluate("target_kind"),
                target=evaluate("target"),
                frame=evaluate("frame"),
                name="reference",
            )
        ]
    rows = []
    count = evaluate("references")
    if count > 129:
        raise ValueError("Multiple References accepts at most 128 rows")
    for i in range(1, count + 1):
        if not evaluate(f"ref_enabled{i}"):
            continue
        rows.append(
            dict(
                file=_reference_file(node, f"ref_file{i}", time) or shared,
                kind=evaluate(f"ref_target_kind{i}"),
                target=evaluate(f"ref_target{i}"),
                frame=evaluate(f"ref_frame{i}"),
                name=evaluate(f"ref_name{i}"),
            )
        )
        try:
            hull = node.parm(f"ref_hull_per_member{i}")
        except KeyError:
            hull = None
        rows[-1]["hull_per_member"] = (
            bool(hull.evalAtTime(time) if time is not None else hull.eval()) if hull else False
        )
        # Only collection rows can carry the checkbox; object rows never read it.
        disabled = None
        if rows[-1]["kind"] == "collection":
            try:
                disabled = node.parm(f"ref_include_disabled_collections{i}")
            except KeyError:
                disabled = None
        rows[-1]["include_disabled_collections"] = (
            bool(disabled.evalAtTime(time) if time is not None else disabled.eval()) if disabled else False
        )
    return rows


def cached_menu(node, token="target"):
    """No Blender, subprocess, discovery, stat, or background scheduling here."""
    if node is None:
        return []
    suffix = token[len("ref_target") :] if token.startswith("ref_target") else ""
    file = node.evalParm("file")
    kind = node.evalParm("target_kind")
    if suffix.isdigit():
        file = node.evalParm("ref_file" + suffix) or file
        kind = node.evalParm("ref_target_kind" + suffix)
    record = _read(node).get(_path(file), {})
    values = record.get("collections" if kind == "collection" else "objects", [])
    return [value for value in values[:10000] if isinstance(value, str)]


def cached_menu_entries(node, token="target"):
    from .blend_targets import menu_entries

    return menu_entries(cached_menu(node, token))


def _binding_key(file, kind):
    return json.dumps([_path(file), kind], ensure_ascii=False, separators=(",", ":"))


def _channel_fingerprint(parm):
    """Track authored channel state, independent of its evaluated frame value."""
    try:
        state = parm.asCode().replace(parm.path(), "<target>")
    except (AttributeError, TypeError):
        state = _saved_text(parm)
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def _edited_legacy_rows(node):
    """Leave an audit trail while making an edited legacy row use new syntax."""
    parm = node.parm("saved_reference_migration")
    if parm is None or not _saved_text(parm):
        return
    try:
        record = json.loads(_saved_text(parm))
        fingerprints = record.get("legacy_target_fingerprints", {})
        changed = False
        for i in range(1, node.evalParm("references") + 1):
            if not node.evalParm(f"ref_enabled{i}"):
                continue
            name = node.evalParm(f"ref_name{i}")
            if name in fingerprints and _channel_fingerprint(node.parm(f"ref_target{i}")) != fingerprints[name]:
                record.setdefault("edited_legacy_names", []).append(name)
                node.parm(f"ref_name{i}").set("")
                changed = True
        if changed:
            parm.set(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
    except (ValueError, TypeError, AttributeError):
        return


def _bindings(node):
    try:
        parm = node.parm("saved_target_bindings")
        text = _saved_text(parm) if parm else ""
        value = json.loads(text) if text and len(text.encode()) <= _MAX_BYTES else {}
        if value.get("schema_version") != 1 or not isinstance(value.get("bindings"), dict):
            return {}
        result = value["bindings"]
        if len(result) > 128 or any(
            not isinstance(k, str)
            or not isinstance(v, dict)
            or len(v) > 10000
            or any(not isinstance(a, str) or not isinstance(b, str) or "\0" in a + b for a, b in v.items())
            for k, v in result.items()
        ):
            return {}
        return result
    except (ValueError, TypeError, AttributeError, KeyError):
        return {}


def bind_targets(node):
    """Selection callback: persist unique aliases; existing bindings never retarget."""
    from .blend_targets import alias, _tokens

    try:
        parm = node.parm("saved_target_bindings")
    except KeyError:
        return
    if parm is None:
        return
    bindings, saved = _bindings(node), _read(node)
    for row in reference_rows(node):
        names = saved.get(_path(row["file"]), {}).get("collections" if row["kind"] == "collection" else "objects", [])
        key = _binding_key(row["file"], row["kind"])
        mapping = bindings.setdefault(key, {})
        try:
            for token, exact, exclude in _tokens(row["target"]):
                if exact or exclude or token in mapping or any(c in token for c in "*?[]"):
                    continue
                matches = [name for name in names if token in (name, alias(name))]
                if len(matches) == 1:
                    mapping[token] = matches[0]
        except ValueError:
            continue
    encoded = json.dumps(dict(schema_version=1, bindings=bindings), ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode()) <= _MAX_BYTES and _saved_text(parm) != encoded:
        parm.set(encoded)


def resolved_reference_rows(node, time=None):
    """Expand saved catalogue patterns only; no filesystem or Blender work."""
    from .blend_targets import resolve_targets
    import hou

    saved, bindings, result, warnings = _read(node), _bindings(node), [], []
    try:
        migration = json.loads(_saved_text(node.parm("saved_reference_migration")) or "{}")
    except (ValueError, TypeError, AttributeError, KeyError):
        migration = {}
    legacy_names = migration.get("legacy_names", []) if isinstance(migration, dict) else []
    if isinstance(migration, dict) and migration.get("warning"):
        warnings.append(migration["warning"])
    status = node.parm("catalogue_status")
    if (
        status is not None
        and status.evalAsString()
        and status.evalAsString() != "Target catalogue ready"
        and not status.evalAsString().startswith("Reading ")
    ):
        warnings.append(status.evalAsString())
    legacy = not (
        node.evalParm("multiple_references") if time is None else node.parm("multiple_references").evalAtTime(time)
    )
    fingerprints = migration.get("legacy_target_fingerprints", {}) if isinstance(migration, dict) else {}
    # Disabled rows must not be evaluated just to locate an active channel.
    active_parms = {}
    if not legacy and fingerprints:
        count = node.parm("references").evalAtTime(time) if time is not None else node.evalParm("references")
        for i in range(1, count + 1):
            enabled = (
                node.parm(f"ref_enabled{i}").evalAtTime(time) if time is not None else node.evalParm(f"ref_enabled{i}")
            )
            if not enabled:
                continue
            name_parm = node.parm(f"ref_name{i}")
            name = name_parm.evalAtTime(time) if time is not None else node.evalParm(f"ref_name{i}")
            if name:
                active_parms[name] = node.parm(f"ref_target{i}")
    for row in reference_rows(node, time):
        if not row["file"] or not row["target"]:
            continue
        original = row["name"] and (row["name"] in legacy_names or not migration)
        if original and row["name"] in fingerprints:
            original = _channel_fingerprint(active_parms[row["name"]]) == fingerprints[row["name"]]
        if legacy or original:
            result.append(row)
            continue
        names = saved.get(_path(row["file"]), {}).get("collections" if row["kind"] == "collection" else "objects", [])
        matcher = getattr(hou, "patternMatch", None)
        targets, messages = resolve_targets(
            row["target"], names, matcher, bindings.get(_binding_key(row["file"], row["kind"]), {})
        )
        warnings.extend(messages)
        for target in targets:
            # Legacy explicit piece names remain stable for single-target rows.
            result.append(
                dict(
                    row,
                    target=target,
                    name=(row["name"] if len(targets) == 1 and original else "") or target.replace(" ", "_"),
                )
            )
        if len(result) > 128:
            raise ValueError("Blender Reference accepts at most 128 resolved targets")
    if len(result) > 128:
        raise ValueError("Blender Reference accepts at most 128 resolved targets")
    from .blend_targets import row_reference_kind

    outputs = {}
    for row in result:
        outputs.setdefault(row["name"], []).append(row_reference_kind(row) + ":" + row["target"])
    for name, originals in outputs.items():
        if len(originals) > 1:
            warnings.append(
                "Output name " + repr(name) + " is shared by Blender targets: " + ", ".join(map(repr, originals))
            )
    return result, list(dict.fromkeys(warnings))


def resolve_node_references(node, time=None):
    """Native STRING_ARRAY: error/warning, then ten fields per render reference."""
    try:
        from . import blend_preview
        from .blend_targets import row_reference, row_reference_kind
        from .blend_viewport import validate_viewport_hulls

        rows, warnings = resolved_reference_rows(node, time)
        shape_frame, _, shape = blend_preview.settings(node, time)
        wire = node.parm("viewport_wireframe")
        wireframe = bool(wire.evalAtTime(time) if time is not None else wire.eval()) if wire else True
        result = ["", ""]
        for row in rows:
            reference = row_reference(row)
            record = blend_preview.matching_record(node, reference, time)
            per_member = row["kind"] == "collection" and row.get("hull_per_member", False)
            hull_json = ""
            if shape == "hull":
                hulls = record.get("viewport_hulls") if record else None
                if validate_viewport_hulls(hulls):
                    hull_json = json.dumps(
                        dict(pieces=hulls["members"] if per_member else [hulls["whole"]]),
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                else:
                    reason = (
                        record.get("viewport_hull_reason", "refresh to evaluate this Shape Frame and hull detail")
                        if record
                        else "refresh to evaluate this Shape Frame and hull detail"
                    )
                    warnings.append("Convex hull unavailable for " + repr(row["target"]) + ": " + reason)
            result.extend(
                (
                    row["file"],
                    # The packed primitive rebuilds its reference from this kind.
                    row_reference_kind(row),
                    row["target"],
                    repr(float(row["frame"])),
                    row["name"],
                    "1" if per_member else "0",
                    hull_json,
                    repr(shape_frame),
                    json.dumps(record, separators=(",", ":"), allow_nan=False) if record else "",
                    "1" if wireframe else "0",
                )
            )
        result[1] = "\n".join(dict.fromkeys(warnings))
        return result
    except Exception as error:
        return [str(error), ""]


def migrate_node(node):
    """Expose an old single selection without evaluating or replacing channels."""
    parm = node.parm("saved_reference_migration")
    # Existing explicit HIP channels retain their authored semantics. Omitted
    # defaults use the native default; new insertions have a zero marker.
    for i in range(1, node.evalParm("references") + 1):
        marker = node.parm(f"ref_frame_initialized{i}")
        if marker is not None:
            marker.set(1)
    if parm is None or _saved_text(parm):
        return
    count = node.evalParm("references")
    record = dict(schema_version=1, legacy_names=[], disabled_channels=[])
    if not node.evalParm("multiple_references"):
        # asCode retains keyframes, expression language and literal values for
        # dormant enable channels. All other dormant channels stay untouched.
        for i in range(1, count + 1):
            enabled = node.parm(f"ref_enabled{i}")
            record["disabled_channels"].append(dict(row=i + 1, code=enabled.asCode()))
        encoded = json.dumps(record, ensure_ascii=False)
        if len(encoded.encode()) > _MAX_BYTES:
            raise ValueError("Legacy reference migration exceeds the 1 MiB metadata budget")
        if count:
            node.parm("references").insertMultiParmInstance(0)
        else:
            node.parm("references").set(1)
        for source, destination in (
            ("target_kind", "ref_target_kind1"),
            ("target", "ref_target1"),
            ("frame", "ref_frame1"),
        ):
            node.parm(destination).setFromParm(node.parm(source))
        node.parm("ref_name1").set("reference")
        marker = node.parm("ref_frame_initialized1")
        if marker is not None:
            marker.set(1)
        for i in range(2, count + 2):
            enabled = node.parm(f"ref_enabled{i}")
            enabled.deleteAllKeyframes()
            enabled.set(0)
        node.parm("multiple_references").set(1)
        if count:
            record["warning"] = (
                "Previous inactive reference rows were retained disabled; their enable channels are saved in migration metadata"
            )
    record["legacy_names"] = [
        node.evalParm(f"ref_name{i}")
        for i in range(1, node.evalParm("references") + 1)
        if node.evalParm(f"ref_name{i}")
    ]
    record["legacy_target_fingerprints"] = {
        node.evalParm(f"ref_name{i}"): _channel_fingerprint(node.parm(f"ref_target{i}"))
        for i in range(1, node.evalParm("references") + 1)
        if node.evalParm(f"ref_name{i}")
    }
    parm.set(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
    node.parm("references_initialized").set(1)


def initialize_rows(node):
    """First opt-in copies channel state exactly once; toggling keeps the list."""
    if not node.evalParm("multiple_references"):
        return
    initialized = node.parm("references_initialized")
    if not initialized.eval():
        empty = node.evalParm("references") == 0
        if empty:
            node.parm("references").set(1)
        if empty:
            for source, destination in (
                ("target_kind", "ref_target_kind1"),
                ("target", "ref_target1"),
                ("frame", "ref_frame1"),
            ):
                node.parm(destination).setFromParm(node.parm(source))
            marker = node.parm("ref_frame_initialized1")
            if marker is not None:
                marker.set(1)
        initialized.set(1)
    for i in range(1, node.evalParm("references") + 1):
        try:
            marker = node.parm(f"ref_frame_initialized{i}")
        except KeyError:
            marker = None
        if marker is not None and not marker.eval():
            frame = node.parm(f"ref_frame{i}")
            try:
                untouched = frame.isAtDefault(compare_expressions=True)
            except AttributeError:
                try:
                    untouched = frame.expression().strip() == "$FF"
                except Exception:
                    untouched = _saved_text(frame).strip() == "$FF"
            if untouched:
                frame.deleteAllKeyframes()
                frame.set(1)
            marker.set(1)


def invalidate():
    global _generation
    with _lock:
        _generation += 1
        _pending.clear()
        for job in _jobs.values():
            job["cancelled"].set()
            if not job.get("started"):
                job["done"].set()
        _jobs.clear()


def _hip_event(event):
    import hou

    if event in (hou.hipFileEventType.BeforeClear, hou.hipFileEventType.BeforeLoad):
        invalidate()


def _stage_record(job, source, record):
    job["records"][source] = record
    encoded = json.dumps(dict(schema_version=1, files=job["records"]), ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > _MAX_BYTES:
        job["records"].pop(source)
        job["error"] = "Target catalogue exceeds the aggregate 1 MiB saved metadata budget"
        return False
    return True


def _cache_record(source, record):
    # Caller owns _lock; the entire shared catalogue cache has a byte budget.
    _cache[source] = record
    _cache_times[source] = time.monotonic()
    _cache_sizes[source] = len(json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    _cache.move_to_end(source)
    while len(_cache) > 64 or sum(_cache_sizes.get(key, 0) for key in _cache) > _MAX_CACHE_BYTES:
        old_source, _ = _cache.popitem(last=False)
        _cache_times.pop(old_source, None)
        _cache_sizes.pop(old_source, None)


def _catalogue_sources(node):
    rows = reference_rows(node)
    sources = {_path(row["file"]) for row in rows if row["file"]}
    # An empty/disabled list still needs the shared file menu. Legacy active
    # rows can all use independent overrides, so their unused shared file must
    # not become a mandatory dependency or block an otherwise valid refresh.
    if not rows:
        shared = _reference_file(node, "file")
        if shared:
            sources.add(_path(shared))
    return tuple(sorted(sources))


def _launch(job):
    global _active
    _active = job
    job["started"] = True

    def run():
        try:
            from . import blend_worker

            missing = []
            for source in job["sources"]:
                signature = blend_worker._signature(source)
                with _lock:
                    record = _cache.get(source) or job["cached"].get(source)
                    newly_read = _cache_times.get(source, 0) >= job["requested"]
                if (not job["force"] or newly_read) and record and tuple(record.get("signature", ())) == signature:
                    _stage_record(job, source, record)
                elif signature is None:
                    job["error"] = "Blender file is missing: " + source
                else:
                    missing.append(source)
            if missing and not job["cancelled"].is_set():
                from ..core import blender_bridge

                enabled, configured, search_path, program_files = job["discovery"]
                executable = blender_bridge._discover(configured, search_path, program_files) if enabled else None
                if not executable or not os.path.isfile(executable):
                    raise ValueError(
                        "Blender is unavailable. Set CYCLES_HOUDINI_BLENDER or enable the Blender bridge in setup."
                    )
                for record in blend_worker.catalogue_many(missing, executable, cancelled=job["cancelled"]):
                    if "error" in record:
                        job["error"] = record["error"]
                    elif not job["cancelled"].is_set():
                        source = os.path.normcase(record.get("source", ""))
                        if source not in missing or not _valid_record(record, source):
                            job["error"] = "Blender target catalogue is malformed or exceeds the 1 MiB metadata budget"
                            continue
                        if tuple(record["signature"]) != blend_worker._signature(source):
                            job["error"] = "Blender file changed during catalogue read"
                            continue
                        if _stage_record(job, source, record):
                            with _lock:
                                _cache_record(source, record)
        except Exception as error:
            job["error"] = str(error)
        finally:
            job["done"].set()

    threading.Thread(target=run, daemon=True, name="CH Blender target catalogue").start()


def drain_catalogues():
    """Publish on Houdini's thread; useful for headless explicit refresh tests."""
    global _active
    import hou

    for node_id, job in list(_jobs.items()):
        if not job["done"].is_set():
            if hou.nodeBySessionId(node_id) is None:
                job["cancelled"].set()
                if not job.get("started"):
                    job["done"].set()
            if not job["done"].is_set():
                continue
        try:
            node = hou.nodeBySessionId(node_id)
            if node is not None and job["generation"] == _generation and not job["cancelled"].is_set():
                current = _catalogue_sources(node)
                if current == job["sources"]:
                    saved = _read(node)
                    saved.update(job["records"])
                    saved = {key: saved[key] for key in current if key in saved}
                    encoded = json.dumps(dict(schema_version=1, files=saved), ensure_ascii=False, separators=(",", ":"))
                    if len(encoded.encode()) <= _MAX_BYTES:
                        if _saved_text(node.parm("saved_catalogue")) != encoded:
                            node.parm("saved_catalogue").set(encoded)
                    else:
                        job["error"] = "Target catalogue exceeds the 1 MiB saved metadata budget"
                    bind_targets(node)
                    if job.get("refresh_bounds") and not job["error"]:
                        try:
                            from .blend_bounds import refresh_node_bounds

                            rows, _ = resolved_reference_rows(node)
                            if rows:
                                job["bounds_job"] = refresh_node_bounds(node)
                        except Exception as error:
                            job["error"] = str(error)
                    status = job["error"] or ""
                    if node.parm("catalogue_status").evalAsString() != status:
                        node.parm("catalogue_status").set(status)
        except Exception as error:
            job["error"] = str(error)
        finally:
            if _jobs.get(node_id) is job:
                _jobs.pop(node_id)
    with _lock:
        if _active and _active["done"].is_set():
            _active = None
        if _active is None:
            while _pending:
                node_id, job = _pending.popitem(last=False)
                if not job["cancelled"].is_set():
                    _launch(job)
                    break
    return not _jobs and not _pending and _active is None


def request_catalogues(node, force=False, *, refresh_bounds=False):
    """Only file-change callbacks and the Refresh button call this entry point."""
    global _poller
    import hou
    from ..core import blender_bridge

    initialize_rows(node)
    sources = _catalogue_sources(node)
    old = _jobs.get(node.sessionId())
    if old:
        old["cancelled"].set()
        if not old.get("started"):
            old["done"].set()
    # Snapshot host-owned configuration here; executable filesystem discovery
    # runs only in _launch's helper, alongside source revision probes.
    discovery = (
        blender_bridge.enabled(),
        blender_bridge._configured_executable(),
        os.environ.get("PATH", ""),
        os.environ.get("ProgramFiles", "C:/Program Files"),
    )
    job = dict(
        sources=sources,
        discovery=discovery,
        generation=_generation,
        records={},
        error=None,
        force=force,
        refresh_bounds=refresh_bounds,
        cached=_read(node),
        requested=time.monotonic(),
        started=False,
        done=threading.Event(),
        cancelled=threading.Event(),
    )
    _jobs[node.sessionId()] = job
    if set(sources) != set(job["cached"]):
        node.parm("catalogue_status").set("Reading target catalogue...")
    if _hip_event not in hou.hipFile.eventCallbacks():
        hou.hipFile.addEventCallback(_hip_event)
    with _lock:
        if _active is None:
            _launch(job)
        else:
            node_id = node.sessionId()
            if node_id not in _pending and len(_pending) >= 128:
                job["error"] = "Target catalogue queue is full; refresh again when current work finishes"
                job["done"].set()
                node.parm("catalogue_status").set(job["error"])
            else:
                _pending[node_id] = job
    if hou.isUIAvailable() and not _poller:
        _poller = True

        def poll():
            global _poller
            if drain_catalogues():
                hou.ui.removeEventLoopCallback(poll)
                _poller = False

        hou.ui.addEventLoopCallback(poll)
    return job


def parameters_changed(node):
    initialize_rows(node)
    bind_targets(node)
    return request_catalogues(node, refresh_bounds=True)


def selection_changed(node):
    """Use saved IDs for row edits; only a new source needs a catalogue read."""
    initialize_rows(node)
    _edited_legacy_rows(node)
    bind_targets(node)
    saved = _read(node)
    rows = reference_rows(node)
    if any(row["file"] and _path(row["file"]) not in saved for row in rows):
        return request_catalogues(node, refresh_bounds=True)
    status = node.parm("catalogue_status")
    if status is not None and status.evalAsString():
        status.set("")
    from .blend_bounds import refresh_node_bounds

    try:
        resolved, _ = resolved_reference_rows(node)
        if resolved:
            from . import blend_preview

            if blend_preview.has_current(node):
                node.cook(force=True)
                return None
            return refresh_node_bounds(node)
    except Exception as error:
        if status is not None:
            status.set(str(error))
    return None


def refresh_node(node, force=True):
    """Unified asynchronous catalogue then selected evaluated-bounds refresh."""
    return request_catalogues(node, force=force, refresh_bounds=True)


def viewport_changed(node):
    """Preview edits reuse the node-owned snapshot for this shape frame/detail."""
    from . import blend_preview

    if blend_preview.has_current(node):
        node.cook(force=True)
        return None
    return selection_changed(node)
