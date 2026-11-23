// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <cstdint>
#include <map>
#include <memory>
#include <set>
#include <string>
#include <vector>
#include <variant>
#include <optional>
#include <compare>
#include <array>
#include <tuple>
#include "cycles_houdini/diagnostic.h"
#include <GA/GA_Handle.h>
class GU_Detail;
namespace cycles_houdini {
struct GeometryRequest;
struct NativeGeometrySelection;
enum class NativeMaterialMode { Reject, Uniform, Native };
struct NativeMaterialAssignment {
  std::string path;
  std::vector<std::string> overrides; // Owned low-to-high priority dictionary literals.
  std::vector<std::string> surface_overrides, displacement_overrides;
  NativeMaterialAssignment() = default;
  NativeMaterialAssignment(std::string path) : path(std::move(path)) {}
  NativeMaterialAssignment(std::string path, std::vector<std::string> overrides, std::vector<std::string> surface = {},
                           std::vector<std::string> displacement = {})
      : path(std::move(path)), overrides(std::move(overrides)), surface_overrides(std::move(surface)),
        displacement_overrides(std::move(displacement)) {}
  bool has_overrides() const {
    return !overrides.empty() || !surface_overrides.empty() || !displacement_overrides.empty();
  }
  auto operator<=>(const NativeMaterialAssignment &) const = default;
};
NativeMaterialAssignment inherit_native_material(NativeMaterialAssignment local,
                                                 const NativeMaterialAssignment &inherited);
struct NativeMaterialTimeline;
struct NativeMaterialStyles;
struct NativeMaterialReference {
  std::string key, path;
  int session_id{};
  std::string identity;
  bool volume{};
  std::optional<SourceDiagnostic> diagnostic;
  std::vector<std::string> overrides;
  std::vector<std::string> surface_overrides, displacement_overrides;
  std::shared_ptr<const NativeMaterialTimeline> timeline;
  NativeMaterialAssignment assignment() const { return {path, overrides, surface_overrides, displacement_overrides}; }
  bool operator==(const NativeMaterialReference &) const;
};
// Pure owned binding provenance. Leaves are shared immutable source references,
// never Houdini nodes. The material graph table owns their evaluated values.
struct NativeMaterialTimeline {
  std::shared_ptr<const NativeMaterialReference> nominal;
  std::vector<std::shared_ptr<const NativeMaterialReference>> samples;
};
NativeMaterialReference native_material_timeline(std::shared_ptr<const NativeMaterialReference> nominal,
                                                 std::vector<std::shared_ptr<const NativeMaterialReference>> samples);
std::set<std::string> native_material_paths(const NativeMaterialReference &reference);
NativeMaterialReference refresh_native_material_reference(const NativeMaterialReference &reference);
// A healthy material rename changes its path metadata, not its resource or
// binding history. Recovery diagnostics and resource replacement remain strict.
bool same_native_material_resource(const NativeMaterialReference &, const NativeMaterialReference &);
NativeMaterialReference resolve_native_material(const std::string &path, bool volume = false);
NativeMaterialReference resolve_native_material(const NativeMaterialAssignment &assignment, bool volume = false);
// An unused binding may reference a missing node. Preserve its diagnostic
// without rejecting until conversion establishes that the binding is used.
struct NativeMaterialPolicy {
  unsigned ignored_overrides{}; // Detail=1, primitive=2, point=4, vertex=8; paths stay intact.
  std::vector<SourceDiagnostic> diagnostics;
};
struct NativeMaterialRecipe {
  NativeMaterialAssignment primitive, point;
  bool point_layer{};
  auto operator<=>(const NativeMaterialRecipe &) const = default;
};
struct NativeMaterialElements {
  // Primitive numbers refer only to the owned nominal detail. An empty slot
  // array uses uniform for every mesh primitive; -1 retains ordinary binding.
  int uniform{-1};
  std::vector<int> primitive_slots;
  std::vector<std::shared_ptr<const NativeMaterialReference>> references;
};
struct NativeMaterialLookup : std::map<NativeMaterialAssignment, NativeMaterialReference> {
  // -1 is a single-component table; mixed shutter captures use geometry kind.
  std::map<int, std::shared_ptr<const NativeMaterialPolicy>> policies;
  bool displacement_only{}; // Off-frame bindings do not add surface field demand.
  std::shared_ptr<const NativeMaterialElements> elements;
  std::vector<SourceDiagnostic> diagnostics;
  std::optional<bool> unassigned;
};
// A Cycles strand has one shader. Compare complete effective assignments at
// its authored keys; never choose a representative key or interpolate shaders.
// The accumulator is owned and may survive a host-preparation continuation.
struct NativeCurveMaterialBinding {
  NativeMaterialAssignment native_primitive, primitive, point, vertex;
  NativeMaterialAssignment styled_primitive, styled_point, styled_vertex;
  bool started{}, point_uniform{true}, vertex_uniform{true}, point_active{}, vertex_active{};
  bool point_style{}, styled_primitive_uniform{true}, styled_point_uniform{true}, styled_vertex_uniform{true};
  unsigned override_owners{};
  NativeMaterialAssignment assignment(unsigned *omitted = nullptr) const;
  unsigned active_overrides() const {
    return !started                                  ? override_owners & 3u
           : point_style && styled_vertex_uniform    ? override_owners
           : point_style && styled_point_uniform     ? override_owners & 7u
           : point_style && styled_primitive_uniform ? override_owners & 3u
           : vertex_uniform                          ? override_owners
           : point_uniform                           ? override_owners & 7u
                                                     : override_owners & 3u;
  }
};
std::shared_ptr<const NativeMaterialLookup>
freeze_native_material_candidates(const GU_Detail &source, const std::set<NativeMaterialAssignment> &fallback_paths,
                                  bool displacement_only = false);
struct NativeMaterialBindingState {
  std::vector<SourceDiagnostic> diagnostics;
  std::string detail_path;
  std::vector<std::string> primitive_paths, point_paths;
  NativeMaterialAssignment detail_binding;
  std::vector<NativeMaterialRecipe> bindings;
  std::shared_ptr<const NativeMaterialPolicy> policy;
  bool fallback_used{}, point_overrides{}, point_stylesheets{}, vertex_bindings{}, vertex_stylesheets{},
      has_overrides{};
};
// Host-only lookup of the paths actually used by one geometry component.
// The returned map owns strings/identities; it contains no OP/GA pointers.
std::shared_ptr<const NativeMaterialLookup> freeze_native_materials(const NativeMaterialBindingState &state,
                                                                    const GeometryRequest &request, bool points,
                                                                    bool volume = false,
                                                                    bool displacement_only = false);
// Attribute IDs avoid rescanning a large primitive string table on a camera
// edit. Only owned metadata is retained, and inactive details are retired.
class NativeMaterialBindingCache {
public:
  const NativeMaterialBindingState &inspect(const GU_Detail &source,
                                            const NativeGeometrySelection *selection = nullptr);
  void clear() { records_.clear(); }
  void reset() {
    clear();
    scans_ = hits_ = 0;
  }
  void retire(const std::set<std::int64_t> &details);
  std::uint64_t scans() const { return scans_; }
  std::uint64_t hits() const { return hits_; }

private:
  struct Record {
    std::vector<std::int64_t> identifiers;
    NativeMaterialBindingState state;
  };
  std::map<std::pair<std::int64_t, int>, Record> records_;
  std::uint64_t scans_{}, hits_{};
};
// Bulk assignment extraction from retained immutable geometry and a frozen
// material lookup. This class never resolves or evaluates an OP node.
class NativeMaterialBindings {
public:
  // Only owned lookup values may survive a preparation turn. Each transient
  // helper reconstructs its borrowed GA handles against the retained detail.
  struct Values {
    std::map<std::string, int> slots;
    std::map<std::pair<GA_StringIndexType, GA_StringIndexType>, int> primitive_slots;
    std::map<std::array<GA_StringIndexType, 4>, int> point_slots;
    std::map<std::tuple<GA_StringIndexType, GA_StringIndexType, std::uint32_t>, int> styled_primitive_slots;
    std::map<std::tuple<std::array<GA_StringIndexType, 4>, std::uint32_t, std::uint64_t>, int> styled_point_slots;
    std::map<NativeMaterialAssignment, std::string> paths;
    std::vector<std::string> materials;
    std::map<std::string, NativeMaterialReference> references;
  };
  NativeMaterialBindings(const GU_Detail &source, const GeometryRequest &request,
                         const NativeGeometrySelection *selection = nullptr, Values *values = nullptr);
  NativeMaterialBindings(const NativeMaterialBindings &) = delete;
  NativeMaterialBindings &operator=(const NativeMaterialBindings &) = delete;
  std::string path(GA_Offset offset) const;
  NativeMaterialAssignment assignment(GA_Offset offset) const;
  // With an extra inherited layer between the primitive and the object (point-instance prototypes).
  NativeMaterialAssignment assignment(GA_Offset offset, const NativeMaterialAssignment &inherited) const;
  NativeMaterialAssignment point_assignment(GA_Offset offset, GA_Offset primitive = GA_INVALID_OFFSET) const;
  NativeMaterialAssignment local_primitive_assignment(GA_Offset offset) const;
  NativeMaterialAssignment local_point_assignment(GA_Offset offset) const;
  NativeMaterialAssignment local_vertex_assignment(GA_Offset offset) const;
  NativeCurveMaterialBinding curve_binding(GA_Offset primitive) const;
  void curve_key(NativeCurveMaterialBinding &, GA_Offset vertex) const;
  NativeMaterialAssignment curve_assignment(GA_Offset primitive, unsigned *omitted = nullptr) const;
  bool curve_key_layers() const;
  std::optional<NativeMaterialAssignment> uniform_primitive_assignment() const {
    if (!native_ || primitive_path_.isValid() || primitive_override_.isValid())
      return {};
    return fallback_assignment_;
  }
  std::optional<NativeMaterialAssignment> uniform_point_assignment() const {
    if (point_path_.isValid() || point_override_.isValid())
      return {};
    return uniform_primitive_assignment();
  }
  const NativeMaterialAssignment &detail_assignment() const { return detail_assignment_; }
  unsigned ignored_overrides() const { return ignored_overrides_; }
  int primitive(GA_Offset offset);
  int point(GA_Offset offset, GA_Offset primitive = GA_INVALID_OFFSET);
  int curve(GA_Offset offset);
  std::vector<std::string> materials() const { return values_->materials; }
  std::vector<NativeMaterialReference> references() const;

private:
  std::optional<std::uint32_t> point_style_context(GA_Offset point, GA_Offset primitive) const;
  int frozen_binding(const NativeMaterialReference &reference);
  std::string resolve(const NativeMaterialAssignment &assignment);
  int binding(const NativeMaterialAssignment &assignment);
  std::string fallback_, fallback_path_;
  NativeMaterialAssignment fallback_assignment_;
  NativeMaterialAssignment detail_assignment_;
  bool native_{};
  const GU_Detail *source_{}; // Borrowed only during this converter invocation.
  std::shared_ptr<const NativeMaterialLookup> frozen_;
  std::shared_ptr<const NativeMaterialStyles> styles_;
  GA_ROHandleS primitive_path_, primitive_override_, primitive_stylesheet_;
  GA_ROHandleS point_path_, point_override_, point_stylesheet_;
  GA_ROHandleS vertex_path_, vertex_override_;
  unsigned ignored_overrides_{};
  Values local_values_;
  Values *values_;
};
} // namespace cycles_houdini
