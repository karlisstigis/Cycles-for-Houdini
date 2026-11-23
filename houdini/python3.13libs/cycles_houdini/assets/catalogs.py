# SPDX-License-Identifier: Apache-2.0
"""Blender asset catalog definitions (`blender_assets.cats.txt`) as a tree.

Pure Python. The file is read as written on disk; nothing is ever written back.
"""

from dataclasses import dataclass, field
import os
import re

CATALOG_FILENAME = "blender_assets.cats.txt"
NULL_UUID = "00000000-0000-0000-0000-000000000000"
SUPPORTED_VERSION = 1
_MAX_BYTES = 16 * 1024 * 1024
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


@dataclass
class CatalogNode:
    path: str
    name: str
    uuids: list = field(default_factory=list)
    simple_names: list = field(default_factory=list)
    children: list = field(default_factory=list)

    def walk(self):
        """Yield this node and all descendants, depth first."""
        yield self
        for child in self.children:
            yield from child.walk()


@dataclass
class CatalogTree:
    root: CatalogNode = field(default_factory=lambda: CatalogNode("", ""))
    warnings: list = field(default_factory=list)

    def __post_init__(self):
        self._by_uuid = {}
        self._by_path = {"": self.root}
        for node in self.root.walk():
            self._by_path[node.path] = node
            for uuid in node.uuids:
                self._by_uuid.setdefault(uuid, node)

    def node_for_uuid(self, uuid):
        if not isinstance(uuid, str):
            return None
        return self._by_uuid.get(uuid.strip().lower())

    def node_for_path(self, path):
        return self._by_path.get(normalize_path(path) if isinstance(path, str) else "")

    def subtree_uuids(self, path):
        """UUIDs of the node at `path` and its descendants; "" is the whole tree."""
        node = self.node_for_path(path)
        if node is None:
            return set()
        return {uuid for item in node.walk() for uuid in item.uuids}

    def is_empty(self):
        return not self.root.children


def normalize_uuid(value):
    """Lower-case UUID, or "" for the null UUID, None and malformed values."""
    if not isinstance(value, str):
        return ""
    value = value.strip().lower()
    return value if _UUID.match(value) and value != NULL_UUID else ""


def normalize_path(path):
    """Slash-separated, components stripped, empty components removed."""
    parts = (part.strip() for part in path.replace("\\", "/").split("/"))
    return "/".join(part for part in parts if part)


def _build(entries, warnings):
    """entries: iterable of (uuid, path, simple_name) in file order."""
    root = CatalogNode("", "")
    nodes = {"": root}
    seen = set()
    for uuid, path, simple_name in entries:
        if uuid in seen:
            warnings.append("Duplicate catalog UUID " + uuid + " ignored (first definition wins)")
            continue
        seen.add(uuid)
        parent = root
        parts = path.split("/")
        for depth in range(len(parts)):
            current = "/".join(parts[: depth + 1])
            node = nodes.get(current)
            if node is None:
                node = nodes[current] = CatalogNode(current, parts[depth])
                parent.children.append(node)
            parent = node
        parent.uuids.append(uuid)
        parent.simple_names.append(simple_name)
    for node in root.walk():
        node.children.sort(key=lambda child: (child.name.casefold(), child.name))
    return CatalogTree(root, warnings)


def parse_catalog_text(text):
    """Parse catalog definition text; bad lines become warnings, never errors."""
    warnings, entries = [], []
    text = text.removeprefix("\ufeff")
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("VERSION"):
            version = line[len("VERSION") :].strip()
            if version != str(SUPPORTED_VERSION):
                warnings.append(f"Line {number}: unsupported catalog file version {version!r}; parsing anyway")
            continue
        fields = line.split(":", 2)
        if len(fields) < 2:
            warnings.append(f"Line {number}: not a catalog definition")
            continue
        uuid = normalize_uuid(fields[0])
        if not uuid:
            warnings.append(f"Line {number}: invalid catalog UUID {fields[0].strip()!r}")
            continue
        path = normalize_path(fields[1])
        if not path:
            warnings.append(f"Line {number}: empty catalog path")
            continue
        entries.append((uuid, path, fields[2].strip() if len(fields) > 2 else ""))
    return _build(entries, warnings)


def tree_from_entries(catalogs):
    """Build a tree from listing catalogs: [{"path", "uuids", "simple_name"}]."""
    warnings, entries = [], []
    if not isinstance(catalogs, list):
        return CatalogTree(warnings=["Catalog list is not an array"])
    for index, item in enumerate(catalogs):
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            warnings.append(f"Catalog {index}: malformed entry")
            continue
        path = normalize_path(item["path"])
        uuids = item.get("uuids")
        simple_name = item.get("simple_name") if isinstance(item.get("simple_name"), str) else ""
        if not path or not isinstance(uuids, list):
            warnings.append(f"Catalog {index}: missing path or uuids")
            continue
        for value in uuids:
            uuid = normalize_uuid(value)
            if uuid:
                entries.append((uuid, path, simple_name))
            else:
                warnings.append(f"Catalog {index}: invalid UUID {value!r}")
    return _build(entries, warnings)


def load_catalogs(library_root):
    """Read `<library_root>/blender_assets.cats.txt`; a missing file is an empty tree."""
    path = os.path.join(os.fspath(library_root), CATALOG_FILENAME)
    try:
        if os.path.getsize(path) > _MAX_BYTES:
            return CatalogTree(warnings=["Catalog file exceeds 16 MiB; ignored"])
        with open(path, "rb") as stream:
            data = stream.read()
    except FileNotFoundError:
        return CatalogTree()
    except OSError as error:
        return CatalogTree(warnings=["Cannot read catalog file: " + str(error)])
    return parse_catalog_text(data.decode("utf-8", errors="replace"))
