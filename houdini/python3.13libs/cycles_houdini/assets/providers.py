# SPDX-License-Identifier: Apache-2.0
"""Asset providers: where records come from.

`BlenderWorkerProvider` (option A, default) reads .blend files with the
headless Blender worker and caches the result on the Houdini side.
`BlenderListingProvider` (option B) reads an existing official Blender remote
listing (`_asset-library-meta.json` + `_v1/`) from the library folder.

`scan()` blocks and is meant for a background thread; providers never call
`hou` or Qt. Construct providers on the host thread: the Blender discovery
inputs are snapshotted there.
"""

from dataclasses import dataclass, field
import json
import os
import re
import subprocess
import time
import urllib.parse

from . import catalogs as _catalogs
from .cache import IndexCache, normalize_signature
from .libraries import Library, PROVIDER_KINDS
from .records import make_record

BATCH_SIZE = 32
META_FILENAME = "_asset-library-meta.json"
LISTING_DIRNAME = "_v1"
_MAX_JSON_BYTES = 64 * 1024 * 1024
_MAX_PAGES = 10000
_RETRY_INITIAL_SECONDS = 5.0
_RETRY_MAX_SECONDS = 300.0
_SHA256 = re.compile(r"^(?:sha256:)?([0-9a-f]{64})$", re.IGNORECASE)


@dataclass
class ScanResult:
    records: list = field(default_factory=list)
    errors: list = field(default_factory=list)  # [{"file": rel, "source": abs, "message": str}]
    scanned: int = 0
    reused: int = 0
    blender_missing: bool = False
    message: str = ""
    cancelled: bool = False


def _as_library(library):
    if isinstance(library, Library):
        return library
    path = os.fspath(library)
    return Library(name=os.path.basename(os.path.normpath(path)) or path, path=path)


def _cancel_check(cancelled):
    if cancelled is None:
        return lambda: False
    if hasattr(cancelled, "is_set"):
        return cancelled.is_set
    return cancelled


class _CancelEvent:
    """Event-like adapter for blend_worker, which polls `is_set()`."""

    def __init__(self, check):
        self.is_set = check


def file_signature(path):
    """Native Windows signature when available, else (mtime_ns, ctime_ns, size, inode)."""
    from ..blender.file_signature import native_signature

    signature = native_signature(os.fspath(path))
    if signature is not None:
        return signature
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return [stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino]


def find_blend_files(root, errors=None):
    """Absolute *.blend paths under root, sorted; skips hidden dirs and the root `_v1` listing.

    Directory read failures are appended to `errors` (a list) when given.
    """
    found = []
    root = os.path.abspath(root)
    for directory, dirnames, filenames in os.walk(root, onerror=errors.append if errors is not None else None):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if not name.startswith(".") and not (directory == root and name == LISTING_DIRNAME)
        )
        for name in filenames:
            if not name.startswith(".") and name.lower().endswith(".blend"):
                found.append(os.path.join(directory, name))
    return sorted(found)


class AssetProvider:
    kind = ""

    def __init__(self, library, cache=None):
        self.library = _as_library(library)
        self.root = os.path.abspath(os.path.expanduser(self.library.path))
        self.cache = cache if cache is not None else IndexCache(self.library, namespace=self.kind)

    def catalogs(self):
        return _catalogs.load_catalogs(self.root)

    def items(self):
        """Cached records; cheap, never launches Blender."""
        return self.cache.all_records()

    def scan(self, *, cancelled=None, progress=None):
        raise NotImplementedError


class BlenderWorkerProvider(AssetProvider):
    kind = "worker"

    def __init__(self, library, cache=None, *, assets_many=None, executable=None, signature=None, clock=None):
        super().__init__(library, cache)
        self._assets_many = assets_many
        self._executable = os.fspath(executable) if executable else None
        self._signature = signature or file_signature
        self._clock = clock or time.time
        from ..core import blender_bridge

        self._discovery = (
            blender_bridge.enabled(),
            blender_bridge._configured_executable(),
            os.environ.get("PATH", ""),
            os.environ.get("ProgramFiles", "C:/Program Files"),
        )

    def _resolve_executable(self):
        """(executable, message); executable is None when Blender is unusable."""
        if self._executable:
            if os.path.isfile(self._executable):
                return self._executable, ""
            return None, "Blender not found at " + self._executable
        from ..core import blender_bridge

        enabled, configured, search_path, program_files = self._discovery
        if not enabled:
            return None, (
                "The Blender bridge is disabled for this installation. Run Cycles Setup again and select "
                "'Blender .blend asset bridge' to read asset libraries."
            )
        executable = blender_bridge._discover(configured, search_path, program_files)
        if not executable or not os.path.isfile(executable):
            return None, (
                "Blender not found at " + (configured or "PATH or Program Files/Blender Foundation") + ". Install "
                "Blender or set CYCLES_HOUDINI_BLENDER to its executable. Cached assets remain available."
            )
        return executable, ""

    def _worker_function(self):
        if self._assets_many is not None:
            return self._assets_many
        from ..blender import blend_worker

        return blend_worker.assets_many

    def _records(self, path, assets, thumbnail_dir):
        rel = self.cache.relative(path)
        records, problems = [], []
        for asset in assets if isinstance(assets, list) else ():
            if not isinstance(asset, dict) or not isinstance(asset.get("name"), str) or not asset["name"]:
                problems.append("malformed asset entry")
                continue
            if not isinstance(asset.get("id_type"), str) or not asset["id_type"]:
                problems.append("asset " + repr(asset["name"]) + " has no type")
                continue
            thumbnail, digest = None, ""
            preview = asset.get("preview")
            if isinstance(preview, str) and preview and os.path.basename(preview) == preview:
                candidate = os.path.join(thumbnail_dir, preview)
                if os.path.isfile(candidate):
                    thumbnail = candidate
                    digest = _file_sha256(candidate)
            records.append(
                make_record(
                    asset["name"],
                    asset["id_type"],
                    rel,
                    source=path,
                    meta=asset.get("meta"),
                    thumbnail=thumbnail,
                    thumbnail_hash=digest,
                    catalog_simple_name=asset.get("catalog_simple_name") or "",
                    object_type=asset.get("object_type"),
                    provider="worker",
                )
            )
        return records, problems

    def scan(self, *, cancelled=None, progress=None, force=False):
        """Read changed .blend files in batches; reuse cache entries whose signature matches.

        `cancelled` is a callable or an Event; `progress(done, total)` counts files.
        `force=True` re-reads every file.
        Transient per-file failures preserve the last listing; normal scans
        retry after 5 s, doubling up to 5 minutes. Changed signatures retry now.
        """
        is_cancelled = _cancel_check(cancelled)
        report = progress or (lambda done, total: None)
        result = ScanResult()
        if not os.path.isdir(self.root):
            result.records = self.cache.all_records()
            result.message = "Library folder not found: " + self.root
            return result
        walk_errors = []
        files = find_blend_files(self.root, walk_errors)
        for error in walk_errors:
            result.errors.append({"file": "", "source": getattr(error, "filename", "") or "", "message": str(error)})
        total = len(files)
        report(0, total)
        pending, resolved, skipped = [], {}, 0
        for path in files:
            # blend_worker deduplicates resolved sources; a linked duplicate would break reply order.
            real = os.path.normcase(os.path.realpath(path))
            if resolved.setdefault(real, path) != path:
                message = "Same file as " + self.cache.relative(resolved[real]) + "; read once"
                result.errors.append({"file": self.cache.relative(path), "source": path, "message": message})
                skipped += 1
                continue
            signature = self._signature(path)
            if signature is None:
                skipped += 1
                continue
            entry = self.cache.scan_metadata(path) or {}
            failed = bool(entry.get("error")) and normalize_signature(signature) == entry.get(
                "failure_signature", entry.get("signature")
            )
            # Old unclassified error entries must heal too. Only explicitly
            # invalid files are negative-cached for their unchanged signature.
            now = self._clock()
            defer_failure = failed and (
                entry.get("error_kind") == "invalid_file"
                or now < entry.get("retry_after", 0) <= now + _RETRY_MAX_SECONDS
            )
            if not force and (
                defer_failure or (not failed and entry.get("signature") == normalize_signature(signature))
            ):
                result.reused += 1
                error = entry.get("error") if defer_failure else None
                if error:
                    result.errors.append({"file": self.cache.relative(path), "source": path, "message": error})
            else:
                pending.append((path, signature))
        if not walk_errors:
            # An unreadable subfolder (offline share) must not erase its cached records.
            self.cache.prune(files)
        done = result.reused + skipped
        report(done, total)
        if pending and not is_cancelled():
            executable, message = self._resolve_executable()
            if executable is None:
                result.blender_missing = True
                result.message = message
            else:
                self._read(pending, executable, is_cancelled, result, lambda count: report(done + count, total))
        if is_cancelled():
            result.cancelled = True
            result.message = result.message or "Scan cancelled"
        result.records = self.cache.all_records()
        return result

    def _read(self, pending, executable, is_cancelled, result, report):
        assets_many = self._worker_function()
        thumbnail_dir = os.fspath(self.cache.thumbnails_dir)
        done = 0
        for start in range(0, len(pending), BATCH_SIZE):
            if is_cancelled():
                return
            batch = pending[start : start + BATCH_SIZE]
            sources = [path for path, _ in batch]
            try:
                replies = assets_many(
                    sources,
                    executable,
                    thumbnail_dir,
                    timeout=60 + 20 * len(batch),
                    cancelled=_CancelEvent(is_cancelled),
                )
                if not isinstance(replies, list) or len(replies) != len(batch):
                    raise ValueError("Malformed Blender asset response")
            except Exception as error:
                if is_cancelled():
                    return
                for path in sources:
                    result.errors.append({"file": self.cache.relative(path), "source": path, "message": str(error)})
                done += len(batch)
                report(done)
                continue
            for (path, signature), reply in zip(batch, replies):
                result.scanned += 1
                rel = self.cache.relative(path)
                if not isinstance(reply, dict):
                    result.errors.append({"file": rel, "source": path, "message": "Malformed Blender asset response"})
                    continue
                if normalize_signature(self._signature(path)) != normalize_signature(signature):
                    result.errors.append({"file": rel, "source": path, "message": "File changed while it was read"})
                    continue
                if reply.get("error"):
                    message = str(reply["error"])
                    kind = "invalid_file" if reply.get("error_kind") == "invalid_file" else "transient"
                    previous = self.cache.scan_metadata(path) or {}
                    attempts = (
                        previous.get("failure_attempts", 0)
                        if previous.get("failure_signature") == normalize_signature(signature)
                        else 0
                    )
                    delay = min(_RETRY_MAX_SECONDS, _RETRY_INITIAL_SECONDS * 2 ** min(int(attempts), 16))
                    self.cache.put_failure(path, signature, message, kind=kind, retry_after=self._clock() + delay)
                    result.errors.append({"file": rel, "source": path, "message": message})
                    continue
                records, problems = self._records(path, reply.get("assets"), thumbnail_dir)
                self.cache.put(path, signature, records)
                for problem in problems:
                    result.errors.append({"file": rel, "source": path, "message": problem})
            done += len(batch)
            report(done)


def _file_sha256(path):
    import hashlib

    digest = hashlib.sha256()
    try:
        with open(path, "rb") as stream:
            for block in iter(lambda: stream.read(1 << 20), b""):
                digest.update(block)
    except OSError:
        return ""
    return digest.hexdigest()


def _sha256_hex(value):
    match = _SHA256.match(value) if isinstance(value, str) else None
    return match.group(1).lower() if match else ""


def _inside(root, relative, *, url=False):
    """Absolute path for a library-relative path or URL, or None if it escapes root."""
    if not isinstance(relative, str) or not relative or "\0" in relative:
        return None
    if url:
        parsed = urllib.parse.urlsplit(relative)
        if parsed.scheme or parsed.netloc:
            return None
        relative = urllib.parse.unquote(parsed.path)
    relative = relative.replace("\\", "/")
    if relative.startswith("/") or re.match(r"^[A-Za-z]:", relative):
        return None
    path = os.path.normpath(os.path.join(root, *relative.split("/")))
    real_root = os.path.realpath(root)
    try:
        if os.path.commonpath([os.path.realpath(path), real_root]) != real_root:
            return None
    except ValueError:
        return None
    return path


def _load_json(path):
    try:
        if os.path.getsize(path) > _MAX_JSON_BYTES:
            raise ValueError("exceeds 64 MiB")
        with open(path, "rb") as stream:
            return json.loads(stream.read().decode("utf-8-sig"))
    except (OSError, ValueError, UnicodeError) as error:
        raise ValueError(os.path.basename(path) + ": " + str(error)) from None


@dataclass
class Listing:
    name: str = ""
    schema_version: str = ""
    catalogs: object = None  # list from the index, or None when absent
    assets: list = field(default_factory=list)  # (record, file signature)
    warnings: list = field(default_factory=list)
    complete: bool = True  # False when a page could not be read


def read_listing(root):
    """Parse `<root>/_asset-library-meta.json` and its v1 index and pages.

    Raises ValueError when there is no usable listing. Bad pages and assets are
    skipped with warnings. Thumbnails stay where the listing put them (often WebP).
    """
    root = os.path.abspath(root)
    meta = _load_json(os.path.join(root, META_FILENAME))
    if not isinstance(meta, dict) or not isinstance(meta.get("api_versions"), dict):
        raise ValueError(META_FILENAME + " has no api_versions")
    v1 = meta["api_versions"].get("v1")
    index_path = _inside(root, v1.get("url"), url=True) if isinstance(v1, dict) else None
    if index_path is None:
        raise ValueError(META_FILENAME + " has no usable v1 index URL")
    index = _load_json(index_path)
    if not isinstance(index, dict) or not isinstance(index.get("pages"), list):
        raise ValueError("Asset index has no pages")
    listing = Listing(
        name=meta.get("name") if isinstance(meta.get("name"), str) else "",
        schema_version=index.get("schema_version") if isinstance(index.get("schema_version"), str) else "",
        catalogs=index.get("catalogs") if isinstance(index.get("catalogs"), list) else None,
    )
    if listing.schema_version and not listing.schema_version.startswith("1."):
        listing.warnings.append("Unsupported listing schema " + listing.schema_version + "; reading anyway")
    simple_names = {}
    for catalog in listing.catalogs or ():
        if isinstance(catalog, dict) and isinstance(catalog.get("uuids"), list):
            for uuid in catalog["uuids"]:
                if isinstance(uuid, str) and isinstance(catalog.get("simple_name"), str):
                    simple_names.setdefault(_catalogs.normalize_uuid(uuid), catalog["simple_name"])
    seen = set()
    for number, page in enumerate(index["pages"][:_MAX_PAGES]):
        page_path = _inside(root, page.get("url"), url=True) if isinstance(page, dict) else None
        if page_path is None:
            listing.warnings.append(f"Page {number}: unusable URL")
            listing.complete = False
            continue
        try:
            data = _load_json(page_path)
        except ValueError as error:
            listing.warnings.append(f"Page {number}: {error}")
            listing.complete = False
            continue
        if not isinstance(data, dict) or not isinstance(data.get("assets"), list):
            listing.warnings.append(f"Page {number}: no asset list")
            listing.complete = False
            continue
        hashes = {
            item["path"]: item.get("hash")
            for item in data.get("files", ())
            if isinstance(item, dict) and isinstance(item.get("path"), str)
        }
        for asset in data["assets"]:
            record = _listing_record(root, asset, simple_names, listing.warnings)
            if record is None or record["ch"]["key"] in seen:
                continue
            seen.add(record["ch"]["key"])
            rel = record["files"][0]
            signature = ["listing", hashes.get(rel) if isinstance(hashes.get(rel), str) else page.get("hash", "")]
            listing.assets.append((record, signature))
    return listing


def _listing_record(root, asset, simple_names, warnings):
    if not isinstance(asset, dict) or not isinstance(asset.get("name"), str) or not asset["name"]:
        warnings.append("Skipped a malformed asset entry")
        return None
    name = asset["name"]
    files = asset.get("files")
    if not isinstance(asset.get("id_type"), str) or not asset["id_type"]:
        warnings.append("Asset " + repr(name) + ": missing id_type")
        return None
    if not isinstance(files, list) or not files or not all(isinstance(f, str) for f in files):
        warnings.append("Asset " + repr(name) + ": missing files")
        return None
    source = _inside(root, files[0])
    if source is None:
        warnings.append("Asset " + repr(name) + ": file outside the library")
        return None
    rel = os.path.relpath(source, root).replace("\\", "/")
    thumbnail, digest = None, ""
    thumb = asset.get("thumbnail")
    if isinstance(thumb, dict):
        path = _inside(root, thumb.get("url"), url=True)
        if path is not None and os.path.isfile(path):
            thumbnail, digest = path, _sha256_hex(thumb.get("hash"))
    meta = asset.get("meta") if isinstance(asset.get("meta"), dict) else {}
    return make_record(
        name,
        asset["id_type"],
        rel,
        source=source,
        meta=meta,
        thumbnail=thumbnail,
        thumbnail_hash=digest,
        catalog_simple_name=simple_names.get(_catalogs.normalize_uuid(meta.get("catalog_id")), ""),
        provider="listing",
        files=[rel] + [f.replace("\\", "/") for f in files[1:]],
    )


class BlenderListingProvider(AssetProvider):
    """Reads an official Blender remote listing already present in the library."""

    kind = "listing"

    def has_listing(self):
        return os.path.isfile(os.path.join(self.root, META_FILENAME))

    def catalogs(self):
        """The listing's catalogs when present, else blender_assets.cats.txt."""
        try:
            listing = read_listing(self.root)
        except ValueError:
            listing = None
        if listing is not None and listing.catalogs is not None:
            return _catalogs.tree_from_entries(listing.catalogs)
        return _catalogs.load_catalogs(self.root)

    def items(self):
        records = self.cache.all_records()
        if records or not self.has_listing():
            return records
        try:
            return [record for record, _ in read_listing(self.root).assets]
        except ValueError:
            return []

    def scan(self, *, cancelled=None, progress=None, force=False):
        is_cancelled = _cancel_check(cancelled)
        result = ScanResult()
        if not os.path.isdir(self.root):
            result.records = self.cache.all_records()
            result.message = "Library folder not found: " + self.root
            return result
        try:
            listing = read_listing(self.root)
        except ValueError as error:
            result.records = self.cache.all_records()
            result.message = "No Blender asset listing in this library (" + str(error) + ")"
            return result
        grouped = {}
        for record, signature in listing.assets:
            grouped.setdefault(record["files"][0], (signature, []))[1].append(record)
        total = len(grouped)
        if progress:
            progress(0, total)
        for done, (rel, (signature, records)) in enumerate(sorted(grouped.items()), 1):
            if is_cancelled():
                result.cancelled = True
                result.message = "Scan cancelled"
                break
            if not force and self.cache.get(rel, signature) is not None:
                result.reused += 1
            else:
                self.cache.put(rel, signature, records)
                result.scanned += 1
            if progress:
                progress(done, total)
        if not result.cancelled and listing.complete:
            self.cache.prune(list(grouped))
        result.errors = [{"file": "", "source": "", "message": warning} for warning in listing.warnings]
        result.records = self.cache.all_records()
        return result

    def generate_listing(self, executable, *, timeout=600):
        """Run Blender's official listing generator on this library.

        WRITES INTO THE LIBRARY FOLDER: `_asset-library-meta.json`, `_v1/` and a
        `<blend name>_thumbnails/` folder beside every .blend. The caller must
        get the artist's confirmation first. Runs
        `<blender> -b --factory-startup -c asset_listing generate <library>`;
        raises RuntimeError on failure.
        """
        from ..blender.blend_worker import blender_environment

        command = [os.fspath(executable), "-b", "--factory-startup", "-c", "asset_listing", "generate", self.root]
        try:
            completed = subprocess.run(
                command,
                check=False,
                env=blender_environment(os.environ.get("CYCLES_HOUDINI_BLENDER_OCIO") or None),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError("Blender listing generation failed: " + str(error)) from None
        if completed.returncode != 0 or not self.has_listing():
            tail = (completed.stderr or completed.stdout or "").strip().splitlines()[-12:]
            raise RuntimeError(
                f"Blender listing generation failed (exit code {completed.returncode})"
                + (":\n" + "\n".join(tail) if tail else "")
            )


def make_provider(library, kind=None, **options):
    """Provider for `library`; kind defaults to the library's own provider setting."""
    library = _as_library(library)
    kind = kind or library.provider
    if kind not in PROVIDER_KINDS:
        raise ValueError("Unknown asset provider " + repr(kind))
    return (BlenderListingProvider if kind == "listing" else BlenderWorkerProvider)(library, **options)
