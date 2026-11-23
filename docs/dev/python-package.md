# Python package ownership

`houdini/python3.13libs/cycles_houdini` is an ordinary Python package. Its
implementations use package-relative imports and keep their real `__file__`,
`__package__` and import specification. Importing the public API or the pure
contracts does not import Houdini or initialize the native renderer.

| Namespace | Owner |
| --- | --- |
| `cycles_houdini` | Public session API and the lazy native runtime |
| `core` | Execution policy, device selection, SDK validation and feature controls |
| `blender` | Evaluation, export, transfer and cache ownership |
| `scene` | Houdini scene capture, properties, lights and instancing |
| `materials` | Material authoring and graph extraction, with explicit public exports |
| `ipr` | Session registry, worker controller, scheduling and display queues |
| `viewer` | Qt Viewer and its display, layout and interaction helpers |
| `rop` | SOHO, ordered final output and background jobs |

The root API and material authoring API declare `__all__` explicitly. The
historical `core.api` and `materials.graph` paths alias those package objects,
including assignments to mutable state such as the root `_native` runtime.

The flat paths listed in `_module_aliases.json` remain small forwarding
entrypoints for saved HIP callbacks, HDAs, SOHO scripts, Python scripts and
pickles. Loading a legacy path imports its owning implementation and registers
that same object under the old name. Both import orders share function globals,
classes, caches and monkeypatches. New callers use the owning namespaces. The
`materials.py` file entrypoint uses the standard package loader for Houdini
importers that prefer the historical file over the package directory. There are
no source-execution compatibility loaders or rewritten implementation paths.

The scripts that run inside Blender are not in this package: they are the Blender
component in the top-level `blender/` folder (GPL-3.0-or-later, package
`cycles_blender`, staged to `<host>/blender`, outside Houdini's Python path; see
`blender/README.md`). Blender receives an explicit script, `blender/worker_main.py`
for the pipe server or `blender/export_main.py` for a bundle export. Those scripts
put their own folder and the package's `python3.13libs` (`<host>/python3.13libs`,
or `houdini/python3.13libs` in a source checkout) on Blender's import path. The
Blender side imports only the shared Apache-2.0 modules that need neither Houdini
nor Blender: `blender.blend_worker` (pipe protocol), `blender.blend_viewport` (hull
budgets and validators), `blender.blend_names`, `blender.blend_ids` and
`blender.blend_cache`. Nothing in this package imports `cycles_blender`;
`tests/pytest_cases/test_blender_component_boundary.py` enforces it. Installed icon, notice and background-host checks resolve from
the canonical files' actual directory depth.

Host-fake tests use `tests/support/package_loader.py`: injected dependencies are
placed in their owning namespaces and the loader returns the registered
canonical module. AST contracts use `product_source` from `support.paths`.
This keeps test patching and pickle identity aligned with normal imports rather
than executing flat shims into private test globals.

## Deferred imports

The migration counted each AST import statement inside a function once,
including nested functions, across the 79 alias-table entries. The public root
and materials package implementations stand in for their `api` and `graph`
aliases.

| Owner | Before | After |
| --- | ---: | ---: |
| Core and public API | 21 | 22 |
| Blender | 24 | 26 |
| Scene | 75 | 77 |
| Materials | 46 | 47 |
| IPR | 18 | 18 |
| Viewer | 64 | 66 |
| ROP | 33 | 34 |
| Total | 281 | 290 |

Seven additional statements split imports that previously mixed owners; two
explicitly import the Blender exporter and CLI parser. This is not additional
startup work. Imports of Houdini, Blender, Qt, OCIO, OpenImageIO and the native
runtime remain at their existing feature boundaries. Cross-owner imports inside
callbacks also defer UI construction, native queries and circular session/scene
dependencies. The migration preserves those boundaries rather than moving
optional host dependencies into package startup.

Fresh-process contracts cover every alias in both orders, canonical source and
resource paths, shared state, public function/class pickle compatibility,
historical callback strings, the isolated test loader and native laziness.
Release qualification additionally exercises actual saved HIPs, material
callbacks and Viewer startup in Houdini. The scoped Blender entrypoint check
uses an animated Shape object and collection with two CPU threads; its source,
process logs and report are retained in the stage 3a evidence directory.
