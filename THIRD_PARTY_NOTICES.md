# Third-party notices

The Houdini integration and most project tooling use Apache-2.0 (`LICENSE`). The shipped Blender component and
six Blender fixture generators use GPL-3.0-or-later (`blender/LICENSE`, section 6). Source files carry SPDX licence
identifiers. Third-party parts keep their own licences. Sections 3 and 4 cover the GPL and CC0 artwork, and section 5
records the provenance of code rewritten from behaviour descriptions.

## 1. Components used by the build

The licence texts and notices of each component are kept in a folder per component: `packaging/notices/<folder>/` in the source (catalogue: `packaging/notices/index.json`, written by `scripts/fetch-release-notices.py`), and `notices/<folder>/` next to this file in installed packages (a text whose path there would be longer than 64 characters is stored in the same folder under a short name, 12 hex digits of its original path and its file name; `notices/index.json` lists every text with its original path). Nothing in this table is bundled as source in this repository except pybind11 headers (`include/third_party/pybind11`) and the notice texts themselves; the build fetches the rest at the pinned versions in `source.lock.json`.

| Component | Version or commit | Licence (as recorded in the catalogue) | Notices folder | Role |
| --- | --- | --- | --- | --- |
| Blender Cycles | `77edc33168528e06b35540452bce5cce6068c3aa` plus the patches in `patches/` | Apache-2.0 and bundled source notices | `cycles` | Rendering engine (Hydra delegate and native renderer) |
| Intel oneAPI DPC++ (SYCL runtime) | v7.1.0 | Apache-2.0 WITH LLVM-exception and third-party notices | `dpcpp` | Runtime dependency of the engine build |
| SPIRV-Headers (DPC++ dependency) | `9268f3057354a2cb65991ba5f38b16d81e803692` | MIT-Khronos-old | `dpcpp-spirv-headers` | Build dependency |
| Eigen | `8a1083e9bf41b91fdea6546681f806154efdc25a` | MPL-2.0 and bundled permissive notices | `eigen` | Header library |
| Embree | 4.4.1 | Apache-2.0 | `embree` | CPU ray tracing |
| emhash | `5e131ba09a5290823fe71099d9c35eb5df5345b6` | MIT | `emhash` | Header library |
| libepoxy | 1.5.10 | MIT | `epoxy` | OpenGL function loader |
| {fmt} | 12.1.0 | MIT | `fmt` | Formatting library, fetched with the pinned dependency libraries; not compiled into this project's own binaries |
| hwloc | 2.10.0 | BSD-3-Clause and bundled notices | `hwloc` | Hardware topology |
| oneAPI Level Zero loader | v1.29.0 | MIT and third-party notices | `level-zero` | Runtime dependency of the engine build. Its DLLs (`ze_loader.dll`, `ze_validation_layer.dll`) contain {fmt} 10.2.1 as bundled in spdlog 1.14.1 (MIT, with fmt's exception for code embedded in compiled programs); spdlog's licence in `level-zero/third_party/spdlog_headers/LICENSE` names it |
| OpenCL Headers | v2025.07.22 | Apache-2.0 | `opencl-headers` | Build dependency |
| OpenCL ICD Loader | v2025.07.22 | Apache-2.0 | `opencl-icd-loader` | Build dependency |
| Intel Open Path Guiding Library | 0.7.1 (from Blender's `lib-windows_x64` repository, commit pinned in `source.lock.json`) | Apache-2.0 and bundled third-party notices | `openpgl` | Path guiding |
| parallel-hashmap | `8a889d3699b3c09ade435641fb034427f3fd12b6` | Apache-2.0 and bundled notices | `parallel-hashmap` | Header library |
| pthreads-win32 | 3.0.0 | Apache-2.0 | `pthreads` | Threading |
| pugixml | 1.10 | MIT | `pugixml` | XML parser |
| pybind11 | 2.13.6 (unmodified headers in `include/third_party/pybind11`, checksums in `packaging/notices/pybind11/source.json`) | BSD-3-Clause | `pybind11` | Python bindings |
| Unified Memory Framework | v1.1.0 | Apache-2.0 WITH LLVM-exception | `unified-memory-framework` | Runtime dependency of the engine build |
| vc-intrinsics | `60cea7590bd022d95f5cf336ee765033bd114d69` | MIT | `vc-intrinsics` | Build dependency |
| zlib | 1.3.1 | Zlib | `zlib` | Compression |
| Zstandard | 1.5.7 | BSD-3-Clause OR GPL-2.0-only (used under BSD-3-Clause) | `zstd` | Compression |
| NVIDIA OptiX SDK headers | v9.1.0, commit `f1f6dd803f3159992d248178f6e09421c6eb8b6d` | NVIDIA DesignWorks agreement and per-header licences: `optix-headers/LICENSE.txt` (agreement), `optix-headers/license_info.txt` (NVIDIA's licence overview), `optix-headers/HEADER-NOTICES.txt` (per-header copyrights, terms and disclaimers) | `optix-headers` | GPU ray tracing and denoising. Downloaded from NVIDIA's public repository at build time; not part of this repository |
| NVIDIA DLSS SDK headers | v310.9.16, commit `9981c6350dc58c0ce21f024fd65e31e851c31689` | NVIDIA RTX SDKs License (`dlss-headers/LICENSE.txt`) | `dlss-headers` | Optional DLSS Ray Reconstruction in the interactive viewer. Downloaded from NVIDIA's public repository at build time; not part of this repository |
| NVIDIA CUDA compiler parts | 12.8 (compiler and headers used for building; not packaged) | NVIDIA CUDA Toolkit End User License Agreement (https://docs.nvidia.com/cuda/eula/index.html), referenced by `cuda/NOTICE.txt` | `cuda` | Make the GPU programs (CUDA and OptiX kernels) of GPU builds. Downloaded from NVIDIA at build time; not part of this repository |
| Intel Open Image Denoise | 2.3.3, supplied by the matching Houdini SDK | Apache-2.0 | `houdini-sdk-openimagedenoise` | Denoising. Headers and import library come from the Houdini installation; its DLLs are not redistributed |

Not redistributed: the Houdini installation and SDK (SideFX licence), the Microsoft Visual C++ runtime, GPU drivers, and Blender (optional; installed separately by the user for the `.blend` bridge).

The two NVIDIA header entries are marked for review by whoever distributes compiled packages (OptiX distribution terms, DLSS attribution); see `packaging/notices/index.json` (`coverage: review_required`). GPU builds also carry `notices/cuda/NOTICE.txt`: their GPU programs are made with NVIDIA's CUDA compiler; no CUDA Toolkit file is packaged.

The NVIDIA parts (OptiX headers, DLSS SDK headers, and the CUDA compiler parts used for building) are not part of this repository and are not covered by its Apache-2.0 or GPL licence: the build downloads them from NVIDIA, under NVIDIA's own licences, which the person who builds accepts. Which feature needs which part, and what applies when built files are handed out: `docs/dev/nvidia-licences.md`. NVIDIA, OptiX, DLSS, RTX and CUDA are trademarks of NVIDIA Corporation; this project is not affiliated with or endorsed by NVIDIA.

## 2. Code adapted from Blender's Cycles folder (Apache-2.0)

Blender's `intern/cycles` folder is Apache-2.0 (SPDX-FileCopyrightText: 2011-2022 Blender Foundation). Code adapted from it keeps that attribution in the adapted file:

| Adapted from (Blender v5.3.0) | In this project |
| --- | --- |
| `intern/cycles/blender/mesh.cpp` (`attr_create_pointiness`) | `src/core/mesh_pointiness.h`; island rule in `src/core/mesh_islands.h` |
| `intern/cycles/blender/util.h` (`mesh_texture_space`), `mesh.cpp` (`create_mesh`) | `src/core/generated_coordinates.h` |
| `intern/cycles/util/hash.h` (`hash_uint2`, `hash_string`) | `src/houdini/scene_adapter.cpp`, `houdini/python3.13libs/cycles_houdini/blender/blend_ids.py` |
| `intern/cycles/blender/curves.cpp`, `pointcloud.cpp`, `geometry.cpp`, `object.cpp`, `util.h`, `intern/cycles/util/color.h` | `blender/cycles_blender/blend_export.py` (Link hair, point clouds, per-object values; a GPL-3.0-or-later file of the Blender component, section 6, in which the adapted Apache-2.0 parts keep their attribution) |

No GafferCycles or legacy hdCycles code has been copied. References to those projects are design research.

## 3. Icons under the GNU GPL

Most Cycles Tools shelf icons, the asset browser icon and the Windows installer icon are Blender interface icons (unmodified or modified), licensed GPL-2.0-or-later. The per-file list, the changes made and the licence texts are in `houdini/config/Icons/CYCLES_TOOLS_NOTICE.txt`, `houdini/addons/asset_browser/config/Icons/NOTICE.txt` and `houdini/config/Icons/licenses/`.

## 4. Icons under CC0

The viewer icon set `houdini/config/Icons/cycles_viewer/`, `houdini/config/Icons/cycles_render_view.svg` and the project logo are original drawings made for this project, dedicated to the public domain under CC0 1.0 by the project's author (`houdini/config/Icons/cycles_viewer/LICENSE`). `houdini/config/Icons/cycles_remove_tabs.svg` and `houdini/config/Icons/cycles_sun_light.svg` are new drawings made for this project, released under CC0 1.0 to the extent that any rights in them exist. Details per file: `houdini/config/Icons/CYCLES_TOOLS_NOTICE.txt`.

## 5. Code rewritten from behaviour descriptions

Earlier versions of the functions below re-implemented behaviour of files in Blender's `source/blender` tree, which is licensed GPL-2.0-or-later, not Apache-2.0. Those implementations were removed and the functions were rewritten from behaviour descriptions. In their recorded accounts, the writers say they did not open Blender's source outside the Apache-2.0 `intern/cycles` folder or read the removed code. The rewritten C++ code carries Apache-2.0. The functions in `blender/cycles_blender/blend_export.py`, part of the Blender component, carry GPL-3.0-or-later (section 6).

| Project file | Functions (rewritten) | Behaviour description | Blender source the removed code had followed |
| --- | --- | --- | --- |
| `blender/cycles_blender/blend_export.py` | `dupli_random_id`, `_rgba`, `property_rgba`, the attribute lookup of `_ObjectValues` | `docs/dev/provenance/blender-object-values.md` | `source/blender/blenkernel/intern/object_dupli.cc`, v5.3.0 |
| `src/core/shader_curves.cpp` | `build_channel`, `Channel::evaluate` (curve mapping, version 1 sources) | `docs/dev/provenance/curve-mapping.md` | `source/blender/blenkernel/intern/colortools.cc` and `curve.cc`, commit `fbe6228777e7` |
| `src/core/hue_ramp.cpp` | hue interpolation (Near, Far, Clockwise, Counter-Clockwise) and the RGB to HSV conversion | `docs/dev/provenance/hue-ramp.md` | `source/blender/blenkernel/intern/colorband.cc` and `source/blender/blenlib/intern/math_color.cc`, commit `fbe6228777e7` |
| `src/core/owned_ramp.cpp` | `rgb_band` (Color Ramp in RGB mode) | `docs/dev/provenance/color-ramp-rgb.md` | `source/blender/blenkernel/intern/colorband.cc` and `key.cc`, commit `fbe6228777e7` |
| `src/core/shader_switches.cpp` | Switch, Enable Output and Index Switch with non-constant selectors | `docs/dev/provenance/shader-switches.md` | `source/blender/nodes/intern/shader_nodes_inline.cc`, v5.3.0 |

Each description's Provenance section records the commits that removed and rewrote the code, what the writer of the replacement used, and its limits: the descriptions were written by someone who had read the removed code and Blender's source, so this is not a clean room in the legal sense. A separate reviewer checked the rewritten code for copied structure or expression. The record is in `docs/dev/provenance/`.

- `docs/dev/provenance/review-2026-11-21.md`: review by a separate reviewer, who wrote neither the descriptions nor the replacements, 21 November 2026, of all five rewritten pieces against the Blender files named above and the removed code. Grades: curve mapping minor (cosmetic changes, applied; the computed values are unchanged), the other four pieces no concern. No piece was graded substantive. The reviewer is not a lawyer; the review is a technical opinion about similarity, not a legal finding.

## 6. The Blender component (GPL-3.0-or-later)

The shipped `blender/` component (installed to `host/blender/`) includes the worker server, the geometry,
material and world exporters, the asset scanner and the viewport hull builder. These scripts use Blender's
Python API (`bpy`, `bmesh`, `mathutils`). Their GPL-3.0-or-later identifiers accompany the FSF's GPL version 3
text in `blender/LICENSE`. The Houdini integration starts the component in a separate Blender process and
exchanges files, command-line arguments and pipe messages. The component uses a few Apache-2.0 modules for
the pipe protocol, shared names, object identifiers, hull budgets and caches. These shared modules run without
Houdini or Blender. `tests/pytest_cases/test_blender_component_boundary.py` checks that division.

### Blender fixture generators (GPL-3.0-or-later)

The following source-only developer utilities run inside Blender to generate test fixtures. They carry the
GPL-3.0-or-later identifier and use the same licence text in [blender/LICENSE](blender/LICENSE).

- `scripts/generate-blender-hue-ramps.py`
- `scripts/generate-blender-ramp-curve-gate.py`
- `scripts/generate-blender-rgb-ramps.py`
- `scripts/generate-blender-rgba-ramps.py`
- `scripts/generate-blender-shader-curves.py`
- `scripts/generate-legacy-ramp-tables.py`
