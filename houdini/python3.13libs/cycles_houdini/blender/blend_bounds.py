# SPDX-License-Identifier: Apache-2.0
"""Bounds from evaluated Blender meshes, with explicit asynchronous refresh.

This module is never called by a packed primitive's load/cook/draw/bounds
callbacks. Render preparation opts in; artists can explicitly refresh via the
Python API. The existing worker pool owns every Blender process.
"""

import hashlib
import itertools
import json
import math
import os
import tempfile
import threading
from collections import OrderedDict

import numpy as np


MAX_DEPENDENCIES = 256
MAX_RECORD_BYTES = 128 * 1024
_refresh_lock = threading.RLock()
_refresh_job = None
_refresh_running = None
_preview_jobs = OrderedDict()
_refresh_generation = 0
_refresh_metrics = dict(
    refresh_evaluation_batches=0, refresh_evaluated_prototypes=0, refresh_cancelled_jobs=0, refresh_failed_evaluations=0
)


def refresh_diagnostics():
    """Cumulative artist-refresh attempts, including failed/cancelled batches.

    The existing assets evaluation_* counters describe render preparation only.
    These refresh_* counters are owned here and synchronized independently of HOM.
    """
    with _refresh_lock:
        return dict(_refresh_metrics)


def invalidate_refreshes():
    """HIP clear/load barrier; called before clearing the native bounds cache."""
    global _refresh_generation
    with _refresh_lock:
        _refresh_generation += 1
        if _refresh_job is not None:
            _refresh_job.cancel("Scene changed during bounds refresh")
            if _refresh_job.done.is_set():
                _refresh_job.close()
        for job in list(_preview_jobs.values()):
            job.cancel("Scene changed during viewport refresh")
        _preview_jobs.clear()


_IMAGE_EXTENSIONS = frozenset(
    (".exr", ".hdr", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".tga", ".bmp", ".webp", ".tx", ".dds", ".psd")
)


def evaluated_record(manifest, arrays, *, native_subdivision=True, mesh_boxes=None, preview=False):
    """Return a conservative box in the reference's Houdini-axis coordinates.

    Each mesh is reduced once, then its eight box corners are transformed per
    instance. Catmull-Clark limit surfaces remain in their control hull. Arbitrary
    shader displacement has no finite proven hull, so its bounds remain unknown.
    """
    # A referenced image that does not exist (e.g. an HDRI path from the asset
    # author's machine) cannot change mesh bounds or hulls; displaced materials
    # already make the bounds unknown below. It cannot be stat'ed for freshness or
    # restored natively, so it is left out instead of failing the whole preview.
    versions = {
        path: version
        for path, version in manifest["dependency_versions"].items()
        if version is not None or os.path.splitext(path)[1].lower() not in _IMAGE_EXTENSIONS
    }
    record = dict(
        schema_version=1,
        known=False,
        source=manifest["source"],
        selector_type=manifest["selector_type"],
        selector=manifest["selector"],
        frame=float(manifest["frame"]),
        dependency_versions=versions,
    )
    if manifest.get("include_excluded_collections", False):
        # Also part of the identity; it pairs the record with the
        # path.blend#collection+disabled=Name reference (native and Python checks).
        record["include_excluded_collections"] = True
    identity = dict(record, native_subdivision=bool(native_subdivision))
    record["source_identity"] = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if len(versions) > MAX_DEPENDENCIES:
        record["dependency_versions"] = {}
        record["reason"] = "Too many dependencies for a bounded packed bounds record"
        return record
    # Native persistence duplicates dependency paths with filesystem signatures.
    if len(json.dumps(record).encode()) > MAX_RECORD_BYTES // 3:
        record["dependency_versions"] = {}
        record["reason"] = "Dependency paths exceed the packed bounds record budget"
        return record
    if any(
        material.get("settings", {}).get("displacement_method", "BUMP") != "BUMP"
        and "displacement" in material.get("outputs", {})
        for material in manifest.get("materials", ())
    ):
        record["reason"] = "Shader displacement has no proven conservative bounds"
        return record
    boxes = {}
    # Link hair and point cloud records (blend_export) are bounded by their keys or points grown
    # by the largest radius.
    for mesh in list(manifest["meshes"]) + list(manifest.get("hair", ())) + list(manifest.get("points", ())):
        if mesh_boxes is not None:
            box = mesh_boxes[mesh["key"]]
            if not box:
                continue
            minimum, maximum = box[:3], box[3:]
        else:
            positions = np.asarray(arrays[mesh["arrays"]["positions"]])
            if positions.ndim != 2 or positions.shape[1] != 3 or not np.isfinite(positions).all():
                raise ValueError("Invalid evaluated Blender positions for packed bounds")
            if not positions.size:
                continue
            minimum, maximum = positions.min(axis=0), positions.max(axis=0)
            if mesh.get("max_radius"):
                minimum, maximum = minimum - float(mesh["max_radius"]), maximum + float(mesh["max_radius"])
        boxes[mesh["key"]] = np.array(list(itertools.product(*zip(minimum, maximum))), dtype=np.float64)
    minimum = np.full(3, np.inf)
    maximum = np.full(3, -np.inf)
    arithmetic_scale = np.ones(3)
    for instance in manifest["instances"]:
        corners = boxes.get(instance["mesh_key"])
        if corners is None:
            continue
        matrix = np.asarray(instance["transform"], dtype=np.float64).reshape(4, 4)
        if not np.isfinite(matrix).all() or not np.array_equal(matrix[3], [0, 0, 0, 1]):
            raise ValueError("Invalid evaluated Blender affine transform for packed bounds")
        transformed = corners @ matrix[:3, :3].T + matrix[:3, 3]
        arithmetic_scale = np.maximum(
            arithmetic_scale, np.abs(corners).max(axis=0) @ np.abs(matrix[:3, :3]).T + np.abs(matrix[:3, 3])
        )
        minimum = np.minimum(minimum, transformed.min(axis=0))
        maximum = np.maximum(maximum, transformed.max(axis=0))
    if not np.isfinite(minimum).all() or not np.isfinite(maximum).all() or not np.isfinite(arithmetic_scale).all():
        record["reason"] = "Evaluated selection has no finite vertices"
        return record
    # Round outward and allow double precision arithmetic accumulated by the
    # affine transform. This also contains translated/negative-scale corners.
    margin = arithmetic_scale * (32 * np.finfo(np.float64).eps)
    record.update(
        known=True,
        bounds=list(map(float, [*np.nextafter(minimum - margin, -np.inf), *np.nextafter(maximum + margin, np.inf)])),
    )
    from .blend_viewport import validate_viewport_hulls

    if validate_viewport_hulls(manifest.get("viewport_hulls")):
        record["viewport_hulls"] = manifest["viewport_hulls"]
        from .blend_preview import MAX_RECORD_BYTES as preview_bytes

        if (
            len(json.dumps(record, separators=(",", ":")).encode())
            > (preview_bytes if preview else MAX_RECORD_BYTES) - 8192
        ):
            record.pop("viewport_hulls")
            record["viewport_hull_reason"] = "Convex hull and dependency metadata exceed the saved record budget"
    elif manifest.get("viewport_hull_reason"):
        record["viewport_hull_reason"] = str(manifest["viewport_hull_reason"])[:1024]
    return record


def prepared_record(entry):
    """Reduce an already resident prototype without evaluating Blender again."""
    from .. import _native

    manifest = dict(entry["bounds_manifest"])
    meshes = {instance["geometry_key"]: instance["mesh"] for instance in entry["instances"]}
    manifest["meshes"] = [dict(key=key) for key in meshes]
    manifest["instances"] = [
        dict(mesh_key=instance["geometry_key"], transform=instance["transform"]) for instance in entry["instances"]
    ]
    boxes = {key: _native.blend_mesh_bounds(capsule) for key, capsule in meshes.items()}
    return evaluated_record(manifest, None, native_subdivision=entry["cache_key"][7], mesh_boxes=boxes)


def publication_generation():
    from .node_bounds import context_epoch

    return _refresh_generation, context_epoch()


def publish_record(reference, frame, record, *, generation=None):
    """Publish a validated result to the bounded native cache; never evaluate."""
    from .. import _native

    # Cosmetic snapshots belong to their requesting node and Shape Frame.
    # Render/culling authority is independent of preview detail and must stay
    # compact even when an adopted prototype carries a large viewport snapshot.
    payload = {
        key: value
        for key, value in record.items()
        if key not in ("viewport_hulls", "viewport_hull_reason", "max_hull_vertices")
    }
    payload.update(reference=reference, frame=float(frame))
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode()) > MAX_RECORD_BYTES:
        raise ValueError("Packed Blender bounds record exceeds its byte budget")
    with _refresh_lock:
        if generation is not None and generation != publication_generation():
            raise RuntimeError("Scene context changed before bounds publication")
        _native.publish_blender_reference_bounds(reference, float(frame), encoded)
        from .node_bounds import notify_publication

        notify_publication(reference, frame)


class BoundsRefresh:
    """One bounded batch. Wait for done, then publish() on the main thread."""

    def __init__(self):
        self.done = threading.Event()
        self.cancelled = threading.Event()
        self.error = None
        self.record = None
        self.records = {}
        self.key = None
        self.specs = ()
        self.generation = None
        self.publication_epoch = None
        self.published = False
        self.started = False
        self.owner = None
        self.resolved_key = None  # compatibility for a one-spec refresh
        self.resolved_keys = ()
        self.aliases = {}
        self.groups = []
        self.raw = {}
        self.directories = {}
        self.accepted = set()
        self.adoption_errors = {}  # key -> render-prototype conversion error (bounds still publish)
        self.checked_groups = set()
        self.metrics = dict(evaluation_batches=0, evaluated_prototypes=0)
        self.hulls = False
        self.hull_vertices = 64
        self.preview_mode = False
        self.preview_ui = False

    def release_preview_owner(self):
        if self.preview_mode and self.owner is not None:
            with _refresh_lock:
                if _preview_jobs.get(self.owner[0]) is self:
                    _preview_jobs.pop(self.owner[0], None)

    def cancel(self, reason="Bounds refresh cancelled"):
        with _refresh_lock:
            if not self.cancelled.is_set() and self.error is None and not self.published:
                _refresh_metrics["refresh_cancelled_jobs"] += 1
        self.cancelled.set()
        self.error = RuntimeError(reason)
        self.release_preview_owner()
        if not self.started or self.done.is_set():
            self.done.set()
            self.close()

    def close(self):
        self.raw.clear()
        for directory in self.directories.values():
            directory.cleanup()
        self.directories.clear()

    def check_owner(self):
        if self.owner is None:
            return
        import hou
        from .node_bounds import _rows
        from .blend_preview import preview_rows, settings

        node_id, node = self.owner
        if hou.nodeBySessionId(node_id) != node:
            self.cancel("Bounds refresh node was deleted")
            raise self.error
        current = tuple(
            dict.fromkeys(
                (ref, frame, True) for ref, frame, _ in (preview_rows(node) if self.preview_mode else _rows(node))
            )
        )
        if current != self.specs or (
            self.preview_mode and settings(node)[1:] != (self.hull_vertices, "hull" if self.hulls else "bounds")
        ):
            self.cancel("Bounds refresh node inputs changed")
            raise self.error

    def check_generation(self):
        if (
            self.cancelled.is_set()
            or self.generation != _refresh_generation
            or self.publication_epoch != publication_generation()
        ):
            raise RuntimeError("Blender inputs or scene changed, or bounds refresh was cancelled")

    def check_group(self, index):
        """Validate the complete source transaction before any host adoption."""
        from . import blend_worker

        self.check_generation()
        group = self.groups[index]
        if group["epoch"] != blend_worker._epoch(group["source"]) or group["launch"] != blend_worker.launch_signature(
            group["executable"], group["config"]
        ):
            raise blend_worker.StaleEvaluationError("Blender source changed during batch bounds refresh")
        for key in group["keys"]:
            record = self.records[next(spec for spec in self.specs if self.aliases[spec] == key)]
            if any(
                # A dependency that was missing must still be missing (None == None).
                blend_worker._signature(path) != (tuple(version) if version is not None else None)
                for path, version in record["dependency_versions"].items()
            ):
                raise blend_worker.StaleEvaluationError("Blender dependencies changed during batch bounds refresh")

    def accept(self, key=None):
        from . import blender_assets

        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("Bounds acceptance must run on Houdini's main thread")
        if not self.done.is_set():
            raise RuntimeError("Bounds evaluation is still running")
        if self.error is not None:
            raise self.error
        self.check_owner()
        self.check_generation()
        requested = self.resolved_keys if key is None else (key,)
        result = {}
        for requested_key in requested:
            if requested_key not in self.resolved_keys:
                continue
            if requested_key in self.accepted:
                result[requested_key] = blender_assets.resident_evaluation(requested_key)
                continue
            group_index = next(i for i, group in enumerate(self.groups) if requested_key in group["keys"])
            group = self.groups[group_index]
            if group_index not in self.checked_groups:
                self.check_group(group_index)
                self.checked_groups.add(group_index)
            from pathlib import Path

            item_index = group["keys"].index(requested_key)
            directory = str(Path(self.directories[group_index].name) / ("request-" + str(item_index)))
            from . import blend_worker

            try:
                result[requested_key] = blender_assets.accept_evaluation(
                    requested_key, self.raw[requested_key], directory, group["launch"], group["epoch"]
                )
            except blend_worker.StaleEvaluationError:
                raise
            except Exception as error:
                # Bounds and hulls need only the evaluated geometry. Adopting the
                # render prototype is an optimisation: a conversion failure leaves
                # rendering to evaluate again (and report it) instead of failing
                # the refresh.
                self.adoption_errors[requested_key] = error
                result[requested_key] = None
            self.accepted.add(requested_key)
            self.raw.pop(requested_key, None)
            if all(member in self.accepted for member in group["keys"]):
                self.directories.pop(group_index).cleanup()
        if key is not None:
            return result.get(key)
        if len(self.resolved_keys) == 1:
            return result.get(self.resolved_keys[0])
        return result

    def publish(self):
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("Bounds publication must run on Houdini's main thread")
        if not self.done.is_set():
            raise RuntimeError("Bounds evaluation is still running")
        try:
            return self._publish()
        except Exception as error:
            self.error = error
            self.close()
            self.release_preview_owner()
            from . import blender_assets

            blender_assets._trim_prototypes()
            raise

    def _publish(self):
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("Bounds publication must run on Houdini's main thread")
        if not self.done.is_set():
            raise RuntimeError("Bounds evaluation is still running")
        if self.error is not None:
            raise self.error
        if self.published:
            return self.record if len(self.specs) == 1 else self.records
        self.check_owner()
        # Check every source transaction before adopting any native prototype.
        for index in range(len(self.groups)):
            self.check_group(index)
        if self.preview_mode:
            from .blend_preview import persist
            from . import blender_assets
            from .. import _native

            # The full evaluation already belongs to this actual Shape Frame.
            # Adopt it into the normal render cache without publishing its box
            # as Source Frame authority or including cosmetic detail in its key.
            self.accept()
            for index in range(len(self.groups)):
                self.check_group(index)
            records = [
                dict(self.records[spec], reference=spec[0], frame=spec[1], max_hull_vertices=self.hull_vertices)
                for spec in self.specs
            ]
            for record in records:
                if record.get("known"):
                    _native.restore_blender_reference_preview(
                        record["reference"], record["frame"], record["source_identity"]
                    )
            persist(self.owner[1], records)
            self.published = True
            self.close()
            self.release_preview_owner()
            blender_assets._trim_prototypes()
            self.owner[1].cook(force=True)
            return records[0] if len(records) == 1 else records
        self.accept()
        for index in range(len(self.groups)):
            self.check_group(index)
        with _refresh_lock:
            self.check_generation()
            for spec in self.specs:
                publish_record(spec[0], spec[1], self.records[spec], generation=self.publication_epoch)
            self.published = True
        from .node_bounds import drain_pending_node_bounds

        drain_pending_node_bounds()
        from . import blender_assets

        blender_assets._trim_prototypes()
        return self.record if len(self.specs) == 1 else self.records


def _start_refresh(job):
    """One helper at a time, with at most one superseding pending batch."""
    global _refresh_running
    _refresh_running = job
    job.started = True

    def run():
        global _refresh_running
        try:
            from . import blend_worker

            for group_index, group in enumerate(job.groups):
                job.check_generation()
                directory = tempfile.TemporaryDirectory(prefix="cycles_blend_bounds_", ignore_cleanup_errors=True)
                job.directories[group_index] = directory
                with _refresh_lock:
                    job.metrics["evaluation_batches"] += 1
                    job.metrics["evaluated_prototypes"] += len(group["requests"])
                    _refresh_metrics["refresh_evaluation_batches"] += 1
                    _refresh_metrics["refresh_evaluated_prototypes"] += len(group["requests"])
                results = blend_worker.evaluate_many(
                    group["source"],
                    group["executable"],
                    group["requests"],
                    directory.name,
                    source_config=group["config"],
                )
                if len(results) != len(group["keys"]):
                    raise ValueError("Malformed Blender bounds batch result count")
                staged = {}
                for key, result in zip(group["keys"], results):
                    if isinstance(result, Exception):
                        raise result
                    manifest, arrays = result
                    staged[key] = evaluated_record(
                        manifest, arrays, native_subdivision=key[7], preview=job.preview_mode
                    )
                    if job.preview_mode:
                        from .file_signature import native_signature

                        staged[key]["native_signatures"] = {
                            path: native_signature(path) for path in staged[key]["dependency_versions"]
                        }
                with _refresh_lock:
                    job.check_generation()
                    for spec in job.specs:
                        if job.aliases[spec] in staged:
                            job.records[spec] = staged[job.aliases[spec]]
                    # No result is accepted until its whole source group passes.
                    job.check_group(group_index)
                    job.raw.update(zip(group["keys"], results))
            if len(job.specs) == 1:
                job.record = job.records[job.specs[0]]
        except Exception as error:
            job.error = error
            with _refresh_lock:
                if not job.cancelled.is_set():
                    _refresh_metrics["refresh_failed_evaluations"] += 1
            job.close()
        finally:
            if job.cancelled.is_set():
                job.close()
            if job.preview_mode and (job.error is not None or not job.preview_ui):
                job.release_preview_owner()
            with _refresh_lock:
                _refresh_running = None
                pending = _refresh_job
                if pending is None or pending.started or pending.cancelled.is_set():
                    pending = next(
                        (
                            candidate
                            for candidate in _preview_jobs.values()
                            if not candidate.started and not candidate.cancelled.is_set()
                        ),
                        None,
                    )
                if (
                    pending is not job
                    and pending is not None
                    and not pending.started
                    and not pending.cancelled.is_set()
                ):
                    _start_refresh(pending)
                job.done.set()

    threading.Thread(target=run, daemon=True, name="CH Blender bounds refresh").start()


def refresh_many(specs, *, supersede=False, hulls=False, hull_vertices=64, preview_owner=None):
    """Resolve <=128 enabled specs now, evaluate one batch per canonical file.

    specs are (reference, frame, native_subdivision) triples. No HOM/native calls
    occur on the helper. publish() explicitly finishes host adoption/publication.
    """
    from . import blender_assets, blend_worker
    from .blend_viewport import hull_vertex_limit
    import hou

    global _refresh_job
    hull_vertices = hull_vertex_limit(hull_vertices)
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("Bounds request resolution must run on Houdini's main thread")
    raw_specs = list(itertools.islice(iter(specs), 129))
    if len(raw_specs) > 128:
        raise ValueError("Bounds refresh accepts at most 128 input rows")
    specs = tuple(dict.fromkeys((reference, float(frame), bool(native)) for reference, frame, native in raw_specs))
    if not specs or len(specs) > 128 or any(not math.isfinite(spec[1]) for spec in specs):
        raise ValueError("Bounds refresh accepts 1 to 128 finite reference/frame pairs")
    with _refresh_lock:
        if preview_owner is not None:
            previous = _preview_jobs.get(preview_owner[0])
            if previous is not None and not previous.done.is_set():
                if (
                    previous.specs == specs
                    and previous.hulls == bool(hulls)
                    and previous.hull_vertices == hull_vertices
                ):
                    return previous
                previous.cancel("Viewport preview settings changed")
            if len(_preview_jobs) >= 128 and preview_owner[0] not in _preview_jobs:
                for ident, previous in list(_preview_jobs.items()):
                    if previous.done.is_set():
                        _preview_jobs.pop(ident)
                if len(_preview_jobs) >= 128:
                    raise ValueError("Viewport refresh queue exceeds 128 nodes")
        elif _refresh_job is not None and not _refresh_job.done.is_set():
            if _refresh_job.hulls == bool(hulls) and (
                _refresh_job.specs == specs or (len(specs) == 1 and _refresh_job.key == specs[0])
            ):
                return _refresh_job
            if not supersede:
                raise RuntimeError("A different Blender bounds refresh is already running; retry when it finishes")
            _refresh_job.cancel("Bounds refresh superseded by a newer node request")
        job = BoundsRefresh()
        job.hulls = bool(hulls)
        job.hull_vertices = hull_vertices
        job.preview_mode = preview_owner is not None
        job.preview_ui = job.preview_mode and hou.isUIAvailable()
        job.owner = preview_owner
        job.specs = specs
        job.key = specs[0] if len(specs) == 1 else specs
        job.generation = _refresh_generation
        job.publication_epoch = publication_generation()
        groups, canonical = {}, {}
        for spec in specs:
            key, request = blender_assets._resolve_spec(spec)
            if hulls:
                request = dict(request, viewport_hulls=True, max_hull_vertices=hull_vertices)
            job.aliases[spec] = key
            if key in canonical:
                continue
            canonical[key] = request
            group_key = key[1], key[0], key[5]
            if group_key not in groups:
                groups[group_key] = dict(
                    source=key[1],
                    executable=key[0],
                    config=key[5],
                    keys=[],
                    requests=[],
                    epoch=blend_worker._epoch(key[1]),
                    launch=blend_worker.launch_signature(key[0], key[5]),
                )
            groups[group_key]["keys"].append(key)
            groups[group_key]["requests"].append(request)
        job.groups = list(groups.values())
        job.resolved_keys = tuple(canonical)
        job.resolved_key = job.resolved_keys[0] if len(job.resolved_keys) == 1 else None
        if blender_assets._hip_event not in hou.hipFile.eventCallbacks():
            hou.hipFile.addEventCallback(blender_assets._hip_event)
        if preview_owner is not None:
            _preview_jobs[preview_owner[0]] = job
        else:
            _refresh_job = job
        if _refresh_running is None:
            _start_refresh(job)
        return job


def refresh_bounds(reference, frame, *, native_subdivision=True):
    """Single-reference compatibility wrapper; wait for done then publish()."""
    return refresh_many([(reference, frame, native_subdivision)])


def refresh_node_bounds(node):
    """Refresh all enabled rows, grouped by source; UI or deterministic headless."""
    import hou
    from .node_bounds import _rows, cook_node

    # A new node may not have cooked yet. Subscribe its metadata before any
    # publication, without requesting geometry or evaluating Blender here.
    cook_node(node)
    from . import blend_preview

    preview = node.parm("saved_viewport_hulls") is not None
    specs = [
        (reference, frame, True)
        for reference, frame, _ in (blend_preview.preview_rows(node) if preview else _rows(node))
    ]
    shape = node.parm("viewport_shape")
    hulls = shape is not None and shape.evalAsString() == "hull"
    job = refresh_many(
        specs,
        supersede=True,
        hulls=hulls,
        hull_vertices=blend_preview.settings(node)[1],
        preview_owner=(node.sessionId(), node) if preview else None,
    )
    node_id = node.sessionId()
    job.owner = (node_id, node)
    refresh_generation = _refresh_generation
    if not hou.isUIAvailable():
        return job

    def report(error):
        # Worker errors arrive as whole tracebacks; the node shows their final line.
        lines = [line.strip() for line in str(error).splitlines() if line.strip()]
        status = node.parm("catalogue_status")
        if status is not None:
            status.set("Blender bounds refresh failed: " + (lines[-1] if lines else type(error).__name__))
        node.cook(force=True)

    def poll():
        if (
            job.done.is_set()
            and job.error is not None
            and not job.cancelled.is_set()
            and hou.nodeBySessionId(node_id) == node
        ):
            # A failed evaluation releases its preview slot; report its own error
            # rather than mistaking the release for a superseding request.
            hou.ui.removeEventLoopCallback(poll)
            report(job.error)
            return
        if (
            refresh_generation != _refresh_generation
            or hou.nodeBySessionId(node_id) != node
            or (_preview_jobs.get(node_id) is not job if job.preview_mode else _refresh_job is not job)
        ):
            job.cancel("Bounds refresh node was deleted or superseded")
            hou.ui.removeEventLoopCallback(poll)
            return
        if not job.done.is_set():
            return
        hou.ui.removeEventLoopCallback(poll)
        try:
            job.publish()
            node.cook(force=True)
        except Exception as error:
            report(error)

    hou.ui.addEventLoopCallback(poll)
    return job


def join_evaluation(key):
    """Adopt a requested canonical member of the active bounded refresh batch."""
    with _refresh_lock:
        job = _refresh_job
        if job is None or key not in job.resolved_keys or job.generation != _refresh_generation:
            return None
    job.done.wait()
    try:
        return job.accept(key)
    except Exception as error:
        job.error = error
        job.close()
        return None  # stale/cancelled work falls through to normal render retries
