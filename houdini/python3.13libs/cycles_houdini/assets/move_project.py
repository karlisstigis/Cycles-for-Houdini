# SPDX-License-Identifier: Apache-2.0
"""Move Imported Assets into Project (blend-import.md: Collect and relink).

Imports into an unsaved scene write ``<user cache>/assets_blender/<blend>/`` (blend-import.md: Where imports write).
Once the scene is saved, this moves every blend folder the scene uses into
``$HIP/assets_blender/`` and rewrites node and texture paths in one undo group:

- no blend folder for that .blend in the project yet: the folder is moved as a whole
  (named by ``blend_assets_layout.blend_folder``, so a clash gets ``__<parent>``);
- one exists: the two are merged. Same content: the cache copy is dropped. Different
  content: both are kept, the cache one renamed by the naming rule (an asset folder
  ``tree_2``, an image ``wood_2.<tile>.exr``). The JSON records are merged through the
  store API.

Files are never moved automatically. ``move_blend_folder`` and ``rewrite_path`` are pure.
"""

import contextlib
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ..blender import blend_assets_layout as layout
from . import texture_files as files

TITLE = "Move Imported Assets into Project"
SAVE_FIRST = "Save the scene first: the assets move into the folder of the .hip file."
_BOOKKEEPING = {layout.JSON_NAME.casefold(), layout.LOCK_NAME.casefold(), layout.JSON_TMP_NAME.casefold()}


@dataclass
class MoveResult:
    source: Path
    target: Path
    merged: bool = False
    renames: list = field(default_factory=list)  # [(old relative path, new relative path)], files or folders
    dropped: list = field(default_factory=list)  # cache files equal to a project file
    kept_both: list = field(default_factory=list)  # (old relative, new relative): different content
    notes: list = field(default_factory=list)


def _files_under(folder):
    """Relative posix paths of every file under ``folder``."""
    found = []
    for root, dirs, names in os.walk(folder):
        dirs.sort()
        for name in sorted(names):
            found.append(files.posix(os.path.relpath(os.path.join(root, name), folder)))
    return found


def _move(source, target):
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(target):
        raise FileExistsError(f"Not overwriting an existing file: {target}")
    shutil.move(str(source), str(target))


def _remove_empty(folder):
    for root, _dirs, _names in sorted(os.walk(folder), key=lambda item: -len(item[0])):
        with contextlib.suppress(OSError):
            os.rmdir(root)


def _folder_state(source, target):
    """ "absent", "same" (every source file equal or absent, at least one equal) or "different"."""
    if not Path(target).exists():
        return "absent"
    states = set()
    for rel in _files_under(source):
        other = Path(target) / rel
        states.add(
            "absent" if not other.exists() else "same" if files.same_content(Path(source) / rel, other) else "different"
        )
    if "different" in states:
        return "different"
    return "same" if "same" in states else "absent"


def _merge_folder(source, target, result, prefix):
    """Move every file of ``source`` into ``target`` (absent ones) or drop it (equal ones)."""
    for rel in _files_under(source):
        src, dst = Path(source) / rel, Path(target) / rel
        if dst.exists():
            os.unlink(src)
            result.dropped.append(prefix + rel)
        else:
            _move(src, dst)


def _tile_groups(folder):
    """Files of a flat folder grouped into logical images (tiles/frames of one pattern together)."""
    groups = {}
    for name in sorted(os.listdir(folder)) if Path(folder).is_dir() else []:
        if (Path(folder) / name).is_file():
            groups.setdefault(layout.file_key(name), []).append(name)
    return groups


def _renamed(name, new_representative, representative):
    """``name`` (a tile of the group of ``representative``) under the group's new stem."""
    old_stem, _ = layout.file_parts(representative)
    new_stem, _ = layout.file_parts(new_representative)
    return new_stem + name[len(old_stem) :]


def _merge_tex(source, target, result, tstore, sstore):
    folder_rel = layout.TEX_FOLDER + "/"
    records = {}
    if sstore is not None:
        for key, record in (sstore.data.get("images") or {}).items():
            if isinstance(record, dict) and record.get("file"):
                records.setdefault(layout.file_key(record["file"]), []).append((key, record))
    listing = sorted(os.listdir(target)) if Path(target).is_dir() else []
    for group_key, names in _tile_groups(source).items():
        states = set()
        for name in names:
            other = Path(target) / name
            states.add(
                "absent"
                if not other.exists()
                else "same"
                if files.same_content(Path(source) / name, other)
                else "different"
            )
        keys = records.get(group_key, [])
        owned_elsewhere = tstore is not None and any(
            layout.file_key(name) == group_key and key not in {k for k, _ in keys}
            for key, name in tstore.data["names"]["images"].items()
        )
        new_representative = names[0]
        if "different" in states or (states == {"absent"} and owned_elsewhere):
            taken = listing + list(tstore.data["names"]["images"].values() if tstore is not None else [])
            new_representative = layout.unique_name(names[0], taken, file=True)
            listing.append(new_representative)
        for name in names:
            new_name = _renamed(name, new_representative, names[0])
            src, dst = Path(source) / name, Path(target) / new_name
            if dst.exists():
                os.unlink(src)
                result.dropped.append(folder_rel + name)
            else:
                _move(src, dst)
            if new_name != name:
                result.renames.append((folder_rel + name, folder_rel + new_name))
                result.kept_both.append((folder_rel + name, folder_rel + new_name))
        if tstore is not None:
            for key, record in keys:
                if tstore.image_record(key) is not None:
                    continue
                file = (
                    _renamed(record["file"], new_representative, names[0])
                    if new_representative != names[0]
                    else record["file"]
                )
                tstore.record_image(
                    key,
                    original=record.get("original"),
                    packed=record.get("packed", False),
                    file=file,
                    sha256=record.get("sha256"),
                    library_root=record.get("library_root"),
                    relative_path=record.get("relative_path"),
                )


def _merge_geo(source, target, result, tstore, sstore, hip):
    geo = layout.GEO_FOLDER + "/"
    originals = {}
    if sstore is not None:
        originals = {folder.casefold(): original for original, folder in sstore.data["names"]["assets"].items()}
    taken = sorted(os.listdir(target)) if Path(target).is_dir() else []
    for name in sorted(os.listdir(source)) if Path(source).is_dir() else []:
        src = Path(source) / name
        if not src.is_dir():
            continue
        original = originals.get(name.casefold())
        known = tstore is not None and original is not None and original in tstore.data["names"]["assets"]
        wanted = tstore.asset_name(original, name) if tstore is not None and original is not None else name
        state = _folder_state(src, Path(target) / wanted)
        conflict = state == "different" or (state == "absent" and original is None and Path(target, wanted).exists())
        if conflict:  # keep both: this copy goes to a free name, unrecorded; the project's record stays
            wanted = layout.unique_name(wanted, taken)
            result.kept_both.append((geo + name, geo + wanted))
        taken.append(wanted)
        if Path(target, wanted).exists():
            _merge_folder(src, Path(target) / wanted, result, geo + name + "/")
            _remove_empty(src)
        else:
            _move(src, Path(target) / wanted)
        if wanted != name:
            result.renames.append((geo + name, geo + wanted))
        if tstore is None or original is None or conflict:
            continue
        if not known:
            record = sstore.asset_record(original) or {}
            tstore.record_asset(
                original, options=record.get("options"), frames=record.get("frames"), files=sstore.asset_files(original)
            )
            for scene in record.get("scenes") or []:
                if isinstance(scene, str) and os.path.exists(scene):
                    tstore.add_scene(original, scene)
        if hip:
            tstore.add_scene(original, hip)


def _merge_rest(source, target, result):
    for rel in _files_under(source):
        if rel.casefold() in _BOOKKEEPING or rel.split("/")[0] in (layout.GEO_FOLDER, layout.TEX_FOLDER):
            continue
        src, dst = Path(source) / rel, Path(target) / rel
        if not dst.exists():
            _move(src, dst)
        elif files.same_content(src, dst):
            os.unlink(src)
            result.dropped.append(rel)
        else:
            new = Path(rel).parent / layout.unique_name(dst.name, os.listdir(dst.parent), file=True)
            _move(src, Path(target) / new)
            result.renames.append((rel, files.posix(new)))
            result.kept_both.append((rel, files.posix(new)))


def _source_of(blend_dir):
    store = layout.ImportStore.load(blend_dir) if layout.json_path(blend_dir).exists() else None
    return store.source_path if store is not None and store.writable else None


def move_blend_folder(source, project_root, *, hip=None):
    """Move or merge one cache blend folder into ``project_root`` (``$HIP/assets_blender``).
    ``hip`` (the .hip path) is added to the merged assets' ``scenes``. Returns MoveResult."""
    source, project_root = Path(source), Path(project_root)
    blend = _source_of(source)
    if blend:
        target = layout.blend_folder(project_root, blend)
    else:
        target = project_root / source.name
        if target.exists():
            taken = os.listdir(project_root)
            target = project_root / layout.unique_name(source.name, taken)
    result = MoveResult(source, target)
    if not target.exists():
        project_root.mkdir(parents=True, exist_ok=True)
        _move(source, target)
        return result
    result.merged = True
    with layout.open_store(target) as tstore, contextlib.ExitStack() as stack:
        sstore = stack.enter_context(layout.open_store(source)) if layout.json_path(source).exists() else None
        if not tstore.writable:
            raise layout.ForeignStoreError(tstore.notes[0] if tstore.notes else f"{target} is newer")
        sstore = sstore if sstore is not None and sstore.writable else None
        if tstore.source_path is None and blend:
            tstore.set_source(blend, (sstore.data.get("source") or {}).get("signature") if sstore else None)
        _merge_geo(source / layout.GEO_FOLDER, target / layout.GEO_FOLDER, result, tstore, sstore, hip)
        _merge_tex(source / layout.TEX_FOLDER, target / layout.TEX_FOLDER, result, tstore, sstore)
        _merge_rest(source, target, result)
        tstore.save()
    for name in (layout.JSON_NAME, layout.JSON_TMP_NAME):
        with contextlib.suppress(OSError):
            os.unlink(source / name)
    _remove_empty(source)
    if source.exists():
        result.notes.append(f"Some files stayed in {files.posix(source)}.")
    return result


def rewrite_path(path, result):
    """New expanded path for a parameter path inside ``result.source`` (tokens kept), or None
    when the path is outside it."""
    if not files.is_under(path, result.source):
        return None
    rel = files.relative(path, result.source)
    folder, name = os.path.split(rel)
    pattern = files.with_placeholder(name)
    for old, new in result.renames:
        old_folder, old_name = os.path.split(old)
        if old.casefold() == rel.casefold():
            rel = new
            break
        if files.is_under("/" + rel, "/" + old):  # a renamed asset folder
            rel = new + rel[len(old) :]
            break
        if files.token(name) and old_folder.casefold() == folder.casefold():
            if layout.file_key(old_name) == layout.file_key(pattern):
                rel = files.posix(
                    os.path.join(
                        folder,
                        files.restore_token(_renamed(pattern, os.path.basename(new), old_name), files.token(name)),
                    )
                )
                break
    return files.posix(Path(result.target) / rel)


# ------------------------------------------------------------------ hou layer


def cache_asset_root():
    """``<user cache>/assets_blender`` (blend-import.md: Where imports write), the folder imports into an unsaved
    scene write."""
    from .cache import cache_root

    return layout.asset_root(cache_root())


def cache_entries():
    """Node and texture parameters of the scene whose path lies under the cache asset root."""
    from . import scene_paths

    root = files.posix(cache_asset_root())
    entries = scene_paths.all_texture_entries() + scene_paths.geometry_entries() + scene_paths.blend_entries()
    return [entry for entry in entries if files.is_under(entry.path, root)]


def blend_folders_in_use(entries, root):
    """``{blend folder name: [entries]}`` for entries under ``root``."""
    folders = {}
    for entry in entries:
        name = files.relative(entry.path, root).split("/", 1)[0]
        folders.setdefault(name, []).append(entry)
    return folders


def move_imported_assets_into_project(show=True):
    """Menu action. Refuses on an unsaved scene. Returns ``(results, failures)``."""
    import hou

    from . import scene_paths
    from .collect import scene_is_saved

    if not scene_is_saved():
        if show:
            scene_paths.show_report(TITLE, SAVE_FIRST, error=True)
        return [], [("", SAVE_FIRST)]
    hip = scene_paths.hip_folder()
    root = cache_asset_root()
    entries = cache_entries()
    if not entries:
        if show:
            scene_paths.show_report(TITLE, "Nothing in this scene reads the user cache folder.")
        return [], []
    results, failures, rewrites = [], [], []
    for name, group in sorted(blend_folders_in_use(entries, root).items()):
        try:
            result = move_blend_folder(root / name, layout.asset_root(hip), hip=files.posix(hou.hipFile.path()))
        except (OSError, layout.ForeignStoreError, layout.ImportLockError) as error:
            failures.append((files.posix(root / name), str(error)))
            continue
        results.append(result)
        for entry in group:
            new = rewrite_path(entry.path, result)
            if new is not None:
                rewrites.append((entry.node, entry.parm, files.hip_text(new, hip)))
    failures += [
        (f"{n}/{p}", e) for n, p, e in scene_paths.apply_rewrites(rewrites, "Move imported assets into project")
    ]
    if show:
        lines = [
            f"Moved {len(results)} blend folder(s) into {files.posix(layout.asset_root(hip))}; {len(rewrites)} path(s) rewritten."
        ]
        details = []
        for result in results:
            verb = "merged into" if result.merged else "moved to"
            details.append(f"{files.posix(result.source)} {verb} {files.posix(result.target)}")
            details += [f"    kept both: {old} -> {new}" for old, new in result.kept_both]
            details += ["    " + note for note in result.notes]
        details += [f"Failed: {where}: {error}" for where, error in failures]
        scene_paths.show_report(TITLE, "\n".join(lines), "\n".join(details), bool(failures))
    return results, failures


_OFFERED = set()


def maybe_offer_move():
    """After the first save of a scene that reads the user cache: offer the move once per
    .hip path and session. Never moves without the user's click. Returns True when moved."""
    import hou

    from .collect import scene_is_saved

    try:
        if not hou.isUIAvailable() or not scene_is_saved():
            return False
        hip_path = files.posix(hou.hipFile.path()).casefold()
        if hip_path in _OFFERED:
            return False
        entries = cache_entries()
    except Exception:
        return False
    if not entries:
        return False
    _OFFERED.add(hip_path)
    choice = hou.ui.displayMessage(
        f"This scene reads {len(entries)} imported file path(s) from the user cache folder, where imports "
        "into an unsaved scene go. Move them into the project's assets_blender folder now?\n"
        "You can do it later with Cycles > Move Imported Assets into Project.",
        buttons=("Move into Project", "Not Now"),
        default_choice=0,
        close_choice=1,
        title=TITLE,
    )
    if choice != 0:
        return False
    move_imported_assets_into_project()
    return True
