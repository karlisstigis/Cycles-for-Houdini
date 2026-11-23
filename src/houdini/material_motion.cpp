// SPDX-License-Identifier: Apache-2.0
#include "material_motion.h"
#include <algorithm>
#include <chrono>
#include <cstdlib>
#include <exception>
#include <functional>
#include <limits>
#include <map>
#include <optional>
#include <stdexcept>
#include <string_view>
#include <tuple>
#include <GA/GA_Iterator.h>
#include <GU/GU_Detail.h>
#include <GU/GU_PackedFragment.h>
#include <GU/GU_PackedGeometry.h>
#include <GU/GU_PrimPacked.h>
#include <OP/OP_Context.h>
#include <SOP/SOP_Node.h>
#include <UT/UT_Thread.h>
#include <UT/UT_XXHash.h>

namespace cycles_houdini {
namespace {
using Clock = std::chrono::steady_clock;
using Reference = std::shared_ptr<const NativeMaterialReference>;
class MotionProfile {
public:
  explicit MotionProfile(std::map<std::string, double> *times) : times_(times) {
    if (times_)
      last_ = Clock::now();
  }
  void mark(const char *name) {
    if (times_) {
      const auto now = Clock::now();
      (*times_)[name] += std::chrono::duration<double>(now - last_).count();
      last_ = now;
    }
  }

private:
  std::map<std::string, double> *times_;
  Clock::time_point last_;
};
struct MeshBindings {
  std::shared_ptr<const NativeMaterialLookup> lookup;
  std::size_t primitive_count{};
  bool full{}, authored{};
  int uniform{-1};
  std::vector<int> slots;
  std::vector<GA_Index> indices;
  std::vector<Reference> references;
  int at(GA_Index index) const {
    if (index < 0 || std::size_t(index) >= primitive_count)
      return -1;
    if (!full && !std::binary_search(indices.begin(), indices.end(), index))
      return -1;
    return slots.empty() ? uniform : slots.at(std::size_t(index));
  }
};
struct Node {
  struct Child {
    std::string identity;
    bool indexed{};
    Matrix transform;
    std::shared_ptr<const Node> node;
  };
  std::shared_ptr<const MeshBindings> mesh;
  std::array<bool, 5> kinds{};
  std::vector<Child> children;
  std::map<std::string, std::size_t> named;
  std::string unavailable;
  std::vector<SourceDiagnostic> diagnostics;
  bool stable{true};
};
struct Record {
  std::uint64_t capture{};
  std::int64_t input_detail{};
  std::vector<std::int64_t> stamp;
  std::string path, material, stylesheet;
  std::string object_revision;
  GeometryRequest::MaterialSamples object_samples;
  NativePointInstancingRef point_instancing;
  NativeMaterialAssignment inherited;
  std::vector<double> times;
  std::array<bool, 5> kinds{};
  std::shared_ptr<const PreservedDetail> nominal;
  std::shared_ptr<const NativePackedDetails> packed;
  std::shared_ptr<const Node> root;
  std::vector<std::shared_ptr<const Node>> samples;
  std::string restore_warning;
  std::vector<Reference> probe_references;
  std::vector<std::pair<std::string, bool>> probe_stamp;
  std::set<std::string> dependencies;
  std::vector<std::string> style_resources, style_stamp;
  bool stable{true}, positive{};
  bool scripted{};
};
std::vector<std::int64_t> stamp(NativeRootSource &sop, const GU_Detail &source) {
  return {sop.getUniqueId(),
          sop.getCookCount(),
          source.getUniqueId(),
          source.getP()->getDataId(),
          source.getTopology().getDataId(),
          source.getPrimitiveList().getDataId(),
          source.getNumPrimitives(),
          source.getNumPoints()};
}
bool authored(const GU_Detail &source) {
  for (const auto owner : {GA_ATTRIB_DETAIL, GA_ATTRIB_PRIMITIVE})
    for (const auto *name : {"shop_materialpath", "material_override", "material_stylesheet"})
      if (source.findAttribute(owner, name))
        return true;
  return false;
}
bool stylesheet_source(const GU_Detail &root, const NativePackedDetails *frozen) {
  // Inspect only owned attribute metadata and the already-acquired packed
  // hierarchy. Ordinary material assignments do not justify a shutter
  // binding probe on static geometry. Visit each shared prototype once and
  // do not inspect packed sources belonging to another scene root.
  std::set<std::int64_t> visited;
  std::vector<std::int64_t> owners;
  const auto add = [&](const GU_Detail &source) {
    if (!visited.insert(source.getUniqueId()).second)
      return false;
    for (const auto owner : {GA_ATTRIB_DETAIL, GA_ATTRIB_PRIMITIVE, GA_ATTRIB_POINT})
      if (source.findAttribute(owner, "material_stylesheet"))
        return true;
    owners.push_back(source.getUniqueId());
    return false;
  };
  if (add(root))
    return true;
  if (frozen)
    for (std::size_t i = 0; i < owners.size(); ++i) {
      const auto id = owners[i];
      auto it = frozen->lower_bound({id, std::numeric_limits<GA_Offset>::min()});
      for (; it != frozen->end() && it->first.first == id; ++it)
        if (it->second && add(*it->second->get()))
          return true;
    }
  return false;
}
// Capture-scoped cooperative proof of mesh absence. Every continuation owns
// its details/ledger and stores only numeric ledger cursors across host turns.
class MeshPresenceProbe {
public:
  bool prepare(const std::shared_ptr<const PreservedDetail> &nominal,
               const std::shared_ptr<const NativePackedDetails> &frozen, NativePartitionCache &partitions,
               NativePreparationBudget *budget) {
    if (done_)
      return possible_;
    if (!root_) {
      root_ = nominal;
      frozen_ = frozen;
      stack_.push_back({nominal});
    }
    const auto *test_slice = std::getenv("CYCLES_HOUDINI_MATERIAL_MOTION_TEST_SLICE_5MS");
    const auto gate_deadline = test_slice && std::string_view(test_slice) == "1"
                                   ? Clock::now() + std::chrono::milliseconds(5)
                                   : Clock::time_point::max();
    std::size_t entries = 0;
    while (!stack_.empty()) {
      if ((entries == 0 || entries >= 256) && budget && (budget->expired() || Clock::now() >= gate_deadline))
        throw NativePreparationPending{};
      if (entries >= 256)
        entries = 0;
      auto &work = stack_.back();
      const auto &source = *work.detail->get();
      const auto id = source.getUniqueId();
      if (!work.initialized) {
        if (stack_.size() > 65 || ancestors_.contains(id))
          return finish(true);
        if (complete_.contains(id)) {
          stack_.pop_back();
          continue;
        }
        const auto layout = partitions.acquire(source);
        if (layout->find(NativeGeometryKind::Mesh))
          return finish(true);
        const auto *packed = layout->find(NativeGeometryKind::Packed);
        if (!packed) {
          complete_.insert(id);
          stack_.pop_back();
          continue;
        }
        if (!frozen_)
          return finish(true);
        work.count = packed->full_detail ? source.getNumPrimitives() : packed->primitives.entries();
        work.initialized = true;
        ancestors_.insert(id);
      }
      auto it = work.cursor ? frozen_->upper_bound({id, work.after})
                            : frozen_->lower_bound({id, std::numeric_limits<GA_Offset>::min()});
      bool child = false;
      for (; it != frozen_->end() && it->first.first == id && entries < 256; ++it) {
        ++entries;
        work.cursor = true;
        work.after = it->first.second;
        ++work.acquired;
        if (!it->second)
          return finish(true);
        const auto *primitive = dynamic_cast<const GU_PrimPacked *>(source.getPrimitive(work.after));
        const auto *implementation = primitive ? primitive->sharedImplementation() : nullptr;
        if (!dynamic_cast<const GU_PackedGeometry *>(implementation) &&
            !dynamic_cast<const GU_PackedFragment *>(implementation))
          return finish(true); // External/procedural implementations keep acquisition.
        const auto child_id = it->second->get()->getUniqueId();
        if (ancestors_.contains(child_id))
          return finish(true);
        if (!complete_.contains(child_id)) {
          stack_.push_back({it->second});
          child = true;
          break;
        }
      }
      if (child)
        continue;
      if (it == frozen_->end() || it->first.first != id) {
        if (work.acquired != work.count)
          return finish(true); // Missing/unexpanded contents are unknown.
        ancestors_.erase(id);
        complete_.insert(id);
        stack_.pop_back();
      }
    }
    return finish(false);
  }

private:
  struct Work {
    std::shared_ptr<const PreservedDetail> detail;
    GA_Offset after{};
    exint count{}, acquired{};
    bool initialized{}, cursor{};
  };
  bool finish(bool possible) {
    done_ = true;
    possible_ = possible;
    stack_.clear();
    root_.reset();
    frozen_.reset();
    complete_.clear();
    ancestors_.clear();
    return possible;
  }
  std::shared_ptr<const PreservedDetail> root_;
  std::shared_ptr<const NativePackedDetails> frozen_;
  std::vector<Work> stack_;
  std::set<std::int64_t> complete_, ancestors_;
  bool done_{}, possible_{};
};
Matrix multiply(const Matrix &a, const Matrix &b) {
  Matrix result{};
  for (int r = 0; r < 3; ++r) {
    for (int c = 0; c < 3; ++c)
      for (int k = 0; k < 3; ++k)
        result[r * 4 + c] += a[r * 4 + k] * b[k * 4 + c];
    result[r * 4 + 3] = a[r * 4 + 3];
    for (int k = 0; k < 3; ++k)
      result[r * 4 + 3] += a[r * 4 + k] * b[k * 4 + 3];
  }
  return result;
}
class MotionSlotCache {
public:
  std::shared_ptr<const MeshBindings> prepare(const GU_Detail &source, const NativeGeometrySelection &selection,
                                              const GeometryRequest &request,
                                              std::shared_ptr<const NativeMaterialLookup> lookup, bool fallback,
                                              bool authored, NativePreparationBudget *budget) {
    if (budget && capture_ != budget->capture) {
      clear();
      capture_ = budget->capture;
    }
    const Key key{source.getUniqueId(),
                  request.material_assignment(),
                  request.native_styles ? request.native_styles->signature : std::string{},
                  request.material_key,
                  authored,
                  lookup->displacement_only};
    Work synchronous;
    auto &work = budget ? work_[key] : synchronous;
    if (work.values && (*work.values->lookup != *lookup || work.fallback != fallback))
      work = Work{};
    if (!work.values) {
      work.values = std::make_shared<MeshBindings>();
      auto &values = *work.values;
      values.lookup = std::move(lookup);
      values.primitive_count = std::size_t(source.getNumPrimitives());
      values.full = selection.full_detail;
      values.authored = authored;
      work.fallback = fallback;
      work.one = values.lookup->empty() || (values.lookup->size() == 1 && !fallback);
      if (!work.one)
        values.slots.assign(values.primitive_count, -1);
    }
    if (work.done)
      return work.values;
    NativeMaterialBindings resolved(source, request, &selection, &work.bindings);
    auto &values = *work.values;
    const auto count = std::size_t(work.one && selection.full_detail ? std::min<exint>(1, source.getNumPrimitives())
                                   : selection.full_detail           ? source.getNumPrimitives()
                                                                     : selection.primitives.entries());
    while (work.next < count) {
      const auto end = std::min(count, work.next + 512);
      while (work.next < end) {
        const auto next = work.next++;
        const auto offset =
            selection.full_detail ? source.primitiveOffset(GA_Index(next)) : selection.primitives(exint(next));
        const auto index = source.primitiveIndex(offset);
        if (!values.full)
          values.indices.push_back(index);
        if (!work.one || next == 0) {
          const auto slot = resolved.primitive(offset);
          if (work.one)
            values.uniform = slot;
          else
            values.slots.at(std::size_t(index)) = slot;
          if (work.first < 0)
            work.first = slot;
          else
            work.uniform &= work.first == slot;
        }
      }
      if (work.next < count && budget && budget->expired())
        throw NativePreparationPending{};
    }
    if (!work.one && work.uniform) {
      values.uniform = work.first;
      values.slots.clear();
    }
    const auto references = resolved.references();
    std::map<std::string, NativeMaterialReference> named;
    for (const auto &ref : references)
      named.emplace(ref.key, ref);
    for (const auto &name : resolved.materials()) {
      NativeMaterialReference ref;
      if (const auto found = named.find(name); found != named.end())
        ref = found->second;
      else
        ref.key = name;
      values.references.push_back(std::make_shared<const NativeMaterialReference>(std::move(ref)));
    }
    work.done = true;
    return work.values;
  }
  void clear() {
    work_.clear();
    capture_ = 0;
  }

private:
  using Key = std::tuple<std::int64_t, NativeMaterialAssignment, std::string, std::string, bool, bool>;
  struct Work {
    std::shared_ptr<MeshBindings> values;
    NativeMaterialBindings::Values bindings;
    std::size_t next{};
    int first{-1};
    bool one{}, uniform{true}, fallback{}, done{};
  };
  std::map<Key, Work> work_;
  std::uint64_t capture_{};
};
// A host continuation retains only owned source values and completed child
// nodes. Neither a graph callback nor a GA handle may survive a yield. Partial
// nodes and their ledger stay private until the entire root is complete.
struct CaptureState {
  using Key = std::tuple<std::int64_t, NativeMaterialAssignment, bool, std::string, std::vector<std::string>,
                         std::vector<std::string>, std::array<bool, 5>, std::uintptr_t>;
  struct Work {
    std::shared_ptr<Node> node = std::make_shared<Node>();
    std::shared_ptr<const NativeMaterialStyles> styles;
    std::shared_ptr<const NativePackedInstances> instances;
    std::size_t next_child{};
    bool initialized{}, mesh_done{}, done{};
  };
  std::map<Key, Work> nodes;
  std::map<std::string, Reference> probe_references;
  std::map<std::string, bool> probe_displaced;
  std::set<std::string> dependencies;
  std::map<std::string, std::string> watched;
  std::shared_ptr<NativePackedDetails> acquired;
  std::shared_ptr<const Node> root;
  bool needed{}, scripted{};
  bool sources_valid() const {
    std::vector<std::string> names, versions;
    for (const auto &[name, version] : watched) {
      names.push_back(name);
      versions.push_back(version);
    }
    return names.empty() || versions == stylesheet_source_signature(names);
  }
  void watch(const std::vector<std::string> &resources) {
    std::vector<std::string> added;
    for (const auto &name : resources)
      if (!watched.contains(name))
        added.push_back(name);
    if (added.empty())
      return;
    const auto versions = stylesheet_source_signature(added);
    for (std::size_t i = 0; i < added.size(); ++i)
      watched.emplace(added[i], versions[i]);
  }
};
std::vector<std::pair<std::string, bool>> prepared_probe_stamp(const std::vector<Reference> &references,
                                                               const NativeMaterialPreparation &materials) {
  NativeMaterialLookup candidates;
  auto elements = std::make_shared<NativeMaterialElements>();
  for (const auto &ref : references)
    elements->references.push_back(
        std::make_shared<const NativeMaterialReference>(refresh_native_material_reference(*ref)));
  candidates.elements = elements;
  materials(candidates);
  std::vector<std::pair<std::string, bool>> result;
  for (const auto &ref : elements->references)
    result.emplace_back(ref->key, materials.has_displacement(ref->key));
  return result;
}
bool probe_valid(const CaptureState &state, const NativeMaterialPreparation &materials) {
  std::vector<Reference> references;
  std::vector<std::pair<std::string, bool>> expected;
  for (const auto &[key, ref] : state.probe_references) {
    references.push_back(ref);
    expected.emplace_back(key, state.probe_displaced.at(key));
  }
  return references.empty() || expected == prepared_probe_stamp(references, materials);
}
struct Capture {
  CaptureState &state;
  NativeMaterialBindingCache &bindings;
  NativePartitionCache &partitions;
  NativePackedSources &owners;
  NativePackedDetails *acquired{};
  std::shared_ptr<const NativePackedDetails> frozen;
  const NativeMaterialPreparation &prepare;
  bool probe{}, displacement_only{};
  std::set<std::int64_t> ancestors;
  NativeStyleCache &styles;
  NativeStyleBindingCache &style_bindings;
  MotionSlotCache &slots;
  NativePackedPreparationCache &packed_preparation;
  double time{};
  std::map<std::string, double> *timings{};
  NativePreparationBudget *budget{};
  Capture(CaptureState &state, NativeMaterialBindingCache &bindings, NativePartitionCache &partitions,
          NativePackedSources &owners, NativeStyleCache &styles, NativeStyleBindingCache &style_bindings,
          MotionSlotCache &slots, NativePackedPreparationCache &packed, const NativeMaterialPreparation &prepare,
          double time, NativePreparationBudget *budget, std::map<std::string, double> *timings)
      : state(state), bindings(bindings), partitions(partitions), owners(owners), prepare(prepare), styles(styles),
        style_bindings(style_bindings), slots(slots), packed_preparation(packed), time(time), timings(timings),
        budget(budget) {}
  std::shared_ptr<const Node> node(const GU_Detail &source, GeometryRequest request, bool inherited_authored = false,
                                   int depth = 0) {
    MotionProfile profile(timings);
    if (depth > 64 || ancestors.contains(source.getUniqueId())) {
      auto failed = std::make_shared<Node>();
      failed->unavailable = "Cyclic or excessive packed material-binding hierarchy";
      failed->stable = false;
      return failed;
    }
    const bool local_authored =
        inherited_authored || authored(source) || !request.stylesheet.empty() || !request.object_material_path.empty();
    const CaptureState::Key key{source.getUniqueId(),
                                request.material_assignment(),
                                local_authored,
                                request.stylesheet,
                                request.stylesheet_errors,
                                request.stylesheet_resources,
                                request.render_types,
                                reinterpret_cast<std::uintptr_t>(request.point_instancing.value.get())};
    auto &work = state.nodes[key];
    if (work.done)
      return work.node;
    ancestors.insert(source.getUniqueId());
    auto &result = work.node;
    if (!work.initialized) {
      work.styles =
          styles.prepare(source, request.stylesheet, request.stylesheet_resources, time,
                         request.object_path.empty() ? request.sop_path : request.object_path, timings, budget);
      if (work.styles) {
        state.watch(work.styles->resources);
        state.scripted |= work.styles->scripted;
        for (auto issue : work.styles->diagnostics) {
          issue.source = request.sop_path;
          result->diagnostics.push_back(std::move(issue));
        }
      }
      for (const auto &message : request.stylesheet_errors)
        result->diagnostics.push_back({"material.stylesheet_ignored",
                                       request.sop_path,
                                       {},
                                       message,
                                       "Keeping supported stylesheet rules and underlying bindings",
                                       "warning",
                                       "material_stylesheet"});
      work.initialized = true;
    }
    request.native_styles = work.styles;
    profile.mark("material_motion_styles");
    const auto layout = partitions.acquire(source);
    for (const auto &selection : layout->selections)
      if (selection.active && request.render_types[std::size_t(selection.kind)])
        result->kinds[std::size_t(selection.kind)] = true;
    if (const auto *mesh = layout->find(NativeGeometryKind::Mesh); mesh && request.render_types[0] && !work.mesh_done) {
      const auto &binding_state = bindings.inspect(source, mesh);
      request.native_material_policy = binding_state.policy;
      auto lookup = freeze_native_materials(binding_state, request, false, false, displacement_only);
      lookup = freeze_styled_materials(source, *mesh, request, std::move(lookup), false, &style_bindings, budget);
      request.native_materials = lookup;
      request.native_fallback.reset();
      profile.mark("material_motion_bindings");
      const bool fallback =
          binding_state.fallback_used && binding_state.detail_path.empty() && request.object_material_path.empty();
      bool displaced = false;
      if (probe) {
        NativeMaterialLookup candidates = *lookup;
        auto extra = std::make_shared<NativeMaterialElements>();
        if (fallback && !request.material_key.empty()) {
          NativeMaterialReference uniform;
          uniform.key = request.material_key;
          extra->references.push_back(std::make_shared<const NativeMaterialReference>(std::move(uniform)));
        }
        candidates.elements = extra;
        prepare(candidates);
        const auto demand = [&](Reference ref) {
          if (!local_authored)
            return;
          const auto active = prepare.has_displacement(ref->key);
          state.probe_references.emplace(ref->key, ref);
          state.probe_displaced.insert_or_assign(ref->key, active);
          displaced |= active;
        };
        for (const auto &[assignment, reference] : *lookup)
          demand(std::make_shared<const NativeMaterialReference>(reference));
        for (const auto &reference : extra->references)
          demand(reference);
        state.needed |= displaced;
        profile.mark("material_motion_graphs");
      }
      if (!probe || displaced) {
        auto values = slots.prepare(source, *mesh, request, lookup, fallback, local_authored, budget);
        for (const auto &ref : values->references) {
          const auto paths = native_material_paths(*ref);
          state.dependencies.insert(paths.begin(), paths.end());
        }
        NativeMaterialLookup candidates = *lookup;
        auto elements = std::make_shared<NativeMaterialElements>();
        elements->references = values->references;
        candidates.elements = std::move(elements);
        profile.mark("material_motion_slots");
        prepare(candidates);
        profile.mark("material_motion_graphs");
        result->mesh = std::move(values);
      }
      work.mesh_done = true;
    }
    if (const auto *packed = layout->find(NativeGeometryKind::Packed); packed && request.render_types[4]) {
      const auto &state = bindings.inspect(source, packed);
      request.native_material_policy = state.policy;
      request.native_materials.reset();
      if (!work.instances)
        work.instances = packed_preparation.prepare(source, request, *packed, owners, frozen, false, budget);
      profile.mark("material_motion_packed_prepare");
      const auto &instances = work.instances->values;
      result->children.reserve(instances.size());
      // Children with equal (prototype, point prototype, assignment, descendant
      // stylesheet) build requests that are equal in every field node() keys
      // on; the rest of the child request comes unchanged from this request (or
      // from the same point prototype). Once node() has completed such a child,
      // a repeat is a node() cache hit, which returns the same stored node and
      // changes nothing. Reuse that node without copying another request.
      // Only completed nodes are reused: a failed (cyclic/too deep) result or a
      // child that yielded is computed by node() as before.
      using ChildKey = std::tuple<std::int64_t, int, NativeMaterialAssignment, std::string>;
      std::map<ChildKey, std::shared_ptr<const Node>> completed_children;
      std::optional<ChildKey> last_child_key;
      std::shared_ptr<const Node> last_child_node;
      // CYCLES_HOUDINI_FULL_MATERIAL_MOTION=1 (the diagnostic full walk) also builds every child request.
      const auto *full_walk = std::getenv("CYCLES_HOUDINI_FULL_MATERIAL_MOTION");
      const bool reuse_children = !full_walk || std::string_view(full_walk) != "1";
      while (work.next_child < instances.size()) {
        const auto end = std::min(instances.size(), work.next_child + 256);
        while (work.next_child < end) {
          const auto &instance = instances[work.next_child];
          if (acquired)
            acquired->emplace(std::make_pair(source.getUniqueId(), instance.primitive_offset), instance.prototype);
          const auto *prototype =
              request.point_instancing && instance.point_prototype >= 0
                  ? request.point_instancing->prototypes.at(std::size_t(instance.point_prototype)).request.get()
                  : nullptr;
          std::shared_ptr<const Node> data;
          if (instance.prototype) {
            const auto prototype_id = instance.prototype->get()->getUniqueId();
            ChildKey child_key{prototype_id, prototype ? instance.point_prototype : -1, instance.material_assignment,
                               descendant_styles(request, source, instance.primitive_offset)};
            // node() reports a cycle or excessive depth with a fresh node that
            // it does not store; those children are never reused.
            const bool reusable = reuse_children && depth + 1 <= 64 && !ancestors.contains(prototype_id);
            if (reusable && last_child_key && *last_child_key == child_key)
              data = last_child_node;
            else if (const auto found = reusable ? completed_children.find(child_key) : completed_children.end();
                     found != completed_children.end())
              data = found->second;
            if (!data) {
              auto child = prototype ? point_prototype_request(request, *prototype) : request;
              child.set_material_assignment(instance.material_assignment);
              child.object_material_samples.clear();
              child.stylesheet = std::get<3>(child_key);
              child.native_styles.reset();
              data = node(*instance.prototype->get(), std::move(child), local_authored, depth + 1);
              if (reusable)
                completed_children.insert_or_assign(child_key, data);
            }
            if (reusable && (!last_child_key || *last_child_key != child_key)) {
              last_child_key = std::move(child_key);
              last_child_node = data;
            }
          } else {
            auto missing = std::make_shared<Node>();
            missing->unavailable =
                instance.unavailable ? instance.unavailable->message : "Packed shutter binding source is unavailable";
            missing->stable = false;
            data = std::move(missing);
          }
          result->stable &= instance.stable_memory_source && data->stable;
          if (!instance.index_identity)
            result->named.emplace(instance.identity, result->children.size());
          result->children.push_back({instance.identity, instance.index_identity, instance.transform, std::move(data)});
          ++work.next_child;
        }
        if (work.next_child < instances.size() && budget && budget->expired()) {
          profile.mark("material_motion_children");
          throw NativePreparationPending{};
        }
      }
      profile.mark("material_motion_children");
    }
    ancestors.erase(source.getUniqueId());
    work.done = true;
    return result;
  }
};
const Node *routed(const std::shared_ptr<const Node> &root, const std::vector<NativeMaterialRoute> &route) {
  auto *node = root.get();
  for (const auto &step : route) {
    if (!node || !node->unavailable.empty())
      return nullptr;
    std::size_t index = step.index;
    if (!step.indexed) {
      const auto found = node->named.find(step.identity);
      if (found == node->named.end())
        return nullptr;
      index = found->second;
    }
    if (index >= node->children.size())
      return nullptr;
    node = node->children[index].node.get();
  }
  return node;
}
struct MotionRecipes {
  std::vector<std::vector<int>> tuples;
  std::vector<int> primitive_recipes;
  int uniform_recipe{-1};
  bool uniform{};
};
class MotionRecipeCache {
public:
  std::shared_ptr<const MotionRecipes> prepare(const std::shared_ptr<const Record> &owner,
                                               const std::vector<std::uintptr_t> &identity, const MeshBindings &base,
                                               const std::vector<const Node *> &samples,
                                               NativePreparationBudget *budget) {
    if (budget && capture_ != budget->capture) {
      clear();
      capture_ = budget->capture;
    }
    Work synchronous;
    auto &work = budget ? work_[identity] : synchronous;
    if (!work.plan) {
      work.owner = owner;
      work.plan = std::make_shared<MotionRecipes>();
      work.plan->uniform = base.slots.empty();
      for (const auto *node : samples)
        work.plan->uniform &= !node || !node->mesh ||
                              (node->mesh->slots.empty() && base.full && node->mesh->full &&
                               node->mesh->primitive_count >= base.primitive_count);
      if (!work.plan->uniform)
        work.plan->primitive_recipes.assign(base.primitive_count, -1);
    }
    if (work.done)
      return work.plan;
    const auto selected = base.full ? base.primitive_count : base.indices.size();
    const auto count = work.plan->uniform ? std::min<std::size_t>(selected, 1) : selected;
    std::vector<int> tuple(samples.size() + 1);
    while (work.next < count) {
      const auto end = std::min(count, work.next + 2048);
      while (work.next < end) {
        const auto next = work.next++;
        const auto primitive = base.full ? GA_Index(next) : base.indices[next];
        tuple[0] = base.at(primitive);
        if (tuple[0] < 0)
          continue;
        for (std::size_t step = 0; step < samples.size(); ++step) {
          const auto *node = samples[step];
          tuple[step + 1] = node && node->mesh ? node->mesh->at(primitive) : -1;
        }
        auto [found, inserted] = work.interned.emplace(tuple, int(work.plan->tuples.size()));
        if (inserted)
          work.plan->tuples.push_back(tuple);
        if (work.plan->uniform)
          work.plan->uniform_recipe = found->second;
        else
          work.plan->primitive_recipes.at(std::size_t(primitive)) = found->second;
      }
      if (work.next < count && budget && budget->expired())
        throw NativePreparationPending{};
    }
    work.done = true;
    return work.plan;
  }
  void clear() {
    work_.clear();
    capture_ = 0;
  }

private:
  struct Work {
    // Node identities in the key remain valid because their owning immutable
    // record is retained. No graph callback or borrowed native data survives.
    std::shared_ptr<const Record> owner;
    std::shared_ptr<MotionRecipes> plan;
    std::map<std::vector<int>, int> interned;
    std::size_t next{};
    bool done{};
  };
  std::map<std::vector<std::uintptr_t>, Work> work_;
  std::uint64_t capture_{};
};
std::string signature(const NativeMaterialElements &elements) {
  auto *hash = XXH3_createState();
  if (!hash)
    throw std::bad_alloc();
  XXH3_128bits_reset(hash);
  for (const auto &reference : elements.references) {
    const auto size = reference->key.size();
    XXH3_128bits_update(hash, &size, sizeof(size));
    XXH3_128bits_update(hash, reference->key.data(), size);
  }
  XXH3_128bits_update(hash, &elements.uniform, sizeof(elements.uniform));
  if (!elements.primitive_slots.empty())
    XXH3_128bits_update(hash, elements.primitive_slots.data(), elements.primitive_slots.size() * sizeof(int));
  const auto result = XXH3_128bits_digest(hash);
  XXH3_freeState(hash);
  return std::to_string(result.low64) + ":" + std::to_string(result.high64);
}
} // namespace
struct NativeMaterialMotionContext::Impl {
  std::shared_ptr<const Record> record;
  NativeMaterialPreparation prepare;
  std::shared_ptr<MotionRecipeCache> recipes;
  NativePreparationBudget *budget{}; // Borrowed only by this host-turn context.
  bool sample_roots_current{};
  std::vector<std::shared_ptr<const PreservedDetail>> sample_details;
  std::vector<std::string> sample_warnings;
  std::map<std::vector<std::uintptr_t>, std::shared_ptr<const NativeMaterialMotionProfile>> profiles;
  std::map<std::string, Reference> refreshed;
  // Exact comparisons confirm hash buckets. This bounded table canonicalizes
  // displacement histories across packed profiles without merging nominal
  // surface assignments or discarding their actual source dependencies.
  std::map<std::tuple<std::string, std::size_t, std::string>, std::vector<Reference>> displacement_peers;
  std::size_t peer_count{};
  Reference refresh(const Reference &ref) {
    if (!ref)
      throw std::logic_error("Missing owned binding source");
    const auto [found, inserted] = refreshed.try_emplace(ref->key);
    if (inserted)
      found->second = std::make_shared<const NativeMaterialReference>(refresh_native_material_reference(*ref));
    return found->second;
  }
};
NativeMaterialMotionContext::NativeMaterialMotionContext(std::unique_ptr<Impl> impl) : impl_(std::move(impl)) {}
NativeMaterialMotionContext::~NativeMaterialMotionContext() = default;
const std::shared_ptr<const PreservedDetail> &NativeMaterialMotionContext::nominal() const {
  return impl_->record->nominal;
}
std::shared_ptr<const NativePackedDetails> NativeMaterialMotionContext::packed_details() const {
  return impl_->record->packed;
}
bool NativeMaterialMotionContext::sample_detail(std::size_t sample, std::shared_ptr<const PreservedDetail> &detail,
                                                std::string &warning) const {
  if (!impl_->sample_roots_current)
    return false;
  warning = impl_->sample_warnings.at(sample);
  detail = impl_->sample_details.at(sample);
  return true;
}
const std::set<std::string> &NativeMaterialMotionContext::dependencies() const { return impl_->record->dependencies; }
const std::vector<std::string> &NativeMaterialMotionContext::stylesheet_resources() const {
  return impl_->record->style_resources;
}
std::shared_ptr<const NativeMaterialMotionProfile>
NativeMaterialMotionContext::profile(const std::vector<NativeMaterialRoute> &route) {
  if (!UT_Thread::isMainThread())
    throw std::logic_error("Binding profiles require host graph preparation");
  const auto &record = *impl_->record;
  const auto *nominal = routed(record.root, route);
  if (!nominal || !nominal->mesh)
    return {};
  std::vector<const Node *> samples;
  std::vector<std::uintptr_t> identity{reinterpret_cast<std::uintptr_t>(nominal)};
  for (const auto &sample : record.samples) {
    const auto *node = routed(sample, route);
    samples.push_back(node);
    identity.push_back(reinterpret_cast<std::uintptr_t>(node));
  }
  if (const auto found = impl_->profiles.find(identity); found != impl_->profiles.end())
    return found->second;
  const auto &base = *nominal->mesh;
  const auto recipes = impl_->recipes->prepare(impl_->record, identity, base, samples, impl_->budget);
  auto result = std::make_shared<NativeMaterialMotionProfile>();
  const auto retain_source_issues = [&](const std::shared_ptr<const Node> &root) {
    auto *node = root.get();
    std::size_t depth = 0;
    while (node) {
      for (const auto &issue : node->diagnostics)
        if (std::find(result->diagnostics.begin(), result->diagnostics.end(), issue) == result->diagnostics.end())
          result->diagnostics.push_back(issue);
      if (depth == route.size())
        break;
      const auto &step = route[depth++];
      auto index = step.index;
      if (!step.indexed) {
        const auto found = node->named.find(step.identity);
        if (found == node->named.end())
          break;
        index = found->second;
      }
      if (index >= node->children.size())
        break;
      node = node->children[index].node.get();
    }
  };
  retain_source_issues(record.root);
  for (const auto &sample : record.samples)
    retain_source_issues(sample);
  auto lookup = std::make_shared<NativeMaterialLookup>(*base.lookup);
  lookup->elements.reset();
  for (auto &[assignment, reference] : *lookup)
    reference = *impl_->refresh(std::make_shared<const NativeMaterialReference>(reference));
  const auto prepare_mesh = [&](const MeshBindings &mesh, bool displacement_only) {
    NativeMaterialLookup candidate;
    candidate.displacement_only = displacement_only;
    auto elements = std::make_shared<NativeMaterialElements>();
    for (const auto &ref : mesh.references)
      elements->references.push_back(impl_->refresh(ref));
    candidate.elements = std::move(elements);
    impl_->prepare(candidate);
  };
  prepare_mesh(base, false);
  for (const auto *node : samples)
    if (node && node->mesh)
      prepare_mesh(*node->mesh, true);
  auto elements = std::make_shared<NativeMaterialElements>();
  std::map<std::string, int> slots;
  std::size_t variants = 0;
  bool warned = false;
  std::vector<Reference> nominal_references;
  std::vector<bool> displaced;
  for (const auto &ref : base.references) {
    nominal_references.push_back(impl_->refresh(ref));
    displaced.push_back(impl_->prepare.has_displacement(nominal_references.back()->key));
  }
  std::vector<std::vector<Reference>> sample_references;
  for (const auto *node : samples) {
    auto &refs = sample_references.emplace_back();
    if (node && node->mesh)
      for (const auto &ref : node->mesh->references)
        refs.push_back(impl_->refresh(ref));
  }
  std::vector<Reference> missing_references(samples.size());
  const auto unavailable = [&](std::size_t step) {
    if (missing_references[step])
      return missing_references[step];
    NativeMaterialReference ref;
    ref.key = "native-material:shutter-unavailable:" + record.path + ":" + std::to_string(step);
    ref.path = record.path;
    ref.diagnostic = SourceDiagnostic{"motion.material_binding_source", record.path, ref.key,
                                      "A mesh/packed binding is unavailable at shutter time " +
                                          std::to_string(record.times.at(step)) + " seconds",
                                      "Retaining requested-frame displacement and independently valid geometry motion"};
    return missing_references[step] = std::make_shared<const NativeMaterialReference>(std::move(ref));
  };
  const auto resolve = [&](const std::vector<int> &recipe) {
    const auto nominal_slot = recipe[0];
    if (nominal_slot < 0)
      return -1;
    if (!displaced.at(std::size_t(nominal_slot)))
      return -1;
    auto centre = nominal_references.at(std::size_t(nominal_slot));
    std::vector<Reference> values;
    bool changed = false;
    for (std::size_t step = 0; step < samples.size(); ++step) {
      const auto slot = recipe[step + 1];
      values.push_back(slot < 0 ? unavailable(step) : sample_references[step].at(std::size_t(slot)));
    }
    const auto retain_issues = [&](const Reference &reference) {
      if (impl_->prepare.diagnostics)
        for (const auto &issue : impl_->prepare.diagnostics(reference->key))
          if (std::find(result->diagnostics.begin(), result->diagnostics.end(), issue) == result->diagnostics.end())
            result->diagnostics.push_back(issue);
    };
    for (std::size_t step = 0; step < values.size(); ++step) {
      if (values[step]->diagnostic && values[step]->diagnostic->code == "motion.material_binding_source") {
        NativeMaterialLookup failure;
        failure.emplace(values[step]->assignment(), *values[step]);
        impl_->prepare(failure);
      }
      // Equal recovered height values can share storage without losing
      // the source problem that caused the recovery. Capture warnings
      // before replacing a sample with its nominal/peer identity.
      if (centre->key == values[step]->key || impl_->prepare.same_displacement(centre->key, values[step]->key, step)) {
        retain_issues(values[step]);
        values[step] = centre;
        continue;
      }
      changed = true;
      if (impl_->prepare.displacement_token && !values[step]->diagnostic &&
          impl_->prepare.has_displacement(values[step]->key)) {
        const auto key = std::make_tuple(centre->key, step, impl_->prepare.displacement_token(values[step]->key, step));
        auto found = impl_->displacement_peers.find(key);
        bool shared = false;
        if (found != impl_->displacement_peers.end())
          for (const auto &peer : found->second)
            if (impl_->prepare.same_displacement(peer->key, values[step]->key, step)) {
              retain_issues(values[step]);
              values[step] = peer;
              shared = true;
              break;
            }
        if (!shared && impl_->peer_count < 8192) {
          impl_->displacement_peers[key].push_back(values[step]);
          ++impl_->peer_count;
        }
      }
    }
    int slot = -1;
    if (changed) {
      auto ref = std::make_shared<const NativeMaterialReference>(native_material_timeline(centre, std::move(values)));
      if (const auto found = slots.find(ref->key); found != slots.end())
        slot = found->second;
      else if (variants < 4096) {
        slot = int(elements->references.size());
        slots.emplace(ref->key, slot);
        elements->references.push_back(std::move(ref));
        ++variants;
      } else if (!warned) {
        result->diagnostics.push_back(
            {"material.temporal_capacity",
             record.path,
             {},
             "More than 4096 changing displacement binding histories occur in a mesh component",
             "Retaining requested-frame displacement for additional histories; keeping geometry motion"});
        warned = true;
      }
    }
    return slot;
  };
  std::vector<int> recipe_slots;
  recipe_slots.reserve(recipes->tuples.size());
  for (const auto &recipe : recipes->tuples)
    recipe_slots.push_back(resolve(recipe));
  if (recipes->uniform) {
    if (recipes->uniform_recipe >= 0)
      elements->uniform = recipe_slots.at(std::size_t(recipes->uniform_recipe));
  } else {
    elements->primitive_slots.reserve(recipes->primitive_recipes.size());
    for (const auto recipe : recipes->primitive_recipes)
      elements->primitive_slots.push_back(recipe < 0 ? -1 : recipe_slots.at(std::size_t(recipe)));
  }
  if (!elements->references.empty()) {
    result->signature = signature(*elements);
    lookup->elements = std::move(elements);
    impl_->prepare(*lookup);
  }
  if (!record.restore_warning.empty())
    result->diagnostics.push_back(
        {"motion.nominal_restore",
         record.path,
         {},
         record.restore_warning,
         "Rendering the retained requested-frame geometry; not caching the failed restoration"});
  result->materials = std::move(lookup);
  impl_->profiles.emplace(std::move(identity), result);
  return result;
}
struct NativeMaterialMotionPoses::Impl {
  std::shared_ptr<const Record> record;
};
NativeMaterialMotionPoses::NativeMaterialMotionPoses(std::unique_ptr<Impl> impl) : impl_(std::move(impl)) {}
NativeMaterialMotionPoses::~NativeMaterialMotionPoses() = default;
std::shared_ptr<const NativeMaterialMotionPoses> NativeMaterialMotionContext::pose_source() const {
  auto impl = std::make_unique<NativeMaterialMotionPoses::Impl>();
  impl->record = impl_->record;
  return std::make_shared<const NativeMaterialMotionPoses>(std::move(impl));
}
bool NativeMaterialMotionPoses::poses(std::size_t sample, const GeometryRequest &request,
                                      std::map<std::string, Matrix> &locals, std::set<std::string> &population,
                                      std::string &warning) const {
  const auto &root = impl_->record->samples.at(sample);
  if (!root || !root->unavailable.empty()) {
    warning = root ? root->unavailable : "Shutter binding source is unavailable";
    return false;
  }
  std::function<void(const Node &, const std::string &, const Matrix &)> visit;
  visit = [&](const Node &node, const std::string &prefix, const Matrix &matrix) {
    // Each node was captured with its own effective prototype render kinds. A point
    // instancer's packed-only root mask must not suppress the meshes below it.
    for (std::size_t kind = 0; kind < 4; ++kind)
      if (node.kinds[kind])
        locals.emplace(prefix + "/part:" + geometry_kind_name(NativeGeometryKind(kind)), matrix);
    if (node.kinds[4])
      for (const auto &child : node.children) {
        const auto key = prefix + "/part:packed/packed:" + child.identity;
        population.insert(key);
        if (child.node && child.node->unavailable.empty())
          visit(*child.node, key, multiply(matrix, child.transform));
      }
  };
  visit(*root, request.object_key, identity_matrix);
  return true;
}
std::vector<std::uintptr_t>
NativeMaterialMotionPoses::route_identity(const std::vector<NativeMaterialRoute> &route) const {
  std::vector<std::uintptr_t> result{reinterpret_cast<std::uintptr_t>(impl_->record.get()),
                                     reinterpret_cast<std::uintptr_t>(routed(impl_->record->root, route))};
  for (const auto &sample : impl_->record->samples)
    result.push_back(reinterpret_cast<std::uintptr_t>(routed(sample, route)));
  return result;
}
struct NativeMaterialMotionCache::Impl {
  struct Inputs {
    struct Sample {
      bool acquired{};
      std::shared_ptr<const PreservedDetail> detail;
      std::string warning;
      CaptureState bindings;
    };
    std::int64_t input_detail{};
    std::optional<GeometryRequest> request;
    std::map<double, Sample> samples;
    CaptureState probe, restored_bindings;
    std::shared_ptr<const PreservedDetail> restored;
    std::string restore_warning;
    NativePackedSources owners;
  };
  std::map<std::string, std::shared_ptr<const Record>> records;
  std::map<std::string, Inputs> inputs;
  std::map<std::int64_t, MeshPresenceProbe> mesh_presence;
  MotionSlotCache slots;
  std::shared_ptr<MotionRecipeCache> recipes = std::make_shared<MotionRecipeCache>();
  std::uint64_t capture{};
};
NativeMaterialMotionCache::NativeMaterialMotionCache() : impl_(std::make_unique<Impl>()) {}
NativeMaterialMotionCache::~NativeMaterialMotionCache() = default;
void NativeMaterialMotionCache::retire(const std::set<std::string> &roots) {
  std::erase_if(impl_->records, [&](const auto &entry) { return !roots.contains(entry.first); });
  std::erase_if(impl_->inputs, [&](const auto &entry) { return !roots.contains(entry.first); });
}
void NativeMaterialMotionCache::release_capture_work() {
  impl_->inputs.clear();
  impl_->mesh_presence.clear();
  impl_->slots.clear();
  impl_->recipes->clear();
  impl_->capture = 0;
}
std::shared_ptr<NativeMaterialMotionContext> NativeMaterialMotionCache::prepare(
    NativeRootSource &sop, const GeometryRequest &request, const NativeMotionWindow &window,
    const std::shared_ptr<const PreservedDetail> &nominal, const std::shared_ptr<const NativePackedDetails> &frozen,
    const NativeMaterialPreparation &materials, ExtractionStats &stats, NativeMaterialBindingCache &bindings,
    NativePartitionCache &partitions, NativeStyleCache &styles, NativeStyleBindingCache &style_bindings,
    NativePackedPreparationCache &packed, NativePreparationBudget *budget, std::map<std::string, double> *timings) {
  if (!UT_Thread::isMainThread())
    throw std::logic_error("Material binding acquisition requires the host thread");
  const auto capture = budget ? budget->capture : 0;
  if (impl_->capture != capture) {
    impl_->inputs.clear();
    impl_->mesh_presence.clear();
    impl_->slots.clear();
    impl_->recipes->clear();
    impl_->capture = capture;
  }
  if (!window.enabled || request.motion == NativeGeometryMotion::Deformation ||
      request.material_mode != NativeMaterialMode::Native || !materials || !materials.has_displacement ||
      !materials.same_displacement || (!request.render_types[0] && !request.render_types[4])) {
    impl_->records.erase(request.object_key);
    return {};
  }
  OP_Context context(window.nominal);
  const bool animated_source = sop.isTimeDependent(context);
  if (!animated_source && request.object_material_samples.empty() && request.stylesheet.empty() &&
      !stylesheet_source(*nominal->get(), frozen.get())) {
    impl_->records.erase(request.object_key);
    return {};
  }
  const auto inherited = request.material_assignment();
  const auto current = stamp(sop, *nominal->get());
  const auto make = [&](std::shared_ptr<const Record> record, bool current,
                        std::vector<std::shared_ptr<const PreservedDetail>> details = {},
                        std::vector<std::string> warnings = {}) {
    auto impl = std::make_unique<NativeMaterialMotionContext::Impl>();
    impl->record = std::move(record);
    impl->prepare = materials;
    impl->recipes = impl_->recipes;
    impl->budget = budget;
    impl->sample_roots_current = current;
    if (current && details.empty()) {
      const auto &input = impl_->inputs.at(request.object_key);
      for (const auto at : impl->record->times) {
        if (at == window.nominal) {
          details.push_back(impl->record->nominal);
          warnings.emplace_back();
        } else {
          const auto &sample = input.samples.at(at);
          details.push_back(sample.detail);
          warnings.push_back(sample.warning);
        }
      }
    }
    impl->sample_details = std::move(details);
    impl->sample_warnings = std::move(warnings);
    return std::make_shared<NativeMaterialMotionContext>(std::move(impl));
  };
  if (const auto found = impl_->records.find(request.object_key); found != impl_->records.end()) {
    const auto &value = *found->second;
    const bool owned_capture =
        capture && value.capture == capture && value.input_detail == nominal->get()->getUniqueId();
    if ((owned_capture || (value.stable && !value.scripted && value.stamp == current)) &&
        value.path == request.sop_path && value.inherited == inherited && value.material == request.material_key &&
        value.stylesheet == request.stylesheet && value.object_samples == request.object_material_samples &&
        value.point_instancing == request.point_instancing &&
        value.object_revision == request.object_material_revision && value.times == window.times &&
        value.kinds == request.render_types &&
        value.style_stamp == stylesheet_source_signature(value.style_resources)) {
      // A positive record can contain several prototypes, only some of
      // which needed displacement when captured. Recheck every authored
      // binding so enabling displacement on another prototype rebuilds
      // its selective mesh table too. Surface-only graph edits reuse it.
      if (value.probe_stamp == prepared_probe_stamp(value.probe_references, materials)) {
        ++stats.material_motion_cache_hits;
        return value.positive ? make(found->second, owned_capture) : nullptr;
      }
    }
  }
  // Cache only within an owned acquisition transaction. A host continuation
  // may revisit this root many times, but a later capture must inspect its
  // newly acquired hierarchy (including external/procedural replacements).
  MotionProfile mesh_profile(timings);
  const auto mesh_started = Clock::now();
  const auto record_mesh_profile = [&]() {
    mesh_profile.mark("material_motion_mesh_gate");
    if (timings) {
      auto &maximum = (*timings)["material_motion_mesh_gate_max_slice"];
      maximum = std::max(maximum, std::chrono::duration<double>(Clock::now() - mesh_started).count());
    }
  };
  const auto *full_motion = std::getenv("CYCLES_HOUDINI_FULL_MATERIAL_MOTION");
  bool mesh_possible = true;
  if (!full_motion || std::string_view(full_motion) != "1") {
    MeshPresenceProbe synchronous_probe;
    auto &probe = capture ? impl_->mesh_presence[nominal->get()->getUniqueId()] : synchronous_probe;
    try {
      mesh_possible = probe.prepare(nominal, frozen, partitions, budget);
    } catch (...) {
      record_mesh_profile();
      throw;
    }
  }
  record_mesh_profile();
  if (!mesh_possible) {
    impl_->records.erase(request.object_key);
    return {};
  }
  Impl::Inputs synchronous;
  auto &inputs = budget ? impl_->inputs[request.object_key] : synchronous;
  if (inputs.input_detail != nominal->get()->getUniqueId() || !inputs.request || *inputs.request != request) {
    inputs = Impl::Inputs{};
    inputs.input_detail = nominal->get()->getUniqueId();
    inputs.request = request;
  }
  const auto captured = [&](CaptureState &state, const std::shared_ptr<const PreservedDetail> &detail,
                            const GeometryRequest &binding_request, double at, bool probe, bool retain,
                            const std::shared_ptr<const NativePackedDetails> &frozen = {}) -> const CaptureState & {
    if (!state.sources_valid() || (probe && !probe_valid(state, materials)))
      state = CaptureState{};
    if (retain && !state.acquired)
      state.acquired = std::make_shared<NativePackedDetails>();
    if (!state.root) {
      Capture capture(state, bindings, partitions, inputs.owners, styles, style_bindings, impl_->slots, packed,
                      materials, at, budget, timings);
      capture.probe = probe;
      // A restored nominal root is a full capture without the selective
      // probe. It still needs surface attributes; only off-frame roots
      // may restrict their field demand to displacement dependencies.
      capture.displacement_only = at != window.nominal;
      capture.acquired = state.acquired.get();
      capture.frozen = frozen;
      state.root = capture.node(*detail->get(), binding_request);
    }
    return state;
  };
  auto result = std::make_shared<Record>();
  result->path = request.sop_path;
  result->material = request.material_key;
  result->stylesheet = request.stylesheet;
  result->inherited = inherited;
  result->times = window.times;
  result->kinds = request.render_types;
  result->capture = capture;
  result->input_detail = nominal->get()->getUniqueId();
  result->object_samples = request.object_material_samples;
  result->object_revision = request.object_material_revision;
  result->point_instancing = request.point_instancing;
  result->nominal = nominal;
  const auto &probe = captured(inputs.probe, nominal, request, window.nominal, true, true, frozen);
  result->root = probe.root;
  result->packed = probe.acquired;
  result->scripted = probe.scripted;
  result->dependencies = probe.dependencies;
  for (const auto &[key, ref] : probe.probe_references) {
    result->probe_references.push_back(ref);
    result->probe_stamp.emplace_back(key, probe.probe_displaced.at(key));
  }
  std::set<std::string> style_resources;
  const auto include_sources = [&](const CaptureState &state) {
    for (const auto &[name, version] : state.watched)
      style_resources.insert(name);
  };
  include_sources(probe);
  const auto save_sources = [&]() {
    result->style_resources.assign(style_resources.begin(), style_resources.end());
    result->style_stamp = stylesheet_source_signature(result->style_resources);
  };
  if (!probe.needed) {
    result->stable = result->root->stable;
    result->stamp = current;
    result->root.reset();
    result->nominal.reset();
    result->packed.reset();
    save_sources();
    impl_->records.insert_or_assign(request.object_key, result);
    return {};
  }
  result->positive = true;
  const auto cook = [&](double time) {
    const auto started = Clock::now();
    const auto before = sop.getCookCount();
    OP_Context at(time);
    auto handle = sop.getCookedGeoHandle(at);
    const auto count = std::uint64_t(std::max<exint>(0, sop.getCookCount() - before));
    stats.motion_cooks += count;
    stats.material_motion_cooks += count;
    stats.cooking_seconds += std::chrono::duration<double>(Clock::now() - started).count();
    return handle;
  };
  result->samples.resize(window.times.size());
  std::vector<std::shared_ptr<const PreservedDetail>> sample_details(window.times.size());
  std::vector<std::string> sample_warnings(window.times.size());
  for (std::size_t step = 0; step < window.times.size(); ++step) {
    if (window.times[step] == window.nominal) {
      result->samples[step] = result->root;
      sample_details[step] = result->nominal;
      continue;
    }
    auto &input = inputs.samples[window.times[step]];
    if (!input.acquired) {
      input.acquired = true;
      input.detail = nominal;
      if (animated_source) {
        // A host continuation must never leave the live SOP at a
        // shutter time. Preserve each off-frame detail once and
        // restore the requested frame before matching can yield.
        input.detail.reset();
        try {
          const auto handle = cook(window.times[step]);
          if (handle && sop.error() < UT_ERROR_ABORT)
            input.detail = std::make_shared<PreservedDetail>(handle);
          else
            input.warning =
                "SOP material-binding cook failed at time " + std::to_string(window.times[step]) + " seconds";
        } catch (...) {
          const auto error = std::current_exception();
          cook(window.nominal);
          std::rethrow_exception(error);
        }
        const auto restored = cook(window.nominal);
        inputs.restored.reset();
        inputs.restored_bindings = CaptureState{};
        inputs.restore_warning.clear();
        if (restored && sop.error() < UT_ERROR_ABORT)
          inputs.restored = std::make_shared<PreservedDetail>(restored);
        else
          inputs.restore_warning = "The nominal SOP could not be restored after material binding sampling";
      }
    }
    sample_details[step] = input.detail;
    sample_warnings[step] = input.warning;
    if (!input.detail) {
      auto missing = std::make_shared<Node>();
      missing->unavailable = input.warning;
      missing->stable = false;
      result->samples[step] = std::move(missing);
      result->stable = false;
      continue;
    }
    const auto &sampled = captured(input.bindings, input.detail, sample_object_materials(request, window.times[step]),
                                   window.times[step], false, false);
    result->samples[step] = sampled.root;
    result->scripted |= sampled.scripted;
    result->dependencies.insert(sampled.dependencies.begin(), sampled.dependencies.end());
    include_sources(sampled);
    result->stable &= result->samples[step]->stable;
  }
  if (animated_source) {
    if (!inputs.restored) {
      result->stable = false;
      result->restore_warning = inputs.restore_warning;
    } else {
      result->nominal = inputs.restored;
      const auto &restored = captured(inputs.restored_bindings, result->nominal, request, window.nominal, false, true);
      result->root = restored.root;
      result->packed = restored.acquired;
      result->scripted |= restored.scripted;
      result->dependencies.insert(restored.dependencies.begin(), restored.dependencies.end());
      include_sources(restored);
      // Nominal geometry, bindings and packed transforms must come from the
      // same acquired detail, including when the first probe was selective.
      for (std::size_t step = 0; step < window.times.size(); ++step)
        if (window.times[step] == window.nominal) {
          result->samples[step] = result->root;
          sample_details[step] = result->nominal;
        }
    }
  }
  result->stable &= result->root->stable;
  result->stamp = stamp(sop, *result->nominal->get());
  stats.material_motion_samples += window.times.size();
  save_sources();
  impl_->records.insert_or_assign(request.object_key, result);
  return make(std::move(result), true, std::move(sample_details), std::move(sample_warnings));
}
} // namespace cycles_houdini
