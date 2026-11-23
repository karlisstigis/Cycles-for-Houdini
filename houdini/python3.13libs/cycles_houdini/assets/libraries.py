# SPDX-License-Identifier: Apache-2.0
"""Registry of Blender asset libraries known to the Houdini asset browser.

JSON: {"version": 1, "libraries": [{name, path, enabled, import_method, provider}]}.
Loading is tolerant; an unreadable registry is reported and never replaced
silently: saving over it first moves it aside as `<name>.corrupt-<time>`.
"""

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import tempfile
import time

REGISTRY_VERSION = 1
PROVIDER_KINDS = ("worker", "listing")
IMPORT_METHODS = ("LINK", "APPEND", "APPEND_REUSE", "PACK", "FOLLOW_PREFS")
_MAX_BYTES = 4 * 1024 * 1024


@dataclass
class Library:
    name: str
    path: str
    enabled: bool = True
    import_method: str = "APPEND"
    provider: str = "worker"

    def __post_init__(self):
        # Houdini convention: forward slashes, also for folders from Windows dialogs or Blender's preferences.
        if isinstance(self.path, (str, os.PathLike)):
            self.path = os.fspath(self.path).replace("\\", "/")

    def key(self):
        return library_key(self.path)


def library_key(path):
    """Identity of a library folder: normcased absolute path."""
    return os.path.normcase(os.path.abspath(os.path.expanduser(os.fspath(path))))


def user_pref_dir():
    """HOUDINI_USER_PREF_DIR from the environment, or None when unusable."""
    value = os.environ.get("HOUDINI_USER_PREF_DIR", "").strip()
    if not value or "__HVER__" in value:
        return None
    return Path(os.path.expanduser(value))


def registry_path():
    explicit = os.environ.get("CYCLES_HOUDINI_ASSET_LIBRARIES_FILE", "").strip()
    if explicit:
        return Path(os.path.expanduser(explicit))
    prefs = user_pref_dir()
    if prefs is not None:
        return prefs / "cycles_houdini" / "asset_libraries.json"
    return Path.home() / ".cycles-houdini" / "asset_libraries.json"


def _library(entry):
    if not isinstance(entry, dict):
        return None
    path, name = entry.get("path"), entry.get("name")
    if not isinstance(path, str) or not path.strip() or "\0" in path:
        return None
    if not isinstance(name, str) or not name.strip():
        name = os.path.basename(os.path.normpath(path)) or path
    method = entry.get("import_method")
    provider = entry.get("provider")
    return Library(
        name=name.strip(),
        path=path,
        enabled=entry.get("enabled") if isinstance(entry.get("enabled"), bool) else True,
        import_method=method if method in IMPORT_METHODS else "APPEND",
        provider=provider if provider in PROVIDER_KINDS else "worker",
    )


def read_registry(path=None):
    """Return (libraries, error). A missing file is ([], ""); a corrupt one is ([], message)."""
    path = Path(path) if path is not None else registry_path()
    try:
        if path.stat().st_size > _MAX_BYTES:
            return [], f"Asset library registry {path} exceeds 4 MiB"
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return [], ""
    except (OSError, ValueError) as error:
        return [], f"Cannot read asset library registry {path}: {error}"
    if not isinstance(data, dict) or not isinstance(data.get("libraries"), list):
        return [], f"Asset library registry {path} has an unknown layout"
    if data.get("version") != REGISTRY_VERSION:
        return [], f"Asset library registry {path} has unsupported version {data.get('version')!r}"
    libraries, seen = [], set()
    for entry in data["libraries"]:
        library = _library(entry)
        if library is not None and library.key() not in seen:
            seen.add(library.key())
            libraries.append(library)
    return libraries, ""


def load_libraries(path=None):
    """Tolerant load; use read_registry() to see why a registry was ignored."""
    return read_registry(path)[0]


def save_libraries(libraries, path=None):
    """Write atomically (temp file + os.replace). A corrupt registry is kept aside first."""
    path = Path(path) if path is not None else registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and read_registry(path)[1]:
        os.replace(path, path.with_name(path.name + time.strftime(".corrupt-%Y%m%d-%H%M%S")))
    payload = {"version": REGISTRY_VERSION, "libraries": [asdict(library) for library in libraries]}
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _unique_name(name, taken):
    candidate, number = name, 2
    while candidate.casefold() in taken:
        candidate = f"{name} ({number})"
        number += 1
    taken.add(candidate.casefold())
    return candidate


def merge_blender_libraries(existing, dumped):
    """Add Blender preference libraries not yet registered.

    Deduplicated by normcased absolute path; existing entries keep their names
    and flags. New names are made unique.
    """
    result = list(existing)
    keys = {library.key() for library in result}
    taken = {library.name.casefold() for library in result}
    for entry in dumped or ():
        library = _library(entry)
        if library is None or library.key() in keys:
            continue
        keys.add(library.key())
        library.name = _unique_name(library.name, taken)
        result.append(library)
    return result
