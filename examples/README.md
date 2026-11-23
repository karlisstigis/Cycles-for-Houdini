# Examples

## `sop-fx-courtyard`: an editable native SOP scene

A courtyard built entirely from ordinary Houdini SOP networks (Copy to Points, wrangles, normals, packed
primitives, one small Python SOP), with ten Cycles materials, a Cycles light and a Cycles ROP
`/out/cycles_sop_courtyard`. It shows that Cycles renders a native OBJ/SOP scene directly, without exporting it
first, and that the scene stays editable: the procedural controls on the SOP nodes change the render.

| Script | What it does |
| --- | --- |
| `build_scene.py` | Builds the scene in a fresh Houdini session and saves it as `sop-fx-courtyard-current.hiplc`, together with an example colour configuration `showcase.ocio` and a summary `scene.json`. With `--render` it also renders one frame (`--width`, `--height`, `--samples`, `--device`) and writes an EXR, a PNG preview and `render-cpu.json` or `render-optix.json`. |
| `native_geometry.py` | The SOP networks themselves; `build_scene.py` calls it. Python only creates the nodes, Houdini does the cooking. |
| `adaptive_scene.py` | Makes a copy, `sop-fx-courtyard-adaptive.hiplc`, in which the turbine and the ground are separate objects: the turbine uses the renderer's adaptive subdivision, the ground a noise displacement. It reads `sop-fx-courtyard-current.hiplc` (or the scene given with `--source`) and never changes it. |
| `open_scene.py` | Startup script for a new Houdini window: opens a scene and starts the **Cycles Render** panel on its ROP. `scripts/launch-sop-showcase.ps1` passes it to Houdini. |

All of them write to `artifacts/examples/sop-fx-courtyard/` in the project folder, or to
`<CYCLES_HOUDINI_TEST_ARTIFACTS>/examples/sop-fx-courtyard/` when that variable is set.

### Running it

You need a development build (see [building from source](../docs/dev/building-from-source.md)). In PowerShell 7,
from the project folder:

```powershell
.\scripts\run-hython.ps1 -HythonArguments @('examples/sop-fx-courtyard/build_scene.py')
.\scripts\run-hython.ps1 -HythonArguments @('examples/sop-fx-courtyard/adaptive_scene.py')
.\scripts\launch-sop-showcase.ps1
```

The first two commands make the scenes; the third opens `sop-fx-courtyard-adaptive.hiplc` in a separate Houdini
with its own preferences and the render panel started (`-SceneFile` opens another scene). Building the scene again
overwrites `sop-fx-courtyard-current.hiplc`, so keep a copy of any changes you want to keep. To build and render
in one step:

```powershell
.\scripts\run-hython.ps1 -HythonArguments @('examples/sop-fx-courtyard/build_scene.py', '--render', '--width', '1280', '--height', '720', '--samples', '256')
```

More about the scene and what to try in it: [Try Cycles in native Houdini SOPs](../docs/user/try-native-sops.md).
