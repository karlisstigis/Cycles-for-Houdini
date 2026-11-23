// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <pybind11/pybind11.h>
#include <cstddef>
#include <cstdint>
#include <utility>
#include <vector>
#include "cycles_houdini/session.h"

namespace cycles_houdini {
// Import Blender's evaluated array record directly into owned engine storage.
// No borrowed Python buffer or host geometry survives this call. The record is
// the blend_export schema, and arrays maps its archive keys to buffer exporters.
std::shared_ptr<const MeshData> capture_blend_mesh(pybind11::handle record, pybind11::handle arrays,
                                                   const std::vector<std::string> &materials, std::uint64_t revision,
                                                   std::shared_ptr<const MeshData> previous = nullptr,
                                                   bool reuse_layout = false);
// Link hair (blend_export hair records): a MeshData carrier whose external_curves holds the
// curves, so the external-geometry path (bindings, packed/direct instancing) carries them.
std::shared_ptr<const MeshData> capture_blend_curves(pybind11::handle record, pybind11::handle arrays,
                                                     const std::vector<std::string> &materials, std::uint64_t revision);
// Link point clouds (blend_export point records): the same carrier, external_points.
std::shared_ptr<const MeshData> capture_blend_points(pybind11::handle record, pybind11::handle arrays,
                                                     const std::vector<std::string> &materials, std::uint64_t revision);
std::size_t blend_mesh_nbytes(const MeshData &mesh);
std::vector<double> blend_mesh_bounds(const MeshData &mesh);
std::vector<std::pair<std::uintptr_t, std::size_t>> blend_mesh_storage(const MeshData &mesh);
pybind11::capsule blend_mesh_capsule(std::shared_ptr<const MeshData> mesh);
std::shared_ptr<const MeshData> blend_mesh_from_capsule(pybind11::handle capsule);
} // namespace cycles_houdini
