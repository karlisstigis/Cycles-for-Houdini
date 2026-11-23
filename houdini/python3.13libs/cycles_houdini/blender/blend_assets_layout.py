# SPDX-License-Identifier: Apache-2.0
"""Folder layout, names and bookkeeping for Blender assets under ``assets_blender``.

Contract: the layout, names and bookkeeping rules described below (from the internal
implementation plan of 2026-11-13, not published). Pure Python: no ``hou``, ``bpy`` or Qt, so every
function here can run on a background thread.

Layout (``asset_root`` is an expanded absolute folder such as ``<hip>/assets_blender``)::

    <asset_root>/<blend>/geo/<asset>/<asset>.bgeo.sc          (or <asset>.0001.bgeo.sc ...)
    <asset_root>/<blend>/geo/<asset>/hierarchy/<object>.bgeo.sc
    <asset_root>/<blend>/geo/<asset>/hierarchy/shapes/<shape>.bgeo.sc
    <asset_root>/<blend>/tex/<image file>
    <asset_root>/<blend>/cycles_import.json

Names are real Blender names made safe for Windows (``safe_name``). A clash with a
name owned by another original in the same parent gets a readable suffix, never a
hash. ``ImportStore`` keeps ``original -> name`` in ``cycles_import.json`` so a name
never changes once given. Typical use by an import::

    blend_dir = blend_folder(asset_root, source)
    with open_store(blend_dir) as store:
        if not store.writable:
            raise ...                                   # newer schema: touch nothing
        asset = store.asset_name(original, blender_name)
        files = {geometry_file(asset): digest}
        check_path_lengths([asset_folder(blend_dir, asset) / name for name in files])
        stale = store.stale_files(original, files)       # [] unless the JSON was sound
        ... write the files ...
        store.record_asset(original, options=..., frames=..., files=files, scene=hip_path)
        store.save()
        delete_stale(asset_folder(blend_dir, asset), stale)
"""

import contextlib
import json
import ntpath
import os
import re
import time
from pathlib import Path
from typing import NamedTuple

ROOT_FOLDER = "assets_blender"
GEO_FOLDER = "geo"
TEX_FOLDER = "tex"
HIERARCHY_FOLDER = "hierarchy"
SHAPES_FOLDER = HIERARCHY_FOLDER + "/shapes"
GEOMETRY_SUFFIX = ".bgeo.sc"
JSON_NAME = "cycles_import.json"
JSON_TMP_NAME = JSON_NAME + ".tmp"
JSON_BAD_NAME = JSON_NAME + ".bad"
LOCK_NAME = "cycles_import.lock"
SCHEMA = 1
MAX_PATH = 240
LOCK_TIMEOUT = 30.0
LOCK_POLL = 0.25
LOCK_STALE_AFTER = 600.0
UDIM_TOKEN = "<UDIM>"

# Windows-invalid and control characters, and Houdini's string expansion ($VAR, `expr`).
_UNSAFE = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*$`]')
_RESERVED = re.compile(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])")
# A frame or tile number right before the extension: wood.1001.exr, render_0001.png.
_NUMBERED = re.compile(r"(.+?)([._-])(\d{4,})(\.[A-Za-z0-9]{1,16})?")
_UDIM = re.compile(re.escape(UDIM_TOKEN), re.IGNORECASE)
_TOKEN_MARK = "\ue000"  # private-use stand-in for <UDIM> while sanitising


class PathTooLongError(ValueError):
    """An import would write a path longer than the limit (blend-import.md: Folder layout)."""

    def __init__(self, path, limit=MAX_PATH):
        self.path, self.limit = str(path), limit
        super().__init__(
            f"The import would write a path of {len(self.path)} characters, over the {limit}-character "
            f"limit: {self.path}. Save the scene in a shorter folder or use shorter names."
        )


class UnsafePathError(ValueError):
    """A relative path from the bookkeeping would leave its folder."""


class ForeignStoreError(RuntimeError):
    """cycles_import.json was written by a newer version; it must not be changed."""


class ImportLockError(TimeoutError):
    """cycles_import.lock stayed held by another import for longer than the timeout."""


class DeleteResult(NamedTuple):
    removed: list  # relative paths deleted
    failed: list  # (relative path, error text)


# ------------------------------------------------------------------ names


def safe_name(text, fallback):
    """One portable file or folder name: Windows-invalid characters, control characters,
    ``$`` and backtick become ``_``; surrounding whitespace and trailing dots/spaces are
    stripped; Windows device names (``CON``, ``con.txt``, ``LPT1``...) get a ``_`` prefix;
    an empty result gives ``fallback``. Spaces inside and non-ASCII letters are kept."""
    text = _UNSAFE.sub("_", "" if text is None else str(text)).strip().rstrip(" .")
    if not text:
        return fallback
    if _RESERVED.fullmatch(text.split(".", 1)[0].rstrip(" ")):
        text = "_" + text
    return text


def safe_image_name(file_name):
    """``safe_name`` of a file path's last component, keeping a ``<UDIM>`` token intact."""
    name = _components(file_name)[-1]
    return safe_name(_UDIM.sub(_TOKEN_MARK, name), "image").replace(_TOKEN_MARK, UDIM_TOKEN)


def file_parts(name):
    """(stem, tail): a clash suffix goes between them. The tail holds the UDIM token or a
    trailing frame/tile number with its separator, and the extension."""
    match = _UDIM.search(name)
    if match:
        head = name[: match.start()]
        stem = head.rstrip("._-")
        return stem, name[len(stem) :]
    match = _NUMBERED.fullmatch(name)
    if match:
        return match.group(1), name[len(match.group(1)) :]
    dot = name.rfind(".")
    if 0 < dot and len(name) - dot <= 17:
        return name[:dot], name[dot:]
    return name, ""


def file_key(name):
    """Case-insensitive identity of a logical image: every tile/frame of one pattern alike."""
    stem, tail = file_parts(name)
    tail = re.sub(r"\d{4,}", "#", _UDIM.sub("#", tail))
    return (stem + tail).casefold()


def _suffixed(name, index, file):
    if not file:
        return f"{name}_{index}"
    stem, tail = file_parts(name)
    return f"{stem}_{index}{tail}"


def unique_name(name, taken, *, file=False):
    """``name``, or ``name_2``, ``name_3``... (before the extension, UDIM token or frame
    number when ``file``) so that it does not equal any of ``taken``, case-insensitively.
    For files, all tiles/frames of one pattern count as one name."""
    key = file_key if file else str.casefold
    used = {key(item) for item in taken}
    candidate, index = name, 2
    while key(candidate) in used:
        candidate, index = _suffixed(name, index, file), index + 1
    return candidate


def image_tile_name(pattern, number):
    """File name of one tile/frame of an allocated image name: the UDIM token or the
    trailing frame number replaced by ``number``; a plain name is returned unchanged."""
    if number is None:
        return pattern
    if _UDIM.search(pattern):
        return _UDIM.sub(str(number), pattern, count=1)
    match = _NUMBERED.fullmatch(pattern)
    if not match:
        return pattern
    digits = match.group(3)
    return match.group(1) + match.group(2) + str(int(number)).zfill(len(digits)) + (match.group(4) or "")


# ------------------------------------------------------------------ paths


def same_path(a, b):
    """True when two path texts name the same file on Windows: case and ``\\``/``/`` ignored."""
    if not isinstance(a, str) or not isinstance(b, str) or not a or not b:
        return False
    return ntpath.normcase(ntpath.normpath(a)) == ntpath.normcase(ntpath.normpath(b))


def _posix(path):
    return str(path).replace("\\", "/")


def asset_root(base):
    """``<base>/assets_blender`` for an expanded $HIP or user-cache folder."""
    return Path(base) / ROOT_FOLDER


def _listing(folder, *, dirs):
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return []
    names = []
    for entry in entries:
        try:
            if dirs is None or entry.is_dir() == dirs:
                names.append(entry.name)
        except OSError:
            continue
    return names


def _owner(blend_dir):
    """Source .blend recorded in a blend folder's JSON, or None when unknown."""
    try:
        data = json.loads((Path(blend_dir) / JSON_NAME).read_text(encoding="utf-8"))
        path = data["source"]["path"]
    except (OSError, ValueError, TypeError, KeyError):
        return None
    return path if isinstance(path, str) else None


def _components(path):
    """Path text split on both separators (Blender's ``//`` prefix is not a UNC share here)."""
    return [part for part in re.split(r"[\\/]+", str(path)) if part] or [""]


def blend_folder_name(source):
    """The blend folder's preferred name: the safe stem of the .blend file (blend-import.md: Folder layout)."""
    name = _components(source)[-1]
    stem = name[: -len(".blend")] if name.lower().endswith(".blend") else name
    return safe_name(stem, "blend")


def _parent_name(source):
    parts = _components(source)
    parent = parts[-2] if len(parts) > 1 else ""
    return safe_name("" if parent.endswith(":") else parent, "root")


def blend_folder(asset_root, source):
    """Blend folder for ``source`` under ``asset_root`` (not created).

    The folder whose JSON names ``source`` is reused. Otherwise the first free name of
    ``<stem>``, ``<stem>__<parent folder>``, ``<stem>__<parent folder>_2``, ...
    (case-insensitive). An existing folder without a readable JSON belongs to an unknown
    .blend and is never reused."""
    root = Path(asset_root)
    stem = blend_folder_name(source)
    folders = {name.casefold(): name for name in _listing(root, dirs=True)}
    taken = {name.casefold() for name in _listing(root, dirs=None)}  # a stray file blocks its name too
    prefix = stem.casefold()
    for key, name in sorted(folders.items()):
        if (key == prefix or key.startswith(prefix + "__")) and same_path(_owner(root / name), _posix(source)):
            return root / name
    clash = stem + "__" + _parent_name(source)
    for candidate in (stem, clash):
        if candidate.casefold() not in taken:
            return root / candidate
    return root / unique_name(clash, taken)


def asset_folder(blend_dir, asset):
    """``<blend>/geo/<asset>`` for an allocated asset folder name."""
    return Path(blend_dir) / GEO_FOLDER / asset


def tex_folder(blend_dir):
    """``<blend>/tex``: flat folder of extracted packed and collected images."""
    return Path(blend_dir) / TEX_FOLDER


def json_path(blend_dir):
    """``<blend>/cycles_import.json``."""
    return Path(blend_dir) / JSON_NAME


def geometry_file(asset, frame=None):
    """``<asset>.bgeo.sc``, or ``<asset>.0001.bgeo.sc`` for one frame of a sequence."""
    return asset + GEOMETRY_SUFFIX if frame is None else f"{asset}.{int(frame):04d}{GEOMETRY_SUFFIX}"


def geometry_pattern(asset, sequence=False):
    """File SOP pattern: ``<asset>.bgeo.sc`` or ``<asset>.$F4.bgeo.sc``."""
    return asset + (".$F4" if sequence else "") + GEOMETRY_SUFFIX


def hierarchy_file(name):
    """``hierarchy/<object>.bgeo.sc`` (relative to the asset folder)."""
    return f"{HIERARCHY_FOLDER}/{name}{GEOMETRY_SUFFIX}"


def shape_file(name):
    """``hierarchy/shapes/<shape>.bgeo.sc`` (relative to the asset folder)."""
    return f"{SHAPES_FOLDER}/{name}{GEOMETRY_SUFFIX}"


def check_path_lengths(paths, limit=MAX_PATH):
    """Raise PathTooLongError naming the longest of ``paths`` when it exceeds ``limit``
    characters; return the longest length (0 for no paths)."""
    longest = max((str(path) for path in paths), key=len, default="")
    if len(longest) > limit:
        raise PathTooLongError(longest, limit)
    return len(longest)


def relative_path(path):
    """A stored relative path in posix form; raise UnsafePathError if it could leave its folder."""
    text = _posix(path) if isinstance(path, (str, os.PathLike)) else None
    parts = text.split("/") if text else []
    if not parts or ":" in text or any(part in ("", ".", "..") for part in parts):
        raise UnsafePathError(f"Not a plain relative path inside the asset folder: {path!r}")
    return text


def _inside(folder, relative):
    folder = Path(folder)
    target = folder.joinpath(*relative_path(relative).split("/"))
    try:
        target.resolve().relative_to(folder.resolve())
    except ValueError:
        raise UnsafePathError(f"{relative!r} leaves {folder}") from None
    return target


def delete_stale(asset_dir, relative_paths):
    """Delete files of a previous import (from ``ImportStore.stale_files``) in ``asset_dir``.

    Every path is checked first; if any would leave the folder, UnsafePathError is raised
    and nothing is deleted. Missing files are ignored. Afterwards empty ``hierarchy/shapes``
    and ``hierarchy`` folders are removed, and no other folder."""
    targets = [(relative_path(rel), _inside(asset_dir, rel)) for rel in relative_paths]
    removed, failed = [], []
    for rel, target in targets:
        try:
            target.unlink()
            removed.append(rel)
        except FileNotFoundError:
            continue
        except OSError as error:
            failed.append((rel, str(error)))
    for folder in (SHAPES_FOLDER, HIERARCHY_FOLDER):
        with contextlib.suppress(OSError):
            os.rmdir(Path(asset_dir).joinpath(*folder.split("/")))
    return DeleteResult(removed, failed)


# ------------------------------------------------------------------ lock


@contextlib.contextmanager
def import_lock(blend_dir, *, timeout=LOCK_TIMEOUT, poll=LOCK_POLL, stale_after=LOCK_STALE_AFTER):
    """Hold ``<blend>/cycles_import.lock`` (created exclusively; the folder is created).

    A lock older than ``stale_after`` seconds is broken. A held lock is polled every
    ``poll`` seconds for up to ``timeout`` seconds, then ImportLockError is raised. The
    lock is removed on exit, unless another process has since replaced it."""
    path = Path(blend_dir) / LOCK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    token = f"{os.getpid()} {time.time()} {os.urandom(8).hex()}"
    deadline = time.monotonic() + timeout
    while True:
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            break
        except (FileExistsError, PermissionError):  # Windows: PermissionError while being deleted
            if _break_stale(path, stale_after):
                continue
            if time.monotonic() >= deadline:
                raise ImportLockError(
                    f"Another import is writing {Path(blend_dir)} ({path.name} held for more than "
                    f"{timeout:g} s). Try again when it has finished, or delete {path} if no import is running."
                ) from None
            time.sleep(poll)
    written = False
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(token)
        written = True
        yield path
    finally:
        with contextlib.suppress(OSError):
            if not written or path.read_text(encoding="utf-8") == token:
                path.unlink()


def _break_stale(path, stale_after):
    """Remove a lock older than stale_after; True when it is gone (retry at once)."""
    try:
        age = time.time() - os.stat(path).st_mtime
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if age <= stale_after:
        return False
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        return False
    return True


# ------------------------------------------------------------------ cycles_import.json


def _empty_document():
    return {
        "schema": SCHEMA,
        "source": {"path": None, "signature": None},
        "names": {"assets": {}, "objects": {}, "shapes": {}, "images": {}},
        "assets": {},
        "images": {},
    }


def _valid(data):
    if not isinstance(data, dict) or type(data.get("schema")) is not int or data["schema"] < 1:
        return False
    names = data.get("names")
    if not isinstance(names, dict) or not isinstance(data.get("assets"), dict):
        return False
    if not isinstance(data.get("images"), dict) or not isinstance(data.get("source"), dict):
        return False
    flat = [names.get("assets"), names.get("images")]
    nested = [names.get("objects"), names.get("shapes")]
    if not all(isinstance(m, dict) and all(isinstance(v, str) for v in m.values()) for m in flat):
        return False
    return all(
        isinstance(m, dict)
        and all(isinstance(v, dict) and all(isinstance(n, str) for n in v.values()) for v in m.values())
        for m in nested
    )


def json_signature(signature):
    """``file_signature.native_signature`` result in JSON form: a list, or None."""
    if signature is None:
        return None
    if isinstance(signature, str):
        return signature.split(":")
    if isinstance(signature, (list, tuple)):
        return [item if isinstance(item, (str, int, float)) else str(item) for item in signature]
    return [str(signature)]


class ImportStore:
    """One blend folder's ``cycles_import.json``.

    ``state`` after ``load``: ``"new"`` (no JSON), ``"ok"``, ``"recovered"`` (unreadable
    JSON renamed to ``.bad``; started empty) or ``"foreign"`` (newer schema; left as is).
    Only ``"ok"`` allows deleting files of a previous import (``can_delete``);
    ``"foreign"`` refuses ``save`` and every name allocation or record (ForeignStoreError;
    check ``writable`` first) and reads as empty. ``notes`` lists what load did, for the
    import report. Lock the folder (``open_store`` / ``import_lock``) around load..save.
    """

    def __init__(self, blend_dir, data, state, notes=()):
        self.folder = Path(blend_dir)
        self.data, self.state, self.notes = data, state, list(notes)

    @classmethod
    def load(cls, blend_dir):
        """Read ``<blend>/cycles_import.json`` (see the class for the resulting state)."""
        path = json_path(blend_dir)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return cls(blend_dir, _empty_document(), "new")
        except OSError as error:
            text, reason = None, str(error)
        else:
            reason = None
        data = None
        if text is not None:
            try:
                data = json.loads(text)
            except ValueError as error:
                reason = str(error)
        if isinstance(data, dict) and type(data.get("schema")) is int and data["schema"] > SCHEMA:
            note = f"{path} has schema {data['schema']} (newer than {SCHEMA}); it was left unchanged."
            return cls(blend_dir, data, "foreign", [note])
        if data is not None and _valid(data):
            return cls(blend_dir, data, "ok")
        bad = path.with_name(JSON_BAD_NAME)
        try:
            os.replace(path, bad)
            note = f"{path} was unreadable ({reason or 'unexpected content'}); renamed to {bad.name}."
        except OSError as error:
            note = f"{path} was unreadable and could not be renamed: {error}"
        return cls(blend_dir, _empty_document(), "recovered", [note])

    @property
    def can_delete(self):
        """True only when the previous JSON was read intact: deletion may rely on it."""
        return self.state == "ok"

    @property
    def writable(self):
        """False for a JSON of a newer schema: neither save it nor delete anything."""
        return self.state != "foreign"

    def _check_writable(self):
        if not self.writable:
            raise ForeignStoreError(
                f"{json_path(self.folder)} was written by a newer version of Cycles for Houdini; "
                "this import does not change it. Update the plug-in or import into another scene folder."
            )

    def save(self):
        """Write atomically (``cycles_import.json.tmp`` then one rename). Refuses when foreign."""
        self._check_writable()
        self.folder.mkdir(parents=True, exist_ok=True)
        path = json_path(self.folder)
        temporary = path.with_name(JSON_TMP_NAME)
        text = json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        try:
            temporary.write_text(text, encoding="utf-8")
            os.replace(temporary, path)
        finally:
            with contextlib.suppress(OSError):
                temporary.unlink()
        return path

    # --- source

    def set_source(self, path, signature):
        """Record the source .blend (forward slashes) and its native file signature."""
        self.data["source"] = {"path": _posix(path), "signature": json_signature(signature)}

    @property
    def source_path(self):
        return self.data.get("source", {}).get("path")

    def same_signature(self, signature):
        """True when ``signature`` (as native_signature returns it) equals the recorded one."""
        stored = self.data.get("source", {}).get("signature")
        return stored is not None and stored == json_signature(signature)

    # --- names

    def _names(self, kind, scope=None):
        self._check_writable()
        names = self.data["names"].setdefault(kind, {})
        return names if scope is None else names.setdefault(scope, {})

    def _allocate(self, mapping, original, wanted, on_disk=(), *, file=False):
        if original in mapping:
            return mapping[original]
        taken = [name for key, name in mapping.items() if key != original]
        claimed = {file_key(n) if file else n.casefold() for n in mapping.values()}
        taken += [n for n in on_disk if (file_key(n) if file else n.casefold()) not in claimed]
        mapping[original] = name = unique_name(wanted, taken, file=file)
        return name

    def asset_name(self, original, name=None):
        """Folder name under ``geo/`` for an asset (``original`` = stable identity, ``name`` =
        Blender name, default ``original``). Recorded; the same original always gets it back.
        Folders on disk not recorded here count as owned by someone else."""
        on_disk = _listing(self.folder / GEO_FOLDER, dirs=True)
        wanted = safe_name(original if name is None else name, "asset")
        return self._allocate(self._names("assets"), original, wanted, on_disk)

    def object_name(self, asset, original, name=None):
        """Name of a hierarchy object file (``hierarchy_file``) inside asset folder ``asset``."""
        wanted = safe_name(original if name is None else name, "object")
        return self._allocate(self._names("objects", asset), original, wanted)

    def shape_name(self, asset, original, name=None):
        """Name of a shared shape file (``shape_file``) inside asset folder ``asset``."""
        wanted = safe_name(original if name is None else name, "shape")
        return self._allocate(self._names("shapes", asset), original, wanted)

    def image_name(self, key, file_name):
        """File name in ``tex/`` for one logical image (``key`` = stable identity). Pass the
        pattern (``wood.<UDIM>.exr``) or one tile/frame (``wood.1001.exr``) once per image and
        derive each tile with ``image_tile_name``. Files on disk not recorded here count as taken."""
        on_disk = _listing(self.folder / TEX_FOLDER, dirs=False)
        wanted = safe_image_name(file_name)
        return self._allocate(self._names("images"), key, wanted, on_disk, file=True)

    # --- assets

    def _asset(self, original, create=False):
        if not self.writable:  # unknown structure: no records to read, none to write
            if create:
                self._check_writable()
            return None
        folder = self.data["names"]["assets"].get(original)
        if folder is None:
            return None
        record = self.data["assets"].get(folder)
        if record is None and create:
            record = self.data["assets"][folder] = {"original": original}
        return record

    def asset_record(self, original):
        """The stored record of an asset (options, frames, files, scenes), or None."""
        return self._asset(original)

    def asset_files(self, original):
        """``{relative posix path: digest}`` of the asset's previous import ({} if none)."""
        record = self._asset(original) or {}
        files = {}
        for entry in record.get("files") or []:
            if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                files[entry["path"]] = entry.get("digest")
        return files

    def stale_files(self, original, new_files):
        """Relative paths the previous import of ``original`` wrote and ``new_files`` (paths or a
        path -> digest mapping) does not; only from this JSON, and [] unless ``can_delete``."""
        if not self.can_delete:
            return []
        keep = {relative_path(path).casefold() for path in new_files}
        return sorted(path for path in self.asset_files(original) if path.casefold() not in keep)

    def record_asset(self, original, *, options, frames, files, scene=None, exists=os.path.exists):
        """Store an import of ``original`` (allocate its name first with ``asset_name``):
        options, frames, ``files`` (relative path -> content digest) and, with ``scene``,
        the .hip path added to ``scenes`` (scenes gone from disk dropped)."""
        if original not in self.data["names"]["assets"]:
            raise KeyError(f"No folder name allocated for asset {original!r}; call asset_name first")
        record = self._asset(original, create=True)
        record["options"] = dict(options or {})
        record["frames"] = list(frames or [])
        record["files"] = [
            {"path": relative_path(path), "digest": digest} for path, digest in sorted(dict(files).items())
        ]
        if scene is not None:
            self.add_scene(original, scene, exists=exists)
        return record

    # --- scenes

    def add_scene(self, original, hip, *, exists=os.path.exists):
        """Add ``hip`` to the asset's ``scenes`` and drop listed scenes that no longer exist."""
        record = self._asset(original, create=True)
        if record is None:
            raise KeyError(f"No folder name allocated for asset {original!r}; call asset_name first")
        scenes = [s for s in record.get("scenes") or [] if isinstance(s, str) and not same_path(s, _posix(hip))]
        record["scenes"] = [s for s in scenes if exists(s)] + [_posix(hip)]
        return list(record["scenes"])

    def other_scenes(self, original, hip, *, exists=os.path.exists):
        """Existing .hip files besides ``hip`` that imported the asset (for the shared-scene warning)."""
        record = self._asset(original) or {}
        return [
            s for s in record.get("scenes") or [] if isinstance(s, str) and not same_path(s, _posix(hip)) and exists(s)
        ]

    # --- images

    def record_image(self, key, *, original, packed, file=None, sha256=None, library_root=None, relative_path=None):
        """Store an image: original path, ``packed``, the copy in ``tex/`` (name defaults to
        the allocated ``image_name``) with its sha256, and optional library location."""
        self._check_writable()
        record = {"original": _posix(original) if original else original, "packed": bool(packed)}
        file = file if file is not None else self.data["names"]["images"].get(key)
        if file is not None:
            record["file"] = file
        if sha256 is not None:
            record["sha256"] = sha256
        if library_root is not None:
            record["library_root"] = _posix(library_root)
            record["relative_path"] = _posix(relative_path) if relative_path is not None else None
        self.data["images"][key] = record
        return record

    def image_record(self, key):
        """The stored record of an image, or None."""
        return self.data["images"].get(key) if self.writable else None


@contextlib.contextmanager
def open_store(blend_dir, **lock_options):
    """Lock the blend folder and yield its loaded ImportStore (call ``save`` inside)."""
    with import_lock(blend_dir, **lock_options):
        yield ImportStore.load(blend_dir)
