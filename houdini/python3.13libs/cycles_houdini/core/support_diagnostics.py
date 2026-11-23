# SPDX-License-Identifier: Apache-2.0
"""Local, reviewable support snapshots. No network or scene-file collection.

Safe snapshots deliberately omit free-form text instead of trying to recognize
every spelling of a private path. The opt-in member holds bounded raw details.
The preview and ZIP consume the very same frozen bytes.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from types import MappingProxyType
import zipfile

LOG_LIMIT = 64 * 1024
DETAIL_LIMIT = 256 * 1024
STATES = frozenset(("idle", "rendering", "paused", "complete", "failed", "cancelled", "closing", "closed"))
NUMBERS = frozenset(
    "progress generation current_sample elapsed_seconds first_image_seconds cooking_seconds extraction_seconds device_allocation_bytes device_allocation_peak_bytes owned_geometry_bytes geometry_conversions renderer_pid extractions cache_hits coalesced_updates object_updates geometry_resources object_resources material_resources light_resources image_iris_preparations image_iris_table_bytes image_iris_preparation_seconds packed_instances".split()
)
FLAGS = frozenset(
    "pending_update update_rejected capture_pending capture_restart_required color_update_pending volume_ray_marching support_status_unavailable support_diagnostics_unavailable".split()
)
DIAGNOSTIC_NUMBERS = frozenset(
    "generation core_update_seconds cancel_join_seconds shader_graph_preparation_seconds display_updates display_conversion_seconds output_deliveries output_delivery_seconds material_builds light_updates background_builds cancel_output_deliveries cancel_display_updates cancel_output_delivery_seconds cancel_display_conversion_seconds engine_snapshot_wait_seconds output_reads_skipped display_updates_skipped cancel_output_reads_skipped cancel_display_updates_skipped scene_boundary_wait_seconds session_cancel_calls volume_vector_assembly_seconds".split()
)


def _json(value):
    return (json.dumps(value, indent=2, ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _digest(value):
    return value if isinstance(value, str) and re.fullmatch(r"[a-fA-F0-9]{7,64}", value) else None


def safe_status(value):
    """Only known typed counters/enums cross the default privacy boundary."""
    result = {key: value[key] for key in NUMBERS if _number(value.get(key))}
    result.update({key: value[key] for key in FLAGS if type(value.get(key)) is bool})
    for key in ("state", "backend_state"):
        if value.get(key) in STATES:
            result[key] = value[key]
    result["source_diagnostic_count"] = len(value.get("source_diagnostics", ()))
    result["motion_warning_count"] = len(value.get("motion_warnings", ()))
    rendering = value.get("rendering", {})
    result["rendering"] = {key: rendering[key] for key in ("width", "height", "samples") if _number(rendering.get(key))}
    result["device_memory_scope"] = (
        "device_allocation_bytes is Cycles tracked allocations for this session; "
        "it is not total GPU usage, physical residency, or free device memory."
    )
    return result


def _bounded(value, depth=0, budget=None):
    if budget is None:
        budget = [DETAIL_LIMIT // 2, 2048]
    if budget[0] <= 0 or budget[1] <= 0:
        return "[total budget limit]"
    budget[1] -= 1
    budget[0] -= 16  # Punctuation, indentation and separators also cost space.
    if depth >= 8:
        return "[depth limit]"
    if value is None or type(value) is bool or _number(value):
        return value
    if isinstance(value, str):
        result = value[:4096] + (" [truncated]" if len(value) > 4096 else "")
        cost = len(json.dumps(result, ensure_ascii=True))
        if cost > budget[0]:
            budget[0] = 0
            return "[byte limit]"
        budget[0] -= cost
        return result
    if isinstance(value, dict):
        result = {}
        items = iter(value.items())
        for _ in range(128):
            if budget[0] <= 0 or budget[1] <= 0:
                result["__truncated__"] = "[total budget limit]"
                break
            try:
                key, item = next(items)
            except StopIteration:
                break
            name = str(key)[:256]
            budget[0] -= len(json.dumps(name, ensure_ascii=True))
            result[name] = _bounded(item, depth + 1, budget)
        return result
    if isinstance(value, (tuple, list)):
        result = []
        for item in value[-64:]:
            if budget[0] <= 0 or budget[1] <= 0:
                result.append("[total budget limit]")
                break
            result.append(_bounded(item, depth + 1, budget))
        return result
    return "[unsupported value]"


def _details(value):
    data = _json(_bounded(value))
    if len(data) > DETAIL_LIMIT:
        return _json(dict(omitted="Details exceed the 256 KiB limit; omitted as a whole"))
    return data


def log_tail(path):
    """Read at most 64 KiB, and never retain the source filename in ZIP names."""
    try:
        with Path(path).open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - LOG_LIMIT))
            data = stream.read(LOG_LIMIT)
        # Normalize invalid/split UTF-8 so the text preview describes every byte
        # actually exported. Keep the normalized content within the same cap.
        data = data.decode("utf-8", errors="replace").encode("utf-8")
        data = data[-LOG_LIMIT:].decode("utf-8", errors="ignore").encode("utf-8")
        return data, dict(available=True, original_bytes=size, captured_bytes=len(data), tail_only=size > LOG_LIMIT)
    except (OSError, ValueError):
        return b"", dict(available=False, captured_bytes=0)


@dataclass(frozen=True)
class Snapshot:
    safe: object
    private: object

    def files(self, include_private=False):
        files = dict(self.safe)
        if include_private:
            files.update(self.private)
        manifest = dict(
            schema_version=1,
            include_private_paths=bool(include_private),
            external_transmission=False,
            entries=[
                dict(name=name, bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
                for name, data in sorted(files.items())
            ],
            omissions=(
                []
                if include_private
                else [
                    "Private paths, free-form status/diagnostic text, raw device names, and worker and session log contents"
                ]
            )
            + ["Scene files, images, textures, environment variables, and arbitrary filesystem scans"],
            limits=dict(worker_log_bytes=LOG_LIMIT, session_log_bytes=LOG_LIMIT, private_details_bytes=DETAIL_LIMIT),
        )
        files["manifest.json"] = _json(manifest)
        return files

    def preview(self, include_private=False):
        return "\n\n".join(
            "=== " + name + f" ({len(data)} bytes) ===\n" + data.decode("utf-8", errors="replace")
            for name, data in sorted(self.files(include_private).items())
        )

    def write(self, destination, include_private=False):
        # Exclusive creation prevents silently replacing an existing support file.
        destination = Path(destination)
        stream = destination.open("xb")
        try:
            with stream, zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name, data in sorted(self.files(include_private).items()):
                    archive.writestr(name, data)
        except BaseException:
            stream.close()
            destination.unlink(missing_ok=True)
            raise


def snapshot(
    *, status, diagnostics, capabilities, identity, private_identity=None, worker_log=None, session_log=None, devices=()
):
    safe_identity = dict(captured_utc=datetime.now(timezone.utc).isoformat())
    for key in ("python_version", "houdini_version", "built_houdini_version"):
        value = identity.get(key, [])
        safe_identity[key] = [v for v in value if type(v) is int][:4]
    # IDs/hashes are product identity, never raw paths or entire manifests.
    for key in ("release_id", "package_id", "build_manifest_sha256", "release_manifest_sha256", "runtime_fingerprint"):
        value = _digest(identity.get(key))
        if value:
            safe_identity[key] = value
    if identity.get("os") in ("Windows", "Linux", "Darwin"):
        safe_identity["os"] = identity["os"]
    if identity.get("release_purpose") in ("internal", "public"):
        safe_identity["release_purpose"] = identity["release_purpose"]
    caps = {
        key: capabilities[key]
        for key in (
            "image_iris",
            "gaussian_splats",
            "osl",
            "path_guiding",
            "native_ipr",
            "blender_bridge",
            "blender_bridge_enabled",
        )
        if type(capabilities.get(key)) is bool
    }
    caps["available"] = bool(capabilities)
    if _digest(capabilities.get("engine_commit")):
        caps["engine_commit"] = capabilities["engine_commit"]
    safe = {
        "identity.json": _json(safe_identity),
        "capabilities.json": _json(caps),
        "latest-status.json": _json(safe_status(status)),
    }
    counters = {key: diagnostics[key] for key in DIAGNOSTIC_NUMBERS if _number(diagnostics.get(key))}
    if type(diagnostics.get("enabled")) is bool:
        counters["enabled"] = diagnostics["enabled"]
    safe["diagnostics-summary.json"] = _json(counters)
    selected = status.get("render_device_ids", ())
    safe["devices.json"] = _json(
        [
            dict(index=index, backend=device["backend"], selected=device.get("id") in selected)
            for index, device in enumerate(devices[:64])
            if device.get("backend") in ("CPU", "CUDA", "OPTIX", "HIP", "ONEAPI", "METAL")
        ]
    )
    private = {
        "private/details.json": _details(
            dict(identity=private_identity or {}, latest_status=status, diagnostics=diagnostics, devices=devices)
        )
    }
    log, info = log_tail(worker_log) if worker_log else (b"", dict(available=False, captured_bytes=0))
    safe["worker-log-summary.json"] = _json(info)
    if info["available"]:
        private["private/worker-tail.log"] = log
    if session_log:
        log, info = log_tail(session_log)
        if info["available"]:
            private["private/session-tail.log"] = log
    return Snapshot(MappingProxyType(safe), MappingProxyType(private))
