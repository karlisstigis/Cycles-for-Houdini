# SPDX-License-Identifier: Apache-2.0
"""Collect Textures and Reload after a collect (blend-import.md: Collect and relink).

Collect copies the files that Cycles texture parameters point at into the project and
rewrites the parameters to ``$HIP/...`` text in one undo group:

- a material, world or light imported from a .blend -> ``$HIP/assets_blender/<blend>/tex/``
  (names and records through that folder's ``cycles_import.json``);
- the same with its import record lost -> ``$HIP/assets_blender/_unknown_source/tex/``;
- anything else -> ``$HIP/tex/<material>/``.

One copy per texture: a file of the same name already there with the same content
(size, then sha256) is reused, otherwise the copy gets ``_2``, ``_3``... A copy recorded
for the same original in ``cycles_import.json`` is refreshed when the original changed.
All tiles of a UDIM set and all frames of a sequence are copied and the parameter keeps
its token. Files already under ``$HIP`` are skipped. The ``.blend`` is never opened.

``collect_entries`` and ``reapply_collected`` are pure (plain data and files);
``collect_textures`` and ``reapply_after_reload`` are the ``hou`` layer.
"""

import contextlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..blender import blend_assets_layout as layout
from . import texture_files as files

UNKNOWN_SOURCE = "_unknown_source"
PROJECT_TEXTURES = "tex"
SAVE_FIRST = "Save the scene first: Collect Textures copies files next to the .hip file."
LINKED_NOTE = (
    "{count} linked assets still read their .blend at render time. Switch them to Converted to make them portable."
)
RELOAD_WARNING = "material.library_texture_missing"


class CollectError(RuntimeError):
    """Collect cannot run (unsaved scene)."""


@dataclass(frozen=True)
class Destination:
    """Where one owner's textures go. ``blend_dir`` is set when the folder has a store."""

    folder: Path
    blend_dir: Path = None
    unknown_source: bool = False


@dataclass
class CollectReport:
    copied: list = field(default_factory=list)  # destination files written
    reused: list = field(default_factory=list)  # destination files that already held the content
    skipped: list = field(default_factory=list)  # parameter paths already inside the project
    missing: list = field(default_factory=list)  # source paths with no file on disk
    unsupported: list = field(default_factory=list)  # patterns the tool cannot expand
    failed: list = field(default_factory=list)  # (path or node/parm, error text)
    unknown_source: list = field(default_factory=list)  # owner paths whose import record is lost
    bytes: int = 0
    rewritten: int = 0
    linked_assets: int = 0
    in_cache: list = field(default_factory=list)  # node/parm paths still reading the user cache

    def text(self):
        """The report: one line per fact, empty categories left out."""
        lines = [
            f"Copied {len(self.copied)} file(s) ({_size(self.bytes)}), reused {len(self.reused)}, "
            f"skipped {len(self.skipped)} already in the project; {self.rewritten} parameter(s) rewritten."
        ]
        if self.missing:
            lines.append(f"{len(self.missing)} source file(s) not found:")
            lines += ["    " + path for path in sorted(set(self.missing))]
        if self.unsupported:
            lines.append(f"{len(self.unsupported)} path(s) with a pattern this tool cannot expand (left unchanged):")
            lines += ["    " + path for path in sorted(set(self.unsupported))]
        if self.unknown_source:
            lines.append(
                f"The import record of {len(self.unknown_source)} Blender asset(s) is lost; "
                f"their textures went to {layout.ROOT_FOLDER}/{UNKNOWN_SOURCE}/{layout.TEX_FOLDER}:"
            )
            lines += ["    " + path for path in sorted(set(self.unknown_source))]
        if self.failed:
            lines.append(f"{len(self.failed)} failure(s):")
            lines += [f"    {where}: {error}" for where, error in self.failed]
        if self.linked_assets:
            lines.append(LINKED_NOTE.format(count=self.linked_assets))
        if self.in_cache:
            lines.append(
                f"{len(self.in_cache)} path(s) still in the user cache (Cycles > Move Imported Assets into Project):"
            )
            lines += ["    " + path for path in sorted(set(self.in_cache))]
        return "\n".join(lines)


def _size(count):
    for unit in ("bytes", "KB", "MB", "GB"):
        if count < 1024 or unit == "GB":
            return f"{count:.0f} {unit}" if unit == "bytes" else f"{count:.1f} {unit}"
        count /= 1024.0
    return ""


@dataclass
class CollectResult:
    rewrites: list  # [(node path, parm name, "$HIP/..." text)]
    report: CollectReport


# ------------------------------------------------------------------ destination


def destination(owner, hip, blend_folders=None):
    """Destination of an owner's textures (``owner`` may be None). ``blend_folders`` caches
    ``source -> blend folder`` within one run."""
    root = layout.asset_root(hip)
    if owner is not None and owner.blender:
        if not owner.source:
            return Destination(layout.tex_folder(root / UNKNOWN_SOURCE), None, True)
        cache = {} if blend_folders is None else blend_folders
        key = files.image_key(owner.source).casefold()
        if key not in cache:
            cache[key] = layout.blend_folder(root, owner.source)
        return Destination(layout.tex_folder(cache[key]), cache[key])
    name = layout.safe_name(owner.name if owner is not None else "", "material")
    return Destination(Path(hip) / PROJECT_TEXTURES / name)


# ------------------------------------------------------------------ one image


def _listing(folder):
    try:
        return sorted(entry.name for entry in os.scandir(folder) if entry.is_file())
    except OSError:
        return []


def _family(name, listing):
    """Names in ``listing`` (tiles folded to their pattern) that are ``name`` or ``name`` with
    a clash suffix (``_2``...), in suffix order. ``name`` holds PLACEHOLDER for a pattern."""
    stem, tail = layout.file_parts(name)
    tail_regex = re.escape(tail).replace(re.escape(files.PLACEHOLDER), r"(\d+|<UDIM>)")
    regex = re.compile(re.escape(stem) + r"(?:_(\d+))?" + tail_regex + r"\Z", re.IGNORECASE)
    found = {}
    for entry in [name, *listing]:
        match = regex.match(entry)
        if match:
            index = int(match.group(1) or 1)
            found.setdefault(index, stem + (f"_{index}" if index > 1 else "") + tail)
    return [found[index] for index in sorted(found)]


def compare(folder, name, sources):
    """State of destination name ``name`` (PLACEHOLDER for a pattern) against the source
    files ``{tile: path}``: "same" (every tile present and equal), "partial" (at least one
    equal, the others absent), "absent" (none present) or "different"."""
    states = set()
    for tile, source in sources.items():
        target = Path(folder) / files.fill(name, tile)
        if not target.exists():
            states.add("absent")
        else:
            states.add("same" if files.same_content(source, target) else "different")
    if "different" in states:
        return "different"
    if states == {"same"}:
        return "same"
    return "partial" if "same" in states else "absent"


def _place(folder, name, sources, report, *, overwrite=False):
    """Copy the source tiles into ``folder/name``: absent tiles always; differing tiles only
    with ``overwrite`` (refreshing this original's own copy). Equal tiles count as reused."""
    for tile, source in sources.items():
        target = Path(folder) / files.fill(name, tile)
        if target.exists() and files.same_content(source, target):
            report.reused.append(files.posix(target))
            continue
        try:
            report.bytes += files.copy_file(source, target, overwrite=overwrite)
            report.copied.append(files.posix(target))
        except OSError as error:
            report.failed.append((files.posix(target), str(error)))
            return False
    return True


def collect_image(dest, path, sources, report, store=None):
    """Put one source image (``{tile: path}``) into ``dest``; return the destination file
    name with the parameter's token, or None on failure."""
    token = files.token(path)
    wanted = layout.safe_image_name(files.with_placeholder(os.path.basename(path)))
    key = files.image_key(path)
    record = store.image_record(key) if store is not None else None
    name, overwrite = None, False
    if record and record.get("file") and not record.get("packed"):
        name, overwrite = record["file"], True  # this original's own copy: refresh it
    else:
        listing = _listing(dest.folder)
        for candidate in _family(wanted, listing):
            if compare(dest.folder, candidate, sources) in ("same", "partial"):
                name = candidate
                break
        if name is None:
            if store is not None:
                name = store.image_name(key, wanted)
                if compare(dest.folder, name, sources) == "different":  # a stale allocation
                    name = layout.unique_name(wanted, listing + [name], file=True)
            else:
                name = layout.unique_name(wanted, listing, file=True)
    if not _place(dest.folder, name, sources, report, overwrite=overwrite):
        return None
    if store is not None:
        previous = record or {}
        store.record_image(
            key,
            original=files.posix(path),
            packed=False,
            file=name,
            sha256=files.content_digest(sources),
            library_root=previous.get("library_root"),
            relative_path=previous.get("relative_path"),
        )
    return files.restore_token(name, token)


# ------------------------------------------------------------------ collect


def _owner_key(owner):
    if owner is None:
        return ("plain", "")
    if owner.blender:
        return ("blend", files.image_key(owner.source).casefold() if owner.source else "")
    return ("plain", owner.name)


@contextlib.contextmanager
def _store_for(dest, owner):
    if dest.blend_dir is None:
        yield None
        return
    with layout.open_store(dest.blend_dir) as store:
        if not store.writable:
            raise layout.ForeignStoreError(store.notes[0] if store.notes else f"{dest.blend_dir} is newer")
        if store.source_path is None:
            store.set_source(owner.source, None)
        yield store
        store.save()


def collect_entries(entries, hip):
    """Plan and copy for ``entries`` (``scene_paths.PathEntry``); returns CollectResult with
    the parameter rewrites (``$HIP/...``, tokens kept) and the report. ``hip`` = expanded $HIP."""
    report = CollectReport()
    rewrites = []
    groups = {}
    for entry in entries:
        groups.setdefault(_owner_key(entry.owner), []).append(entry)
    blend_folders = {}
    for group in groups.values():
        owner = group[0].owner
        dest = destination(owner, hip, blend_folders)
        if dest.unknown_source:
            report.unknown_source += [entry.owner.path for entry in group]
        done = {}
        try:
            with _store_for(dest, owner) as store:
                for entry in group:
                    if files.is_under(entry.path, hip):
                        report.skipped.append(entry.path)
                        continue
                    if not files.supported(entry.path):
                        report.unsupported.append(entry.path)
                        continue
                    key = files.image_key(entry.path).casefold()
                    if key not in done:
                        sources = files.existing_files(entry.path)
                        if not sources:
                            report.missing.append(entry.path)
                            done[key] = None
                        else:
                            done[key] = collect_image(dest, entry.path, sources, report, store)
                    if done[key] is not None:
                        rewrites.append((entry.node, entry.parm, files.hip_text(dest.folder / done[key], hip)))
        except (OSError, layout.ForeignStoreError, layout.ImportLockError) as error:
            report.failed.append((files.posix(dest.folder), str(error)))
    report.rewritten = len(rewrites)
    return CollectResult(rewrites, report)


# ------------------------------------------------------------------ hou layer


def scene_is_saved():
    from .importers import scene_is_untitled

    return not scene_is_untitled()


def _cache_paths(entries):
    from .cache import cache_root

    cache = files.posix(cache_root())
    return [f"{entry.node}/{entry.parm}: {entry.path}" for entry in entries if files.is_under(entry.path, cache)]


def collect_textures(nodes=None, *, show=True):
    """Collect Textures for ``nodes`` (and everything inside them), or the whole scene when
    ``nodes`` is None. Raises CollectError on an unsaved scene. Returns the CollectReport."""
    from . import scene_paths

    if not scene_is_saved():
        if show:
            scene_paths.show_report("Collect Textures", SAVE_FIRST, error=True)
        raise CollectError(SAVE_FIRST)
    hip = scene_paths.hip_folder()
    entries = scene_paths.all_texture_entries() if nodes is None else scene_paths.texture_entries(nodes)
    result = collect_entries(entries, hip)
    report = result.report
    failed = scene_paths.apply_rewrites(result.rewrites, "Collect textures")
    report.failed += [(f"{node}/{parm}", error) for node, parm, error in failed]
    report.rewritten -= len(failed)
    rewritten = {(node, parm) for node, parm, _ in result.rewrites} - {(node, parm) for node, parm, _ in failed}
    remaining = [entry for entry in entries if (entry.node, entry.parm) not in rewritten]
    report.in_cache = _cache_paths(remaining + scene_paths.geometry_entries() + scene_paths.blend_entries())
    report.linked_assets = scene_paths.linked_asset_count()
    if show:
        summary = report.text().split("\n", 1)
        scene_paths.show_report(
            "Collect Textures", summary[0], summary[1] if len(summary) > 1 else "", bool(report.failed)
        )
    return report


# ------------------------------------------------------------------ reload after collect (5.2)


def after_reload_action(original_exists, original_sha256, recorded_sha256):
    """What Reload from Library does with a collected copy (blend-import.md: Collect and relink).

    "keep" (original unchanged), "recollect" (original changed: overwrite the copy) or
    "keep_missing" (original gone: keep the copy and warn)."""
    if not original_exists:
        return "keep_missing"
    if recorded_sha256 is not None and original_sha256 == recorded_sha256:
        return "keep"
    return "recollect"


@dataclass(frozen=True)
class ReloadItem:
    """A texture parameter after a reload: the library path it now has, and the collected
    path (expanded, tokens kept) and text it had before."""

    node: str
    parm: str
    original: str
    collected: str
    collected_text: str


def reapply_collected(items, *, record_sha=None):
    """Pure core of 5.2. For each ReloadItem decide keep / recollect / keep_missing, refresh
    copies that need it and return ``(rewrites, warnings, refreshed)``: rewrites restore the
    collected text; ``refreshed`` lists ``(key, original, sha256)`` for the store.
    ``record_sha(original)`` gives the recorded sha256 (None: compare with the copy itself)."""
    rewrites, warnings, refreshed = [], [], []
    for item in items:
        originals = files.existing_files(item.original)
        copies = files.existing_files(item.collected)
        recorded = record_sha(item.original) if record_sha is not None else None
        if recorded is None and copies:
            recorded = files.content_digest(copies)
        current = files.content_digest(originals) if originals else None
        action = after_reload_action(bool(originals), current, recorded)
        if action == "recollect":
            folder, name = os.path.split(item.collected)
            pattern = files.with_placeholder(name)
            try:
                for tile, source in originals.items():
                    files.copy_file(source, Path(folder) / files.fill(pattern, tile), overwrite=True)
            except OSError as error:
                warnings.append(
                    _warning(item, f"could not refresh the collected copy: {error}", "material.collect_failed")
                )
                rewrites.append((item.node, item.parm, item.collected_text))
                continue
            refreshed.append((files.image_key(item.original), files.posix(item.original), current))
        elif action == "keep_missing":
            warnings.append(_warning(item, "library texture not found; the collected copy is kept: " + item.original))
        rewrites.append((item.node, item.parm, item.collected_text))
    return rewrites, warnings, refreshed


def _warning(item, text, code=RELOAD_WARNING):
    return dict(code=code, severity="warning", node=item.node, path=item.original, message=f"{item.node}: {text}")


def blend_dir_of(path, hip):
    """The blend folder of a file in ``$HIP/assets_blender/<blend>/tex/``, if it has a JSON."""
    folder = Path(path).parent
    if folder.name.casefold() != layout.TEX_FOLDER or not files.is_under(path, layout.asset_root(hip)):
        return None
    return folder.parent if layout.json_path(folder.parent).is_file() else None


def texture_values(material):
    """Before a reload: ``{node path relative to the material + "/" + parm: unexpanded text}``."""
    from . import scene_paths

    prefix = material.path().rstrip("/")
    return {
        entry.node[len(prefix) :] + "/" + entry.parm: entry.raw for entry in scene_paths.texture_entries([material])
    }


def reapply_after_reload(material, previous):
    """After ``importers._rebuild`` replaced a linked material: put back the collected paths
    recorded in ``previous`` (from ``texture_values`` on the old node), as after_reload_action says.
    Locked parameters are unlocked for the write and locked again. Returns the warnings."""
    from . import scene_paths

    hip = scene_paths.hip_folder()
    prefix = material.path().rstrip("/")
    items = []
    for entry in scene_paths.texture_entries([material]):
        before = previous.get(entry.node[len(prefix) :] + "/" + entry.parm)
        if not before or before == entry.raw:
            continue
        collected = scene_paths.expand(before)
        if not files.is_under(collected, hip) or files.is_under(entry.path, hip):
            continue
        items.append(ReloadItem(entry.node, entry.parm, entry.path, collected, before))
    if not items:
        return []
    blend_dir = blend_dir_of(items[0].collected, hip)
    opened = layout.open_store(blend_dir) if blend_dir is not None else contextlib.nullcontext(None)
    with opened as store:
        store = store if store is not None and store.writable else None

        def record_sha(original):
            record = store.image_record(files.image_key(original)) if store is not None else None
            return (record or {}).get("sha256")

        rewrites, warnings, refreshed = reapply_collected(items, record_sha=record_sha)
        changed = False
        for key, original, sha256 in refreshed:
            record = store.image_record(key) if store is not None else None
            if record and record.get("file"):
                store.record_image(
                    key,
                    original=original,
                    packed=False,
                    file=record["file"],
                    sha256=sha256,
                    library_root=record.get("library_root"),
                    relative_path=record.get("relative_path"),
                )
                changed = True
        if changed:
            store.save()
    failed = scene_paths.apply_rewrites(rewrites, "Keep collected textures")
    warnings += [_warning(ReloadItem(n, p, "", "", ""), e, "material.collect_failed") for n, p, e in failed]
    return warnings
