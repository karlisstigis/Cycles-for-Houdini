// SPDX-License-Identifier: Apache-2.0
#include "point_instancer.h"
#include "scene_adapter.h"
#include "deformation_adapter.h"
#include <chrono>
#include <cmath>
#include <cstring>
#include <algorithm>
#include <cctype>
#include <set>
#include <sstream>
#include <GA/GA_AttributeFilter.h>
#include <stdexcept>
#include <GA/GA_AIFSharedStringTuple.h>
#include <GA/GA_AIFTuple.h>
#include <GA/GA_AttributeInstanceMatrix.h>
#include <GA/GA_Handle.h>
#include <GA/GA_Iterator.h>
#include <GU/GU_Detail.h>
#include <GU/GU_PackedGeometry.h>
#include <GU/GU_PrimPacked.h>
#include <OBJ/OBJ_Node.h>
#include <OP/OP_Director.h>
#include <SOP/SOP_Node.h>
#include <UT/UT_Matrix3.h>
#include <UT/UT_Matrix4.h>
#include <UT/UT_Thread.h>

namespace cycles_houdini {
bool native_blend_reference(const std::string &value) {
  const auto separator = value.find('#');
  if (separator == std::string::npos || separator < 6)
    return false;
  std::string extension = value.substr(separator - 6, 6);
  std::transform(extension.begin(), extension.end(), extension.begin(),
                 [](unsigned char c) { return char(std::tolower(c)); });
  return extension == ".blend";
}
void native_source_dependencies(std::vector<std::int64_t> &result, SOP_Node &root); // deformation_adapter.cpp
namespace {
using Clock = std::chrono::steady_clock;
double seconds_since(Clock::time_point t) { return std::chrono::duration<double>(Clock::now() - t).count(); }
// Soho's IFDmisc.absoluteObjectPath and hou.Node.node(): relative to the instancer object.
OBJ_Node *resolve_object(OP_Node &instancer, const char *value, std::string &reason) {
  std::string path = value;
  if (path.rfind("op:", 0) == 0)
    path.erase(0, 3);
  auto *node = instancer.findNode(path.c_str());
  if (!node) {
    reason = "does not exist";
    return nullptr;
  }
  auto *object = node->castToOBJNode();
  if (!object) {
    reason = "is not an object (Houdini instances objects, not SOPs)";
    return nullptr;
  }
  return object;
}
void append_matrix(std::vector<std::int64_t> &stamp, const UT_Matrix4D &m) {
  for (int r = 0; r < 4; ++r)
    for (int c = 0; c < 4; ++c) {
      std::int64_t bits;
      const double v = m(r, c);
      std::memcpy(&bits, &v, sizeof bits);
      stamp.push_back(bits);
    }
}
// Public edit versions of an OBJ, its parents and explicit references (transform sources).
void object_dependencies(std::vector<std::int64_t> &result, OP_Node *root) {
  std::vector<OP_Node *> pending{root};
  std::set<int> seen;
  while (!pending.empty()) {
    auto *node = pending.back();
    pending.pop_back();
    if (!node || !seen.insert(node->getUniqueId()).second || seen.size() > 4096)
      continue;
    result.insert(result.end(), {node->getUniqueId(), std::int64_t(node->getVersionParms()), node->nInputs()});
    for (unsigned input = 0; input < node->nInputs(); ++input)
      pending.push_back(node->getInput(input));
    OP_NodeList references;
    node->getFullOpReferences(references, false);
    for (auto *reference : references)
      pending.push_back(reference);
  }
}
} // namespace
bool NativePointPrototype::operator==(const NativePointPrototype &other) const {
  if (external_members.size() != other.external_members.size())
    return false;
  for (std::size_t i = 0; i < external_members.size(); ++i)
    if (external_members[i] != other.external_members[i] &&
        (!external_members[i] || !other.external_members[i] || *external_members[i] != *other.external_members[i]))
      return false;
  return path == other.path && node_id == other.node_id && source_reference == other.source_reference &&
         source_frame == other.source_frame &&
         (request == other.request || (request && other.request && *request == *other.request));
}
bool NativePointInstancing::operator==(const NativePointInstancing &other) const {
  return mode == other.mode && instancer_path == other.instancer_path && fallback == other.fallback &&
         frame == other.frame && frame_samples == other.frame_samples && prototypes == other.prototypes &&
         keep_attributes == other.keep_attributes;
}
GeometryRequest point_prototype_request(const GeometryRequest &instancer, const GeometryRequest &prototype) {
  auto child = instancer;
  // Geometry interpretation of the prototype (Mantra: subdivision follows the instanced object).
  child.smooth = prototype.smooth;
  child.subdivision = prototype.subdivision;
  child.point_radius = prototype.point_radius;
  child.curve_width = prototype.curve_width;
  child.point_render_mode = prototype.point_render_mode;
  child.point_splat_default_material = prototype.point_splat_default_material;
  child.curve_shape = prototype.curve_shape;
  child.curve_segments = prototype.curve_segments;
  child.render_types = prototype.render_types;
  child.volume_object_space = prototype.volume_object_space;
  child.volume_step_size = prototype.volume_step_size;
  // Nested instancers keep their own table; ordinary prototypes have none.
  child.point_instancing = prototype.point_instancing;
  child.packed_blender = prototype.packed_blender;
  return child;
}

struct NativePointInstanceCache::Impl {
  struct Entry {
    std::vector<std::int64_t> stamp;
    std::shared_ptr<const NativePointInstanceSample> sample;
    std::uint64_t epoch{};
  };
  std::map<std::pair<std::string, double>, Entry> entries;
  std::uint64_t epoch{1};
};
NativePointInstanceCache::NativePointInstanceCache() : impl_(std::make_unique<Impl>()) {}
NativePointInstanceCache::~NativePointInstanceCache() = default;
void NativePointInstanceCache::clear() { impl_->entries.clear(); }
void NativePointInstanceCache::begin_capture() {
  ++impl_->epoch;
  for (auto it = impl_->entries.begin(); it != impl_->entries.end();)
    if (it->second.epoch + 1 < impl_->epoch)
      it = impl_->entries.erase(it);
    else
      ++it;
}
std::shared_ptr<const NativePointInstanceSample>
NativePointInstanceCache::synthesize(SOP_Node &points, const GeometryRequest &request, double time,
                                     const NativePointInstanceSample *nominal, ExtractionStats *stats, int depth) {
  if (!UT_Thread::isMainThread())
    throw std::logic_error("Point instancing requires Houdini's host thread");
  if (!request.point_instancing)
    throw std::logic_error("Point instance synthesis requires an instancing request");
  if (depth > 32)
    throw std::invalid_argument("Point instancing is nested more than 32 levels: " +
                                request.point_instancing->instancer_path);
  const auto &spec = *request.point_instancing;
  auto result = std::make_shared<NativePointInstanceSample>();
  const auto issue = [&](const char *code, std::string message, std::string action) {
    result->diagnostics.push_back(
        {code, spec.instancer_path, request.object_key, std::move(message), std::move(action)});
  };
  auto *instancer = OPgetDirector()->findOBJNode(spec.instancer_path.c_str());
  if (!instancer) {
    result->warning = "The instancing object no longer exists: " + spec.instancer_path;
    return result;
  }
  OP_Context context(time);
  const auto cook_started = Clock::now();
  const auto handle = points.getCookedGeoHandle(context);
  if (stats)
    stats->cooking_seconds += seconds_since(cook_started);
  if (!handle.gdp() || points.error() >= UT_ERROR_ABORT) {
    result->warning = "Instancing SOP cook failed at time " + std::to_string(time) + " seconds";
    return result;
  }
  const PreservedDetail source_lease(handle);
  const GU_Detail &source = *source_lease.get();

  // 1. Unique `instance` strings → prototype slots (the string table, not the points).
  //    >=0 prototype, -1 no value and no Instance Object, -2 unresolvable, -3 not captured.
  const bool members = spec.mode == NativeInstancingMode::Members;
  const GA_Attribute *instance_attribute =
      spec.mode == NativeInstancingMode::Off || members ? nullptr : source.findPointAttribute("instance");
  if (instance_attribute && !instance_attribute->getAIFSharedStringTuple()) {
    issue("instancing.invalid_attribute", "The instance point attribute must be a string attribute",
          "Using the Instance Object for every point");
    instance_attribute = nullptr;
  }
  std::map<std::int64_t, int> by_node;
  std::map<std::pair<std::string, double>, int> by_external;
  std::set<std::string> external_references;
  for (std::size_t i = 0; i < spec.prototypes.size(); ++i) {
    const auto &prototype = spec.prototypes[i];
    if (prototype.source_frame) {
      by_external.emplace(std::make_pair(prototype.source_reference, *prototype.source_frame), int(i));
      external_references.insert(prototype.source_reference);
    } else
      by_node.emplace(prototype.node_id, int(i));
  }
  std::vector<int> table_slot;
  std::vector<std::string> table_reason;
  std::vector<std::string> table_external;
  if (instance_attribute) {
    const auto *aif = instance_attribute->getAIFSharedStringTuple();
    table_slot.assign(std::size_t(std::max<GA_Size>(0, aif->getTableEntries(instance_attribute))), -2);
    table_reason.resize(table_slot.size());
    table_external.resize(table_slot.size());
    for (auto it = aif->begin(instance_attribute); !it.atEnd(); it.advance()) {
      const auto slot_index = std::size_t(it.getHandle());
      if (slot_index >= table_slot.size())
        continue;
      const char *value = it.getString() ? it.getString() : "";
      if (!*value) {
        table_slot[slot_index] = spec.fallback;
        continue;
      }
      if (native_blend_reference(value)) {
        external_references.insert(value);
        table_slot[slot_index] = -4;
        table_external[slot_index] = value;
        table_reason[slot_index] =
            "'" + std::string(value) + "' has no prepared Blender prototype for this point's index";
        continue;
      }
      std::string reason;
      auto *object = resolve_object(*instancer, value, reason);
      if (!object) {
        table_reason[slot_index] = "'" + std::string(value) + "' " + reason;
        continue;
      }
      const auto found = by_node.find(object->getUniqueId());
      if (found != by_node.end()) {
        table_slot[slot_index] = found->second;
        continue;
      }
      table_slot[slot_index] = -3;
      table_reason[slot_index] = "'" + std::string(value) + "' is referenced only away from the requested frame";
    }
  }
  GA_ROHandleS instance_values(instance_attribute);
  const auto *index_attribute = external_references.empty() ? nullptr : source.findPointAttribute("index");
  const bool invalid_index = index_attribute && (index_attribute->getStorageClass() != GA_STORECLASS_FLOAT ||
                                                 index_attribute->getTupleSize() != 1);
  GA_ROHandleD frame_values(invalid_index ? nullptr : index_attribute);
  if (invalid_index)
    issue("instancing.invalid_blender_index", "Blender instance index must be a scalar float point attribute",
          "Omitting external Blender instances; retaining ordinary OBJ instances");
  const auto sampled_frame = spec.frame_samples.find(time);
  const double default_frame = sampled_frame != spec.frame_samples.end() ? sampled_frame->second : spec.frame;
  const auto fallback_slot = [&]() {
    if (spec.fallback < 0 || spec.prototypes[std::size_t(spec.fallback)].source_reference.empty())
      return spec.fallback;
    const auto found = by_external.find({spec.prototypes[std::size_t(spec.fallback)].source_reference, default_frame});
    return found == by_external.end() ? -3 : found->second;
  };
  const auto slot_of = [&](GA_Offset point) -> int {
    if (!instance_values.isValid())
      return fallback_slot();
    const auto index = instance_values.getIndex(point);
    if (index < 0)
      return fallback_slot();
    if (std::size_t(index) >= table_slot.size())
      return -2;
    const auto slot = table_slot[std::size_t(index)];
    if (slot != -4)
      return slot == spec.fallback ? fallback_slot() : slot;
    if (invalid_index)
      return -2;
    const double frame = frame_values.isValid() ? double(frame_values.get(point)) : default_frame;
    if (!std::isfinite(frame))
      return -2;
    const auto found = by_external.find({table_external[std::size_t(index)], frame});
    return found == by_external.end() ? -3 : found->second;
  };

  // 2. Used prototypes: cooked detail and world transform at this time.
  struct Prototype {
    GU_ConstDetailHandle handle;
    std::unique_ptr<PreservedDetail> lease;
    UT_Matrix4D world{1.0};
    bool used{};
    std::string failure;
    GU_PrimPacked *first{};
    std::vector<NativePointInstanceSample::DirectPlacement> direct;
  };
  std::vector<Prototype> prototypes(spec.prototypes.size());
  // Empty values may have no string-table entry at all, so the fallback is always prepared.
  if (spec.fallback >= 0)
    prototypes[std::size_t(spec.fallback)].used = true;
  if (members)
    for (auto &prototype : prototypes)
      prototype.used = true;
  for (std::size_t i = 0; i < spec.prototypes.size(); ++i)
    if (spec.prototypes[i].source_frame)
      prototypes[i].used = true;
  for (const auto slot : table_slot)
    if (slot >= 0)
      prototypes[std::size_t(slot)].used = true;
  std::vector<std::int64_t> stamp{points.getUniqueId(),
                                  points.getCookCount(),
                                  source.getUniqueId(),
                                  std::int64_t(source.getMetaCacheCount()),
                                  instancer->getUniqueId(),
                                  std::int64_t(instancer->getVersionParms()),
                                  nominal && nominal->handle.gdp() ? nominal->handle.gdp()->getUniqueId() : -1};
  for (std::size_t i = 0; i < spec.prototypes.size(); ++i) {
    auto &prototype = prototypes[i];
    if (!prototype.used)
      continue;
    const auto &entry = spec.prototypes[i];
    if (!entry.source_reference.empty()) {
      for (const auto &member : entry.external_members) {
        if (!member || !member->external_mesh)
          continue;
        NativePointInstanceSample::DirectPlacement placement;
        placement.request = member;
        placement.transform = member->transform;
        placement.identity = member->object_key;
        if (member->blender_object) {
          // Blender's Object/Instancer attribute fields come first; template point fields follow.
          placement.attributes = member->blender_object->attributes;
          // Blender's per-object values can change while the capsule, key and transform stay.
          const auto &values = *member->blender_object;
          stamp.push_back(std::int64_t(values.random_id));
          stamp.push_back(std::int64_t(values.visibility) | (std::int64_t(values.holdout) << 8) |
                          (std::int64_t(values.shadow_catcher) << 9) | (std::int64_t(values.pass_index) << 10));
          for (const auto value : {values.color.x, values.color.y, values.color.z, values.alpha}) {
            std::int32_t bits;
            std::memcpy(&bits, &value, sizeof bits);
            stamp.push_back(bits);
          }
          for (const auto &field : values.attributes) {
            stamp.push_back(std::int64_t(std::hash<std::string>{}(field.name)));
            for (const auto value : {field.value.x, field.value.y, field.value.z}) {
              std::int32_t bits;
              std::memcpy(&bits, &value, sizeof bits);
              stamp.push_back(bits);
            }
          }
        }
        prototype.direct.push_back(std::move(placement));
        stamp.push_back(std::int64_t(reinterpret_cast<std::uintptr_t>(member->external_mesh.get())));
        // Transform-only and material-only animation can share the
        // same capsule while retaining distinct object/slot bindings.
        stamp.push_back(std::int64_t(std::hash<std::string>{}(member->object_key)));
        stamp.push_back(std::int64_t(member->external_material_paths.size()));
        for (const auto &path : member->external_material_paths)
          stamp.push_back(std::int64_t(std::hash<std::string>{}(path)));
        for (const auto value : member->transform) {
          std::int32_t bits;
          std::memcpy(&bits, &value, sizeof bits);
          stamp.push_back(bits);
        }
      }
      continue;
    }
    auto *object = OPgetDirector()->findOBJNode(entry.path.c_str());
    if (!object || object->getUniqueId() != entry.node_id || !entry.request) {
      prototype.failure = "no longer exists";
      continue;
    }
    OP_Context at(time);
    if (!object->getWorldTransform(prototype.world, at)) {
      prototype.failure = "has a transform that cannot be evaluated";
      continue;
    }
    // A subnet prototype places its members at their own world transforms (which include the subnet's).
    if (entry.request->point_instancing && entry.request->point_instancing->mode == NativeInstancingMode::Members)
      prototype.world.identity();
    auto *sop = OPgetDirector()->findSOPNode(entry.request->sop_path.c_str());
    if (!sop) {
      prototype.failure = "has no render SOP";
      continue;
    }
    if (entry.request->point_instancing) {
      const auto nested = synthesize(*sop, *entry.request, time, nullptr, stats, depth + 1);
      result->diagnostics.insert(result->diagnostics.end(), nested->diagnostics.begin(), nested->diagnostics.end());
      if (!nested->handle.gdp()) {
        prototype.failure = nested->warning;
        continue;
      }
      prototype.handle = nested->handle;
      prototype.direct = nested->direct;
    } else {
      const auto started = Clock::now();
      prototype.handle = sop->getCookedGeoHandle(at);
      if (stats)
        stats->cooking_seconds += seconds_since(started);
      if (!prototype.handle.gdp() || sop->error() >= UT_ERROR_ABORT) {
        prototype.handle = {};
        prototype.failure = "has a SOP that failed to cook";
        continue;
      }
    }
    prototype.lease = std::make_unique<PreservedDetail>(prototype.handle);
    stamp.insert(stamp.end(),
                 {std::int64_t(i), sop->getUniqueId(), sop->getCookCount(), prototype.handle.gdp()->getUniqueId(),
                  std::int64_t(prototype.handle.gdp()->getMetaCacheCount())});
    append_matrix(stamp, prototype.world);
  }
  { // The request itself: a changed table, mode or kept-attribute set must not reuse a cached detail.
    std::string fingerprint = std::to_string(int(spec.mode)) + "|" + std::to_string(spec.fallback);
    const auto append_frame = [&](double frame) {
      std::int64_t bits;
      std::memcpy(&bits, &frame, sizeof bits);
      stamp.push_back(bits);
    };
    // Timeline time participates only when it actually selects an external
    // frame. Authored f@index is absolute, including fractional frames.
    if (!external_references.empty() && !frame_values.isValid())
      append_frame(default_frame);
    for (const auto &prototype : spec.prototypes) {
      fingerprint += "|" + std::to_string(prototype.node_id) + ":" + prototype.path + ":" + prototype.source_reference;
      stamp.push_back(prototype.source_frame.has_value());
      if (prototype.source_frame)
        append_frame(*prototype.source_frame);
    }
    for (const auto &name : spec.keep_attributes)
      fingerprint += "|keep:" + name;
    stamp.push_back(std::int64_t(std::hash<std::string>{}(fingerprint)));
    stamp.push_back(std::int64_t(fingerprint.size()));
  }
  // The time slot is storage, not source identity. A static point source and
  // fixed external samples retain the exact same synthesized detail across
  // Houdini time. The stamp includes cooked source IDs, member transforms,
  // source frames and the nominal detail used for shutter population rules.
  // Never alias a changed shutter recipe merely because its time matches.
  for (auto &[slot, cached] : impl_->entries)
    if (slot.first == spec.instancer_path && cached.sample && cached.stamp == stamp) {
      cached.epoch = impl_->epoch;
      ++counters_.cache_hits;
      return cached.sample;
    }
  auto &entry = impl_->entries[{spec.instancer_path, time}];
  entry.epoch = impl_->epoch;
  if (entry.sample && entry.stamp == stamp) {
    ++counters_.cache_hits;
    return entry.sample;
  }

  // 3. Owned detail: the instancer's points with point/detail attributes, one packed primitive each.
  const auto build_started = Clock::now();
  auto *dest = new GU_Detail;
  GU_DetailHandle owned;
  owned.allocateAndSet(dest);
  // Attributes consumed by the instance transform are not carried by the instances (Copy to Points drops
  // them too); per-instance work downstream then scales with the attributes that matter.
  std::string trimmed;
  for (const auto *name : {"orient", "N", "up", "pscale", "scale", "rot", "trans", "pivot", "transform", "instance"})
    if (std::find(spec.keep_attributes.begin(), spec.keep_attributes.end(), name) == spec.keep_attributes.end())
      trimmed += std::string(trimmed.empty() ? "" : " ") + name;
  const bool packed_points = std::any_of(prototypes.begin(), prototypes.end(),
                                         [](const auto &prototype) { return bool(prototype.handle.gdp()); });
  // External meshes own their point fields in DirectPlacement. They do not
  // need a second copy of the instancer's points in an otherwise empty detail.
  // A subnet is a synthetic list of member objects, not a copy of the first
  // member's points. Carrying that SOP's point attributes onto the newly
  // appended member points creates spurious default-valued instance fields.
  if (!members && packed_points) {
    if (trimmed.empty())
      dest->replaceWithPoints(source);
    else {
      const auto skip = GA_AttributeFilter::selectByPattern(trimmed.c_str());
      dest->replaceWithPoints(source, &skip);
      // Skipped attributes may exist uninitialized afterwards; remove them on every owner.
      std::istringstream words(trimmed);
      std::string word;
      while (words >> word)
        for (const auto owner : {GA_ATTRIB_POINT, GA_ATTRIB_VERTEX, GA_ATTRIB_PRIMITIVE, GA_ATTRIB_DETAIL})
          dest->destroyAttribute(owner, word.c_str());
    }
    if (dest->getNumPoints() != source.getNumPoints())
      throw std::logic_error("Point instancing lost instancing points");
  }
  static constexpr const char *binding_names[] = {"shop_materialpath", "material_override", "material_stylesheet"};
  for (const auto *name : binding_names)
    dest->destroyAttribute(GA_ATTRIB_DETAIL, name); // Not instancer semantics in Houdini.
  GA_RWHandleI prototype_index(dest->addIntTuple(GA_ATTRIB_PRIMITIVE, point_prototype_attribute, 1));
  std::vector<std::pair<GA_ROHandleS, GA_RWHandleS>> bindings; // Point bindings become primitive bindings.
  for (const auto *name : binding_names)
    if (const auto *attribute = source.findPointAttribute(name); attribute && attribute->getAIFSharedStringTuple())
      bindings.emplace_back(GA_ROHandleS(attribute), GA_RWHandleS(dest->addStringTuple(GA_ATTRIB_PRIMITIVE, name, 1)));
  const GA_AttributeInstanceMatrix matrices(source.pointAttribs());
  // Nominal prototypes by explicit identity (name before id, as packed identity does).
  const GA_ROHandleS names(source.findPointAttribute("name"));
  const GA_ROHandleID ids(source.findPointAttribute("id"));
  std::map<std::string, int> nominal_names;
  std::map<exint, int> nominal_ids;
  if (nominal && nominal->handle.gdp() && (names.isValid() || ids.isValid())) {
    const auto &n = *nominal->handle.gdp();
    const GA_ROHandleI n_slot(n.findPrimitiveAttribute(point_prototype_attribute));
    const GA_ROHandleS n_names(n.findPointAttribute("name"));
    const GA_ROHandleID n_ids(n.findPointAttribute("id"));
    if (n_slot.isValid())
      for (GA_Iterator it(n.getPrimitiveRange()); !it.atEnd(); ++it) {
        const auto *primitive = dynamic_cast<const GU_PrimPacked *>(n.getPrimitive(*it));
        if (!primitive)
          continue;
        const auto point = primitive->getPointOffset(0);
        if (names.isValid()) {
          if (n_names.isValid())
            nominal_names.emplace(n_names.get(point).toStdString(), n_slot.get(*it));
        } else if (n_ids.isValid())
          nominal_ids.emplace(n_ids.get(point), n_slot.get(*it));
      }
  }
  std::map<std::string, std::size_t> omitted;
  std::size_t changed = 0;
  const auto nominal_slot = [&](GA_Offset point) -> int { // -1: unknown identity or no nominal sample.
    if (!nominal || !nominal->handle.gdp())
      return -1;
    if (names.isValid()) {
      const auto f = nominal_names.find(names.get(point).toStdString());
      return f != nominal_names.end() ? f->second : -1;
    }
    if (ids.isValid()) {
      const auto f = nominal_ids.find(ids.get(point));
      return f != nominal_ids.end() ? f->second : -1;
    }
    return -1;
  };
  const auto count = source.getNumPoints();
  std::map<std::string, GA_Index> direct_identities;
  bool reported_direct_style = false, reported_direct_velocity = false;
  const GA_ROHandleS direct_material(source.findPointAttribute("shop_materialpath")),
      direct_overrides(source.findPointAttribute("material_override")),
      direct_styles(source.findPointAttribute("material_stylesheet"));
  const GA_ROHandleV3 direct_velocity(source.findPointAttribute("v"));
  const auto *direct_accel_attribute = source.findPointAttribute("accel");
  const GA_ROHandleV3 direct_acceleration(direct_accel_attribute &&
                                                  direct_accel_attribute->getStorageClass() == GA_STORECLASS_FLOAT &&
                                                  direct_accel_attribute->getTupleSize() == 3
                                              ? direct_accel_attribute
                                              : nullptr);
  struct DirectField {
    const GA_Attribute *attribute;
    std::string name;
    int components;
  };
  std::vector<DirectField> direct_fields;
  // Resolve the schema once, rather than classifying every source attribute
  // again for every member of every point's external collection.
  if (std::any_of(prototypes.begin(), prototypes.end(),
                  [](const auto &prototype) { return !prototype.direct.empty(); }))
    for (GA_AttributeDict::iterator it = source.pointAttribs().begin(GA_SCOPE_PUBLIC); !it.atEnd(); ++it) {
      const auto *field = it.attrib();
      const auto name = field->getName().toStdString();
      if (name == "P" || name == "instance" || name == "v")
        continue;
      const bool consumed = name == "orient" || name == "N" || name == "up" || name == "pscale" || name == "scale" ||
                            name == "rot" || name == "trans" || name == "pivot" || name == "transform";
      if (consumed &&
          std::find(spec.keep_attributes.begin(), spec.keep_attributes.end(), name) == spec.keep_attributes.end())
        continue;
      const auto components = field->getTupleSize();
      if (components < 1 || components > 3 || !field->getAIFTuple() ||
          (field->getStorageClass() != GA_STORECLASS_FLOAT && field->getStorageClass() != GA_STORECLASS_INT))
        continue;
      direct_fields.push_back({field, name, int(components)});
    }
  const auto append_direct = [&](const Prototype &prototype, const UT_Matrix4D &full, GA_Offset point, GA_Index index,
                                 int slot) {
    if (prototype.direct.empty())
      return;
    Matrix parent{};
    for (int r = 0; r < 3; ++r)
      for (int c = 0; c < 4; ++c)
        parent[r * 4 + c] = float(full(c, r));
    std::string identity = names.isValid() ? names.get(point).toStdString()
                           : ids.isValid() ? std::to_string(ids.get(point))
                                           : std::to_string(index);
    // Point identity follows Houdini's name/id preference. Disambiguate duplicate authored identities.
    if (names.isValid() || ids.isValid()) {
      const auto [found_identity, unique_identity] = direct_identities.emplace(identity, index);
      if (!unique_identity && found_identity->second != index)
        identity += "/point:" + std::to_string(index);
    }
    bool fields_read = false, velocity_valid = false;
    std::string material, overrides;
    Float3 velocity{}, acceleration{};
    std::vector<UniformAttribute> attributes;
    for (const auto &member : prototype.direct) {
      auto placed = member;
      placed.point = std::size_t(index);
      placed.prototype = std::size_t(slot);
      placed.identity = identity + "/member:" + member.identity;
      for (int r = 0; r < 3; ++r) {
        for (int c = 0; c < 3; ++c) {
          placed.transform[r * 4 + c] = 0;
          for (int k = 0; k < 3; ++k)
            placed.transform[r * 4 + c] += parent[r * 4 + k] * member.transform[k * 4 + c];
        }
        placed.transform[r * 4 + 3] = parent[r * 4 + 3];
        for (int k = 0; k < 3; ++k)
          placed.transform[r * 4 + 3] += parent[r * 4 + k] * member.transform[k * 4 + 3];
      }
      const auto &m = placed.transform;
      const double determinant = double(m[0]) * (double(m[5]) * m[10] - double(m[6]) * m[9]) -
                                 double(m[1]) * (double(m[4]) * m[10] - double(m[6]) * m[8]) +
                                 double(m[2]) * (double(m[4]) * m[9] - double(m[5]) * m[8]);
      if (determinant == 0 || !std::all_of(m.begin(), m.end(), [](float v) { return std::isfinite(v); })) {
        ++omitted["have a zero or non-finite external member transform"];
        continue;
      }
      // All collection members inherit the same point fields. Read them once
      // after finding a valid member, preserving omission/diagnostic semantics.
      if (!fields_read) {
        fields_read = true;
        if (direct_material.isValid())
          material = direct_material.get(point).toStdString();
        if (direct_overrides.isValid())
          overrides = direct_overrides.get(point).toStdString();
        if (!reported_direct_style && direct_styles.isValid() && direct_styles.get(point).length()) {
          issue("material.external_stylesheet",
                "Houdini point material style sheets are not evaluated against external Blender mesh faces",
                "Using Blender's material slots and supported point material assignments and overrides");
          reported_direct_style = true;
        }
        if (direct_velocity.isValid()) {
          const auto v = direct_velocity.get(point);
          velocity_valid = std::isfinite(v.x()) && std::isfinite(v.y()) && std::isfinite(v.z());
          if (velocity_valid)
            velocity = {float(v.x()), float(v.y()), float(v.z())};
          else if (!reported_direct_velocity) {
            issue("motion.external_velocity", "Point v contains non-finite values",
                  "Holding those points at their requested-frame placement; retaining parent motion");
            reported_direct_velocity = true;
          }
        }
        if (velocity_valid && direct_acceleration.isValid()) {
          const auto a = direct_acceleration.get(point);
          if (std::isfinite(a.x()) && std::isfinite(a.y()) && std::isfinite(a.z()))
            acceleration = {float(a.x()), float(a.y()), float(a.z())};
        }
        attributes.reserve(direct_fields.size());
        for (const auto &field : direct_fields) {
          fpreal32 values[3]{};
          if (!field.attribute->getAIFTuple()->get(field.attribute, point, values, field.components) ||
              !std::isfinite(values[0]) || !std::isfinite(values[1]) || !std::isfinite(values[2]))
            continue;
          UniformAttribute attribute;
          attribute.name = field.name;
          attribute.components = field.components;
          attribute.value = {values[0], values[1], values[2]};
          attributes.push_back(std::move(attribute));
        }
      }
      if (direct_material.isValid())
        placed.binding.path = material;
      if (!overrides.empty())
        placed.binding.overrides.push_back(overrides);
      if (velocity_valid) {
        placed.velocity = velocity;
        placed.acceleration = acceleration;
      }
      if (&member == &prototype.direct.back() && placed.attributes.empty())
        placed.attributes = std::move(attributes);
      else
        placed.attributes.insert(placed.attributes.end(), attributes.begin(), attributes.end());
      result->direct.push_back(std::move(placed));
      ++result->instances;
    }
  };
  for (GA_Index index = 0; spec.mode != NativeInstancingMode::Off && !members && index < count; ++index) {
    const auto point = source.pointOffset(index);
    const int slot = slot_of(point);
    const int expected = nominal_slot(point);
    const bool same_external_reference =
        expected >= 0 && slot >= 0 && !spec.prototypes[std::size_t(expected)].source_reference.empty() &&
        spec.prototypes[std::size_t(expected)].source_reference == spec.prototypes[std::size_t(slot)].source_reference;
    if (expected >= 0 && expected != slot && !same_external_reference) {
      ++changed;
      continue;
    } // Absent here: population rules freeze it.
    if (slot < 0) {
      // An empty value without an Instance Object is simply not an instance (Mantra renders nothing,
      // and for geometry objects with s@instance such points are ordinary geometry points).
      if (slot == -1)
        continue;
      {
        const auto string_index = instance_values.isValid() ? instance_values.getIndex(point) : -1;
        ++omitted[string_index >= 0 && std::size_t(string_index) < table_reason.size() &&
                          !table_reason[std::size_t(string_index)].empty()
                      ? table_reason[std::size_t(string_index)]
                      : std::string("reference an unavailable Instance Object")];
      }
      continue;
    }
    auto &prototype = prototypes[std::size_t(slot)];
    if (!prototype.handle.gdp() && prototype.direct.empty()) {
      ++omitted["'" + spec.prototypes[std::size_t(slot)].path + "' " + prototype.failure];
      continue;
    }
    UT_Matrix4D point_matrix(1.0);
    if (spec.mode != NativeInstancingMode::Off)
      matrices.getMatrix(point_matrix, UT_Vector3D(source.getPos3(point)), point, 1.0);
    const UT_Matrix4D full = prototype.world * point_matrix; // Row vectors: prototype first (Mantra-verified).
    { // Zero scale (e.g. pscale 0) renders nothing in Houdini; it must not reject the whole scene.
      bool finite = true;
      for (int r = 0; r < 4; ++r)
        for (int c = 0; c < 4; ++c)
          finite &= std::isfinite(float(full(r, c)));
      if (!finite || UT_Matrix3D(full).determinant() == 0) {
        ++omitted["have a zero or non-finite instance scale (Houdini renders nothing for them)"];
        continue;
      }
    }
    append_direct(prototype, full, point, index, slot);
    if (!prototype.handle.gdp())
      continue;
    const auto target = dest->pointOffset(index);
    GU_PrimPacked *primitive = nullptr;
    if (!prototype.first) {
      primitive = GU_PackedGeometry::packGeometry(*dest, prototype.handle, target);
      if (!primitive)
        throw std::runtime_error("Could not create a packed instance of " + spec.prototypes[std::size_t(slot)].path);
      prototype.first = primitive;
    } else {
      primitive = GU_PrimPacked::build(*dest, prototype.first->getTypeId(), target);
      primitive->setImplementation(prototype.first->sharedImplementation());
    }
    primitive->setLocalTransform(UT_Matrix3D(full));
    UT_Vector3D translate;
    full.getTranslates(translate);
    dest->setPos3(target, translate);
    const auto offset = primitive->getMapOffset();
    prototype_index.set(offset, slot);
    for (auto &[from, to] : bindings)
      to.set(offset, from.get(point));
    ++result->instances;
  }
  // `off`: the Instance Object replaces the instancer once; template points are ignored (Mantra).
  // Members: every member object of a subnet prototype, once, at its world transform.
  std::vector<int> once;
  if (spec.mode == NativeInstancingMode::Off && spec.fallback >= 0)
    once.push_back(spec.fallback);
  if (members)
    for (std::size_t i = 0; i < prototypes.size(); ++i)
      once.push_back(int(i));
  for (const auto slot : once) {
    auto &prototype = prototypes[std::size_t(slot)];
    if (!prototype.handle.gdp() && prototype.direct.empty()) {
      ++omitted["'" + spec.prototypes[std::size_t(slot)].path + "' " + prototype.failure];
      continue;
    }
    if (UT_Matrix3D(prototype.world).determinant() == 0) {
      ++omitted["have a zero or non-finite instance scale (Houdini renders nothing for them)"];
      continue;
    }
    const auto point = dest->appendPoint();
    // Off/members use a synthetic point; no template point attributes participate.
    if (!prototype.direct.empty()) {
      for (const auto &member : prototype.direct) {
        auto placed = member;
        placed.point = std::size_t(dest->pointIndex(point));
        placed.prototype = std::size_t(slot);
        placed.identity = std::to_string(slot) + "/member:" + member.identity;
        result->direct.push_back(std::move(placed));
        ++result->instances;
      }
    }
    if (!prototype.handle.gdp())
      continue;
    auto *primitive = GU_PackedGeometry::packGeometry(*dest, prototype.handle, point);
    if (!primitive)
      throw std::runtime_error("Could not create a packed instance of " + spec.prototypes[std::size_t(slot)].path);
    primitive->setLocalTransform(UT_Matrix3D(prototype.world));
    UT_Vector3D translate;
    prototype.world.getTranslates(translate);
    dest->setPos3(point, translate);
    prototype_index.set(primitive->getMapOffset(), slot);
    ++result->instances;
  }
  for (const auto *name : binding_names)
    dest->destroyAttribute(GA_ATTRIB_POINT, name);
  dest->bumpAllDataIds();
  for (const auto &[reason, points] : omitted)
    issue("instancing.unavailable_prototype", std::to_string(points) + " point(s): " + reason,
          "Omitting these instances; rendering the others");
  if (changed)
    issue("instancing.prototype_changed",
          std::to_string(changed) + " instance(s) change their instanced object during the shutter",
          "Rendering them at the requested frame without instance motion");
  if (spec.mode == NativeInstancingMode::Full)
    issue("instancing.full_expressions",
          "Full point instancing renders like Fast point instancing: per-point instancepoint() expressions on object "
          "parameters are not evaluated",
          "Use point attributes (instance, shop_materialpath, material_override, Cd, cycles_dielectric_priority) for "
          "per-instance variation");
  using DirectBindingKey = std::tuple<std::uintptr_t, NativeMaterialAssignment, AttributeLayout>;
  std::map<DirectBindingKey, std::size_t> direct_bindings;
  for (std::size_t index = 0; index < result->direct.size(); ++index) {
    auto &placement = result->direct[index];
    AttributeLayout layout;
    for (const auto &attribute : placement.attributes)
      layout.emplace(attribute.name, attribute.components);
    auto [found, inserted] =
        direct_bindings.try_emplace(DirectBindingKey{reinterpret_cast<std::uintptr_t>(placement.request.get()),
                                                     placement.binding, std::move(layout)},
                                    result->direct_bindings.size());
    if (inserted)
      result->direct_bindings.push_back(index);
    placement.binding_index = found->second;
  }
  counters_.seconds += seconds_since(build_started);
  ++counters_.syntheses;
  counters_.instances += result->instances;
  result->handle = GU_ConstDetailHandle(owned);
  entry.stamp = std::move(stamp);
  entry.sample = result;
  return result;
}

NativeRootSource::NativeRootSource(SOP_Node &sop, const GeometryRequest &request, NativePointInstanceCache *cache,
                                   double nominal, ExtractionStats *stats, Report report)
    : sop_(&sop), request_(&request), cache_(cache), nominal_(nominal), stats_(stats), report_(std::move(report)) {
  if (request.point_instancing && !cache_)
    throw std::logic_error("Point instancing requires the adapter's instance cache");
}
bool NativeRootSource::instancer() const { return bool(request_->point_instancing); }
int NativeRootSource::getUniqueId() const { return sop_->getUniqueId(); }
GU_ConstDetailHandle NativeRootSource::getCookedGeoHandle(OP_Context &context) {
  if (!instancer()) {
    failed_ = false;
    return sop_->getCookedGeoHandle(context);
  }
  const double time = context.getTime();
  const NativePointInstanceSample *reference = nullptr;
  if (time != nominal_) {
    if (!nominal_sample_)
      nominal_sample_ = cache_->synthesize(*sop_, *request_, nominal_, nullptr, stats_);
    reference = nominal_sample_.get();
  }
  auto sample = cache_->synthesize(*sop_, *request_, time, reference, stats_);
  latest_sample_ = sample;
  ++syntheses_;
  if (time == nominal_)
    nominal_sample_ = sample;
  if (report_)
    for (const auto &issue : sample->diagnostics)
      report_(issue);
  failed_ = !sample->handle.gdp();
  if (failed_ && report_)
    report_({"geometry.cook_failed", request_->sop_path, request_->geometry_key, sample->warning,
             "Omitting this instancer at this time"});
  return sample->handle;
}
UT_ErrorSeverity NativeRootSource::error() const { return failed_ ? UT_ERROR_ABORT : sop_->error(); }
exint NativeRootSource::getCookCount() const { return sop_->getCookCount() + (instancer() ? syntheses_ : 0); }
bool NativeRootSource::isTimeDependent(OP_Context &context) {
  if (sop_->isTimeDependent(context) || !instancer())
    return sop_->isTimeDependent(context);
  std::vector<const GeometryRequest *> pending{request_};
  while (!pending.empty()) {
    const auto *request = pending.back();
    pending.pop_back();
    if (!request->point_instancing)
      continue;
    if (auto *owner = OPgetDirector()->findOBJNode(request->point_instancing->instancer_path.c_str());
        owner && owner->isTimeDependent(context))
      return true;
    for (const auto &prototype : request->point_instancing->prototypes) {
      if (auto *object = OPgetDirector()->findOBJNode(prototype.path.c_str());
          object && object->isTimeDependent(context))
        return true;
      if (!prototype.request)
        continue;
      if (auto *sop = OPgetDirector()->findSOPNode(prototype.request->sop_path.c_str());
          sop && sop->isTimeDependent(context))
        return true;
      pending.push_back(prototype.request.get());
    }
  }
  return false;
}
void NativeRootSource::dependencies(std::vector<std::int64_t> &result) const {
  native_source_dependencies(result, *sop_);
  if (!instancer())
    return;
  std::vector<const GeometryRequest *> pending{request_};
  while (!pending.empty()) {
    const auto *request = pending.back();
    pending.pop_back();
    if (!request->point_instancing)
      continue;
    object_dependencies(result, OPgetDirector()->findOBJNode(request->point_instancing->instancer_path.c_str()));
    for (const auto &prototype : request->point_instancing->prototypes) {
      result.push_back(prototype.node_id);
      object_dependencies(result, OPgetDirector()->findOBJNode(prototype.path.c_str()));
      if (!prototype.request)
        continue;
      if (auto *sop = OPgetDirector()->findSOPNode(prototype.request->sop_path.c_str()))
        native_source_dependencies(result, *sop);
      else
        result.push_back(-1);
      pending.push_back(prototype.request.get());
    }
  }
}
} // namespace cycles_houdini
