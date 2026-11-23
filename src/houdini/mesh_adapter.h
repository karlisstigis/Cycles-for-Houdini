// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "scene_adapter.h"
class GU_Detail;
namespace cycles_houdini {
struct NativeNamedAttributeCache;
struct NativePointIdentities {
  std::vector<std::string> keys;
  bool index_fallback{};
};
struct NativeCurveIdentities {
  NativePointIdentities curves, keys;
};
struct NativeMeshFace {
  int primitive{}, polygon{}, first_corner{}, corner_count{}, first_triangle{}, triangle_count{};
};
struct NativeMeshTopology {
  std::vector<int> source_points, corner_points;
  std::vector<std::int64_t> corner_vertices;
  std::vector<NativeMeshFace> faces;
  std::vector<Triangle> triangle_corners;
};
struct NativeMeshCaptureCache {
  std::int64_t position_id{-1};
  bool full_detail{}; // Only complete homogeneous domains can seed another binding's capture.
  std::vector<std::int64_t> topology_ids, normal_ids, uv_ids, color_ids;
  std::shared_ptr<const NativeMeshTopology> topology;
  std::shared_ptr<const MeshData> data;
  std::shared_ptr<const NativeNamedAttributeCache> named;
  SubdivisionSettings subdivision;
  std::optional<SourceDiagnostic> normal_diagnostic;
  std::optional<SourceDiagnostic> uv_diagnostic, color_diagnostic;
  std::optional<bool> closed_control_mesh;
  std::string subdivision_topology_error;
};
struct NativeMeshCaptureMetrics {
  std::uint64_t topology_builds{}, topology_reuses{}, topology_scans{}, triangulated_faces{}, retained_faces{};
  std::uint64_t named_fields_captured{}, named_fields_reused{}, named_values_read{}, named_values_reused{};
};
std::uint64_t mesh_capture_cache_bytes(const NativeMeshCaptureCache &cache, bool include_topology = true);
MeshData capture_mesh(const GU_Detail &source, const GeometryRequest &request, std::uint64_t revision,
                      std::vector<Float3> *velocities = nullptr, NativePointIdentities *identities = nullptr,
                      bool shading_attributes = true,
                      std::vector<NativeMaterialReference> *material_references = nullptr,
                      const NativeGeometrySelection *selection = nullptr,
                      std::shared_ptr<NativeMeshCaptureCache> *cache = nullptr,
                      NativeMeshCaptureMetrics *metrics = nullptr, std::vector<SourceDiagnostic> *diagnostics = nullptr,
                      std::vector<Float3> *accelerations = nullptr);
} // namespace cycles_houdini
