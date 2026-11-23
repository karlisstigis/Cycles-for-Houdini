# SPDX-License-Identifier: Apache-2.0
"""Per-user asset browser panel settings, stored beside the library registry.

Pure Python: no `hou`, no Qt. Reading is tolerant (unknown keys and invalid
values fall back to defaults); writing is atomic (temp file + os.replace).
The provider choice per library lives in the library registry, not here.
Settings of earlier builds load without error: ``texture_location`` (textures now stay
where the .blend points) is dropped like any unknown key.
"""

import json
import math
import os
import tempfile
from pathlib import Path

SETTINGS_FILENAME = "asset_browser_settings.json"
SETTINGS_VERSION = 1
_MAX_BYTES = 1024 * 1024

# "append" is shown as "Convert & Append" (importers.IMPORT_MODES).
IMPORT_MODES = ("link", "append")
# Viewport Shape set on imported Blender Reference SOPs (the node's own menu values).
REFERENCE_SHAPES = ("hull", "bounds")
# Save Location of Convert & Append for objects/collections (blend_convert storage):
# "files" = Save to Disk as .bgeo.sc ($HIP/assets_blender), "embedded" = Stash in .hip.
APPEND_STORAGES = ("files", "embedded")
SAVE_LOCATION_LABELS = {"files": "Save to Disk as .bgeo.sc", "embedded": "Stash in .hip"}
# "experimental": after a drop outside the panel, import/assign at the pane
# under the cursor. "experimental_unaccepted": the same, but only when no Qt
# drop target accepted the data (avoids doubling a host-side file drop).
DRAG_ROUTES = ("off", "experimental", "experimental_unaccepted")
THUMBNAIL_MIN, THUMBNAIL_MAX = 48, 256

DEFAULTS = {
    "thumbnail_size": 112,
    "import_mode": "link",
    "reference_shape": "hull",
    "append_storage": "files",
    "drag_enabled": False,
    "drag_route": "off",
    "last_library": "",
    "type_filter": "ALL",
    "splitter_sizes": [],
    "left_visible": True,
    "right_visible": True,
}


def settings_path():
    """CYCLES_HOUDINI_ASSET_BROWSER_SETTINGS_FILE, else beside the library registry."""
    explicit = os.environ.get("CYCLES_HOUDINI_ASSET_BROWSER_SETTINGS_FILE", "").strip()
    if explicit:
        return Path(os.path.expanduser(explicit))
    try:
        from .libraries import registry_path

        return Path(registry_path()).with_name(SETTINGS_FILENAME)
    except ImportError:
        prefs = os.environ.get("HOUDINI_USER_PREF_DIR", "").strip()
        if prefs and "__HVER__" not in prefs:
            return Path(os.path.expanduser(prefs)) / "cycles_houdini" / SETTINGS_FILENAME
        return Path.home() / ".cycles-houdini" / SETTINGS_FILENAME


def _choice(value, allowed, default):
    return value if value in allowed else default


def normalize(values):
    """Return a complete settings dict; every invalid field takes its default."""
    values = values if isinstance(values, dict) else {}
    result = dict(DEFAULTS)
    size = values.get("thumbnail_size")
    if isinstance(size, (int, float)) and not isinstance(size, bool) and math.isfinite(size):
        result["thumbnail_size"] = int(min(THUMBNAIL_MAX, max(THUMBNAIL_MIN, size)))
    result["import_mode"] = _choice(values.get("import_mode"), IMPORT_MODES, DEFAULTS["import_mode"])
    result["reference_shape"] = _choice(values.get("reference_shape"), REFERENCE_SHAPES, DEFAULTS["reference_shape"])
    result["append_storage"] = _choice(values.get("append_storage"), APPEND_STORAGES, DEFAULTS["append_storage"])
    result["drag_route"] = _choice(values.get("drag_route"), DRAG_ROUTES, DEFAULTS["drag_route"])
    for name in ("drag_enabled", "left_visible", "right_visible"):
        if isinstance(values.get(name), bool):
            result[name] = values[name]
    for name in ("last_library", "type_filter"):
        value = values.get(name)
        if isinstance(value, str) and len(value) < 4096 and "\0" not in value:
            result[name] = value
    sizes = values.get("splitter_sizes")
    if (
        isinstance(sizes, list)
        and len(sizes) == 3
        and all(isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 100000 for v in sizes)
    ):
        result["splitter_sizes"] = list(sizes)
    # Drag without a route is meaningless; keep the two consistent.
    if result["drag_route"] == "off":
        result["drag_enabled"] = False
    return result


def load_settings(path=None):
    """Tolerant read: missing, oversized or malformed files give defaults."""
    path = Path(path) if path is not None else settings_path()
    try:
        if path.stat().st_size > _MAX_BYTES:
            return normalize({})
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return normalize({})
    if not isinstance(data, dict):
        return normalize({})
    return normalize(data.get("settings") if isinstance(data.get("settings"), dict) else data)


def save_settings(values, path=None):
    """Atomic write of the normalized settings."""
    path = Path(path) if path is not None else settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": SETTINGS_VERSION, "settings": normalize(values)}
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
    return payload["settings"]
