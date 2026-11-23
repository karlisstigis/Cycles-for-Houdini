// SPDX-License-Identifier: Apache-2.0
#include "material_assignment.h"
#include "scene_adapter.h"
#include <algorithm>
#include <limits>
#include <stdexcept>
#include <GA/GA_Iterator.h>
#include <GA/GA_AIFSharedStringTuple.h>
#include <GEO/GEO_PrimPart.h>
#include <GEO/GEO_PrimPoly.h>
#include <GEO/GEO_Curve.h>
#include <GU/GU_Detail.h>
#include <GU/GU_PrimVDB.h>
#include <GEO/GEO_PrimVolume.h>
#include <OP/OP_Director.h>
#include <OP/OP_Operator.h>
#include <UT/UT_Thread.h>
#include <UT/UT_XXHash.h>

namespace cycles_houdini {
namespace {
constexpr std::size_t override_variant_limit = 4096;
constexpr std::size_t override_literal_limit = 8 * 1024 * 1024;
bool usable_override(const std::string &value) {
  return value.size() <= 65536 && value.find('\0') == std::string::npos;
}
void append_override(NativeMaterialAssignment &assignment, const GA_ROHandleS &handle, GA_Offset offset) {
  if (!handle.isValid() || offset == GA_INVALID_OFFSET)
    return;
  auto value = handle.get(offset).toStdString();
  if (!value.empty() && value != "{}" && usable_override(value))
    assignment.overrides.push_back(std::move(value));
}
const GA_Attribute *string_attribute(const GA_Attribute *attribute, const char *name) {
  if (attribute && (attribute->getStorageClass() != GA_STORECLASS_STRING || attribute->getTupleSize() != 1))
    return nullptr;
  return attribute;
}
std::vector<std::string> strings(const GA_Attribute *attribute, const char *name, const GA_Range *selection = nullptr,
                                 std::size_t limit = std::numeric_limits<std::size_t>::max()) {
  attribute = string_attribute(attribute, name);
  if (!attribute)
    return {};
  GA_ROHandleS handle(attribute);
  if (!handle.isValid())
    throw std::invalid_argument(std::string("Material ") + name + " requires supported Houdini string storage");
  std::set<GA_StringIndexType> indices;
  std::vector<std::string> result;
  // Shared-string table extraction can omit default handles. Read actual
  // active values on an ID change and copy each interned value once. No
  // vertex data is read or hashed.
  for (GA_Iterator it(selection ? *selection : GA_Range(attribute->getIndexMap())); !it.atEnd(); ++it)
    if (indices.insert(handle.getIndex(*it)).second) {
      if (indices.size() > limit)
        break;
      result.push_back(handle.get(*it).toStdString());
    }
  return result;
}
bool same_plain_reference(const NativeMaterialReference &a, const NativeMaterialReference &b) {
  return a.key == b.key && a.path == b.path && a.session_id == b.session_id && a.identity == b.identity &&
         a.volume == b.volume && a.diagnostic == b.diagnostic && a.overrides == b.overrides &&
         a.surface_overrides == b.surface_overrides && a.displacement_overrides == b.displacement_overrides;
}
} // namespace
bool NativeMaterialReference::operator==(const NativeMaterialReference &other) const {
  if (!same_plain_reference(*this, other) || bool(timeline) != bool(other.timeline))
    return false;
  if (!timeline || timeline == other.timeline)
    return true;
  if (!timeline->nominal || !other.timeline->nominal ||
      !same_plain_reference(*timeline->nominal, *other.timeline->nominal) ||
      timeline->samples.size() != other.timeline->samples.size())
    return false;
  for (std::size_t i = 0; i < timeline->samples.size(); ++i)
    if (!timeline->samples[i] || !other.timeline->samples[i] ||
        !same_plain_reference(*timeline->samples[i], *other.timeline->samples[i]))
      return false;
  return true;
}
bool same_native_material_resource(const NativeMaterialReference &a, const NativeMaterialReference &b) {
  if (a == b)
    return true;
  if (!a.key.starts_with("native-material:") || a.diagnostic || b.diagnostic)
    return false;
  if (a.key != b.key || a.session_id != b.session_id || a.identity != b.identity || a.volume != b.volume ||
      a.overrides != b.overrides || a.surface_overrides != b.surface_overrides ||
      a.displacement_overrides != b.displacement_overrides || bool(a.timeline) != bool(b.timeline))
    return false;
  if (a.timeline) {
    if (!a.timeline->nominal || !b.timeline->nominal ||
        !same_native_material_resource(*a.timeline->nominal, *b.timeline->nominal) ||
        a.timeline->samples.size() != b.timeline->samples.size())
      return false;
    for (std::size_t i = 0; i < a.timeline->samples.size(); ++i)
      if (!a.timeline->samples[i] || !b.timeline->samples[i] ||
          !same_native_material_resource(*a.timeline->samples[i], *b.timeline->samples[i]))
        return false;
  }
  return true;
}
NativeMaterialReference native_material_timeline(std::shared_ptr<const NativeMaterialReference> nominal,
                                                 std::vector<std::shared_ptr<const NativeMaterialReference>> samples) {
  if (!nominal || nominal->timeline || samples.size() < 3 || samples.size() > 129 || samples.size() % 2 == 0)
    throw std::logic_error("Malformed native material timeline");
  std::string payload = std::to_string(nominal->key.size()) + ":" + nominal->key;
  for (const auto &sample : samples) {
    if (!sample || sample->timeline)
      throw std::logic_error("Native material timelines require plain owned leaves");
    payload += std::to_string(sample->key.size()) + ":" + sample->key;
  }
  const auto hash = XXH3_128bits(payload.data(), payload.size());
  auto result = *nominal;
  result.key = "native-material:timeline:" + std::to_string(hash.high64) + ":" + std::to_string(hash.low64);
  result.timeline = std::make_shared<const NativeMaterialTimeline>(std::move(nominal), std::move(samples));
  return result;
}
std::set<std::string> native_material_paths(const NativeMaterialReference &reference) {
  std::set<std::string> result;
  const auto add = [&](const NativeMaterialReference &value) {
    if (!value.path.empty())
      result.insert(value.path);
  };
  add(reference);
  if (reference.timeline) {
    add(*reference.timeline->nominal);
    for (const auto &sample : reference.timeline->samples)
      add(*sample);
  }
  return result;
}
NativeMaterialReference refresh_native_material_reference(const NativeMaterialReference &reference) {
  if (reference.timeline) {
    auto nominal = std::make_shared<const NativeMaterialReference>(
        refresh_native_material_reference(*reference.timeline->nominal));
    std::vector<std::shared_ptr<const NativeMaterialReference>> samples;
    for (const auto &sample : reference.timeline->samples)
      samples.push_back(std::make_shared<const NativeMaterialReference>(refresh_native_material_reference(*sample)));
    return native_material_timeline(std::move(nominal), std::move(samples));
  }
  if (!reference.key.starts_with("native-material:"))
    return reference;
  if (!UT_Thread::isMainThread())
    throw std::logic_error("Native material refresh requires the host thread");
  auto assignment = reference.assignment();
  if (reference.session_id && !reference.identity.empty()) {
    if (auto *node = OP_Node::lookupNode(reference.session_id)) {
      UT_StringHolder identity;
      if (node->getUserData("cycles_graph_id", identity) && identity.toStdString() == reference.identity)
        assignment.path = node->getFullPath().toStdString();
    }
  }
  return resolve_native_material(assignment, reference.volume);
}
NativeMaterialAssignment inherit_native_material(NativeMaterialAssignment local,
                                                 const NativeMaterialAssignment &inherited) {
  if (local.path.starts_with("op:"))
    local.path.erase(0, 3);
  const auto inherited_path = inherited.path.starts_with("op:") ? inherited.path.substr(3) : inherited.path;
  if (local.path.empty() || local.path == inherited_path) {
    if (local.path.empty())
      local.path = inherited_path;
    local.overrides.insert(local.overrides.begin(), inherited.overrides.begin(), inherited.overrides.end());
    local.surface_overrides.insert(local.surface_overrides.begin(), inherited.surface_overrides.begin(),
                                   inherited.surface_overrides.end());
    local.displacement_overrides.insert(local.displacement_overrides.begin(), inherited.displacement_overrides.begin(),
                                        inherited.displacement_overrides.end());
  }
  return local;
}
NativeMaterialReference resolve_native_material(const NativeMaterialAssignment &assignment, bool volume) {
  auto result = resolve_native_material(assignment.path, volume);
  result.overrides = assignment.overrides;
  result.surface_overrides = assignment.surface_overrides;
  result.displacement_overrides = assignment.displacement_overrides;
  if (assignment.has_overrides()) {
    std::string payload;
    for (const auto &layer : result.overrides)
      payload += std::to_string(layer.size()) + ":" + layer;
    for (const auto *layers : {&result.surface_overrides, &result.displacement_overrides}) {
      payload += "/domain:" + std::to_string(layers->size()) + ":";
      for (const auto &layer : *layers)
        payload += std::to_string(layer.size()) + ":" + layer;
    }
    const auto hash = XXH3_128bits(payload.data(), payload.size());
    result.key += "/override:" + std::to_string(hash.high64) + ":" + std::to_string(hash.low64);
    if (result.diagnostic)
      result.diagnostic->resource = result.key;
  }
  return result;
}
NativeMaterialReference resolve_native_material(const std::string &value, bool volume) {
  if (!UT_Thread::isMainThread())
    throw std::logic_error("Native material lookup requires the host thread");
  std::string path = value.starts_with("op:") ? value.substr(3) : value;
  const auto fallback = [&](const std::string &code, const std::string &reason) {
    NativeMaterialReference result;
    result.key = "native-material:unresolved:" + path + (volume ? "/volume" : "");
    result.path = path;
    result.volume = volume;
    result.diagnostic = SourceDiagnostic{code, path, result.key, reason,
                                         volume ? "Rendering density with a magenta volume material"
                                                : "Rendering assigned geometry with a magenta diagnostic material"};
    return result;
  };
  if (path.empty() || path[0] != '/' || path.find('\0') != std::string::npos)
    return fallback("material.invalid_path",
                    "Native geometry material paths must be absolute operator paths: " + value);
  auto *node = OPgetDirector()->findNode(path.c_str());
  if (!node)
    return fallback("material.missing", "Missing assigned material: " + path);
  if (node->getOperator()->getName() != "cycles::material::1" &&
      node->getOperator()->getName() != "cycles::volume_material::1")
    return fallback("material.unsupported",
                    "Unsupported assigned shader " + node->getOperator()->getName().toStdString() + ": " + path);
  // Houdini can reuse a unique/session ID immediately after deletion. A
  // saved GUID distinguishes lifetimes; the host ID disambiguates copies
  // which retain user data. Both components stay stable on ordinary rename.
  UT_StringHolder identity;
  if (!node->getUserData("cycles_graph_id", identity) || identity.length() != 32 ||
      !std::all_of(identity.begin(), identity.end(),
                   [](char c) { return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'); }))
    return fallback("material.invalid_identity", "Cycles material has an invalid saved graph identity: " + path);
  return {"native-material:" + identity.toStdString() + ":" + std::to_string(node->getUniqueId()) +
              (volume ? "/volume" : ""),
          node->getFullPath().toStdString(),
          node->getUniqueId(),
          identity.toStdString(),
          volume,
          {}};
}
std::shared_ptr<const NativeMaterialLookup> freeze_native_materials(const NativeMaterialBindingState &state,
                                                                    const GeometryRequest &request, bool points,
                                                                    bool volume, bool displacement_only) {
  if (!UT_Thread::isMainThread())
    throw std::logic_error("Material preparation requires the host thread");
  auto result = std::make_shared<NativeMaterialLookup>();
  result->displacement_only = displacement_only;
  result->policies[-1] = state.policy;
  const auto inherited = request.material_assignment();
  const auto add_resolved = [&](NativeMaterialAssignment binding) {
    if (!binding.path.empty() && !result->contains(binding))
      result->emplace(binding, resolve_native_material(binding, volume));
  };
  const auto add = [&](NativeMaterialAssignment local) {
    add_resolved(inherit_native_material(std::move(local), inherited));
  };
  if (!state.bindings.empty()) {
    const auto detail = inherit_native_material(state.detail_binding, inherited);
    for (const auto &recipe : state.bindings) {
      auto binding = inherit_native_material(recipe.primitive, detail);
      if (recipe.point_layer)
        binding = inherit_native_material(recipe.point, binding);
      add_resolved(std::move(binding));
    }
    return result;
  }
  if (state.fallback_used)
    add(state.detail_path);
  for (const auto &path : state.primitive_paths)
    add(path);
  if (points)
    for (const auto &path : state.point_paths)
      add(path);
  return result;
}
std::shared_ptr<const NativeMaterialLookup>
freeze_native_material_candidates(const GU_Detail &source, const std::set<NativeMaterialAssignment> &fallback_paths,
                                  bool displacement_only) {
  if (!UT_Thread::isMainThread())
    throw std::logic_error("Material preparation requires the host thread");
  auto result = std::make_shared<NativeMaterialLookup>();
  result->displacement_only = displacement_only;
  const bool local_overrides = source.findGlobalAttribute("material_override") ||
                               source.findPrimitiveAttribute("material_override") ||
                               source.findPointAttribute("material_override");
  bool key_bindings = false;
  if (source.findPointAttribute("shop_materialpath") || source.findVertexAttribute("shop_materialpath") ||
      source.findVertexAttribute("material_override"))
    for (GA_Iterator it(source.getPrimitiveRange()); !it.atEnd() && !key_bindings; ++it) {
      const auto *primitive = source.getPrimitive(*it);
      const auto *poly = dynamic_cast<const GEO_PrimPoly *>(primitive);
      key_bindings = dynamic_cast<const GEO_Curve *>(primitive) != nullptr || (poly && !poly->isClosed());
    }
  const bool inherited_overrides = std::any_of(fallback_paths.begin(), fallback_paths.end(),
                                               [](const auto &binding) { return binding.has_overrides(); });
  if (!local_overrides && !inherited_overrides && !key_bindings) {
    const auto add = [&](NativeMaterialAssignment binding) {
      binding = inherit_native_material(std::move(binding), {});
      if (!binding.path.empty() && !result->contains(binding))
        result->emplace(binding, resolve_native_material(binding));
    };
    for (const auto &fallback : fallback_paths)
      add(fallback);
    for (const auto owner : {GA_ATTRIB_PRIMITIVE, GA_ATTRIB_DETAIL, GA_ATTRIB_POINT, GA_ATTRIB_VERTEX}) {
      const auto *attribute = string_attribute(source.findAttribute(owner, "shop_materialpath"), "shop_materialpath");
      if (!attribute)
        continue;
      if (const auto *table = attribute->getAIFSharedStringTuple()) {
        for (auto it = table->begin(attribute); !it.atEnd(); ++it)
          add(std::string(it.getString()));
      } else
        for (const auto &path : strings(attribute, "shop_materialpath"))
          add(path);
    }
    return result;
  }
  // Prepare only effective supported assignments. Unused point/vertex paths
  // must not expand shader demand or defeat a component's recovery policy.
  NativeMaterialBindingCache cache;
  const auto partitions = partition_geometry(source);
  auto fallbacks = fallback_paths;
  if (fallbacks.empty())
    fallbacks.insert(NativeMaterialAssignment{});
  for (const auto &selection : partitions.selections)
    if (selection.active) {
      const auto &state = cache.inspect(source, &selection);
      result->policies[int(selection.kind)] = state.policy;
      for (const auto &fallback : fallbacks) {
        GeometryRequest request;
        request.material_mode = NativeMaterialMode::Native;
        request.set_material_assignment(fallback);
        auto frozen = freeze_native_materials(state, request, selection.kind == NativeGeometryKind::Points,
                                              selection.kind == NativeGeometryKind::Volume);
        if (selection.kind == NativeGeometryKind::Curves)
          frozen = freeze_styled_materials(source, selection, request, std::move(frozen));
        if (const auto policy = frozen->policies.find(int(selection.kind)); policy != frozen->policies.end())
          result->policies[int(selection.kind)] = policy->second;
        for (const auto &[assignment, reference] : *frozen)
          result->emplace(assignment, reference);
      }
    }
  if (partitions.count == 1)
    result->policies[-1] = result->policies.begin()->second;
  return result;
}
const NativeMaterialBindingState &NativeMaterialBindingCache::inspect(const GU_Detail &source,
                                                                      const NativeGeometrySelection *selection) {
  std::vector<std::int64_t> ids{source.getNumPrimitives(), source.getNumPoints(), source.getTopology().getDataId(),
                                source.getPrimitiveList().getDataId()};
  bool valid = ids[2] != GA_INVALID_DATAID && ids[3] != GA_INVALID_DATAID;
  for (const auto owner : {GA_ATTRIB_PRIMITIVE, GA_ATTRIB_DETAIL, GA_ATTRIB_POINT, GA_ATTRIB_VERTEX})
    for (const auto *name : {"shop_materialpath", "material_override", "material_stylesheet"}) {
      const auto *attribute = source.findAttribute(owner, name);
      const auto id = attribute ? attribute->getDataId() : GA_INVALID_DATAID;
      ids.push_back(id);
      if (attribute && id == GA_INVALID_DATAID)
        valid = false;
    }
  auto &record = records_[{source.getUniqueId(), selection ? int(selection->kind) : -1}];
  if (valid && record.identifiers == ids) {
    ++hits_;
    return record.state;
  }
  ++scans_;
  // Constructing a selected vertex range can walk every mesh vertex. Cache
  // hits must return before building ranges, and mesh bindings never need it.
  const auto primitives = selection ? selection->primitive_range(source) : source.getPrimitiveRange();
  const auto selected_points = selection ? selection->point_range(source) : source.getPointRange();
  NativeMaterialBindingState state;
  for (const auto owner : {GA_ATTRIB_PRIMITIVE, GA_ATTRIB_DETAIL, GA_ATTRIB_POINT, GA_ATTRIB_VERTEX}) {
    for (const auto *name : {"shop_materialpath", "material_override", "material_stylesheet"}) {
      const auto *attribute = source.findAttribute(owner, name);
      if (attribute && !string_attribute(attribute, name))
        state.diagnostics.push_back({"material.invalid_attribute",
                                     name,
                                     {},
                                     std::string(name) + " is not a scalar string attribute",
                                     "Ignoring this attribute; using supported assignment layers"});
    }
  }
  GA_ROHandleS detail_path(string_attribute(source.findGlobalAttribute("shop_materialpath"), "shop_materialpath"));
  if (detail_path.isValid())
    state.detail_path = detail_path.get(GA_DETAIL_OFFSET).toStdString();
  const auto *first = source.getNumPrimitives() ? source.getPrimitive(source.primitiveOffset(0)) : nullptr;
  const bool points = selection ? selection->kind == NativeGeometryKind::Points
                                : (!first || dynamic_cast<const GEO_PrimParticle *>(first));
  if (points) {
    GA_ROHandleS point_path(string_attribute(source.findPointAttribute("shop_materialpath"), "shop_materialpath"));
    GA_ROHandleS primitive_path(
        string_attribute(source.findPrimitiveAttribute("shop_materialpath"), "shop_materialpath"));
    std::set<GA_StringIndexType> point_indices, primitive_indices;
    const auto append = [](const GA_ROHandleS &handle, GA_Offset offset, std::set<GA_StringIndexType> &indices,
                           std::vector<std::string> &paths) {
      if (!handle.isValid())
        return false;
      const bool inserted = indices.insert(handle.getIndex(offset)).second;
      if (handle.get(offset).isEmpty())
        return false;
      if (inserted)
        paths.push_back(handle.get(offset).toStdString());
      return true;
    };
    if (primitives.isEmpty()) {
      for (GA_Iterator it(selected_points); !it.atEnd(); ++it)
        if (!append(point_path, *it, point_indices, state.point_paths))
          state.fallback_used = true;
    } else {
      // Ignore loose points outside particle primitive vertex lists.
      // A point binding takes precedence over every containing primitive.
      for (GA_Iterator it(primitives); !it.atEnd(); ++it) {
        const auto *primitive = dynamic_cast<const GEO_PrimParticle *>(source.getPrimitive(*it));
        if (!primitive)
          throw std::invalid_argument("Mixed particle/non-particle input requires separate inputs");
        for (const auto vertex : primitive->getVertexList()) {
          const auto offset = source.vertexPoint(vertex);
          if (!append(point_path, offset, point_indices, state.point_paths) &&
              !append(primitive_path, *it, primitive_indices, state.primitive_paths))
            state.fallback_used = true;
        }
      }
    }
  } else {
    const auto primitive_paths =
        strings(source.findPrimitiveAttribute("shop_materialpath"), "shop_materialpath", &primitives);
    const auto *primitive_attribute =
        string_attribute(source.findPrimitiveAttribute("shop_materialpath"), "shop_materialpath");
    state.fallback_used = !primitives.isEmpty() && !primitive_attribute;
    for (const auto &path : primitive_paths) {
      if (path.empty())
        state.fallback_used = true;
      else
        state.primitive_paths.push_back(path);
    }
    for (const auto &path :
         strings(source.findPointAttribute("shop_materialpath"), "shop_materialpath", &selected_points))
      if (!path.empty())
        state.point_paths.push_back(path);
  }
  const auto *curve = dynamic_cast<const GEO_PrimPoly *>(first);
  const bool restricted =
      selection ? (selection->kind == NativeGeometryKind::Curves || selection->kind == NativeGeometryKind::Volume)
                : ((curve && !curve->isClosed()) || dynamic_cast<const GEO_Curve *>(first) ||
                   dynamic_cast<const GU_PrimVDB *>(first) || dynamic_cast<const GEO_PrimVolume *>(first));
  if ((points || restricted) &&
      (source.findVertexAttribute("shop_materialpath") || source.findVertexAttribute("material_override") ||
       source.findVertexAttribute("material_stylesheet"))) {
    const auto vertices = selection ? selection->vertex_range(source) : source.getVertexRange();
    for (const auto *name : {"shop_materialpath", "material_override", "material_stylesheet"})
      for (const auto &value : strings(source.findVertexAttribute(name), name, &vertices))
        if (!value.empty() && (std::string(name) == "shop_materialpath" || value != "{}")) {
          state.vertex_bindings = true;
          if (std::string(name) == "material_stylesheet")
            state.vertex_stylesheets = true;
        }
  }
  for (const auto *name : {"material_override", "material_stylesheet"})
    for (const auto &value : strings(source.findPointAttribute(name), name, &selected_points))
      if (!value.empty() && value != "{}")
        state.point_overrides = true;
  unsigned active_overrides = 0;
  for (const auto owner : {GA_ATTRIB_PRIMITIVE, GA_ATTRIB_DETAIL})
    for (const auto *name : {"material_override", "material_stylesheet"})
      for (const auto &value :
           strings(source.findAttribute(owner, name), name, owner == GA_ATTRIB_PRIMITIVE ? &primitives : nullptr,
                   override_variant_limit + 1))
        if (!value.empty() && value != "{}") {
          if (std::string(name) == "material_stylesheet")
            state.diagnostics.push_back({"material.unsupported_override",
                                         name,
                                         {},
                                         std::string("Unsupported ") + name,
                                         "Ignoring this layer; retaining the base material"});
          else if (!usable_override(value))
            state.diagnostics.push_back({"material.invalid_override",
                                         name,
                                         {},
                                         "Material override exceeds 64 KiB or contains NULs",
                                         "Ignoring this layer; retaining the base material"});
          else {
            state.has_overrides = true;
            active_overrides |= owner == GA_ATTRIB_PRIMITIVE ? 2u : 1u;
          }
        }
  for (const auto &value : strings(source.findPointAttribute("material_override"), "material_override",
                                   &selected_points, override_variant_limit + 1))
    if (!value.empty() && value != "{}") {
      if (!usable_override(value))
        state.diagnostics.push_back({"material.invalid_override",
                                     "material_override",
                                     {},
                                     "Material override exceeds 64 KiB or contains NULs",
                                     "Ignoring this layer; retaining the base material"});
      else if (points) {
        state.has_overrides = true;
        active_overrides |= 4u;
      }
    }
  if (restricted)
    for (const auto &value : strings(source.findVertexAttribute("material_override"), "material_override", nullptr,
                                     override_variant_limit + 1))
      if (!value.empty() && value != "{}" && !usable_override(value))
        state.diagnostics.push_back({"material.invalid_override",
                                     "material_override",
                                     {},
                                     "Vertex material override exceeds 64 KiB or contains NULs",
                                     "Ignoring this layer; retaining supported assignment layers"});
  for (const auto &value :
       strings(source.findPointAttribute("material_stylesheet"), "material_stylesheet", &selected_points))
    if (!value.empty() && value != "{}")
      state.point_stylesheets = true;
  if (state.has_overrides || !state.detail_path.empty() ||
      (points && source.findPointAttribute("shop_materialpath") &&
       source.findPrimitiveAttribute("shop_materialpath"))) {
    auto policy = std::make_shared<NativeMaterialPolicy>();
    for (;;) {
      GeometryRequest request;
      request.material_mode = NativeMaterialMode::Native;
      request.native_material_policy = policy;
      NativeMaterialBindings bindings(source, request, selection);
      std::set<NativeMaterialRecipe> used;
      std::set<NativeMaterialAssignment> effective;
      std::size_t variants = 0, literal_bytes = 0;
      bool excess = false;
      const auto add = [&](NativeMaterialRecipe recipe, const NativeMaterialAssignment &assignment) {
        if (excess)
          return;
        used.insert(std::move(recipe));
        // A local path can discard many different parent layers. Only
        // distinct surviving bindings consume shader variants.
        if (assignment.has_overrides() && effective.insert(assignment).second) {
          ++variants;
          for (const auto *layers :
               {&assignment.overrides, &assignment.surface_overrides, &assignment.displacement_overrides})
            for (const auto &layer : *layers)
              literal_bytes += layer.size();
          excess = variants > override_variant_limit || literal_bytes > override_literal_limit;
        }
      };
      if (points) {
        if (primitives.isEmpty())
          for (GA_Iterator it(selected_points); !it.atEnd() && !excess; ++it)
            add({{}, bindings.local_point_assignment(*it), true}, bindings.point_assignment(*it));
        else
          for (GA_Iterator it(primitives); !it.atEnd() && !excess; ++it) {
            const auto *primitive = dynamic_cast<const GEO_PrimParticle *>(source.getPrimitive(*it));
            if (!primitive)
              throw std::logic_error("Non-particle in prepared point binding selection");
            for (const auto vertex : primitive->getVertexList()) {
              const auto point = source.vertexPoint(vertex);
              add({bindings.local_primitive_assignment(*it), bindings.local_point_assignment(point), true},
                  bindings.point_assignment(point, *it));
              if (excess)
                break;
            }
          }
      } else
        for (GA_Iterator it(primitives); !it.atEnd() && !excess; ++it)
          add({bindings.local_primitive_assignment(*it), {}, false}, bindings.assignment(*it));
      if (!excess) {
        state.detail_binding = bindings.detail_assignment();
        state.bindings.assign(used.begin(), used.end());
        break;
      }
      unsigned dropped = 0;
      for (const unsigned owner : {4u, 2u, 1u})
        if ((active_overrides & owner) && !(policy->ignored_overrides & owner)) {
          dropped = owner;
          break;
        }
      if (!dropped)
        throw std::logic_error("Override capacity recovery has no contributing source layer");
      policy->ignored_overrides |= dropped;
      const std::string owner = dropped == 4 ? "point" : dropped == 2 ? "primitive" : "detail";
      const std::string reason = literal_bytes > override_literal_limit
                                     ? "Override combinations exceed the 8 MiB owned-literal budget for this component"
                                     : "Override combinations exceed the 4096-variant budget for this component";
      const std::string action =
          "Ignoring the " + owner +
          " override layer; retaining base material assignments, lower-priority supported layers and geometry";
      if (policy->diagnostics.empty())
        policy->diagnostics.push_back(
            {"material.override_capacity", "material_override", {}, reason, action, "warning", "material_override"});
      else {
        auto &issue = policy->diagnostics.front();
        if (issue.message.find(reason) == std::string::npos)
          issue.message += "; " + reason;
        issue.action += "; " + action;
      }
    }
    state.policy = std::move(policy);
  }
  record.identifiers = std::move(ids);
  record.state = std::move(state);
  return record.state;
}
void NativeMaterialBindingCache::retire(const std::set<std::int64_t> &details) {
  for (auto it = records_.begin(); it != records_.end();)
    if (!details.contains(it->first.first))
      it = records_.erase(it);
    else
      ++it;
}
NativeMaterialBindings::NativeMaterialBindings(const GU_Detail &source, const GeometryRequest &request,
                                               const NativeGeometrySelection *selection, Values *values)
    : fallback_(request.material_key), fallback_path_(request.object_material_path),
      native_(request.material_mode == NativeMaterialMode::Native), source_(&source), frozen_(request.native_materials),
      styles_(request.native_styles), values_(values ? values : &local_values_) {
  if (!native_)
    return;
  auto policy = request.native_material_policy;
  if (frozen_) {
    auto found = frozen_->policies.find(selection ? int(selection->kind) : -1);
    if (found == frozen_->policies.end())
      found = frozen_->policies.find(-1);
    if (found != frozen_->policies.end())
      policy = found->second;
  }
  const auto ignored = ignored_overrides_ = policy ? policy->ignored_overrides : 0u;
  primitive_path_.bind(string_attribute(source.findPrimitiveAttribute("shop_materialpath"), "shop_materialpath"));
  if (!(ignored & 2u))
    primitive_override_.bind(string_attribute(source.findPrimitiveAttribute("material_override"), "material_override"));
  primitive_stylesheet_.bind(
      string_attribute(source.findPrimitiveAttribute("material_stylesheet"), "material_stylesheet"));
  point_path_.bind(string_attribute(source.findPointAttribute("shop_materialpath"), "shop_materialpath"));
  if (!(ignored & 4u))
    point_override_.bind(string_attribute(source.findPointAttribute("material_override"), "material_override"));
  point_stylesheet_.bind(string_attribute(source.findPointAttribute("material_stylesheet"), "material_stylesheet"));
  vertex_path_.bind(string_attribute(source.findVertexAttribute("shop_materialpath"), "shop_materialpath"));
  if (!(ignored & 8u))
    vertex_override_.bind(string_attribute(source.findVertexAttribute("material_override"), "material_override"));
  GA_ROHandleS detail_override(
      ignored & 1u ? nullptr : string_attribute(source.findGlobalAttribute("material_override"), "material_override"));
  GA_ROHandleS detail_stylesheet(
      string_attribute(source.findGlobalAttribute("material_stylesheet"), "material_stylesheet"));
  GA_ROHandleS detail_path(string_attribute(source.findGlobalAttribute("shop_materialpath"), "shop_materialpath"));
  if (detail_path.isValid() && !detail_path.get(GA_DETAIL_OFFSET).isEmpty())
    fallback_path_ = detail_path.get(GA_DETAIL_OFFSET).toStdString();
  NativeMaterialAssignment local;
  if (detail_path.isValid())
    local.path = detail_path.get(GA_DETAIL_OFFSET).toStdString();
  append_override(local, detail_override, GA_DETAIL_OFFSET);
  detail_assignment_ = local;
  fallback_assignment_ = inherit_native_material(std::move(local), request.material_assignment());
  if (request.native_fallback)
    values_->references.emplace(request.native_fallback->key, *request.native_fallback);
}
std::string NativeMaterialBindings::path(GA_Offset offset) const { return assignment(offset).path; }
NativeMaterialAssignment NativeMaterialBindings::assignment(GA_Offset offset,
                                                            const NativeMaterialAssignment &inherited) const {
  auto value = inherit_native_material(local_primitive_assignment(offset),
                                       inherit_native_material(inherited, fallback_assignment_));
  if (styles_ && !styles_->primitives.empty() && offset != GA_INVALID_OFFSET)
    value = styled_material(
        std::move(value),
        styles_->primitives.at(styles_->primitives.size() == 1 ? 0 : std::size_t(source_->primitiveIndex(offset))));
  return value;
}
NativeMaterialAssignment NativeMaterialBindings::assignment(GA_Offset offset) const {
  auto value = inherit_native_material(local_primitive_assignment(offset), fallback_assignment_);
  if (styles_ && !styles_->primitives.empty() && offset != GA_INVALID_OFFSET)
    value = styled_material(
        std::move(value),
        styles_->primitives.at(styles_->primitives.size() == 1 ? 0 : std::size_t(source_->primitiveIndex(offset))));
  return value;
}
NativeMaterialAssignment NativeMaterialBindings::local_primitive_assignment(GA_Offset offset) const {
  if (!native_)
    return {};
  NativeMaterialAssignment local;
  if (offset != GA_INVALID_OFFSET && primitive_path_.isValid())
    local.path = primitive_path_.get(offset).toStdString();
  append_override(local, primitive_override_, offset);
  return local;
}
NativeMaterialAssignment NativeMaterialBindings::point_assignment(GA_Offset offset, GA_Offset primitive) const {
  auto parent = inherit_native_material(local_primitive_assignment(primitive), fallback_assignment_);
  auto value = inherit_native_material(local_point_assignment(offset), parent);
  if (const auto context = point_style_context(offset, primitive))
    return styled_material(std::move(value), styles_->point_context_layers.values().at(*context));
  if (styles_ && !styles_->primitives.empty() && primitive != GA_INVALID_OFFSET)
    value = styled_material(
        std::move(value),
        styles_->primitives.at(styles_->primitives.size() == 1 ? 0 : std::size_t(source_->primitiveIndex(primitive))));
  if (styles_ && !styles_->points.empty())
    value =
        styled_material(std::move(value),
                        styles_->points.at(styles_->points.size() == 1 ? 0 : std::size_t(source_->pointIndex(offset))));
  return value;
}
std::optional<std::uint32_t> NativeMaterialBindings::point_style_context(GA_Offset point, GA_Offset primitive) const {
  if (!styles_ || primitive == GA_INVALID_OFFSET)
    return {};
  const auto owner = std::size_t(source_->primitiveIndex(primitive));
  if (!styles_->point_context_primitives.contains(owner))
    return {};
  const auto found = styles_->point_contexts.find({owner, std::size_t(source_->pointIndex(point))});
  if (found == styles_->point_contexts.end())
    throw std::logic_error("Native point stylesheet context was not frozen for its source owner");
  return found->second;
}
bool NativeMaterialBindings::curve_key_layers() const {
  return point_path_.isValid() || point_override_.isValid() || vertex_path_.isValid() || vertex_override_.isValid() ||
         (styles_ && !styles_->point_context_primitives.empty());
}
NativeMaterialAssignment NativeMaterialBindings::local_point_assignment(GA_Offset offset) const {
  if (!native_)
    return {};
  NativeMaterialAssignment local;
  if (point_path_.isValid())
    local.path = point_path_.get(offset).toStdString();
  append_override(local, point_override_, offset);
  return local;
}
NativeMaterialAssignment NativeMaterialBindings::local_vertex_assignment(GA_Offset offset) const {
  if (!native_)
    return {};
  NativeMaterialAssignment local;
  if (vertex_path_.isValid())
    local.path = vertex_path_.get(offset).toStdString();
  append_override(local, vertex_override_, offset);
  return local;
}
NativeMaterialAssignment NativeCurveMaterialBinding::assignment(unsigned *omitted) const {
  if (omitted)
    *omitted = 0;
  if (!started)
    return primitive;
  if (point_style) {
    if (styled_vertex_uniform)
      return styled_vertex;
    if (styled_point_uniform) {
      if (omitted && vertex_active)
        *omitted |= 2u;
      return styled_point;
    }
    if (styled_primitive_uniform) {
      if (omitted && vertex_active)
        *omitted |= 2u;
      if (omitted && point_active)
        *omitted |= 1u;
      return styled_primitive;
    }
    if (omitted)
      *omitted |= 4u;
  }
  if (vertex_uniform)
    return vertex;
  if (omitted && vertex_active)
    *omitted |= 2u;
  if (point_uniform)
    return point;
  if (omitted && point_active)
    *omitted |= 1u;
  return primitive;
}
NativeCurveMaterialBinding NativeMaterialBindings::curve_binding(GA_Offset primitive) const {
  NativeCurveMaterialBinding value;
  const auto local = local_primitive_assignment(primitive);
  value.override_owners = (detail_assignment_.has_overrides() ? 1u : 0u) | (local.has_overrides() ? 2u : 0u);
  value.native_primitive = inherit_native_material(local, fallback_assignment_);
  value.primitive = assignment(primitive);
  return value;
}
void NativeMaterialBindings::curve_key(NativeCurveMaterialBinding &value, GA_Offset vertex) const {
  const auto point = source_->vertexPoint(vertex);
  auto point_layer = local_point_assignment(point), vertex_layer = local_vertex_assignment(vertex);
  if (point_layer.has_overrides())
    value.override_owners |= 4u;
  if (vertex_layer.has_overrides())
    value.override_owners |= 8u;
  value.point_active |= !point_layer.path.empty() || point_layer.has_overrides();
  value.vertex_active |= !vertex_layer.path.empty() || vertex_layer.has_overrides();
  auto point_value = inherit_native_material(std::move(point_layer), value.native_primitive);
  auto vertex_value = inherit_native_material(std::move(vertex_layer), point_value);
  if (const auto context = point_style_context(point, source_->vertexPrimitive(vertex))) {
    value.point_style = true;
    const auto &style = styles_->point_context_layers.values().at(*context);
    auto primitive_styled = styled_material(value.native_primitive, style);
    auto point_styled = styled_material(point_value, style), vertex_styled = styled_material(vertex_value, style);
    if (value.started) {
      value.styled_primitive_uniform &= value.styled_primitive == primitive_styled;
      value.styled_point_uniform &= value.styled_point == point_styled;
      value.styled_vertex_uniform &= value.styled_vertex == vertex_styled;
    } else {
      value.styled_primitive = std::move(primitive_styled);
      value.styled_point = std::move(point_styled);
      value.styled_vertex = std::move(vertex_styled);
    }
  }
  // The native stylesheet has the same priority as in point_assignment():
  // apply it after local path/override layers, including material switches.
  if (styles_ && !styles_->primitives.empty()) {
    const auto primitive = source_->vertexPrimitive(vertex);
    const auto &style =
        styles_->primitives.at(styles_->primitives.size() == 1 ? 0 : std::size_t(source_->primitiveIndex(primitive)));
    point_value = styled_material(std::move(point_value), style);
    vertex_value = styled_material(std::move(vertex_value), style);
  }
  if (value.started) {
    value.point_uniform &= value.point == point_value;
    value.vertex_uniform &= value.vertex == vertex_value;
  } else {
    value.point = std::move(point_value);
    value.vertex = std::move(vertex_value);
    value.started = true;
  }
}
NativeMaterialAssignment NativeMaterialBindings::curve_assignment(GA_Offset primitive, unsigned *omitted) const {
  if (!curve_key_layers()) {
    if (omitted)
      *omitted = 0;
    return assignment(primitive);
  }
  auto value = curve_binding(primitive);
  const auto *source_primitive = source_->getPrimitive(primitive);
  for (GA_Size index = 0; index < source_primitive->getVertexCount(); ++index)
    curve_key(value, source_primitive->getVertexOffset(index));
  return value.assignment(omitted);
}
std::string NativeMaterialBindings::resolve(const NativeMaterialAssignment &path) {
  // Never query live nodes during conversion. Missing frozen input is an
  // integration error, not permission to fall back to an OP lookup.
  if (const auto found = values_->paths.find(path); found != values_->paths.end())
    return found->second;
  if (!frozen_)
    throw std::logic_error("Native material conversion requires frozen bindings");
  const auto found = frozen_->find(path);
  if (found == frozen_->end())
    throw std::logic_error("Native material binding was not frozen: " + path.path);
  auto reference = found->second;
  const auto key = reference.key;
  values_->paths.emplace(path, key);
  values_->references.emplace(key, std::move(reference));
  return key;
}
int NativeMaterialBindings::primitive(GA_Offset offset) {
  if (native_ && frozen_ && frozen_->elements && offset != GA_INVALID_OFFSET) {
    const auto &elements = *frozen_->elements;
    auto slot = elements.uniform;
    if (!elements.primitive_slots.empty()) {
      const auto index = source_->primitiveIndex(offset);
      if (index < 0 || std::size_t(index) >= elements.primitive_slots.size())
        throw std::logic_error("Owned material primitive numbers do not match nominal geometry");
      slot = elements.primitive_slots[std::size_t(index)];
    }
    if (slot >= 0)
      return frozen_binding(*elements.references.at(std::size_t(slot)));
  }
  if (styles_) {
    const auto style = offset != GA_INVALID_OFFSET && !styles_->primitives.empty()
                           ? styles_->primitives.slots().at(
                                 styles_->primitives.size() == 1 ? 0 : std::size_t(source_->primitiveIndex(offset)))
                           : std::numeric_limits<std::uint32_t>::max();
    const auto key = std::make_tuple(
        primitive_path_.isValid() && offset != GA_INVALID_OFFSET ? primitive_path_.getIndex(offset)
                                                                 : GA_StringIndexType(-1),
        primitive_override_.isValid() && offset != GA_INVALID_OFFSET ? primitive_override_.getIndex(offset)
                                                                     : GA_StringIndexType(-1),
        style);
    if (const auto found = values_->styled_primitive_slots.find(key); found != values_->styled_primitive_slots.end())
      return found->second;
    const auto slot = binding(assignment(offset));
    values_->styled_primitive_slots.emplace(key, slot);
    return slot;
  }
  // Resolve an interned primitive binding once, including the empty/default
  // handle. Triangulation may expand one authored face into many triangles.
  const auto index =
      std::make_pair(primitive_path_.isValid() && offset != GA_INVALID_OFFSET ? primitive_path_.getIndex(offset)
                                                                              : GA_StringIndexType(-1),
                     primitive_override_.isValid() && offset != GA_INVALID_OFFSET ? primitive_override_.getIndex(offset)
                                                                                  : GA_StringIndexType(-1));
  if (const auto found = values_->primitive_slots.find(index); found != values_->primitive_slots.end())
    return found->second;
  const auto slot = binding(assignment(offset));
  values_->primitive_slots.emplace(index, slot);
  return slot;
}
int NativeMaterialBindings::point(GA_Offset offset, GA_Offset primitive_offset) {
  if (native_ && styles_) {
    const auto primitive_style =
        primitive_offset != GA_INVALID_OFFSET && !styles_->primitives.empty()
            ? styles_->primitives.slots().at(
                  styles_->primitives.size() == 1 ? 0 : std::size_t(source_->primitiveIndex(primitive_offset)))
            : std::numeric_limits<std::uint32_t>::max();
    std::uint64_t point_style =
        !styles_->points.empty()
            ? styles_->points.slots().at(styles_->points.size() == 1 ? 0 : std::size_t(source_->pointIndex(offset)))
            : std::numeric_limits<std::uint32_t>::max();
    if (const auto context = point_style_context(offset, primitive_offset))
      point_style = (std::uint64_t(1) << 32) | *context;
    const std::array<GA_StringIndexType, 4> bindings{
        point_path_.isValid() ? point_path_.getIndex(offset) : GA_StringIndexType(-1),
        point_override_.isValid() ? point_override_.getIndex(offset) : GA_StringIndexType(-1),
        primitive_path_.isValid() && primitive_offset != GA_INVALID_OFFSET ? primitive_path_.getIndex(primitive_offset)
                                                                           : GA_StringIndexType(-1),
        primitive_override_.isValid() && primitive_offset != GA_INVALID_OFFSET
            ? primitive_override_.getIndex(primitive_offset)
            : GA_StringIndexType(-1)};
    const auto key = std::make_tuple(bindings, primitive_style, point_style);
    if (const auto found = values_->styled_point_slots.find(key); found != values_->styled_point_slots.end())
      return found->second;
    const auto slot = binding(point_assignment(offset, primitive_offset));
    values_->styled_point_slots.emplace(key, slot);
    return slot;
  }
  if (native_) {
    if ((point_path_.isValid() && !point_path_.get(offset).isEmpty()) || point_override_.isValid()) {
      const std::array<GA_StringIndexType, 4> index{
          point_path_.isValid() ? point_path_.getIndex(offset) : GA_StringIndexType(-1),
          point_override_.isValid() ? point_override_.getIndex(offset) : GA_StringIndexType(-1),
          primitive_path_.isValid() && primitive_offset != GA_INVALID_OFFSET
              ? primitive_path_.getIndex(primitive_offset)
              : GA_StringIndexType(-1),
          primitive_override_.isValid() && primitive_offset != GA_INVALID_OFFSET
              ? primitive_override_.getIndex(primitive_offset)
              : GA_StringIndexType(-1)};
      if (const auto found = values_->point_slots.find(index); found != values_->point_slots.end())
        return found->second;
      const auto slot = binding(point_assignment(offset, primitive_offset));
      values_->point_slots.emplace(index, slot);
      return slot;
    }
  }
  return primitive(primitive_offset);
}
int NativeMaterialBindings::curve(GA_Offset offset) { return binding(curve_assignment(offset)); }
int NativeMaterialBindings::binding(const NativeMaterialAssignment &value) {
  auto key = fallback_;
  if (!value.path.empty())
    key = resolve(value);
  if (native_ && key.empty())
    throw std::invalid_argument("Unassigned native primitives require a nonempty fallback material key");
  auto [found, inserted] = values_->slots.emplace(key, int(values_->materials.size()));
  if (inserted)
    values_->materials.push_back(key);
  return found->second;
}
int NativeMaterialBindings::frozen_binding(const NativeMaterialReference &reference) {
  if (reference.key.empty())
    throw std::logic_error("Owned sampled binding has no material identity");
  const auto [found, inserted] = values_->slots.emplace(reference.key, int(values_->materials.size()));
  if (inserted) {
    values_->materials.push_back(reference.key);
    values_->references.emplace(reference.key, reference);
  }
  return found->second;
}
std::vector<NativeMaterialReference> NativeMaterialBindings::references() const {
  std::vector<NativeMaterialReference> result;
  for (const auto &key : values_->materials)
    if (const auto found = values_->references.find(key); found != values_->references.end())
      result.push_back(found->second);
  return result;
}
} // namespace cycles_houdini
