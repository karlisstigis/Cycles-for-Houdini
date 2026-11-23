# Saved-scene protection and recovery

New protected HIP files use Houdini's native reduced operator definitions for
compiled Cycles shader nodes. An ordinary save records the complete class
parameter layout and the exact socket order. When Cycles is unavailable,
Houdini loads these definitions as placeholders and retains the native nodes,
channels, parameter templates, connections and children. When the package is
available again, Houdini selects the actual compiled operators automatically.
The integration does not rename material roots or replace artist-authored nodes.

Dynamic group interfaces use Houdini's built-in `subnet` and `subnetconnector`
operators. Their parameter-generation behavior remains available without the
Cycles DSO; they are not approximated by static shader placeholders. Creation
helpers retain the Cycles language, node identity and authoring presentation.
Static shader output order is preserved because HIP links retain upstream
output indices. Sorting or merging output names would silently change wiring.

The runtime registers `cycles_houdini_offline` from `pythonrc.py` in UI and
headless Houdini. Registration and serialization run during an ordinary HIP
save. A scene needs neither a Cycles ROP nor the Viewer to receive protection.
Camera navigation and object edits do not serialize graphs. Native registration
accepts only operators actually owned by this runtime, not arbitrary types
whose names start with `cycles::`. The native definition cache resets on scene
load and clear. No live HDA is installed over the compiled operator.

This applies to files saved with the current implementation. It cannot restore
shader data discarded by an earlier save without the necessary definitions.
The user has chosen forward correctness rather than old-file migration.

## Independent archive

Each top-level Cycles surface, volume, light and world builder is also serialized
with Houdini's native `saveItemsToFile`. These files, UUIDs, paths, graph wires
and SHA-256 hashes are stored in the binary `cycles.offline.graphs` data block
on the built-in `/obj` node. This archive is an independent recovery aid.
It survives saves without the integration; native reduced definitions provide
the normal automatic save/reload path.

Secondary archive refresh uses the supported `saveItemsToFile` writer and
compares each healthy root's complete native bytes. Identical bytes and unchanged root path/type reuse its prior
payload and graph metadata. If every root is unchanged, the saved data block
is not rewritten or recompressed. The reader reuses validated archive contents
only while the complete binary data block matches exactly; a merge, fragment
import or external block replacement therefore cannot silently reuse stale
contents. Scene load and clear also discard that cache. Native reduced operator
definitions are still republished on **every** save, including Save As after a
merge, independently of secondary archive reuse.

Reuse additionally requires a record/payload pair captured successfully in the
current session. The first save after load recaptures graph metadata, even if
the loaded payload happens to match. Editing or replacing archive metadata
cannot make an old record pass the session-local proof.

This deliberately retains native serialization as the change proof; it does
not infer unchanged state from evaluated parameters, timestamps or callbacks.
Native serialization covers expressions, channels, wiring, flags, layout and
other authored metadata. External references retain their native expressions;
the enclosing HIP saves the external nodes. One owned scratch directory and
file serve all roots in a save. The file is truncated before every native write,
and the directory is cleaned on success or failure. This reduces directory
churn and repeated ZIP validation/decompression while retaining full native
file serialization. It does not substitute low-level binary flags or virtual
filesystem protocols for the supported writer. There are no edit or drag
callbacks, and no archive work occurs until an ordinary save.

Licensed native streams can vary even when authored graph state is unchanged.
Those bytes are preserved verbatim: differing packets always trigger full
secondary refresh. Protection does not normalize license envelopes or infer
equivalence from a subset of parameters. The benchmark reports exact-byte hit
rate and includes saves separated by clock-second boundaries. Scratch directory
counts and serialized file counts are recorded separately from payload reuse.

Unknown formats, invalid checksums, unresolvable duplicate identities and
ambiguous loaded structural damage preserve the prior archive. Intentional deletion does not
recreate a graph. Intentional Change Type does not trigger scripted restoration.
Lost wires count as a structural change, including subnet indirect connections.
An archive is not substituted for a valid graph just because its UUID matches.

For explicit recovery of an archived graph into a separate review network:

```python
import hou
import cycles_houdini_offline as protection
records = protection.inspect()
review = hou.node('/obj').createNode('matnet', 'cycles_recovery_review')
copy = protection.recover_copy(records[0]['id'], review)
```

This leaves the original graph, material assignments and HIP untouched. Review
copies are excluded from automatic capture. If a loaded structural change was
intentional, the artist right-clicks the material and chooses **Cycles > Keep
Current Graph** (`protection.keep_current(kwargs)`, which calls
`protection.accept_current(identity)`), then saves. The item appears only on a
material whose graph waits for review, also without the Blender bridge. Native
archives may contain callbacks; use trusted HIPs.

## Copied graphs

Each protected root carries its identity in the node user data
`cycles.offline.id`. Houdini copies user data with a node, so a copy, duplicate,
Alt-drag or paste (also from another HIP, also inside a copied subnet or material
network) arrives with the original's identity. Earlier versions then refused to
refresh the archive of both graphs and repeated a conflict notice at every load.

Now a copy gets its own identity (`_separate_copies`). Rule for the original,
which keeps the identity and its archive record:

1. the node at the path of the archived record;
2. otherwise the oldest node by Houdini session id: creation order in the
   session, load order for nodes that came from the HIP.

The archived path is the stronger evidence: the record was written for the node
at that path. Session order decides only when no holder sits at that path (for
example, both were saved by an older version before the first archive, or the
original was renamed). Every other holder gets a fresh UUID and is protected as a
new graph at the next save. Paste into a scene that does not hold the identity
keeps it: identities need to be unique only within one scene.

Only healthy builder roots are separated. When a placeholder (missing or damaged
operator), a nested builder or anything other than a protected root carries the
identity, the conflict stays as before: the archive is retained and the notice
asks for review. Review copies are never counted.

When copies are separated:

- After a copy or paste in the UI. ChildCreated callbacks on `/mat`, `/obj` and
  every network holding a protected root schedule one deferred check
  (`hdefereval`), because Houdini creates the node before it loads its user data.
  The identity change is not an undo step. Pastes elsewhere are caught below.
  No global `OnLoaded.py` is installed: measured on Houdini 22.0.466, one costs
  about 0.17 ms per node on every scene load (10,000 nodes: 0.32 s to 2.0 s), and
  only the first such file on `HOUDINI_PATH` runs, so it would hide or be hidden
  by a studio's own script. Per-type event scripts do not run for these compiled
  operators.
- Whenever the safeguard inspects (`inspect()`, `accept_current()`) or saves
  (`capture()`) the scene.
- On load. Duplicates saved by earlier versions are separated with one short
  notice listing the copies. Loading writes nothing to disk; the user's next save
  keeps the result, and later loads are silent.

While copies shared an identity, the original's archive could not be refreshed,
so an original edited after the copy differs from its archive. For an original
separated this session, `inspect()` reports it healthy when the change can only
be authored: every archived node is still there with the same type, every
archived input is still wired (possibly to a new node), and nothing looks damaged
(`_authored_growth`). Loading damage removes nodes, replaces types or drops
wires; it does not add nodes or reconnect inputs to other sources. Any other
difference is reported for review as before.

## Notices

Notices are written for artists and name the node, what is wrong and what to do,
one line per problem and session. A graph waiting for review is reported at each
load (not at each save) until the artist keeps the current graph or restores it. An archived graph whose node was deleted is not reported: its
archive simply stays available. A node that was moved or renamed while its
archive could not follow is reported and can be kept with the same menu item.
`inspect()` status strings are unchanged for scripts and tests.

Saved ROP callbacks and integration-owned dynamic menus embed missing-module
guards. Missing integration modules yield one shared notice per scene and retain
saved menu values. Genuine initialization failures remain visible.

## Qualification

`tests/houdini_native_graph_roundtrip.py` runs in three isolated processes:
`prepare`, `missing`, then `reload`, with the same `--directory`. The missing
phase uses empty package directories and no integration Python path. It performs
two ordinary Save As operations, edits available parameters and wires, and never
calls recovery APIs. Native reload checks four graph domains, nested dynamic
interfaces, promoted ramps/curves, animation, external expressions, assignments,
intentional deletion/replacement and a fixed-seed assigned-material render.

`tests/houdini_offline_copies_smoke.py` (one hython process, temporary folder)
covers copy, Alt-drag (`copyItems`) and clipboard paste, paste from another
scene, copying a network with several graphs, load-time repair of duplicates
saved by earlier versions (nothing written until save, silent after one save),
a genuine structural conflict that stays reported next to a separated copy, and
Keep Current Graph followed by a silent save/reload round trip. In hython it
forces the copy watch on and runs the deferred check by hand.
`tests/houdini_offline_copies_gui_smoke.py` (GUI) checks the real deferred check
after copy, paste and network copy, and evaluates the installed node menu's
expressions and script for Keep Current Graph.

Pure contracts are in `tests/native_scene_definition_test.py`,
`tests/offline_archive_test.py` and `tests/offline_scripts_test.py`. Serialized
native tests use the project's job queue and disposable preferences. Execution
reports, including failed qualification attempts, are retained under
`artifacts/final-hardening-20261107`. A fixture's existence is not evidence of a
pass; the final hardening report records the actual qualified runtime and results.

`tests/houdini_offline_save_benchmark.py` compares ordinary save wall time with
the former full temporary-file/ZIP refresh path for unchanged graphs, one
edited root and Save As after import/merge. It checks payload stability across
a clock-second boundary, authored-state changes and explicit CPIO recovery.
The unchanged-save optimization is not a claim of zero serialization or zero
cost, and performance claims require the benchmark's actual timing report.

The isolated `.466` three-process round trip passed with 114 authored nodes:
prepare (`artifacts/final-hardening-20261107/native-graph-roundtrip06/prepare.json`),
two offline Save As operations (`artifacts/final-hardening-20261107/native-graph-roundtrip06/missing.json`)
and native reload (`artifacts/final-hardening-20261107/native-graph-roundtrip06/reload.json`).
The missing process had no integration Python module or registered Cycles type
before loading the HIP. Typed builtin interfaces and neutral compiled sockets
retained exact order and wiring. Final native shader language and assigned-target
render checks passed. Placeholder warnings from Houdini are distinct from the
guarded integration callbacks; the renderer cannot operate while absent.

The definition scan reads already-instantiated locked descendants without
forcing delayed asset contents to load. Archive writes still exclude locked
interiors. Arbitrary locked-HDA internals have not been separately qualified by
this unlocked graph fixture.
