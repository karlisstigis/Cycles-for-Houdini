# SPDX-License-Identifier: GPL-3.0-or-later
# Blender component of Cycles for Houdini (see blender/README.md). This file is one of the scripts
# that run inside Blender and use its Python API, so it is published under the GNU General Public
# License, version 3 or later (blender/LICENSE). The Houdini integration (Apache-2.0) never imports
# it; the two sides exchange only files, command-line arguments and pipe messages.
# Pipe protocol: cycles_houdini.blender.blend_worker (Apache-2.0).
"""Blender side of the persistent Blender evaluation pipe (runs inside Blender).

Started by blender/worker_main.py in a Blender process that the Houdini side
(cycles_houdini.blender.blend_worker) launches. Only JSON and raw numeric
buffers cross the connection; pickle is never used.
"""

import glob
import os
from pathlib import Path
import sys
import time
import traceback

from cycles_houdini.blender.blend_worker import (
    StaleEvaluationError,
    _ASSET_COMMANDS,
    _MAX_ASSET_SOURCES,
    _Pipe,
    _make_digest_cache,
    _material_names,
    _recv_json,
    _send_json,
    _send_result,
    _signature,
    _texture_mode,
    _timing_record,
    _timing_root,
)


def _list_contents_command(command):
    """Worker side (inside Blender): open the main file read-only and describe it."""
    from . import blend_list_contents

    source = command.get("source")
    if not isinstance(source, str) or not source:
        raise ValueError("Invalid Blender listing request")
    before = _signature(source)
    if before is None:
        raise FileNotFoundError("Blender file not found: " + source)
    result = blend_list_contents.list_file(source)
    if _signature(source) != before:
        raise StaleEvaluationError("Blender file changed during listing")
    result["ok"] = True
    result["signature"] = list(before)
    return result


def _asset_command(command):
    """Worker-side handler (inside Blender) for the asset browser and importer commands."""
    kind = command.get("command")
    if kind == "list_blend_contents":
        return _list_contents_command(command)
    from . import blend_asset_scan as scan

    if kind == "assets_many":
        sources, thumbnail_dir = command.get("sources"), command.get("thumbnail_dir")
        if not isinstance(sources, list) or len(sources) > _MAX_ASSET_SOURCES or not isinstance(thumbnail_dir, str):
            raise ValueError("Invalid Blender asset scan request")
        result = []
        for source in sources:
            try:
                if not isinstance(source, str):
                    raise ValueError("Invalid Blender asset source")
                before = _signature(source)
                if before is None:
                    raise FileNotFoundError("Blender file not found: " + source)
                scanned = scan.scan_file(source, thumbnail_dir, signature=list(before))
                after = _signature(source)
                if before != after:
                    raise StaleEvaluationError("Blender file changed during asset scan")
                entry = {"source": source, "signature": list(after), "assets": scanned["assets"]}
                if scanned.get("warnings"):
                    entry["warnings"] = list(scanned["warnings"])[:64]
                result.append(entry)
            except Exception as error:
                # Classify at the source, before the exception type is lost to
                # JSON. Unknown Blender/OSError failures are retryable; never
                # infer permanent invalidity from a generic "cannot read".
                kind = "invalid_file" if isinstance(error, scan.InvalidAssetFileError) else "transient"
                result.append({"source": source, "error": str(error) or type(error).__name__, "error_kind": kind})
        return result
    if kind == "export_library_materials":
        source, names, output_dir = command.get("source"), command.get("names"), command.get("output_dir")
        if not isinstance(source, str) or not isinstance(output_dir, str):
            raise ValueError("Invalid Blender material export request")
        names = _material_names(names)
        textures = _texture_mode(command.get("textures", "copy"))
        before = _signature(source)
        if before is None:
            raise FileNotFoundError("Blender file not found: " + source)
        if textures == "copy":
            result = scan.export_named_materials(source, names, output_dir)
        else:  # needs blend_asset_scan.export_named_materials(..., textures=) forwarding to export_materials
            result = scan.export_named_materials(source, names, output_dir, textures=textures)
        if _signature(source) != before:
            raise StaleEvaluationError("Blender file changed during material export")
        return {"ok": True, "records": result["records"], "errors": result["errors"]}
    if kind == "export_library_worlds":
        source, names, output_dir = command.get("source"), command.get("names"), command.get("output_dir")
        if not isinstance(source, str) or not isinstance(output_dir, str):
            raise ValueError("Invalid Blender world export request")
        names = _material_names(names)
        textures = _texture_mode(command.get("textures", "in_place"))
        before = _signature(source)
        if before is None:
            raise FileNotFoundError("Blender file not found: " + source)
        result = scan.export_named_worlds(source, names, output_dir, textures=textures)
        if _signature(source) != before:
            raise StaleEvaluationError("Blender file changed during world export")
        return {"ok": True, "records": result["records"], "errors": result["errors"]}
    raise ValueError("Unknown Blender asset command")


def _reply_asset_command(connection, command):
    try:
        reply = _asset_command(command)
    except Exception as error:
        reply = {"ok": False, "error": str(error) or type(error).__name__}
    try:
        _send_json(connection, reply)
    except ValueError as error:
        # Oversized or non-JSON replies are reported, keeping the stream framed.
        _send_json(connection, {"ok": False, "error": str(error)})


def _serve():
    import bpy

    token = bytes.fromhex(os.environ.pop("CH_BLEND_PIPE_KEY"))
    # Duplicate stdout before redirecting both the Python stream and OS fd 1.
    # Blender's native logging must never become a geometry protocol frame.
    sys.stdout.flush()
    writer = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    connection = _Pipe(sys.stdin.buffer, writer)
    writer.write(token)
    writer.flush()
    from . import blend_export as exporter

    _send_json(connection, dict(protocol="ch-blend-arrays", version=2, pid=os.getpid()))
    batch = 0
    # Set to 0 for an otherwise identical SHA-every-array benchmark baseline.
    # Four workers therefore retain at most 4 * 128 MiB by default.
    digest_cache = _make_digest_cache()
    try:
        while True:
            try:
                command = _recv_json(connection)
            except EOFError:
                return
            if command.get("command") == "prewarm":
                _send_json(connection, dict(ok=True))
                continue
            if command.get("command") == "catalogue_many":
                sources = command.get("sources", [])
                if len(sources) > 128:
                    raise ValueError("Too many Blender catalogue files")
                result = []
                for source in sources:
                    try:
                        before = _signature(source)
                        with bpy.data.libraries.load(source, link=False) as (available, unused):
                            objects = sorted(available.objects)
                            collections = sorted(available.collections)
                        after = _signature(source)
                        if before is None or before != after:
                            raise StaleEvaluationError("Blender file changed during catalogue read")
                        result.append(dict(source=source, signature=after, objects=objects, collections=collections))
                    except Exception as error:
                        result.append(dict(source=source, error=str(error)))
                _send_json(connection, result)
                continue
            if command.get("command") in _ASSET_COMMANDS:
                _reply_asset_command(connection, command)
                continue
            if command.get("command") != "evaluate_many":
                raise ValueError("Unknown Blender worker command")
            requests = command["requests"]
            batch += 1
            sent, iterator = 0, None
            try:
                phases = {} if _timing_root else None
                open_start = time.perf_counter() if phases is not None else None
                source = str(Path(requests[0]["source"]).resolve())
                known = {source} | set(getattr(_serve, "dependencies", ()))
                before = {path: _signature(path) for path in known}
                loaded = command.get("reload") or str(Path(bpy.data.filepath).resolve()) != source
                if loaded:
                    if digest_cache is not None:
                        digest_cache.clear()
                    bpy.ops.wm.open_mainfile(filepath=source, load_ui=False, use_scripts=False)
                for library in bpy.data.libraries:
                    known.add(str(Path(bpy.path.abspath(library.filepath, library=library.parent)).resolve()))
                for image in bpy.data.images:
                    if image.source != "GENERATED" and not image.packed_file and image.filepath:
                        path = str(Path(bpy.path.abspath(image.filepath, library=image.library)).resolve())
                        known.update(glob.glob(path.replace("<UDIM>", "*")) or [path])
                discovered = known - before.keys()
                for path in discovered:
                    before[path] = _signature(path)
                if loaded and discovered:
                    # Linked datablocks may already have read these newly found
                    # files. Reopen after their baseline is captured, before any
                    # graph evaluation, to exclude a mixed initial load.
                    bpy.ops.wm.open_mainfile(filepath=source, load_ui=False, use_scripts=False)
                if phases is not None:
                    phases["source_open_dependencies"] = time.perf_counter() - open_start
                directories = [
                    str(Path(command["texture_directory"]) / ("request-" + str(i))) for i in range(len(requests))
                ]
                for directory in directories:
                    Path(directory).mkdir(parents=True, exist_ok=True)
                iterator = exporter.iter_evaluate_many(requests, directories)
                while sent < len(requests):
                    request_phases = (dict(phases) if sent == 0 else {}) if phases is not None else None
                    evaluate_start = time.perf_counter() if request_phases is not None else None
                    try:
                        result = next(iterator)
                    except Exception:
                        failure = traceback.format_exc()[-32768:]
                        for _ in range(len(requests) - sent):
                            _send_json(connection, dict(ok=False, error=failure))
                        sent = len(requests)
                        break
                    if request_phases is not None:
                        request_phases["evaluation"] = time.perf_counter() - evaluate_start
                        dependency_start = time.perf_counter()
                    if not isinstance(result, Exception):
                        known.update(result[0].get("dependencies", ()))
                    after = {path: _signature(path) for path in known}
                    stale = set(after) != set(before) or after != before
                    if request_phases is not None:
                        request_phases["dependency_check"] = time.perf_counter() - dependency_start
                    _serve.dependencies = known
                    if stale:
                        if digest_cache is not None:
                            digest_cache.clear()
                        _send_json(
                            connection,
                            dict(
                                ok=False,
                                stale=True,
                                error="Blender dependencies changed or were discovered during evaluation",
                            ),
                        )
                    elif isinstance(result, Exception):
                        _send_json(connection, dict(ok=False, error=str(result)))
                    else:
                        manifest, arrays = result
                        manifest["dependency_versions"] = after
                        if digest_cache is not None:
                            digest_cache.set_context((source, after))
                        _send_result(connection, manifest, arrays, request_phases, digest_cache)
                        del manifest, arrays
                    if request_phases is not None:
                        # Flush each completed request rather than retaining a
                        # whole batch's potentially large per-array sidecars.
                        # This is outside every measured worker phase and is
                        # absent entirely when profiling is disabled.
                        _timing_record(
                            "worker",
                            dict(
                                source=source,
                                batch=batch,
                                ordinal=sent,
                                frame=requests[sent].get("frame"),
                                selector=requests[sent].get("selector"),
                                **request_phases,
                            ),
                        )
                    del result
                    sent += 1
            except (EOFError, BrokenPipeError):
                return
            except Exception:
                # Failures while sending framed payloads cannot be resynchronized.
                # Let the host discard this process and recover on its next call.
                if sent:
                    raise
                failure = traceback.format_exc()[-32768:]
                for _ in requests:
                    _send_json(connection, dict(ok=False, error=failure))
            finally:
                if iterator is not None:
                    iterator.close()
    finally:
        if digest_cache is not None:
            digest_cache.clear()
        connection.close()


def main():
    """Run the Blender pipe server through the explicit worker script."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", required=True)
    parser.parse_args(sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else sys.argv[1:])
    _serve()


if __name__ == "__main__":
    main()
