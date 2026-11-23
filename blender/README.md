# Blender component (GPL-3.0-or-later)

This folder holds the shipped Cycles for Houdini Python component that runs **inside Blender**.
It uses Blender's Python API (`bpy`, `bmesh`, `mathutils`) and is licensed under the
[GNU General Public License version 3 or later](LICENSE). The six source-only Blender fixture
generators listed in [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) use the same licence.
The Houdini integration that starts this component uses the [Apache License 2.0](../LICENSE).

## What runs here

| File | What it does |
|---|---|
| `worker_main.py` | Started by Houdini as `blender --background --factory-startup --disable-autoexec --python worker_main.py -- --worker`. Runs the worker server (`cycles_blender/worker_server.py`): a long-lived Blender process that opens a `.blend` file and answers requests from Houdini. |
| `asset_prefs_main.py` | Started by Houdini (`blender --background --disable-autoexec --python asset_prefs_main.py`) to print the asset libraries set in the user's Blender preferences. |
| `export_main.py` | Optional one-shot export of one request to a bundle folder, for use by hand: `blender --background asset.blend --python export_main.py -- --request request.json --output bundle`. |
| `cycles_blender/` | The code behind them: geometry and instance export (`blend_export.py`), materials (`blend_material_export.py`), worlds (`blend_world_export.py`), asset scan and previews (`blend_asset_scan.py`), file listing (`blend_list_contents.py`), subdivision rules (`blend_subdivision.py`), viewport hull previews (`blend_viewport_hulls.py`) and the worker server (`worker_server.py`). |

## How the two sides talk

Houdini never imports anything from this folder. The two sides exchange only:

- **command-line arguments and environment variables** when Houdini starts Blender (the script
  path, `--worker`, a random pipe key in `CH_BLEND_PIPE_KEY`, a marker in `CH_ASSET_PREFS_MARKER`);
- **pipe messages**: framed JSON headers and raw numeric arrays over the process's standard input
  and output (no pickle). The message format is defined once, on the Houdini side, in
  `houdini/python3.13libs/cycles_houdini/blender/blend_worker.py`;
- **files**: exported textures, asset previews and material/world records written to folders that
  Houdini names in its requests, and the JSON/NumPy bundle of `export_main.py`.

The scripts here import a few small Apache-2.0 modules of the Houdini package that need no Houdini
and no Blender (Apache-2.0 code may be used in a GPL program): the pipe protocol
(`cycles_houdini.blender.blend_worker`), the shared names (`blend_names`), the object id hashes
(`blend_ids`), the hull budgets and validators (`blend_viewport`) and the byte-budget caches
(`blend_cache`). The entry scripts put this folder and the package's `python3.13libs` on Python's
path: `<host root>/python3.13libs` in a built or installed host (this folder is staged to
`<host root>/blender`), `houdini/python3.13libs` in a source checkout.

The rule is enforced by `tests/pytest_cases/test_blender_component_boundary.py`.
