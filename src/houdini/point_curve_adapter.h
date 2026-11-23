// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "mesh_adapter.h"
#include <atomic>
class GU_Detail;
namespace cycles_houdini {
std::vector<std::int64_t> point_curve_identifiers(const GU_Detail &source, bool rational_curves = false);
PointData capture_points(const GU_Detail &source, const GeometryRequest &request, std::uint64_t revision,
                         std::uint64_t &index_ids, std::vector<Float3> *velocities = nullptr,
                         NativePointIdentities *identities = nullptr, bool shading_attributes = true,
                         std::vector<NativeMaterialReference> *materials = nullptr,
                         const NativeGeometrySelection *selection = nullptr,
                         std::vector<SourceDiagnostic> *diagnostics = nullptr,
                         std::vector<Float3> *accelerations = nullptr);
CurveData capture_curves(const GU_Detail &source, const GeometryRequest &request, std::uint64_t revision,
                         std::uint64_t &index_ids, std::vector<Float3> *velocities = nullptr,
                         NativeCurveIdentities *identities = nullptr, bool shading_attributes = true,
                         std::vector<NativeMaterialReference> *materials = nullptr,
                         const NativeGeometrySelection *selection = nullptr,
                         std::vector<SourceDiagnostic> *diagnostics = nullptr,
                         std::vector<Float3> *accelerations = nullptr);
// Which curve converter ran; tests check that Bezier/NURBS and other unusual
// input take the serial one.
struct CurveConversionCounters {
  std::atomic<std::uint64_t> serial{}, parallel{};
};
extern CurveConversionCounters curve_conversion_counters;
} // namespace cycles_houdini
