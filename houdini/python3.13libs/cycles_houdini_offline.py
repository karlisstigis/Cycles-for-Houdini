# SPDX-License-Identifier: Apache-2.0
"""Save native graph archives on a built-in node, independently of the renderer.

Ordinary saves include Houdini's reduced native operator definitions, so graphs
remain networks when the renderer is absent and real DSO nodes return on reload.
The independent graph archive remains a safeguard, never a reason to overwrite
an intentionally replaced or edited node. Navigation installs no callbacks.

Each protected root carries a hidden identity (user data ``cycles.offline.id``)
that links it to its archive record. Houdini copies user data with a node, so a
copied, duplicated or pasted root arrives with the original's identity. Copies
are given their own identity (see ``_separate_copies``) after a paste in the UI,
whenever the safeguard inspects or saves the scene, and on load.
"""

from collections import Counter
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import sys
import uuid
import zipfile

import hou

BLOCK = "cycles.offline.graphs"
MARKER = "cycles.offline.id"
FORMAT = 1
BUILDERS = frozenset(
    ("cycles::material::1", "cycles::volume_material::1", "cycles::world_material::1", "cycles::light_material::1")
)
_installed = False
_reported = set()
_unresolved = set()
# Identities whose copies were separated this session. Their archive could not be
# refreshed while the copies existed, so an original that only gained nodes or
# rewired inputs since then is an authored edit, not loading damage.
_separated = set()
_copy_pending = False
# Copy events are watched in the UI, where a deferred check can run after a paste.
# None follows hou.isUIAvailable(); tests may force it.
_watch_copies = None
_busy = False
_archive_cache = None
_capture_proofs = {}
_statistics = dict(
    captures=0,
    serialized_roots=0,
    bytes=0,
    refreshed_roots=0,
    unchanged_roots=0,
    archive_cache_hits=0,
    temporary_serializations=0,
    temporary_directories=0,
)


class ArchiveError(ValueError):
    pass


def _prepare_scene_definitions():
    """Describe native classes once per save without changing socket order.

    Dummy sockets are neutral transport ports. HIP links retain source output
    indices, so their order must match the native class exactly. Dynamic group
    interfaces use Houdini's built-in subnet and connector operators instead.
    """
    groups = {}
    for node in hou.node("/").allSubChildren(recurse_in_locked_nodes=True, sync_delayed_definition=False):
        kind = node.type()
        if (
            not kind.name().startswith("cycles::")
            or kind.category() != hou.vopNodeTypeCategory()
            or kind.definition() is not None
        ):
            continue
        inputs = tuple(name for name in node.inputNames() if name)
        outputs = tuple(name for name in node.outputNames() if name)
        group = groups.setdefault(kind.name(), dict(node=node, inputs=inputs, outputs=outputs))
        if group["inputs"] != inputs or group["outputs"] != outputs:
            group["inconsistent"] = True
    if not groups:
        return
    from cycles_houdini import _native

    updated = registered = 0
    for group in groups.values():
        node = group["node"]
        script = node.type().parmTemplateGroup().asDialogScript(full_info=True)
        ports = []
        for direction in ("input", "output"):
            for name in group[direction + "s"]:
                ports.append(direction + " float " + json.dumps(name) + " " + json.dumps(name) + "\n")
        close = script.rfind("}")
        if close < 0:
            raise ArchiveError("invalid native operator dialog script: " + node.type().name())
        script = script[:close] + "".join(ports) + script[close:]
        outcome = _native.scene_definition_fallback(node.path(), script, len(group["inputs"]), len(group["outputs"]))
        if outcome.get("unowned"):
            continue
        if group.get("inconsistent"):
            raise ArchiveError("native operator has inconsistent socket layout: " + node.type().name())
        if not outcome["registered"]:
            raise ArchiveError("native scene definition registration failed: " + node.path())
        updated += bool(outcome["updated"])
        registered += 1
    _statistics["native_definition_types"] = registered
    _statistics["native_definition_updates"] = _statistics.get("native_definition_updates", 0) + updated


def _clear_scene_definitions():
    package = sys.modules.get("cycles_houdini")
    native = getattr(package, "_native", None)
    if native is not None:
        # Clearing an uninitialized runtime has no native definitions to
        # retire. Inspect the proxy without triggering its lazy __getattr__:
        # HIP replacement must remain usable when the SDK is unavailable.
        native = vars(native).get("_module", native)
    if native is not None:
        native.clear_scene_definitions()


def _notice(message):
    if message not in _reported:
        _reported.add(message)
        print("Cycles scene protection: " + message)


def _carrier():
    return hou.node("/obj")


def _has_cycles_content():
    """Cheap save gate: a retained archive or a loaded Cycles graph root.

    NodeType.instances() covers material-only HIPs and embedded placeholder
    definitions without walking unrelated OBJ, SOP and VOP networks.
    """
    carrier = _carrier()
    if carrier is not None:
        try:
            carrier.dataBlockType(BLOCK)
            return True
        except ValueError:
            pass
    category = hou.vopNodeTypeCategory()
    for name in BUILDERS:
        kind = hou.nodeType(category, name)
        if kind is not None and kind.instances():
            return True
    return False


def _identity(value):
    if not isinstance(value, str):
        raise ArchiveError("graph identity must be a canonical UUID string")
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError("noncanonical UUID")
    except ValueError as error:
        raise ArchiveError("graph identity must be a canonical UUID string") from error
    return value


def _read():
    global _archive_cache
    carrier = _carrier()
    try:
        block_type = carrier.dataBlockType(BLOCK)
    except ValueError:
        return {}, {}
    if block_type:
        raise ArchiveError("unknown data block type: " + block_type)
    data = carrier.dataBlock(BLOCK)
    if _archive_cache is not None and data == _archive_cache[0]:
        _statistics["archive_cache_hits"] += 1
        return copy.deepcopy(_archive_cache[1]), dict(_archive_cache[2])
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            if (
                not isinstance(manifest, dict)
                or type(manifest.get("format")) is not int
                or manifest["format"] != FORMAT
            ):
                raise ArchiveError("unknown archive format")
            if not isinstance(manifest.get("records"), list):
                raise ArchiveError("invalid archive record list")
            records, payloads = {}, {}
            if len(archive.namelist()) != len(set(archive.namelist())):
                raise ArchiveError("duplicated archive entry")
            for record in manifest["records"]:
                if not isinstance(record, dict):
                    raise ArchiveError("invalid graph record")
                identity = _identity(record["id"])
                if identity in records:
                    raise ArchiveError("invalid or duplicated graph identity")
                if (
                    not isinstance(record.get("path"), str)
                    or not record["path"].startswith("/")
                    or any(part in ("", ".", "..") for part in record["path"][1:].split("/"))
                    or "\\" in record["path"]
                    or record.get("type") not in BUILDERS
                    or not isinstance(record.get("nodes"), list)
                    or any(
                        not isinstance(n, dict)
                        or not isinstance(n.get("path"), str)
                        or not isinstance(n.get("type"), str)
                        for n in record["nodes"]
                    )
                ):
                    raise ArchiveError("invalid graph record metadata")
                paths = [n["path"] for n in record["nodes"]]
                if (
                    not paths
                    or len(paths) != len(set(paths))
                    or any(
                        p != "."
                        and (not p or p.startswith("/") or any(part in ("", ".", "..") for part in p.split("/")))
                        for p in paths
                    )
                    or [n for n in record["nodes"] if n["path"] == "."] != [dict(path=".", type=record["type"])]
                ):
                    raise ArchiveError("invalid graph node paths or root")
                if "connections" in record:
                    wires = record["connections"]
                    if (
                        not isinstance(wires, list)
                        or any(
                            not isinstance(wire, dict)
                            or set(wire)
                            not in (
                                {"target", "input", "source", "output"},
                                {"target", "input", "source", "output", "indirect"},
                            )
                            or ("indirect" in wire and wire["indirect"] is not True)
                            or wire["target"] not in paths
                            or wire["source"] not in paths
                            or type(wire["input"]) is not int
                            or wire["input"] < 0
                            or type(wire["output"]) is not int
                            or wire["output"] < 0
                            for wire in wires
                        )
                        or len({(wire["target"], wire["input"]) for wire in wires}) != len(wires)
                    ):
                        raise ArchiveError("invalid graph connection metadata")
                payload = archive.read(identity + ".cpio")
                if hashlib.sha256(payload).hexdigest() != record["sha256"]:
                    raise ArchiveError("graph checksum mismatch")
                records[identity], payloads[identity] = record, payload
            _archive_cache = (data, copy.deepcopy(records), dict(payloads))
            return records, payloads
    except (KeyError, ValueError, TypeError, zipfile.BadZipFile, OSError, NotImplementedError, RuntimeError) as error:
        raise ArchiveError(str(error)) from error


def _write(records, payloads):
    global _archive_cache
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "manifest.json",
            json.dumps(dict(format=FORMAT, records=sorted(records.values(), key=lambda r: r["id"])), sort_keys=True),
        )
        for identity, payload in payloads.items():
            archive.writestr(identity + ".cpio", payload)
    data = stream.getvalue()
    _carrier().setDataBlock(BLOCK, data)
    _archive_cache = (data, copy.deepcopy(records), dict(payloads))
    _statistics["bytes"] = len(data)


def _roots():
    roots = []
    for node in hou.node("/").allSubChildren(recurse_in_locked_nodes=False):
        if (
            node.type().name() not in BUILDERS
            or node.userData("cycles.offline.review")
            or node.userData("cycles_external_blend")
        ):
            continue
        ancestor = node.parent()
        skip = False
        while ancestor is not None:
            if ancestor.type().name() in BUILDERS or ancestor.userData("cycles.offline.review"):
                skip = True
                break
            ancestor = ancestor.parent()
        if not skip:
            roots.append(node)
    return roots


def _is_review(node):
    while node is not None:
        if node.userData("cycles.offline.review"):
            return True
        node = node.parent()
    return False


def _graph_nodes(node):
    return sorted(
        (
            dict(path=node.relativePathTo(child), type=child.type().name())
            for child in (node, *node.allSubChildren(recurse_in_locked_nodes=False))
        ),
        key=lambda row: row["path"],
    )


def _graph_connections(root):
    nodes = (root, *root.allSubChildren(recurse_in_locked_nodes=False))
    paths = {node.path() for node in nodes}
    result = []
    for node in nodes:
        for wire in node.inputConnections():
            indirect = wire.subnetIndirectInput()
            source = node.parent() if indirect is not None else wire.inputNode()
            # Boundary connections belong to the original parent network;
            # loading a review copy must not reconnect another live graph.
            if source is None or source.path() not in paths:
                continue
            item = dict(
                target=root.relativePathTo(node),
                input=wire.inputIndex(),
                source=root.relativePathTo(source),
                output=indirect.number() if indirect is not None else wire.outputIndex(),
            )
            if indirect is not None:
                item["indirect"] = True
            result.append(item)
    return sorted(result, key=lambda wire: (wire["target"], wire["input"], wire["source"], wire["output"]))


def _same_structure(node, record):
    return _graph_nodes(node) == sorted(record["nodes"], key=lambda row: row["path"]) and (
        "connections" not in record or _graph_connections(node) == record["connections"]
    )


def _has_damaged_descendants(node, previous):
    # A native VOP null is a legitimate authored node. A null/add carrying a
    # Cycles shader identity, or replacing a previously archived Cycles type,
    # instead indicates a possible missing operator and must retain the archive.
    # Houdini can also load a structurally identical graph using incomplete
    # Embedded definitions when the renderer DSO is absent. Serializing that
    # placeholder would silently replace the complete native CPIO payload.
    saved = {n["path"]: n["type"] for n in previous.get("nodes", ())}
    return any(
        (
            child.type().name() in ("add", "null")
            and (child.userData("cycles_graph_id") or saved.get(node.relativePathTo(child), "").startswith("cycles::"))
        )
        or (
            child.type().name().startswith("cycles::")
            and child.type().definition() is not None
            and child.type().definition().libraryFilePath() == "Embedded"
        )
        for child in (node, *node.allSubChildren(recurse_in_locked_nodes=False))
    )


def _holders():
    """Identity -> live (non-review) nodes carrying it, in one walk of unlocked networks."""
    holders = {}
    for node in hou.node("/").allSubChildren(recurse_in_locked_nodes=False):
        identity = node.userData(MARKER)
        if identity and not _is_review(node):
            holders.setdefault(identity, []).append(node)
    return holders


def _separate_copies(records):
    """Give every copy of a protected root its own identity; return {copy path: original path}.

    Copying a node copies its user data, so a copy, duplicate, Alt-drag or paste
    (also from another HIP, also inside a copied subnet or material network)
    carries the original's identity. Rule for which node is the original, i.e.
    keeps the identity and its archive record:
      1. the node at the archived record's path;
      2. otherwise the oldest node, by Houdini session id (creation order in this
         session; load order for nodes that came from the HIP).
    Every other holder gets a fresh identity and becomes a new protected graph.
    Only healthy builder roots are separated: when a placeholder, nested or
    review node carries the identity, the conflict is left for explicit review.
    """
    holders = _holders()
    duplicated = {identity: nodes for identity, nodes in holders.items() if len(nodes) > 1}
    if not duplicated:
        return {}
    roots = set(_roots())
    copies = {}
    for identity, nodes in duplicated.items():
        if any(node not in roots for node in nodes):
            continue
        record = records.get(identity)
        at_record = [node for node in nodes if record is not None and node.path() == record["path"]]
        original = at_record[0] if at_record else min(nodes, key=lambda node: node.sessionId())
        for node in nodes:
            if node != original:
                node.setUserData(MARKER, str(uuid.uuid4()))
                copies[node.path()] = original.path()
        _separated.add(identity)
    return copies


def _authored_growth(node, record):
    """True when the root still has every archived node (same type) and every archived input wired.

    Loading damage removes nodes, replaces their types or drops wires; it never adds
    nodes or reconnects an input to another source. Only such additions remain.
    """
    current = {row["path"]: row["type"] for row in _graph_nodes(node)}
    if any(current.get(row["path"]) != row["type"] for row in record["nodes"]):
        return False
    wired = {(wire["target"], wire["input"]) for wire in _graph_connections(node)}
    return all((wire["target"], wire["input"]) in wired for wire in record.get("connections", ()))


def _defer(callback):
    """Run after the current UI operation (a paste has loaded user data only by then)."""
    if hou.isUIAvailable():
        import hdefereval

        hdefereval.executeDeferred(callback)


def _after_copy():
    global _copy_pending
    _copy_pending = False
    if _busy:
        return
    try:
        records, _ = _read()
    except ArchiveError:
        records = {}
    with hou.undos.disabler():
        _separate_copies(records)
    _watch_scene()


def _child_created(**kwargs):
    """ChildCreated on /mat, /obj and networks holding protected roots: check copies after a paste."""
    global _copy_pending
    if _copy_pending or hou.hipFile.isLoadingHipFile():
        return
    _copy_pending = True
    try:
        _defer(_after_copy)
    except Exception:
        _copy_pending = False
        raise


def _watching():
    if _watch_copies is not None:
        return _watch_copies
    try:
        return bool(hou.isUIAvailable())
    except Exception:
        return False


def _watch(network):
    try:
        if network is not None and not any(callback is _child_created for _, callback in network.eventCallbacks()):
            network.addEventCallback((hou.nodeEventType.ChildCreated,), _child_created)
    except Exception:
        pass  # Watching only makes copies resolve sooner; save and inspect still resolve them.


def _watch_scene():
    """Watch the networks where copies are made; elsewhere the save/inspect safety net applies."""
    if not _watching():
        return
    try:
        for path in ("/mat", "/obj"):
            _watch(hou.node(path))
        for node in _roots():
            _watch(node.parent())
    except Exception:
        pass


def _serialize_graph(node, directory=None):
    """Keep the supported native writer and its licensed bytes unchanged.

    A capture supplies one owned scratch directory for all roots. Standalone
    callers still receive a complete, automatically cleaned native file save.
    The file is truncated before every write so a failed/partial save can never
    accidentally reuse the previous root's bytes. Empty output is rejected.
    """
    if directory is None:
        _statistics["temporary_directories"] += 1
        with tempfile.TemporaryDirectory(prefix="cycles-hip-") as directory:
            return _serialize_graph(node, directory)
    _statistics["temporary_serializations"] += 1
    path = Path(directory) / "graph.cpio"
    path.write_bytes(b"")
    node.parent().saveItemsToFile((node,), str(path))
    payload = path.read_bytes()
    if not payload:
        raise ArchiveError("native graph writer returned an empty archive")
    return payload


def capture():
    """Refresh healthy authored roots, retaining unresolved/deleted graph archives.

    A corrupt/unknown existing block is never overwritten, even by healthy graphs.
    Intentional deletions are not recreated; their archives remain available.
    """
    global _busy
    if _busy:
        return
    _busy = True
    scratch = None
    try:
        # Native persistence must not depend on a readable, unambiguous archive.
        # Registration changes no live node or parameter and happens only here.
        _prepare_scene_definitions()
        try:
            records, payloads = _read()
        except ArchiveError as error:
            _notice(f"the backup of the Cycles materials (/obj/{BLOCK}) is unreadable ({error}); it was kept as is")
            return
        # Copies made since the last check get their own identity first, so the
        # original keeps refreshing its archive record.
        _separate_copies(records)
        roots = _roots()
        # A pending damaged placeholder owns its identity just as strongly as a
        # healthy builder. Merged/copied healthy roots must not overwrite it.
        counts = Counter({identity: len(nodes) for identity, nodes in _holders().items()})
        captured = 0
        proofs = {}
        for node in roots:
            identity = node.userData(MARKER)
            if identity in _unresolved:
                # Reported once at load with what to do; nothing new to say per save.
                continue
            if identity and counts[identity] > 1:
                _notice(
                    f"{node.path()} shares its hidden backup ID with a node that could not be checked "
                    "(missing or damaged Cycles node?); its backup was kept unchanged"
                )
                continue
            if identity:
                try:
                    _identity(identity)
                except ArchiveError:
                    _notice(f"{node.path()} has an invalid hidden backup ID; its backup was kept unchanged")
                    continue
            else:
                identity = str(uuid.uuid4())
                node.setUserData(MARKER, identity)
                if _watching():
                    _watch(node.parent())
            # Never replace an archive when a surviving root is partly damaged.
            if _has_damaged_descendants(node, records.get(identity, {})):
                _notice(f"{node.path()} may be damaged (missing Cycles nodes?); its backup was kept unchanged")
                continue
            if scratch is None:
                scratch = tempfile.TemporaryDirectory(prefix="cycles-hip-")
                _statistics["temporary_directories"] += 1
            payload = _serialize_graph(node, scratch.name)
            _statistics["serialized_roots"] += 1
            previous = records.get(identity)
            if (
                previous is not None
                and previous["path"] == node.path()
                and previous["type"] == node.type().name()
                and payloads.get(identity) == payload
                and _capture_proofs.get(identity) == (previous, payload)
            ):
                _statistics["unchanged_roots"] += 1
                continue
            records[identity] = dict(
                id=identity,
                path=node.path(),
                type=node.type().name(),
                sha256=hashlib.sha256(payload).hexdigest(),
                nodes=_graph_nodes(node),
                connections=_graph_connections(node),
            )
            payloads[identity] = payload
            proofs[identity] = (copy.deepcopy(records[identity]), payload)
            captured += 1
        if captured:
            _write(records, payloads)
            _capture_proofs.update(proofs)
            _statistics["captures"] += 1
            _statistics["refreshed_roots"] += captured
    finally:
        try:
            if scratch is not None:
                scratch.cleanup()
        finally:
            _busy = False


def inspect():
    """Return graph archive status without serializing anything.

    The only change it makes is giving copies their own identity (``_separate_copies``).
    """
    records, _ = _read()
    _separate_copies(records)
    return _inspect(records)


def _inspect(records):
    counts = Counter({identity: len(nodes) for identity, nodes in _holders().items()})
    result = []
    for record in records.values():
        node = hou.node(record["path"])
        if node is None:
            status = "missing (intentional deletion or move; no automatic restore)"
        elif counts[record["id"]] > 1:
            status = "conflict (duplicate identity; explicit review required)"
        elif node.userData(MARKER) != record["id"]:
            status = "conflict (identity differs)"
        elif node.type().name() == record["type"]:
            if _same_structure(node, record):
                status = "healthy graph structure (archive available)"
            elif (
                record["id"] in _separated
                and _authored_growth(node, record)
                and not _has_damaged_descendants(node, record)
            ):
                # Copies kept this archive from refreshing; nothing archived is lost.
                status = "healthy graph structure (authored additions since the archive; refreshed on save)"
            else:
                status = "changed graph structure (authored edit or damage; explicit review required)"
        else:
            status = "damaged or replaced (explicit review required)"
        result.append(dict(path=record["path"], id=record["id"], status=status, archive="/obj/" + BLOCK))
    return result


def recover_copy(identity, destination):
    """Explicitly load one archived root into an empty compatible review network.

    The caller creates/chooses the destination. This method refuses an existing
    child, never renames the original and never rewrites material assignments.
    On load failure it retains partial review nodes and the archive for diagnosis.
    Native serialization may contain Python callbacks; use only trusted HIPs.
    """
    records, payloads = _read()
    record = records[identity]
    original = hou.node(record["path"])
    if destination.path() == record["path"] or destination.path().startswith(record["path"] + "/"):
        raise ArchiveError("review network must be outside the original graph")
    if destination == hou.node(record["path"].rsplit("/", 1)[0]):
        raise ArchiveError("choose a separate review network")
    if destination.children():
        raise ArchiveError("review network must be empty")
    try:
        with tempfile.TemporaryDirectory(prefix="cycles-review-") as directory:
            path = Path(directory) / "graph.cpio"
            path.write_bytes(payloads[identity])
            destination.loadItemsFromFile(str(path))
    finally:
        # Mark only newly loaded roots (including a partial failed load), so
        # future authored graphs in this destination remain protected normally.
        for child in destination.children():
            child.setUserData("cycles.offline.review", "1")
    children = destination.children()
    if (
        len(children) != 1
        or children[0].type().name() != record["type"]
        or children[0].name() != record["path"].rsplit("/", 1)[1]
        or children[0].userData(MARKER) != identity
        or not _same_structure(children[0], record)
    ):
        raise ArchiveError("review load differs from saved root; archive retained")
    if original is not None and original.path() != record["path"]:
        raise ArchiveError("original graph path changed during review load")
    return children[0]


def accept_current(identity):
    """Explicitly accept a loaded structural edit for the next ordinary save.

    This does not restore or save anything. Only use after reviewing a graph
    whose structure differed from its archive when the HIP was loaded. The
    next save may replace that archive with the artist's current graph.
    """
    records, _ = _read()
    record = records[identity]
    _separate_copies(records)
    node = hou.node(record["path"])
    if node is None:
        # A root moved or renamed while its archive could not follow.
        moved = _holders().get(identity, ())
        node = moved[0] if len(moved) == 1 else None
    if node is None or node.userData(MARKER) != identity or node.type().name() != record["type"]:
        raise ArchiveError("acceptance requires the original healthy root and identity")
    owners = _holders().get(identity, ())
    if len(owners) != 1 or _has_damaged_descendants(node, record):
        raise ArchiveError("ambiguous or possibly damaged graph cannot be accepted")
    _unresolved.discard(identity)
    _capture_proofs.pop(identity, None)


def _review_root(kwargs):
    """The clicked protected root, if its graph is waiting for explicit review."""
    node = (kwargs or {}).get("node")
    if node is None or node.type().name() not in BUILDERS:
        return None
    identity = node.userData(MARKER)
    return node if identity and identity in _unresolved else None


def needs_review(kwargs=None):
    """Node menu context (OPmenu.xml): show "Keep Current Graph" only where it applies. Never raises."""
    try:
        return _review_root(kwargs) is not None
    except Exception:
        return False


def _tell(text):
    message = "Cycles scene protection: " + text
    try:
        if hou.isUIAvailable():
            hou.ui.setStatusMessage(message, severity=hou.severityType.Message)
    except Exception:
        pass
    print(message)


def keep_current(kwargs=None):
    """Right-click > Cycles > Keep Current Graph: the graph in the scene is right; back it up on save."""
    node = _review_root(kwargs)
    if node is None:
        return False
    try:
        accept_current(node.userData(MARKER))
    except (ArchiveError, KeyError):
        _tell(f"{node.path()}: cannot keep this graph automatically (shared or damaged); its backup stays unchanged")
        return False
    _tell(f"{node.path()}: current graph kept; save the scene to update its backup")
    return True


def _explain(row):
    """One line for an artist: what is wrong and what to do."""
    path, status = row["path"], row["status"]
    if row.get("moved"):
        return (
            f"{row['moved']}: was moved or renamed from {path} while its backup could not follow. "
            "If the graph looks right: right-click it > Cycles > Keep Current Graph, then save"
        )
    if "duplicate identity" in status:
        return (
            f"{path}: shares its hidden backup ID with a node that could not be checked "
            "(missing or damaged Cycles node?); its backup is kept unchanged"
        )
    if status.startswith("changed graph structure"):
        return (
            f"{path}: nodes or wires differ from its saved backup (deleted on purpose, or lost while loading?). "
            "If the graph looks right: right-click it > Cycles > Keep Current Graph, then save"
        )
    if status.startswith("conflict (identity differs)"):
        return f"{path}: was replaced by another node; the old graph's backup is kept at {row['archive']}"
    if status.startswith("damaged or replaced"):
        return f"{path}: the material node changed type or failed to load; its backup is kept at {row['archive']}"
    return f"{path}: does not match its saved backup ({status}); its backup is kept at {row['archive']}"


def statistics():
    return dict(_statistics)


def _scene_event(event):
    global _archive_cache
    if event == hou.hipFileEventType.BeforeSave:
        if _has_cycles_content():
            capture()
    elif event in (hou.hipFileEventType.BeforeLoad, hou.hipFileEventType.AfterClear):
        _clear_scene_definitions()
        _reported.clear()
        _unresolved.clear()
        _separated.clear()
        _archive_cache = None
        _capture_proofs.clear()
        if event == hou.hipFileEventType.AfterClear:
            _watch_scene()
    elif event == hou.hipFileEventType.AfterLoad:
        try:
            records, _ = _read()
            # Copies saved by earlier versions repair themselves; the next save keeps it.
            copies = _separate_copies(records)
            if copies:
                _notice(
                    "gave copied materials their own backup: "
                    + ", ".join(sorted(copies))
                    + " (save the scene to keep this)"
                )
            holders = None
            for record in _inspect(records):
                if record["status"].startswith("missing"):
                    if holders is None:
                        holders = _holders()
                    moved = holders.get(record["id"])
                    if not moved:
                        continue  # Deleted on purpose; the archive simply stays available.
                    record = dict(record, moved=moved[0].path())
                if not record["status"].startswith("healthy"):
                    _unresolved.add(record["id"])
                    _notice(_explain(record))
        except ArchiveError as error:
            _notice(f"the backup of the Cycles materials (/obj/{BLOCK}) is unreadable ({error}); it was kept as is")
        _watch_scene()


def install():
    global _installed
    if not _installed:
        hou.hipFile.addEventCallback(_scene_event)
        _installed = True
        _watch_scene()
