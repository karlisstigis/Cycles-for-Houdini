// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "material_assignment.h"
#include "stylesheet_sources.h"
#include "native_preparation.h"
#include <array>
#include <deque>
#include <stdexcept>
#include <tuple>

namespace cycles_houdini {
struct GeometryRequest;
struct NativeGeometrySelection;
struct NativeGeometryPartitions;
GeometryRequest sample_object_materials(const GeometryRequest &, double time);
void object_material_identifiers(std::vector<std::int64_t> &, const GeometryRequest &);
// Only owned strings and values survive host evaluation. STY subjects, their
// GA pointers and script closures never enter conversion or the worker.
struct NativeMaterialStyles {
  struct Layer {
    std::string material, parameters, surface_parameters, displacement_parameters;
    auto operator<=>(const Layer &) const = default;
  };
  class Layers {
  public:
    std::uint32_t intern(Layer);
    void resize(std::size_t count) {
      if (sealed_)
        throw std::logic_error("Cannot resize frozen native stylesheet layers");
      slots_.resize(count);
    }
    void set(std::size_t index, std::uint32_t slot) {
      if (sealed_)
        throw std::logic_error("Cannot change frozen native stylesheet slots");
      slots_.at(index) = slot;
    }
    void push_back(Layer value) { slots_.push_back(intern(std::move(value))); }
    void seal();
    bool empty() const { return slots_.empty(); }
    std::size_t size() const { return slots_.size(); }
    const Layer &at(std::size_t index) const { return values_.at(slots_.at(index)); }
    const Layer &front() const { return at(0); }
    const auto &values() const { return values_; }
    const auto &slots() const { return slots_; }

  private:
    std::vector<Layer> values_;
    std::vector<std::uint32_t> slots_;
    std::map<Layer, std::uint32_t> interned_;
    bool sealed_{};
  };
  Layers primitives, points;
  // Complete native parent->point results, keyed by actual containing
  // primitive and source point numbers. Mesh-only points never enter here.
  Layers point_context_layers;
  std::map<std::pair<std::size_t, std::size_t>, std::uint32_t> point_contexts;
  std::set<std::size_t> point_context_primitives;
  std::string default_descendant;
  std::map<std::size_t, std::string> descendants;
  std::string signature;
  std::vector<SourceDiagnostic> diagnostics;
  std::vector<std::string> resources;
  bool scripted{};
};
class NativeStyleCache {
public:
  std::shared_ptr<const NativeMaterialStyles>
  prepare(const GU_Detail &, const std::string &stylesheet, const std::vector<std::string> &resources, double time,
          const std::string &cwd, std::map<std::string, double> *timings = nullptr,
          NativePreparationBudget *budget = nullptr, const NativeGeometryPartitions *partitions = nullptr,
          const std::array<bool, 5> *render_types = nullptr);
  void clear() {
    records_.clear();
    used_.clear();
    captures_.clear();
    capture_ = 0;
  }
  void release_capture_work() {
    captures_.clear();
    capture_ = 0;
  }
  void retire(const std::set<std::int64_t> &);

private:
  struct Record {
    std::vector<std::int64_t> stamp;
    std::shared_ptr<const NativeMaterialStyles> styles;
    std::vector<std::string> source_signature;
  };
  std::map<std::pair<std::int64_t, std::string>, Record> records_;
  std::set<std::pair<std::int64_t, std::string>> used_;
  struct Work {
    std::vector<std::int64_t> stamp;
    std::shared_ptr<NativeMaterialStyles> result = std::make_shared<NativeMaterialStyles>();
    std::map<std::string, bool> checked;
    std::map<std::string, std::string> watched;
    std::size_t validation_owner{}, validation_index{}, next_primitive{}, next_point{};
    // Segmented owner storage avoids copying an acquired prefix when a
    // large point scope grows during a cooperative host turn.
    std::deque<std::size_t> point_scope, point_primitives, standalone_points;
    std::array<std::size_t, 2> scope_point{}, scope_primitive{};
    std::size_t scope_primitive_kind{};
    std::shared_ptr<const NativeGeometryPartitions> scope_partitions;
    std::size_t next_context{}, context_vertex{}, next_standalone{};
    int subject_limit{1024};
    bool parent_safe{}, local{}, detail_safe{true}, started{}, validated{}, initialized{}, done{}, point_scope_ready{},
        point_local{}, point_targets{};
  };
  std::map<std::tuple<std::int64_t, std::string, std::uint64_t>, Work> captures_;
  std::uint64_t capture_{};
};
NativeMaterialAssignment styled_material(NativeMaterialAssignment, const NativeMaterialStyles::Layer &);
struct NativeExternalMaterialStyle {
  NativeMaterialStyles::Layer layer;
  std::vector<SourceDiagnostic> diagnostics;
  std::vector<std::string> resources;
};
// Blanket/self/primitive rules only. Selective targets never become blanket
// assignments when their geometry domain is unavailable.
NativeExternalMaterialStyle external_uniform_style(const std::string &, double time, const std::string &cwd);
std::string descendant_styles(const GeometryRequest &, const GU_Detail &, GA_Offset);
// A per-capture catalog of effective assignments. Native handles are borrowed
// only by the transient caller; a continuation stores owner numbers and strings.
class NativeStyleBindingCache {
public:
  struct Assignments {
    std::set<NativeMaterialAssignment> values;
    std::size_t point_curves{}, vertex_curves{}, styled_curves{};
    std::size_t override_variants{}, override_bytes{};
    unsigned active_overrides{};
    bool override_excess{};
    std::vector<std::size_t> point_examples, vertex_examples, style_examples;
  };
  const Assignments &assignments(const GU_Detail &, const NativeGeometrySelection &, const GeometryRequest &,
                                 const NativeMaterialBindings &, NativePreparationBudget &, bool can_yield = true);
  void clear() {
    work_.clear();
    capture_ = 0;
  }

private:
  using Key = std::tuple<std::int64_t, int, NativeMaterialAssignment, std::string, unsigned>;
  struct Work {
    Assignments assignments;
    std::size_t primitive{}, point{}, vertex{};
    bool done{};
    std::optional<NativeCurveMaterialBinding> curve;
  };
  std::map<Key, Work> work_;
  std::uint64_t capture_{};
};
std::shared_ptr<const NativeMaterialLookup>
freeze_styled_materials(const GU_Detail &, const NativeGeometrySelection &, const GeometryRequest &,
                        std::shared_ptr<const NativeMaterialLookup>, bool volume = false,
                        NativeStyleBindingCache *cache = nullptr, NativePreparationBudget *budget = nullptr);
} // namespace cycles_houdini
