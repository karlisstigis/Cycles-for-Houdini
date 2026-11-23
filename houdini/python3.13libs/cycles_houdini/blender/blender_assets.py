# SPDX-License-Identifier: Apache-2.0
"""Prepare Blender render prototypes for the native Houdini point instancer."""

import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
from functools import lru_cache
import tempfile
import threading
import sys
from time import perf_counter
from .blend_cache import ByteBudgetCache, budget_bytes

import hou
from . import blend_worker


_directory = None


def _entry_storage(entry):
    from .. import _native

    storage = {}
    visited = set()

    def metadata(value):
        identity = id(value)
        if identity in visited:
            return
        visited.add(identity)
        storage[("python", identity)] = sys.getsizeof(value)
        if isinstance(value, dict):
            for key, child in value.items():
                metadata(key)
                metadata(child)
        elif isinstance(value, (tuple, list)):
            for child in value:
                metadata(child)

    metadata(entry)
    for instance in entry["instances"]:
        for identity, amount in _native.blend_mesh_storage(instance["mesh"]).items():
            storage[("native", identity)] = amount
    for digest in entry["material_paths"]:
        metadata(_materials[digest]["record"])
    return storage


_bundles = ByteBudgetCache(
    budget_bytes("CYCLES_HOUDINI_BLEND_CACHE_MB", 1024),
    storage=_entry_storage,
    label="Blender prototypes",
    describe=lambda key: (
        f"{key[1]}#{key[2].lower()}{'+disabled' if key[8:] == (True,) else ''}={key[3]} frame {key[4]:g}"
    ),
)
_meshes = {}
_topologies = {}
_materials = {}
_retained = {}
_consumers = {}
_capture_callbacks = {}
_metrics = dict(
    cache_hits=0,
    cache_misses=0,
    invalidations=0,
    evaluation_batches=0,
    evaluated_prototypes=0,
    native_conversions=0,
    evaluation_transfer_seconds=0.0,
    native_conversion_seconds=0.0,
    evicted_prototypes=0,
    cache_clears=0,
    cleared_prototypes=0,
)
_miss_reasons = dict.fromkeys(
    ("not_resident", "dependency_changed", "launch_changed", "forced_epoch", "material_missing"), 0
)
_root_id = None
_last_cache_diagnostics = None
_marker = "cycles_external_blend"
# Grammar shared with blend_targets.REFERENCE_KINDS; see blend_targets.
_selector = re.compile(r"^(.+?\.blend)#(object|collection(?:\+disabled)?)=(.+)$", re.IGNORECASE)


def _blender():
    from ..core.blender_bridge import require_executable

    return require_executable()


def _signature(paths):
    def stat(path):
        try:
            info = os.stat(path)
            return (info.st_mtime_ns, info.st_ctime_ns, info.st_size, info.st_ino)
        except OSError:
            return None

    return tuple(stat(path) for path in paths)


def _share_textures(directory, materials):
    """Workers can still read a texture after a frame's geometry has been evicted."""
    session = Path(_directory.name)
    for material in materials:
        try:
            _share_material_textures(directory, session, material)
        except (ValueError, OSError) as error:
            # A texture problem belongs to its material, never to the geometry:
            # the material renders with the fallback (see _material_path).
            material["unsupported"] = "Texture unavailable: " + str(error)


def _share_material_textures(directory, session, material):
    for node in material["nodes"]:
        image = node.get("image")
        if image is None:
            continue
        records = image["dependencies"]
        group = hashlib.sha256(
            json.dumps([(Path(d["filename"]).name, d["sha256"]) for d in records]).encode()
        ).hexdigest()
        prefix = Path("images") / group
        (session / prefix).mkdir(parents=True, exist_ok=True)
        for record in records:
            src = (directory / record["filename"]).resolve()
            if not src.is_relative_to(directory) or not src.is_file():
                raise ValueError("Invalid owned Blender texture: " + str(src))
            filename = prefix / src.name
            if not (session / filename).exists():
                shutil.copyfile(src, session / filename)
            record["filename"] = filename.as_posix()
        image["filename"] = (prefix / Path(image["filename"]).name).as_posix()


def _node(ident):
    return hou.nodeBySessionId(ident) if ident is not None else None


def _hip_event(event):
    global _root_id
    if event in (hou.hipFileEventType.BeforeSave, hou.hipFileEventType.BeforeClear, hou.hipFileEventType.BeforeLoad):
        # Only the artist's instance/index attributes belong in the saved HIP.
        root = _node(_root_id)
        if root is not None and event == hou.hipFileEventType.BeforeSave:
            with hou.undos.disabler():
                root.destroy()
        _root_id = None
        if event != hou.hipFileEventType.BeforeSave:
            _retained.clear()
            _consumers.clear()
            _capture_callbacks.clear()
            _metrics["cache_clears"] += 1
            _metrics["cleared_prototypes"] += len(_bundles)
            _bundles.clear()
            _meshes.clear()
            _topologies.clear()
            _materials.clear()
            from .. import _native
            from .blend_bounds import invalidate_refreshes

            invalidate_refreshes()
            _native.clear_blender_reference_bounds()
            blend_worker.shutdown_all()


def _network():
    global _root_id
    root = _node(_root_id)
    if root is None:
        root = hou.node("/obj").createNode("subnet", "cycles_blend_prototypes", run_init_scripts=False)
        _root_id = root.sessionId()
        root.setUserData(_marker, "1")
        root.setDisplayFlag(False)
        root.hide(True)
    return root


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _material_path(material):
    """Native path of a translated material, or "" for one that cannot translate.

    A reference renders its geometry even when a material cannot be rebuilt:
    "" is the empty slot, so those faces use the inherited Houdini material or
    the ROP fallback material, and _prepare_result reports why. The verdict is
    cached with the material digest (record + working colour configuration).
    """
    if material.get("error") is not None:
        return ""
    if _node(material.get("node_id")) is None:
        from .blend_materials import BlendMaterialError, import_materials

        record = material["record"]
        if record.get("unsupported"):
            material["error"] = str(record["unsupported"])
            return ""
        warnings = []
        with hou.undos.disabler():
            network = _network().createNode("matnet", "shader_" + material["digest"])
            try:
                paths = import_materials([record], Path(_directory.name), parent=network, diagnostics=warnings)
            except BlendMaterialError as error:
                network.destroy()
                material["error"] = str(error) or type(error).__name__
                return ""
            except BaseException:
                network.destroy()
                raise
        material["warnings"] = [issue for issue in warnings if issue.get("severity") != "error"]
        material["path"] = paths[record["key"]]
        material["node_id"] = network.sessionId()
    return material["path"]


def _material_diagnostics(materials):
    """Render diagnostics for untranslated and approximated Blender materials."""
    result = []
    for material in materials:
        name = material["record"].get("name", material["record"].get("key", ""))
        if material.get("error") is not None:
            result.append(
                dict(
                    code="blend.material_untranslated",
                    severity="warning",
                    source=name,
                    message="Blender material " + name + " cannot be translated: " + material["error"],
                    action="Rendering its faces with the inherited Houdini material or the ROP fallback material",
                )
            )
        for issue in material.get("warnings", ()):
            result.append(dict(issue, action=issue.get("action", "Using the translated material")))
    return result


def _native_mesh(mesh, arrays, identities, material_slot_count):
    """Intern raw shape buffers independently of instance material bindings."""
    from .. import _native

    record = dict(mesh)
    layout = {
        k: v
        for k, v in mesh.items()
        if k not in ("name", "key", "arrays", "uv_layers", "attributes", "color_attributes")
    }
    layout["arrays"] = {name: identities[key] for name, key in sorted(mesh["arrays"].items())}
    if material_slot_count > 65536:
        raise ValueError("Blender mesh has too many material slots")
    # Blender's create_mesh clamps material indices to the object's slots (a negative index
    # uses the first slot, one past the end the last); the native capture clamps the same way.
    slots = arrays[mesh["arrays"]["material_indices"]]
    lowest, highest = (int(slots.min()), int(slots.max())) if slots.size else (0, 0)
    slot_count = max(1, material_slot_count)
    if lowest < 0 or highest >= slot_count:
        layout["material_slot_count"] = slot_count  # the clamped result depends on it
    for group in ("uv_layers", "attributes", "color_attributes"):
        fields = [dict(field, native_name=hou.text.encodeAttrib(field["name"])) for field in mesh.get(group, ())]
        record[group] = fields
        layout[group] = [dict(field, array=identities[field["array"]]) for field in fields]
    key = "blend/mesh/" + hashlib.sha256(_json(layout)).hexdigest()
    capsule = _meshes.get(key)
    if capsule is None:
        topology = dict(
            layout,
            arrays={
                name: identity
                for name, identity in layout["arrays"].items()
                if name not in ("positions", "normals", "vertex_normals")
            },
        )
        topology_key = hashlib.sha256(_json(topology)).hexdigest()
        previous = _meshes.get(_topologies.get(topology_key))
        record["key"] = key
        palette = ("",) * (min(max(highest, 0), slot_count - 1) + 1)
        capsule = _native.blend_mesh(record, arrays, palette, previous, previous is not None)
        _meshes[key] = capsule
        _topologies[topology_key] = key
    return key, capsule


# Blender colour attributes become a colour field and an alpha field (as on meshes, blend_mesh.cpp).
_HAIR_COLOR_TYPES = frozenset(("FLOAT_COLOR", "BYTE_COLOR"))


def _native_curves(hair, arrays, identities, material_slot_count):
    """Intern a Link hair record (blend_export._hair_curves_record / _particle_hair_record) as
    native curves: Blender's control points and per-curve values expanded per key, curves of one
    key removed (Cycles builds no segment for them; their random values stay Blender's)."""
    import numpy as np

    from .. import _native

    layout = {k: v for k, v in hair.items() if k not in ("name", "key", "arrays", "attributes", "skipped_attributes")}
    layout["arrays"] = {name: identities[key] for name, key in sorted(hair["arrays"].items())}
    layout["attributes"] = [dict(field, array=identities[field["array"]]) for field in hair.get("attributes", ())]
    slot_count = max(1, material_slot_count)
    layout["material_slot_count"] = slot_count
    key = "blend/curves/" + hashlib.sha256(_json(layout)).hexdigest()
    capsule = _meshes.get(key)
    if capsule is not None:
        return key, capsule
    source = {name: np.asarray(arrays[archive]) for name, archive in hair["arrays"].items()}
    counts = np.diff(source["offsets"].astype(np.int64))
    keep = counts >= 2
    points = np.repeat(keep, counts)
    kept = counts[keep]
    local = dict(
        positions=np.ascontiguousarray(source["positions"].reshape(-1, 3)[points], dtype=np.float32),
        radii=np.ascontiguousarray(source["radius"].reshape(-1)[points], dtype=np.float32),
        first_key=np.ascontiguousarray(np.concatenate([[0], np.cumsum(kept)[:-1]]) if len(kept) else kept, np.int32),
        randoms=np.ascontiguousarray(source["randoms"][keep], dtype=np.float32),
    )
    if "material_slots" in source:
        local["material_slots"] = np.ascontiguousarray(
            np.clip(source["material_slots"][keep], 0, slot_count - 1), dtype=np.int32
        )
    if "generated" in source:
        local["generated"] = np.ascontiguousarray(source["generated"].reshape(-1, 3)[keep], dtype=np.float32)
    fields = []

    def add(name, values):
        archive = "attribute_" + str(len(fields))
        local[archive] = np.ascontiguousarray(values, dtype=np.float32)
        fields.append(dict(name=name, native_name=name, components=int(values.shape[1]), array=archive))

    for field in hair.get("attributes", ()):
        values = np.asarray(arrays[field["array"]], dtype=np.float32)
        values = values.reshape(len(values), -1)
        if field["domain"] == "CURVE":
            per_curve = values[keep]
            if field["name"] == hair.get("uv_attribute"):
                local["uv"] = np.ascontiguousarray(per_curve[:, :2], dtype=np.float32)
            values = np.repeat(per_curve, kept, axis=0)
        else:
            values = values[points]
        name = hou.text.encodeAttrib(field["name"])
        if field["data_type"] in _HAIR_COLOR_TYPES:
            add(name, values[:, :3])
            add("__cycles_blender_alpha_" + name, values[:, 3:4])
        elif values.shape[1] <= 3:
            add(name, values)
    record = dict(
        key=key,
        shape=hair["shape"],
        # A Curves object has no Generated attribute in Blender's translator (object-space
        # position fallback); particle hair carries one value per strand.
        generated_attribute=hair["kind"] != "curves",
        arrays={name: name for name in local if not name.startswith("attribute_")},
        attributes=fields,
    )
    capsule = _native.blend_curves(record, local, ("",) * slot_count)
    _meshes[key] = capsule
    return key, capsule


def _native_points(cloud, arrays, identities):
    """Intern a Link point cloud record (blend_export._point_cloud_record) as native points:
    Blender's points in their own order with one palette entry (Blender renders every point with
    shader 0), no Generated attribute (generated_attribute False: Cycles falls back to the
    object-space position), attributes by name with colours split into colour and alpha."""
    import numpy as np

    from .. import _native

    layout = {k: v for k, v in cloud.items() if k not in ("name", "key", "arrays", "attributes", "skipped_attributes")}
    layout["arrays"] = {name: identities[key] for name, key in sorted(cloud["arrays"].items())}
    layout["attributes"] = [dict(field, array=identities[field["array"]]) for field in cloud.get("attributes", ())]
    key = "blend/points/" + hashlib.sha256(_json(layout)).hexdigest()
    capsule = _meshes.get(key)
    if capsule is not None:
        return key, capsule
    source = {name: np.asarray(arrays[archive]) for name, archive in cloud["arrays"].items()}
    local = dict(
        positions=np.ascontiguousarray(source["positions"].reshape(-1, 3), dtype=np.float32),
        radii=np.ascontiguousarray(source["radius"].reshape(-1), dtype=np.float32),
        randoms=np.ascontiguousarray(source["randoms"].reshape(-1), dtype=np.float32),
    )
    fields = []

    def add(name, values):
        archive = "attribute_" + str(len(fields))
        local[archive] = np.ascontiguousarray(values, dtype=np.float32)
        fields.append(dict(name=name, native_name=name, components=int(values.shape[1]), array=archive))

    for field in cloud.get("attributes", ()):
        values = np.asarray(arrays[field["array"]], dtype=np.float32)
        values = values.reshape(len(values), -1)
        name = hou.text.encodeAttrib(field["name"])
        if field["data_type"] in _HAIR_COLOR_TYPES:
            add(name, values[:, :3])
            add("__cycles_blender_alpha_" + name, values[:, 3:4])
        elif values.shape[1] <= 3:
            add(name, values)
    record = dict(
        key=key,
        generated_attribute=False,
        arrays={name: name for name in local if not name.startswith("attribute_")},
        attributes=fields,
    )
    capsule = _native.blend_points(record, local, ("",))
    _meshes[key] = capsule
    return key, capsule


def _blender_object(values):
    """Blender's per-object values of one Link instance for the native member (python_scene.cpp
    "blender_object"). Object/Instancer attributes become a colour field and an alpha field
    under the names blend_materials gives the Attribute node (blend_export.object_attribute_field)."""
    from .blend_names import object_attribute_field

    fields = []
    for attribute in values.get("attributes", ()):
        value = [float(v) for v in attribute["value"]]
        for alpha in (False, True):
            name = hou.text.encodeAttrib(object_attribute_field(attribute["type"], attribute["name"], alpha))
            fields.append(dict(name=name, components=1 if alpha else 3, value=value[3:4] if alpha else value[:3]))
    return dict(
        random_id=int(values["random_id"]),
        visibility=int(values["visibility"]),
        holdout=bool(values["holdout"]),
        shadow_catcher=bool(values["shadow_catcher"]),
        pass_index=int(values["pass_index"]),
        color=[float(v) for v in values["color"]],
        attributes=fields,
    )


def _prepare_result(manifest, arrays, working_config):
    materials = {}
    for record in manifest["materials"]:
        digest = hashlib.sha256(_json([record, working_config])).hexdigest()
        material = _materials.setdefault(digest, dict(digest=digest, record=record))
        materials[record["key"]] = material
    paths = {k: _material_path(m) for k, m in materials.items()}
    meshes = {m["key"]: m for m in manifest["meshes"]}
    hair = {h["key"]: h for h in manifest.get("hair", ())}
    clouds = {p["key"]: p for p in manifest.get("points", ())}
    prepared = {}
    instances = []
    for instance in manifest["instances"]:
        palette = tuple(paths[k] if k else "" for k in instance["material_keys"])
        geometry_type = instance.get("geometry_type", "mesh")
        if geometry_type in ("curves", "particle_hair"):
            record = hair[instance["mesh_key"]]
            variant = (record["key"], len(palette))
            if variant not in prepared:
                prepared[variant] = _native_curves(record, arrays, manifest["array_identities"], len(palette))
            mesh = {}
        elif geometry_type == "points":
            record = clouds[instance["mesh_key"]]
            variant = ("points", record["key"])
            if variant not in prepared:
                prepared[variant] = _native_points(record, arrays, manifest["array_identities"])
            mesh = {}
        else:
            mesh = meshes[instance["mesh_key"]]
            variant = mesh["key"]
            if variant not in prepared:
                prepared[variant] = _native_mesh(mesh, arrays, manifest["array_identities"], len(palette))
        geometry_key, capsule = prepared[variant]
        identity = [
            manifest["source"],
            instance.get("source_library", ""),
            instance.get("source_name", instance.get("name", instance["key"])),
            instance.get("parent_name"),
            instance.get("persistent_id", []),
        ]
        if geometry_type == "particle_hair":
            identity.append(geometry_type)  # the emitter's mesh is another member
        instances.append(
            dict(
                key=instance["key"],
                identity="blend/member/" + hashlib.sha256(_json(identity)).hexdigest(),
                transform=instance["transform"],
                mesh=capsule,
                geometry_key=geometry_key,
                material_paths=palette,
                subdivision=mesh.get("subdivision"),
            )
        )
        if instance.get("blender_object") is not None:
            instances[-1]["blender_object"] = _blender_object(instance["blender_object"])
    actions = {
        "blend.non_mesh_object": "Skipping this collection member",
        "blend.unsupported_geometry": "Rendering the rest of the selection without it",
        "blend.excluded_collection": "Skipping this excluded child collection",
        "blend.native_subdivision": "Subdividing the control cage in Cycles",
        "blend.subdivision_evaluated": "Using Blender's evaluated subdivision mesh",
    }
    diagnostics = [
        dict(issue, action=issue.get("action", actions.get(issue["code"], "Using the exported material")))
        for issue in manifest["diagnostics"]
    ] + _material_diagnostics(materials.values())
    return dict(
        instances=instances,
        bounds_manifest=dict(
            source=manifest["source"],
            selector_type=manifest["selector_type"],
            selector=manifest["selector"],
            frame=manifest["frame"],
            dependency_versions=manifest["dependency_versions"],
            materials=[
                dict(settings=material.get("settings", {}), outputs={key: None for key in material.get("outputs", {})})
                for material in manifest["materials"]
            ],
            viewport_hulls=manifest.get("viewport_hulls"),
            viewport_hull_reason=manifest.get("viewport_hull_reason"),
            include_excluded_collections=manifest.get("include_excluded_collections", False),
        ),
        diagnostics=diagnostics,
        material_paths={m["digest"]: paths[k] for k, m in materials.items()},
    )


def _resolve_reference(reference):
    match = _selector.fullmatch(reference)
    if match is None:
        raise ValueError(
            "Expected path.blend#object=Name, path.blend#collection=Name or "
            "path.blend#collection+disabled=Name and a finite frame"
        )
    path = Path(hou.text.expandString(match[1]))
    if not path.is_absolute():
        path = Path(hou.text.expandString("$HIP")) / path
    source = os.path.normcase(str(path.resolve(strict=True)))
    executable = _blender()
    import PyOpenColorIO as ocio

    source_config = hou.getenv("CYCLES_HOUDINI_BLENDER_OCIO")
    working_config = ocio.GetCurrentConfig().getCacheID()
    return os.path.normcase(str(executable)), source, match[2].upper(), match[3], source_config, working_config


def _resolve_spec(spec, resolve=_resolve_reference):
    reference, frame, native_subdivision = spec
    if not math.isfinite(frame):
        raise ValueError("Blender frame must be finite")
    executable, source, selector_type, selector, source_config, working_config = resolve(reference)
    # COLLECTION+DISABLED keeps children the source view layer excludes. It
    # stays a separate prototype: the flag is the last element of the key.
    selector_type, _, variant = selector_type.partition("+")
    include_excluded = variant == "DISABLED"
    key = (
        executable,
        source,
        selector_type,
        selector,
        frame,
        source_config,
        working_config,
        native_subdivision,
        include_excluded,
    )
    request = dict(
        source=source, selector_type=key[2], selector=key[3], frame=frame, native_subdivision=native_subdivision
    )
    if include_excluded:
        request["include_excluded_collections"] = True
    return key, request


class _NoPrewarm:
    scheduled = False

    def close(self):
        pass

    cancel = close

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class _Prewarm:
    scheduled = True

    def __init__(self, sources):
        self.sources = sources
        self.cancelled = threading.Event()
        self.errors = []

        def start():
            for source, executable, config in sources:
                if self.cancelled.is_set():
                    break
                try:
                    blend_worker.prewarm(source, executable, source_config=config, cancelled=self.cancelled)
                except Exception as error:
                    self.errors.append(error)  # prepare_prototypes reports launch errors normally.

        self.thread = threading.Thread(target=start, daemon=True, name="CH Blender prewarm")
        self.thread.start()

    def close(self):
        self.cancelled.set()
        self.thread.join()
        for source, executable, config in self.sources:
            blend_worker.discard_prewarm(source, executable, config)

    cancel = close

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def prewarm_prototypes(specs):
    """Resolve Houdini state now; overlap only process startup on a helper thread."""
    sources = set()
    for spec in specs:
        try:
            key, request = _resolve_spec(spec)
            entry = _bundles.get(key)
            if (
                entry is not None
                and entry["launch_signature"] == blend_worker.launch_signature(key[0], key[5])
                and _signature(entry["dependencies"]) == entry["signature"]
                and entry.get("epoch", 0) == blend_worker._epoch(key[1])
            ):
                continue
            sources.add((key[1], key[0], key[5]))
        except Exception:
            continue  # Per-spec preparation returns the useful selector/path diagnostic.
    return _Prewarm(sources) if sources else _NoPrewarm()


def _convert_evaluation(key, result, directory, launch, epoch):
    """Convert one owned stream on the host thread, validating its revision."""
    manifest, arrays = result
    if manifest.get("schema_version") != 1:
        raise ValueError("Incompatible Blender geometry stream")
    versions = manifest["dependency_versions"]
    dependencies = sorted({*versions, key[0], *([key[5]] if key[5] else [])})
    before = _signature(dependencies)
    if any(
        blend_worker._signature(path) != (tuple(version) if version is not None else None)
        for path, version in versions.items()
    ):
        raise blend_worker.StaleEvaluationError("Blender dependency changed before native conversion")
    _share_textures(Path(directory), manifest["materials"])
    started = perf_counter()
    try:
        entry = _prepare_result(manifest, arrays, key[6])
    finally:
        _metrics["native_conversion_seconds"] += perf_counter() - started
    _metrics["native_conversions"] += 1
    entry.update(cache_key=key, dependencies=dependencies, signature=before, launch_signature=launch, epoch=epoch)
    return entry


def accept_evaluation(key, raw_result, tempdir, launch, epoch):
    """Adopt an explicit bounds refresh into the existing prototype cache.

    The refresh owns its raw buffers/textures until this host-thread conversion
    completes. No second persistent raw cache or Houdini geometry is created.
    """
    global _directory
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("Blender prototype conversion requires Houdini's main thread")
    if _directory is None:
        _directory = tempfile.TemporaryDirectory(prefix="cycles_blend_images_", ignore_cleanup_errors=True)
    entry = _convert_evaluation(key, raw_result, tempdir, launch, epoch)
    import PyOpenColorIO as ocio

    if (
        entry["signature"] != _signature(entry["dependencies"])
        or epoch != blend_worker._epoch(key[1])
        or launch != blend_worker.launch_signature(key[0], key[5])
        or key[6] != ocio.GetCurrentConfig().getCacheID()
    ):
        raise blend_worker.StaleEvaluationError("Blender inputs changed during bounds prototype conversion")
    _bundles[key] = entry
    return entry


def resident_evaluation(key):
    """Return an accepted refresh only while its ordinary cache entry is valid."""
    entry = _bundles.get(key)
    if (
        entry is not None
        and entry["signature"] == _signature(entry["dependencies"])
        and entry.get("epoch", 0) == blend_worker._epoch(key[1])
        and entry["launch_signature"] == blend_worker.launch_signature(key[0], key[5])
        and all(_material_path(_materials[digest]) == path for digest, path in entry["material_paths"].items())
    ):
        return entry
    return None


def prepare_prototypes(specs, *, _attempt=0, publish_bounds=False, bounds_specs=None):
    """Return one prepared entry or Exception per spec, batching source misses."""
    global _directory
    from .prototype_leases import drain_releases

    drain_releases()
    from .blend_bounds import publication_generation

    publication_epoch = publication_generation() if publish_bounds else None
    specs = list(specs)
    bounds_specs = set(bounds_specs) if bounds_specs is not None else None
    results = [None] * len(specs)
    groups = {}
    canonical, aliases = {}, {}
    # Multiple samples of a reference share path expansion, OCIO lookup and
    # filesystem observations. Take fresh observations again before returning.
    resolve = lru_cache(maxsize=None)(_resolve_reference)
    observe = lru_cache(maxsize=None)(_signature)
    launch_for = lru_cache(maxsize=None)(blend_worker.launch_signature)
    for index, spec in enumerate(specs):
        try:
            key, request = _resolve_spec(spec, resolve)
            # Packed primitives retain symbolic paths; point attributes may
            # already contain their expanded spelling. Resolve once per spec,
            # then evaluate each canonical source/selector/frame state once.
            if key in canonical:
                aliases[index] = canonical[key]
                continue
            canonical[key] = index
            entry = _bundles.get(key)
            launch = launch_for(key[0], key[5])
            reason = "not_resident"
            if entry is not None:
                if entry["launch_signature"] != launch:
                    reason = "launch_changed"
                elif observe(tuple(entry["dependencies"])) != entry["signature"]:
                    reason = "dependency_changed"
                elif entry.get("epoch", 0) != blend_worker._epoch(key[1]):
                    reason = "forced_epoch"
                else:
                    reason = "material_missing"
            if entry is not None and reason == "material_missing":
                if all(_material_path(_materials[digest]) == path for digest, path in entry["material_paths"].items()):
                    _metrics["cache_hits"] += 1
                    results[index] = entry
                    continue
            _metrics["cache_misses"] += 1
            _miss_reasons[reason] += 1
            if entry is not None:
                _metrics["invalidations"] += 1
            from .blend_bounds import join_evaluation

            joined = join_evaluation(key)
            if joined is not None:
                results[index] = joined
                continue
            groups.setdefault((key[1], key[0], key[5]), []).append((index, key, request, launch))
        except Exception as error:
            results[index] = error
    if _directory is None:
        _directory = tempfile.TemporaryDirectory(prefix="cycles_blend_images_", ignore_cleanup_errors=True)
    if _hip_event not in hou.hipFile.eventCallbacks():
        hou.hipFile.addEventCallback(_hip_event)
    # Do not trim between sources: the caller pins the complete scene only once
    # every prepared entry has reached its native instance index.
    for (source, executable, source_config), items in groups.items():
        for attempt in range(3):
            staged = []
            generation = blend_worker._epoch(source)
            launch = blend_worker.launch_signature(executable, source_config)
            try:
                with tempfile.TemporaryDirectory(dir=_directory.name) as temp:

                    def consume(ordinal, result):
                        index, key, request, previous_launch = items[ordinal]
                        try:
                            if isinstance(result, Exception):
                                return result
                            return _convert_evaluation(
                                key, result, Path(temp) / ("request-" + str(ordinal)), launch, generation
                            )
                        except Exception as error:
                            return error

                    _metrics["evaluation_batches"] += 1
                    _metrics["evaluated_prototypes"] += len(items)
                    evaluation_started = perf_counter()
                    conversion_before = _metrics["native_conversion_seconds"]
                    try:
                        raw = blend_worker.evaluate_many(
                            source,
                            executable,
                            [item[2] for item in items],
                            temp,
                            source_config=source_config,
                            consume=consume,
                        )
                    finally:
                        _metrics["evaluation_transfer_seconds"] += max(
                            0.0,
                            perf_counter()
                            - evaluation_started
                            - (_metrics["native_conversion_seconds"] - conversion_before),
                        )
                    staged = [(item[0], item[1], result) for item, result in zip(items, raw)]
                    for index, key, entry in staged:
                        if isinstance(entry, blend_worker.StaleEvaluationError):
                            raise entry
                    # Publish this entire source group only after conversion has
                    # completed and every input still belongs to one revision.
                    if generation != blend_worker._epoch(source) or launch != blend_worker.launch_signature(
                        executable, source_config
                    ):
                        raise blend_worker.StaleEvaluationError(
                            "Blender launch inputs changed during native conversion"
                        )
                    for index, key, entry in staged:
                        if not isinstance(entry, Exception) and entry["signature"] != _signature(entry["dependencies"]):
                            raise blend_worker.StaleEvaluationError(
                                "Blender dependency changed during native conversion"
                            )
                    for index, key, entry in staged:
                        results[index] = entry
                        if not isinstance(entry, Exception):
                            _bundles[key] = entry
                break
            except blend_worker.StaleEvaluationError as error:
                if attempt == 2:
                    for index, key, request, previous_launch in items:
                        results[index] = error
            except Exception as error:
                for index, key, request, previous_launch in items:
                    results[index] = error
                break
    import PyOpenColorIO as ocio

    current_working_config = ocio.GetCurrentConfig().getCacheID()
    observe.cache_clear()
    launch_for.cache_clear()
    stale_sources = set()
    for result in results:
        if not isinstance(result, Exception) and result is not None:
            key = result["cache_key"]
            if (
                result["signature"] != observe(tuple(result["dependencies"]))
                or result["launch_signature"] != launch_for(key[0], key[5])
                or current_working_config != key[6]
                or result.get("epoch", 0) != blend_worker._epoch(key[1])
            ):
                stale_sources.add(key[1])
    if stale_sources:
        indices = [
            index
            for index, result in enumerate(results)
            if isinstance(result, dict) and result["cache_key"][1] in stale_sources
        ]
        if _attempt < 2:
            refreshed = prepare_prototypes(
                [specs[index] for index in indices],
                _attempt=_attempt + 1,
                publish_bounds=publish_bounds,
                bounds_specs=bounds_specs,
            )
            for index, result in zip(indices, refreshed):
                results[index] = result
        else:
            for index in indices:
                results[index] = blend_worker.StaleEvaluationError(
                    "Blender inputs kept changing before scene publication"
                )
    # Fan out only after retries and dependency validation. Every spelling
    # receives the same accepted entry (or failure), while publication below
    # still visits each requested alias and its own bounds-selection policy.
    for index, original_index in aliases.items():
        results[index] = results[original_index]
    if publish_bounds:
        from .blend_bounds import prepared_record, publish_record

        for index, (spec, result) in enumerate(zip(specs, results)):
            if isinstance(result, dict) and (bounds_specs is None or tuple(spec) in bounds_specs):
                try:
                    if "bounds_record" not in result:
                        result["bounds_record"] = prepared_record(result)
                    publish_record(spec[0], spec[1], result["bounds_record"], generation=publication_epoch)
                except Exception as error:
                    # Preview bounds are optional; a cache/publication failure
                    # must never discard an otherwise valid render prototype.
                    unknown = dict(schema_version=1, known=False, reason=str(error))
                    try:
                        publish_record(spec[0], spec[1], unknown, generation=publication_epoch)
                    except Exception:
                        pass
                    # This alias's failure is a capture diagnostic. Keep the
                    # accepted cache entry healthy so later publication retries.
                    result = dict(result, bounds_record=unknown, diagnostics=list(result.get("diagnostics", ())))
                    results[index] = result
                    message = "Could not cache Blender preview bounds: " + str(error)
                    diagnostics = result.setdefault("diagnostics", [])
                    if not any(
                        issue.get("code") == "blend.bounds_unavailable" and issue.get("message") == message
                        for issue in diagnostics
                    ):
                        diagnostics.append(
                            dict(
                                code="blend.bounds_unavailable",
                                severity="warning",
                                source=spec[0],
                                message=message,
                                action="Using the unknown bounds placeholder",
                            )
                        )
    return results


def prepare_prototype(reference, frame, *, native_subdivision=True):
    result = prepare_prototypes([(reference, frame, native_subdivision)])[0]
    if isinstance(result, Exception):
        raise result
    return result


def cache_diagnostics(*, workers=False):
    """Bounded counters and current ownership, without per-prototype history.

    A changed identity (target/frame/config/subdivision/path) is not_resident;
    revision reasons distinguish invalidation of an entry at the same identity.
    evaluation_* counters cover render preparation; refresh_* counters include
    artist refresh attempts, including failed and cancelled evaluations.
    """
    diagnostics = dict(
        prototypes=dict(_bundles.snapshot(), evicted=(_last_cache_diagnostics or {}).get("evicted", [])),
        runtime=dict(_metrics, cache_miss_reasons=dict(_miss_reasons)),
    )
    from .blend_bounds import refresh_diagnostics

    diagnostics["runtime"].update(refresh_diagnostics())
    if workers:
        with blend_worker._pool_lock:
            worker_snapshot = tuple(blend_worker._workers.values())
        diagnostics["workers"] = [
            dict(pid=worker.process.pid, transfer=worker.metrics.copy(), chunks=worker.chunks.snapshot())
            for worker in worker_snapshot
        ]
    return diagnostics


def retain_prototypes(rop, keys):
    """Pin every live ROP sample, then retain inactive samples up to the byte target."""
    _retained[rop.sessionId()] = set(keys)
    if rop.sessionId() not in _capture_callbacks:
        ident = rop.sessionId()

        def deleted(**unused):
            _capture_callbacks.pop(ident, None)
            release_capture(ident)

        rop.addEventCallback((hou.nodeEventType.BeingDeleted,), deleted)
        _capture_callbacks[ident] = deleted
    for ident in tuple(_retained):
        if _node(ident) is None:
            del _retained[ident]
    for ident, retained in {**_retained, **_consumers}.items():
        _bundles.pin(ident, retained)
    for ident in tuple(_bundles._pins):
        if ident not in _retained and ident not in _consumers:
            _bundles.unpin(ident)
    _trim_prototypes()


def _trim_prototypes():
    """Reclaim inactive native resources under the same shared byte budget."""
    global _last_cache_diagnostics
    _last_cache_diagnostics = _bundles.trim()
    _metrics["evicted_prototypes"] += len(_last_cache_diagnostics["evicted"])
    used_meshes = {i["geometry_key"] for entry in _bundles.values() for i in entry["instances"]}
    for key in _meshes.keys() - used_meshes:
        del _meshes[key]
    for key in tuple(_topologies):
        if _topologies[key] not in _meshes:
            del _topologies[key]
    used_materials = {digest for entry in _bundles.values() for digest in entry["material_paths"]}
    with hou.undos.disabler():
        for digest in _materials.keys() - used_materials:
            material = _materials.pop(digest)
            network = _node(material.get("node_id"))
            if network is not None:
                network.destroy()


def retain_consumer(owner, keys):
    """Lease the latest samples for one rendering consumer, independently of ROPs."""
    token = ("consumer", owner)
    _consumers[token] = set(keys)
    _bundles.pin(token, keys)


def release_consumer(owner):
    """Release one stopped/closed renderer without discarding warm resident data."""
    token = ("consumer", owner)
    _consumers.pop(token, None)
    _bundles.unpin(token)
    _trim_prototypes()


def release_capture(rop_id):
    """Transfer capture ownership to rendering consumers without dropping them."""
    _retained.pop(rop_id, None)
    _bundles.unpin(rop_id)
    _trim_prototypes()
