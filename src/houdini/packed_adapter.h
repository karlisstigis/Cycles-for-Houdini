// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "preserved_detail.h"
#include "material_assignment.h"
#include "cycles_houdini/session.h"
#include <cstdint>
#include <map>
#include <memory>
#include <optional>
#include <string>
#include <vector>
#include <cstddef>
namespace cycles_houdini {
struct GeometryRequest;
struct NativeGeometrySelection;
struct NativePreparationBudget;
// One acquisition transaction retains each original prototype once. Keeping
// its source identity preserves cross-instance cache/sharing semantics.
class NativePackedSources {
public:
  std::shared_ptr<const PreservedDetail> retain(const GU_ConstDetailHandle &handle);
  std::shared_ptr<const PreservedDetail> retain(std::shared_ptr<const PreservedDetail> detail);
  const std::shared_ptr<const PreservedDetail> &preserved(std::int64_t identity) const { return details_.at(identity); }
  std::size_t size() const { return details_.size(); }

private:
  std::map<std::int64_t, std::shared_ptr<const PreservedDetail>> details_;
};
struct NativePackedInstance {
  GA_Offset primitive_offset{GA_INVALID_OFFSET};
  std::shared_ptr<const PreservedDetail> prototype;
  std::string blender_reference;
  double blender_frame{};
  // A known source failure is distinct from a missing acquisition ledger entry.
  // Allocate warning strings only for failed instances, not every packed copy.
  std::unique_ptr<SourceDiagnostic> unavailable;
  std::unique_ptr<SourceDiagnostic> invalid_velocity;
  std::optional<Float3> velocity;
  // Velocity mode only: point w (radians per second) and the point position,
  // the pivot w rotates the instance about (scene_adapter.cpp packed_velocity_samples).
  std::optional<Float3> angular_velocity;
  Float3 pivot{};
  // Acceleration mode only: point accel (units per second squared).
  std::optional<Float3> acceleration;
  std::string identity;
  NativeMaterialAssignment material_assignment;
  Matrix transform{identity_matrix};
  bool index_identity{};
  bool ambiguous_identity{};
  bool stable_memory_source{}; // Known in-memory implementations; external procedurals need reacquisition.
  int point_prototype{-1}; // Index into the request's point-instancing prototypes; -1 for ordinary packed primitives.
};
// An explicit null entry records an unavailable source. An absent key is still
// an internal acquisition/ownership error, never a recoverable source problem.
using NativePackedDetails = std::map<std::pair<std::int64_t, GA_Offset>, std::shared_ptr<const PreservedDetail>>;
struct NativePackedInstances {
  std::vector<NativePackedInstance> values;
  std::map<std::int64_t, std::shared_ptr<const PreservedDetail>> prototypes;
  std::map<std::string, std::size_t> named;
};
// Host-only acquisition with immutable completed tables. A continuation owns
// numeric positions and values, never GA handles or native callbacks. Frozen
// ledgers have shared ownership; a missing entry remains an internal error.
class NativePackedPreparationCache {
public:
  NativePackedPreparationCache();
  ~NativePackedPreparationCache();
  std::shared_ptr<const NativePackedInstances>
  prepare(const GU_Detail &source, const GeometryRequest &request, const NativeGeometrySelection &selection,
          NativePackedSources &sources, const std::shared_ptr<const NativePackedDetails> &frozen = {},
          bool capture_velocity = false, NativePreparationBudget *budget = nullptr, bool build_named_index = true);
  void clear();

private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};
} // namespace cycles_houdini
