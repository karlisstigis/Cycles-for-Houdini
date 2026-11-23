// SPDX-License-Identifier: Apache-2.0
#include "point_curve_adapter.h"
#include "shading_attribute.h"
#include "named_attribute.h"
#include "size_attribute.h"
#include "identity_attribute.h"
#include "curve_sampling.h"
#include "gaussian_splats.h"
#include <algorithm>
#include <cmath>
#include <limits>
#include <set>
#include <stdexcept>
#include <unordered_set>
#include <unordered_map>
#include <GA/GA_Handle.h>
#include <GA/GA_Iterator.h>
#include <GEO/GEO_PrimPoly.h>
#include <GEO/GEO_PrimPart.h>
#include <GU/GU_Detail.h>
#include <UT/UT_ParallelUtil.h>
#include <atomic>
#include <cstdlib>
#include <cstring>
#include <xmmintrin.h>

namespace cycles_houdini {
namespace {
Float3 point(const GU_Detail &source, GA_Offset offset) {
  const auto p = source.getPos3(offset);
  return {float(p.x()), float(p.y()), float(p.z())};
}
Float3 vector(const UT_Vector3 &p) { return {float(p.x()), float(p.y()), float(p.z())}; }
std::int64_t identifier(const GA_Attribute *attribute) {
  return attribute ? attribute->getDataId() : GA_INVALID_DATAID;
}
float identity_value(const std::string &key) {
  std::uint32_t hash = 2166136261u;
  for (unsigned char byte : key) {
    hash ^= byte;
    hash *= 16777619u;
  }
  return float(hash >> 8) * (1.0f / 16777216.0f);
}
// Per-curve work on Houdini's TBB pool with the mesh converter's contract
// (mesh_adapter.cpp mesh_slots): each task writes only its own slots and runs
// with the caller's SSE control word, so every value is bitwise the serial one.
// CYCLES_HOUDINI_SERIAL_CONVERSION=1 runs it on this thread. Isolation keeps
// this thread from stealing an outer task that could wait on a lock held here.
class CurveFloatControl {
public:
  explicit CurveFloatControl(unsigned int control) : saved_(_mm_getcsr()) { _mm_setcsr(control); }
  ~CurveFloatControl() { _mm_setcsr(saved_); }
  CurveFloatControl(const CurveFloatControl &) = delete;
  CurveFloatControl &operator=(const CurveFloatControl &) = delete;

private:
  unsigned int saved_;
};
bool serial_curve_conversion() {
  const auto *value = std::getenv("CYCLES_HOUDINI_SERIAL_CONVERSION");
  return value && std::strcmp(value, "1") == 0;
}
template <class Body> void curve_slots(std::size_t count, std::size_t grain, const Body &body) {
  if (count <= grain || serial_curve_conversion()) {
    body(std::size_t{0}, count);
    return;
  }
  const auto control = _mm_getcsr();
  UTisolate([&] {
    UTparallelFor(UT_BlockedRange<std::size_t>(0, count, grain), [&](const UT_BlockedRange<std::size_t> &range) {
      CurveFloatControl scope(control);
      body(range.begin(), range.end());
    });
  });
}
void numeric(const GA_Attribute *attribute, const char *name, int components) {
  if (attribute && (attribute->getStorageClass() != GA_STORECLASS_FLOAT || attribute->getTupleSize() != components))
    throw std::invalid_argument(std::string(name) + " must be a float attribute with " + std::to_string(components) +
                                " component(s)");
}

} // namespace
CurveConversionCounters curve_conversion_counters;
std::vector<std::int64_t> point_curve_identifiers(const GU_Detail &source, bool rational_curves) {
  std::vector<std::int64_t> result = {identifier(source.getP()), source.getTopology().getDataId(),
                                      source.getPrimitiveList().getDataId(), source.getNumPoints(),
                                      source.getNumPrimitives()};
  for (const char *name : {"pscale", "width", "Cd", "uv", "id", "name"}) {
    result.push_back(identifier(source.findPointAttribute(name)));
    result.push_back(identifier(source.findVertexAttribute(name)));
    result.push_back(identifier(source.findPrimitiveAttribute(name)));
    result.push_back(identifier(source.findGlobalAttribute(name)));
  }
  if (rational_curves)
    result.push_back(identifier(source.findPointAttribute("Pw")));
  return result;
}
PointData capture_points(const GU_Detail &source, const GeometryRequest &request, std::uint64_t revision,
                         std::uint64_t &index_ids, std::vector<Float3> *velocities,
                         NativePointIdentities *correspondence, bool shading_attributes,
                         std::vector<NativeMaterialReference> *materials, const NativeGeometrySelection *selection,
                         std::vector<SourceDiagnostic> *diagnostics, std::vector<Float3> *accelerations) {
  const auto primitives = selection ? selection->primitive_range(source) : source.getPrimitiveRange();
  const auto points = selection ? selection->point_range(source) : source.getPointRange();
  const bool has_primitives = !primitives.isEmpty();
  PointData data;
  data.key = "points:" + request.geometry_key;
  data.revision = revision;
  data.material = request.material_key;
  std::optional<NativeGaussianSplats> splats;
  if (native_gaussian_splats(source, request)) {
    splats.emplace(source);
    data.splats.emplace();
    splats->initialize(*data.splats);
  }
  NativeNamedAttributes named(source, request, false, shading_attributes);
  const bool native = request.material_mode == NativeMaterialMode::Native;
  NativeMaterialBindings bindings(source, request, selection);
  if (native)
    data.material.clear();
  NativeSizeAttribute scales(native_size_attribute(source, false));
  const auto *color_attribute = shading_attributes ? named_attribute(source, "Cd", false) : nullptr;
  NativeShadingAttribute colors(color_attribute, false, request.sop_path, request.geometry_key);
  NativeShadingAttribute uv(shading_attributes ? source.findPointAttribute("uv") : nullptr, true, request.sop_path,
                            request.geometry_key);
  const auto *velocity_attribute = source.findPointAttribute("v");
  if (velocities) {
    numeric(velocity_attribute, "v", 3);
    if (!velocity_attribute && !points.isEmpty())
      throw std::invalid_argument("Velocity blur requires a three-component float point v attribute: " +
                                  request.sop_path);
  }
  GA_ROHandleV3 velocity(velocity_attribute);
  // Acceleration mode: point accel beside v; absent or not a float vector leaves it empty.
  const auto *accel_attribute = velocities && accelerations ? source.findPointAttribute("accel") : nullptr;
  GA_ROHandleV3 acceleration(accel_attribute && accel_attribute->getStorageClass() == GA_STORECLASS_FLOAT &&
                                     accel_attribute->getTupleSize() == 3
                                 ? accel_attribute
                                 : nullptr);
  const auto *name_attribute = source.findPointAttribute("name"), *id_attribute = source.findPointAttribute("id");
  NativeIdentityReader identities(name_attribute, id_attribute, points);
  if (source.getNumPoints() > std::numeric_limits<int>::max())
    throw std::invalid_argument("Too many points for the pinned Cycles geometry interface");
  // One byte per host point, instead of a tree allocation for every vertex.
  // Particle primitives may overlap; render a referenced point only once.
  std::vector<unsigned char> referenced(std::size_t(source.getNumPoints()), has_primitives ? 0 : 1);
  std::vector<int> point_slots(native && has_primitives ? std::size_t(source.getNumPoints()) : 0, -1);
  std::vector<GA_Offset> point_primitives(splats || named.has_primitive_field() || colors.owner == GA_ATTRIB_PRIMITIVE
                                              ? std::size_t(source.getNumPoints())
                                              : 0,
                                          GA_INVALID_OFFSET);
  std::size_t count = has_primitives ? 0 : std::size_t(points.getEntries());
  std::size_t conflicts = 0;
  std::string conflict_examples;
  if (has_primitives) {
    for (GA_Iterator iterator(primitives); !iterator.atEnd(); ++iterator) {
      const auto *primitive = dynamic_cast<const GEO_PrimParticle *>(source.getPrimitive(*iterator));
      if (!primitive)
        throw std::invalid_argument("Mixed particle/non-particle input requires separate inputs: " + request.sop_path);
      for (GA_Offset vertex : primitive->getVertexList()) {
        const auto offset = source.vertexPoint(vertex);
        const auto index = std::size_t(source.pointIndex(offset));
        auto &used = referenced[index];
        if (!point_primitives.empty()) {
          if (!used)
            point_primitives[index] = *iterator;
          else if (point_primitives[index] != *iterator)
            point_primitives[index] = GA_INVALID_OFFSET;
        }
        if (!used) {
          used = 1;
          ++count;
        }
        if (native) {
          const auto slot = bindings.point(offset, *iterator);
          if (point_slots[index] >= 0 && point_slots[index] != slot) {
            if (!(used & 2u)) {
              used |= 2u;
              if (conflicts++ < 16)
                conflict_examples += (conflict_examples.empty() ? "" : ", ") + std::to_string(index);
            }
          } else
            point_slots[index] = slot;
        }
      }
    }
  }
  data.positions.reserve(count);
  data.radii.reserve(count);
  data.randoms.reserve(count);
  if (splats) {
    auto &g = *data.splats;
    g.scales.reserve(count);
    g.rotations.reserve(count);
    g.radiance.reserve(count);
    if (splats->harmonics)
      g.harmonics.reserve(count);
  }
  named.reserve(count);
  if (native)
    data.material_slots.reserve(count);
  if (velocities)
    velocities->reserve(count);
  if (acceleration.isValid())
    accelerations->reserve(count);
  if (colors)
    data.colors.reserve(count);
  if (uv)
    data.uv.reserve(count);
  for (GA_Iterator iterator(points); !iterator.atEnd(); ++iterator) {
    const auto offset = *iterator;
    if (!referenced[std::size_t(source.pointIndex(offset))])
      continue;
    const auto key = identities.read(offset, data.positions.size(), index_ids);
    // Simulation attributes (age/life/dead/stopped) are ordinary user data.
    // The SOP supplies the rendered population; only explicit motion settings
    // and the velocity attribute control extrapolation here.
    data.positions.push_back(point(source, offset));
    data.radii.push_back(scales.read(offset, request.point_radius));
    if (splats)
      splats->read(*data.splats, offset,
                   point_primitives.empty() ? GA_INVALID_OFFSET
                                            : point_primitives[std::size_t(source.pointIndex(offset))]);
    if (named)
      named.read(GA_INVALID_OFFSET, offset,
                 point_primitives.empty() ? GA_INVALID_OFFSET
                                          : point_primitives[std::size_t(source.pointIndex(offset))]);
    if (native)
      data.material_slots.push_back(has_primitives ? point_slots[std::size_t(source.pointIndex(offset))]
                                                   : bindings.point(offset));
    if (velocities) {
      const auto value = vector(velocity.get(offset));
      if (!std::isfinite(value.x) || !std::isfinite(value.y) || !std::isfinite(value.z))
        throw std::invalid_argument("Non-finite particle velocity: " + request.sop_path);
      velocities->push_back(value);
      if (acceleration.isValid())
        accelerations->push_back(vector(acceleration.get(offset)));
    }
    if (shading_attributes)
      data.randoms.push_back(identity_value("point:" + key));
    if (correspondence) {
      correspondence->keys.push_back(key);
      correspondence->index_fallback |= key.starts_with("index:");
    }
    if (colors)
      data.colors.push_back(colors.read_element(
          GA_INVALID_OFFSET, offset,
          point_primitives.empty() ? GA_INVALID_OFFSET : point_primitives[std::size_t(source.pointIndex(offset))]));
    if (uv) {
      const auto value = uv.read(offset);
      data.uv.push_back({value.x, value.y});
    }
  }
  scales.finish(false, request.sop_path, request.geometry_key, diagnostics);
  colors.finish(data.colors, diagnostics);
  uv.finish(data.uv, diagnostics);
  named.finish(data.attributes, request, diagnostics);
  if (native)
    data.materials = bindings.materials();
  if (conflicts) {
    if (diagnostics)
      diagnostics->push_back(
          {"material.conflicting_particle_binding", request.sop_path, request.geometry_key,
           std::to_string(conflicts) +
               " shared points have conflicting containing-particle materials (examples: " + conflict_examples + ")",
           "Keeping the first containing primitive's material on affected points; explicit point bindings take "
           "precedence",
           "warning", "shop_materialpath"});
    // Ignored owners must not leave unused shader demand or diagnostics in
    // the rendered palette. Retain only slots actually used by the points.
    std::vector<int> remap(data.materials.size(), -1);
    std::vector<std::string> used_materials;
    for (auto &slot : data.material_slots) {
      auto &target = remap.at(std::size_t(slot));
      if (target < 0) {
        target = int(used_materials.size());
        used_materials.push_back(data.materials.at(std::size_t(slot)));
      }
      slot = target;
    }
    data.materials = std::move(used_materials);
  }
  if (materials) {
    *materials = native ? bindings.references() : std::vector<NativeMaterialReference>{};
    if (conflicts)
      std::erase_if(*materials, [&](const auto &ref) {
        return std::find(data.materials.begin(), data.materials.end(), ref.key) == data.materials.end();
      });
  }
  // The ordinary unassigned SOP fallback is diffuse. Gaussian splats need
  // Cycles' radiance-reading default instead; explicit native assignments
  // retain their authored shader and slot. An empty GSplat palette entry
  // represents that intrinsic engine default on precisely those points.
  if (splats) {
    if (native || request.point_splat_default_material) {
      if (data.material == request.material_key)
        data.material.clear();
      for (auto &material : data.materials)
        if (material == request.material_key)
          material.clear();
    } else if (request.material_mode == NativeMaterialMode::Reject && data.material == "default")
      data.material.clear();
  }
  return data;
}
// The serial curve converter: the reference for capture_curves below.
static CurveData capture_curves_serial(const GU_Detail &source, const GeometryRequest &request, std::uint64_t revision,
                                       std::uint64_t &index_ids, std::vector<Float3> *velocities,
                                       NativeCurveIdentities *correspondence, bool shading_attributes,
                                       std::vector<NativeMaterialReference> *materials,
                                       const NativeGeometrySelection *selection,
                                       std::vector<SourceDiagnostic> *diagnostics, std::vector<Float3> *accelerations) {
  const auto primitives = selection ? selection->primitive_range(source) : source.getPrimitiveRange();
  CurveData data;
  data.key = "curves:" + request.geometry_key;
  data.revision = revision;
  data.material = request.material_key;
  data.shape = request.curve_shape;
  NativeNamedAttributes named(source, request, true, shading_attributes);
  named.reserve(selection ? selection->vertex_range(source).getEntries() : source.getNumVertices());
  const bool native = request.material_mode == NativeMaterialMode::Native;
  NativeMaterialBindings bindings(source, request, selection);
  if (native) {
    data.material.clear();
    data.material_slots.reserve(std::size_t(primitives.getEntries()));
  }
  NativeSizeAttribute widths(native_size_attribute(source, true));
  const auto width_owner = widths.attribute ? widths.attribute->getOwner() : GA_ATTRIB_DETAIL;
  NativeShadingAttribute colors(shading_attributes ? named_attribute(source, "Cd", true) : nullptr, false,
                                request.sop_path, request.geometry_key);
  const auto *vertex_uv = shading_attributes ? source.findVertexAttribute("uv") : nullptr;
  NativeShadingAttribute uv(vertex_uv            ? vertex_uv
                            : shading_attributes ? source.findPointAttribute("uv")
                                                 : nullptr,
                            true, request.sop_path, request.geometry_key);
  const auto *velocity_attribute = source.findPointAttribute("v");
  if (velocities) {
    numeric(velocity_attribute, "v", 3);
    if (!velocity_attribute && source.getNumPoints())
      throw std::invalid_argument("Velocity blur requires a three-component float point v attribute: " +
                                  request.sop_path);
  }
  GA_ROHandleV3 velocity(velocity_attribute);
  const auto *accel_attribute = velocities && accelerations ? source.findPointAttribute("accel") : nullptr;
  GA_ROHandleV3 acceleration(accel_attribute && accel_attribute->getStorageClass() == GA_STORECLASS_FLOAT &&
                                     accel_attribute->getTupleSize() == 3
                                 ? accel_attribute
                                 : nullptr);
  const auto *name_attribute = source.findPrimitiveAttribute("name"),
             *id_attribute = source.findPrimitiveAttribute("id");
  NativeIdentityReader identities(name_attribute, id_attribute, primitives);
  const auto *key_names_attribute = correspondence ? source.findPointAttribute("name") : nullptr,
             *key_ids_attribute = correspondence ? source.findPointAttribute("id") : nullptr;
  NativeIdentityReader key_identities(key_names_attribute, key_ids_attribute,
                                      selection ? selection->point_range(source) : source.getPointRange());
  std::size_t failed_curves = 0;
  std::string failed_examples, first_failure;
  bool invalid_sampled_velocity = false;
  const auto omitted = [&](GA_Offset primitive, const std::string &reason) {
    if (failed_curves++ < 16) {
      if (!failed_examples.empty())
        failed_examples += ", ";
      failed_examples += std::to_string(source.primitiveIndex(primitive));
    }
    if (first_failure.empty())
      first_failure = reason;
  };
  for (GA_Iterator iterator(primitives); !iterator.atEnd(); ++iterator) {
    if (const auto *parametric = dynamic_cast<const GEO_Curve *>(source.getPrimitive(*iterator))) {
      auto sampled = sample_native_curve(source, *parametric, request.curve_segments);
      if (!sampled.error.empty()) {
        omitted(*iterator, sampled.error);
        continue;
      }
      std::size_t keys = 0;
      for (const auto &path : sampled.paths)
        keys += path.size();
      if (data.positions.size() + keys > std::size_t(std::numeric_limits<int>::max())) {
        omitted(*iterator, "Too many curve keys for the pinned Cycles geometry interface");
        continue;
      }
      const auto key = identities.read(*iterator, source.primitiveIndex(*iterator), index_ids);
      for (std::size_t piece = 0; piece < sampled.paths.size(); ++piece) {
        const auto &path = sampled.paths[piece];
        data.first_key.push_back(int(data.positions.size()));
        if (shading_attributes)
          data.randoms.push_back(identity_value("curve:" + key));
        if (native)
          data.material_slots.push_back(bindings.curve(*iterator));
        if (correspondence) {
          correspondence->curves.keys.push_back(key + "/piece:" + std::to_string(piece));
          correspondence->curves.index_fallback |= key.starts_with("index:");
        }
        for (std::size_t index = 0; index < path.size(); ++index) {
          const auto &sample = path[index];
          const auto sample_vector = [&](const auto &read, bool geometry = false) {
            double x = 0, y = 0, z = 0;
            for (std::size_t n = 0; n < sample.vertices.size(); ++n) {
              const auto value = read(sample.vertices[n]);
              const double weight =
                  geometry && !sample.geometry_weights.empty() ? sample.geometry_weights[n] : sample.weights[n];
              x += weight * value.x;
              y += weight * value.y;
              z += weight * value.z;
            }
            return Float3{float(x), float(y), float(z)};
          };
          double width = 0;
          if (width_owner == GA_ATTRIB_POINT || width_owner == GA_ATTRIB_VERTEX)
            for (std::size_t n = 0; n < sample.vertices.size(); ++n)
              width += double(sample.weights[n]) * widths.read(width_owner == GA_ATTRIB_VERTEX
                                                                   ? sample.vertices[n]
                                                                   : source.vertexPoint(sample.vertices[n]),
                                                               request.curve_width);
          else
            width = widths.read(width_owner == GA_ATTRIB_PRIMITIVE ? *iterator : GA_DETAIL_OFFSET, request.curve_width);
          data.positions.push_back(sample.position);
          data.radii.push_back(.5f * widths.checked(float(width), request.curve_width));
          named.read_interpolated(source, sample.vertices, sample.weights, *iterator);
          if (colors)
            data.colors.push_back(colors.checked(sample_vector(
                [&](GA_Offset vertex) { return colors.read_element(vertex, source.vertexPoint(vertex), *iterator); })));
          if (index == 0 && uv) {
            const auto value = uv.checked(sample_vector(
                [&](GA_Offset vertex) { return uv.read(vertex_uv ? vertex : source.vertexPoint(vertex)); }));
            data.uv.push_back({value.x, value.y});
          }
          if (velocities) {
            // Point v extrapolates control positions before curve
            // evaluation. Ordinary shader fields keep Houdini's
            // non-rational attribute interpolation above.
            const auto value =
                sample_vector([&](GA_Offset vertex) { return vector(velocity.get(source.vertexPoint(vertex))); }, true);
            invalid_sampled_velocity |= !std::isfinite(value.x) || !std::isfinite(value.y) || !std::isfinite(value.z);
            velocities->push_back(value);
            if (acceleration.isValid())
              accelerations->push_back(sample_vector(
                  [&](GA_Offset vertex) { return vector(acceleration.get(source.vertexPoint(vertex))); }, true));
          }
          if (correspondence)
            correspondence->keys.keys.push_back("sample:" + std::to_string(index));
        }
      }
      continue;
    }
    const auto *curve = dynamic_cast<const GEO_PrimPoly *>(source.getPrimitive(*iterator));
    if (!curve || curve->isClosed())
      throw std::logic_error("Curve partition contains an unsupported primitive: " + request.sop_path);
    if (curve->getVertexCount() < 2) {
      omitted(*iterator, "An open curve requires at least two vertices");
      continue;
    }
    bool finite = true;
    for (GA_Size index = 0; index < curve->getVertexCount(); ++index) {
      const auto p = point(source, curve->getPointOffset(index));
      finite &= std::isfinite(p.x) && std::isfinite(p.y) && std::isfinite(p.z);
    }
    if (!finite) {
      omitted(*iterator, "Curve contains a non-finite position");
      continue;
    }
    if (data.positions.size() + curve->getVertexCount() > std::size_t(std::numeric_limits<int>::max()))
      throw std::invalid_argument("Too many curve keys for the pinned Cycles geometry interface");
    const auto key = identities.read(*iterator, data.first_key.size(), index_ids);
    data.first_key.push_back(int(data.positions.size()));
    if (shading_attributes)
      data.randoms.push_back(identity_value("curve:" + key));
    if (native)
      data.material_slots.push_back(bindings.curve(*iterator));
    if (correspondence) {
      correspondence->curves.keys.push_back(key);
      correspondence->curves.index_fallback |= key.starts_with("index:");
    }
    for (GA_Size index = 0; index < curve->getVertexCount(); ++index) {
      const auto vertex = curve->getVertexOffset(index), offset = curve->getPointOffset(index);
      const float width = widths.read(width_owner == GA_ATTRIB_VERTEX      ? vertex
                                      : width_owner == GA_ATTRIB_POINT     ? offset
                                      : width_owner == GA_ATTRIB_PRIMITIVE ? *iterator
                                                                           : GA_DETAIL_OFFSET,
                                      request.curve_width);
      data.positions.push_back(point(source, offset));
      data.radii.push_back(width * 0.5f);
      named.read(vertex, offset, *iterator);
      if (velocities)
        velocities->push_back(vector(velocity.get(offset)));
      if (velocities && acceleration.isValid())
        accelerations->push_back(vector(acceleration.get(offset)));
      if (correspondence) {
        std::uint64_t fallback = 0;
        const auto identity_key = key_identities.read(offset, index, fallback);
        correspondence->keys.keys.push_back(identity_key);
        correspondence->keys.index_fallback |= fallback > 0;
      }
      if (colors)
        data.colors.push_back(colors.read_element(vertex, offset, *iterator));
      if (index == 0 && uv) {
        const auto value = uv.read(vertex_uv ? vertex : offset);
        data.uv.push_back({value.x, value.y});
      }
    }
  }
  widths.finish(true, request.sop_path, request.geometry_key, diagnostics);
  if (failed_curves && diagnostics)
    diagnostics->push_back({"geometry.curve_unavailable", request.sop_path, request.geometry_key,
                            "Omitted " + std::to_string(failed_curves) +
                                " curve(s); primitive indices: " + failed_examples + "; first issue: " + first_failure,
                            "Skipping only failed curves; retaining usable curves and other geometry"});
  if (invalid_sampled_velocity && velocities) {
    velocities->clear();
    if (accelerations)
      accelerations->clear();
    if (diagnostics)
      diagnostics->push_back(
          {"geometry.invalid_velocity", request.sop_path, request.geometry_key,
           "Curve interpolation produced a non-finite velocity",
           "Disabling this component's velocity blur; retaining geometry and independent transform motion", "warning",
           "v"});
  }
  colors.finish(data.colors, diagnostics);
  uv.finish(data.uv, diagnostics);
  named.finish(data.attributes, request, diagnostics);
  if (native)
    data.materials = bindings.materials();
  if (materials)
    *materials = native ? bindings.references() : std::vector<NativeMaterialReference>{};
  return data;
}
// Open polygon curves on all cores. The serial converter above stays the
// reference and takes every other input (Bezier/NURBS, closed or other
// primitives, an exception while resolving materials, too many keys).
// Three passes: (A) per primitive, in parallel: kind, key count, finiteness
// and the const material assignment; (B) on this thread, in primitive order:
// omitted curves, key offsets and material slots, exactly as the serial loop
// decides them; (C) per curve, in parallel: every value into its own slot.
// A field's first failure in serial order (the lowest failing slot) chooses
// its diagnostic, as the serial loop's first failure did.
CurveData capture_curves(const GU_Detail &source, const GeometryRequest &request, std::uint64_t revision,
                         std::uint64_t &index_ids, std::vector<Float3> *velocities,
                         NativeCurveIdentities *correspondence, bool shading_attributes,
                         std::vector<NativeMaterialReference> *materials, const NativeGeometrySelection *selection,
                         std::vector<SourceDiagnostic> *diagnostics, std::vector<Float3> *accelerations) {
  const auto serial = [&] {
    ++curve_conversion_counters.serial;
    return capture_curves_serial(source, request, revision, index_ids, velocities, correspondence, shading_attributes,
                                 materials, selection, diagnostics, accelerations);
  };
  const auto primitives = selection ? selection->primitive_range(source) : source.getPrimitiveRange();
  std::vector<GA_Offset> offsets;
  offsets.reserve(std::size_t(primitives.getEntries()));
  {
    GA_Offset begin, end;
    for (GA_Iterator it(primitives); it.blockAdvance(begin, end);)
      for (auto offset = begin; offset < end; ++offset)
        offsets.push_back(offset);
  }
  const std::size_t count = offsets.size();
  const bool native = request.material_mode == NativeMaterialMode::Native;
  NativeMaterialBindings bindings(source, request, selection);
  // The serial converter builds its bindings, then checks v, before it reads anything.
  const auto *velocity_attribute = source.findPointAttribute("v");
  if (velocities) {
    numeric(velocity_attribute, "v", 3);
    if (!velocity_attribute && source.getNumPoints())
      throw std::invalid_argument("Velocity blur requires a three-component float point v attribute: " +
                                  request.sop_path);
  }
  // Pass A.
  enum : unsigned char { usable, too_short, non_finite };
  struct Run {
    std::size_t first{};
    NativeMaterialAssignment value;
  };
  constexpr std::size_t chunk = 4096;
  const std::size_t chunks = (count + chunk - 1) / chunk;
  std::vector<unsigned char> status(count);
  std::vector<GA_Size> sizes(count);
  // Assignments change rarely along the primitive order: each chunk keeps
  // only the usable primitives whose assignment differs from the previous one.
  std::vector<std::vector<Run>> runs(native ? chunks : 0);
  std::atomic<bool> unusual{false};
  {
    const GA_Offset *offset_in = offsets.data();
    unsigned char *status_out = status.data();
    GA_Size *size_out = sizes.data();
    std::vector<Run> *run_out = runs.data();
    curve_slots(chunks, 1, [&](std::size_t begin, std::size_t end) {
      for (auto k = begin; k < end; ++k) {
        const auto last = std::min(count, (k + 1) * chunk);
        for (auto i = k * chunk; i < last; ++i) {
          if (unusual.load(std::memory_order_relaxed))
            return;
          const auto *curve = dynamic_cast<const GEO_PrimPoly *>(source.getPrimitive(offset_in[i]));
          if (!curve || curve->isClosed()) {
            unusual.store(true, std::memory_order_relaxed);
            return;
          }
          const auto vertices = curve->getVertexCount();
          size_out[i] = vertices;
          if (vertices < 2) {
            status_out[i] = too_short;
            continue;
          }
          bool finite = true;
          for (GA_Size index = 0; index < vertices; ++index) {
            const auto p = point(source, curve->getPointOffset(index));
            finite &= std::isfinite(p.x) && std::isfinite(p.y) && std::isfinite(p.z);
          }
          if (!finite) {
            status_out[i] = non_finite;
            continue;
          }
          status_out[i] = usable;
          if (!native)
            continue;
          try {
            auto value = bindings.curve_assignment(offset_in[i]);
            auto &list = run_out[k];
            if (list.empty() || list.back().value != value)
              list.push_back({i, std::move(value)});
          } catch (...) {
            unusual.store(true, std::memory_order_relaxed);
            return;
          }
        }
      }
    });
  }
  if (unusual.load())
    return serial();
  // Pass B.
  CurveData data;
  data.key = "curves:" + request.geometry_key;
  data.revision = revision;
  data.material = request.material_key;
  data.shape = request.curve_shape;
  if (native) {
    data.material.clear();
    data.material_slots.reserve(count);
  }
  std::size_t failed_curves = 0;
  std::string failed_examples, first_failure;
  const auto omitted = [&](GA_Offset primitive, const std::string &reason) {
    if (failed_curves++ < 16) {
      if (!failed_examples.empty())
        failed_examples += ", ";
      failed_examples += std::to_string(source.primitiveIndex(primitive));
    }
    if (first_failure.empty())
      first_failure = reason;
  };
  std::vector<GA_Offset> kept;
  kept.reserve(count);
  data.first_key.reserve(count);
  std::size_t keys = 0, run = 0, run_chunk = std::numeric_limits<std::size_t>::max();
  const NativeMaterialAssignment *bound = nullptr;
  int bound_slot = -1;
  for (std::size_t i = 0; i < count; ++i) {
    if (status[i] == too_short) {
      omitted(offsets[i], "An open curve requires at least two vertices");
      continue;
    }
    if (status[i] == non_finite) {
      omitted(offsets[i], "Curve contains a non-finite position");
      continue;
    }
    if (keys + std::size_t(sizes[i]) > std::size_t(std::numeric_limits<int>::max()))
      return serial();
    data.first_key.push_back(int(keys));
    kept.push_back(offsets[i]);
    keys += std::size_t(sizes[i]);
    if (!native)
      continue;
    // The first usable primitive of a chunk starts that chunk's first run.
    const auto &list = runs[i / chunk];
    if (run_chunk != i / chunk) {
      run_chunk = i / chunk;
      run = 0;
    }
    while (run + 1 < list.size() && list[run + 1].first <= i)
      ++run;
    const auto &value = list.at(run).value;
    if (!bound || *bound != value) {
      // bindings.curve() is binding(curve_assignment()); equal assignments
      // intern the same slot, so it is called only when the value changes.
      try {
        bound_slot = bindings.curve(offsets[i]);
      } catch (...) {
        return serial();
      }
      bound = &value;
    }
    data.material_slots.push_back(bound_slot);
  }
  runs.clear();
  // Pass C.
  NativeNamedAttributes named(source, request, true, shading_attributes);
  NativeSizeAttribute widths(native_size_attribute(source, true));
  const auto width_owner = widths.attribute ? widths.attribute->getOwner() : GA_ATTRIB_DETAIL;
  NativeShadingAttribute colors(shading_attributes ? named_attribute(source, "Cd", true) : nullptr, false,
                                request.sop_path, request.geometry_key);
  const auto *vertex_uv = shading_attributes ? source.findVertexAttribute("uv") : nullptr;
  NativeShadingAttribute uv(vertex_uv            ? vertex_uv
                            : shading_attributes ? source.findPointAttribute("uv")
                                                 : nullptr,
                            true, request.sop_path, request.geometry_key);
  GA_ROHandleV3 velocity(velocity_attribute);
  const auto *accel_attribute = velocities && accelerations ? source.findPointAttribute("accel") : nullptr;
  GA_ROHandleV3 acceleration(accel_attribute && accel_attribute->getStorageClass() == GA_STORECLASS_FLOAT &&
                                     accel_attribute->getTupleSize() == 3
                                 ? accel_attribute
                                 : nullptr);
  const auto *name_attribute = source.findPrimitiveAttribute("name"),
             *id_attribute = source.findPrimitiveAttribute("id");
  NativeIdentityReader identities(name_attribute, id_attribute, primitives);
  const auto *key_names_attribute = correspondence ? source.findPointAttribute("name") : nullptr,
             *key_ids_attribute = correspondence ? source.findPointAttribute("id") : nullptr;
  NativeIdentityReader key_identities(key_names_attribute, key_ids_attribute,
                                      selection ? selection->point_range(source) : source.getPointRange());
  const std::size_t curves = kept.size();
  data.positions.resize(keys);
  data.radii.resize(keys);
  if (shading_attributes)
    data.randoms.resize(curves);
  const bool read_colors = bool(colors), read_uv = bool(uv);
  if (read_colors)
    data.colors.resize(keys);
  if (read_uv)
    data.uv.resize(curves);
  // Outputs may arrive non-empty: append, as the serial loop does.
  const bool read_velocity = velocities != nullptr, read_acceleration = velocities && acceleration.isValid();
  const std::size_t velocity_base = read_velocity ? velocities->size() : 0,
                    acceleration_base = read_acceleration ? accelerations->size() : 0;
  if (read_velocity)
    velocities->resize(velocity_base + keys);
  if (read_acceleration)
    accelerations->resize(acceleration_base + keys);
  std::size_t curve_key_base = 0, point_key_base = 0;
  if (correspondence) {
    curve_key_base = correspondence->curves.keys.size();
    point_key_base = correspondence->keys.keys.size();
    correspondence->curves.keys.resize(curve_key_base + curves);
    correspondence->keys.keys.resize(point_key_base + keys);
  }
  struct Field {
    std::size_t index{};
    const GA_Attribute *attribute{};
    const GA_AIFTuple *tuple{};
    GA_AttributeOwner owner{};
    int components{};
    Float3 *values{};
  };
  std::vector<Field> fields;
  for (std::size_t f = 0; f < named.field_count(); ++f)
    if (named.slot_readable(f)) {
      const auto *attribute = named.slot_attribute(f);
      fields.push_back({f, attribute, attribute->getAIFTuple(), attribute->getOwner(), named.slot_components(f),
                        named.slot_resize(f, keys)});
    }
  constexpr auto none = std::numeric_limits<std::uint64_t>::max();
  const auto lower = [](std::atomic<std::uint64_t> &target, std::uint64_t value) {
    auto current = target.load(std::memory_order_relaxed);
    while (value < current && !target.compare_exchange_weak(current, value, std::memory_order_relaxed)) {
    }
  };
  std::atomic<std::uint64_t> color_failure{none}, uv_failure{none}, fallbacks{0};
  std::vector<std::atomic<std::uint64_t>> field_failures(fields.size());
  for (auto &failure : field_failures)
    failure.store(none, std::memory_order_relaxed);
  std::atomic<bool> invalid_width{false}, curve_index_fallback{false}, key_index_fallback{false};
  // A detail Cd or width is one value: every key would read GA_DETAIL_OFFSET.
  const bool constant_color = read_colors && colors.owner == GA_ATTRIB_DETAIL;
  Float3 detail_color{};
  const int detail_color_code = constant_color ? colors.peek(GA_DETAIL_OFFSET, detail_color) : 0;
  const float fallback_width = request.curve_width;
  const bool width_values = widths.values.isValid();
  // Raw pointers: tasks must not reload vector members through shared objects.
  const GA_Offset *const kept_in = kept.data();
  const int *const first_keys = data.first_key.data();
  Float3 *const positions_out = data.positions.data();
  float *const radii_out = data.radii.data();
  float *const randoms_out = shading_attributes ? data.randoms.data() : nullptr;
  Float3 *const colors_out = read_colors ? data.colors.data() : nullptr;
  Float2 *const uv_out = read_uv ? data.uv.data() : nullptr;
  Float3 *const velocities_out = read_velocity ? velocities->data() + velocity_base : nullptr;
  Float3 *const accelerations_out = read_acceleration ? accelerations->data() + acceleration_base : nullptr;
  std::string *const curve_keys_out = correspondence ? correspondence->curves.keys.data() + curve_key_base : nullptr;
  std::string *const point_keys_out = correspondence ? correspondence->keys.keys.data() + point_key_base : nullptr;
  const Field *const field_in = fields.data();
  const std::size_t field_total = fields.size();
  curve_slots(curves, 1024, [&](std::size_t begin, std::size_t end) {
    std::uint64_t local_fallbacks = 0, local_color = none, local_uv = none;
    bool local_width = false, local_curve_index = false, local_key_index = false;
    std::vector<std::uint64_t> local_fields(field_total, none);
    for (auto c = begin; c < end; ++c) {
      const auto primitive = kept_in[c];
      const auto *curve = static_cast<const GEO_PrimPoly *>(source.getPrimitive(primitive));
      std::uint64_t fallback = 0;
      const auto key = identities.read(primitive, std::int64_t(c), fallback);
      local_fallbacks += fallback;
      if (randoms_out)
        randoms_out[c] = identity_value("curve:" + key);
      if (curve_keys_out) {
        local_curve_index |= key.starts_with("index:");
        curve_keys_out[c] = key;
      }
      const auto first = std::size_t(first_keys[c]);
      const auto vertices = curve->getVertexCount();
      for (GA_Size index = 0; index < vertices; ++index) {
        const auto slot = first + std::size_t(index);
        const auto vertex = curve->getVertexOffset(index), offset = curve->getPointOffset(index);
        float width = fallback_width;
        if (width_values) {
          const float value = widths.values.get(width_owner == GA_ATTRIB_VERTEX      ? vertex
                                                : width_owner == GA_ATTRIB_POINT     ? offset
                                                : width_owner == GA_ATTRIB_PRIMITIVE ? primitive
                                                                                     : GA_DETAIL_OFFSET);
          if (NativeSizeAttribute::valid(value))
            width = value;
          else
            local_width = true;
        }
        positions_out[slot] = point(source, offset);
        radii_out[slot] = width * 0.5f;
        for (std::size_t f = 0; f < field_total; ++f)
          if (local_fields[f] == none) {
            const auto &field = field_in[f];
            const auto element = field.owner == GA_ATTRIB_VERTEX      ? vertex
                                 : field.owner == GA_ATTRIB_POINT     ? offset
                                 : field.owner == GA_ATTRIB_PRIMITIVE ? primitive
                                                                      : GA_DETAIL_OFFSET;
            fpreal32 values[3]{};
            int code = 0;
            if (element == GA_INVALID_OFFSET || !field.tuple->get(field.attribute, element, values, field.components))
              code = 1;
            else if (!std::isfinite(values[0]) || !std::isfinite(values[1]) || !std::isfinite(values[2]))
              code = 2;
            if (code)
              local_fields[f] = std::uint64_t(slot) << 2 | std::uint64_t(code);
            else
              field.values[slot] = {values[0], values[1], values[2]};
          }
        if (velocities_out)
          velocities_out[slot] = vector(velocity.get(offset));
        if (accelerations_out)
          accelerations_out[slot] = vector(acceleration.get(offset));
        if (point_keys_out) {
          std::uint64_t key_fallback = 0;
          point_keys_out[slot] = key_identities.read(offset, index, key_fallback);
          local_key_index |= key_fallback > 0;
        }
        if (colors_out && local_color == none) {
          int code;
          if (constant_color) {
            code = detail_color_code;
            colors_out[slot] = detail_color;
          } else
            code = colors.peek(colors.element(vertex, offset, primitive), colors_out[slot]);
          if (code)
            local_color = std::uint64_t(slot) << 2 | std::uint64_t(code);
        }
        if (index == 0 && uv_out && local_uv == none) {
          Float3 value;
          if (const int code = uv.peek(vertex_uv ? vertex : offset, value))
            local_uv = std::uint64_t(c) << 2 | std::uint64_t(code);
          uv_out[c] = {value.x, value.y};
        }
      }
    }
    // Within a task slots ascend, so each local failure is the task's lowest.
    if (local_color != none)
      lower(color_failure, local_color);
    if (local_uv != none)
      lower(uv_failure, local_uv);
    for (std::size_t f = 0; f < field_total; ++f)
      if (local_fields[f] != none)
        lower(field_failures[f], local_fields[f]);
    if (local_fallbacks)
      fallbacks.fetch_add(local_fallbacks, std::memory_order_relaxed);
    if (local_width)
      invalid_width.store(true, std::memory_order_relaxed);
    if (local_curve_index)
      curve_index_fallback.store(true, std::memory_order_relaxed);
    if (local_key_index)
      key_index_fallback.store(true, std::memory_order_relaxed);
  });
  ++curve_conversion_counters.parallel;
  index_ids += fallbacks.load();
  if (invalid_width.load())
    widths.invalid = true;
  if (correspondence) {
    correspondence->curves.index_fallback |= curve_index_fallback.load();
    correspondence->keys.index_fallback |= key_index_fallback.load();
  }
  if (const auto failure = color_failure.load(); failure != none)
    colors.fail_code(int(failure & 3));
  if (const auto failure = uv_failure.load(); failure != none)
    uv.fail_code(int(failure & 3));
  // A serial field reads every key up to and including its first failure.
  for (std::size_t f = 0; f < field_total; ++f) {
    const auto failure = field_failures[f].load();
    named.slot_finish(fields[f].index, failure == none ? 0 : int(failure & 3),
                      failure == none ? keys : std::size_t(failure >> 2) + 1);
  }
  widths.finish(true, request.sop_path, request.geometry_key, diagnostics);
  if (failed_curves && diagnostics)
    diagnostics->push_back({"geometry.curve_unavailable", request.sop_path, request.geometry_key,
                            "Omitted " + std::to_string(failed_curves) +
                                " curve(s); primitive indices: " + failed_examples + "; first issue: " + first_failure,
                            "Skipping only failed curves; retaining usable curves and other geometry"});
  colors.finish(data.colors, diagnostics);
  uv.finish(data.uv, diagnostics);
  named.finish(data.attributes, request, diagnostics);
  if (native)
    data.materials = bindings.materials();
  if (materials)
    *materials = native ? bindings.references() : std::vector<NativeMaterialReference>{};
  return data;
}
} // namespace cycles_houdini
