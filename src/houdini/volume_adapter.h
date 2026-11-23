// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <cstdint>
#include <memory>
#include <string>
#include <vector>
#include <utility>
class GU_Detail;
class GA_Primitive;
namespace cycles_houdini {
struct GeometryRequest;
struct NativeGeometrySelection;
struct NativeMotionWindow;
struct VolumeData;
struct PreparedVolume;
struct VolumeConversionCache;
std::shared_ptr<VolumeConversionCache> make_volume_conversion_cache();
struct SourceDiagnostic;
std::string native_volume_name(const GU_Detail &source, const GA_Primitive &primitive);
// Host-only: synchronizes Houdini's grid transforms/OpenCL writes, copies
// metadata and value-only affine/taper maps, and retains source read handles
// so supported edits use copy-on-write.
std::shared_ptr<const PreparedVolume> prepare_volume(const GU_Detail &source, const GeometryRequest &request,
                                                     const NativeGeometrySelection &selection, const std::string &key,
                                                     std::uint64_t revision, const NativeMotionWindow &window);
const std::vector<std::int64_t> &volume_capture_identifiers(const PreparedVolume &prepared);
std::size_t dense_volume_fields(const PreparedVolume &prepared);
// Off-thread accounting. Retained read handles keep pointer identities valid
// and force host writes to make their voxel arrays unique, including cache hits.
std::vector<std::pair<std::uintptr_t, std::uint64_t>> volume_source_allocations(const PreparedVolume &prepared);
// No GU/OP access. Retains immutable grids, converts frozen dense fields, and
// normalizes sparse fields. Nonlinear native maps use bounded oriented affine
// resampling through their frozen SDK map and typed trilinear sampler. Safe off
// the host thread; affine compressed tiles retain their existing fast path.
// With diagnostics, known unusable source fields recover locally. A null result
// means there is no usable density; callers must retire this component's objects.
std::shared_ptr<const VolumeData> convert_volume(const PreparedVolume &prepared, std::uint64_t revision = 0,
                                                 std::string *motion_warning = nullptr,
                                                 std::vector<SourceDiagnostic> *diagnostics = nullptr,
                                                 VolumeConversionCache *cache = nullptr,
                                                 std::uint64_t *dense_conversions = nullptr);
} // namespace cycles_houdini
