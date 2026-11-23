// SPDX-License-Identifier: Apache-2.0
#pragma once
// Point instancing (Instance OBJ, s@instance) in Houdini's own instance representation:
// an owned detail with one packed geometry primitive per instancing point, referencing the
// prototype SOP's cooked detail. Everything downstream is the ordinary packed pipeline.
// Host thread only; no OP pointer survives a call.
#include "preserved_detail.h"
#include "material_assignment.h"
#include "cycles_houdini/diagnostic.h"
#include "cycles_houdini/session.h"
#include <GU/GU_DetailHandle.h>
#include <UT/UT_Error.h>
#include <SYS/SYS_Types.h>
#include <cstdint>
#include <functional>
#include <map>
#include <memory>
#include <optional>
#include <string>
#include <vector>
class SOP_Node;
class OP_Context;
namespace cycles_houdini {
struct GeometryRequest;
struct ExtractionStats;
// Members: an object subnet used as a prototype; one instance per member object at its world transform.
enum class NativeInstancingMode { Fast, Full, Off, Members };
bool native_blend_reference(const std::string &value);
// One resolved prototype OBJ. Its request carries the prototype's geometry settings and
// object material binding; object/ray settings stay the instancer's (DESIGN.md section 5).
struct NativePointPrototype {
  std::string path;       // Absolute OBJ path at capture time.
  std::int64_t node_id{}; // OP unique id; a rename keeps it, delete/recreate changes it.
  std::shared_ptr<const GeometryRequest> request;
  std::string source_reference;       // External s@instance token; empty for ordinary OBJ paths.
  std::optional<double> source_frame; // Absolute fractional Blender frame, not a time offset.
  std::vector<std::shared_ptr<const GeometryRequest>> external_members;
  bool operator==(const NativePointPrototype &other) const;
};
struct NativePointInstancing {
  NativeInstancingMode mode{NativeInstancingMode::Fast};
  std::string instancer_path;             // Relative instance paths resolve against this object.
  int fallback{-1};                       // Prototype index of `instancepath`, -1 when empty/unresolved.
  double frame{1};                        // Requested Houdini frame, used when f@index is absent.
  std::map<double, double> frame_samples; // Exact captured host time -> default source frame over the shutter.
  std::vector<NativePointPrototype> prototypes;
  // Transform-only point attributes (orient, N, up, pscale, scale, rot, trans, pivot, transform) and
  // `instance` are consumed by the instance transform and not copied to the instances, except these:
  // names a shader or style sheet in the scene may read (computed by the host).
  std::vector<std::string> keep_attributes;
  bool operator==(const NativePointInstancing &other) const;
};
// Shared immutable value compared by content, so GeometryRequest::operator== stays a value test.
struct NativePointInstancingRef {
  std::shared_ptr<const NativePointInstancing> value;
  explicit operator bool() const { return bool(value); }
  const NativePointInstancing *operator->() const { return value.get(); }
  const NativePointInstancing &operator*() const { return *value; }
  bool operator==(const NativePointInstancingRef &other) const {
    return value == other.value || (value && other.value && *value == *other.value);
  }
};
// Primitive attribute of the synthetic detail: index into NativePointInstancing::prototypes.
inline constexpr const char *point_prototype_attribute = "__cycles_prototype";
// Request whose prototype is a point prototype: the instancer's object state with the
// prototype's geometry settings (DESIGN.md section 5).
GeometryRequest point_prototype_request(const GeometryRequest &instancer, const GeometryRequest &prototype);

struct NativePointInstanceSample {
  struct DirectPlacement {
    std::shared_ptr<const GeometryRequest> request;
    Matrix transform{identity_matrix};
    std::string identity;
    std::size_t point{}, prototype{};
    NativeMaterialAssignment binding;
    std::vector<UniformAttribute> attributes;
    Float3 velocity{};
    Float3 acceleration{}; // Point accel; read only in Acceleration mode.
    std::size_t binding_index{};
  };
  std::vector<DirectPlacement> direct;
  // Representative placement for each member/material/field-layout binding.
  // Built with the sample and reused with its immutable placement table, so
  // host material resolution never needs to revisit every template point.
  std::vector<std::size_t> direct_bindings;
  GU_ConstDetailHandle handle; // Empty when the instancer could not be evaluated.
  std::vector<SourceDiagnostic> diagnostics;
  std::string warning;
  std::size_t instances{};
};
// Memoizes synthetic details by complete source stamps so an unchanged instancer keeps the
// same GU_Detail (and unique id) across captures. Pure memoization: clear() is always safe.
class NativePointInstanceCache {
public:
  NativePointInstanceCache();
  ~NativePointInstanceCache();
  // `nominal` is the nominal sample of the same capture when `time` is a shutter sample: a
  // point (with name/id) whose prototype differs from its nominal prototype is omitted here,
  // so the packed population rules freeze it instead of interpolating unrelated instances.
  std::shared_ptr<const NativePointInstanceSample> synthesize(SOP_Node &points, const GeometryRequest &request,
                                                              double time, const NativePointInstanceSample *nominal,
                                                              ExtractionStats *stats, int depth = 0);
  // Start of a capture that synthesizes: drops entries unused by the previous such capture.
  // Camera-only captures never call it, so they cannot evict reusable details.
  void begin_capture();
  void clear();
  struct Counters {
    std::uint64_t syntheses{}, cache_hits{}, instances{};
    double seconds{};
  };
  const Counters &counters() const { return counters_; } // Cumulative, host thread.
  Counters counters_;

private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};
// Every "cook the request's SOP at time t" in the adapter goes through this. Its interface
// mirrors the SOP_Node calls it replaces; for ordinary requests it forwards 1:1.
class NativeRootSource {
public:
  using Report = std::function<void(const SourceDiagnostic &)>;
  NativeRootSource(SOP_Node &sop, const GeometryRequest &request, NativePointInstanceCache *cache, double nominal,
                   ExtractionStats *stats, Report report = {});
  GU_ConstDetailHandle getCookedGeoHandle(OP_Context &context);
  UT_ErrorSeverity error() const; // Of the most recent cook.
  exint getCookCount() const;     // SOP cooks plus syntheses of this source.
  bool isTimeDependent(OP_Context &context);
  int getUniqueId() const;
  SOP_Node &node() const { return *sop_; }
  bool instancer() const;
  const std::shared_ptr<const NativePointInstanceSample> &point_sample() const { return latest_sample_; }
  // Public edit versions of the SOP network (deformation_dependencies) and, for instancers,
  // of every prototype SOP network and prototype/instancer OBJ.
  void dependencies(std::vector<std::int64_t> &result) const;

private:
  SOP_Node *sop_;
  const GeometryRequest *request_;
  NativePointInstanceCache *cache_;
  double nominal_;
  ExtractionStats *stats_;
  Report report_;
  std::shared_ptr<const NativePointInstanceSample> nominal_sample_;
  std::shared_ptr<const NativePointInstanceSample> latest_sample_;
  bool failed_{};
  exint syntheses_{};
};
} // namespace cycles_houdini
