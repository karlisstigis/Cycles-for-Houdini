# SPDX-License-Identifier: Apache-2.0
"""Relink Blender Assets (blend-import.md: Collect and relink).

Missing files are matched by file name (case-insensitive) under a folder the user
picks. UDIM sets and sequences match as patterns: a folder holding at least one
tile/frame is a candidate, and the new path keeps the token. One candidate is a
proposed rewrite; several are listed for the user to choose, never guessed. When the
import record (``cycles_import.json`` or the link data) gives a ``relative_path`` under
a library root, ``<folder>/<relative_path>`` is tried first, without walking the folder.

Covers texture parameters of materials, worlds and Environment Lights, File SOP paths
under ``assets_blender`` and the .blend path of Blender asset nodes. ``plan_relink`` is
pure; ``relink_blender_assets`` is the menu action.
"""

import json
import os
from dataclasses import dataclass, field

from ..blender import blend_assets_layout as layout
from . import texture_files as files

MAX_WALK = 200_000  # files indexed under the chosen folder before the walk stops
TITLE = "Relink Blender Assets"


@dataclass
class FolderIndex:
    by_name: dict = field(default_factory=dict)  # casefolded file name -> [paths]
    count: int = 0
    capped: bool = False


def index_folder(folder, limit=MAX_WALK):
    """Every file under ``folder`` by case-folded name; stops after ``limit`` files (``capped``)."""
    index = FolderIndex()
    for root, dirs, names in os.walk(folder):
        dirs.sort()
        for name in sorted(names):
            if index.count >= limit:
                index.capped = True
                return index
            index.by_name.setdefault(name.casefold(), []).append(files.posix(os.path.join(root, name)))
            index.count += 1
    return index


@dataclass
class RelinkPlan:
    found: list = field(default_factory=list)  # [(entry, new path)]
    ambiguous: list = field(default_factory=list)  # [(entry, [candidate paths])]
    not_found: list = field(default_factory=list)  # [entry]
    capped: bool = False
    searched: int = 0

    def text(self):
        lines = [f"Found {len(self.found)}, several candidates {len(self.ambiguous)}, not found {len(self.not_found)}."]
        if self.capped:
            lines.append(f"The search stopped after {self.searched} files; pick a smaller folder to search the rest.")
        return "\n".join(lines)

    def details(self):
        lines = []
        for title, rows in (
            ("Found", [f"{e.node}/{e.parm}: {e.path}\n    -> {new}" for e, new in self.found]),
            (
                "Several candidates",
                [f"{e.node}/{e.parm}: {e.path}\n    " + "\n    ".join(c) for e, c in self.ambiguous],
            ),
            ("Not found", [f"{e.node}/{e.parm}: {e.path}" for e in self.not_found]),
        ):
            if rows:
                lines += [title + ":"] + rows + [""]
        return "\n".join(lines).rstrip()


def is_missing(entry):
    """True for an entry whose file (or every tile/frame of whose pattern) is gone."""
    return files.supported(entry.path) and not files.existing_files(entry.path)


def _candidates(entry, index):
    name = os.path.basename(entry.path)
    regex = files.name_regex(name)
    if regex is None:
        return list(index.by_name.get(name.casefold(), []))
    folders = {}
    prefix = name[: files.TOKEN.search(name).start()].casefold()
    for key, paths in index.by_name.items():
        if key.startswith(prefix) and regex.match(key):
            for path in paths:
                folder = os.path.dirname(path)
                folders.setdefault(folder.casefold(), folder)
    return [files.posix(os.path.join(folder, name)) for _, folder in sorted(folders.items())]


def _unique(paths):
    result = []
    for path in paths:
        if not any(layout.same_path(path, seen) for seen in result):
            result.append(path)
    return result


def plan_relink(entries, folder, *, hints=None, limit=MAX_WALK, index=None):
    """Match missing ``entries`` (``scene_paths.PathEntry``) under ``folder``.

    ``hints``: ``{(node, parm): [relative path, ...]}`` tried as ``folder/relative`` first.
    The folder is walked once, only when some entry needs it."""
    plan = RelinkPlan()
    hints = hints or {}
    for entry in entries:
        hinted = [files.posix(os.path.join(folder, rel)) for rel in hints.get((entry.node, entry.parm), ())]
        hinted = [path for path in hinted if files.existing_files(path)]
        if hinted:
            plan.found.append((entry, hinted[0]))
            continue
        if index is None:
            index = index_folder(folder, limit)
            plan.capped, plan.searched = index.capped, index.count
        candidates = _unique(_candidates(entry, index))
        if len(candidates) == 1:
            plan.found.append((entry, candidates[0]))
        elif candidates:
            plan.ambiguous.append((entry, candidates))
        else:
            plan.not_found.append(entry)
    return plan


def rewrites(plan, hip):
    """``[(node, parm, text)]`` for the found entries: ``$HIP/...`` inside the scene folder,
    otherwise the plain absolute path."""
    return [(entry.node, entry.parm, files.parameter_text(path, hip)) for entry, path in plan.found]


# ------------------------------------------------------------------ hou layer


def _image_records(blend_dir):
    """Image records of a blend folder's JSON, read without locking or repairing it."""
    try:
        data = json.loads(layout.json_path(blend_dir).read_text(encoding="utf-8"))
        images = data.get("images")
    except (OSError, ValueError, AttributeError):
        return []
    return [record for record in (images or {}).values() if isinstance(record, dict)]


def _link_info(node_path):
    import hou

    from ..blender.blend_library_materials import link_info

    try:
        return link_info(hou.node(node_path))
    except Exception:
        return None


def hints_for(entries, hip):
    """``{(node, parm): [relative path]}`` from the JSON records (``library_root`` +
    ``relative_path``) and from link data (``library_root``) of Blender-imported owners."""
    from .cache import cache_root

    hints = {}
    roots = [layout.asset_root(hip), layout.asset_root(cache_root())]
    records = {}
    for entry in entries:
        owner = entry.owner
        if owner is None or not owner.blender or not owner.source:
            continue
        found = []
        if owner.source not in records:
            records[owner.source] = [
                r for root in roots for r in _image_records(layout.blend_folder(root, owner.source))
            ]
        for record in records[owner.source]:
            if layout.same_path(record.get("original"), files.posix(entry.path)) and record.get("relative_path"):
                found.append(record["relative_path"])
        info = _link_info(owner.path) or {}
        library_root = info.get("library_root")
        if library_root and files.is_under(entry.path, library_root):
            found.append(files.relative(entry.path, library_root))
        if found:
            hints[(entry.node, entry.parm)] = list(dict.fromkeys(found))
    return hints


def prefill_folder(entries, hip):
    """Start folder for the chooser: a registered library of the same name as a moved
    library root of a missing linked material, else ``$HIP``."""
    from .libraries import load_libraries

    try:
        libraries = [library for library in load_libraries() if os.path.isdir(library.path)]
    except Exception:
        libraries = []
    for entry in entries:
        if entry.owner is None or not entry.owner.blender:
            continue
        info = _link_info(entry.owner.path) or {}
        root, name = info.get("library_root"), info.get("library")
        if root and not os.path.isdir(root):
            for library in libraries:
                if library.name == name or os.path.basename(library.path.rstrip("/")) == os.path.basename(
                    root.rstrip("/")
                ):
                    return library.path
    return hip


def missing_entries():
    """Missing files of the scene: textures, File SOP paths under assets_blender, .blend paths."""
    from . import scene_paths

    geometry = [
        entry
        for entry in scene_paths.geometry_entries()
        if "/" + layout.ROOT_FOLDER + "/" in "/" + files.posix(entry.path).casefold()
    ]
    entries = scene_paths.all_texture_entries() + geometry + scene_paths.blend_entries()
    return [entry for entry in entries if is_missing(entry)]


def _choose(hou, plan):
    """Ask the user to pick one candidate per ambiguous entry; unpicked ones stay unresolved."""
    unresolved = []
    for entry, candidates in plan.ambiguous:
        picked = hou.ui.selectFromList(
            candidates,
            exclusive=True,
            message=f"Several files match {os.path.basename(entry.path)} for {entry.node}/{entry.parm}. Pick one, or Cancel to leave it.",
            title=TITLE,
            column_header="Candidates",
        )
        if picked:
            plan.found.append((entry, candidates[picked[0]]))
        else:
            unresolved.append((entry, candidates))
    plan.ambiguous = unresolved


def relink_blender_assets(kwargs=None, folder=None):
    """Menu action: pick a folder, match the missing files, confirm, rewrite in one undo group."""
    import hou

    from . import scene_paths

    hip = scene_paths.hip_folder()
    entries = missing_entries()
    if not entries:
        scene_paths.show_report(TITLE, "No missing textures, geometry or .blend files were found.")
        return None
    ui = hou.isUIAvailable()
    if folder is None:
        if not ui:
            raise ValueError("relink_blender_assets needs a folder without a UI")
        folder = hou.ui.selectFile(
            start_directory=prefill_folder(entries, hip),
            title=f"{TITLE}: folder to search for {len(entries)} missing file(s)",
            file_type=hou.fileType.Directory,
            chooser_mode=hou.fileChooserMode.Read,
        )
        if not folder:
            return None
    folder = hou.text.expandString(folder).rstrip("/\\")
    if not os.path.isdir(folder):
        scene_paths.show_report(TITLE, "Not a folder: " + folder, error=True)
        return None
    plan = plan_relink(entries, folder, hints=hints_for(entries, hip))
    if ui and plan.ambiguous:
        _choose(hou, plan)
    if not plan.found:
        scene_paths.show_report(TITLE, plan.text(), plan.details(), error=True)
        return plan
    if ui:
        choice = hou.ui.displayMessage(
            plan.text() + f"\n\nRelink {len(plan.found)} path(s)?",
            buttons=("Relink", "Cancel"),
            default_choice=0,
            close_choice=1,
            title=TITLE,
            details=plan.details(),
        )
        if choice != 0:
            return plan
    failed = scene_paths.apply_rewrites(rewrites(plan, hip), "Relink Blender assets")
    if failed:
        details = "\n".join(f"{node}/{parm}: {error}" for node, parm, error in failed)
        scene_paths.show_report(TITLE, f"{len(failed)} parameter(s) could not be changed.", details, error=True)
    return plan
