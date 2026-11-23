// SPDX-License-Identifier: Apache-2.0
#include "deformation_adapter.h"
#include "gaussian_splat_attribute.h"
#include "named_attribute.h"
#include "mesh_adapter.h"
#include "point_curve_adapter.h"
#include "preserved_detail.h"
#include "../core/mesh_payload.h"
#include <algorithm>
#include <bit>
#include <chrono>
#include <exception>
#include <stdexcept>
#include <type_traits>
#include <unordered_map>
#include <map>
#include <numeric>
#include <set>
#include <GEO/GEO_PrimPoly.h>
#include <GEO/GEO_PrimPart.h>
#include <GEO/GEO_PrimPolySoup.h>
#include <GU/GU_Detail.h>
#include <GU/GU_DetailHandle.h>
#include <OP/OP_Context.h>
#include <SOP/SOP_Node.h>
#include <UT/UT_Interrupt.h>
#include <UT/UT_Thread.h>

namespace cycles_houdini {
namespace {
using Clock = std::chrono::steady_clock;
double elapsed(Clock::time_point start) { return std::chrono::duration<double>(Clock::now() - start).count(); }
struct BindingSample {
  int uniform{-1};
  std::size_t size{};
  std::vector<int> varying;
  void append(int slot) {
    if (!size)
      uniform = slot;
    else if (varying.empty() && uniform != slot)
      varying.assign(size, uniform);
    if (!varying.empty())
      varying.push_back(slot);
    ++size;
  }
  int at(std::size_t index) const {
    if (index >= size)
      throw std::logic_error("Incomplete deformation material correspondence");
    return varying.empty() ? uniform : varying.at(index);
  }
  bool operator==(const BindingSample &) const = default;
};
struct Snapshot {
  std::variant<MeshData, PointData, CurveData> data;
  NativePointIdentities points;
  NativeCurveIdentities curves;
  std::uint64_t index_ids{};
  std::vector<NativeMaterialReference> material_references;
  std::vector<SourceDiagnostic> source_diagnostics;
  std::map<std::string, int> binding_indices;
  std::vector<std::shared_ptr<const NativeMaterialReference>> binding_sources;
  std::vector<BindingSample> shutter_bindings;
  BindingSample nominal_bindings;
};
std::vector<int> binding_palette(Snapshot &target, const Snapshot &source) {
  std::map<std::string, const NativeMaterialReference *> references;
  for (const auto &reference : source.material_references)
    references.emplace(reference.key, &reference);
  return std::visit(
      [&](const auto &geometry) {
        auto keys = geometry.materials;
        if (keys.empty()) {
          if constexpr (std::is_same_v<std::decay_t<decltype(geometry)>, MeshData>)
            keys.push_back("");
          else
            keys.push_back(geometry.material);
        }
        std::vector<int> result;
        result.reserve(keys.size());
        for (const auto &key : keys) {
          const auto [found, inserted] = target.binding_indices.emplace(key, int(target.binding_sources.size()));
          if (inserted) {
            NativeMaterialReference value;
            if (const auto reference = references.find(key); reference != references.end())
              value = *reference->second;
            else
              value.key = key; // An already owned uniform/default material.
            target.binding_sources.push_back(std::make_shared<const NativeMaterialReference>(std::move(value)));
          }
          result.push_back(found->second);
        }
        return result;
      },
      source.data);
}
void initialize_bindings(Snapshot &data, std::size_t samples) {
  const auto palette = binding_palette(data, data);
  data.nominal_bindings = std::visit(
      [&](const auto &geometry) {
        BindingSample result;
        const auto &slots = [&]() -> const std::vector<int> & {
          if constexpr (std::is_same_v<std::decay_t<decltype(geometry)>, MeshData>)
            return geometry.subdivision ? geometry.subdivision->face_material : geometry.triangle_material;
          else
            return geometry.material_slots;
        }();
        const auto count = [&] {
          if constexpr (std::is_same_v<std::decay_t<decltype(geometry)>, MeshData>)
            return geometry.subdivision ? geometry.subdivision->face_sizes.size() : geometry.triangles.size();
          else if constexpr (std::is_same_v<std::decay_t<decltype(geometry)>, CurveData>)
            return geometry.first_key.size();
          else
            return geometry.positions.size();
        }();
        if (slots.empty()) {
          result.uniform = palette.at(0);
          result.size = count;
        } else
          for (const auto slot : slots)
            result.append(palette.at(slot));
        return result;
      },
      data.data);
  data.shutter_bindings.resize(samples);
  data.shutter_bindings.at(samples / 2) = data.nominal_bindings;
}
void finish_bindings(Snapshot &data, const std::string &source) {
  if (std::all_of(data.shutter_bindings.begin(), data.shutter_bindings.end(),
                  [&](const auto &sample) { return sample == data.nominal_bindings; }))
    return;
  const auto count = data.nominal_bindings.size;
  for (const auto &sample : data.shutter_bindings)
    if (sample.size != count)
      throw std::logic_error("Incomplete native binding timeline");
  std::map<std::vector<int>, int> recipes;
  std::map<std::string, int> palette;
  std::map<std::string, NativeMaterialReference> references;
  std::vector<std::string> keys;
  std::vector<int> slots, recipe(data.shutter_bindings.size() + 1);
  std::size_t variants = 0;
  bool capacity_warning = false;
  const auto resolve = [&](std::size_t element) {
    recipe[0] = data.nominal_bindings.at(element);
    for (std::size_t step = 0; step < data.shutter_bindings.size(); ++step)
      recipe[step + 1] = data.shutter_bindings[step].at(element);
    if (const auto found = recipes.find(recipe); found != recipes.end())
      return found->second;
    const auto &nominal = data.binding_sources.at(recipe[0]);
    auto reference = *nominal;
    if (std::any_of(recipe.begin() + 1, recipe.end(), [&](int value) { return value != recipe[0]; })) {
      if (variants < 4096) {
        std::vector<std::shared_ptr<const NativeMaterialReference>> samples;
        for (auto it = recipe.begin() + 1; it != recipe.end(); ++it)
          samples.push_back(data.binding_sources.at(*it));
        reference = native_material_timeline(nominal, std::move(samples));
        ++variants;
      } else if (!capacity_warning) {
        data.source_diagnostics.push_back(
            {"material.temporal_capacity",
             source,
             {},
             "More than 4096 changing material timelines occur in one geometry component",
             "Using requested-frame materials for additional timelines; retaining geometry motion"});
        capacity_warning = true;
      }
    }
    if (reference.key.starts_with("native-material:"))
      references.emplace(reference.key, reference);
    const auto [found, inserted] = palette.emplace(reference.key, int(keys.size()));
    if (inserted)
      keys.push_back(reference.key);
    // Bound correspondence memoization even after temporal fallback starts.
    if (recipes.size() < 8192)
      recipes.emplace(recipe, found->second);
    return found->second;
  };
  const bool uniform =
      data.nominal_bindings.varying.empty() && std::all_of(data.shutter_bindings.begin(), data.shutter_bindings.end(),
                                                           [](const auto &sample) { return sample.varying.empty(); });
  if (count) {
    if (uniform)
      resolve(0);
    else {
      slots.reserve(count);
      for (std::size_t element = 0; element < count; ++element)
        slots.push_back(resolve(element));
    }
  }
  std::visit(
      [&](auto &geometry) {
        geometry.materials = std::move(keys);
        if constexpr (std::is_same_v<std::decay_t<decltype(geometry)>, MeshData>) {
          if (geometry.subdivision)
            geometry.subdivision->face_material = std::move(slots);
          else
            geometry.triangle_material = std::move(slots);
        } else
          geometry.material_slots = std::move(slots);
      },
      data.data);
  data.material_references.clear();
  for (auto &[key, reference] : references)
    data.material_references.push_back(std::move(reference));
}
Snapshot snapshot(const GU_Detail &source, const GeometryRequest &request, std::uint64_t revision, bool shading,
                  const NativeGeometrySelection &selection) {
  Snapshot result;
  if (request.native_styles)
    for (auto issue : request.native_styles->diagnostics) {
      issue.source = request.sop_path;
      issue.resource = request.geometry_key;
      result.source_diagnostics.push_back(std::move(issue));
    }
  for (const auto &message : request.stylesheet_errors)
    result.source_diagnostics.push_back({"material.stylesheet_ignored", request.sop_path, request.geometry_key, message,
                                         "Keeping supported stylesheet rules and underlying bindings", "warning",
                                         "material_stylesheet"});
  if (request.native_materials) {
    for (auto issue : request.native_materials->diagnostics) {
      issue.source = request.sop_path;
      issue.resource = request.geometry_key;
      result.source_diagnostics.push_back(std::move(issue));
    }
    auto policy = request.native_materials->policies.find(int(selection.kind));
    if (policy == request.native_materials->policies.end())
      policy = request.native_materials->policies.find(-1);
    if (policy != request.native_materials->policies.end() && policy->second &&
        policy->second->ignored_overrides !=
            (request.native_material_policy ? request.native_material_policy->ignored_overrides : 0u))
      for (auto issue : policy->second->diagnostics) {
        issue.source = request.sop_path;
        issue.resource = request.geometry_key;
        result.source_diagnostics.push_back(std::move(issue));
      }
  }
  if (selection.kind == NativeGeometryKind::Points)
    result.data = capture_points(source, request, revision, result.index_ids, nullptr, &result.points, shading,
                                 &result.material_references, &selection, &result.source_diagnostics);
  else if (selection.kind == NativeGeometryKind::Curves)
    result.data = capture_curves(source, request, revision, result.index_ids, nullptr, &result.curves, shading,
                                 &result.material_references, &selection, &result.source_diagnostics);
  else if (selection.kind == NativeGeometryKind::Mesh)
    result.data = capture_mesh(source, request, revision, nullptr, &result.points, shading, &result.material_references,
                               &selection, nullptr, nullptr, &result.source_diagnostics);
  else
    throw std::invalid_argument("Native deformation requires polygon meshes, point spheres or open linear curves; "
                                "packed prototypes/volumes need a separate method: " +
                                request.sop_path);
  return result;
}
std::vector<std::size_t> correspondence(const NativePointIdentities &base, const NativePointIdentities &sample) {
  // Data IDs are cache invalidation tokens, not proof that point ordering
  // changed. A procedural recook can allocate new topology with the same
  // ordering. Match implicit identities by index, then validate actual mesh
  // connectivity / strand key counts below. Authored identities allow reorder.
  if (base.keys.size() != sample.keys.size())
    throw NativeDeformationSourceError(
        "Geometry is born/deleted over the shutter; deformation correspondence does not match");
  std::vector<std::size_t> result;
  result.reserve(base.keys.size());
  if (base.keys == sample.keys) {
    for (std::size_t i = 0; i < base.keys.size(); ++i)
      result.push_back(i);
    return result;
  }
  std::unordered_map<std::string, std::size_t> lookup;
  lookup.reserve(sample.keys.size());
  for (std::size_t i = 0; i < sample.keys.size(); ++i)
    if (!lookup.emplace(sample.keys[i], i).second)
      throw NativeDeformationSourceError("Duplicate deformation identity");
  for (const auto &key : base.keys) {
    const auto found = lookup.find(key);
    if (found == lookup.end())
      throw NativeDeformationSourceError(
          "Geometry identities change over the shutter; deformation correspondence does not match");
    result.push_back(found->second);
  }
  return result;
}
template <class Buffer>
std::vector<typename Buffer::value_type> aligned(Buffer values, const std::vector<std::size_t> &order) {
  using T = typename Buffer::value_type;
  if (values.size() != order.size())
    throw NativeDeformationSourceError("Deformation attribute correspondence does not match");
  bool identity = true;
  for (std::size_t i = 0; i < order.size(); ++i)
    identity &= order[i] == i;
  if (identity)
    return values;
  std::vector<T> result;
  result.reserve(order.size());
  for (const auto index : order)
    result.push_back(values[index]);
  return result;
}
using Face = std::array<int, 3>;
void deformation_dependencies(std::vector<std::int64_t> &result, SOP_Node &root) {
  // A switch can retain its nominal output while an edited inactive branch
  // changes other shutter poses. Cook/data IDs at the centre are insufficient.
  // Read public edit versions on the host, including uncooked wired inputs,
  // subnet contents and explicit/cooked references. Never read vertex data.
  std::vector<OP_Node *> pending{&root};
  std::map<int, OP_Node *> nodes;
  while (!pending.empty()) {
    auto *node = pending.back();
    pending.pop_back();
    if (!node || !nodes.emplace(node->getUniqueId(), node).second)
      continue;
    for (unsigned input = 0; input < node->nInputs(); ++input)
      pending.push_back(node->getInput(input));
    OP_NodeList references;
    node->getFullOpReferences(references, false);
    for (auto *reference : references)
      pending.push_back(reference);
    if (dynamic_cast<SOP_Node *>(node)) {
      OP_NodeList children;
      node->getAllChildren(children);
      for (auto *child : children)
        pending.push_back(child);
    }
  }
  result.push_back(nodes.size());
  for (const auto &[id, node] : nodes) {
    result.insert(result.end(),
                  {id, std::int64_t(node->getVersionParms()), node->getBypass(), node->getHardLock(), node->nInputs()});
    for (unsigned input = 0; input < node->nInputs(); ++input) {
      auto *value = node->getInput(input);
      result.push_back(value ? value->getUniqueId() : -1);
    }
  }
}
template <class Buffer, class Samples>
void shading_sample(const Buffer &centre, const Buffer &source, Samples &samples, bool &complete,
                    const std::vector<std::size_t> &order) {
  if (centre.empty())
    return;
  if (source.size() != centre.size() || order.size() != centre.size()) {
    complete = false;
    samples.clear();
    return;
  }
  if (!complete)
    return;
  auto &result = samples.emplace_back();
  result.reserve(order.size());
  for (const auto index : order)
    result.push_back(source.at(index));
}
void named_samples(std::vector<NamedAttribute> &centre, const std::vector<NamedAttribute> &sample,
                   const std::vector<std::size_t> &order) {
  for (auto &field : centre) {
    const auto found =
        std::find_if(sample.begin(), sample.end(), [&](const auto &value) { return value.name == field.name; });
    if (found == sample.end() || found->components != field.components) {
      field.motion_complete = false;
      field.motion_values.clear();
      continue;
    }
    shading_sample(field.values, found->values, field.motion_values, field.motion_complete, order);
  }
}
Face oriented_face(int a, int b, int c) {
  if (a <= b && a <= c)
    return {a, b, c};
  if (b <= a && b <= c)
    return {b, c, a};
  return {c, a, b};
}
struct FaceHash {
  std::size_t operator()(const Face &face) const {
    std::size_t hash = 2166136261u;
    for (const auto index : face) {
      hash ^= std::size_t(index);
      hash *= 16777619u;
    }
    return hash;
  }
};
void mesh_sample(MeshData &base, MeshData &sample, const std::vector<std::size_t> &order, BindingSample &bindings,
                 const std::vector<int> &palette, bool nominal = false) {
  if (base.corner_normals.size() != sample.corner_normals.size())
    throw NativeDeformationSourceError("Mesh split-normal correspondence changes over the shutter");
  std::vector<std::size_t> corner_order;
  const auto commit = [&](std::vector<Float3> normals) {
    auto positions = aligned(std::move(sample.positions), order);
    if (nominal) {
      MeshNominalPose pose;
      pose.positions = std::move(positions);
      pose.corner_normals = std::move(normals);
      for (const auto corner : corner_order) {
        if (!sample.corner_uv.empty())
          pose.corner_uv.push_back(sample.corner_uv.at(corner));
        if (!sample.corner_colors.empty())
          pose.corner_colors.push_back(sample.corner_colors.at(corner));
      }
      base.nominal_pose = std::move(pose);
      if (!sample.pointiness.empty())
        base.pointiness = aligned(std::move(sample.pointiness), order);
      for (const auto &field : sample.attributes) {
        auto &output = base.nominal_pose->attributes.emplace_back();
        output.name = field.name;
        output.components = field.components;
        for (const auto corner : corner_order)
          output.values.push_back(field.values.at(corner));
      }
    } else {
      base.motion.positions.push_back(std::move(positions));
      if (!normals.empty())
        base.motion_corner_normals.push_back(std::move(normals));
      const auto attribute = [&](const auto &centre, const auto &source, auto &samples, bool &complete) {
        if (centre.empty())
          return;
        if (source.size() != centre.size()) {
          complete = false;
          samples.clear();
          return;
        }
        if (!complete)
          return;
        auto &values = samples.emplace_back();
        values.reserve(corner_order.size());
        for (const auto corner : corner_order)
          values.push_back(source.at(corner));
      };
      attribute(base.corner_uv, sample.corner_uv, base.motion_corner_uv, base.motion_uv_complete);
      attribute(base.corner_colors, sample.corner_colors, base.motion_corner_colors, base.motion_color_complete);
      named_samples(base.attributes, sample.attributes, corner_order);
    }
  };
  if (base.subdivision.has_value() != sample.subdivision.has_value())
    throw NativeDeformationSourceError("Subdivision changes over the shutter");
  if (base.subdivision) {
    const auto &a = *base.subdivision;
    const auto &b = *sample.subdivision;
    if (a.settings != b.settings || a.face_sizes.size() != b.face_sizes.size() ||
        a.edge_creases.size() != b.edge_creases.size() || a.vertex_creases.size() != b.vertex_creases.size())
      throw NativeDeformationSourceError("Subdivision topology/settings change over the shutter");
    std::vector<int> inverse(order.size());
    for (std::size_t i = 0; i < order.size(); ++i)
      inverse[order[i]] = int(i);
    using Polygon = std::vector<int>;
    std::map<Polygon, std::pair<std::size_t, std::size_t>> faces;
    const auto oriented = [](Polygon polygon) {
      auto lowest = std::min_element(polygon.begin(), polygon.end());
      std::rotate(polygon.begin(), lowest, polygon.end());
      return polygon;
    };
    std::size_t offset = 0;
    for (std::size_t f = 0; f < b.face_sizes.size(); ++f) {
      Polygon polygon;
      for (int c = 0; c < b.face_sizes[f]; ++c)
        polygon.push_back(inverse.at(b.face_corners.at(offset + c)));
      if (!faces.emplace(oriented(std::move(polygon)), std::pair{f, offset}).second)
        throw NativeDeformationSourceError("Duplicate coarse faces require explicit deformation provenance");
      offset += b.face_sizes[f];
    }
    std::vector<Float3> normals;
    offset = 0;
    for (std::size_t f = 0; f < a.face_sizes.size(); ++f) {
      Polygon polygon;
      for (int c = 0; c < a.face_sizes[f]; ++c)
        polygon.push_back(a.face_corners.at(offset + c));
      const auto found = faces.find(oriented(polygon));
      if (found == faces.end())
        throw NativeDeformationSourceError("Coarse polygon connectivity changes over the shutter");
      bindings.append(palette.at(b.face_material.empty() ? 0 : b.face_material.at(found->second.first)));
      if (nominal || !base.corner_normals.empty() || !base.corner_uv.empty() || !base.corner_colors.empty() ||
          !base.attributes.empty())
        for (const auto point : polygon) {
          bool matched = false;
          for (int c = 0; c < b.face_sizes[found->second.first]; ++c)
            if (inverse[b.face_corners[found->second.second + c]] == point) {
              const auto corner = found->second.second + c;
              corner_order.push_back(corner);
              if (!base.corner_normals.empty())
                normals.push_back(sample.corner_normals.at(corner));
              matched = true;
              break;
            }
          if (!matched)
            throw NativeDeformationSourceError("Coarse normal correspondence changes over the shutter");
        }
      offset += a.face_sizes[f];
    }
    std::map<std::pair<int, int>, float> base_edges, sample_edges;
    for (const auto &e : a.edge_creases) {
      auto pair = std::minmax(e.a, e.b);
      base_edges[{pair.first, pair.second}] = e.sharpness;
    }
    for (const auto &e : b.edge_creases) {
      auto pair = std::minmax(inverse.at(e.a), inverse.at(e.b));
      sample_edges[{pair.first, pair.second}] = e.sharpness;
    }
    std::map<int, float> base_vertices, sample_vertices;
    for (const auto &v : a.vertex_creases)
      base_vertices[v.point] = v.sharpness;
    for (const auto &v : b.vertex_creases)
      sample_vertices[inverse.at(v.point)] = v.sharpness;
    if (base_edges != sample_edges || base_vertices != sample_vertices)
      throw NativeDeformationSourceError("Subdivision creases change over the shutter");
    commit(std::move(normals));
    return;
  }
  if (base.triangles.size() != sample.triangles.size())
    throw NativeDeformationSourceError("Mesh topology changes over the shutter");
  if (base.corner_normals.size() != sample.corner_normals.size())
    throw NativeDeformationSourceError("Mesh split-normal correspondence changes over the shutter");
  std::vector<int> sample_to_base(order.size());
  for (std::size_t i = 0; i < order.size(); ++i)
    sample_to_base[order[i]] = int(i);
  std::unordered_map<Face, std::size_t, FaceHash> faces;
  faces.reserve(sample.triangles.size());
  for (std::size_t i = 0; i < sample.triangles.size(); ++i) {
    const auto &t = sample.triangles[i];
    const auto face = oriented_face(sample_to_base[t.a], sample_to_base[t.b], sample_to_base[t.c]);
    if (!faces.emplace(face, i).second)
      throw NativeDeformationSourceError(
          "Overlapping duplicate triangles need explicit primitive provenance for deformation correspondence");
  }
  std::vector<Float3> normals;
  normals.reserve(base.corner_normals.size());
  for (std::size_t index = 0; index < base.triangles.size(); ++index) {
    const auto &triangle = base.triangles[index];
    const auto found = faces.find(oriented_face(triangle.a, triangle.b, triangle.c));
    if (found == faces.end())
      throw NativeDeformationSourceError("Mesh topology/triangulation correspondence changes over the shutter");
    bindings.append(palette.at(sample.triangle_material.empty() ? 0 : sample.triangle_material.at(found->second)));
    if (nominal || !base.corner_normals.empty() || !base.corner_uv.empty() || !base.corner_colors.empty() ||
        !base.attributes.empty()) {
      const auto &sample_triangle = sample.triangles[found->second];
      const int corners[] = {sample_triangle.a, sample_triangle.b, sample_triangle.c};
      for (const auto vertex : {triangle.a, triangle.b, triangle.c}) {
        bool matched = false;
        for (int corner = 0; corner < 3; ++corner)
          if (sample_to_base[corners[corner]] == vertex) {
            const auto offset = found->second * 3 + corner;
            corner_order.push_back(offset);
            if (!base.corner_normals.empty())
              normals.push_back(sample.corner_normals[offset]);
            matched = true;
            break;
          }
        if (!matched)
          throw NativeDeformationSourceError("Mesh split-normal corner correspondence does not match");
      }
    }
  }
  commit(std::move(normals));
}
// Recover shader fields before the strict engine boundary. A constant owned
// requested-frame field is a complete render payload, even when its source was
// missing at other shutter times. Geometry positions and their correspondence
// remain untouched; no placeholder coordinate is invented when nominal data is
// unavailable. Unsupported shader/basis evaluation retains the core's policy.
void recover_mesh_shading(MeshData &mesh, std::vector<SourceDiagnostic> &diagnostics, const std::string &source) {
  const auto *nominal = mesh.nominal_pose ? &*mesh.nominal_pose : nullptr;
  const auto warn = [&](const std::string &code, const std::string &name, bool available) {
    const auto action = available ? "Holding the valid requested-frame " + name +
                                        " throughout the shutter; retaining geometry and transform motion"
                                  : "No usable requested-frame " + name +
                                        " exists; omitting this field and recovering dependent shader branches while "
                                        "retaining geometry motion";
    const auto found = std::find_if(diagnostics.begin(), diagnostics.end(), [&](const auto &issue) {
      return issue.code == code && (code != "geometry.shutter_attribute" || issue.attribute == name);
    });
    if (found != diagnostics.end())
      found->action = action;
    else
      diagnostics.push_back({code, source, mesh.key,
                             name + " is unavailable or changes type at a shutter/requested-frame sample", action,
                             "warning", code == "geometry.shutter_attribute" ? name : std::string{}});
  };
  const auto recover = [&](auto &values, auto &samples, bool &complete, const auto &intended, const char *name) {
    if (values.size() != intended.size())
      complete = false;
    if (complete)
      return;
    values = intended;
    samples.clear();
    complete = true;
    warn(std::string("geometry.shutter_") + name, name, !values.empty());
  };
  recover(mesh.corner_uv, mesh.motion_corner_uv, mesh.motion_uv_complete, nominal ? nominal->corner_uv : mesh.corner_uv,
          "uv");
  recover(mesh.corner_colors, mesh.motion_corner_colors, mesh.motion_color_complete,
          nominal ? nominal->corner_colors : mesh.corner_colors, "color");
  std::vector<NamedAttribute> fields;
  fields.reserve(mesh.attributes.size());
  std::set<std::string> existing;
  for (auto &field : mesh.attributes) {
    existing.insert(field.name);
    const NamedAttribute *intended = &field;
    if (nominal) {
      const auto found = std::find_if(nominal->attributes.begin(), nominal->attributes.end(),
                                      [&](const auto &value) { return value.name == field.name; });
      intended = found == nominal->attributes.end() ? nullptr : &*found;
    }
    if (!field.motion_complete || !intended || field.components != intended->components ||
        field.values.size() != intended->values.size()) {
      warn("geometry.shutter_attribute", field.name, intended && !intended->values.empty());
      if (!intended || intended->values.empty())
        continue;
      field.components = intended->components;
      field.values = intended->values;
      field.motion_values.clear();
      field.motion_complete = true;
    }
    fields.push_back(std::move(field));
  }
  if (nominal)
    for (const auto &field : nominal->attributes)
      if (!existing.contains(field.name)) {
        warn("geometry.shutter_attribute", field.name, true);
        auto owned = field;
        owned.motion_values.clear();
        owned.motion_complete = true;
        fields.push_back(std::move(owned));
      }
  mesh.attributes = std::move(fields);
}
void add_sample(Snapshot &base, Snapshot sample, bool nominal = false, std::size_t step = 0) {
  if (base.data.index() != sample.data.index())
    throw NativeDeformationSourceError(
        "Geometry type changes over the shutter; deformation correspondence does not match");
  const auto palette = binding_palette(base, sample);
  BindingSample bindings;
  std::visit(
      [&](auto &data) {
        using Data = std::decay_t<decltype(data)>;
        auto &other = std::get<Data>(sample.data);
        if constexpr (std::is_same_v<Data, CurveData>) {
          const auto curves = correspondence(base.curves.curves, sample.curves.curves);
          std::vector<std::size_t> order;
          order.reserve(data.positions.size());
          for (std::size_t i = 0; i < curves.size(); ++i) {
            bindings.append(palette.at(other.material_slots.empty() ? 0 : other.material_slots.at(curves[i])));
            const auto begin = std::size_t(data.first_key[i]),
                       end = i + 1 < curves.size() ? std::size_t(data.first_key[i + 1]) : data.positions.size();
            const auto j = curves[i], sample_begin = std::size_t(other.first_key[j]),
                       sample_end =
                           j + 1 < curves.size() ? std::size_t(other.first_key[j + 1]) : other.positions.size();
            if (end - begin != sample_end - sample_begin)
              throw NativeDeformationSourceError("Curve key count changes over the shutter");
            for (std::size_t k = 0; k < end - begin; ++k) {
              if (base.curves.keys.keys[begin + k] != sample.curves.keys.keys[sample_begin + k])
                throw NativeDeformationSourceError("Curve key connectivity/identity changes over the shutter");
              order.push_back(sample_begin + k);
            }
          }
          if (nominal)
            return;
          data.motion.positions.push_back(aligned(std::move(other.positions), order));
          data.motion.radii.push_back(aligned(std::move(other.radii), order));
          named_samples(data.attributes, other.attributes, order);
          shading_sample(data.colors, other.colors, data.motion_colors, data.motion_color_complete, order);
          shading_sample(data.uv, other.uv, data.motion_uv, data.motion_uv_complete, curves);
        } else {
          const auto order = correspondence(base.points, sample.points);
          if constexpr (std::is_same_v<Data, MeshData>)
            mesh_sample(data, other, order, bindings, palette, nominal);
          else {
            for (std::size_t i = 0; i < order.size(); ++i)
              bindings.append(palette.at(other.material_slots.empty() ? 0 : other.material_slots.at(order[i])));
            if (bool(data.splats) != bool(other.splats))
              throw NativeDeformationSourceError(
                  "Point rendering changes between Gaussian splats and spheres over the shutter");
            if (data.splats && !nominal) {
              auto &g = *data.splats;
              auto &sample = *other.splats;
              if (g.scene_linear_color != sample.scene_linear_color || g.harmonics.empty() != sample.harmonics.empty())
                throw NativeDeformationSourceError("Gaussian radiance encoding changes over the shutter");
              g.motion_scales.push_back(aligned(std::move(sample.scales), order));
              g.motion_rotations.push_back(aligned(std::move(sample.rotations), order));
              g.motion_radiance.push_back(aligned(std::move(sample.radiance), order));
              if (!g.harmonics.empty() && g.harmonics != aligned(std::move(sample.harmonics), order) &&
                  std::none_of(base.source_diagnostics.begin(), base.source_diagnostics.end(),
                               [](const auto &d) { return d.code == "geometry.gsplat_sh_motion"; }))
                base.source_diagnostics.push_back(
                    {"geometry.gsplat_sh_motion", "", data.key,
                     "Spherical harmonic coefficients or their restorient rotation change over the shutter",
                     "The pinned Cycles engine holds higher harmonic bands at the shutter centre; position, scale, "
                     "orientation, DC and opacity retain motion",
                     "warning", "GS_SPH_R/GS_SPH_G/GS_SPH_B"});
            }
            if (nominal)
              return;
            data.motion.positions.push_back(aligned(std::move(other.positions), order));
            data.motion.radii.push_back(aligned(std::move(other.radii), order));
            named_samples(data.attributes, other.attributes, order);
            shading_sample(data.colors, other.colors, data.motion_colors, data.motion_color_complete, order);
            shading_sample(data.uv, other.uv, data.motion_uv, data.motion_uv_complete, order);
          }
        }
      },
      base.data);
  if (nominal)
    base.nominal_bindings = std::move(bindings);
  else
    base.shutter_bindings.at(step) = std::move(bindings);
}
} // namespace
MeshData assemble_external_mesh_motion(const MeshData &nominal,
                                       const std::vector<std::shared_ptr<const MeshData>> &samples,
                                       const NativeMotionWindow &window, const std::string &source,
                                       std::vector<SourceDiagnostic> &diagnostics) {
  if (!window.enabled || samples.empty())
    return nominal;
  try {
    if (samples.size() != window.times.size() ||
        std::any_of(samples.begin(), samples.end(), [](const auto &sample) { return !sample; }))
      throw NativeDeformationSourceError("External mesh is missing a shutter sample");
    auto mesh = *samples.at(samples.size() / 2);
    mesh.motion.positions.clear();
    mesh.motion_corner_normals.clear();
    mesh.motion_corner_uv.clear();
    mesh.motion_corner_colors.clear();
    mesh.nominal_pose.reset();
    for (auto &field : mesh.attributes)
      field.motion_values.clear();
    std::vector<std::size_t> order(mesh.positions.size());
    std::iota(order.begin(), order.end(), 0);
    const auto append = [&](const MeshData &input, bool requested) {
      if (input.positions.size() != mesh.positions.size())
        throw NativeDeformationSourceError("External mesh point count changes over the shutter");
      // Material slots must retain their authored correspondence. The normal
      // material resolver owns shader conversion; this routine owns geometry.
      const auto &slots = input.subdivision ? input.subdivision->face_material : input.triangle_material;
      int maximum = 0;
      for (const int slot : slots) {
        if (slot < 0)
          throw NativeDeformationSourceError("External mesh has an invalid material slot");
        maximum = std::max(maximum, slot);
      }
      std::vector<int> palette(std::size_t(maximum) + 1);
      std::iota(palette.begin(), palette.end(), 0);
      BindingSample bindings;
      auto sample = input;
      mesh_sample(mesh, sample, order, bindings, palette, requested);
    };
    for (std::size_t i = 0; i < samples.size(); ++i)
      if (i != samples.size() / 2)
        append(*samples[i], false);
    if (window.nominal != window.centre) {
      append(nominal, true);
      // Blender computes Pointiness on the requested frame (core/mesh_pointiness.h reads the
      // nominal pose's positions), so it needs that frame's Blender vertex normals as well.
      if (!nominal.blender_vertex_normals.empty())
        mesh.blender_vertex_normals = nominal.blender_vertex_normals;
    }
    recover_mesh_shading(mesh, diagnostics, source);
    return mesh;
  } catch (const NativeDeformationSourceError &error) {
    diagnostics.push_back({"motion.external_mesh_correspondence", source, nominal.key, error.what(),
                           "Rendering requested-frame geometry without deformation blur; retaining placement motion"});
    return nominal;
  }
}
std::vector<std::int64_t> deformation_payload_identifiers(const GU_Detail &source, const GeometryRequest &request,
                                                          const NativeMotionWindow &window, NativeGeometryKind kind) {
  auto result = point_curve_identifiers(source, kind == NativeGeometryKind::Curves);
  if (kind == NativeGeometryKind::Points) {
    gaussian_splat_identifiers(result, source, request);
    result.push_back(request.point_splat_default_material);
  }
  named_attribute_identifiers(result, source, request);
  for (const auto *attribute : {source.findVertexAttribute("N"), source.findPointAttribute("N")})
    result.push_back(attribute ? attribute->getDataId() : GA_INVALID_DATAID);
  result.push_back(request.smooth ? 1 : 0);
  result.push_back(3);
  result.push_back(std::int64_t(kind));
  subdivision_identifiers(result, request.subdivision);
  if (request.subdivision.type != SubdivisionType::None && request.subdivision.use_creases)
    for (const auto *attribute :
         {source.findVertexAttribute("creaseweight"), source.findPrimitiveAttribute("creaseweight"),
          source.findPointAttribute("cornerweight")})
      result.push_back(attribute ? attribute->getDataId() : GA_INVALID_DATAID);
  result.push_back(static_cast<std::int64_t>(request.material_mode));
  if (request.material_mode == NativeMaterialMode::Native)
    object_material_identifiers(result, request);
  if (request.material_mode == NativeMaterialMode::Native)
    for (const auto owner : {GA_ATTRIB_PRIMITIVE, GA_ATTRIB_DETAIL, GA_ATTRIB_POINT, GA_ATTRIB_VERTEX})
      for (const auto *name : {"shop_materialpath", "material_override", "material_stylesheet"}) {
        const auto *attribute = source.findAttribute(owner, name);
        result.push_back(attribute ? attribute->getDataId() : GA_INVALID_DATAID);
      }
  if (kind == NativeGeometryKind::Points)
    result.push_back(std::bit_cast<std::int32_t>(request.point_radius));
  else if (kind == NativeGeometryKind::Curves) {
    result.push_back(std::bit_cast<std::int32_t>(request.curve_width));
    result.push_back(request.curve_segments);
  }
  for (const auto time : window.times)
    result.push_back(std::bit_cast<std::int64_t>(time));
  return result;
}
std::vector<std::int64_t> deformation_identifiers(const GU_Detail &source, SOP_Node &sop,
                                                  const GeometryRequest &request, const NativeMotionWindow &window,
                                                  NativeGeometryKind kind) {
  auto result = deformation_payload_identifiers(source, request, window, kind);
  result.push_back(sop.getCookCount());
  deformation_dependencies(result, sop);
  return result;
}
std::vector<std::int64_t> deformation_identifiers(const GU_Detail &source, NativeRootSource &sop,
                                                  const GeometryRequest &request, const NativeMotionWindow &window,
                                                  NativeGeometryKind kind) {
  auto result = deformation_payload_identifiers(source, request, window, kind);
  result.push_back(sop.getCookCount());
  sop.dependencies(result);
  return result;
}
void native_source_dependencies(std::vector<std::int64_t> &result, SOP_Node &root) {
  deformation_dependencies(result, root);
}
struct PreparedDeformation {
  struct Sample {
    std::shared_ptr<const PreservedDetail> detail;
    std::map<NativeGeometryKind, GeometryRequest> requests;
    std::map<NativeGeometryKind, std::shared_ptr<const NativeGeometrySelection>> selections;
  };
  NativeMotionWindow window;
  std::vector<Sample> samples;
  Sample nominal;
  std::string shutter_cook_warning;
  std::map<NativeGeometryKind, std::vector<std::int64_t>> identifiers;
};
std::shared_ptr<const PreparedDeformation>
prepare_deformation_samples(NativeGeometryKind kind, NativeDeformationSample nominal,
                            std::vector<NativeDeformationSample> samples, std::vector<std::int64_t> identifiers,
                            const NativeMotionWindow &window, std::string warning) {
  if (!nominal.detail || samples.size() != window.times.size() || !window.enabled || samples.size() < 3 ||
      samples.size() % 2 == 0)
    throw std::logic_error("Malformed owned deformation sample recipe");
  if (kind != NativeGeometryKind::Mesh && kind != NativeGeometryKind::Points && kind != NativeGeometryKind::Curves)
    throw std::logic_error("Unsupported owned deformation component");
  auto prepared = std::make_shared<PreparedDeformation>();
  prepared->window = window;
  prepared->shutter_cook_warning = std::move(warning);
  for (auto &sample : samples) {
    if (!sample.detail && prepared->shutter_cook_warning.empty())
      throw std::logic_error("Missing acquired deformation sample without source diagnostic");
    prepared->samples.push_back(
        {std::move(sample.detail), {{kind, std::move(sample.request)}}, {{kind, std::move(sample.selection)}}});
  }
  prepared->nominal = {
      std::move(nominal.detail), {{kind, std::move(nominal.request)}}, {{kind, std::move(nominal.selection)}}};
  auto fields = prepared->nominal.requests.at(kind).shader_attributes;
  for (const auto &sample : prepared->samples) {
    const auto &names = sample.requests.at(kind).shader_attributes;
    fields.insert(names.begin(), names.end());
  }
  prepared->nominal.requests.at(kind).shader_attributes = fields;
  for (auto &sample : prepared->samples)
    sample.requests.at(kind).shader_attributes = fields;
  prepared->identifiers.emplace(kind, std::move(identifiers));
  return prepared;
}
// Only an explicit SOP cook failure is recoverable here. Resolver, ownership
// and programming exceptions must still abort acquisition.
class NativeShutterCookError : public std::runtime_error {
public:
  using std::runtime_error::runtime_error;
};
void check_shutter_interrupt() {
  if (auto *interrupt = UTgetInterrupt(); interrupt && interrupt->getOpDepth() && interrupt->opInterrupt())
    throw MotionCaptureInterrupted();
}

std::shared_ptr<const PreparedDeformation>
prepare_deformation_set(SOP_Node &sop, const std::map<NativeGeometryKind, GeometryRequest> &requests,
                        const NativeMotionWindow &window, ExtractionStats &stats,
                        const NativeMaterialPreparation &prepare_materials) {
  if (!UT_Thread::isMainThread())
    throw std::logic_error("Deformation preparation requires the host thread");
  if (requests.empty())
    throw std::invalid_argument("Deformation preparation requires geometry components");
  if (!window.enabled || window.times.size() < 3 || window.times.size() % 2 == 0 ||
      window.times.size() != window.deltas.size())
    throw std::invalid_argument("Deformation preparation requires an odd sampled shutter window");
  auto prepared = std::make_shared<PreparedDeformation>();
  prepared->window = window;
  prepared->samples.resize(window.times.size());
  const auto &path = requests.begin()->second.sop_path;
  bool native = false;
  std::set<NativeMaterialAssignment> material_paths;
  for (const auto &[kind, request] : requests) {
    if (kind != NativeGeometryKind::Mesh && kind != NativeGeometryKind::Points && kind != NativeGeometryKind::Curves)
      throw std::invalid_argument("Native deformation requires polygon meshes, point spheres or open linear curves; "
                                  "packed prototypes/volumes need a separate method: " +
                                  path);
    if (request.material_mode == NativeMaterialMode::Native) {
      native = true;
      material_paths.insert(request.material_assignment());
    }
  }
  const auto cook_at = [&](double time, bool check_interrupt = true) {
    if (check_interrupt)
      check_shutter_interrupt();
    const auto cook = Clock::now();
    const auto before = sop.getCookCount();
    OP_Context context(time);
    auto handle = sop.getCookedGeoHandle(context);
    stats.cooking_seconds += elapsed(cook);
    stats.motion_cooks += std::uint64_t(std::max<exint>(0, sop.getCookCount() - before));
    if (check_interrupt)
      check_shutter_interrupt();
    if (!handle || sop.error() >= UT_ERROR_ABORT)
      throw NativeShutterCookError("SOP deformation cook failed at time " + std::to_string(time) + " seconds: " + path);
    return handle;
  };
  NativeStyleCache style_cache;
  NativeMaterialBindingCache binding_cache;
  const auto style_requests = [&](const GU_Detail &source, auto &values, double time, bool displacement_only) {
    const bool local_styles = source.findGlobalAttribute("material_stylesheet") ||
                              source.findPrimitiveAttribute("material_stylesheet") ||
                              source.findPointAttribute("material_stylesheet");
    const bool curve_keys =
        source.findPointAttribute("shop_materialpath") || source.findPointAttribute("material_override") ||
        source.findPointAttribute("material_stylesheet") || source.findVertexAttribute("shop_materialpath") ||
        source.findVertexAttribute("material_override");
    if (!local_styles && std::none_of(values.begin(), values.end(), [&](const auto &entry) {
          const auto &request = entry.second;
          return request.material_mode == NativeMaterialMode::Native &&
                 ((entry.first == NativeGeometryKind::Curves && curve_keys) ||
                  !request.object_material_samples.empty() || !request.stylesheet.empty());
        }))
      return;
    const auto partitions = partition_geometry(source);
    for (auto &[kind, request] : values) {
      if (request.material_mode != NativeMaterialMode::Native ||
          (!(kind == NativeGeometryKind::Curves && curve_keys) && request.object_material_samples.empty() &&
           request.stylesheet.empty() && !local_styles))
        continue;
      if (time != window.nominal)
        request = sample_object_materials(request, time);
      request.native_styles = style_cache.prepare(source, request.stylesheet, request.stylesheet_resources, time,
                                                  request.object_path.empty() ? request.sop_path : request.object_path,
                                                  nullptr, nullptr, &partitions, &request.render_types);
      const auto *selection = partitions.find(kind);
      if (!selection)
        continue;
      const auto &state = binding_cache.inspect(source, selection);
      request.native_material_policy = state.policy;
      auto lookup =
          freeze_native_materials(state, request, kind == NativeGeometryKind::Points, false, displacement_only);
      request.native_materials = freeze_styled_materials(source, *selection, request, std::move(lookup));
      request.native_fallback.reset();
      if (prepare_materials) {
        const auto fields = prepare_materials(*request.native_materials);
        request.shader_attributes.insert(fields.begin(), fields.end());
        if (kind == NativeGeometryKind::Mesh)
          request.pointiness |= prepare_materials.pointiness(*request.native_materials);
      }
    }
  };
  const auto acquire = [&](std::size_t index) {
    auto handle = cook_at(window.times[index]);
    const auto convert = Clock::now();
    auto &sample = prepared->samples[index];
    sample.detail = std::make_shared<PreservedDetail>(handle.duplicateGeometry(GA_DATA_ID_CLONE));
    const auto materials =
        native ? freeze_native_material_candidates(*sample.detail->get(), material_paths, true) : nullptr;
    const auto fields = materials && prepare_materials ? prepare_materials(*materials) : std::set<std::string>{};
    sample.requests = requests;
    for (auto &[kind, request] : sample.requests) {
      request.shader_attributes.insert(fields.begin(), fields.end());
      if (kind == NativeGeometryKind::Mesh && materials && prepare_materials)
        request.pointiness |= prepare_materials.pointiness(*materials);
    }
    for (auto &[kind, request] : sample.requests)
      if (request.material_mode == NativeMaterialMode::Native)
        request.native_materials = materials;
    style_requests(*sample.detail->get(), sample.requests, window.times[index], true);
    stats.conversion_seconds += elapsed(convert);
  };
  try {
    acquire(window.times.size() / 2);
    for (std::size_t i = 0; i < window.times.size(); ++i)
      if (i != window.times.size() / 2)
        acquire(i);
  } catch (const NativeShutterCookError &error) {
    // A usable nominal cook below is required before publishing recovery.
    // Never let an incomplete shutter sample reach background conversion.
    prepared->shutter_cook_warning = error.what();
  } catch (const MotionCaptureInterrupted &) {
    // Do not start another cook after Esc. The next acquisition requests its
    // own time, and this interrupted capture publishes no geometry.
    throw;
  } catch (...) {
    // Restore the requested frame after an ordinary preparation failure.
    const auto original = std::current_exception();
    cook_at(window.nominal, false);
    std::rethrow_exception(original);
  }
  const auto nominal = cook_at(window.nominal);
  if (window.nominal == window.centre && prepared->samples[window.times.size() / 2].detail)
    prepared->nominal = prepared->samples[window.times.size() / 2];
  else {
    prepared->nominal.detail = std::make_shared<PreservedDetail>(nominal.duplicateGeometry(GA_DATA_ID_CLONE));
    const auto materials =
        native ? freeze_native_material_candidates(*prepared->nominal.detail->get(), material_paths) : nullptr;
    const auto fields = materials && prepare_materials ? prepare_materials(*materials) : std::set<std::string>{};
    prepared->nominal.requests = requests;
    for (auto &[kind, request] : prepared->nominal.requests) {
      request.shader_attributes.insert(fields.begin(), fields.end());
      if (kind == NativeGeometryKind::Mesh && materials && prepare_materials)
        request.pointiness |= prepare_materials.pointiness(*materials);
    }
    for (auto &[kind, request] : prepared->nominal.requests)
      if (request.material_mode == NativeMaterialMode::Native)
        request.native_materials = materials;
    style_requests(*prepared->nominal.detail->get(), prepared->nominal.requests, window.nominal, false);
  }
  for (const auto &[kind, request] : requests) {
    auto fields = prepared->nominal.requests.at(kind).shader_attributes;
    bool pointiness = prepared->nominal.requests.at(kind).pointiness;
    for (const auto &sample : prepared->samples)
      if (const auto found = sample.requests.find(kind); found != sample.requests.end()) {
        fields.insert(found->second.shader_attributes.begin(), found->second.shader_attributes.end());
        pointiness |= found->second.pointiness;
      }
    prepared->nominal.requests.at(kind).pointiness = pointiness;
    prepared->nominal.requests.at(kind).shader_attributes = fields;
    for (auto &sample : prepared->samples)
      if (const auto found = sample.requests.find(kind); found != sample.requests.end()) {
        found->second.shader_attributes = fields;
        found->second.pointiness = pointiness;
      }
    prepared->identifiers.emplace(
        kind, deformation_identifiers(*nominal.gdp(), sop, prepared->nominal.requests.at(kind), window, kind));
  }
  return prepared;
}
std::map<NativeGeometryKind, NativeDeformation> convert_deformation_set(const PreparedDeformation &prepared,
                                                                        std::uint64_t revision, ExtractionStats &stats,
                                                                        bool recover_source) {
  const auto &window = prepared.window;
  const auto selection_at = [](const PreparedDeformation::Sample &sample, NativeGeometryKind kind,
                               NativeGeometryPartitions &fallback) {
    if (const auto found = sample.selections.find(kind); found != sample.selections.end() && found->second) {
      if (!found->second->active || found->second->kind != kind)
        throw std::logic_error("Malformed owned deformation selection");
      return found->second.get();
    }
    fallback = partition_geometry(*sample.detail->get());
    return fallback.find(kind);
  };
  const auto capture = [&](std::size_t index, NativeGeometryKind kind) {
    const auto convert = Clock::now();
    const auto &sample = prepared.samples.at(index);
    const auto &source = *sample.detail->get();
    NativeGeometryPartitions partitions;
    auto request = sample.requests.at(kind);
    // This intrinsic is nominal-frame data, just as in Blender. Do not
    // calculate an unused copy for every deformation shutter pose.
    request.pointiness &= window.nominal == window.times[index];
    const auto *selection = selection_at(sample, kind, partitions);
    if (!selection)
      throw NativeDeformationSourceError("Geometry type changes over the shutter: " + request.sop_path);
    auto result = snapshot(source, request, revision, true, *selection);
    stats.conversion_seconds += elapsed(convert);
    return result;
  };
  std::map<NativeGeometryKind, NativeDeformation> result;
  for (const auto &[kind, identifiers] : prepared.identifiers) {
    auto &item = result[kind];
    Snapshot data;
    try {
      if (!prepared.shutter_cook_warning.empty())
        throw NativeDeformationSourceError(prepared.shutter_cook_warning);
      data = capture(window.times.size() / 2, kind);
      initialize_bindings(data, window.times.size());
      for (std::size_t i = 0; i < window.times.size(); ++i)
        if (i != window.times.size() / 2) {
          auto sample = capture(i, kind);
          const auto convert = Clock::now();
          if (kind == NativeGeometryKind::Mesh) {
            const auto bad_normal = [](const Snapshot &value) {
              return std::any_of(value.source_diagnostics.begin(), value.source_diagnostics.end(),
                                 [](const auto &issue) { return issue.code == "geometry.invalid_normal"; });
            };
            if (bad_normal(data) || bad_normal(sample)) {
              // Keep valid deformation positions. All shutter samples
              // must use the same generated-normal policy.
              auto &base = std::get<MeshData>(data.data);
              base.corner_normals.clear();
              base.motion_corner_normals.clear();
              std::get<MeshData>(sample.data).corner_normals.clear();
              if (!bad_normal(data))
                for (auto issue : sample.source_diagnostics)
                  if (issue.code == "geometry.invalid_normal") {
                    issue.message += " at a shutter sample";
                    issue.action =
                        "Computing normals from geometry throughout the shutter; retaining deformation motion";
                    data.source_diagnostics.push_back(std::move(issue));
                  }
            }
          }
          for (auto issue : sample.source_diagnostics)
            if ((issue.code == "geometry.invalid_width" || issue.code == "geometry.invalid_pscale") &&
                std::none_of(data.source_diagnostics.begin(), data.source_diagnostics.end(),
                             [&](const auto &existing) { return existing.code == issue.code; })) {
              issue.message += " at a shutter sample";
              data.source_diagnostics.push_back(std::move(issue));
            }
          for (auto issue : sample.source_diagnostics)
            if (issue.code == "material.stylesheet_ignored" || issue.code == "material.unsupported_binding" ||
                issue.code == "material.override_capacity") {
              issue.message += " at a shutter sample";
              if (std::none_of(data.source_diagnostics.begin(), data.source_diagnostics.end(),
                               [&](const auto &current) {
                                 return current.code == issue.code && current.source == issue.source &&
                                        current.action == issue.action;
                               }))
                data.source_diagnostics.push_back(std::move(issue));
            }
          for (auto issue : sample.source_diagnostics)
            if (issue.code == "geometry.invalid_uv" || issue.code == "geometry.invalid_color" ||
                issue.code == "geometry.invalid_attribute") {
              issue.code = issue.code == "geometry.invalid_uv"      ? "geometry.shutter_uv"
                           : issue.code == "geometry.invalid_color" ? "geometry.shutter_color"
                                                                    : "geometry.shutter_attribute";
              if (std::none_of(data.source_diagnostics.begin(), data.source_diagnostics.end(),
                               [&](const auto &existing) {
                                 return existing.code == issue.code && existing.source == issue.source &&
                                        existing.attribute == issue.attribute;
                               })) {
                issue.message += " at a shutter sample";
                issue.action = "Retaining the centre attribute and independent geometry motion; recovering "
                               "incompatible displacement motion";
                data.source_diagnostics.push_back(std::move(issue));
              }
            }
          add_sample(data, std::move(sample), false, i);
          stats.conversion_seconds += elapsed(convert);
        }
      if (window.nominal != window.centre) {
        const auto &source = *prepared.nominal.detail->get();
        NativeGeometryPartitions partitions;
        const auto *selection = selection_at(prepared.nominal, kind, partitions);
        if (!selection)
          throw NativeDeformationSourceError("Requested-frame geometry type differs from the shutter");
        auto nominal = snapshot(source, prepared.nominal.requests.at(kind), revision, true, *selection);
        for (const auto &issue : nominal.source_diagnostics)
          if (issue.code == "geometry.invalid_uv" || issue.code == "geometry.invalid_color" ||
              issue.code == "geometry.invalid_attribute" || issue.code == "material.unsupported_binding" ||
              issue.code == "material.override_capacity")
            data.source_diagnostics.push_back(issue);
        const auto convert = Clock::now();
        add_sample(data, std::move(nominal), true);
        stats.conversion_seconds += elapsed(convert);
      }
      finish_bindings(data, prepared.nominal.requests.at(kind).sop_path);
      std::visit(
          [&](auto &geometry) {
            using Data = std::decay_t<decltype(geometry)>;
            if constexpr (std::is_same_v<Data, MeshData>)
              recover_mesh_shading(geometry, data.source_diagnostics, prepared.nominal.requests.at(kind).sop_path);
            const auto finish = [&](const auto &centre, auto &samples, bool complete, const char *name) {
              if (!complete &&
                  std::none_of(data.source_diagnostics.begin(), data.source_diagnostics.end(), [&](const auto &issue) {
                    return issue.code == std::string("geometry.shutter_") + name;
                  })) {
                data.source_diagnostics.push_back({std::string("geometry.shutter_") + name,
                                                   prepared.nominal.requests.at(kind).sop_path, geometry.key,
                                                   std::string(name) + " is unavailable at one or more shutter samples",
                                                   "Retaining the centre attribute and independent geometry motion; "
                                                   "recovering incompatible displacement motion"});
                // Hash-based shader inputs distinguish signed zero. Only
                // drop shutter fields when every consumed bit is unchanged.
              } else if (std::all_of(samples.begin(), samples.end(),
                                     [&](const auto &values) { return mesh_payload_equal(values, centre); }))
                samples.clear();
            };
            if constexpr (std::is_same_v<Data, MeshData>) {
              finish(geometry.corner_uv, geometry.motion_corner_uv, geometry.motion_uv_complete, "uv");
              finish(geometry.corner_colors, geometry.motion_corner_colors, geometry.motion_color_complete, "color");
            } else {
              finish(geometry.uv, geometry.motion_uv, geometry.motion_uv_complete, "uv");
              finish(geometry.colors, geometry.motion_colors, geometry.motion_color_complete, "color");
            }
            for (auto &field : geometry.attributes) {
              if (!field.motion_complete) {
                const auto source = prepared.nominal.requests.at(kind).sop_path;
                if (std::none_of(data.source_diagnostics.begin(), data.source_diagnostics.end(),
                                 [&](const auto &issue) {
                                   return issue.code == "geometry.shutter_attribute" && issue.attribute == field.name;
                                 }))
                  data.source_diagnostics.push_back(
                      {"geometry.shutter_attribute", source, geometry.key,
                       "Attribute " + field.name + " is missing or changes tuple size over the shutter",
                       "Retaining the centre field and independent geometry motion; recovering dependent displacement "
                       "motion",
                       "warning", field.name});
              } else if (std::all_of(field.motion_values.begin(), field.motion_values.end(),
                                     [&](const auto &values) { return mesh_payload_equal(values, field.values); }))
                field.motion_values.clear();
            }
          },
          data.data);
    } catch (const NativeDeformationSourceError &error) {
      if (!recover_source)
        throw;
      const auto &source = *prepared.nominal.detail->get();
      NativeGeometryPartitions partitions;
      const auto *selection = selection_at(prepared.nominal, kind, partitions);
      if (!selection)
        throw std::logic_error("Nominal deformation component was not retained");
      const auto &request = prepared.nominal.requests.at(kind);
      // Capture nominal shading without correspondence or deformation samples.
      // Failed motion must not change the requested render time.
      data = {};
      if (kind == NativeGeometryKind::Mesh)
        data.data = capture_mesh(source, request, revision, nullptr, nullptr, true, &data.material_references,
                                 selection, nullptr, nullptr, &data.source_diagnostics);
      else if (kind == NativeGeometryKind::Points)
        data.data = capture_points(source, request, revision, data.index_ids, nullptr, nullptr, true,
                                   &data.material_references, selection, &data.source_diagnostics);
      else
        data.data = capture_curves(source, request, revision, data.index_ids, nullptr, nullptr, true,
                                   &data.material_references, selection, &data.source_diagnostics);
      item.motion_warning =
          std::string(error.what()) + ". Rendering requested-frame geometry without deformation blur.";
    }
    item.identifiers = identifiers;
    item.index_ids = data.index_ids;
    item.material_references = std::move(data.material_references);
    item.shader_attributes = prepared.nominal.requests.at(kind).shader_attributes;
    item.source_diagnostics = std::move(data.source_diagnostics);
    std::visit(
        [&](const auto &geometry) {
          using Data = std::decay_t<decltype(geometry)>;
          if constexpr (std::is_same_v<Data, CurveData>)
            item.primitive_count = geometry.first_key.size();
          else if constexpr (std::is_same_v<Data, PointData>)
            item.primitive_count = geometry.positions.size();
        },
        data.data);
    item.data = std::move(data.data);
  }
  return result;
}
std::map<NativeGeometryKind, NativeDeformation>
capture_deformation_set(SOP_Node &sop, const std::map<NativeGeometryKind, GeometryRequest> &requests,
                        std::uint64_t revision, const NativeMotionWindow &window, ExtractionStats &stats,
                        const NativeMaterialPreparation &prepare_materials) {
  if (requests.empty())
    return {};
  const auto prepared = prepare_deformation_set(sop, requests, window, stats, prepare_materials);
  return convert_deformation_set(*prepared, revision, stats);
}
} // namespace cycles_houdini
