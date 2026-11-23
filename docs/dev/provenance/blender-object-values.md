# Blender per-instance values: instance random number and Object/Instancer attributes (behaviour specification)

Status: behaviour description for a separately written replacement. Written 21 November 2026.
Scope: three pieces of `blender/cycles_blender/blend_export.py` (until 21 November 2026
`houdini/python3.13libs/cycles_houdini/blender/blend_export.py`), which runs inside
Blender (it imports `bpy` objects through its callers) when a .blend file is linked for rendering:

1. `dupli_random_id(name, persistent_id, root_name=None)`: the Object Info > Random number of an instance;
2. `_rgba(value)` and `property_rgba(block, name)`: a property of a data-block as an RGBA colour;
3. `_ObjectValues.attributes(instance, values, requests)` and whatever helpers it needs: the values of
   Attribute nodes of type Object and Instancer for one exported instance, including attributes stored on
   geometry-nodes instances.

Everything else in the file stays, in particular `hash_uint2`, `hash_string`, `object_random_id`,
`ray_visibility`, `_layer_flags`, `_dupli_path` and `_ObjectValues.__init__`, `track`, `_root` and `values`.
(Note, 21 November 2026: when the scripts that run inside Blender became the GPL-3.0-or-later Blender
component, `hash_uint2`, `hash_string` and `object_random_id` moved unchanged to the Apache-2.0 module
`houdini/python3.13libs/cycles_houdini/blender/blend_ids.py`, which `blend_export.py` imports.)

This document describes behaviour and mathematics only. Rules that exist only because Blender behaves that
way are marked **[interop]** and listed again in section 6.

Python API pages used (Blender Python API 5.3, read 21 November 2026):
`bpy.types.DepsgraphObjectInstance` (https://docs.blender.org/api/current/bpy.types.DepsgraphObjectInstance.html),
`bpy.types.GeometrySet` (https://docs.blender.org/api/current/bpy.types.GeometrySet.html),
`bpy.types.bpy_struct.path_resolve` and `keys` (https://docs.blender.org/api/current/bpy.types.bpy_struct.html).
Manual pages used (manual 5.3, read 21 November 2026): Attribute Node
(https://docs.blender.org/manual/en/latest/render/shader_nodes/input/attribute.html) and Attributes
reference (https://docs.blender.org/manual/en/latest/modeling/geometry_nodes/attributes_reference.html).

## 1. Interfaces the rest of the file expects (keep them)

- `dupli_random_id(name: str, persistent_id: list[int], root_name: str | None = None) -> int`: returns an
  unsigned 32-bit integer. Called from `_ObjectValues.values` for members of a collection selection
  (see 3).
- `_rgba(value) -> list[float] | None` and `property_rgba(block, name: str) -> list[float] | None`: four
  Python floats, or None. Only used by `attributes`; you may restructure them, but keep the names (the
  licence note in the file and this document refer to them).
- `_ObjectValues.attributes(self, instance, values, requests) -> list[dict]`:
  - `instance`: a `bpy.types.DepsgraphObjectInstance` from `depsgraph.object_instances` (read
    `instance.object`, `instance.particle_system`);
  - `values`: the dict returned by `self.values(instance, original)`; its entry `values["_root"]` is the
    tuple `(root_evaluated, path, dupli)`: `root_evaluated` is the evaluated scene object that generated
    the instance as Blender would see it (for a real object: the object itself), `path` is the list of
    used `persistent_id` entries below that root, innermost first, and `dupli` is True when Blender would
    treat the instance as an instance (False for a real object, including members of a selected
    collection);
  - `requests`: a list of `(type, name)` pairs, type `"OBJECT"` or `"INSTANCER"` (Blender's
    `ShaderNodeAttribute.attribute_type`), in a fixed order;
  - returns one `dict(type=type, name=name, value=[r, g, b, a])` per request, in the same order, with
    Python floats; `[0.0, 0.0, 0.0, 0.0]` when nothing is found.
  - Raise `ValueError("Non-finite Blender " + type.lower() + " attribute " + name)` when a found value has a
    non-finite component (own rule; the exporter reports it).
- `_ObjectValues.__init__` already creates `self.levels = {}` and `self.columns = {}`, free for caches of
  the geometry-nodes lookup (one `_ObjectValues` lives for one export; caching per root object and per
  attribute name is what keeps large scatters fast). Use them or not.

## 2. Instance random number: `dupli_random_id`

Blender exposes the number as `DepsgraphObjectInstance.random_id`: "Random id for this instance,
typically for randomized shading" (API page above). The exporter reads that number directly for object
selections. For a selected collection, the exporter evaluates the collection through a temporary
selector object whose own level must not take part in the number, so the number has to be recomputed.
The rule **[interop: the composition below]**:

- Let H2(x, y) be `hash_uint2(x, y)` and S(text) be `hash_string(text)`, both already in this file:
  transcriptions of Cycles' `hash_uint2` and `hash_string` (Apache-2.0, `third_party/cycles/src/util/
  hash.h` lines 139-150 and 637-648; SPDX-FileCopyrightText 2011-2022 Blender Foundation). H2 is the
  "final" mix of Bob Jenkins' lookup3 hash with the initial value 0xDEADBEEF + 8 + 13 (public domain:
  B. Jenkins, lookup3.c, 2006, http://burtleburtle.net/bob/c/lookup3.c). S is the multiplicative string
  hash h = 37 h + byte over the UTF-8 bytes, taken as unsigned, modulo 2^32.
- h = S(name).
- If `persistent_id` is not empty: for each entry e in the given order (innermost first), h = H2(h, e mod
  2^32) (negative entries wrap as two's complement). If it is empty: h = H2(h, 0).
- If `root_name` is not None: h = h XOR H2(S(root_name), 0).
- Return h (0 ... 2^32 - 1).

## 3. A property as a colour: `_rgba` and `property_rgba`

Blender manual, Attribute Node (https://docs.blender.org/manual/en/latest/render/shader_nodes/input/attribute.html,
"Blender 5.3 Manual", read 21 November 2026), property Attribute Type: "Specifies the type of the attribute.
The property value must be an integer, float, boolean, or a vector of 1 to 4 floats or ints; properties of
other types are ignored. If a suitable property is not found, all sockets of the node, including *Alpha*,
output 0." Type Object: "The attribute name specifies a custom property name, or an RNA path to a built-in
property (like the single property driver variables). The values of attributes of this type are defined
once per object. The name or path is looked up first in the object data-block, followed by the mesh
data-block if not found. Custom properties have priority over built-in ones." (Quoted as the page reads;
the asterisks mark the page's italics. The lookup in the object and then its data is the order of
section 4.) Precisely:

`property_rgba(block, name)`:

1. If `block` is None: None.
2. If `name` is one of the block's custom property keys (`block.keys()`): convert `block[name]` and return
   the result, even if that is None (a custom property of an unusable type hides a built-in property of
   the same name) **[interop]**.
3. Otherwise resolve `name` as a data path on the block (`block.path_resolve(name)`, which accepts names
   such as `color`, `location`, `data.materials[0].diffuse_color` or `["prop"]`); convert the value. If the
   path does not resolve (Blender raises `ValueError`; also treat `AttributeError` and `TypeError` from
   unusual blocks as not found): None.

Conversion `_rgba(value)` **[interop: the alpha of 1 and the zero fill]**:

- a Python `bool`: as the number 1.0 (True) or 0.0 (False);
- an `int` or `float`: `[v, v, v, 1.0]`;
- a `str` or `bytes`: None;
- anything iterable with 1 to 4 items that are all `int` or `float` (not `bool`): start from
  `[0.0, 0.0, 0.0, 1.0]` and replace the first items by the values (so a 3-vector gets alpha 1, a 4-vector
  keeps its own fourth value, a 2-vector gets blue 0);
- anything else (not iterable, empty, more than 4 items, boolean arrays, nested sequences such as
  matrices, data-block pointers, enums): None.

## 4. Object and Instancer attributes: `attributes`

For each request `(type, name)`:

1. Only if `type` is `"INSTANCER"` and `dupli` is True, try in this order and stop at the first value
   found **[interop: the order]** (manual, Attribute node, type Instancer: "Similar to Object, but the
   attribute is looked up in the instancer particle system settings, followed by Geometry Node instance
   attributes (searching from the innermost instancing layer to outer ones), and finally in the instancer
   object. If the current object is not instanced, or the property is not found, it falls back to the
   Object mode."):
   a. `property_rgba(instance.particle_system.settings, name)` when `instance.particle_system` is not None;
   b. the geometry-nodes instance attribute of section 5;
   c. `property_rgba(root_evaluated, name)`.
2. If nothing was found (and always for `"OBJECT"`): `property_rgba(instance.object, name)`, then
   `property_rgba(instance.object.data, name)` (manual, type Object: "The name or path is looked up first
   in the object data-block, followed by the mesh data-block if not found.").
3. If still nothing: `[0.0, 0.0, 0.0, 0.0]`.
4. Check finiteness (section 1) and append `dict(type, name, value)`.

## 5. Attributes stored on geometry-nodes instances

### 5.1 What Blender's numbering looks like

When an object's evaluated geometry contains instances, every instance it generates carries in its
`persistent_id` one entry per instancing level, innermost first; `path` (section 1) is that list below the
root. Reading `path` from the outermost entry inwards, each geometry level contributes **[interop]**:

- first, if that level's geometry also holds real (non-instance) components that Blender renders as
  separate instances, one entry equal to the number c of such components (see below); these components
  themselves are numbered 0 ... c-1 at that position, which is how they are told apart from instances;
- then one entry identifying the instance within that level's instances: the instance's integer `id`
  attribute when the instances carry one (an `id` attribute of type `INT` on the instance domain, which the
  Python API shows as domain `POINT` of `GeometrySet.instances_pointcloud()`), otherwise the instance's
  index. When several instances share an `id`, only the first of them can be matched; Blender gives the
  others substitute numbers that cannot be predicted from the API **[interop]**.

Counting the components c of one level: count which of `mesh`, `volume`, `curves`, `pointcloud`,
`grease_pencil` of the level's `GeometrySet` are not None. At the outermost level (the root object's own
evaluated geometry), do not count the component that is the root object's own kind: `mesh` for object type
`MESH`; `volume` for `VOLUME`; `curves` for `CURVE`, `FONT` and `CURVES`; `pointcloud` for `POINTCLOUD`;
`grease_pencil` for `GREASEPENCIL` (Blender renders that one as the object itself) **[interop]**. At deeper
levels count every component present.

The geometry of the next level is the instance's reference: `GeometrySet.instance_references()` indexed by
the instance's `.reference_index` point attribute ("The data instanced by each point is referenced by the
`.reference_index` attribute, indexing into the list returned by `bpy.types.GeometrySet.instance_references()`.
It may contain other geometry sets, objects, collections and None values." API page above). A
`GeometrySet` reference is the next level directly; an object or collection reference continues the walk as
5.2 describes **[interop]** (established by experiment, see 5.4); a None reference ends the walk.

### 5.2 Finding the layers of one instance

Start with the root's evaluated geometry, `root_evaluated.evaluated_geometry()` (none if the object has no
such method), counted as an outermost level (5.1: the root's own kind is not counted), and walk `path` from
its last entry (outermost) towards its first:

1. If the current geometry is None, or its `instances_pointcloud()` is None or has no points: stop.
2. Compute c (5.1). If c > 0: the next path entry must equal c, otherwise stop (the instance is one of the
   level's own components, not an instance); consume it; if `path` is now exhausted, stop.
3. Take the next path entry as the identifier and find the instance index: with an `id` attribute
   (5.1), the first point whose `id` equals it; without, the identifier itself if 0 <= identifier < number
   of points. If there is none: stop.
4. Record this layer (the level's instances point cloud and the index). Then look at the instance's
   reference (5.1) **[interop]**:
   - a `GeometrySet`: it is the next geometry; continue with 1;
   - an object: the next geometry is that object's own evaluated geometry, treated like the root's (its own
     kind not counted at that level). The object takes no path entry of its own. Continue with 1;
   - a collection: the next path entry e selects the member: member k = e // 2 of the collection's
     `objects` (in that order). An even entry e = 2k is member k's own geometry: stop (no further layer).
     An odd entry e = 2k + 1 is one of the instances member k's geometry makes: consume the entry and
     continue with member k's evaluated geometry as for an object reference. If e is outside the members,
     or the collection has child collections (their order is not established, 5.4): stop;
   - None or anything else: stop.
5. When the walk stops, only the 4 innermost recorded layers (the last 4 recorded) take part in the search
   of 5.3 **[interop]** (manual, Attribute node: "Currently only up to 4 layers of Geometry Node instancing
   are searched.").

The Instancer fallback object of section 4 (step 1c) stays `root_evaluated`, the scene object that generated
the whole tree of instances, also when the walk passed through object or collection references **[interop]**.

### 5.3 Reading the value

Search the recorded layers from the innermost (last recorded) to the outermost **[interop: innermost
first]**. In a layer, look for a point attribute called `name` on the instances point cloud with domain
`POINT`. Convert it to RGBA by its `data_type` **[interop: the alpha of 1 and the zero fill]** (manual,
Attributes reference, Data Conversion: "Between color and vector - mapping between color channels and
vector components"; "when a float becomes a vector the value is used for each component"; "true maps to
1, and false maps to 0"):

| `data_type` | Read through | RGBA |
|---|---|---|
| `FLOAT`, `INT`, `INT8`, `BOOLEAN` | `value` | (v, v, v, 1) |
| `FLOAT2` | `vector` | (x, y, 0, 1) |
| `INT32_2D` | `value` (2 per point) | (x, y, 0, 1) |
| `FLOAT_VECTOR` | `vector` | (x, y, z, 1) |
| `FLOAT_COLOR`, `BYTE_COLOR` | `color` | (r, g, b, a) as the API returns them (scene-linear floats) |

Any other type (for example quaternions, matrices, strings) counts as "not in this layer" and the search
goes on to the next outer layer. The first layer that has a convertible attribute gives the value for the
instance's index, as four Python floats. If no layer has it: not found.

Reading whole columns with `foreach_get` into numpy arrays and caching them per layer and name is the
efficient way; the implementer is free in how.

### 5.4 Basis of 5.1 and 5.2, and points not pinned down

The rules of 5.1 and 5.2 beyond what the manual states were established by experiment with Blender 5.3
itself (the `bpy` 5.3.0 module on Linux): Cycles renders of Attribute nodes of type Instancer compared with
the values the rules predict, per instance (by the implementer, 21 November 2026; what was built and what Blender
rendered is recorded in 5.5). They are pinned by `tests/pytest_cases/test_blend_instance_layers.py`.

- Layer limit (E1): six and five nested Instance on Points layers, an attribute on every layer. Blender
  found the attributes of the 4 innermost layers and rendered 0 for layers 5 and 6. The limit of 4 is
  confirmed.
- Component counting (E2): the rule of 5.1 (count the components present; at the outermost level not the
  root's own kind) agreed in 11 scenes with roots of type `MESH`, `CURVES`, `CURVE`, `FONT`, `POINTCLOUD`
  and `VOLUME` and with components on levels inside the instances; two deliberately wrong variants (no
  count; counting the own kind) failed. A mesh object's evaluated geometry has a `mesh` component even when
  its node tree outputs instances only, so the own-kind rule matters.
- Objects instanced inside instances (E3): Blender continues into the instanced object's own instance
  layers, numbered like a root, with no path entry for the object itself; tested also with an extra point
  cloud in the instanced object. The Instancer fallback object is the generating scene object (a custom
  property on both objects told them apart).
- Instanced collections (E3 variant `coll`, E4 with three members): member k uses path entry 2k for its own
  geometry and 2k + 1 for the instances its geometry nodes make (16 of 16 instances agreed).

Not confirmed:
- `GREASEPENCIL` roots (creating one aborts the `bpy` module on the headless test machine); the rule of 5.1
  is kept for them.
- Whether the 4-layer limit counts the intermediate level that Object Info / Collection Info "As Instance"
  creates, and how it counts levels across an object or collection boundary (E1 had an attribute on every
  level and no such boundary).
- Collections with child collections (their member order was not measured).
- Several instances sharing one `id` value: the rule of 5.1 (only the first can be matched) is kept.
- With a collection selected for export, instances nested 7 layers deep were missing from the export (the
  selection adds a level and Blender stops at its nesting limit); this concerns the export selection, not
  the attribute lookup.

### 5.5 Experiment record (E1-E4)

All four experiments used the same method. A Python script run with the `bpy` 5.3.0 module (Linux,
headless) built a scene from scratch through the public API, stored FLOAT_COLOR instance attributes with
Store Named Attribute nodes (domain Instance; the value is a function of the instance position, so each
instance has its own colour), gave every visible plane an emission material whose colour is an Attribute
node of type Instancer (or Object), and rendered it with Blender's own Cycles to an EXR file. For every
instance in the evaluated depsgraph (`object_instances`), the script compared the colour Blender rendered
at that instance's plane with the value the rules of 5.1-5.3 predict from its `persistent_id`. A rule
counts as confirmed only if every instance agrees; each experiment also includes cases in which a wrong
rule gives a different colour.

- E1, layer limit. A root mesh object whose geometry-nodes tree nests Instance on Points six times (and,
  in a second run, five times) around emitting planes; layer k (1 = innermost) stores attribute `d<k>`.
  Each `d<k>` was rendered separately. Six layers: for `d1` to `d4` all 64 instances showed the stored
  values; for `d5` and `d6` all 64 rendered 0. Five layers (32 instances): `d1` to `d4` found, `d5` 0.
  Shown: Blender searches the four innermost instancing layers and no more, as its manual says
  ("Currently only up to 4 layers of Geometry Node instancing are searched").
- E2, counting the own components of a level. Eleven root objects of types MESH, CURVES, CURVE, FONT,
  POINTCLOUD and VOLUME, each joining instances (planes with attribute `ca`) with extra non-instance
  components (mesh, points, curves, volume) moved out of view; in two of them the extra components sit one
  level deeper, inside the instanced geometry, with `oa` on the outer instances. With the rule of 5.1 all
  4 instances of every root agreed for `ca` and for `oa` (88 of 88; for the nine roots without an outer
  level `oa` rendered 0, as predicted). Two deliberately wrong rules were run as controls: not counting
  the components at all (9 of the 10 roots of that run then disagreed for `ca`), and also counting the
  root's own kind (10 of the 11 roots then disagreed for `ca`; the exception, a legacy curve joined with a
  mesh, does not tell the two rules apart). Also seen: a
  mesh object's evaluated geometry has a mesh component even when its node tree outputs instances only.
- E3, an object instanced inside instances. "Inner" scatters planes and stores `ia` and `sa` on its
  instances; "Outer" instances Inner three times (Object Info, As Instance) and stores `oa` and `sa` on
  its own instances; a custom property `ip` on Outer. Blender rendered Inner's `ia` through Outer (the
  rule as first written, "an object reference ends the walk", predicted 0: 0 of 6 agreed); with the walk
  continuing into Inner's own instance layers, numbered like a root, all 6 agreed for `ia`, `oa`, `sa` and
  `ip`. `sa`, stored on both levels, rendered Inner's value: the innermost layer wins. `ip` rendered
  Outer's property: the fallback object is the scene object that generated everything. Variants: an extra
  point cloud inside Inner (6 of 6 for each attribute) and Inner reached through an instanced collection
  (6 of 6), which showed the collection member numbering studied in E4.
- E4, members of an instanced collection. Outer instances a collection with three members: A (a plain
  mesh, out of view), B and C (each scatters emitting planes and stores its own `ia`). Member k used
  path entry 2k for its own geometry and 2k + 1 for the instances its node tree makes; with that rule all
  8 planes agreed for `ia` and for `ip` (16 of 16).

## 6. Interoperability facts (no basis other than "Blender does this")

| # | Fact | Pinned by |
|---|---|---|
| O1 | Instance random number: string hash of the name, chained with H2 over the persistent id (innermost first; 0 if none), XOR H2(S(root), 0) when the root differs | `test_ported_hashes_reference_values`, `test_instance_random_ids_equal_blenders_depsgraph`, `test_collection_members_get_the_real_object_hash` |
| O2 | Property conversion: scalar to (v, v, v, 1); 1-4 numbers fill (0, 0, 0, 1); other types none; a custom property hides a built-in one even when unusable | `test_object_colour_pass_index_and_object_attributes` (tint, color, pass_index, flag); the hiding rule and the 2-vector fill are not pinned |
| O3 | Lookup order for Instancer (particle settings, instance attribute, instancer object, then Object mode) and Object (object, then its data), zeros when missing | `test_object_colour_pass_index_and_object_attributes`, `test_instancer_attributes_follow_each_instance` |
| O4 | Instance numbering in `persistent_id`: component count entry, `id` attribute or index; an instanced object continues the walk with no entry of its own; collection member k takes entry 2k (own geometry) or 2k + 1 (its instances) | `test_instancer_attributes_follow_each_instance` (Scatter: index; Spread: `id`; Pebbles: object instances); `test_blend_instance_layers.py` (objects and collections inside instances; basis: experiments E3, E4, see 5.4) |
| O5 | Instance attribute conversion table of 5.3, innermost layer first | `test_instancer_attributes_follow_each_instance` (FLOAT_COLOR only) |
| O6 | Which component of the root's own kind is not counted at the outermost level (and at an instanced object's level) | `test_blend_instance_layers.py` (basis: experiment E2, see 5.4); `GREASEPENCIL` not confirmed |
| O7 | Only the 4 innermost instancing layers are searched | `test_blend_instance_layers.py` (basis: experiment E1, see 5.4) |
| O8 | The Instancer fallback object is the generating scene object, also through object and collection references | `test_blend_instance_layers.py` (basis: experiment E3, see 5.4) |

## 7. Reference tests

These tests judge the replacement. **They must stay as they are.** All are pytest cases under
`tests/pytest_cases/`; marker `pure` runs without Blender, marker `blender` needs Blender as a Python
module (`bpy`).

- `test_blend_object_values.py` (9 cases). It builds a .blend with Blender 5.3 through `bpy`, exports it
  with `blend_export.evaluate_many` and compares with Blender's own numbers: the depsgraph's per-instance
  `random_id`, and a Cycles render of Object Info > Random for two real objects (one with a non-ASCII
  name). Cases: `test_ported_hashes_reference_values` (pure; fixed numbers, for example
  `object_random_id("Plain") == 3921286968`; `dupli_random_id("A", [3], "R") == H2(S("A"), 3) XOR
  H2(S("R"), 0)`), random ids of collection members and of deep instances, ray visibility/holdout,
  object colour, pass index and Object attributes (custom property `tint`, RNA path `color`, integer
  `pass_index` -> (7, 7, 7, 1), boolean `flag` -> (1, 1, 1, 1), missing -> zeros), and Instancer
  attributes per instance (a `FLOAT_COLOR` instance attribute equal to the instance position, at
  `atol = 1e-5`).
- Also failing while the stubs are in place (they export scenes whose instances go through
  `dupli_random_id` or `attributes`; they judge the replacement too): `test_blend_hair.py` (8 cases),
  `test_blend_pointcloud.py` (4), `test_blend_sync_parity.py`
  (`test_point_clouds_and_hair_are_carried_without_warnings`), `test_blend_convert_export.py`
  (`test_render_requests_are_unchanged`).
- At `a777b6c` all cases of these files pass on Linux (measured 21 November 2026, Python 3.13, `bpy` 5.3.0
  from PyPI): object values 9, hair 13, point cloud 9, sync parity 19, convert export 8. With the stubs:
  6, 8, 4, 1 and 1 of them fail, as intended.
- Added with the rewrite (pins the behaviour of 5.2 established in 5.4; `blender` marker):
  `test_blend_instance_layers.py` (4 cases: four innermost layers, own components shifting the numbering,
  instanced objects and collections, property conversion rules). With it the Linux command below runs
  62 cases.

How to run on Linux (no Houdini needed):

```
uv venv -p 3.13 ~/bpyenv/v313
uv pip install -p ~/bpyenv/v313/bin/python "bpy==5.3.*" numpy pytest opencolorio
cd <source tree>
~/bpyenv/v313/bin/python -m pytest -c tests/pytest.ini -p no:cacheprovider \
  tests/pytest_cases/test_blend_object_values.py tests/pytest_cases/test_blend_hair.py \
  tests/pytest_cases/test_blend_pointcloud.py tests/pytest_cases/test_blend_sync_parity.py \
  tests/pytest_cases/test_blend_convert_export.py tests/pytest_cases/test_blend_instance_layers.py
```

(`opencolorio` is needed because the exporter's material path imports `PyOpenColorIO`; without it the
export fails and the cases report a missing `instances` key.) The pure case alone runs with any Python 3
that has numpy and pytest: add `-m pure`.

Windows: the pytest suite runs inside the test tiers of `scripts/run-tests.ps1` through `tests/pytest_contracts.py` (`hython -m pytest` with the test
wheelhouse), and the tiers select only the markers `pure` (fast, extended) or `pure or hython` (nightly)
(`scripts/native_test_plan.py` line 91). So on Windows only `test_ported_hashes_reference_values` runs, for
example in the fast tier of a build of the commit to test:

```powershell
./scripts/run-tests.ps1 -Tier fast -BuildDirectory <build> -Artifacts <folder>
```

The `blender`-marked cases are not run by any Windows tier; use the Linux route above.

## 8. Provenance

- The previous implementation (`dupli_random_id`, `_rgba`, `property_rgba`, `_ObjectValues.attributes`,
  `_level`, `_instance_attribute`, `_column` and the type table `_INSTANCE_ATTRIBUTE_TYPES` in
  `blend_export.py`, commit `a777b6c` and earlier) followed Blender's GPL-2.0-or-later
  `source/blender/blenkernel/intern/object_dupli.cc` (v5.3.0) closely: same formula step by step, same
  conversion rules and lookup order, same instance numbering.
- It was removed in commit `e9fb7c281f35ef2043eef14d4f4d810bb5abd0b7` (title "Remove Blender-derived curve, hue
  and object-value code pending independent rewrite"). The hash is that of the private development branch; in a
  repository with new history it names the commit that removed the code.
- Commit hashes in this section are those of the private branch that integrated the removal and the rewrite on top of the main line (the side branches named here had other hashes for the same commits): removal `e9fb7c2`, base of the rewrite `a2708bc`, replacement `58b4f67`, `e066543`, `2a846c9`, `a2f0720`, and this implementer text `516c4ec`.
- This description was written by an author who had read both the previous implementation and
  Blender's source, in order to state the behaviour completely. It contains no Blender code and no Blender
  internal names, but its author was not isolated from that code. This is not a clean-room process in the
  legal sense; provenance and code review still matter.
- The replacement was written from this description by an implementer (21 November 2026), on a separate
  branch from the base commit `a2708bc`.
  - Used: this document; the reference tests and the rest of this project's code at `a2708bc` (the kept
    hash functions are called, not changed); Blender's Python API documentation and user manual pages
    listed at the top; NumPy's documentation (`numpy.unique` with `return_index`, `numpy.searchsorted`);
    and experiments with Blender 5.3 itself (the `bpy` 5.3.0 module from PyPI on Linux: Cycles renders of
    Attribute nodes and the depsgraph's public `object_instances` data).
  - Not looked at: Blender's source outside `intern/cycles` (not opened at all), and the removed
    implementation (no `git show`/`log -p`/`diff`/`blame` of the old bodies, no reading of the main
    checkout's or other working copies of `blend_export.py`). Nothing was seen by accident.
  - Method: the random number is the composition of section 2 written as a fold over the path entries.
    Property conversion is a type test followed by a zero-filled RGBA (reading at most five items of an
    iterable). The instance-attribute lookup is the implementer's own structure: a small `_InstanceLevel` class per
    instancing level (instances point cloud, component count, a sorted table of `id` values with the first
    index of each, built with `numpy.unique(return_index=True)` and searched with binary search,
    `numpy.searchsorted`; Knuth, TAOCP vol. 3, 6.2.1), converted attribute columns cached per level and
    name, levels cached per evaluated object in `self.levels` (`self.columns` is not used); a
    `_CollectionLevel` for instanced collections; the walk of 5.2 as a loop over the path entries from
    the outermost.
  - Where the implementation goes beyond or differs from sections 5.2-5.4 (all settled by experiment,
    recorded in 5.5; pinned by `tests/pytest_cases/test_blend_instance_layers.py`):
    1. Layer limit: only the 4 innermost recorded layers are searched (E1: six nested layers, Blender
       found the attributes of layers 1-4 and rendered 0 for layers 5 and 6; same with five layers).
    2. Component counting (5.1/5.2) confirmed as written for roots of type MESH, CURVES, CURVE, FONT,
       POINTCLOUD and VOLUME and for levels inside the instances (E2, 11 scenes, with two negative
       controls that fail as expected). Note: a mesh object's evaluated geometry has a `mesh` component
       even when its node tree outputs instances only, so the own-kind rule matters. GREASEPENCIL could
       not be tested (creating it aborts the `bpy` module on the headless VM); it follows the document.
    3. Object references: unlike 5.2 ("an object ... reference ends the walk"), Blender continues into
       the instanced object's own evaluated geometry, numbered like a root (its own kind not counted),
       with no path entry of its own; its layers are searched innermost first like the others (E3, also
       with an extra point cloud in the instanced object). The Instancer fallback object stays the scene
       object that generated everything (`root_evaluated`), confirmed by a custom property on both.
    4. Collection references (not in the document): member k of an instanced collection (in
       `collection.objects` order) takes path entry 2k for its own geometry and 2k + 1 for the instances
       its geometry nodes make; the walk continues into that member as in 3 (E3 variant `coll`, E4 with
       three members). Collections with child collections are not followed (order not measured).
    5. Property rules pinned by the new test against Blender renders: a 2-vector gives (x, y, 0, 1); a
       string custom property `color` hides the built-in colour (renders 0); a boolean array is not
       converted; a mesh custom property is found when the object has none.
  - Still unconfirmed: GREASEPENCIL roots; whether the 4-layer limit counts the intermediate level that
    Object Info / Collection Info "As Instance" creates and levels across an object boundary (E1 had
    attributes on every level and no object boundary); collections with child collections; duplicate
    `id` values (the document's rule is kept: the first match).
- Review against Blender's source for copied structure or expression: a separate reviewer, 21 November
  2026, compared with Blender's `source/blender/blenkernel/intern/object_dupli.cc` and
  `source/blender/blenlib/BLI_hash_c.hh` at `v5.3.0` and with the removed implementation. Grade: no
  concern. Details: `docs/dev/provenance/review-2026-11-21.md`, section 6. Its follow-up (a verbatim
  quotation of the manual in section 3) was applied in a follow-up change on 21 November 2026 (commit
  `019e18a`, which also adds the experiment record of section 5.5).

## Performance (21 November 2026)

Export time (`evaluate_many`, whole export of one selection) of the previous file (commit `03c6abf`, loaded as
a black box in place of this one) against the rewrite, inside Blender 5.3.0 on Windows (Ryzen 9 7950X), one
Blender process per run, 5 alternating runs each, median. Scenes: geometry-nodes Instance on Points of a cube
or of a collection, 1 or 3 nested layers, with and without a FLOAT_COLOR instance attribute and Attribute
nodes (Instancer and Object), object and collection selection. 1,000 instances: 0.92-1.05 of the previous
time (all 16 cases, within spread). 100,000 instances: with attributes 0.74-1.02 (the time inside `attributes`
itself 0.64-1.13; the 1.13 case, collection instances in 3 layers, has the same total, 1.01), without attributes 0.99-1.09 (the 1.09 case has a 25 % spread in the
previous file's own runs). The exported instances, random numbers and attribute values were identical
(sha-256 of all per-instance values) in every case measured. 500,000 instances (object selection, 3 runs each): cube instances with
attributes 1 layer 0.85 (26.7 -> 22.5 s), 3 layers 0.80 (27.7 -> 22.2 s); without attributes 1.00 (13.3 s);
collection instances in 3 layers with attributes 0.98 (29.5 -> 29.1 s; one of the rewrite's runs took 43.8 s,
an outlier the other two runs and the 100,000 case do not show). A trial with per-export caching of the instancer
object's property and Python-float rows for the instance attribute columns was 5 % slower on Linux and was
dropped. The remaining time is per-instance access to Blender data in the export loop that calls these
functions; making it smaller needs bulk reads in that loop, which is outside this piece.
