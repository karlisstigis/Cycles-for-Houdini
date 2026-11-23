# SPDX-License-Identifier: Apache-2.0
"""File sets, content digests and safe copies for Collect, Relink and Move into Project.

Pure Python: no ``hou``. A parameter value names one file or a pattern of files: a UDIM
set (``<UDIM>``, ``%(UDIM)d``) or a frame sequence (``$F``, ``$F4``, ``${F4}``, ``<F>``).
A pattern covers every existing tile/frame. One varying token per path is supported,
in the file name only; anything else is "unsupported" and left alone by the tools.

Paths here are expanded text in which the pattern tokens are kept (see
``expand_keeping_tokens``). ``PLACEHOLDER`` (``<UDIM>``) stands for the varying part
while names are allocated, because ``blend_assets_layout`` keeps that token intact and
treats every tile/frame of one pattern as one name.
"""

import contextlib
import hashlib
import ntpath
import os
import re
import shutil
from pathlib import Path

PLACEHOLDER = "<UDIM>"
TOKEN = re.compile(r"<UDIM>|%\(UDIM\)d|\$\{F(\d?)\}|\$F(\d?)(?![A-Za-z_])|<F(\d?)>", re.IGNORECASE)
_KEEP = re.compile("(\\d+)")
_CHUNK = 1 << 20


def posix(path):
    """Path text with forward slashes (the Houdini convention, also on Windows)."""
    return str(path).replace("\\", "/")


def expand_keeping_tokens(raw, expand):
    """``expand(raw)`` (e.g. ``hou.text.expandString``) without expanding the pattern tokens:
    ``$HIP/tex/wood.$F4.exr`` becomes ``/shots/tex/wood.$F4.exr``, not frame 1."""
    kept = []

    def keep(match):
        kept.append(match.group(0))
        return f"{len(kept) - 1}"

    return _KEEP.sub(lambda m: kept[int(m.group(1))], expand(TOKEN.sub(keep, raw)))


def token(path):
    """The pattern token text of a path's file name (``<UDIM>``, ``$F4``...), or None."""
    found = TOKEN.search(os.path.basename(path))
    return found.group(0) if found else None


def supported(path):
    """False for a path whose tokens these tools cannot expand (several, or in a folder)."""
    found = list(TOKEN.finditer(posix(path)))
    return not found or (len(found) == 1 and TOKEN.search(os.path.basename(path)) is not None)


def _token_regex(match):
    if "UDIM" in match.group(0).upper():
        return r"(\d{4})"
    pad = next((group for group in match.groups() if group), "")
    return r"(-?\d{%d,})" % int(pad) if pad else r"(-?\d+)"


def name_regex(name):
    """Case-insensitive regex matching the files of a pattern file name (group 1 = the
    varying text), or None for a plain name or more than one token."""
    found = list(TOKEN.finditer(name))
    if len(found) != 1:
        return None
    match = found[0]
    head, tail = re.escape(name[: match.start()]), re.escape(name[match.end() :])
    return re.compile(head + _token_regex(match) + tail + r"\Z", re.IGNORECASE)


def with_placeholder(name):
    """A pattern file name with its token replaced by PLACEHOLDER (plain names unchanged)."""
    return TOKEN.sub(PLACEHOLDER, name, count=1)


def fill(name, value):
    """File name of one tile/frame: the token or PLACEHOLDER replaced by ``value``."""
    if not value:
        return name
    return TOKEN.sub(lambda _match: value, name, count=1)


def restore_token(name, token_text):
    """Turn PLACEHOLDER back into the parameter's own token (``$F4``, ``%(UDIM)d``...)."""
    return name.replace(PLACEHOLDER, token_text, 1) if token_text else name


def existing_files(path):
    """``{varying text: file path}`` of the files a parameter value names.

    A plain existing file gives ``{"": path}``; a pattern gives every existing
    tile/frame (keys sorted); nothing existing or an unsupported pattern gives ``{}``."""
    path = str(path)
    if not TOKEN.search(path):
        return {"": path} if os.path.isfile(path) else {}
    if not supported(path):
        return {}
    folder, name = os.path.split(path)
    regex = name_regex(name)
    found = {}
    try:
        entries = list(os.scandir(folder or "."))
    except OSError:
        return {}
    for entry in entries:
        match = regex.match(entry.name)
        try:
            if match and entry.is_file():
                found[match.group(1)] = entry.path
        except OSError:
            continue
    return dict(sorted(found.items()))


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def content_digest(files):
    """sha256 of one file, or of the sorted ``tile<TAB>sha256`` lines of a pattern's files."""
    if list(files) == [""]:
        return file_sha256(files[""])
    lines = "".join(f"{key}\t{file_sha256(path)}\n" for key, path in sorted(files.items()))
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


def same_content(a, b):
    """True when two files hold the same bytes: size first, then sha256."""
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
    except OSError:
        return False
    return file_sha256(a) == file_sha256(b)


def copy_file(source, target, *, overwrite=False):
    """Copy through a temporary name in the target folder, then rename into place.

    Without ``overwrite`` an existing target is never replaced (FileExistsError).
    Returns the number of bytes copied."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.urandom(4).hex()}.tmp")
    try:
        shutil.copy2(source, temporary)
        if overwrite:
            os.replace(temporary, target)
        else:
            if os.path.lexists(target):
                raise FileExistsError(f"Not overwriting an existing file: {target}")
            os.rename(temporary, target)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
    return os.path.getsize(target)


def _norm(path):
    return ntpath.normcase(ntpath.normpath(str(path)))


def is_under(path, folder):
    """True when ``path`` lies inside ``folder`` (Windows rules: case and separators ignored)."""
    if not path or not folder:
        return False
    return _norm(path).startswith(_norm(folder).rstrip("\\") + "\\")


def relative(path, folder):
    """``path`` relative to ``folder`` in posix form (``path`` must be inside it)."""
    if not is_under(path, folder):
        raise ValueError(f"{path} is not inside {folder}")
    path, folder = ntpath.normpath(str(path)), ntpath.normpath(str(folder)).rstrip("\\")
    return posix(path[len(folder) + 1 :])


def hip_text(path, hip):
    """``$HIP/...`` parameter text for a path inside the scene folder."""
    return "$HIP/" + relative(path, hip)


def parameter_text(path, hip):
    """``$HIP/...`` inside the scene folder, otherwise the plain absolute path (forward slashes)."""
    return hip_text(path, hip) if hip and is_under(path, hip) else posix(path)


def image_key(path):
    """Stable identity of a source image in ``cycles_import.json``: the normalised absolute
    path with forward slashes (case kept; compare with ``blend_assets_layout.same_path``)."""
    return posix(ntpath.normpath(str(path)))
