# Python boundaries

Implementations live in `core`, `scene`, `ipr`, `viewer`, `materials`, `blender`
and `rop`. Importing `cycles_houdini` does not load the native DLL. Call `load()`
to validate and initialize it explicitly, or let the first native API call do
so. A failed load is retryable after the host environment is repaired. Pure
contracts such as execution policy, caches, override syntax and job state can
be imported with ordinary Python and no Houdini environment.

The existing flat modules are compatibility entrypoints. Normal imports load
the grouped implementation exactly once; `_compat.publish` registers that
object under its old name after a successful import. Failed optional host imports
leave no partially initialized legacy alias behind. No import hook or copied
module dictionary is involved. `__package__` and `__file__` retain their original
anchors so relative imports, serialized callbacks, source-file Blender launches,
viewer icons and background host-root checks retain their meaning. The two
package APIs (`__init__` and `materials`) execute their private implementation
once; `core.api` and `materials.graph` are explicit aliases of those APIs.
Isolated fake-host tests execute through the same flat script paths.
`_module_aliases.json` documents the supported paths. The scripts that run inside
Blender are not in this package: they are the Blender component in the top-level
`blender/` folder (GPL-3.0-or-later, staged to `<host>/blender`), which this
package never imports; Blender launches start its entry scripts by path.

Keep Houdini extraction and HOM operations on the host thread. The queued
controller owns worker transport and immutable captured data; it must not call
HOM. Qt widgets and polling timers belong to the GUI thread. Background display
work receives retained native image handles rather than a live Houdini scene.
Native lazy initialization is serialized independently of session transport;
it does not move initialization or scene capture to another thread.
