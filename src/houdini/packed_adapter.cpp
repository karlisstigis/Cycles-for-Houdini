// SPDX-License-Identifier: Apache-2.0
#include "packed_adapter.h"
#include "scene_adapter.h"
#include "packed_blender_reference.h"
#include "identity_attribute.h"
#include "packed_fragments.h"
#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <exception>
#include <vector>
#include <xmmintrin.h>
#include <set>
#include <stdexcept>
#include <unordered_map>
#include <tuple>
#include <GA/GA_Handle.h>
#include <GA/GA_Iterator.h>
#include <GU/GU_PrimPacked.h>
#include <GU/GU_PackedDisk.h>
#include <GU/GU_PackedGeometry.h>
#include <GU/GU_PackedFragment.h>
#include <UT/UT_ParallelUtil.h>
#include <UT/UT_Thread.h>

namespace cycles_houdini {
namespace {
// CYCLES_HOUDINI_SERIAL_CONVERSION=1 keeps the original serial rows loop (equivalence tests).
bool serial_packed_rows() {
  const auto *value = std::getenv("CYCLES_HOUDINI_SERIAL_CONVERSION");
  return value && std::strcmp(value, "1") == 0;
}
} // namespace
std::shared_ptr<const PreservedDetail> NativePackedSources::retain(std::shared_ptr<const PreservedDetail> detail) {
  if (!UT_Thread::isMainThread())
    throw std::logic_error("Packed preparation requires the host thread");
  if (const auto found = details_.find(detail->get()->getUniqueId()); found != details_.end())
    return found->second;
  return details_.emplace(detail->get()->getUniqueId(), std::move(detail)).first->second;
}
std::shared_ptr<const PreservedDetail> NativePackedSources::retain(const GU_ConstDetailHandle &handle) {
  if (!UT_Thread::isMainThread())
    throw std::logic_error("Packed preparation requires the host thread");
  if (!handle)
    throw std::invalid_argument("Packed primitive has no supported in-memory detail");
  const auto identity = handle.gdp()->getUniqueId();
  if (const auto found = details_.find(identity); found != details_.end())
    return found->second;
  auto result = std::make_shared<PreservedDetail>(handle);
  details_.emplace(identity, result);
  return result;
}
struct NativePackedPreparationCache::Impl {
  using Key = std::tuple<std::int64_t, std::uintptr_t, int, NativeMaterialAssignment, std::string, std::string,
                         std::string, int, unsigned, bool>;
  struct Work {
    std::shared_ptr<NativePackedInstances> result = std::make_shared<NativePackedInstances>();
    std::shared_ptr<const NativePackedDetails> frozen;
    std::unordered_map<std::string, std::size_t> identities;
    NativeMaterialBindings::Values bindings;
    std::size_t next{}, next_identity{};
    // Parallel rows (prepare): rows [read_first, read_end) are read; those
    // from `next` on wait for their serial commit.
    std::size_t read_first{}, read_end{};
    std::vector<std::exception_ptr> row_errors;
    std::vector<const std::shared_ptr<const PreservedDetail> *> row_ledger;
    std::vector<char> row_blender;
    bool done{};
  };
  std::map<Key, Work> work;
  std::uint64_t capture{};
};
NativePackedPreparationCache::NativePackedPreparationCache() : impl_(std::make_unique<Impl>()) {}
NativePackedPreparationCache::~NativePackedPreparationCache() = default;
void NativePackedPreparationCache::clear() {
  impl_->work.clear();
  impl_->capture = 0;
}
std::shared_ptr<const NativePackedInstances>
NativePackedPreparationCache::prepare(const GU_Detail &source, const GeometryRequest &request,
                                      const NativeGeometrySelection &selection, NativePackedSources &sources,
                                      const std::shared_ptr<const NativePackedDetails> &frozen, bool capture_velocity,
                                      NativePreparationBudget *budget, bool build_named_index) {
  if (!UT_Thread::isMainThread())
    throw std::logic_error("Packed preparation requires the host thread");
  if (selection.kind != NativeGeometryKind::Packed)
    throw std::invalid_argument("Packed preparation requires a packed selection");
  if (budget && impl_->capture != budget->capture) {
    clear();
    impl_->capture = budget->capture;
  }
  // Nominal scene rows need identities for duplicate recovery, but only the
  // shutter-field hierarchy consumes the name-to-row lookup. Keep that index
  // optional and keyed independently from the resumable row preparation.
  const auto *legacy = std::getenv("CYCLES_HOUDINI_PACKED_NAMED_INDEX_LEGACY");
  build_named_index = build_named_index || (legacy && std::strcmp(legacy, "1") == 0);
  const NativeMaterialBindings policy(source, request, &selection);
  const Impl::Key key{source.getUniqueId(),
                      reinterpret_cast<std::uintptr_t>(frozen.get()),
                      int(request.material_mode),
                      request.material_assignment(),
                      request.native_styles ? request.native_styles->signature : std::string{},
                      request.sop_path,
                      request.geometry_key,
                      capture_velocity ? (request.motion == NativeGeometryMotion::Acceleration ? 2 : 1) : 0,
                      policy.ignored_overrides(),
                      build_named_index};
  Impl::Work synchronous;
  auto &work = budget ? impl_->work[key] : synchronous;
  // Retain the actual ledger owner, not just its address in the key. A new
  // owned ledger cannot accidentally reuse the identity of retired storage.
  if (!work.frozen)
    work.frozen = frozen;
  if (work.done) {
    for (const auto &[id, detail] : work.result->prototypes)
      sources.retain(detail);
    return work.result;
  }
  GA_ROHandleS names(identity_attribute(source.findPrimitiveAttribute("name"), GA_STORECLASS_STRING));
  GA_ROHandleID ids(identity_attribute(source.findPrimitiveAttribute("id"), GA_STORECLASS_INT));
  GA_ROHandleS string_ids(identity_attribute(source.findPrimitiveAttribute("id"), GA_STORECLASS_STRING));
  GA_ROHandleS point_names(identity_attribute(source.findPointAttribute("name"), GA_STORECLASS_STRING));
  GA_ROHandleID point_ids(identity_attribute(source.findPointAttribute("id"), GA_STORECLASS_INT));
  GA_ROHandleS point_string_ids(identity_attribute(source.findPointAttribute("id"), GA_STORECLASS_STRING));
  GA_ROHandleS bindings(source.findPrimitiveAttribute("shop_materialpath"));
  GA_ROHandleI point_prototypes(request.point_instancing ? source.findPrimitiveAttribute(point_prototype_attribute)
                                                         : nullptr);
  // Prototype object bindings, built once per call rather than once per instance.
  std::vector<NativeMaterialAssignment> prototype_bindings;
  if (point_prototypes.isValid())
    for (const auto &prototype : request.point_instancing->prototypes)
      prototype_bindings.push_back(prototype.request ? prototype.request->material_assignment()
                                                     : NativeMaterialAssignment{});
  const auto *velocity_attribute = capture_velocity ? source.findPointAttribute("v") : nullptr;
  GA_ROHandleV3 velocity(velocity_attribute);
  const auto *angular_attribute = capture_velocity ? source.findPointAttribute("w") : nullptr;
  GA_ROHandleV3 angular(angular_attribute);
  // Acceleration mode bends the path by point accel; anything else ignores it.
  const auto *accel_attribute = capture_velocity && request.motion == NativeGeometryMotion::Acceleration
                                    ? source.findPointAttribute("accel")
                                    : nullptr;
  GA_ROHandleV3 accel(accel_attribute && accel_attribute->getStorageClass() == GA_STORECLASS_FLOAT &&
                              accel_attribute->getTupleSize() == 3
                          ? accel_attribute
                          : nullptr);
  NativeMaterialBindings native_bindings(source, request, &selection, &work.bindings);
  auto &result = work.result->values;
  const auto count = std::size_t(selection.full_detail ? source.getNumPrimitives() : selection.primitives.entries());
  if (result.empty())
    result.reserve(count);
  // Rows in parallel (T1, 17 Nov 2026). With a frozen ledger every per-row
  // read is a const HDK read (primitive lookup, packed implementation casts,
  // GA_RO handle reads of name/id/v/w/accel/P/point prototype, the packed
  // full transform) or a lookup in the immutable ledger. Each row writes only
  // its own slot. Fixed blocks of 4096 rows from index 0, parallel over the
  // block index, with the caller's SSE control word: values are bitwise the
  // serial ones. Everything with shared state (identity counts, the retained
  // prototype owners, the material-assignment cache, the prototype table,
  // diagnostics that format primitive details) and the first error are
  // committed serially in index order; the commit checks the budget like the
  // serial loop, and rows read ahead wait in the continuation. Without a
  // ledger, or with CYCLES_HOUDINI_SERIAL_CONVERSION=1, the serial loop below
  // runs unchanged.
  if (frozen && !serial_packed_rows() && work.next < count) {
    constexpr std::size_t block = 4096;
    constexpr std::size_t commit_batch = 256;
    constexpr std::size_t turn_blocks = 16; // Rows read per parallel turn: 65,536.
    const auto read_row = [&](std::size_t index, NativePackedInstance &instance,
                              const std::shared_ptr<const PreservedDetail> *&entry, char &is_blender) {
      const auto offset =
          selection.full_detail ? source.primitiveOffset(GA_Index(index)) : selection.primitives(exint(index));
      const auto *primitive = dynamic_cast<const GU_PrimPacked *>(source.getPrimitive(offset));
      if (!primitive)
        throw std::invalid_argument("Mixed packed/non-packed geometry requires a packed selection: " +
                                    request.sop_path);
      if (request.material_mode == NativeMaterialMode::Reject && bindings.isValid() && !bindings.get(offset).isEmpty())
        throw std::invalid_argument("Packed shop_materialpath bindings require native material assignment mode or an "
                                    "explicit uniform override: " +
                                    request.sop_path);
      instance.primitive_offset = offset;
      const auto point = primitive->getPointOffset(0);
      const auto *implementation = primitive->sharedImplementation();
      instance.stable_memory_source = dynamic_cast<const GU_PackedGeometry *>(implementation) ||
                                      dynamic_cast<const GU_PackedFragment *>(implementation);
      if (velocity_attribute) {
        const char *invalid = nullptr;
        if (velocity_attribute->getStorageClass() != GA_STORECLASS_FLOAT || velocity_attribute->getTupleSize() != 3)
          invalid = "v must be a three-component float point attribute";
        else if (!velocity.isValid())
          invalid = "v cannot be read as a vector";
        else {
          const auto value = velocity.get(point);
          const Float3 v{float(value.x()), float(value.y()), float(value.z())};
          if (!std::isfinite(v.x) || !std::isfinite(v.y) || !std::isfinite(v.z))
            invalid = "v must contain finite representable values";
          else if (v.x != 0 || v.y != 0 || v.z != 0)
            instance.velocity = v;
        }
        if (invalid)
          instance.invalid_velocity = std::make_unique<SourceDiagnostic>(
              SourceDiagnostic{"motion.packed_velocity", request.sop_path, request.geometry_key,
                               std::string(invalid) + " (point " + std::to_string(source.pointIndex(point)) + ")",
                               "Rendering this instance without its packed velocity; retaining independent parent, OBJ "
                               "and prototype motion"});
      }
      if (accel.isValid() && !instance.invalid_velocity) {
        const auto value = accel.get(point);
        const Float3 a{float(value.x()), float(value.y()), float(value.z())};
        if (std::isfinite(a.x) && std::isfinite(a.y) && std::isfinite(a.z) && (a.x != 0 || a.y != 0 || a.z != 0))
          instance.acceleration = a;
      }
      const auto *blender = packed_blender_reference(*primitive);
      is_blender = blender != nullptr;
      if (angular_attribute && !blender && !instance.invalid_velocity) {
        const char *invalid = nullptr;
        if (angular_attribute->getStorageClass() != GA_STORECLASS_FLOAT || angular_attribute->getTupleSize() != 3)
          invalid = "w must be a three-component float point attribute";
        else if (!angular.isValid())
          invalid = "w cannot be read as a vector";
        else {
          const auto value = angular.get(point);
          const Float3 w{float(value.x()), float(value.y()), float(value.z())};
          if (!std::isfinite(w.x) || !std::isfinite(w.y) || !std::isfinite(w.z))
            invalid = "w must contain finite representable values";
          else if (w.x != 0 || w.y != 0 || w.z != 0) {
            instance.angular_velocity = w;
            const auto position = source.getPos3(point);
            instance.pivot = {float(position.x()), float(position.y()), float(position.z())};
          }
        }
        if (invalid) {
          instance.velocity.reset();
          instance.acceleration.reset();
          instance.invalid_velocity = std::make_unique<SourceDiagnostic>(
              SourceDiagnostic{"motion.packed_velocity", request.sop_path, request.geometry_key,
                               std::string(invalid) + " (point " + std::to_string(source.pointIndex(point)) + ")",
                               "Rendering this instance without its packed velocity; retaining independent parent, OBJ "
                               "and prototype motion"});
        }
      }
      // "index:<row>": the serial loop names a row by result.size(), which is its index.
      if (blender) {
        const auto string_id = string_ids.isValid() ? string_ids.get(offset).toStdString() : std::string{};
        const auto point_string_id =
            point_string_ids.isValid() ? point_string_ids.get(point).toStdString() : std::string{};
        if (!string_id.empty())
          instance.identity = "id:string:" + std::to_string(string_id.size()) + ":" + string_id;
        else if (ids.isValid())
          instance.identity = "id:" + std::to_string(ids.get(offset));
        else if (!point_string_id.empty())
          instance.identity = "id:string:" + std::to_string(point_string_id.size()) + ":" + point_string_id;
        else if (point_ids.isValid())
          instance.identity = "id:" + std::to_string(point_ids.get(point));
        else {
          instance.identity = "index:" + std::to_string(index);
          instance.index_identity = true;
        }
      } else {
        const auto name = names.isValid()
                              ? names.get(offset).toStdString()
                              : (point_names.isValid() ? point_names.get(point).toStdString() : std::string{});
        if (!name.empty())
          instance.identity = "name:" + std::to_string(name.size()) + ":" + name;
        else if (ids.isValid())
          instance.identity = "id:" + std::to_string(ids.get(offset));
        else if (point_ids.isValid())
          instance.identity = "id:" + std::to_string(point_ids.get(point));
        else {
          instance.identity = "index:" + std::to_string(index);
          instance.index_identity = true;
        }
      }
      if (blender) {
        instance.blender_reference = blender->reference();
        instance.blender_frame = blender->sourceFrame();
      } else {
        const auto found = frozen->find({source.getUniqueId(), offset});
        if (found == frozen->end())
          throw std::logic_error("Packed prototype was not acquired before conversion");
        entry = &found->second;
      }
      UT_Matrix4D local;
      primitive->getFullTransform4(local);
      for (int row = 0; row < 3; ++row)
        for (int col = 0; col < 4; ++col)
          instance.transform[row * 4 + col] = float(local(col, row));
      if (point_prototypes.isValid()) {
        const auto slot = point_prototypes.get(offset);
        if (slot < 0 || std::size_t(slot) >= request.point_instancing->prototypes.size() ||
            !request.point_instancing->prototypes[std::size_t(slot)].request)
          throw std::logic_error("Point instance references an unknown prototype: " + request.sop_path);
        instance.point_prototype = slot;
      }
    };
    // Consecutive rows usually share a prototype: retain() returns the same
    // owner for the same detail, so call it again only when the detail changes.
    const PreservedDetail *retained_input = nullptr;
    std::shared_ptr<const PreservedDetail> retained_output;
    std::int64_t last_prototype = 0;
    bool any_prototype = false;
    const auto control = _mm_getcsr();
    while (work.next < count) {
      if (work.next == work.read_end) {
        const auto first = work.next;
        // Earlier roots can consume the whole host time slice on every retry.
        // Read one commit batch in that case so the saved cursor always advances.
        const auto last =
            std::min(count, budget && budget->expired() ? first + commit_batch : (first / block + turn_blocks) * block);
        // Rows are written in place: one row per index, all rows before
        // `first` are committed and none after it exist yet.
        if (result.size() != first)
          throw std::logic_error("Packed row preparation lost its position");
        result.resize(last);
        work.read_first = first;
        work.row_errors.assign(last - first, nullptr);
        work.row_ledger.assign(last - first, nullptr);
        work.row_blender.assign(last - first, 0);
        const auto run_block = [&](std::size_t begin, std::size_t end) {
          for (auto index = begin; index < end; ++index) {
            try {
              read_row(index, result[index], work.row_ledger[index - first], work.row_blender[index - first]);
            } catch (...) {
              work.row_errors[index - first] = std::current_exception();
            }
          }
        };
        const auto first_block = first / block, end_block = (last + block - 1) / block;
        if (end_block - first_block <= 1)
          run_block(first, last);
        else
          UTisolate([&] {
            UTparallelFor(UT_BlockedRange<std::size_t>(first_block, end_block, 1),
                          [&](const UT_BlockedRange<std::size_t> &range) {
                            const auto saved = _mm_getcsr();
                            _mm_setcsr(control);
                            for (auto b = range.begin(); b < range.end(); ++b)
                              run_block(std::max(first, b * block), std::min(last, (b + 1) * block));
                            _mm_setcsr(saved);
                          });
          });
        work.read_end = last;
      }
      while (work.next < work.read_end) {
        const auto slot = work.next - work.read_first;
        if (work.row_errors[slot]) {
          result.resize(work.next); // Rows before the first failure, as the serial loop leaves them.
          work.read_end = work.next;
          std::rethrow_exception(work.row_errors[slot]);
        }
        auto &instance = result[work.next];
        if (!instance.index_identity)
          ++work.identities[instance.identity];
        if (const auto *entry = work.row_ledger[slot]; entry && *entry) {
          if (entry->get() != retained_input) {
            retained_input = entry->get();
            retained_output = sources.retain(*entry);
          }
          instance.prototype = retained_output;
        }
        if (!instance.prototype && !work.row_blender[slot]) {
          const auto *primitive = static_cast<const GU_PrimPacked *>(source.getPrimitive(instance.primitive_offset));
          const auto fragment = packed_fragment_problem(*primitive);
          std::string reason = "Packed primitive " + std::to_string(source.primitiveIndex(instance.primitive_offset)) +
                               (fragment.empty() ? " has no accessible geometry detail" : ": " + fragment);
          if (const auto *disk = dynamic_cast<const GU_PackedDisk *>(primitive->sharedImplementation()))
            reason += " (" + disk->filename().toStdString() + ")";
          instance.unavailable = std::make_unique<SourceDiagnostic>(SourceDiagnostic{
              "geometry.packed_unavailable", request.sop_path, request.geometry_key, std::move(reason),
              "Omitting only unavailable packed contents; rendering valid instances and other components"});
        }
        if (request.material_mode == NativeMaterialMode::Native)
          instance.material_assignment =
              instance.point_prototype >= 0
                  ? native_bindings.assignment(instance.primitive_offset,
                                               prototype_bindings[std::size_t(instance.point_prototype)])
                  : native_bindings.assignment(instance.primitive_offset);
        if (instance.prototype) {
          const auto id = instance.prototype->get()->getUniqueId();
          if (!any_prototype || id != last_prototype) {
            work.result->prototypes.try_emplace(id, instance.prototype);
            last_prototype = id;
            any_prototype = true;
          }
        }
        ++work.next;
        if (work.next % commit_batch == 0 && work.next < count && budget && budget->expired())
          throw NativePreparationPending{};
      }
    }
    work.row_errors = {};
    work.row_ledger = {};
    work.row_blender = {};
    if (budget && budget->expired() && work.next_identity < result.size())
      throw NativePreparationPending{};
  }
  while (work.next < count) {
    const auto end = std::min(count, work.next + 256);
    while (work.next < end) {
      const auto index = work.next;
      const auto offset =
          selection.full_detail ? source.primitiveOffset(GA_Index(index)) : selection.primitives(exint(index));
      const auto *primitive = dynamic_cast<const GU_PrimPacked *>(source.getPrimitive(offset));
      if (!primitive)
        throw std::invalid_argument("Mixed packed/non-packed geometry requires a packed selection: " +
                                    request.sop_path);
      if (request.material_mode == NativeMaterialMode::Reject && bindings.isValid() && !bindings.get(offset).isEmpty())
        throw std::invalid_argument("Packed shop_materialpath bindings require native material assignment mode or an "
                                    "explicit uniform override: " +
                                    request.sop_path);
      NativePackedInstance instance;
      instance.primitive_offset = offset;
      const auto point = primitive->getPointOffset(0);
      const auto *implementation = primitive->sharedImplementation();
      instance.stable_memory_source = dynamic_cast<const GU_PackedGeometry *>(implementation) ||
                                      dynamic_cast<const GU_PackedFragment *>(implementation);
      if (velocity_attribute) {
        const char *invalid = nullptr;
        if (velocity_attribute->getStorageClass() != GA_STORECLASS_FLOAT || velocity_attribute->getTupleSize() != 3)
          invalid = "v must be a three-component float point attribute";
        else if (!velocity.isValid())
          invalid = "v cannot be read as a vector";
        else {
          const auto value = velocity.get(point);
          const Float3 v{float(value.x()), float(value.y()), float(value.z())};
          if (!std::isfinite(v.x) || !std::isfinite(v.y) || !std::isfinite(v.z))
            invalid = "v must contain finite representable values";
          else if (v.x != 0 || v.y != 0 || v.z != 0)
            instance.velocity = v;
        }
        if (invalid)
          instance.invalid_velocity = std::make_unique<SourceDiagnostic>(
              SourceDiagnostic{"motion.packed_velocity", request.sop_path, request.geometry_key,
                               std::string(invalid) + " (point " + std::to_string(source.pointIndex(point)) + ")",
                               "Rendering this instance without its packed velocity; retaining independent parent, OBJ "
                               "and prototype motion"});
      }
      if (accel.isValid() && !instance.invalid_velocity) {
        const auto value = accel.get(point);
        const Float3 a{float(value.x()), float(value.y()), float(value.z())};
        if (std::isfinite(a.x) && std::isfinite(a.y) && std::isfinite(a.z) && (a.x != 0 || a.y != 0 || a.z != 0))
          instance.acceleration = a;
      }
      const auto *blender = packed_blender_reference(*primitive);
      // Linked Blender instances follow Blender and take no angular velocity.
      if (angular_attribute && !blender && !instance.invalid_velocity) {
        const char *invalid = nullptr;
        if (angular_attribute->getStorageClass() != GA_STORECLASS_FLOAT || angular_attribute->getTupleSize() != 3)
          invalid = "w must be a three-component float point attribute";
        else if (!angular.isValid())
          invalid = "w cannot be read as a vector";
        else {
          const auto value = angular.get(point);
          const Float3 w{float(value.x()), float(value.y()), float(value.z())};
          if (!std::isfinite(w.x) || !std::isfinite(w.y) || !std::isfinite(w.z))
            invalid = "w must contain finite representable values";
          else if (w.x != 0 || w.y != 0 || w.z != 0) {
            instance.angular_velocity = w;
            const auto position = source.getPos3(point);
            instance.pivot = {float(position.x()), float(position.y()), float(position.z())};
          }
        }
        if (invalid) {
          instance.velocity.reset();
          instance.acceleration.reset();
          instance.invalid_velocity = std::make_unique<SourceDiagnostic>(
              SourceDiagnostic{"motion.packed_velocity", request.sop_path, request.geometry_key,
                               std::string(invalid) + " (point " + std::to_string(source.pointIndex(point)) + ")",
                               "Rendering this instance without its packed velocity; retaining independent parent, OBJ "
                               "and prototype motion"});
        }
      }
      if (blender) {
        // Blender reference names select Copy to Points pieces. Copies of one
        // piece deliberately share that name, so it is not an instance ID.
        const auto string_id = string_ids.isValid() ? string_ids.get(offset).toStdString() : std::string{};
        const auto point_string_id =
            point_string_ids.isValid() ? point_string_ids.get(point).toStdString() : std::string{};
        if (!string_id.empty())
          instance.identity = "id:string:" + std::to_string(string_id.size()) + ":" + string_id;
        else if (ids.isValid())
          instance.identity = "id:" + std::to_string(ids.get(offset));
        else if (!point_string_id.empty())
          instance.identity = "id:string:" + std::to_string(point_string_id.size()) + ":" + point_string_id;
        else if (point_ids.isValid())
          instance.identity = "id:" + std::to_string(point_ids.get(point));
        else {
          instance.identity = "index:" + std::to_string(result.size());
          instance.index_identity = true;
        }
      } else {
        const auto name = names.isValid()
                              ? names.get(offset).toStdString()
                              : (point_names.isValid() ? point_names.get(point).toStdString() : std::string{});
        if (!name.empty())
          instance.identity = "name:" + std::to_string(name.size()) + ":" + name;
        else if (ids.isValid())
          instance.identity = "id:" + std::to_string(ids.get(offset));
        else if (point_ids.isValid())
          instance.identity = "id:" + std::to_string(point_ids.get(point));
        else {
          // Ordinary packed SOPs need no authored identity attribute. The
          // enclosing object/packed hierarchy namespaces this local index.
          // As with Blender's implicit instance indices, correspondence is
          // by order; explicit names/IDs preserve identity through reorders.
          instance.identity = "index:" + std::to_string(result.size());
          instance.index_identity = true;
        }
      }
      // Generated primitive indices are already unique. Building and probing
      // a string map for them costs O(n log n) without adding any validation.
      if (!instance.index_identity)
        ++work.identities[instance.identity];
      if (blender) {
        instance.blender_reference = blender->reference();
        instance.blender_frame = blender->sourceFrame();
      } else if (frozen) {
        const auto found = frozen->find({source.getUniqueId(), offset});
        if (found == frozen->end())
          throw std::logic_error("Packed prototype was not acquired before conversion");
        if (found->second)
          instance.prototype = sources.retain(found->second);
      } else {
        const auto handle = packed_contents(source, selection, index, *primitive);
        if (handle)
          instance.prototype = sources.retain(handle);
      }
      if (!instance.prototype && !blender) {
        const auto fragment = packed_fragment_problem(*primitive);
        std::string reason = "Packed primitive " + std::to_string(source.primitiveIndex(offset)) +
                             (fragment.empty() ? " has no accessible geometry detail" : ": " + fragment);
        if (const auto *disk = dynamic_cast<const GU_PackedDisk *>(primitive->sharedImplementation()))
          reason += " (" + disk->filename().toStdString() + ")";
        instance.unavailable = std::make_unique<SourceDiagnostic>(SourceDiagnostic{
            "geometry.packed_unavailable", request.sop_path, request.geometry_key, std::move(reason),
            "Omitting only unavailable packed contents; rendering valid instances and other components"});
      }
      UT_Matrix4D local;
      primitive->getFullTransform4(local);
      for (int row = 0; row < 3; ++row)
        for (int col = 0; col < 4; ++col)
          instance.transform[row * 4 + col] = float(local(col, row));
      if (point_prototypes.isValid()) {
        const auto slot = point_prototypes.get(offset);
        if (slot < 0 || std::size_t(slot) >= request.point_instancing->prototypes.size() ||
            !request.point_instancing->prototypes[std::size_t(slot)].request)
          throw std::logic_error("Point instance references an unknown prototype: " + request.sop_path);
        instance.point_prototype = slot;
      }
      // Point instances: point binding > prototype OBJ material > instancer OBJ material (Mantra order).
      if (request.material_mode == NativeMaterialMode::Native)
        instance.material_assignment =
            instance.point_prototype >= 0
                ? native_bindings.assignment(offset, prototype_bindings[std::size_t(instance.point_prototype)])
                : native_bindings.assignment(offset);
      if (instance.prototype)
        work.result->prototypes.try_emplace(instance.prototype->get()->getUniqueId(), instance.prototype);
      result.push_back(std::move(instance));
      ++work.next;
    }
    if (budget && budget->expired() && (work.next < count || work.next_identity < result.size()))
      throw NativePreparationPending{};
  }
  // Resolve every member of an ambiguous group, including its first member.
  // Never merge separate instances or trust duplicate IDs for shutter motion.
  if (work.identities.empty())
    work.next_identity = result.size();
  while (work.next_identity < result.size()) {
    const auto end = std::min(result.size(), work.next_identity + 512);
    while (work.next_identity < end) {
      const auto i = work.next_identity++;
      auto &instance = result[i];
      if (instance.index_identity)
        continue;
      if (work.identities.at(instance.identity) > 1) {
        instance.identity = "ambiguous:index:" + std::to_string(i);
        instance.index_identity = instance.ambiguous_identity = true;
      } else if (build_named_index)
        work.result->named.emplace(instance.identity, i);
    }
    if (work.next_identity < result.size() && budget && budget->expired())
      throw NativePreparationPending{};
  }
  work.done = true;
  for (const auto &[id, detail] : work.result->prototypes)
    sources.retain(detail);
  return work.result;
}
} // namespace cycles_houdini
