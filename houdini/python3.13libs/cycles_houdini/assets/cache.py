# SPDX-License-Identifier: Apache-2.0
"""Houdini-side index cache for one asset library.

Layout under cache_root():
    <sha256(normcased abs library path)[:20]>/
        library.json                      human-readable owner path
        thumbnails/                       PNG previews written by the worker
        <namespace>/files/<sha256(rel)[:24]>.json
                                          {"version", "file", "signature", "records", "error"}
                                          + optional failed-attempt signature/kind/retry metadata
        <namespace>/importability.json    {key: {"state", "reason", "signature"}}

`namespace` is the provider kind so worker and listing records never mix. The
library folder itself is never written. Thread-safe: one scan thread may write
while the host thread reads.
"""

import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import threading

from . import libraries as _libraries
from .records import validate_record

CACHE_VERSION = 1
_MAX_ENTRY_BYTES = 32 * 1024 * 1024


def cache_root():
    explicit = os.environ.get("CYCLES_HOUDINI_ASSET_CACHE", "").strip()
    if explicit:
        return Path(os.path.expanduser(explicit))
    prefs = _libraries.user_pref_dir()
    if prefs is not None:
        return prefs / "cycles_houdini" / "asset_cache"
    return Path.home() / ".cycles-houdini" / "asset_cache"


def normalize_signature(value):
    """JSON-stable form: tuples become lists recursively; scalars pass through."""
    if isinstance(value, (list, tuple)):
        return [normalize_signature(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _failure_attempts(value):
    return min(max(value, 0), 32) if type(value) is int else 0


def _retry_after(value):
    if type(value) not in (int, float):
        return 0.0
    try:
        return float(value) if value >= 0 and math.isfinite(value) else 0.0
    except (OverflowError, ValueError):
        return 0.0


def _hash(text, length):
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()[:length]


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _read_json(path):
    try:
        if path.stat().st_size > _MAX_ENTRY_BYTES:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class IndexCache:
    def __init__(self, library, root=None, *, namespace="worker"):
        path = library.path if hasattr(library, "path") else os.fspath(library)
        self.library_root = os.path.abspath(os.path.expanduser(path))
        self.directory = Path(root if root is not None else cache_root()) / _hash(
            _libraries.library_key(self.library_root), 20
        )
        self.namespace = namespace
        self._files_dir = self.directory / namespace / "files"
        self._importability_path = self.directory / namespace / "importability.json"
        self._lock = threading.RLock()
        self._entries = None
        self._importability = None

    @property
    def thumbnails_dir(self):
        path = self.directory / "thumbnails"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def relative(self, blend_path):
        """POSIX path relative to the library root; outside paths stay absolute."""
        path = os.fspath(blend_path)
        if not os.path.isabs(path):
            return path.replace("\\", "/").lstrip("/")
        path = os.path.abspath(path)
        try:
            rel = os.path.relpath(path, self.library_root)
        except ValueError:
            return path.replace("\\", "/")
        if rel == os.pardir or rel.startswith(os.pardir + os.sep):
            return path.replace("\\", "/")
        return rel.replace("\\", "/")

    def _key(self, rel):
        return os.path.normcase(rel) if os.name == "nt" else rel

    def _entry_path(self, rel):
        return self._files_dir / (_hash(self._key(rel), 24) + ".json")

    def _load(self):
        if self._entries is not None:
            return self._entries
        self._entries = {}
        if self._files_dir.is_dir():
            for path in self._files_dir.glob("*.json"):
                entry = _read_json(path)
                if (
                    not isinstance(entry, dict)
                    or entry.get("version") != CACHE_VERSION
                    or not isinstance(entry.get("file"), str)
                    or not isinstance(entry.get("records"), list)
                    or path.name != self._entry_path(entry["file"]).name
                ):
                    continue
                entry["records"] = [record for record in entry["records"] if validate_record(record)]
                if not isinstance(entry.get("error"), str):
                    entry["error"] = None
                self._entries[self._key(entry["file"])] = entry
        return self._entries

    def _importability_map(self):
        if self._importability is None:
            data = _read_json(self._importability_path)
            self._importability = data if isinstance(data, dict) else {}
        return self._importability

    def _overlay(self, entry):
        stored = self._importability_map()
        result = []
        for record in entry["records"]:
            verdict = stored.get(record["ch"]["key"])
            if isinstance(verdict, dict) and verdict.get("signature") == entry["signature"]:
                record = dict(
                    record, ch=dict(record["ch"], importable={"state": verdict["state"], "reason": verdict["reason"]})
                )
            result.append(record)
        return result

    def get(self, blend_path, signature):
        """Records for a successful matching signature, else None.

        Failed attempts keep that signature/listing and separate retry metadata.
        Legacy empty error entries are migrated by the worker provider on scan.
        """
        with self._lock:
            entry = self._load().get(self._key(self.relative(blend_path)))
            if entry is None or signature is None or entry.get("signature") != normalize_signature(signature):
                return None
            return self._overlay(entry)

    def put(self, blend_path, signature, records, error=None):
        rel = self.relative(blend_path)
        entry = {
            "version": CACHE_VERSION,
            "file": rel,
            "signature": normalize_signature(signature),
            "records": [record for record in records if validate_record(record)],
            "error": str(error) if error else None,
        }
        with self._lock:
            entries = self._load()
            old = entries.get(self._key(rel))
            _write_json(self._entry_path(rel), entry)
            entries[self._key(rel)] = entry
            if old is not None:
                self._drop_thumbnails(old["records"])
            if old is not None and old.get("signature") != entry["signature"]:
                self._forget_importability(old["records"])

    def entry(self, blend_path):
        """Raw entry dict (signature, records, error) or None."""
        with self._lock:
            entry = self._load().get(self._key(self.relative(blend_path)))
            return json.loads(json.dumps(entry)) if entry else None

    def scan_metadata(self, blend_path):
        """Small defensive scan-decision snapshot; never copy/materialize records.

        Keep persisted legacy/malformed retry fields from wedging a scan. The
        provider additionally bounds a valid deadline relative to its clock.
        """
        with self._lock:
            entry = self._load().get(self._key(self.relative(blend_path)))
            if entry is None:
                return None
            return dict(
                signature=normalize_signature(entry.get("signature")),
                error=entry.get("error") if isinstance(entry.get("error"), str) else None,
                error_kind="invalid_file" if entry.get("error_kind") == "invalid_file" else "transient",
                failure_signature=normalize_signature(entry.get("failure_signature", entry.get("signature"))),
                failure_attempts=_failure_attempts(entry.get("failure_attempts")),
                retry_after=_retry_after(entry.get("retry_after")),
            )

    def put_failure(self, blend_path, signature, error, *, kind="transient", retry_after=0):
        """Remember a failed attempt without replacing the last successful listing.

        The attempt signature is separate: stale records and importability still
        belong to the last successful signature. Also works before a first success.
        """
        rel = self.relative(blend_path)
        signature = normalize_signature(signature)
        with self._lock:
            entries = self._load()
            old = entries.get(self._key(rel)) or {}
            attempts = (
                _failure_attempts(old.get("failure_attempts")) if old.get("failure_signature") == signature else 0
            )
            entry = dict(
                version=CACHE_VERSION,
                file=rel,
                signature=old.get("signature"),
                records=old.get("records", []),
                error=str(error),
                error_kind="invalid_file" if kind == "invalid_file" else "transient",
                failure_signature=signature,
                failure_attempts=min(int(attempts) + 1, 32),
                retry_after=_retry_after(retry_after),
            )
            _write_json(self._entry_path(rel), entry)
            entries[self._key(rel)] = entry

    def errors(self):
        with self._lock:
            return {entry["file"]: entry["error"] for entry in self._load().values() if entry.get("error")}

    def all_records(self):
        with self._lock:
            entries = sorted(self._load().values(), key=lambda entry: entry["file"])
            return [record for entry in entries for record in self._overlay(entry)]

    def files(self):
        with self._lock:
            return sorted(entry["file"] for entry in self._load().values())

    def prune(self, existing_blend_paths):
        """Remove entries for files not listed (deleted or moved)."""
        keep = {self._key(self.relative(path)) for path in existing_blend_paths}
        with self._lock:
            entries = self._load()
            for key in [key for key in entries if key not in keep]:
                entry = entries.pop(key)
                try:
                    self._entry_path(entry["file"]).unlink()
                except OSError:
                    pass
                self._drop_thumbnails(entry["records"])
                self._forget_importability(entry["records"])

    def clear(self):
        self.prune(())

    def set_importability(self, key, state, reason, *, signature=None):
        """Store a verdict until the owning file's signature changes."""
        if state not in ("yes", "no", "unknown"):
            raise ValueError("Importability state must be yes, no or unknown")
        with self._lock:
            if signature is None:
                owner = next(
                    (
                        entry
                        for entry in self._load().values()
                        if any(record["ch"]["key"] == key for record in entry["records"])
                    ),
                    None,
                )
                if owner is None:
                    raise KeyError("No cached asset record " + repr(key))
                signature = owner["signature"]
            stored = self._importability_map()
            stored[key] = {"state": state, "reason": str(reason or ""), "signature": normalize_signature(signature)}
            _write_json(self._importability_path, stored)

    def _forget_importability(self, records):
        stored = self._importability_map()
        keys = [record["ch"]["key"] for record in records if record["ch"]["key"] in stored]
        if keys:
            for key in keys:
                stored.pop(key)
            _write_json(self._importability_path, stored)

    def _drop_thumbnails(self, records):
        """Delete thumbnails owned by this cache that no current record references."""
        directory = os.path.normcase(os.path.abspath(self.directory / "thumbnails"))
        referenced = {
            os.path.normcase(os.path.abspath(record["thumbnail"]["url"]))
            for entry in self._load().values()
            for record in entry["records"]
            if record.get("thumbnail")
        }
        for record in records:
            if not record.get("thumbnail"):
                continue
            path = os.path.normcase(os.path.abspath(record["thumbnail"]["url"]))
            if os.path.dirname(path) == directory and path not in referenced:
                try:
                    os.unlink(path)
                except OSError:
                    pass
